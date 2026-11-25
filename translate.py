"""Translations of the content between English and French.

- Titles and teasers: during each refresh, 10 articles per AI call, newest first.
  The AI also tells the language of the original, so French sources (CNIL)
  get English titles.
- Full article text: on request from the article page (it is long, and most
  articles are never opened).
- Weekly summaries: written in English, translated once into weekly/<week>.fr.md.

Every translation is stored, so a text is translated only once.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3

import httpx

import db
import enrich
import llm
import settings
import weekly

log = logging.getLogger("portal.translate")

LANG_NAMES = {"en": "English", "fr": "French"}
BATCH = 10
CONCURRENCY = 2
MAX_ATTEMPTS = 3
CHUNK_CHARS = 6000          # full text is translated in pieces of this size
MAX_TEXT_CHARS = 40_000     # longer texts are cut (about 7 000 words)

SYSTEM = """You translate the titles and teasers of AI news articles between English and French.

Rules:
- Translate faithfully. Never invent, remove or explain facts.
- Keep names of people, companies, products and models, code, numbers and units unchanged.
- Plain, natural language. No hype.
- When a text is already in the target language, return it unchanged.

For each item return:
- "id": unchanged,
- "lang": the ISO 639-1 code of the original title's language ("en", "fr", ...),
- "title_en", "title_fr": the title in English and in French,
- "teaser_en", "teaser_fr": the teaser in English and in French (empty when the teaser is empty)."""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "lang": {"type": "string"},
                    "title_en": {"type": "string"},
                    "title_fr": {"type": "string"},
                    "teaser_en": {"type": "string"},
                    "teaser_fr": {"type": "string"},
                },
                "required": ["id", "lang", "title_en", "title_fr", "teaser_en", "teaser_fr"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

TEXT_SYSTEM = """Translate the user's text into {language}.

- Translate everything, faithfully. Do not summarize, shorten, add or comment.
- Keep the paragraphs: one blank line between paragraphs, as in the original.
- Keep names, model names, code, numbers and units unchanged.
- Return only the translation."""

WEEKLY_SYSTEM = """Translate this weekly AI news briefing from English into French.

- Keep the Markdown exactly: the first line "# title", headings, bullets, bold text, links.
- Never change a URL. Source names in link texts (TechCrunch, CNIL, OpenAI...) stay as they are.
- Translate faithfully: never add, remove or explain facts. Keep names, numbers and units.
- Return only the translated Markdown."""


def _json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise llm.LLMError("no JSON in the answer")
        return json.loads(m.group(0))


# --------------------------------------------------------------- titles
async def translate_articles(conn: sqlite3.Connection, client: httpx.AsyncClient,
                             progress, limit: int | None = None) -> dict:
    """Translate titles and teasers of articles that have none yet."""
    counts = {"ok": 0, "failed": 0}
    if not settings.summary_provider():
        return counts
    limit = settings.translate_limit() if limit is None else limit
    rows = conn.execute(
        "SELECT id, title, summary FROM articles "
        "WHERE (tr_status IS NULL OR tr_status = 'failed') AND tr_attempts < ? "
        "ORDER BY CASE WHEN category = 'Research' THEN 1 ELSE 0 END, "
        "COALESCE(published, fetched) DESC LIMIT ?",
        (MAX_ATTEMPTS, max(limit, 0))).fetchall()
    if not rows:
        return counts
    batches = [rows[i:i + BATCH] for i in range(0, len(rows), BATCH)]
    done = 0
    sem = asyncio.Semaphore(CONCURRENCY)
    await enrich._report(progress, f"Translating titles 0/{len(rows)}")

    async def one(batch):
        nonlocal done
        async with sem:
            items = [{"id": r["id"], "title": r["title"], "teaser": r["summary"] or ""} for r in batch]
            got: dict[str, dict] = {}
            try:
                text, _ = await llm.complete(
                    client, SYSTEM, json.dumps({"items": items}, ensure_ascii=False),
                    schema=SCHEMA, max_tokens=8000)
                got = {str(it.get("id")): it for it in _json(text).get("items", []) if isinstance(it, dict)}
            except Exception as e:
                log.warning("title translation failed: %s", e)
            for r in batch:
                it = got.get(r["id"])
                title_en = str((it or {}).get("title_en") or "").strip()
                title_fr = str((it or {}).get("title_fr") or "").strip()
                if not (title_en and title_fr):
                    conn.execute("UPDATE articles SET tr_status='failed', tr_attempts=tr_attempts+1 "
                                 "WHERE id=?", (r["id"],))
                    counts["failed"] += 1
                    continue
                lang = str(it.get("lang") or "en").strip().lower()[:2] or "en"
                conn.execute(
                    "UPDATE articles SET src_lang=?, title_en=?, title_fr=?, teaser_en=?, teaser_fr=?, "
                    "tr_status='ok', tr_attempts=tr_attempts+1 WHERE id=?",
                    (lang, title_en, title_fr, str(it.get("teaser_en") or "").strip() or None,
                     str(it.get("teaser_fr") or "").strip() or None, r["id"]))
                counts["ok"] += 1
            conn.commit()
            done += len(batch)
            await enrich._report(progress, f"Translating titles {done}/{len(rows)}")

    await asyncio.gather(*(one(b) for b in batches))
    return counts


# ------------------------------------------------------------ full text
def _chunks(text: str) -> list[str]:
    out, cur = [], ""
    for para in text[:MAX_TEXT_CHARS].split("\n\n"):
        if cur and len(cur) + len(para) > CHUNK_CHARS:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


async def translate_text(client: httpx.AsyncClient, text: str, target: str) -> tuple[str, str]:
    system = TEXT_SYSTEM.format(language=LANG_NAMES[target])
    parts, model = [], ""
    for chunk in _chunks(text):
        out, model = await llm.complete(client, system, chunk, max_tokens=12000)
        out = out.strip()
        if len(out) < len(chunk) * 0.4:  # a translation is never much shorter
            raise llm.LLMError("translation too short")
        parts.append(out)
    return "\n\n".join(parts), model


async def translate_article_text(conn: sqlite3.Connection, client: httpx.AsyncClient,
                                 article_id: str, target: str) -> str:
    """Translate one article's full text into `target` and store it."""
    if target not in LANG_NAMES:
        raise ValueError("unknown language")
    row = conn.execute("SELECT content FROM articles WHERE id=?", (article_id,)).fetchone()
    if not row or not row["content"]:
        raise llm.LLMError("no text to translate")
    text, model = await translate_text(client, row["content"], target)
    conn.execute(f"UPDATE articles SET content_{target}=?, content_tr_model=? WHERE id=?",
                 (enrich.normalize_paragraphs(text), model, article_id))
    conn.commit()
    return text


# --------------------------------------------------------------- weekly
async def translate_weekly(client: httpx.AsyncClient, progress, limit: int = 3) -> list[str]:
    """Write weekly/<week>.fr.md for English weekly summaries that have none."""
    if not settings.summary_provider():
        return []
    done: list[str] = []
    for d in weekly.list_all(lang="en"):
        if len(done) >= limit:
            break
        if weekly.translation_path(d["week"], "fr").is_file():
            continue
        await enrich._report(progress, f"Translating weekly summary {d['week']}")
        try:
            text, model = await llm.complete(
                client, WEEKLY_SYSTEM, f"# {d['title']}\n\n{d['body']}",
                max_tokens=16000, effort="low")
            title, body = weekly.split_title(text)
            allowed = set(re.findall(r"\]\((\S+?)\)", d["body"]))
            body = weekly.clean_links(body, allowed)
            if len(body) < len(d["body"]) * 0.5 or not title:
                raise llm.LLMError("translation too short")
            weekly.save(d["week"], title, body, f"Traduit par {model} depuis la version anglaise",
                        lang="fr")
            done.append(d["week"])
            log.info("weekly summary %s translated into French", d["week"])
        except Exception as e:
            log.warning("weekly translation %s failed: %s", d["week"], e)
    return done


# ------------------------------------------------------------------ CLI
async def translate_all(progress=print) -> dict:
    """Translate every pending title and weekly summary (make translate)."""
    conn = db.connect()
    try:
        db.migrate(conn)
        headers = {"User-Agent": settings.user_agent()}
        async with httpx.AsyncClient(headers=headers, timeout=60) as client:
            arts = await translate_articles(conn, client, progress, limit=1_000_000)
            weeks = await translate_weekly(client, progress, limit=100)
        return {"articles": arts, "weekly": weeks}
    finally:
        conn.close()
