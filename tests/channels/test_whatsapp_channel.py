"""Tests for WhatsApp channel outbound media support."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.channels.whatsapp import WhatsAppChannel
from nanobot.knowledge.wa_group_kb import group_root


def _make_channel(tmp_path: Path | None = None, monkeypatch=None, config: dict | None = None) -> WhatsAppChannel:
    bus = MagicMock()

    if tmp_path is not None and monkeypatch is not None:
        class _Loaded:
            workspace_path = tmp_path

        monkeypatch.setattr("nanobot.config.loader.load_config", lambda *args, **kwargs: _Loaded())

    base_cfg = {"enabled": True}
    if config:
        base_cfg.update(config)

    ch = WhatsAppChannel(base_cfg, bus)
    ch._ws = AsyncMock()
    ch._connected = True
    return ch


@pytest.mark.asyncio
async def test_send_text_only(tmp_path: Path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    msg = OutboundMessage(channel="whatsapp", chat_id="123@s.whatsapp.net", content="hello")

    await ch.send(msg)

    ch._ws.send.assert_called_once()
    payload = json.loads(ch._ws.send.call_args[0][0])
    assert payload["type"] == "send"
    assert payload["text"] == "hello"


@pytest.mark.asyncio
async def test_send_media_dispatches_send_media_command(tmp_path: Path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    msg = OutboundMessage(
        channel="whatsapp",
        chat_id="123@s.whatsapp.net",
        content="check this out",
        media=["/tmp/photo.jpg"],
    )

    await ch.send(msg)

    assert ch._ws.send.call_count == 2
    text_payload = json.loads(ch._ws.send.call_args_list[0][0][0])
    media_payload = json.loads(ch._ws.send.call_args_list[1][0][0])

    assert text_payload["type"] == "send"
    assert text_payload["text"] == "check this out"

    assert media_payload["type"] == "send_media"
    assert media_payload["filePath"] == "/tmp/photo.jpg"
    assert media_payload["mimetype"] == "image/jpeg"
    assert media_payload["fileName"] == "photo.jpg"


@pytest.mark.asyncio
async def test_send_media_only_no_text(tmp_path: Path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    msg = OutboundMessage(
        channel="whatsapp",
        chat_id="123@s.whatsapp.net",
        content="",
        media=["/tmp/doc.pdf"],
    )

    await ch.send(msg)

    ch._ws.send.assert_called_once()
    payload = json.loads(ch._ws.send.call_args[0][0])
    assert payload["type"] == "send_media"
    assert payload["mimetype"] == "application/pdf"


@pytest.mark.asyncio
async def test_send_multiple_media(tmp_path: Path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    msg = OutboundMessage(
        channel="whatsapp",
        chat_id="123@s.whatsapp.net",
        content="",
        media=["/tmp/a.png", "/tmp/b.mp4"],
    )

    await ch.send(msg)

    assert ch._ws.send.call_count == 2
    p1 = json.loads(ch._ws.send.call_args_list[0][0][0])
    p2 = json.loads(ch._ws.send.call_args_list[1][0][0])
    assert p1["mimetype"] == "image/png"
    assert p2["mimetype"] == "video/mp4"


@pytest.mark.asyncio
async def test_send_when_disconnected_is_noop(tmp_path: Path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch)
    ch._connected = False

    msg = OutboundMessage(
        channel="whatsapp",
        chat_id="123@s.whatsapp.net",
        content="hello",
        media=["/tmp/x.jpg"],
    )
    await ch.send(msg)

    ch._ws.send.assert_not_called()


@pytest.mark.asyncio
async def test_group_policy_mention_skips_unmentioned_group_message(tmp_path: Path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch, {"groupPolicy": "mention"})
    ch._handle_message = AsyncMock()

    await ch._handle_bridge_message(
        json.dumps(
            {
                "type": "message",
                "id": "m1",
                "sender": "12345@g.us",
                "pn": "user@s.whatsapp.net",
                "content": "hello group",
                "timestamp": 1,
                "isGroup": True,
                "wasMentioned": False,
            }
        )
    )

    ch._handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_group_policy_mention_accepts_mentioned_group_message(tmp_path: Path, monkeypatch):
    ch = _make_channel(tmp_path, monkeypatch, {"groupPolicy": "mention"})
    ch._handle_message = AsyncMock()

    await ch._handle_bridge_message(
        json.dumps(
            {
                "type": "message",
                "id": "m1",
                "sender": "12345@g.us",
                "pn": "user@s.whatsapp.net",
                "content": "hello @bot",
                "timestamp": 1,
                "isGroup": True,
                "wasMentioned": True,
            }
        )
    )

    ch._handle_message.assert_awaited_once()
    kwargs = ch._handle_message.await_args.kwargs
    assert kwargs["chat_id"] == "12345@g.us"
    assert kwargs["sender_id"] == "user"


@pytest.mark.asyncio
async def test_kb_archives_configured_group_messages(tmp_path: Path, monkeypatch):
    group_id = "120363038334877727"
    ch = _make_channel(
        tmp_path,
        monkeypatch,
        {
            "knowledge": {
                "enabled": True,
                "groups": {
                    group_id: {
                        "enabled": True,
                        "maxDailyMessages": 100,
                    }
                },
            }
        },
    )
    ch._handle_message = AsyncMock()

    await ch._handle_bridge_message(
        json.dumps(
            {
                "type": "message",
                "id": "m-kb-1",
                "sender": f"{group_id}@g.us",
                "pn": "62811@s.whatsapp.net",
                "content": "latest AI Vision Model link https://example.com/vision",
                "timestamp": 1711886400,
                "isGroup": True,
                "wasMentioned": False,
            }
        )
    )

    raw_dir = group_root(tmp_path, group_id) / "raw"
    files = sorted(raw_dir.glob("*.jsonl"))
    assert files, "expected JSONL archive to be created"
    body = files[-1].read_text(encoding="utf-8")
    assert "m-kb-1" in body
    assert "https://example.com/vision" in body


@pytest.mark.asyncio
async def test_kb_does_not_archive_unlisted_group(tmp_path: Path, monkeypatch):
    ch = _make_channel(
        tmp_path,
        monkeypatch,
        {
            "knowledge": {
                "enabled": True,
                "groups": {
                    "120363038334877727": {"enabled": True},
                },
            }
        },
    )
    ch._handle_message = AsyncMock()

    await ch._handle_bridge_message(
        json.dumps(
            {
                "type": "message",
                "id": "m-kb-2",
                "sender": "99999@g.us",
                "pn": "62811@s.whatsapp.net",
                "content": "hello world",
                "timestamp": 1711886400,
                "isGroup": True,
                "wasMentioned": False,
            }
        )
    )

    raw_dir = group_root(tmp_path, "99999") / "raw"
    assert not raw_dir.exists()
