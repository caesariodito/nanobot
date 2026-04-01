"""WhatsApp group knowledge archive and retrieval utilities."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nanobot.utils.helpers import ensure_dir

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_DATE_IN_PATH_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")


@dataclass(slots=True)
class WAGroupKnowledgeDeepMode:
    """Optional deep link-analysis settings for nightly processing."""

    enabled: bool = False
    max_links_per_day: int = 8
    timeout_seconds: int = 8
    max_chars_per_page: int = 12000
    fetch_mode: str = "auto"
    browser_domains: list[str] = field(default_factory=list)
    wait_after_load_ms: int = 1200


@dataclass(slots=True)
class WAGroupKnowledgeGroup:
    """Per-group WhatsApp knowledge settings."""

    group_id: str
    enabled: bool = True
    chat_jid: str | None = None
    timezone: str = "Asia/Jakarta"
    retrieval_top_k: int = 6
    max_daily_messages: int = 5000
    daily_run_at: str = "00:05"
    recap_enabled: bool = False
    recap_channel: str = "telegram"
    recap_chat_id: str | None = None
    deep_mode: WAGroupKnowledgeDeepMode = field(default_factory=WAGroupKnowledgeDeepMode)


@dataclass(slots=True)
class WAGroupKnowledgeConfig:
    """Top-level WhatsApp knowledge settings."""

    enabled: bool = False
    groups: dict[str, WAGroupKnowledgeGroup] | None = None


def normalize_group_id(chat_jid: str) -> str:
    """Normalize a WhatsApp group JID/id to bare numeric-ish group id."""
    base = chat_jid.split("@", 1)[0] if "@" in chat_jid else chat_jid
    return base.split(":", 1)[0] if ":" in base else base


def extract_urls(text: str) -> list[str]:
    """Extract URLs from text payload."""
    if not text:
        return []
    urls = _URL_RE.findall(text)
    # Drop trailing punctuation commonly attached in chat messages.
    cleaned: list[str] = []
    for item in urls:
        cleaned.append(item.rstrip(").,;!?:"))
    return cleaned


def _parse_group_config(group_id: str, raw: Any) -> WAGroupKnowledgeGroup | None:
    if raw is False:
        return None
    if raw is True:
        return WAGroupKnowledgeGroup(group_id=group_id)
    if not isinstance(raw, dict):
        return WAGroupKnowledgeGroup(group_id=group_id)

    retrieval_top_k = raw.get("retrievalTopK", raw.get("retrieval_top_k", 6))
    max_daily_messages = raw.get("maxDailyMessages", raw.get("max_daily_messages", 5000))

    try:
        retrieval_top_k_int = max(int(retrieval_top_k), 1)
    except (TypeError, ValueError):
        retrieval_top_k_int = 6

    try:
        max_daily_messages_int = max(int(max_daily_messages), 1)
    except (TypeError, ValueError):
        max_daily_messages_int = 5000

    deep_raw = raw.get("deepMode", raw.get("deep_mode", {}))
    if deep_raw is True:
        deep_raw = {"enabled": True}
    elif deep_raw is False or not isinstance(deep_raw, dict):
        deep_raw = {}

    def _safe_int(v: Any, default: int, min_value: int, max_value: int) -> int:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return default
        return max(min(n, max_value), min_value)

    fetch_mode_raw = str(deep_raw.get("fetchMode", deep_raw.get("fetch_mode", "auto")) or "auto").strip().lower()
    fetch_mode = fetch_mode_raw if fetch_mode_raw in {"http", "browser", "auto"} else "auto"

    browser_domains_raw = deep_raw.get("browserDomains", deep_raw.get("browser_domains", []))
    browser_domains: list[str] = []
    if isinstance(browser_domains_raw, list):
        for item in browser_domains_raw:
            host = str(item or "").strip().lower()
            if host.startswith("www."):
                host = host[4:]
            if host:
                browser_domains.append(host)

    deep_mode = WAGroupKnowledgeDeepMode(
        enabled=bool(deep_raw.get("enabled", False)),
        max_links_per_day=_safe_int(deep_raw.get("maxLinksPerDay", deep_raw.get("max_links_per_day", 8)), 8, 1, 50),
        timeout_seconds=_safe_int(deep_raw.get("timeoutSeconds", deep_raw.get("timeout_seconds", 8)), 8, 2, 30),
        max_chars_per_page=_safe_int(deep_raw.get("maxCharsPerPage", deep_raw.get("max_chars_per_page", 12000)), 12000, 1000, 100000),
        fetch_mode=fetch_mode,
        browser_domains=browser_domains,
        wait_after_load_ms=_safe_int(deep_raw.get("waitAfterLoadMs", deep_raw.get("wait_after_load_ms", 1200)), 1200, 0, 10000),
    )

    return WAGroupKnowledgeGroup(
        group_id=str(group_id),
        enabled=bool(raw.get("enabled", True)),
        chat_jid=raw.get("chatJid") or raw.get("chat_jid"),
        timezone=str(raw.get("timezone", "Asia/Jakarta") or "Asia/Jakarta"),
        retrieval_top_k=retrieval_top_k_int,
        max_daily_messages=max_daily_messages_int,
        daily_run_at=str(raw.get("dailyRunAt", raw.get("daily_run_at", "00:05")) or "00:05"),
        recap_enabled=bool(raw.get("recapEnabled", raw.get("recap_enabled", False))),
        recap_channel=str(raw.get("recapChannel", raw.get("recap_channel", "telegram")) or "telegram"),
        recap_chat_id=(
            str(raw.get("recapChatId") or raw.get("recap_chat_id") or "").strip() or None
        ),
        deep_mode=deep_mode,
    )


def parse_whatsapp_knowledge_config(whatsapp_cfg: Any) -> WAGroupKnowledgeConfig:
    """Parse WhatsApp knowledge config from channel config object or dict."""
    if whatsapp_cfg is None:
        return WAGroupKnowledgeConfig(enabled=False, groups={})

    knowledge_raw = (
        whatsapp_cfg.get("knowledge") if isinstance(whatsapp_cfg, dict) else getattr(whatsapp_cfg, "knowledge", None)
    )
    if not isinstance(knowledge_raw, dict):
        return WAGroupKnowledgeConfig(enabled=False, groups={})

    enabled = bool(knowledge_raw.get("enabled", False))
    groups_raw = knowledge_raw.get("groups")
    groups: dict[str, WAGroupKnowledgeGroup] = {}

    if isinstance(groups_raw, dict):
        for key, raw_group in groups_raw.items():
            group_id = normalize_group_id(str(key))
            parsed = _parse_group_config(group_id, raw_group)
            if not parsed:
                continue
            if parsed.chat_jid is None:
                parsed.chat_jid = f"{group_id}@g.us"
            groups[group_id] = parsed

    return WAGroupKnowledgeConfig(enabled=enabled, groups=groups)


def get_whatsapp_kb_groups(channels_config: Any) -> dict[str, WAGroupKnowledgeGroup]:
    """Return enabled WA KB groups from channels config."""
    whatsapp_cfg = None
    if channels_config is not None:
        whatsapp_cfg = channels_config.get("whatsapp") if isinstance(channels_config, dict) else getattr(channels_config, "whatsapp", None)
    parsed = parse_whatsapp_knowledge_config(whatsapp_cfg)
    if not parsed.enabled:
        return {}
    out: dict[str, WAGroupKnowledgeGroup] = {}
    for gid, group in (parsed.groups or {}).items():
        if group.enabled:
            out[gid] = group
    return out


def group_root(workspace: Path, group_id: str) -> Path:
    """Return the per-group KB root path."""
    return workspace / "knowledge" / "whatsapp" / str(group_id)


def _ensure_group_dirs(root: Path) -> None:
    for name in ("raw", "daily", "facts", "index", "state"):
        ensure_dir(root / name)


def _safe_write_text(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def resolve_event_datetime(timestamp: int | float | str | None, tz_name: str = "Asia/Jakarta") -> datetime:
    """Resolve inbound event timestamp to timezone-aware datetime in target timezone."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc

    if isinstance(timestamp, (int, float)) and timestamp > 0:
        # Bridge timestamps are typically Unix epoch seconds (UTC).
        n = float(timestamp)
        if n > 1e12:
            n = n / 1000.0
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc).astimezone(tz)
        except (ValueError, OSError):
            return datetime.now(tz)

    if isinstance(timestamp, str):
        ts = timestamp.strip()
        if ts:
            # Numeric strings: epoch seconds (or milliseconds, defensively).
            if re.fullmatch(r"\d+(?:\.\d+)?", ts):
                try:
                    n = float(ts)
                    if n > 1e12:
                        n = n / 1000.0
                    if n > 0:
                        return datetime.fromtimestamp(n, tz=timezone.utc).astimezone(tz)
                except (TypeError, ValueError, OSError):
                    pass

            # ISO strings: if naive, assume UTC; if aware, convert to target tz.
            try:
                parsed = datetime.fromisoformat(ts)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(tz)
            except ValueError:
                pass

    return datetime.now(tz)


def archive_inbound_message(
    *,
    workspace: Path,
    group_id: str,
    chat_jid: str,
    message_id: str,
    sender_jid: str,
    sender_name: str,
    content: str,
    participant: str | None,
    timestamp: int | float | str | None,
    media: list[str] | None,
    metadata: dict[str, Any] | None = None,
    max_daily_messages: int = 5000,
    timezone_name: str = "Asia/Jakarta",
) -> Path:
    """Append a normalized inbound message event into per-day JSONL."""
    root = group_root(workspace, group_id)
    _ensure_group_dirs(root)

    ts_dt = resolve_event_datetime(timestamp, timezone_name)

    raw_file = root / "raw" / f"{ts_dt.date().isoformat()}.jsonl"
    payload = {
        "ts": ts_dt.isoformat(),
        "chat_jid": chat_jid,
        "group_id": str(group_id),
        "message_id": message_id or "",
        "sender_jid": sender_jid,
        "sender_name": sender_name,
        "direction": "inbound",
        "text": content,
        "urls": extract_urls(content),
        "reply_to": None,
        "participant": participant or "",
        "media": media or [],
        "metadata": metadata or {},
    }

    lines = []
    if raw_file.exists():
        lines = raw_file.read_text(encoding="utf-8", errors="ignore").splitlines()

    # Keep file bounded by dropping oldest lines once threshold is reached.
    keep = max(max_daily_messages - 1, 0)
    if keep and len(lines) >= keep:
        lines = lines[-keep:]
    elif max_daily_messages <= 1:
        lines = []

    lines.append(json.dumps(payload, ensure_ascii=False))
    _safe_write_text(raw_file, "\n".join(lines) + "\n")
    return raw_file


def load_day_events(root: Path, day: date) -> list[dict[str, Any]]:
    """Load one day of archived JSONL events."""
    file_path = root / "raw" / f"{day.isoformat()}.jsonl"
    if not file_path.exists():
        return []

    events: list[dict[str, Any]] = []
    for line in file_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _score_doc(query: str, text: str, path: Path) -> int:
    q = query.lower().strip()
    if not q:
        return 0

    score = 0
    t = text.lower()

    tokens = [tok for tok in re.split(r"\s+", q) if tok]
    for token in tokens:
        if token in t:
            score += 2

    # Tiny phrase bonus
    if q in t:
        score += 3

    # recency hint
    if "latest" in q or "newest" in q or "recent" in q:
        if _DATE_IN_PATH_RE.search(path.as_posix()):
            score += 1

    return score


def build_runtime_context_lines(
    *,
    workspace: Path,
    group_id: str,
    query: str,
    top_k: int = 6,
) -> list[str]:
    """Search KB markdown files and return short context lines for runtime injection."""
    root = group_root(workspace, group_id)
    if not root.exists():
        return []

    candidates: list[tuple[int, Path, str]] = []
    for folder in ("facts", "daily", "index"):
        p = root / folder
        if not p.exists():
            continue
        for md in sorted(p.glob("*.md")):
            text = md.read_text(encoding="utf-8", errors="ignore")
            score = _score_doc(query, text, md)
            if score > 0:
                candidates.append((score, md, text))

    if not candidates:
        return []

    candidates.sort(key=lambda item: item[0], reverse=True)
    top = candidates[: max(top_k, 1)]

    lines = [
        "[WA Group Knowledge Matches — untrusted reference snippets]",
        f"group_id={group_id}",
    ]
    for idx, (score, path, text) in enumerate(top, start=1):
        excerpt = " ".join(text.split())[:280]
        rel = path.relative_to(root)
        lines.append(f"{idx}. {rel} (score={score}) :: {excerpt}")
    return lines


def write_daily_outputs(
    *,
    root: Path,
    day: date,
    summary_md: str,
    facts_md: str,
    links: list[dict[str, str]],
    topics: list[dict[str, str]],
    entities: list[dict[str, str]],
) -> None:
    """Write daily and index markdown outputs atomically."""
    _ensure_group_dirs(root)

    daily_file = root / "daily" / f"{day.isoformat()}.md"
    facts_file = root / "facts" / f"{day.isoformat()}.md"
    _safe_write_text(daily_file, summary_md.rstrip() + "\n")
    _safe_write_text(facts_file, facts_md.rstrip() + "\n")

    def _append_index(path: Path, title: str, rows: list[dict[str, str]]) -> None:
        old = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else f"# {title}\n"
        marker = f"## {day.isoformat()}"

        # Idempotent rewrite: remove an existing section for this day before appending.
        lines = old.splitlines()
        kept: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.strip() == marker:
                i += 1
                while i < len(lines) and not lines[i].startswith("## "):
                    i += 1
                continue
            kept.append(line)
            i += 1

        base = "\n".join(kept).rstrip()
        chunk = ["", marker]
        for row in rows:
            chunk.append("- " + " | ".join(f"{k}={v}" for k, v in row.items() if v))
        _safe_write_text(path, base + "\n" + "\n".join(chunk) + "\n")

    _append_index(root / "index" / "links.md", "Links Index", links)
    _append_index(root / "index" / "topics.md", "Topics Index", topics)
    _append_index(root / "index" / "entities.md", "Entities Index", entities)

    _safe_write_text(root / "state" / "last_processed_date.txt", day.isoformat() + "\n")
