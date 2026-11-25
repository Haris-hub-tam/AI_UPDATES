"""Weekly summaries: one Markdown file per ISO week in weekly/ (e.g. 2026-W47.md).

Files can be written by hand or by the refresh: when AI summaries are on, each
refresh writes the summary of the last complete week(s) if the file is missing.
Existing files are never overwritten, so hand edits are safe.

File format:
    ---
    title: Headline of the week
    week: 2026-W47
    author: who or what wrote it
    written: 2026-11-23
    ---
    Markdown body (raw HTML is not rendered).
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from markdown_it import MarkdownIt
from markupsafe import Markup

import enrich
import i18n
import llm
import settings

log = logging.getLogger("portal.weekly")

WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
MIN_ARTICLES = 15        # fewer articles in a week -> no automatic summary
BACKFILL_WEEKS = 2       # complete weeks checked on each refresh
MAX_INPUT_ARTICLES = 160
MAX_INPUT_CHARS = 60_000

_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})


def _external_links(state) -> None:
    for tok in state.tokens:
        for child in tok.children or []:
            if child.type == "link_open" and str(child.attrGet("href") or "").startswith("http"):
                child.attrSet("target", "_blank")
                child.attrSet("rel", "noopener noreferrer")


_md.core.ruler.push("external_links", _external_links)


# ------------------------------------------------------------------ weeks
def week_of(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_bounds(week: str) -> tuple[date, date] | None:
    """Monday and Sunday of an ISO week, or None for an invalid id."""
    m = WEEK_RE.match(week or "")
    if not m:
        return None
    try:
        monday = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError:
        return None
    return monday, monday + timedelta(days=6)


def period_label(week: str) -> str:
    b = week_bounds(week)
    if not b:
        return ""
    start, end = b
    if start.month == end.month:
        return f"{start.day}–{end.day} {end:%B %Y}"
    if start.year == end.year:
        return f"{start.day} {start:%b} – {end.day} {end:%b %Y}"
    return f"{start.day} {start:%b %Y} – {end.day} {end:%b %Y}"


def period_display(week: str) -> str:
    """period_label in the interface language (the AI prompt keeps English)."""
    if i18n.lang() == "en":
        return period_label(week)
    b = week_bounds(week)
    if not b:
        return ""
    start, end = b
    if start.month == end.month:
        return f"{start.day}–{i18n.date_long(end)}"
    if start.year == end.year:
        return f"{i18n.date_short(start).rsplit(' ', 1)[0]} – {i18n.date_short(end)}"
    return f"{i18n.date_short(start)} – {i18n.date_short(end)}"


def complete_weeks(today: date | None = None, count: int = BACKFILL_WEEKS) -> list[str]:
    """The last `count` ISO weeks that have fully ended, newest first."""
    today = today or datetime.now(timezone.utc).date()
    return [week_of(today - timedelta(days=7 * i)) for i in range(1, count + 1)]


# ------------------------------------------------------------------ files
def weekly_dir() -> Path:
    return Path(settings.get("WEEKLY_DIR", str(settings.BASE_DIR / "weekly")))


def parse(text: str) -> tuple[dict[str, str], str]:
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        lines = text.splitlines()
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                body = "\n".join(lines[i + 1:])
                break
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip().lower()] = value.strip()
    return meta, body.strip()


def translation_path(week: str, lang: str) -> Path:
    """English is <week>.md; a translation is <week>.<lang>.md."""
    return weekly_dir() / (f"{week}.md" if lang == "en" else f"{week}.{lang}.md")


def _localized(week: str, lang: str | None) -> tuple[Path, bool]:
    """The file to show for `lang` (default: interface language), and whether it is a translation."""
    lang = lang or i18n.lang()
    if lang != "en":
        tr = translation_path(week, lang)
        if tr.is_file():
            return tr, True
    return translation_path(week, "en"), False


def _digest(week: str, path: Path, translated: bool = False) -> dict | None:
    try:
        meta, body = parse(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    title = meta.get("title") or f"Week {week}"
    return {
        "week": week,
        "title": title,
        "author": meta.get("author", ""),
        "written": meta.get("written", ""),
        "period": period_display(week),
        "body": body,
        "excerpt": excerpt(body),
        "translated": translated,
    }


def load(week: str, lang: str | None = None) -> dict | None:
    """The week's summary, in `lang` (default: interface language) when translated."""
    if not week_bounds(week) or not translation_path(week, "en").is_file():
        return None
    path, translated = _localized(week, lang)
    return _digest(week, path, translated)


def list_all(lang: str | None = None) -> list[dict]:
    d = weekly_dir()
    if not d.is_dir():
        return []
    out = []
    for path in d.glob("*.md"):
        if week_bounds(path.stem):  # English originals only; <week>.fr.md is not a week id
            dg = load(path.stem, lang)
            if dg:
                out.append(dg)
    return sorted(out, key=lambda x: x["week"], reverse=True)


def excerpt(body: str, limit: int = 320) -> str:
    """First real paragraph as plain text."""
    for para in re.split(r"\n\s*\n", body):
        p = para.strip()
        if not p or p.startswith(("#", "-", "*", ">", "|")):
            continue
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", p)
        text = re.sub(r"[*_`]+", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > limit:
            text = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
        return text
    return ""


def render(body: str) -> Markup:
    return Markup(_md.render(body))


def save(week: str, title: str, body: str, author: str, lang: str = "en") -> Path:
    d = weekly_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = translation_path(week, lang)
    title = re.sub(r"\s+", " ", title).strip()
    text = (f"---\ntitle: {title}\nweek: {week}\nauthor: {author}\n"
            f"written: {datetime.now(timezone.utc):%Y-%m-%d}\n---\n\n{body.strip()}\n")
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------ AI writing
SYSTEM_PROMPT = """You write a weekly briefing on AI news for a busy, technical reader in Europe.

Rules:
- Use only the articles provided. Never invent facts, numbers, names, quotes or links.
- Link every item to its source with the exact URL given, as a Markdown link.
- Group duplicate coverage of the same story into one item.
- Plain, factual English. No hype, no filler, no emojis.

Format (Markdown):
# <headline naming the one or two biggest stories>

<2-3 sentence overview of the week>

## Models and products
## Industry and money
## Regulation and policy
## Security and safety
## Research and science
## Worth reading

Under each heading, 2-6 bullets: "- **Short lead-in.** One or two sentences. [Source name](url)".
Skip a heading when nothing fits it. French sources (CNIL, ANSSI) are summarized in English.
Stay under 900 words."""


class DigestError(Exception):
    pass


def week_articles(conn: sqlite3.Connection, week: str) -> list[sqlite3.Row]:
    start, end = week_bounds(week)
    lo = f"{start.isoformat()}T00:00:00Z"
    hi = f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z"
    return conn.execute(
        "SELECT title, url, source, category, published, summary, ai_tldr FROM articles "
        "WHERE published >= ? AND published < ? "
        "ORDER BY CASE category WHEN 'Research' THEN 1 ELSE 0 END, published",
        (lo, hi)).fetchall()


def build_prompt(week: str, arts: list[sqlite3.Row]) -> str:
    research = 0
    lines: list[str] = []
    size = 0
    for a in arts:
        if a["category"] == "Research":
            research += 1
            if research > 25:
                continue
        text = (a["ai_tldr"] or a["summary"] or "").replace("\n", " ")[:300]
        entry = (f"- [{a['category']}] {a['source']} | {(a['published'] or '')[:10]} | {a['title']}\n"
                 f"  URL: {a['url']}\n  {text}\n")
        if size + len(entry) > MAX_INPUT_CHARS or len(lines) >= MAX_INPUT_ARTICLES:
            break
        lines.append(entry)
        size += len(entry)
    return (f"Week {week} ({period_label(week)}). {len(lines)} articles:\n\n" + "".join(lines)
            + "\nWrite the weekly briefing.")


def clean_links(body: str, allowed: set[str]) -> str:
    """Drop links whose URL was not in the input (keeps the link text)."""
    def fix(m: re.Match) -> str:
        return m.group(0) if m.group(2) in allowed else m.group(1)
    return re.sub(r"\[([^\]]+)\]\((\S+?)\)", fix, body)


def split_title(text: str) -> tuple[str, str]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n|\n```$", "", text).strip()
    first, _, rest = text.partition("\n")
    if first.startswith("# "):
        return first[2:].strip(), rest.strip()
    return "", text


async def write_week(conn: sqlite3.Connection, client: httpx.AsyncClient, week: str) -> Path:
    arts = week_articles(conn, week)
    prompt = build_prompt(week, arts)
    try:
        text, model = await llm.complete(client, SYSTEM_PROMPT, prompt,
                                         max_tokens=12000, effort="medium")
    except llm.LLMError as e:
        raise DigestError(str(e)) from e
    title, body = split_title(text)
    body = clean_links(body, {a["url"] for a in arts})
    if len(body) < 200:
        raise DigestError("summary too short")
    return save(week, title or f"AI news, {period_label(week)}", body,
                f"AI-written by {model} from {len(arts)} articles")


async def write_missing(conn: sqlite3.Connection, client: httpx.AsyncClient,
                        progress) -> dict:
    """Write summaries for recently completed weeks that have none yet."""
    out: dict = {"written": [], "failed": []}
    if not settings.summary_provider():
        return out
    for week in complete_weeks():
        if load(week) or len(week_articles(conn, week)) < MIN_ARTICLES:
            continue
        await enrich._report(progress, f"Writing weekly summary {week}")
        try:
            path = await write_week(conn, client, week)
            log.info("weekly summary written: %s", path)
            out["written"].append(week)
        except Exception as e:
            log.warning("weekly summary %s failed: %s", week, e)
            out["failed"].append(week)
    return out
