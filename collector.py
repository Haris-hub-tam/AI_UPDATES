"""Data collector. Runs only when asked: `uv run collector.py` or the
"Refresh now" button. Never two refreshes at once (cross-process file lock).
"""
from __future__ import annotations

import asyncio
import calendar
import fcntl
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import feedparser
import httpx

import db
import enrich
import i18n
import translate
import weekly
import settings

log = logging.getLogger("portal.collector")

RETENTION_DAYS = 120
FEED_CONCURRENCY = 6
DEFAULT_FEED_LIMIT = 25
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
AA_URL = "https://artificialanalysis.ai/api/v2/language/models/free"
AA_MAX_PAGES = 20  # free tier: 100 requests / day


# =================================================================== lock
class RefreshLock:
    """Non-blocking exclusive flock on <db>.lock, shared by app and CLI."""

    def __init__(self) -> None:
        self.fd: int | None = None

    @staticmethod
    def path() -> Path:
        p = settings.db_path()
        return p.with_name(p.name + ".lock")

    def acquire(self) -> bool:
        fd = os.open(self.path(), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def refresh_running() -> bool:
    probe = RefreshLock()
    if probe.acquire():
        probe.release()
        return False
    return True


# ================================================================ helpers
def article_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def load_feeds(path: Path | None = None) -> tuple[list[dict], list[str]]:
    data = tomllib.loads((path or settings.feeds_path()).read_text(encoding="utf-8"))
    feeds = [f for f in data.get("feed", []) if f.get("enabled", True)]
    return feeds, list(data.get("keywords", []))


def keyword_regex(keywords: list[str]) -> re.Pattern | None:
    if not keywords:
        return None
    alts = sorted((re.escape(k) for k in keywords), key=len, reverse=True)
    # word boundaries that also work for keywords ending in punctuation (A.I.)
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.I)


def entry_time(entry) -> str | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            try:
                return db.to_ts(datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc))
            except (OverflowError, ValueError):
                continue
    return None


def entry_image(entry) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key) or []:
            url = m.get("url")
            medium = m.get("medium") or m.get("type") or "image"
            if url and url.startswith("http") and ("image" in medium or key == "media_thumbnail"):
                return url
    for enc in entry.get("enclosures") or []:
        if (enc.get("type") or "").startswith("image") and (enc.get("href") or enc.get("url")):
            return enc.get("href") or enc.get("url")
    for link in entry.get("links") or []:
        if link.get("rel") == "enclosure" and (link.get("type") or "").startswith("image"):
            return link.get("href")
    html = ""
    if entry.get("content"):
        html = entry["content"][0].get("value", "")
    return enrich.first_img(html) or enrich.first_img(entry.get("summary", ""))


def short_summary(text: str, limit: int = 400) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    # arXiv: "arXiv:2511.01234v1 Announce Type: new Abstract: ..."
    text = re.sub(r"^arXiv:\S+\s+Announce Type:\s*\w+\s*Abstract:\s*", "", text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + "…"


# ================================================================== feeds
async def fetch_feed(client: httpx.AsyncClient, feed: dict, kw: re.Pattern | None,
                     cutoff: datetime) -> list[dict]:
    for attempt in range(3):  # retry transient failures (hnrss.org is flaky)
        try:
            r = await client.get(feed["url"], timeout=30)
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            await asyncio.sleep(2 * (attempt + 1))
            continue
        break
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    parsed = await asyncio.to_thread(feedparser.parse, r.content)
    if not parsed.entries:
        if parsed.bozo:
            raise RuntimeError(f"not a valid feed ({type(parsed.bozo_exception).__name__})")
        return []
    limit = int(feed.get("limit", DEFAULT_FEED_LIMIT))
    out: list[dict] = []
    for e in parsed.entries:
        url = (e.get("link") or "").strip()
        title = re.sub(r"\s+", " ", enrich.html_to_text(e.get("title", "")) or e.get("title", "")).strip()
        if not url.startswith("http") or not title:
            continue
        summary_html = e.get("summary", "") or ""
        summary_text = enrich.html_to_text(summary_html).replace("\n\n", " ")
        if feed.get("filter") and kw and not kw.search(f"{title} {summary_text}"):
            continue
        published = entry_time(e)
        pdt = db.parse_ts(published)
        if pdt and pdt < cutoff:
            continue
        content_html = e["content"][0].get("value", "") if e.get("content") else ""
        content_text = enrich.html_to_text(content_html) if content_html else ""
        if not content_text and len(summary_text) >= enrich.MIN_FEED_CHARS:
            content_text = enrich.html_to_text(summary_html)
        out.append({
            "url": url,
            "title": title,
            "summary": short_summary(summary_text or content_text),
            "published": published,
            "image": entry_image(e),
            "content": content_text if len(content_text) >= enrich.MIN_FEED_CHARS else None,
        })
        if len(out) >= limit:
            break
    return out


async def collect_feeds(conn: sqlite3.Connection, client: httpx.AsyncClient,
                        progress) -> dict:
    feeds, keywords = load_feeds()
    kw = keyword_regex(keywords)
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    total, done = len(feeds), 0
    new_total, failed = 0, []
    sem = asyncio.Semaphore(FEED_CONCURRENCY)
    await enrich._report(progress, f"Reading feeds 0/{total}")

    async def one(feed):
        nonlocal done, new_total
        async with sem:
            now = db.now_ts()
            try:
                items = await fetch_feed(client, feed, kw, cutoff)
                new = 0
                for it in items:
                    aid = article_id(it["url"])
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO articles(id, url, title, summary, source, category, "
                        "published, fetched, image, content, word_count, fulltext_status) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (aid, it["url"], it["title"], it["summary"], feed["name"],
                         feed.get("category", "News"), it["published"] or now, now,
                         it["image"], it["content"], enrich.word_count(it["content"]),
                         "feed" if it["content"] else None),
                    )
                    new += cur.rowcount
                conn.execute(
                    "INSERT INTO sources(name, url, category, last_success, last_error, last_count) "
                    "VALUES (?,?,?,?,NULL,?) ON CONFLICT(name) DO UPDATE SET url=excluded.url, "
                    "category=excluded.category, last_success=excluded.last_success, "
                    "last_error=NULL, last_error_at=NULL, last_count=excluded.last_count",
                    (feed["name"], feed["url"], feed.get("category"), now, len(items)),
                )
                conn.commit()
                new_total += new
                log.info("feed %-24s %3d entries, %3d new", feed["name"], len(items), new)
            except Exception as e:
                msg = str(e) or type(e).__name__
                failed.append(feed["name"])
                log.warning("feed %-24s FAILED: %s", feed["name"], msg)
                conn.execute(
                    "INSERT INTO sources(name, url, category, last_error, last_error_at) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET url=excluded.url, "
                    "category=excluded.category, last_error=excluded.last_error, "
                    "last_error_at=excluded.last_error_at",
                    (feed["name"], feed["url"], feed.get("category"), msg[:300], now),
                )
                conn.commit()
            done += 1
            await enrich._report(progress, f"Reading feeds {done}/{total}")

    await asyncio.gather(*(one(f) for f in feeds))
    # forget sources that were removed/disabled in feeds.toml
    names = [f["name"] for f in feeds]
    if names:
        conn.execute(
            f"DELETE FROM sources WHERE name NOT IN ({','.join('?' * len(names))})", names)
        conn.commit()
    return {"new": new_total, "failed": failed, "sources": total}


def prune_old(conn: sqlite3.Connection) -> int:
    cutoff = db.to_ts(datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS))
    cur = conn.execute(
        "DELETE FROM articles WHERE COALESCE(published, fetched) < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


# ========================================================= models & prices
def per_million(value) -> float:
    """OpenRouter prices are USD per token strings; negative/unknown -> -1."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return -1.0
    if v < 0:
        return -1.0
    return round(v * 1_000_000, 6)


_PAREN = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def match_key(name: str) -> str:
    """Normalize a model name so OpenRouter and Artificial Analysis names match.

    "Anthropic: Claude Sonnet 4.5" and "Claude 4.5 Sonnet (Reasoning)" both
    become "4.5 claude sonnet".
    """
    if not name:
        return ""
    name = name.split(":", 1)[1] if ":" in name else name
    name = _PAREN.sub(" ", name.lower())
    tokens = re.findall(r"\d+(?:\.\d+)*|[a-z]+", name)
    return " ".join(sorted(tokens))


def provider_of(model: dict) -> str:
    name = model.get("name") or ""
    if ":" in name:
        return name.split(":", 1)[0].strip()
    mid = model.get("id") or ""
    return mid.split("/", 1)[0] if "/" in mid else "Other"


async def update_models(conn: sqlite3.Connection, client: httpx.AsyncClient) -> dict:
    r = await client.get(OPENROUTER_URL, timeout=60)
    r.raise_for_status()
    data = r.json().get("data") or []
    now = db.now_ts()
    added, changes = 0, 0
    existing = {row["id"]: row for row in conn.execute("SELECT id, price_in, price_out FROM models")}
    for m in data:
        mid = m.get("id")
        if not mid:
            continue
        pricing = m.get("pricing") or {}
        arch = m.get("architecture") or {}
        top = m.get("top_provider") or {}
        p_in, p_out = per_million(pricing.get("prompt")), per_million(pricing.get("completion"))
        cached = pricing.get("input_cache_read")
        p_cached = per_million(cached) if cached is not None else None
        created = m.get("created")
        created_ts = db.to_ts(datetime.fromtimestamp(created, tz=timezone.utc)) if created else None
        row = {
            "id": mid,
            "name": m.get("name") or mid,
            "provider": provider_of(m),
            "context_length": m.get("context_length") or top.get("context_length"),
            "price_in": p_in,
            "price_out": p_out,
            "price_cached": p_cached,
            "max_output": top.get("max_completion_tokens"),
            "input_modalities": json.dumps(arch.get("input_modalities") or []),
            "output_modalities": json.dumps(arch.get("output_modalities") or []),
            "tokenizer": arch.get("tokenizer"),
            "supported_params": json.dumps(m.get("supported_parameters") or []),
            "description": m.get("description") or "",
            "created": created_ts,
            "updated": now,
            "match_key": match_key(m.get("name") or mid),
        }
        old = existing.get(mid)
        if old is None:
            row["first_seen"] = now
            cols = ", ".join(row)
            conn.execute(f"INSERT INTO models({cols}) VALUES ({','.join('?' * len(row))})",
                         tuple(row.values()))
            conn.execute("INSERT INTO price_history(model_id, price_in, price_out, recorded_at) "
                         "VALUES (?,?,?,?)", (mid, p_in, p_out, now))
            added += 1
        else:
            sets = ", ".join(f"{k}=?" for k in row if k != "id")
            conn.execute(f"UPDATE models SET {sets} WHERE id=?",
                         tuple(v for k, v in row.items() if k != "id") + (mid,))
            if old["price_in"] != p_in or old["price_out"] != p_out:
                conn.execute("INSERT INTO price_history(model_id, price_in, price_out, recorded_at) "
                             "VALUES (?,?,?,?)", (mid, p_in, p_out, now))
                changes += 1
                log.info("price change %s: %s/%s -> %s/%s", mid, old["price_in"],
                         old["price_out"], p_in, p_out)
    conn.commit()
    return {"models": len(data), "added": added, "price_changes": changes}


# ============================================================== benchmarks
def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


async def update_benchmarks(conn: sqlite3.Connection, client: httpx.AsyncClient) -> dict:
    key = settings.aa_key()
    if not key:
        return {"configured": False, "count": 0}
    entries: list[dict] = []
    page = 1
    while page <= AA_MAX_PAGES:
        r = await client.get(AA_URL, params={"page": page},
                             headers={"x-api-key": key}, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"Artificial Analysis HTTP {r.status_code}: {r.text[:150]}")
        body = r.json()
        data = body.get("data") or []
        entries.extend(d for d in data if isinstance(d, dict))
        pag = body.get("pagination") or {}
        total_pages = pag.get("total_pages")
        has_more = pag.get("has_more")
        if not data or has_more is False or (has_more is None and (not total_pages or page >= total_pages)):
            break
        if total_pages and page >= total_pages:
            break
        page += 1
    now = db.now_ts()
    seen = set()
    for e in entries:
        bid = str(e.get("id") or e.get("slug") or e.get("name"))
        if not bid or bid in seen:
            continue
        seen.add(bid)
        ev = e.get("evaluations") or {}
        pricing = e.get("pricing") or {}
        perf = e.get("performance") or {}
        creator = e.get("model_creator") or {}
        speed = _num(perf.get("median_output_tokens_per_second", e.get("median_output_tokens_per_second")))
        ttft = _num(perf.get("median_time_to_first_token_seconds", e.get("median_time_to_first_token_seconds")))
        conn.execute(
            "INSERT INTO benchmarks(id, slug, name, creator, release_date, intelligence, coding, "
            "agentic, evaluations, price_in, price_out, speed, ttft, raw, match_key, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "slug=excluded.slug, name=excluded.name, creator=excluded.creator, "
            "release_date=excluded.release_date, intelligence=excluded.intelligence, "
            "coding=excluded.coding, agentic=excluded.agentic, evaluations=excluded.evaluations, "
            "price_in=excluded.price_in, price_out=excluded.price_out, speed=excluded.speed, "
            "ttft=excluded.ttft, raw=excluded.raw, match_key=excluded.match_key, updated=excluded.updated",
            (bid, e.get("slug"), e.get("name") or bid, creator.get("name"), e.get("release_date"),
             _num(ev.get("artificial_analysis_intelligence_index")),
             _num(ev.get("artificial_analysis_coding_index")),
             _num(ev.get("artificial_analysis_agentic_index")),
             json.dumps({k: v for k, v in ev.items() if _num(v) is not None}),
             _num(pricing.get("price_1m_input_tokens")), _num(pricing.get("price_1m_output_tokens")),
             speed, ttft, json.dumps(e), match_key(e.get("name") or ""), now),
        )
    if seen:
        conn.execute(
            f"DELETE FROM benchmarks WHERE id NOT IN ({','.join('?' * len(seen))})", list(seen))
    conn.commit()
    return {"configured": True, "count": len(seen)}


# ============================================================ orchestration
def _set_status(conn: sqlite3.Connection, **kw) -> None:
    state = db.get_meta_json(conn, "refresh_status", {}) or {}
    state.update(kw)
    db.set_meta_json(conn, "refresh_status", state)


async def run_refresh(lock: RefreshLock | None = None) -> dict | None:
    """Run one full refresh. Returns the result dict, or None if another
    refresh is already running. A caller may pass an already-acquired lock;
    it is released when the refresh ends."""
    if lock is None:
        lock = RefreshLock()
        if not lock.acquire():
            log.info("refresh already running - not starting another one")
            return None
    started = time.monotonic()
    conn = db.connect()
    try:
        db.migrate(conn)
        _set_status(conn, running=True, message="Starting refresh", started=db.now_ts(), pid=os.getpid())

        def progress(msg: str) -> None:
            _set_status(conn, message=msg)

        result: dict = {"new_articles": 0, "price_changes": 0, "new_models": 0,
                        "failed_sources": [], "fulltexts": 0, "summaries": 0,
                        "summary_failures": 0, "benchmarks": 0, "errors": []}
        headers = {"User-Agent": settings.user_agent(),
                   "Accept": "application/rss+xml, application/atom+xml, application/xml, text/html, */*"}
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
            fr = await collect_feeds(conn, client, progress)
            result["new_articles"] = fr["new"]
            result["failed_sources"] = fr["failed"]
            result["pruned"] = prune_old(conn)

            ft = await enrich.fetch_fulltexts(conn, client, progress)
            result["fulltexts"] = ft.get("ok", 0)
            result["fulltext_counts"] = ft

            sm = await enrich.write_summaries(conn, client, progress)
            result["summaries"], result["summary_failures"] = sm["ok"], sm["failed"]

            wk = await weekly.write_missing(conn, client, progress)
            result["weekly"] = wk["written"]
            if wk["failed"]:
                result["errors"].append("Weekly summary: " + ", ".join(wk["failed"]))

            tr = await translate.translate_articles(conn, client, progress)
            result["translations"] = tr["ok"]
            result["weekly_translated"] = await translate.translate_weekly(client, progress)

            progress("Updating model prices")
            try:
                mr = await update_models(conn, client)
                result["price_changes"], result["new_models"] = mr["price_changes"], mr["added"]
                result["models"] = mr["models"]
            except Exception as e:
                log.warning("OpenRouter update failed: %s", e)
                result["errors"].append(f"Model prices: {e}")

            progress("Updating benchmarks")
            try:
                br = await update_benchmarks(conn, client)
                result["benchmarks"] = br["count"]
            except Exception as e:
                log.warning("benchmark update failed: %s", e)
                result["errors"].append(f"Benchmarks: {e}")

        result["duration"] = round(time.monotonic() - started, 1)
        result["finished"] = db.now_ts()
        result["message"] = result_message(result)
        db.set_meta(conn, "last_refresh", result["finished"])
        db.set_meta_json(conn, "last_result", result)
        log.info(
            "refresh done in %.1fs: %d new articles, %d new models, %d price changes, "
            "%d full texts (%s), %d summaries (%d failed), %d benchmarks, failed sources: %s",
            result["duration"], result["new_articles"], result["new_models"],
            result["price_changes"], result["fulltexts"], json.dumps(result["fulltext_counts"]),
            result["summaries"], result["summary_failures"], result["benchmarks"],
            ", ".join(result["failed_sources"]) or "none",
        )
        return result
    except Exception as e:
        log.exception("refresh crashed")
        err = {"finished": db.now_ts(), "message": f"Refresh failed: {e}", "error": True}
        db.set_meta_json(conn, "last_result", err)
        return err
    finally:
        try:
            _set_status(conn, running=False, message="")
        finally:
            conn.close()
            lock.release()


def result_message(r: dict) -> str:
    return i18n.result_message(r)  # English outside a web request


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "trafilatura", "htmldate", "courlan"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    setup_logging()
    db.init_db()
    if "--translate" in sys.argv[1:]:
        return translate_backlog()
    result = asyncio.run(run_refresh())
    if result is None:
        print("A refresh is already running; not starting another one.")
        return 1
    print(result.get("message"))
    for err in result.get("errors", []):
        print("  error:", err)
    return 0 if not result.get("error") else 2


def translate_backlog() -> int:
    """Translate every title, teaser and weekly summary not translated yet."""
    if not settings.summary_provider():
        print("No AI provider configured (GROQ_API_KEY, ANTHROPIC_API_KEY or OLLAMA_MODEL).")
        return 1
    lock = RefreshLock()
    if not lock.acquire():
        print("A refresh is running; try again when it is done.")
        return 1
    try:
        r = asyncio.run(translate.translate_all(progress=lambda m: print(m, flush=True)))
    finally:
        lock.release()
    a = r["articles"]
    print(f"Done: {a['ok']} articles translated, {a['failed']} failed; "
          f"weekly summaries translated: {', '.join(r['weekly']) or 'none'}")
    return 0 if not a["failed"] else 2


if __name__ == "__main__":
    sys.exit(main())
