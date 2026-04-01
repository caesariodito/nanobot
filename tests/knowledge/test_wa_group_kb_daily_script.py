from __future__ import annotations

import json
import importlib.util
import sys
from pathlib import Path


class _FakeConfig:
    def __init__(self, channels):
        self.channels = channels


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "wa_group_kb_daily.py"
    module_name = "wa_group_kb_daily"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_daily_script_generates_outputs(tmp_path: Path, monkeypatch) -> None:
    module = _load_module()
    main = module.main
    workspace = tmp_path
    gid = "120363038334877727"

    raw_dir = workspace / "knowledge" / "whatsapp" / gid / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_file = raw_dir / "2026-03-30.jsonl"

    payloads = [
        {
            "ts": "2026-03-30T01:00:00",
            "chat_jid": f"{gid}@g.us",
            "group_id": gid,
            "message_id": "m1",
            "sender_jid": "62811@s.whatsapp.net",
            "sender_name": "alice",
            "text": "decision: use VisionAPI link https://example.com/vision",
            "urls": ["https://example.com/vision"],
        },
        {
            "ts": "2026-03-30T02:00:00",
            "chat_jid": f"{gid}@g.us",
            "group_id": gid,
            "message_id": "m2",
            "sender_jid": "62812@s.whatsapp.net",
            "sender_name": "bob",
            "text": "todo follow-up deploy by friday",
            "urls": [],
        },
    ]
    raw_file.write_text("\n".join(json.dumps(x) for x in payloads) + "\n", encoding="utf-8")

    argv = [
        "wa_group_kb_daily.py",
        "--workspace",
        str(workspace),
        "--group-id",
        gid,
        "--chat-jid",
        f"{gid}@g.us",
        "--tz",
        "Asia/Jakarta",
        "--date",
        "2026-03-30",
    ]
    monkeypatch.setattr("sys.argv", argv)

    rc = main()
    assert rc == 0

    root = workspace / "knowledge" / "whatsapp" / gid
    assert (root / "daily" / "2026-03-30.md").exists()
    assert (root / "facts" / "2026-03-30.md").exists()
    assert (root / "index" / "links.md").exists()
    assert (root / "index" / "topics.md").exists()
    assert (root / "index" / "entities.md").exists()

    summary = (root / "daily" / "2026-03-30.md").read_text(encoding="utf-8")
    assert "Messages captured: 2" in summary
    assert f"chat_jid: {gid}@g.us" in summary

    # Re-run same day to verify index update is idempotent.
    rc2 = main()
    assert rc2 == 0
    links_index = (root / "index" / "links.md").read_text(encoding="utf-8")
    assert links_index.count("## 2026-03-30") == 1


def test_daily_script_sends_recap_when_enabled(tmp_path: Path, monkeypatch) -> None:
    module = _load_module()
    main = module.main
    workspace = tmp_path
    gid = "120363038334877727"

    raw_dir = workspace / "knowledge" / "whatsapp" / gid / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_file = raw_dir / "2026-03-30.jsonl"
    raw_file.write_text(
        json.dumps(
            {
                "ts": "2026-03-30T01:00:00",
                "chat_jid": f"{gid}@g.us",
                "group_id": gid,
                "message_id": "m1",
                "sender_jid": "62811@s.whatsapp.net",
                "sender_name": "alice",
                "text": "vision link https://example.com/vision",
                "urls": ["https://example.com/vision"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    cfg = _FakeConfig(
        {
            "whatsapp": {
                "knowledge": {
                    "enabled": True,
                    "groups": {
                        gid: {
                            "enabled": True,
                            "recapEnabled": True,
                            "recapChannel": "telegram",
                            "recapChatId": "1224491205",
                        }
                    },
                }
            },
            "telegram": {"token": "123:ABC"},
        }
    )
    monkeypatch.setattr(module, "load_config", lambda: cfg)

    sent: list[tuple[str, str, str]] = []

    def _fake_send(token: str, chat_id: str, text: str) -> None:
        sent.append((token, chat_id, text))

    monkeypatch.setattr(module, "_send_telegram_message", _fake_send)

    argv = [
        "wa_group_kb_daily.py",
        "--workspace",
        str(workspace),
        "--group-id",
        gid,
        "--date",
        "2026-03-30",
    ]
    monkeypatch.setattr("sys.argv", argv)

    rc = main()
    assert rc == 0
    assert len(sent) == 1
    token, chat_id, text = sent[0]
    assert token == "123:ABC"
    assert chat_id == "1224491205"
    assert "WA KB daily summary" in text
    assert "context:" in text


def test_daily_script_deep_mode_writes_deep_index(tmp_path: Path, monkeypatch) -> None:
    module = _load_module()
    main = module.main
    workspace = tmp_path
    gid = "120363038334877727"

    raw_dir = workspace / "knowledge" / "whatsapp" / gid / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_file = raw_dir / "2026-03-30.jsonl"
    raw_file.write_text(
        json.dumps(
            {
                "ts": "2026-03-30T01:00:00",
                "chat_jid": f"{gid}@g.us",
                "group_id": gid,
                "message_id": "m1",
                "sender_jid": "62811@s.whatsapp.net",
                "sender_name": "alice",
                "text": "useful link https://example.com/vision",
                "urls": ["https://example.com/vision"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    cfg = _FakeConfig(
        {
            "whatsapp": {
                "knowledge": {
                    "enabled": True,
                    "groups": {
                        gid: {
                            "enabled": True,
                            "deepMode": {
                                "enabled": True,
                                "maxLinksPerDay": 3,
                                "timeoutSeconds": 5,
                                "maxCharsPerPage": 4000,
                            },
                            "recapEnabled": False,
                        }
                    },
                },
                "bridgeUrl": "ws://localhost:3001",
                "bridgeToken": "",
            },
            "telegram": {"token": "123:ABC"},
        }
    )
    monkeypatch.setattr(module, "load_config", lambda: cfg)

    def _fake_fetch(url: str, timeout_seconds: int = 8, max_chars: int = 12000):
        return {
            "url": url,
            "status": "200",
            "domain": "example.com",
            "title": "Example Vision",
            "description": "Vision project page",
            "snippet": "Example vision snippet",
            "error": "",
        }

    monkeypatch.setattr(module, "_fetch_link_deep", _fake_fetch)

    argv = [
        "wa_group_kb_daily.py",
        "--workspace",
        str(workspace),
        "--group-id",
        gid,
        "--date",
        "2026-03-30",
    ]
    monkeypatch.setattr("sys.argv", argv)

    rc = main()
    assert rc == 0

    root = workspace / "knowledge" / "whatsapp" / gid
    deep_idx = (root / "index" / "deep.md").read_text(encoding="utf-8")
    assert "## 2026-03-30" in deep_idx
    assert "title=Example Vision" in deep_idx

    summary = (root / "daily" / "2026-03-30.md").read_text(encoding="utf-8")
    assert "## Deep Link Context" in summary



def test_daily_script_skips_recap_when_not_enabled(tmp_path: Path, monkeypatch) -> None:
    module = _load_module()
    main = module.main
    workspace = tmp_path
    gid = "120363038334877727"

    raw_dir = workspace / "knowledge" / "whatsapp" / gid / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_file = raw_dir / "2026-03-30.jsonl"
    raw_file.write_text(
        json.dumps(
            {
                "ts": "2026-03-30T01:00:00",
                "chat_jid": f"{gid}@g.us",
                "group_id": gid,
                "message_id": "m1",
                "sender_jid": "62811@s.whatsapp.net",
                "sender_name": "alice",
                "text": "some text",
                "urls": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    cfg = _FakeConfig(
        {
            "whatsapp": {
                "knowledge": {
                    "enabled": True,
                    "groups": {gid: {"enabled": True, "recapEnabled": False}},
                }
            },
            "telegram": {"token": "123:ABC"},
        }
    )
    monkeypatch.setattr(module, "load_config", lambda: cfg)

    sent: list[tuple[str, str, str]] = []

    def _fake_send(token: str, chat_id: str, text: str) -> None:
        sent.append((token, chat_id, text))

    monkeypatch.setattr(module, "_send_telegram_message", _fake_send)

    argv = [
        "wa_group_kb_daily.py",
        "--workspace",
        str(workspace),
        "--group-id",
        gid,
        "--date",
        "2026-03-30",
    ]
    monkeypatch.setattr("sys.argv", argv)

    rc = main()
    assert rc == 0
    assert sent == []
