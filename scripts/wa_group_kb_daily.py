#!/usr/bin/env python3
"""Daily WhatsApp group knowledge summarizer.

MVP behavior:
- Read one day of archived raw events (JSONL)
- Build summary markdown
- Build fact cards markdown (links/topics/entities/tasks/decisions heuristic)
- Update markdown indexes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import urllib.error
import urllib.request
from collections import Counter
from urllib.parse import urlparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nanobot.config.loader import load_config
from nanobot.knowledge.wa_group_kb import (
    extract_urls,
    group_root,
    load_day_events,
    parse_whatsapp_knowledge_config,
    write_daily_outputs,
)

_TOPIC_STOPWORDS = {
    "the", "and", "for", "you", "that", "this", "with", "from", "are", "have", "your", "about",
    "what", "when", "will", "just", "need", "into", "https", "http", "www", "com", "org", "net",
    "chat", "group", "message", "today", "tomorrow", "please", "thanks", "done", "okay", "ok",
}

ENTITY_RE = re.compile(r"\b([A-Z][A-Za-z0-9_.-]{2,})\b")
TASK_RE = re.compile(r"\b(todo|action|follow[- ]?up|next step|deadline|due)\b", re.IGNORECASE)
DECISION_RE = re.compile(r"\b(decide|decided|agreement|agreed|use\s+\w+|final)\b", re.IGNORECASE)


@dataclass(slots=True)
class DailyBuild:
    summary_md: str
    facts_md: str
    links: list[dict[str, str]]
    topics: list[dict[str, str]]
    entities: list[dict[str, str]]
    message_count: int


def _tokenize(text: str) -> list[str]:
    out = []
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text.lower()):
        if tok in _TOPIC_STOPWORDS:
            continue
        out.append(tok)
    return out


def _day_from_args(day: str | None, tz_name: str) -> date:
    tz = ZoneInfo(tz_name)
    if day:
        return date.fromisoformat(day)
    return (datetime.now(tz).date() - timedelta(days=1))


@contextmanager
def _file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    try:
        fd = lock_path.open("w", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception as exc:  # pragma: no cover - best effort on non-posix
            raise RuntimeError(f"unable to acquire lock: {lock_path}: {exc}") from exc
        yield
    finally:
        if fd is not None:
            try:
                import fcntl

                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            fd.close()


def _build(events: list[dict[str, Any]], day: date, group_id: str, chat_jid: str = "") -> DailyBuild:
    message_count = len(events)
    all_text = []
    links_seen: dict[str, dict[str, str]] = {}
    topic_counter: Counter[str] = Counter()
    entity_counter: Counter[str] = Counter()
    tasks: list[dict[str, str]] = []
    decisions: list[dict[str, str]] = []

    for e in events:
        text = (e.get("text") or "").strip()
        if not text:
            continue
        all_text.append(text)

        for u in set((e.get("urls") or []) + extract_urls(text)):
            if u not in links_seen:
                links_seen[u] = {
                    "url": u,
                    "src": f"raw/{day.isoformat()}.jsonl#{e.get('message_id') or ''}".rstrip("#"),
                    "topic": "",
                }

        for tok in _tokenize(text):
            topic_counter[tok] += 1

        for m in ENTITY_RE.findall(text):
            entity_counter[m] += 1

        if TASK_RE.search(text):
            tasks.append(
                {
                    "text": text[:220],
                    "source": f"raw/{day.isoformat()}.jsonl#{e.get('message_id') or ''}".rstrip("#"),
                }
            )

        if DECISION_RE.search(text):
            decisions.append(
                {
                    "text": text[:220],
                    "source": f"raw/{day.isoformat()}.jsonl#{e.get('message_id') or ''}".rstrip("#"),
                }
            )

    top_topics = [tok for tok, _ in topic_counter.most_common(8)]
    top_entities = [ent for ent, _ in entity_counter.most_common(8)]

    summary_lines = [
        "---",
        f"group_id: {group_id}",
        f"chat_jid: {chat_jid or f'{group_id}@g.us'}",
        f"date: {day.isoformat()}",
        f"message_count: {message_count}",
        "---",
        f"# Daily Summary ({day.isoformat()})",
        "",
        "## Executive Summary",
        "- Auto-generated daily digest from archived WhatsApp messages.",
        f"- Messages captured: {message_count}",
        f"- Unique URLs: {len(links_seen)}",
        "",
        "## Key Topics",
    ]
    summary_lines.extend([f"- {t}" for t in top_topics] or ["- (no dominant topics)"])

    summary_lines += ["", "## Decisions"]
    summary_lines.extend([f"- {d['text']} ({d['source']})" for d in decisions[:10]] or ["- (none detected)"])

    summary_lines += ["", "## Action Items"]
    summary_lines.extend([f"- [ ] {t['text']} ({t['source']})" for t in tasks[:10]] or ["- (none detected)"])

    summary_lines += ["", "## URLs Mentioned"]
    summary_lines.extend([f"- {u}" for u in sorted(links_seen.keys())] or ["- (none)"])

    summary_lines += ["", "## Entities"]
    summary_lines.extend([f"- {e}" for e in top_entities] or ["- (none)"])

    facts_lines = [f"# Facts ({day.isoformat()})", ""]
    card_idx = 1

    for u, row in sorted(links_seen.items(), key=lambda kv: kv[0]):
        facts_lines += [
            f"## CARD-{day.strftime('%Y%m%d')}-{card_idx:03d}",
            "type: link",
            f"topic_tags: [{', '.join(top_topics[:3])}]" if top_topics else "topic_tags: []",
            f"url: {u}",
            "summary: URL mentioned in group discussion",
            f"source: {row['src']}",
            "",
        ]
        card_idx += 1

    for d in decisions[:10]:
        facts_lines += [
            f"## CARD-{day.strftime('%Y%m%d')}-{card_idx:03d}",
            "type: decision",
            f"summary: {d['text']}",
            f"source: {d['source']}",
            "",
        ]
        card_idx += 1

    for t in tasks[:10]:
        facts_lines += [
            f"## CARD-{day.strftime('%Y%m%d')}-{card_idx:03d}",
            "type: task",
            f"summary: {t['text']}",
            f"source: {t['source']}",
            "",
        ]
        card_idx += 1

    links_rows = list(links_seen.values())
    topics_rows = [
        {"topic": topic, "count": str(cnt), "day": day.isoformat()}
        for topic, cnt in topic_counter.most_common(20)
    ]
    entities_rows = [
        {"entity": ent, "count": str(cnt), "day": day.isoformat()}
        for ent, cnt in entity_counter.most_common(20)
    ]

    return DailyBuild(
        summary_md="\n".join(summary_lines).rstrip() + "\n",
        facts_md="\n".join(facts_lines).rstrip() + "\n",
        links=links_rows,
        topics=topics_rows,
        entities=entities_rows,
        message_count=message_count,
    )


def _send_telegram_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
        _ = resp.read()


async def _send_whatsapp_message(bridge_url: str, bridge_token: str, chat_id: str, text: str) -> None:
    import websockets

    async with websockets.connect(bridge_url) as ws:
        if bridge_token:
            await ws.send(json.dumps({"type": "auth", "token": bridge_token}))
        await ws.send(
            json.dumps(
                {
                    "type": "send",
                    "to": chat_id,
                    "text": text,
                },
                ensure_ascii=False,
            )
        )


def _build_recap_text(*, group_id: str, day: date, built: DailyBuild, root: Path) -> str:
    top_topics = [row.get("topic", "") for row in built.topics[:5] if row.get("topic")]
    topics_line = ", ".join(top_topics) if top_topics else "(none)"

    domains: list[str] = []
    for row in built.links[:5]:
        raw_url = str(row.get("url") or "").strip()
        if not raw_url:
            continue
        host = (urlparse(raw_url).netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host and host not in domains:
            domains.append(host)

    if built.message_count <= 0:
        context_line = "No usable context captured for this day."
    else:
        pieces: list[str] = []
        if top_topics:
            pieces.append(f"Discussion mostly around {', '.join(top_topics[:3])}")
        else:
            pieces.append("Discussion had no dominant repeated topics")

        if domains:
            pieces.append(f"with references shared from {', '.join(domains[:3])}")

        context_line = "; ".join(pieces) + "."

    return (
        f"🧠 WA KB daily summary\n"
        f"date: {day.isoformat()}\n"
        f"messages: {built.message_count}\n"
        f"links: {len(built.links)} | entities: {len(built.entities)}\n"
        f"top_topics: {topics_line}\n"
        f"context: {context_line}"
    )


def _maybe_send_recap(*, workspace: Path, group_id: str, day: date, built: DailyBuild, root: Path, recap_chat_id_arg: str = "") -> None:
    cfg = load_config()

    channels = cfg.channels
    whatsapp_cfg = channels.get("whatsapp") if isinstance(channels, dict) else getattr(channels, "whatsapp", None)
    kb_cfg = parse_whatsapp_knowledge_config(whatsapp_cfg)
    group_cfg = (kb_cfg.groups or {}).get(group_id) if kb_cfg.enabled else None
    if not group_cfg or not group_cfg.recap_enabled:
        return

    channel = (group_cfg.recap_channel or "telegram").lower()
    text = _build_recap_text(group_id=group_id, day=day, built=built, root=root)

    if channel == "telegram":
        telegram_cfg = channels.get("telegram") if isinstance(channels, dict) else getattr(channels, "telegram", None)
        tg_token = ""
        if isinstance(telegram_cfg, dict):
            tg_token = str(telegram_cfg.get("token") or "")
        else:
            tg_token = str(getattr(telegram_cfg, "token", "") or "")

        recap_chat_id = (recap_chat_id_arg or group_cfg.recap_chat_id or "").strip()
        if not tg_token or not recap_chat_id:
            print("[wa-kb] recap skipped: telegram token or recap chat_id missing")
            return

        try:
            _send_telegram_message(tg_token, recap_chat_id, text)
            print(f"[wa-kb] recap sent to telegram chat_id={recap_chat_id}")
        except urllib.error.URLError as exc:
            print(f"[wa-kb] recap send failed: {exc}")
        return

    if channel == "whatsapp":
        recap_chat_id = (recap_chat_id_arg or group_cfg.recap_chat_id or group_cfg.chat_jid or f"{group_id}@g.us").strip()
        whatsapp_cfg_obj = channels.get("whatsapp") if isinstance(channels, dict) else getattr(channels, "whatsapp", None)

        bridge_url = ""
        bridge_token = ""
        if isinstance(whatsapp_cfg_obj, dict):
            bridge_url = str(whatsapp_cfg_obj.get("bridgeUrl") or whatsapp_cfg_obj.get("bridge_url") or "")
            bridge_token = str(whatsapp_cfg_obj.get("bridgeToken") or whatsapp_cfg_obj.get("bridge_token") or "")
        else:
            bridge_url = str(getattr(whatsapp_cfg_obj, "bridge_url", "") or "")
            bridge_token = str(getattr(whatsapp_cfg_obj, "bridge_token", "") or "")

        if not bridge_url or not recap_chat_id:
            print("[wa-kb] recap skipped: whatsapp bridge_url or recap chat_id missing")
            return

        try:
            asyncio.run(_send_whatsapp_message(bridge_url, bridge_token, recap_chat_id, text))
            print(f"[wa-kb] recap sent to whatsapp chat_id={recap_chat_id}")
        except Exception as exc:
            print(f"[wa-kb] recap send failed: {exc}")
        return

    print(f"[wa-kb] recap skipped: unsupported recap channel={channel}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build daily WhatsApp group knowledge markdown files")
    ap.add_argument("--workspace", default="~/.nanobot/workspace")
    ap.add_argument("--group-id", required=True)
    ap.add_argument("--chat-jid", default="")
    ap.add_argument("--tz", default="Asia/Jakarta")
    ap.add_argument("--date", default="", help="YYYY-MM-DD (default: yesterday in --tz)")
    ap.add_argument("--recap-chat-id", default="", help="override Telegram chat id for nightly recap")
    args = ap.parse_args()

    workspace = Path(args.workspace).expanduser()
    group_id = str(args.group_id)
    day = _day_from_args(args.date or None, args.tz)

    root = group_root(workspace, group_id)
    lock_path = root / "state" / "lock"

    try:
        with _file_lock(lock_path):
            events = load_day_events(root, day)
            if args.chat_jid:
                events = [e for e in events if str(e.get("chat_jid") or "") == args.chat_jid]
            if not events:
                scope = f" chat_jid={args.chat_jid}" if args.chat_jid else ""
                print(f"[wa-kb] no events for group={group_id}{scope} day={day.isoformat()}")
                return 0

            built = _build(events, day, group_id, args.chat_jid or "")
            write_daily_outputs(
                root=root,
                day=day,
                summary_md=built.summary_md,
                facts_md=built.facts_md,
                links=built.links,
                topics=built.topics,
                entities=built.entities,
            )
            print(
                f"[wa-kb] processed group={group_id} day={day.isoformat()} "
                f"events={len(events)} links={len(built.links)}"
            )
            _maybe_send_recap(
                workspace=workspace,
                group_id=group_id,
                day=day,
                built=built,
                root=root,
                recap_chat_id_arg=args.recap_chat_id,
            )
            return 0
    except RuntimeError as exc:
        print(f"[wa-kb] skipped: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
