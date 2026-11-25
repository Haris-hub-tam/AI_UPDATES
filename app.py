"""AI portal web app. Start with: uv run uvicorn app:app --host 0.0.0.0 --port 8801

Starting the app never refreshes data; only the "Refresh now" button
(POST /refresh) or `uv run collector.py` does.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import collector
import db
import i18n
import settings
import translate
import weekly

collector.setup_logging()
log = logging.getLogger("portal.app")

PER_PAGE = 25
MODELS_PER_PAGE = 60
NEW_VISIT_GAP = timedelta(minutes=30)
CATEGORY_ORDER = ["Labs", "News", "Analysis", "Regulation", "Research", "Community"]

_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # create tables / add missing columns. No refresh here.
    yield


app = FastAPI(title="AI updates", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(settings.BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(settings.BASE_DIR / "templates"))


# ================================================================ filters
def tone(name: str | None) -> int:
    return int(hashlib.sha1((name or "").encode()).hexdigest(), 16) % 6


def _now() -> datetime:
    return datetime.now(timezone.utc)


def reltime(ts: str | None) -> str:
    dt = db.parse_ts(ts)
    if not dt:
        return ""
    s = (_now() - dt).total_seconds()
    if s < 60:
        return i18n.tr("just now")
    if s < 3600:
        return i18n.tr("{n} min ago", n=int(s // 60))
    if s < 86400:
        return i18n.tr("{n} h ago", n=int(s // 3600))
    if s < 86400 * 30:
        d = int(s // 86400)
        return i18n.tr("{n} day ago" if d == 1 else "{n} days ago", n=d)
    return i18n.date_short(local(dt).date())


def local(dt: datetime) -> datetime:
    return dt.astimezone()  # server local timezone


def fmt_date(ts: str | None, with_time: bool = False) -> str:
    dt = db.parse_ts(ts)
    if not dt:
        return ""
    dt = local(dt)
    out = i18n.date_long(dt.date())
    return out + dt.strftime(", %H:%M") if with_time else out


def fmt_short_date(ts: str | None) -> str:
    dt = db.parse_ts(ts)
    return i18n.date_short(local(dt).date()) if dt else ""


def day_label(ts: str | None) -> str:
    dt = db.parse_ts(ts)
    if not dt:
        return i18n.tr("Earlier")
    d = local(dt).date()
    today = local(_now()).date()
    if d == today:
        return i18n.tr("Today")
    if d == today - timedelta(days=1):
        return i18n.tr("Yesterday")
    return i18n.weekday_label(d, with_year=d.year != today.year)


def fmt_price(p) -> str:
    if p is None or p < 0:
        return "—"
    if p == 0:
        return i18n.tr("Free")
    if p < 0.01:
        return f"${p:.4f}".rstrip("0")
    if p < 1:
        txt = f"{p:.3f}".rstrip("0")
        return "$" + (txt + "0" if len(txt.split(".")[1]) < 2 else txt)
    return f"${p:,.2f}"


def fmt_tokens(n) -> str:
    if not n:
        return "—"
    n = int(n)
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1000:
        return f"{round(n / 1000)}K"
    return str(n)


def fmt_num(v, digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:,.{digits}f}"


def read_minutes(words: int | None) -> int:
    return max(1, round((words or 0) / 230))


def paragraphs(text: str | None) -> list[str]:
    return [p.strip() for p in (text or "").split("\n\n") if p.strip()]


def loads(v, default=None):
    try:
        return json.loads(v) if v else (default if default is not None else [])
    except ValueError:
        return default if default is not None else []


for _name, _fn in {
    "tone": tone, "reltime": reltime, "fmt_date": fmt_date, "fmt_short_date": fmt_short_date,
    "fmt_price": fmt_price, "fmt_tokens": fmt_tokens, "fmt_num": fmt_num,
    "read_minutes": read_minutes, "paragraphs": paragraphs, "loads": loads,
}.items():
    templates.env.filters[_name] = _fn


# ================================================================ helpers
def get_conn() -> sqlite3.Connection:
    return db.connect()


def like_terms(q: str, columns: list[str]) -> tuple[str, list[str]]:
    """AND of terms; each term must match any of the columns (LIKE, escaped)."""
    clauses, params = [], []
    for term in q.split()[:8]:
        esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in columns) + ")")
        params.extend([f"%{esc}%"] * len(columns))
    return " AND ".join(clauses), params


ARTICLE_SEARCH_COLS = ["title", "summary", "ai_tldr", "ai_points", "ai_why",
                       "title_en", "title_fr", "teaser_fr", "ai_tldr_fr"]
CARD_COLS = ("id, url, title, summary, source, category, published, fetched, image, "
             "word_count, ai_tldr, ai_status, fulltext_status, src_lang, title_en, title_fr, "
             "teaser_en, teaser_fr, ai_tldr_fr")


def localize(d: dict) -> dict:
    """Use the translated title, teaser and AI summary for the interface language.
    Whatever is not translated yet stays in its original language."""
    lang = i18n.lang()
    d["orig_title"] = d.get("title")
    if lang != (d.get("src_lang") or "en"):
        d["title"] = d.get(f"title_{lang}") or d["title"]
        d["summary"] = d.get(f"teaser_{lang}") or d.get("summary")
    if lang == "fr" and d.get("ai_tldr_fr"):
        d["ai_tldr"] = d["ai_tldr_fr"]
        if "ai_points_fr" in d:
            d["ai_points"] = d.get("ai_points_fr") or d.get("ai_points")
            d["ai_why"] = d.get("ai_why_fr") or d.get("ai_why")
    return d


def card(row: sqlite3.Row, baseline: str | None = None) -> dict:
    d = localize(dict(row))
    d["tone"] = tone(d["category"])
    d["is_new"] = bool(baseline and d.get("fetched") and d["fetched"] > baseline)
    d["blurb"] = d.get("ai_tldr") or d.get("summary") or ""
    d["when"] = d.get("published") or d.get("fetched")
    return d


def failing_sources(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT name, last_error FROM sources WHERE last_error IS NOT NULL "
        "AND (last_success IS NULL OR last_error_at > last_success) ORDER BY name").fetchall()


def use_request_lang(request: Request) -> str:
    """Pick the interface language for this request (cookie, else browser)."""
    lang = i18n.pick(request.cookies.get(i18n.COOKIE), request.headers.get("accept-language"))
    i18n.set_lang(lang)
    return lang


def common_context(request: Request, conn, active: str) -> dict:
    lang = use_request_lang(request)
    status = db.get_meta_json(conn, "refresh_status", {}) or {}
    running = collector.refresh_running()
    last_result = db.get_meta_json(conn, "last_result", None)
    show_result = None
    if last_result and request.cookies.get("seen_result") != last_result.get("finished"):
        show_result = last_result
    return {
        "request": request,
        "active": active,
        "last_refresh": db.get_meta(conn, "last_refresh"),
        "running": running,
        "progress": i18n.progress(status.get("message", "")) if running else "",
        "failing": failing_sources(conn),
        "summaries_on": settings.summary_provider() is not None,
        "benchmarks_on": bool(settings.aa_key()),
        "show_result": show_result,
        "q": request.query_params.get("q", ""),
        "lang": lang,
        "lang_name": {"en": "English", "fr": "French"}[lang],
    }


def render(name: str, ctx: dict, status_code: int = 200) -> HTMLResponse:
    resp = templates.TemplateResponse(ctx["request"], name, ctx, status_code=status_code)
    if ctx.get("show_result"):
        resp.set_cookie("seen_result", ctx["show_result"].get("finished") or "",
                        max_age=86400 * 365, samesite="lax", httponly=True)
    return resp


def not_found(request: Request, conn, what: str) -> HTMLResponse:
    ctx = common_context(request, conn, "")
    ctx["what"] = what
    return render("404.html", ctx, status_code=404)


def page_url(request: Request, **changes) -> str:
    params = dict(request.query_params)
    for k, v in changes.items():
        if v in (None, "", 1) and k == "page":
            params.pop(k, None)
        elif v in (None, ""):
            params.pop(k, None)
        else:
            params[k] = v
    qs = urlencode(params)
    return request.url.path + ("?" + qs if qs else "")


templates.env.globals["page_url"] = page_url
templates.env.globals["tone"] = tone
templates.env.globals["_"] = i18n.gettext
templates.env.globals["_n"] = i18n.ngettext
templates.env.globals["LANGS"] = i18n.LANGS
templates.env.globals["result_text"] = i18n.result_message


def visit_baseline(request: Request) -> tuple[str | None, dict]:
    """Articles fetched after the baseline are 'New'. The baseline is the
    previous visit; page views within 30 minutes count as the same visit."""
    now = db.now_ts()
    last = request.cookies.get("visit_last")
    base = request.cookies.get("visit_base")
    last_dt = db.parse_ts(last)
    if last_dt is None:
        base = None  # first visit ever: nothing is "new"
        new_base = now
    elif _now() - last_dt > NEW_VISIT_GAP:
        base = last
        new_base = last
    else:
        new_base = base or last
        base = base or last
    return base, {"visit_base": new_base, "visit_last": now}


# ================================================================== News
@app.get("/", response_class=HTMLResponse)
def news(request: Request, category: str = "", source: str = "", q: str = "", page: int = 1):
    page = max(1, min(page, 10_000))
    conn = get_conn()
    try:
        ctx = common_context(request, conn, "news")
        baseline, cookies = visit_baseline(request)
        where, params = [], []
        if q.strip():
            clause, p = like_terms(q, ARTICLE_SEARCH_COLS)
            where.append(clause)
            params += p
        base_where, base_params = list(where), list(params)
        if source:
            where.append("source = ?")
            params.append(source)
        cat_where, cat_params = list(where), list(params)
        if category:
            where.append("category = ?")
            params.append(category)
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(f"SELECT COUNT(*) FROM articles{wsql}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {CARD_COLS} FROM articles{wsql} ORDER BY COALESCE(published, fetched) DESC "
            "LIMIT ? OFFSET ?", params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()
        cards = [card(r, baseline) for r in rows]

        feature = None
        if cards and page == 1 and not (category or source or q.strip()):
            feature = cards.pop(0)
        groups: list[tuple[str, list[dict]]] = []
        for c in cards:
            label = day_label(c["when"])
            if not groups or groups[-1][0] != label:
                groups.append((label, []))
            groups[-1][1].append(c)

        cwsql = (" WHERE " + " AND ".join(cat_where)) if cat_where else ""
        cat_counts = {r[0]: r[1] for r in conn.execute(
            f"SELECT category, COUNT(*) FROM articles{cwsql} GROUP BY category", cat_params)}
        cats = sorted(cat_counts.items(), key=lambda kv: (
            CATEGORY_ORDER.index(kv[0]) if kv[0] in CATEGORY_ORDER else 99, kv[0]))
        bwsql = (" WHERE " + " AND ".join(base_where)) if base_where else ""
        sources = conn.execute(
            f"SELECT source, COUNT(*) AS n FROM articles{bwsql} GROUP BY source "
            "ORDER BY source COLLATE NOCASE", base_params).fetchall()
        day_ago = db.to_ts(_now() - timedelta(hours=24))
        recent = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT source) FROM articles "
            "WHERE COALESCE(published, fetched) >= ?", (day_ago,)).fetchone()
        any_articles = conn.execute("SELECT 1 FROM articles LIMIT 1").fetchone() is not None
        ctx.update({
            "feature": feature, "groups": groups, "total": total, "page": page,
            "has_older": page * PER_PAGE < total, "cats": cats, "category": category,
            "sources": sources, "source": source, "recent_count": recent[0],
            "recent_sources": recent[1], "any_articles": any_articles,
            "all_count": sum(cat_counts.values()),
        })
        resp = render("news.html", ctx)
        for k, v in cookies.items():
            resp.set_cookie(k, v, max_age=86400 * 365, samesite="lax", httponly=True)
        return resp
    finally:
        conn.close()


# ================================================================ Article
@app.get("/article/{article_id}", response_class=HTMLResponse)
def article(request: Request, article_id: str):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
        if not row:
            return not_found(request, conn, "article")
        ctx = common_context(request, conn, "news")
        a = localize(dict(row))
        a["tone"] = tone(a["category"])
        a["points"] = loads(a.get("ai_points"))
        a["when"] = a.get("published") or a.get("fetched")
        lang = i18n.lang()
        # full text: translated version when there is one, unless ?original=1
        a["needs_translation"] = bool(a.get("content")) and lang != (a.get("src_lang") or "en")
        a["translated_text"] = a.get(f"content_{lang}") if a["needs_translation"] else None
        a["show_original"] = request.query_params.get("original") == "1"
        if a["translated_text"] and not a["show_original"]:
            a["content"] = a["translated_text"]
        a["can_translate"] = a["needs_translation"] and not a["translated_text"] \
            and settings.summary_provider() is not None
        a["tr_error"] = request.query_params.get("tr_error") == "1"
        related = conn.execute(
            f"SELECT {CARD_COLS} FROM articles WHERE category = ? AND id != ? "
            "ORDER BY COALESCE(published, fetched) DESC LIMIT 3",
            (a["category"], a["id"])).fetchall()
        ctx.update({"a": a, "related": [card(r) for r in related],
                    "host": urlsplit(a["url"]).hostname or a["source"]})
        return render("article.html", ctx)
    finally:
        conn.close()


# ================================================================= Models
MODEL_SORTS = {
    "newest": "COALESCE(created, first_seen) DESC, name COLLATE NOCASE",
    "smartest": None,  # sorted in Python by benchmark score
    "cheapest_in": "CASE WHEN price_in < 0 THEN 1 ELSE 0 END, price_in ASC, name COLLATE NOCASE",
    "cheapest_out": "CASE WHEN price_out < 0 THEN 1 ELSE 0 END, price_out ASC, name COLLATE NOCASE",
    "context": "context_length DESC, name COLLATE NOCASE",
    "name": "name COLLATE NOCASE ASC",
}
SORT_LABELS = {"newest": "Newest", "smartest": "Smartest", "cheapest_in": "Cheapest input",
               "cheapest_out": "Cheapest output", "context": "Largest context", "name": "Name"}


def bench_by_key(conn) -> dict[str, sqlite3.Row]:
    """Best benchmark entry (highest intelligence) per normalized name."""
    out: dict[str, sqlite3.Row] = {}
    for b in conn.execute("SELECT * FROM benchmarks WHERE match_key != '' "
                          "ORDER BY COALESCE(intelligence, -1) ASC"):
        out[b["match_key"]] = b  # later (higher) rows win
    return out


def model_card(row: sqlite3.Row, benches: dict, max_intel: float) -> dict:
    d = dict(row)
    d["tone"] = tone(d["provider"])
    b = benches.get(d.get("match_key") or "")
    d["intelligence"] = b["intelligence"] if b else None
    d["intel_pct"] = (100 * d["intelligence"] / max_intel) if d["intelligence"] and max_intel else 0
    d["in_mod"] = loads(d.get("input_modalities"))
    d["out_mod"] = loads(d.get("output_modalities"))
    created = db.parse_ts(d.get("created") or d.get("first_seen"))
    d["is_new"] = bool(created and _now() - created < timedelta(days=14))
    return d


def price_changes(conn, days: int = 30, model_id: str | None = None, limit: int = 12) -> list[dict]:
    since = db.to_ts(_now() - timedelta(days=days))
    extra, params = "", [since]
    if model_id:
        extra, params = " AND x.model_id = ?", [since, model_id]
    rows = conn.execute(
        "SELECT x.*, m.name, m.provider FROM ("
        " SELECT ph.*, LAG(price_in) OVER w AS old_in, LAG(price_out) OVER w AS old_out"
        " FROM price_history ph WINDOW w AS (PARTITION BY model_id ORDER BY recorded_at, id)"
        ") x JOIN models m ON m.id = x.model_id "
        f"WHERE x.old_in IS NOT NULL AND x.recorded_at >= ?{extra} "
        "ORDER BY x.recorded_at DESC, x.id DESC LIMIT ?", params + [limit]).fetchall()
    out = []
    for r in rows:
        d = dict(r)

        def total(a, b):
            return (max(a or 0, 0)) + (max(b or 0, 0))
        d["cheaper"] = total(d["price_in"], d["price_out"]) < total(d["old_in"], d["old_out"])
        d["tone"] = tone(d["provider"])
        out.append(d)
    return out


@app.get("/models", response_class=HTMLResponse)
def models(request: Request, q: str = "", provider: str = "", sort: str = "newest",
           free: str = "", page: int = 1):
    if sort not in MODEL_SORTS:
        sort = "newest"
    page = max(1, min(page, 10_000))
    conn = get_conn()
    try:
        ctx = common_context(request, conn, "models")
        where, params = [], []
        if q.strip():
            clause, p = like_terms(q, ["name", "id", "description"])
            where.append(clause)
            params += p
        prov_where, prov_params = list(where), list(params)
        if free:
            where.append("price_in = 0 AND price_out = 0")
            prov_where.append("price_in = 0 AND price_out = 0")
        if provider:
            where.append("provider = ?")
            params.append(provider)
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        order = MODEL_SORTS[sort] or MODEL_SORTS["newest"]
        rows = conn.execute(f"SELECT * FROM models{wsql} ORDER BY {order}", params).fetchall()
        benches = bench_by_key(conn)
        max_intel = max((b["intelligence"] or 0 for b in benches.values()), default=0)
        cards = [model_card(r, benches, max_intel) for r in rows]
        if sort == "smartest":
            cards.sort(key=lambda c: (c["intelligence"] is None, -(c["intelligence"] or 0), c["name"].lower()))
        total = len(cards)
        cards = cards[(page - 1) * MODELS_PER_PAGE: page * MODELS_PER_PAGE]
        pwsql = (" WHERE " + " AND ".join(prov_where)) if prov_where else ""
        providers = conn.execute(
            f"SELECT provider, COUNT(*) AS n FROM models{pwsql} GROUP BY provider "
            "ORDER BY provider COLLATE NOCASE", prov_params).fetchall()
        month_ago = db.to_ts(_now() - timedelta(days=30))
        stats = conn.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN COALESCE(created, first_seen) >= ? THEN 1 ELSE 0 END) AS recent, "
            "SUM(CASE WHEN price_in = 0 AND price_out = 0 THEN 1 ELSE 0 END) AS free "
            "FROM models", (month_ago,)).fetchone()
        biggest = conn.execute(
            "SELECT name, context_length FROM models ORDER BY context_length DESC LIMIT 1").fetchone()
        ctx.update({
            "cards": cards, "total": total, "page": page,
            "has_more": page * MODELS_PER_PAGE < total, "providers": providers,
            "provider": provider, "sort": sort, "sort_labels": SORT_LABELS, "free": bool(free),
            "stats": stats, "biggest": biggest, "changes": price_changes(conn, limit=8),
        })
        return render("models.html", ctx)
    finally:
        conn.close()


# ============================================================ Model detail
BENCH_NAMES = {
    "artificial_analysis_intelligence_index": "Intelligence Index",
    "artificial_analysis_coding_index": "Coding Index",
    "artificial_analysis_agentic_index": "Agentic Index",
    "artificial_analysis_math_index": "Math Index",
    "mmlu_pro": "MMLU-Pro", "gpqa": "GPQA Diamond", "gpqa_diamond": "GPQA Diamond",
    "hle": "Humanity's Last Exam", "humanitys_last_exam": "Humanity's Last Exam",
    "livecodebench": "LiveCodeBench", "scicode": "SciCode", "math_500": "MATH-500",
    "aime": "AIME 2024", "aime_24": "AIME 2024", "aime_25": "AIME 2025", "aime25": "AIME 2025",
    "ifbench": "IFBench", "lcr": "AA-LCR (long context)", "terminalbench_hard": "Terminal-Bench Hard",
    "tau2": "τ²-Bench Telecom", "critpt": "CritPt", "gdpval": "GDPval-AA",
    "omniscience": "AA-Omniscience", "humaneval": "HumanEval", "mmmu": "MMMU",
}


def bench_name(key: str) -> str:
    if key in BENCH_NAMES:
        return BENCH_NAMES[key]
    k = key.removeprefix("artificial_analysis_")
    return " ".join(w if w.isupper() else w.capitalize() for w in k.split("_")).replace(" And ", " & ")


def bench_rows(conn, b: sqlite3.Row | None) -> list[dict]:
    if not b:
        return []
    evals = loads(b["evaluations"], {})
    maxima: dict[str, float] = {}
    for other in conn.execute("SELECT evaluations FROM benchmarks"):
        for k, v in loads(other["evaluations"], {}).items():
            if isinstance(v, (int, float)):
                maxima[k] = max(maxima.get(k, 0), v)
    out = []
    for k, v in evals.items():
        if not isinstance(v, (int, float)):
            continue
        is_index = "index" in k
        if not is_index and 0 <= v <= 1:
            display, pct = f"{v * 100:.1f}%", v * 100
        else:
            m = maxima.get(k) or v or 1
            display, pct = f"{v:.1f}", 100 * v / m if m else 0
        out.append({"key": k, "name": bench_name(k), "value": v, "display": display,
                    "pct": max(2, min(100, pct)), "is_index": is_index})
    out.sort(key=lambda r: (not r["is_index"], r["key"] != "artificial_analysis_intelligence_index", r["name"]))
    return out


def news_query_name(name: str) -> str:
    n = name.split(":", 1)[1] if ":" in name else name
    n = re.sub(r"\([^)]*\)", "", n)
    return re.sub(r"\s+", " ", n).strip()


@app.get("/model/{model_id:path}", response_class=HTMLResponse)
def model_detail(request: Request, model_id: str):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
        if not row:
            return not_found(request, conn, "model")
        ctx = common_context(request, conn, "models")
        benches = bench_by_key(conn)
        max_intel = max((b["intelligence"] or 0 for b in benches.values()), default=0)
        m = model_card(row, benches, max_intel)
        b = benches.get(row["match_key"] or "")
        hist = conn.execute(
            "SELECT price_in, price_out, recorded_at FROM price_history WHERE model_id = ? "
            "ORDER BY recorded_at, id", (model_id,)).fetchall()
        chart = {"labels": [], "input": [], "output": [],
                 "names": {"input": i18n.tr("Input"), "output": i18n.tr("Output")}}
        for h in hist:
            chart["labels"].append(fmt_short_date(h["recorded_at"]))
            chart["input"].append(h["price_in"] if h["price_in"] is not None and h["price_in"] >= 0 else None)
            chart["output"].append(h["price_out"] if h["price_out"] is not None and h["price_out"] >= 0 else None)
        if hist:
            chart["labels"].append(i18n.tr("Today"))
            chart["input"].append(chart["input"][-1])
            chart["output"].append(chart["output"][-1])
        more = conn.execute(
            "SELECT * FROM models WHERE provider = ? AND id != ? "
            "ORDER BY COALESCE(created, first_seen) DESC LIMIT 6",
            (row["provider"], model_id)).fetchall()
        news_rows = []
        qname = news_query_name(row["name"])
        if len(qname) >= 3:
            # exact model name anywhere in the article; title mentions first
            esc = qname.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pat = f"%{esc}%"
            news_rows = conn.execute(
                f"SELECT {CARD_COLS} FROM articles WHERE title LIKE ? ESCAPE '\\' "
                "OR summary LIKE ? ESCAPE '\\' OR ai_tldr LIKE ? ESCAPE '\\' "
                "OR ai_points LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' "
                "ORDER BY (title LIKE ? ESCAPE '\\') DESC, COALESCE(published, fetched) DESC LIMIT 6",
                [pat] * 6).fetchall()
        ctx.update({
            "m": m, "b": b, "bench": bench_rows(conn, b), "chart": chart,
            "params": loads(row["supported_params"]),
            "more": [model_card(r, benches, max_intel) for r in more],
            "news": [card(r) for r in news_rows], "qname": qname,
            "changes": price_changes(conn, days=36500, model_id=model_id, limit=50),
        })
        return render("model.html", ctx)
    finally:
        conn.close()


# ============================================================= Benchmarks
BENCH_SORTS = {
    "intelligence": ("intelligence", True), "coding": ("coding", True),
    "agentic": ("agentic", True), "fastest": ("speed", True), "cheapest": ("price_out", False),
}
BENCH_SORT_LABELS = {"intelligence": "Intelligence", "coding": "Coding", "agentic": "Agentic",
                     "fastest": "Fastest", "cheapest": "Cheapest"}


@app.get("/benchmarks", response_class=HTMLResponse)
def benchmarks(request: Request, sort: str = "intelligence", q: str = ""):
    if sort not in BENCH_SORTS:
        sort = "intelligence"
    col, desc = BENCH_SORTS[sort]  # whitelisted column name
    conn = get_conn()
    try:
        ctx = common_context(request, conn, "benchmarks")
        where, params = [f"{col} IS NOT NULL"], []
        if sort == "cheapest":
            where.append("price_out >= 0")
        if q.strip():
            clause, p = like_terms(q, ["name", "creator"])
            where.append(clause)
            params += p
        rows = conn.execute(
            f"SELECT * FROM benchmarks WHERE {' AND '.join(where)} "
            f"ORDER BY {col} {'DESC' if desc else 'ASC'}, name COLLATE NOCASE", params).fetchall()
        maxima = conn.execute(
            "SELECT MAX(intelligence), MAX(coding), MAX(agentic) FROM benchmarks").fetchone()
        model_ids: dict[str, str] = {}
        for m in conn.execute("SELECT id, match_key FROM models WHERE match_key != '' "
                              "ORDER BY (id LIKE '%:%'), LENGTH(id) DESC"):
            model_ids[m["match_key"]] = m["id"]  # later = shorter, non-variant id wins
        items = []
        for i, r in enumerate(rows, 1):
            d = dict(r)
            d["rank"] = i
            d["model_id"] = model_ids.get(r["match_key"] or "")
            d["tone"] = tone(r["creator"])
            for k, mx in zip(("intelligence", "coding", "agentic"), maxima):
                d[k + "_pct"] = (100 * d[k] / mx) if d[k] is not None and mx else 0
            items.append(d)
        total = conn.execute("SELECT COUNT(*) FROM benchmarks").fetchone()[0]
        ctx.update({"items": items, "sort": sort, "sort_labels": BENCH_SORT_LABELS,
                    "bench_total": total})
        return render("benchmarks.html", ctx)
    finally:
        conn.close()


# ================================================================ Weekly
@app.get("/weekly", response_class=HTMLResponse)
def weekly_list(request: Request):
    conn = get_conn()
    try:
        ctx = common_context(request, conn, "weekly")
        ctx["digests"] = weekly.list_all()
        return render("weekly.html", ctx)
    finally:
        conn.close()


@app.get("/weekly/{week}", response_class=HTMLResponse)
def weekly_post(request: Request, week: str):
    conn = get_conn()
    try:
        ctx = common_context(request, conn, "weekly")  # sets the language first
        digest = weekly.load(week)
        if not digest:
            return not_found(request, conn, "weekly summary")
        weeks = [d["week"] for d in weekly.list_all()]
        i = weeks.index(week) if week in weeks else -1
        ctx.update({
            "d": digest,
            "html": weekly.render(digest["body"]),
            "newer": weeks[i - 1] if i > 0 else None,
            "older": weeks[i + 1] if 0 <= i < len(weeks) - 1 else None,
        })
        return render("weekly_post.html", ctx)
    finally:
        conn.close()


# ================================================================ Refresh
def _same_origin(request: Request) -> bool:
    """Refuse refreshes triggered by other websites (CSRF).

    Modern browsers send Sec-Fetch-Site, which is reliable even when the
    Origin header is "null" (e.g. under a no-referrer policy). Older clients
    fall back to comparing Origin with Host; curl & co. send neither.
    """
    site = request.headers.get("sec-fetch-site")
    if site:
        return site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return True
    return urlsplit(origin).netloc == request.headers.get("host", "")


@app.post("/refresh")
async def refresh(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
    started = False
    lock = collector.RefreshLock()
    if lock.acquire():  # atomic: a second click cannot also get the lock
        task = asyncio.create_task(collector.run_refresh(lock))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        started = True
        log.info("refresh started from the web UI")
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"started": started, "running": True})
    form_next = request.query_params.get("next", "/")
    target = form_next if form_next.startswith("/") and not form_next.startswith("//") else "/"
    return RedirectResponse(target, status_code=303)


_translating: set[str] = set()


@app.post("/article/{article_id}/translate")
async def translate_article(request: Request, article_id: str, to: str = "fr"):
    """Translate an article's full text into `to`, then show the article again."""
    if not _same_origin(request):
        return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
    back = f"/article/{article_id}"
    if to not in i18n.LANGS or not settings.summary_provider():
        return RedirectResponse(back + "?tr_error=1", status_code=303)
    key = f"{article_id}:{to}"
    if key not in _translating:  # a double click does not start a second translation
        _translating.add(key)
        conn = get_conn()
        try:
            async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent()}) as client:
                await translate.translate_article_text(conn, client, article_id, to)
            log.info("article %s translated into %s", article_id, to)
        except Exception as e:
            log.warning("translation of article %s failed: %s", article_id, e)
            return RedirectResponse(back + "?tr_error=1", status_code=303)
        finally:
            conn.close()
            _translating.discard(key)
    return RedirectResponse(back, status_code=303)


@app.get("/lang/{code}")
def set_language(code: str, next: str = "/"):
    """Switch the interface language and go back to the page."""
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(target, status_code=303)
    if code in i18n.LANGS:
        resp.set_cookie(i18n.COOKIE, code, max_age=86400 * 365, samesite="lax")
    return resp


@app.get("/api/status")
def status(request: Request):
    use_request_lang(request)
    conn = get_conn()
    try:
        state = db.get_meta_json(conn, "refresh_status", {}) or {}
        running = collector.refresh_running()
        last = db.get_meta(conn, "last_refresh")
        result = db.get_meta_json(conn, "last_result", None)
        return {
            "running": running,
            "message": i18n.progress(state.get("message") or "Starting refresh") if running else "",
            "last_refresh": last,
            "last_refresh_ago": reltime(last) if last else i18n.tr("never"),
            "last_result": i18n.result_message(result) if result else None,
            "last_finished": result.get("finished") if result else None,
        }
    finally:
        conn.close()


@app.exception_handler(404)
async def handle_404(request: Request, exc):
    conn = get_conn()
    try:
        return not_found(request, conn, "page")
    finally:
        conn.close()


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return RedirectResponse("/static/favicon.svg", status_code=301)
