from __future__ import annotations

from pathlib import Path

from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config


class _FakeChannel(BaseChannel):
    name = "fake"
    display_name = "Fake"

    def __init__(self, config, bus):
        super().__init__(config, bus)
        self.workspace_seen: Path | None = None

    def set_workspace(self, workspace: Path) -> None:
        self.workspace_seen = workspace

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg):
        return None


def test_channel_manager_sets_workspace_for_channels(monkeypatch, tmp_path: Path) -> None:
    def _discover_all():
        return {"fake": _FakeChannel}

    monkeypatch.setattr("nanobot.channels.registry.discover_all", _discover_all)

    cfg = Config.model_validate(
        {
            "agents": {"defaults": {"workspace": str(tmp_path)}},
            "channels": {
                "fake": {
                    "enabled": True,
                    "allowFrom": ["*"],
                }
            },
        }
    )

    manager = ChannelManager(cfg, MessageBus())
    fake = manager.get_channel("fake")
    assert isinstance(fake, _FakeChannel)
    assert fake.workspace_seen == tmp_path
