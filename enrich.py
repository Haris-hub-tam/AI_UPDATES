"""Article enrichment: full-text extraction and AI summaries."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from typing import Awaitable, Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import lxml.html
import trafilatura

import llm
import settings

log = logging.getLogger("portal.enrich")

Progress = Callable[[str], Awaitable[None] | None]

MIN_FEED_CHARS = 1500  # feed content:encoded counts as "full text" above this
FULLTEXT_CONCURRENCY = 4
SUMMARY_CONCURRENCY = 3
SUMMARY_INPUT_CHARS = 14_000
MAX_SUMMARY_ATTEMPTS = 5

# Sites that block readers / where the "article" is a discussion page.
BLOCKED_HOSTS = ("reddit.com", "news.ycombinator.com", "redd.it")
# Sites whose feed already carries the useful text (abstracts) - not downloaded.
SKIPPED_HOSTS = ("arxiv.org",)

BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "figcaption"}


# --------------------------------------------------------------------- helpers
def host_matches(url: str, hosts: tuple[str, ...]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


def _clean(text: str) -> str:
    return re.sub(r"[ \t\r\f\v\xa0]+", " ", text).strip()


def html_to_text(html: str) -> str:
    """Convert an HTML fragment into plain text paragraphs separated by blank lines."""
    if not html or not html.strip():
        return ""
    try:
        root = lxml.html.fragment_fromstring(html, create_parent="div")
    except Exception:
        return _clean(re.sub(r"<[^>]+>", " ", html))
    for bad in root.xpath("//script|//style|//noscript|//iframe|//form"):
        bad.drop_tree()
    paras: list[str] = []
    blocks = [el for el in root.iter() if isinstance(el.tag, str) and el.tag in BLOCK_TAGS]
    # keep only innermost block elements to avoid duplicates
    blocks = [el for el in blocks if not any(
        isinstance(d.tag, str) and d.tag in BLOCK_TAGS for d in el.iterdescendants())]
    for el in blocks:
        txt = _clean(el.text_content())
        if txt:
            paras.append(txt)
    if not paras:
        txt = _clean(root.text_content())
        if txt:
            paras = [txt]
    return "\n\n".join(paras)


def first_img(html: str) -> str | None:
    if not html:
        return None
    m = re.search(r"<img[^>]+src=[\"']([^\"']+)[\"']", html, re.I)
    if m and m.group(1).startswith("http"):
        return m.group(1)
    return None


def normalize_paragraphs(text: str) -> str:
    """trafilatura returns one paragraph per line; store blank-line separated."""
    lines = [_clean(l) for l in text.splitlines()]
    return "\n\n".join(l for l in lines if l)


# Glossary tooltips (e.g. CNIL wraps "IA" in <button class="glossary-tooltips">).
# trafilatura drops <button> elements, which would cut those words out of the text.
_TOOLTIP_BUTTON = re.compile(
    r"<button\b[^>]*(?:glossary|tooltip|data-definition)[^>]*>((?:(?!</?button\b).){0,300}?)</button>",
    re.I | re.S)


def unwrap_tooltips(html: str) -> str:
    return _TOOLTIP_BUTTON.sub(r"<span>\1</span>", html)


def word_count(text: str | None) -> int:
    return len(text.split()) if text else 0


# ---------------------------------------------------------------- robots.txt
class RobotsCache:
    """Caches parsed robots.txt per scheme+host (in process, 12 h TTL)."""

    TTL = 12 * 3600

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, RobotFileParser | None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def allowed(self, client: httpx.AsyncClient, url: str, agent: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        lock = self._locks.setdefault(base, asyncio.Lock())
        async with lock:
            cached = self._cache.get(base)
            if not cached or time.time() - cached[0] > self.TTL:
                parser: RobotFileParser | None = RobotFileParser()
                try:
                    r = await client.get(base + "/robots.txt", timeout=10)
                    if r.status_code in (401, 403):
                        parser.disallow_all = True
                    elif r.status_code >= 400:
                        parser = None  # no robots.txt -> everything allowed
                    else:
                        parser.parse(r.text.splitlines())
                except httpx.HTTPError:
                    parser = None
                cached = (time.time(), parser)
                self._cache[base] = cached
        parser = cached[1]
        if parser is None:
            return True
        return parser.can_fetch(agent, url) and parser.can_fetch("ai-portal", url)


ROBOTS = RobotsCache()


# --------------------------------------------------------------- full text
async def _fetch_one(client: httpx.AsyncClient, art: sqlite3.Row) -> tuple[str, str | None, str | None]:
    """Returns (status, content, og_image)."""
    url = art["url"]
    if host_matches(url, BLOCKED_HOSTS):
        return "blocked", None, None
    if host_matches(url, SKIPPED_HOSTS):
        return "skipped", None, None
    if not await ROBOTS.allowed(client, url, settings.user_agent()):
        return "blocked", None, None
    try:
        r = await client.get(url, timeout=25)
    except httpx.HTTPError as e:
        log.info("full text failed for %s: %s", url, e)
        return "failed", None, None
    if r.status_code in (401, 402, 403, 451):
        return "blocked", None, None
    if r.status_code >= 400 or "html" not in r.headers.get("content-type", "html"):
        return "failed", None, None
    html = unwrap_tooltips(r.text)

    def _extract():
        text = trafilatura.extract(
            html, url=url, include_comments=False, include_tables=False,
            favor_precision=True, deduplicate=True,
        )
        image = None
        try:
            meta = trafilatura.extract_metadata(html)
            image = meta.image if meta else None
        except Exception:
            image = None
        return text, image

    text, image = await asyncio.to_thread(_extract)
    if image and not str(image).startswith("http"):
        image = None
    if not text or len(text) < 300:
        return "failed", None, image
    return "ok", normalize_paragraphs(text), image


async def fetch_fulltexts(conn: sqlite3.Connection, client: httpx.AsyncClient,
                          progress: Progress) -> dict:
    """Download readable text for pending articles. Returns counters."""
    limit = settings.fulltext_limit()
    # Mark blocked/skipped hosts immediately - they don't count against the limit.
    pending = conn.execute(
        "SELECT id, url FROM articles WHERE fulltext_status IS NULL").fetchall()
    for row in pending:
        if host_matches(row["url"], BLOCKED_HOSTS):
            conn.execute("UPDATE articles SET fulltext_status='blocked' WHERE id=?", (row["id"],))
        elif host_matches(row["url"], SKIPPED_HOSTS):
            conn.execute("UPDATE articles SET fulltext_status='skipped' WHERE id=?", (row["id"],))
    conn.commit()

    rows = conn.execute(
        "SELECT id, url, image FROM articles WHERE fulltext_status IS NULL "
        "ORDER BY COALESCE(published, fetched) DESC LIMIT ?", (max(limit, 0),)
    ).fetchall()
    counts = {"ok": 0, "failed": 0, "blocked": 0, "skipped": 0}
    if not rows:
        return counts
    total, done = len(rows), 0
    sem = asyncio.Semaphore(FULLTEXT_CONCURRENCY)
    await _report(progress, f"Downloading full articles 0/{total}")

    async def worker(art):
        nonlocal done
        async with sem:
            try:
                status, content, image = await _fetch_one(client, art)
            except Exception as e:  # never let one page break the refresh
                log.warning("full text error for %s: %s", art["url"], e)
                status, content, image = "failed", None, None
        conn.execute(
            "UPDATE articles SET fulltext_status=?, content=COALESCE(?, content), "
            "word_count=CASE WHEN ? IS NULL THEN word_count ELSE ? END, "
            "image=COALESCE(image, ?) WHERE id=?",
            (status, content, content, word_count(content), image, art["id"]),
        )
        conn.commit()
        counts[status] = counts.get(status, 0) + 1
        done += 1
        await _report(progress, f"Downloading full articles {done}/{total}")

    await asyncio.gather(*(worker(a) for a in rows))
    return counts


# ------------------------------------------------------------- AI summaries
SYSTEM_PROMPT = """You summarize AI news articles for a busy, technical reader.

Rules:
- Use ONLY facts stated in the article text. Never invent or guess names, numbers, prices, benchmark scores or dates. If the article does not give a detail, leave it out.
- Prefer concrete facts: who, what, model names, numbers, prices, benchmark scores, dates.
- Write in plain English, no hype, no marketing language.
- Also give a faithful French version of each field, in natural French. Keep names, model names, numbers and units unchanged.

Return a JSON object with exactly these fields:
- "tldr": one sentence (max ~30 words).
- "key_points": a list of 3 to 6 short bullet strings with concrete facts.
- "why_it_matters": one or two sentences on why this matters to someone following AI.
- "tldr_fr", "key_points_fr", "why_it_matters_fr": the same three fields in French.
"""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "tldr": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "why_it_matters": {"type": "string"},
        "tldr_fr": {"type": "string"},
        "key_points_fr": {"type": "array", "items": {"type": "string"}},
        "why_it_matters_fr": {"type": "string"},
    },
    "required": ["tldr", "key_points", "why_it_matters", "tldr_fr", "key_points_fr", "why_it_matters_fr"],
    "additionalProperties": False,
}


SummaryError = llm.LLMError


def _user_prompt(art: sqlite3.Row) -> str:
    body = art["content"] or art["summary"] or ""
    body = body[:SUMMARY_INPUT_CHARS]
    return (
        f"Source: {art['source']}\nTitle: {art['title']}\n\n"
        f"Article text:\n<article>\n{body}\n</article>\n\n"
        "Summarize this article as the JSON object described."
    )


def parse_summary(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise SummaryError("no JSON in model output")
        try:
            data = json.loads(m.group(0))
        except ValueError as e:
            raise SummaryError(f"bad JSON: {e}") from e
    tldr = str(data.get("tldr") or "").strip()
    points = [str(p).strip() for p in (data.get("key_points") or []) if str(p).strip()]
    why = str(data.get("why_it_matters") or "").strip()
    if not tldr or not points:
        raise SummaryError("summary missing tldr or key points")
    # French is optional: a summary without it is still shown in English
    tldr_fr = str(data.get("tldr_fr") or "").strip()
    points_fr = [str(p).strip() for p in (data.get("key_points_fr") or []) if str(p).strip()]
    why_fr = str(data.get("why_it_matters_fr") or "").strip()
    return {"tldr": tldr, "key_points": points[:6], "why_it_matters": why,
            "tldr_fr": tldr_fr, "key_points_fr": points_fr[:6], "why_it_matters_fr": why_fr}


async def summarize(client: httpx.AsyncClient, art: sqlite3.Row) -> tuple[dict, str]:
    text, model = await llm.complete(client, SYSTEM_PROMPT, _user_prompt(art),
                                     schema=SUMMARY_SCHEMA, max_tokens=6000)
    return parse_summary(text), model


async def write_summaries(conn: sqlite3.Connection, client: httpx.AsyncClient,
                          progress: Progress) -> dict:
    provider = settings.summary_provider()
    counts = {"ok": 0, "failed": 0}
    if not provider:
        return counts
    limit = settings.summary_limit()
    # Articles with text, not yet summarized (or failed earlier). Research
    # papers go last so news gets summarized first.
    rows = conn.execute(
        "SELECT id, url, title, source, summary, content FROM articles "
        "WHERE (ai_status IS NULL OR ai_status='failed') AND ai_attempts < ? "
        "AND (LENGTH(COALESCE(content,'')) > 200 OR LENGTH(COALESCE(summary,'')) > 200) "
        "ORDER BY CASE WHEN category='Research' THEN 1 ELSE 0 END, "
        "COALESCE(published, fetched) DESC LIMIT ?",
        (MAX_SUMMARY_ATTEMPTS, max(limit, 0)),
    ).fetchall()
    if not rows:
        return counts
    total, done = len(rows), 0
    sem = asyncio.Semaphore(SUMMARY_CONCURRENCY)
    await _report(progress, f"Writing AI summaries 0/{total}")

    async def worker(art):
        nonlocal done
        async with sem:
            try:
                summary, model = await summarize(client, art)
                conn.execute(
                    "UPDATE articles SET ai_tldr=?, ai_points=?, ai_why=?, ai_tldr_fr=?, "
                    "ai_points_fr=?, ai_why_fr=?, ai_status='ok', "
                    "ai_model=?, ai_error=NULL, ai_attempts=ai_attempts+1 WHERE id=?",
                    (summary["tldr"], json.dumps(summary["key_points"]),
                     summary["why_it_matters"], summary["tldr_fr"] or None,
                     json.dumps(summary["key_points_fr"]) if summary["key_points_fr"] else None,
                     summary["why_it_matters_fr"] or None, model, art["id"]),
                )
                counts["ok"] += 1
            except Exception as e:
                log.warning("summary failed for %s: %s", art["url"], e)
                conn.execute(
                    "UPDATE articles SET ai_status='failed', ai_error=?, "
                    "ai_attempts=ai_attempts+1 WHERE id=?", (str(e)[:300], art["id"]))
                counts["failed"] += 1
            conn.commit()
            done += 1
            await _report(progress, f"Writing AI summaries {done}/{total}")

    await asyncio.gather(*(worker(a) for a in rows))
    return counts


async def _report(progress: Progress, msg: str) -> None:
    res = progress(msg)
    if asyncio.iscoroutine(res):
        await res
