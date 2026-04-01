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
import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
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
    deep_rows: list[dict[str, str]]


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


def _strip_html_to_text(raw_html: str) -> str:
    cleaned = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw_html)
    cleaned = re.sub(r"(?is)<style.*?>.*?</style>", " ", cleaned)
    cleaned = re.sub(r"(?is)<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _extract_meta_content(raw_html: str, name: str) -> str:
    patterns = [
        rf'(?is)<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'(?is)<meta[^>]+property=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'(?is)<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(name)}["\']',
        rf'(?is)<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(name)}["\']',
    ]
    for pat in patterns:
        m = re.search(pat, raw_html)
        if m:
            return html.unescape((m.group(1) or "").strip())
    return ""


def _new_deep_row(url: str) -> dict[str, str]:
    row: dict[str, str] = {
        "url": url,
        "status": "",
        "domain": "",
        "title": "",
        "description": "",
        "snippet": "",
        "error": "",
    }
    try:
        host = (urlparse(url).hostname or "").lower().strip()
        if host.startswith("www."):
            host = host[4:]
        row["domain"] = host
    except Exception:
        pass
    return row


def _normalize_host(host: str) -> str:
    out = (host or "").strip().lower()
    if out.startswith("www."):
        out = out[4:]
    return out


def _host_matches(domain: str, candidates: list[str] | None) -> bool:
    host = _normalize_host(domain)
    for item in candidates or []:
        candidate = _normalize_host(str(item or ""))
        if not candidate:
            continue
        if host == candidate or host.endswith("." + candidate):
            return True
    return False


def _resolve_host_ips(host: str) -> set[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    out: set[str] = set()
    for info in infos:
        addr = (info[4] or [""])[0]
        if addr:
            out.add(addr)
    return out


def _is_blocked_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip_obj.is_loopback or ip_obj.is_private or ip_obj.is_link_local:
        return True
    if ip_obj.is_multicast or ip_obj.is_reserved or ip_obj.is_unspecified:
        return True
    if isinstance(ip_obj, ipaddress.IPv4Address):
        extra_v4 = [
            ipaddress.ip_network("100.64.0.0/10"),
            ipaddress.ip_network("198.18.0.0/15"),
        ]
        return any(ip_obj in net for net in extra_v4)
    return False


def _validate_fetch_url(url: str) -> tuple[dict[str, str], str]:
    row = _new_deep_row(url)

    try:
        parsed = urlparse(url)
    except Exception:
        return row, "invalid URL"

    scheme = (parsed.scheme or "").lower().strip()
    if scheme not in {"http", "https"}:
        return row, f"unsupported URL scheme: {scheme or '(empty)'}"

    host = _normalize_host(parsed.hostname or "")
    if not host:
        return row, "missing URL host"
    row["domain"] = host

    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return row, f"blocked host: {host}"

    try:
        ipaddress.ip_address(host)
        return row, "blocked direct IP host"
    except ValueError:
        pass

    try:
        addresses = _resolve_host_ips(parsed.hostname or host)
    except OSError as exc:
        return row, f"dns lookup failed: {exc}"

    if not addresses:
        return row, "dns lookup returned no addresses"

    for addr in addresses:
        ip_str = str(addr).split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_blocked_ip(ip_obj):
            return row, f"blocked target address: {ip_str}"

    return row, ""


def _fetch_link_http(url: str, timeout_seconds: int = 8, max_chars: int = 12000) -> dict[str, str]:
    row = _new_deep_row(url)

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "nanobot-wa-kb/1.0 (+https://github.com/HKUDS/nanobot)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:  # nosec B310
            status = getattr(resp, "status", 200)
            row["status"] = str(status)
            ctype = str(resp.headers.get("Content-Type", ""))
            raw = resp.read(max_chars + 1)
            raw = raw[:max_chars]
            text = raw.decode("utf-8", errors="ignore")

        if "html" not in ctype.lower():
            row["snippet"] = f"Non-HTML content type: {ctype}"[:280]
            return row

        title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
        if title_match:
            row["title"] = re.sub(r"\s+", " ", html.unescape(title_match.group(1))).strip()[:180]

        desc = _extract_meta_content(text, "description") or _extract_meta_content(text, "og:description")
        if desc:
            row["description"] = re.sub(r"\s+", " ", desc).strip()[:220]

        plain = _strip_html_to_text(text)
        if plain:
            row["snippet"] = plain[:320]

        return row
    except Exception as exc:
        row["error"] = str(exc)[:200]
        return row


class _BrowserFetcher:
    def __init__(self, *, timeout_seconds: int = 8, wait_after_load_ms: int = 1200):
        self.timeout_ms = max(int(timeout_seconds * 1000), 1000)
        self.wait_after_load_ms = max(int(wait_after_load_ms), 0)
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def _ensure_started(self) -> None:
        if self._page is not None:
            return

        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._context = self._browser.new_context()

        def _route_handler(route):
            resource_type = str(getattr(route.request, "resource_type", "") or "")
            if resource_type in {"image", "media", "font"}:
                route.abort()
                return
            route.continue_()

        self._context.route("**/*", _route_handler)
        self._page = self._context.new_page()

    def fetch(self, url: str, *, max_chars: int = 12000) -> dict[str, str]:
        row = _new_deep_row(url)
        try:
            self._ensure_started()
        except Exception as exc:
            row["error"] = f"browser init failed: {exc}"[:200]
            return row

        try:
            resp = self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            if resp is not None:
                row["status"] = str(getattr(resp, "status", "") or "")

            if self.wait_after_load_ms > 0:
                self._page.wait_for_timeout(self.wait_after_load_ms)

            payload = self._page.evaluate(
                """
                () => {
                  const pickMeta = (keys) => {
                    for (const key of keys) {
                      const byName = document.querySelector(`meta[name=\"${key}\"]`);
                      if (byName?.content) return byName.content;
                      const byProp = document.querySelector(`meta[property=\"${key}\"]`);
                      if (byProp?.content) return byProp.content;
                    }
                    return "";
                  };
                  const root = document.querySelector("article") || document.querySelector("main") || document.body;
                  const text = (root?.innerText || "").replace(/\\s+/g, " ").trim();
                  return {
                    title: (document.title || "").trim(),
                    description: (pickMeta(["description", "og:description", "twitter:description"]) || "").trim(),
                    snippet: text.slice(0, 5000),
                  };
                }
                """
            ) or {}

            title = str(payload.get("title") or "").strip()
            desc = str(payload.get("description") or "").strip()
            snippet = str(payload.get("snippet") or "").strip()

            if title:
                row["title"] = re.sub(r"\s+", " ", title)[:180]
            if desc:
                row["description"] = re.sub(r"\s+", " ", desc)[:220]
            if snippet:
                row["snippet"] = re.sub(r"\s+", " ", snippet)[: min(max_chars, 320)]

            return row
        except Exception as exc:
            row["error"] = str(exc)[:200]
            return row

    def close(self) -> None:
        for obj_name in ("_page", "_context", "_browser"):
            obj = getattr(self, obj_name, None)
            if obj is None:
                continue
            try:
                obj.close()
            except Exception:
                pass
            setattr(self, obj_name, None)

        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None


def _fetch_link_browser(
    url: str,
    timeout_seconds: int = 8,
    max_chars: int = 12000,
    *,
    wait_after_load_ms: int = 1200,
    browser_fetcher: _BrowserFetcher | None = None,
) -> dict[str, str]:
    if browser_fetcher is not None:
        return browser_fetcher.fetch(url, max_chars=max_chars)

    fetcher = _BrowserFetcher(timeout_seconds=timeout_seconds, wait_after_load_ms=wait_after_load_ms)
    try:
        return fetcher.fetch(url, max_chars=max_chars)
    finally:
        fetcher.close()


def _is_low_quality_result(row: dict[str, str]) -> bool:
    if row.get("error"):
        return True

    title = str(row.get("title") or "").strip()
    snippet = str(row.get("description") or row.get("snippet") or "").strip()
    if title:
        return False
    if not snippet:
        return True

    lowered = snippet.lower()
    if "enable javascript" in lowered or "javascript is required" in lowered:
        return True
    if re.search(r"<(script|meta|html|body|div)\b", lowered):
        return True
    return len(snippet) < 48


def _has_useful_content(row: dict[str, str]) -> bool:
    if row.get("error"):
        return False
    if str(row.get("title") or "").strip():
        return True
    if len(str(row.get("description") or "").strip()) >= 24:
        return True
    return len(str(row.get("snippet") or "").strip()) >= 80


def _fetch_link_deep(
    url: str,
    timeout_seconds: int = 8,
    max_chars: int = 12000,
    *,
    fetch_mode: str = "auto",
    browser_domains: list[str] | None = None,
    wait_after_load_ms: int = 1200,
    browser_fetcher: _BrowserFetcher | None = None,
) -> dict[str, str]:
    pre_row, validation_error = _validate_fetch_url(url)
    if validation_error:
        pre_row["error"] = validation_error[:200]
        return pre_row

    mode = (fetch_mode or "auto").strip().lower()
    if mode not in {"http", "browser", "auto"}:
        mode = "auto"

    domain = pre_row.get("domain") or ""

    def _http() -> dict[str, str]:
        row = _fetch_link_http(url, timeout_seconds=timeout_seconds, max_chars=max_chars)
        if not row.get("domain") and domain:
            row["domain"] = domain
        return row

    def _browser() -> dict[str, str]:
        row = _fetch_link_browser(
            url,
            timeout_seconds=timeout_seconds,
            max_chars=max_chars,
            wait_after_load_ms=wait_after_load_ms,
            browser_fetcher=browser_fetcher,
        )
        if not row.get("domain") and domain:
            row["domain"] = domain
        return row

    if mode == "http":
        return _http()
    if mode == "browser":
        return _browser()

    http_row = _http()
    force_browser = _host_matches(domain, browser_domains)
    if force_browser or _is_low_quality_result(http_row):
        browser_row = _browser()
        if _has_useful_content(browser_row):
            return browser_row
        if not browser_row.get("error") and _is_low_quality_result(http_row):
            return browser_row
    return http_row


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


def _build(
    events: list[dict[str, Any]],
    day: date,
    group_id: str,
    chat_jid: str = "",
    *,
    deep_enabled: bool = False,
    deep_max_links: int = 8,
    deep_timeout_seconds: int = 8,
    deep_max_chars_per_page: int = 12000,
    deep_fetch_mode: str = "auto",
    deep_browser_domains: list[str] | None = None,
    deep_wait_after_load_ms: int = 1200,
) -> DailyBuild:
    message_count = len(events)
    all_text = []
    links_seen: dict[str, dict[str, str]] = {}
    topic_counter: Counter[str] = Counter()
    entity_counter: Counter[str] = Counter()
    tasks: list[dict[str, str]] = []
    decisions: list[dict[str, str]] = []
    deep_rows: list[dict[str, str]] = []

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

    if deep_enabled and links_seen:
        urls = sorted(links_seen.keys())[: max(deep_max_links, 1)]
        needs_browser_session = (deep_fetch_mode in {"browser", "auto"})
        browser_fetcher = (
            _BrowserFetcher(timeout_seconds=deep_timeout_seconds, wait_after_load_ms=deep_wait_after_load_ms)
            if needs_browser_session
            else None
        )
        try:
            for url in urls:
                deep_rows.append(
                    _fetch_link_deep(
                        url,
                        timeout_seconds=deep_timeout_seconds,
                        max_chars=deep_max_chars_per_page,
                        fetch_mode=deep_fetch_mode,
                        browser_domains=deep_browser_domains,
                        wait_after_load_ms=deep_wait_after_load_ms,
                        browser_fetcher=browser_fetcher,
                    )
                )
        finally:
            if browser_fetcher is not None:
                browser_fetcher.close()

    if deep_rows:
        summary_lines += ["", "## Deep Link Context"]
        for row in deep_rows:
            title = row.get("title") or "(untitled)"
            domain = row.get("domain") or "(unknown domain)"
            status = row.get("status") or "n/a"
            summary_lines.append(f"- {domain} [{status}] — {title}")
            desc = (row.get("description") or row.get("snippet") or "").strip()
            if desc:
                summary_lines.append(f"  - {desc[:220]}")
            if row.get("error"):
                summary_lines.append(f"  - fetch_error: {row['error']}")

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

    for row in deep_rows:
        facts_lines += [
            f"## CARD-{day.strftime('%Y%m%d')}-{card_idx:03d}",
            "type: deep-link",
            f"url: {row.get('url', '')}",
            f"domain: {row.get('domain', '')}",
            f"status: {row.get('status', '')}",
            f"title: {row.get('title', '')}",
            f"description: {row.get('description', '')}",
            f"snippet: {(row.get('snippet', '') or '')[:220]}",
            f"error: {row.get('error', '')}",
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
        deep_rows=deep_rows,
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


def _append_daily_deep_index(root: Path, day: date, rows: list[dict[str, str]]) -> None:
    if not rows:
        return

    index_dir = root / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    path = index_dir / "deep.md"
    old = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else "# Deep Link Index\n"
    marker = f"## {day.isoformat()}"

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

    chunk = ["", marker]
    for row in rows:
        url = row.get("url", "")
        domain = row.get("domain", "")
        title = row.get("title", "")
        status = row.get("status", "")
        snippet = (row.get("description") or row.get("snippet") or "").strip()[:200]
        err = row.get("error", "")
        line = f"- url={url} | domain={domain} | status={status} | title={title}"
        if snippet:
            line += f" | summary={snippet}"
        if err:
            line += f" | error={err}"
        chunk.append(line)

    content = "\n".join(kept).rstrip() + "\n" + "\n".join(chunk) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


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

    deep_domains: list[str] = []
    deep_titles: list[str] = []
    deep_ok = 0
    for row in built.deep_rows:
        if row.get("status") and not row.get("error"):
            deep_ok += 1
        d = (row.get("domain") or "").strip()
        if d and d not in deep_domains:
            deep_domains.append(d)
        t = (row.get("title") or "").strip()
        if t:
            deep_titles.append(t)

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

        if built.deep_rows:
            if deep_domains:
                pieces.append(f"deep-check read {deep_ok}/{len(built.deep_rows)} links ({', '.join(deep_domains[:3])})")
            else:
                pieces.append(f"deep-check read {deep_ok}/{len(built.deep_rows)} links")

        context_line = "; ".join(pieces) + "."

    out = (
        f"🧠 WA KB daily summary\n"
        f"date: {day.isoformat()}\n"
        f"messages: {built.message_count}\n"
        f"links: {len(built.links)} | entities: {len(built.entities)}\n"
        f"top_topics: {topics_line}\n"
        f"context: {context_line}"
    )

    if deep_titles:
        out += "\ncontent_hint: " + " | ".join(deep_titles[:2])

    return out


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

            cfg = load_config()
            channels_cfg = cfg.channels
            whatsapp_cfg = channels_cfg.get("whatsapp") if isinstance(channels_cfg, dict) else getattr(channels_cfg, "whatsapp", None)
            kb_cfg = parse_whatsapp_knowledge_config(whatsapp_cfg)
            group_cfg = (kb_cfg.groups or {}).get(group_id) if kb_cfg.enabled else None
            deep_cfg = group_cfg.deep_mode if group_cfg else None

            built = _build(
                events,
                day,
                group_id,
                args.chat_jid or "",
                deep_enabled=bool(deep_cfg.enabled) if deep_cfg else False,
                deep_max_links=int(deep_cfg.max_links_per_day) if deep_cfg else 8,
                deep_timeout_seconds=int(deep_cfg.timeout_seconds) if deep_cfg else 8,
                deep_max_chars_per_page=int(deep_cfg.max_chars_per_page) if deep_cfg else 12000,
                deep_fetch_mode=str(deep_cfg.fetch_mode) if deep_cfg else "auto",
                deep_browser_domains=list(deep_cfg.browser_domains) if deep_cfg else [],
                deep_wait_after_load_ms=int(deep_cfg.wait_after_load_ms) if deep_cfg else 1200,
            )
            write_daily_outputs(
                root=root,
                day=day,
                summary_md=built.summary_md,
                facts_md=built.facts_md,
                links=built.links,
                topics=built.topics,
                entities=built.entities,
            )
            _append_daily_deep_index(root, day, built.deep_rows)
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
