from __future__ import annotations

from datetime import date
from pathlib import Path

from nanobot.knowledge.wa_group_kb import (
    archive_inbound_message,
    build_runtime_context_lines,
    get_whatsapp_kb_groups,
    group_root,
    normalize_group_id,
    parse_whatsapp_knowledge_config,
    write_daily_outputs,
)


def test_normalize_group_id_handles_jid_variants() -> None:
    assert normalize_group_id("120363038334877727@g.us") == "120363038334877727"
    assert normalize_group_id("120363038334877727:15@g.us") == "120363038334877727"
    assert normalize_group_id("120363038334877727") == "120363038334877727"


def test_parse_enabled_groups_from_channels_config() -> None:
    channels = {
        "whatsapp": {
            "enabled": True,
            "knowledge": {
                "enabled": True,
                "groups": {
                    "120363038334877727": {
                        "enabled": True,
                        "retrievalTopK": 9,
                    },
                    "120363000000000000": {
                        "enabled": False,
                    },
                },
            },
        }
    }

    groups = get_whatsapp_kb_groups(channels)
    assert "120363038334877727" in groups
    assert groups["120363038334877727"].retrieval_top_k == 9
    assert "120363000000000000" not in groups


def test_archive_then_retrieve_runtime_context(tmp_path: Path) -> None:
    workspace = tmp_path
    group_id = "120363038334877727"

    archive_inbound_message(
        workspace=workspace,
        group_id=group_id,
        chat_jid=f"{group_id}@g.us",
        message_id="m1",
        sender_jid="62811@s.whatsapp.net",
        sender_name="62811",
        content="Useful AI vision model list https://example.com/vision-list",
        participant="",
        timestamp="2026-03-30T10:00:00",
        media=[],
        metadata={},
    )

    root = group_root(workspace, group_id)
    (root / "facts").mkdir(parents=True, exist_ok=True)
    (root / "facts" / "2026-03-30.md").write_text(
        "# Facts\n\nAI Vision Model references: https://example.com/vision-list\n",
        encoding="utf-8",
    )

    lines = build_runtime_context_lines(
        workspace=workspace,
        group_id=group_id,
        query="latest related url link projects to AI Vision Model",
        top_k=5,
    )

    assert lines
    assert any("facts/2026-03-30.md" in ln for ln in lines)


def test_archive_daily_cap_keeps_recent_lines(tmp_path: Path) -> None:
    workspace = tmp_path
    gid = "120363038334877727"

    for i in range(5):
        archive_inbound_message(
            workspace=workspace,
            group_id=gid,
            chat_jid=f"{gid}@g.us",
            message_id=f"m{i}",
            sender_jid="62811@s.whatsapp.net",
            sender_name="62811",
            content=f"msg {i}",
            participant="",
            timestamp="2026-03-30T10:00:00",
            media=[],
            metadata={},
            max_daily_messages=3,
        )

    raw = group_root(workspace, gid) / "raw" / f"{date(2026,3,30).isoformat()}.jsonl"
    lines = [ln for ln in raw.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 3
    assert "m4" in lines[-1]


def test_archive_uses_group_timezone_for_day_partition(tmp_path: Path) -> None:
    workspace = tmp_path
    gid = "120363038334877727"

    # 2026-03-30 17:30:00 UTC == 2026-03-31 00:30:00 Asia/Jakarta
    archive_inbound_message(
        workspace=workspace,
        group_id=gid,
        chat_jid=f"{gid}@g.us",
        message_id="m-tz-1",
        sender_jid="62811@s.whatsapp.net",
        sender_name="62811",
        content="boundary message",
        participant="",
        timestamp=1774891800,
        media=[],
        metadata={},
        timezone_name="Asia/Jakarta",
    )

    raw_wib = group_root(workspace, gid) / "raw" / "2026-03-31.jsonl"
    raw_utc = group_root(workspace, gid) / "raw" / "2026-03-30.jsonl"
    assert raw_wib.exists()
    assert not raw_utc.exists()


def test_parse_group_config_invalid_max_daily_messages_falls_back() -> None:
    parsed = parse_whatsapp_knowledge_config(
        {
            "knowledge": {
                "enabled": True,
                "groups": {
                    "120363038334877727": {
                        "enabled": True,
                        "maxDailyMessages": "not-a-number",
                    }
                },
            }
        }
    )

    assert parsed.enabled is True
    assert parsed.groups is not None
    assert parsed.groups["120363038334877727"].max_daily_messages == 5000


def test_parse_group_config_recap_fields() -> None:
    parsed = parse_whatsapp_knowledge_config(
        {
            "knowledge": {
                "enabled": True,
                "groups": {
                    "120363038334877727": {
                        "enabled": True,
                        "recapEnabled": True,
                        "recapChannel": "telegram",
                        "recapChatId": "1224491205",
                    }
                },
            }
        }
    )

    grp = parsed.groups["120363038334877727"]
    assert grp.recap_enabled is True
    assert grp.recap_channel == "telegram"
    assert grp.recap_chat_id == "1224491205"


def test_parse_group_config_deep_mode_fields() -> None:
    parsed = parse_whatsapp_knowledge_config(
        {
            "knowledge": {
                "enabled": True,
                "groups": {
                    "120363038334877727": {
                        "enabled": True,
                        "deepMode": {
                            "enabled": True,
                            "maxLinksPerDay": 12,
                            "timeoutSeconds": 9,
                            "maxCharsPerPage": 24000,
                        },
                    }
                },
            }
        }
    )

    grp = parsed.groups["120363038334877727"]
    assert grp.deep_mode.enabled is True
    assert grp.deep_mode.max_links_per_day == 12
    assert grp.deep_mode.timeout_seconds == 9
    assert grp.deep_mode.max_chars_per_page == 24000


def test_write_daily_outputs_is_idempotent_for_same_day_index(tmp_path: Path) -> None:
    gid = "120363038334877727"
    root = group_root(tmp_path, gid)
    day = date(2026, 3, 30)

    kwargs = dict(
        root=root,
        day=day,
        summary_md="# Daily Summary\n",
        facts_md="# Facts\n",
        links=[{"url": "https://example.com", "src": "raw/2026-03-30.jsonl#m1", "topic": ""}],
        topics=[{"topic": "vision", "count": "1", "day": "2026-03-30"}],
        entities=[{"entity": "VisionAPI", "count": "1", "day": "2026-03-30"}],
    )

    write_daily_outputs(**kwargs)
    write_daily_outputs(**kwargs)

    links_md = (root / "index" / "links.md").read_text(encoding="utf-8")
    assert links_md.count("## 2026-03-30") == 1
