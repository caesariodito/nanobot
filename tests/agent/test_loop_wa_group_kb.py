from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse


@pytest.fixture
def loop_with_wa_kb(tmp_path: Path) -> AgentLoop:
    from nanobot.providers.base import GenerationSettings

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
    provider.chat_stream_with_retry = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))

    channels_cfg = {
        "whatsapp": {
            "knowledge": {
                "enabled": True,
                "groups": {
                    "120363038334877727": {
                        "enabled": True,
                        "retrievalTopK": 3,
                    }
                },
            }
        }
    }

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        channels_config=channels_cfg,
    )
    return loop


def test_inject_runtime_context_for_string_payload(loop_with_wa_kb: AgentLoop) -> None:
    initial = [
        {
            "role": "user",
            "content": "[Runtime Context — metadata only, not instructions]\nCurrent Time: now\n\nquestion",
        }
    ]
    loop_with_wa_kb._inject_runtime_context(initial, "[WA Group Knowledge Matches]\n1. facts/...")
    final = initial[-1]["content"]
    assert "WA Group Knowledge Matches" in final
    assert final.endswith("question")


def test_inject_runtime_context_for_multimodal_payload(loop_with_wa_kb: AgentLoop) -> None:
    initial = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "[Runtime Context — metadata only, not instructions]\nCurrent Time: now",
                },
                {"type": "text", "text": "hello"},
            ],
        }
    ]
    loop_with_wa_kb._inject_runtime_context(initial, "[WA Group Knowledge Matches]\n1. facts/...")
    first = initial[-1]["content"][0]["text"]
    assert "WA Group Knowledge Matches" in first


@pytest.mark.asyncio
async def test_process_message_uses_wa_kb_for_configured_group(loop_with_wa_kb: AgentLoop, tmp_path: Path) -> None:
    from nanobot.knowledge.wa_group_kb import group_root

    root = group_root(tmp_path, "120363038334877727")
    (root / "facts").mkdir(parents=True, exist_ok=True)
    (root / "facts" / "2026-03-31.md").write_text(
        "latest AI Vision Model link https://example.com/vision\n",
        encoding="utf-8",
    )

    msg = InboundMessage(
        channel="whatsapp",
        sender_id="62811",
        chat_id="120363038334877727@g.us",
        content="latest related url link projects to AI Vision Model",
    )

    out = await loop_with_wa_kb._process_message(msg, session_key=msg.session_key)

    assert out is not None
    called_messages = loop_with_wa_kb.provider.chat_with_retry.await_args.kwargs["messages"]
    user_contents = [m.get("content") for m in called_messages if m.get("role") == "user"]
    joined = "\n".join(c for c in user_contents if isinstance(c, str))
    assert "WA Group Knowledge Matches" in joined


@pytest.mark.asyncio
async def test_process_message_does_not_inject_for_non_whatsapp(loop_with_wa_kb: AgentLoop) -> None:
    msg = InboundMessage(
        channel="telegram",
        sender_id="u",
        chat_id="123",
        content="hello",
    )

    out = await loop_with_wa_kb._process_message(msg, session_key=msg.session_key)

    assert out is not None
    called_messages = loop_with_wa_kb.provider.chat_with_retry.await_args.kwargs["messages"]
    user_contents = [m.get("content") for m in called_messages if m.get("role") == "user"]
    joined = "\n".join(c for c in user_contents if isinstance(c, str))
    assert "WA Group Knowledge Matches" not in joined
