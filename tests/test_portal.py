"""End-to-end tests with every external HTTP call mocked (respx).

Run: uv run pytest -q
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
import respx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import collector  # noqa: E402
import db  # noqa: E402

NOW = datetime.now(timezone.utc)
LONG_PARA = ("OpenAI released GPT-9 on Tuesday with a 2 million token context window and "
             "prices of $1.25 per million input tokens. ") * 9


def rfc822(dt: datetime) -> str:
    return format_datetime(dt)


RSS_LAB = f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:media="http://search.yahoo.com/mrss/">
<channel><title>Lab</title>
<item><title>Lab launches GPT-9</title><link>https://lab.example/gpt-9</link>
<description>Short teaser about GPT-9.</description>
<pubDate>{rfc822(NOW - timedelta(hours=2))}</pubDate>
<media:content url="https://img.example/gpt9.jpg" medium="image"/>
<content:encoded><![CDATA[<p>{LONG_PARA}</p><p>{LONG_PARA}</p><p>Second paragraph about Claude Test 4.5 pricing.</p>]]></content:encoded>
</item>
<item><title>Lab news on robotics</title><link>https://lab.example/robots</link>
<description>A blog post with an image &lt;img src="https://img.example/robot.png"&gt; and more.</description>
<pubDate>{rfc822(NOW - timedelta(days=1, hours=1))}</pubDate>
</item>
<item><title>Ancient post</title><link>https://lab.example/old</link>
<description>Too old to keep.</description>
<pubDate>{rfc822(NOW - timedelta(days=200))}</pubDate>
</item>
</channel></rss>"""

ATOM_NEWS = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title>
<entry><title>Startup raises money for AI agents</title>
<link href="https://news.example/story-1"/><id>1</id>
<updated>{NOW.strftime('%Y-%m-%dT%H:%M:%SZ')}</updated>
<summary>An AI startup raised $50M.</summary></entry>
<entry><title>Gardening tips for autumn</title>
<link href="https://news.example/garden"/><id>2</id>
<updated>{NOW.strftime('%Y-%m-%dT%H:%M:%SZ')}</updated>
<summary>Nothing about computers here.</summary></entry>
<entry><title>Paywalled LLM story</title>
<link href="https://closed.example/llm"/><id>3</id>
<updated>{NOW.strftime('%Y-%m-%dT%H:%M:%SZ')}</updated>
<summary>Machine learning story on a site that disallows robots.</summary></entry>
</feed>"""

HN_RSS = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>HN</title>
<item><title>Show HN: my LLM thing</title><link>https://news.ycombinator.com/item?id=1</link>
<description>Points: 150</description><pubDate>{rfc822(NOW - timedelta(hours=5))}</pubDate></item>
</channel></rss>"""

ARTICLE_HTML = """<html><head><title>Startup raises money</title>
<meta property="og:image" content="https://img.example/og.jpg"></head>
<body><nav>Menu</nav><article><h1>Startup raises money for AI agents</h1>
""" + "".join(
    f"<p>Paragraph {i}: The company, AgentCo, said it raised $50 million in a Series B round "
    f"led by Example Ventures to build AI agents for logistics companies in Europe.</p>"
    for i in range(8)) + "</article><footer>Copyright</footer></body></html>"

ROBOTS_LAB_HTML = "<html><body><article>" + "".join(
    f"<p>Robots paragraph {i} describing a humanoid robot with 28 joints that costs $20,000.</p>"
    for i in range(8)) + "</article></body></html>"


def openrouter_payload(price_in: str = "0.000003") -> dict:
    return {"data": [
        {"id": "anthropic/claude-test-4.5", "name": "Anthropic: Claude Test 4.5",
         "created": int((NOW - timedelta(days=3)).timestamp()),
         "description": "A test model.\n\nSecond paragraph.", "context_length": 200000,
         "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"],
                          "tokenizer": "Claude"},
         "pricing": {"prompt": price_in, "completion": "0.000015", "input_cache_read": "0.0000003"},
         "top_provider": {"context_length": 200000, "max_completion_tokens": 64000},
         "supported_parameters": ["tools", "max_tokens", "reasoning"]},
        {"id": "meta/llama-free:free", "name": "Meta: Llama Free 3.3 70B (free)",
         "created": int((NOW - timedelta(days=90)).timestamp()), "context_length": 131072,
         "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
         "pricing": {"prompt": "0", "completion": "0"}, "top_provider": {}},
        {"id": "openrouter/auto", "name": "Auto Router", "created": None, "context_length": 2000000,
         "architecture": {}, "pricing": {"prompt": "-1", "completion": "-1"}, "top_provider": {}},
    ]}


AA_PAGE_1 = {"tier": "free", "pagination": {"page": 1, "page_size": 1, "total_pages": 2, "has_more": True},
             "data": [{"id": "aa-1", "name": "Claude 4.5 Test (Reasoning)", "slug": "claude-4-5-test",
                       "model_creator": {"id": "c1", "name": "Anthropic"},
                       "evaluations": {"artificial_analysis_intelligence_index": 61.2,
                                       "artificial_analysis_coding_index": 55.0,
                                       "artificial_analysis_agentic_index": None,
                                       "gpqa": 0.81, "mmlu_pro": 0.87},
                       "pricing": {"price_1m_input_tokens": 3, "price_1m_output_tokens": 15},
                       "performance": {"median_output_tokens_per_second": 71.5,
                                       "median_time_to_first_token_seconds": 1.4}}]}
AA_PAGE_2 = {"tier": "free", "pagination": {"page": 2, "page_size": 1, "total_pages": 2, "has_more": False},
             "data": [{"id": "aa-2", "name": "Llama Free 3.3 70B", "slug": "llama-3-3",
                       "model_creator": {"id": "c2", "name": "Meta"},
                       "evaluations": {"artificial_analysis_intelligence_index": 30.1},
                       "pricing": {"price_1m_input_tokens": 0.1, "price_1m_output_tokens": 0.3},
                       "performance": {"median_output_tokens_per_second": 150.0,
                                       "median_time_to_first_token_seconds": 0.4}}]}

ANTHROPIC_REPLY = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
    "stop_reason": "end_turn",
    "content": [{"type": "thinking", "thinking": "", "signature": "x"},
                {"type": "text", "text": json.dumps({
                    "tldr": "A startup raised $50M for AI agents.",
                    "key_points": ["AgentCo raised $50 million", "Series B led by Example Ventures",
                                   "Targets logistics in Europe"],
                    "why_it_matters": "Shows investor appetite for agents."})}],
}

FEEDS_TOML = """
keywords = ["AI", "LLM", "machine learning"]

[[feed]]
name = "Test Lab"
url = "https://feeds.example/lab.xml"
category = "Labs"

[[feed]]
name = "Test News"
url = "https://feeds.example/news.atom"
category = "News"
filter = true

[[feed]]
name = "Broken Feed"
url = "https://feeds.example/broken.xml"
category = "News"

[[feed]]
name = "Hacker News"
url = "https://feeds.example/hn.xml"
category = "Community"

[[feed]]
name = "Disabled"
url = "https://feeds.example/never.xml"
category = "News"
enabled = false
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    feeds = tmp_path / "feeds.toml"
    feeds.write_text(FEEDS_TOML)
    monkeypatch.setenv("PORTAL_DB", str(tmp_path / "portal.db"))
    monkeypatch.setenv("FEEDS_FILE", str(feeds))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AA_API_KEY", "aa-test-key")
    monkeypatch.setenv("OLLAMA_MODEL", "")
    monkeypatch.setenv("FULLTEXT_LIMIT", "60")
    monkeypatch.setenv("SUMMARY_LIMIT", "30")
    monkeypatch.setenv("WEEKLY_DIR", str(tmp_path / "weekly"))
    monkeypatch.setenv("TRANSLATE_LIMIT", "0")  # translation has its own test
    import enrich
    enrich.ROBOTS._cache.clear()
    from trafilatura.meta import reset_caches
    reset_caches()  # trafilatura drops paragraphs it has seen several times in one process
    db.init_db()
    return tmp_path


def mock_internet(router: respx.MockRouter, price_in: str = "0.000003", slow_models: float = 0.0):
    router.get("https://feeds.example/lab.xml").respond(200, text=RSS_LAB, headers={"content-type": "application/rss+xml"})
    router.get("https://feeds.example/news.atom").respond(200, text=ATOM_NEWS, headers={"content-type": "application/atom+xml"})
    router.get("https://feeds.example/broken.xml").respond(500, text="oops")
    router.get("https://feeds.example/hn.xml").respond(200, text=HN_RSS)
    router.get("https://news.example/robots.txt").respond(200, text="User-agent: *\nAllow: /\n")
    router.get("https://lab.example/robots.txt").respond(404)
    router.get("https://closed.example/robots.txt").respond(200, text="User-agent: *\nDisallow: /\n")
    router.get("https://news.example/story-1").respond(200, text=ARTICLE_HTML, headers={"content-type": "text/html"})
    router.get("https://lab.example/robots").respond(200, text=ROBOTS_LAB_HTML, headers={"content-type": "text/html; charset=utf-8"})
    closed = router.get("https://closed.example/llm").respond(200, text="should never be fetched")

    async def models_side_effect(request):
        if slow_models:
            await asyncio.sleep(slow_models)
        return httpx.Response(200, json=openrouter_payload(price_in))
    router.get("https://openrouter.ai/api/v1/models").mock(side_effect=models_side_effect)

    def aa_side_effect(request):
        assert request.headers["x-api-key"] == "aa-test-key"
        page = request.url.params.get("page", "1")
        return httpx.Response(200, json=AA_PAGE_1 if page == "1" else AA_PAGE_2)
    aa = router.get("https://artificialanalysis.ai/api/v2/language/models/free").mock(side_effect=aa_side_effect)

    def anthropic_side_effect(request):
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"]
        body = json.loads(request.content)
        assert body["model"] == "claude-haiku-5-5"
        assert "Never invent" in body["system"]
        assert len(body["messages"][0]["content"]) < 15_000
        return httpx.Response(200, json=ANTHROPIC_REPLY)
    anth = router.post("https://api.anthropic.com/v1/messages").mock(side_effect=anthropic_side_effect)
    return {"closed": closed, "aa": aa, "anthropic": anth}


def rows(sql, *params):
    conn = db.connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------------ tests
def test_match_key():
    assert collector.match_key("Anthropic: Claude Sonnet 4.5") == collector.match_key("Claude 4.5 Sonnet")
    assert collector.match_key("Claude 4.5 Sonnet (Reasoning)") == collector.match_key("Anthropic: Claude Sonnet 4.5")
    assert collector.match_key("OpenAI: GPT-5.1") != collector.match_key("OpenAI: GPT-5")


def test_full_refresh(env):
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        calls = mock_internet(router)
        result = asyncio.run(collector.run_refresh())

    assert result["new_articles"] == 5  # 2 lab + 2 matching news (one robots-blocked) + 1 HN
    assert result["failed_sources"] == ["Broken Feed"]
    assert result["new_models"] == 3 and result["price_changes"] == 0
    assert result["benchmarks"] == 2
    assert calls["aa"].call_count == 2  # pagination followed
    assert not calls["closed"].called  # robots.txt respected
    assert "Refresh done: 5 new articles" in result["message"] and "1 source failed" in result["message"]

    arts = {r["url"]: r for r in rows("SELECT * FROM articles")}
    assert "https://news.example/garden" not in arts  # keyword filter
    assert "https://lab.example/old" not in arts  # older than 120 days
    gpt = arts["https://lab.example/gpt-9"]
    assert gpt["fulltext_status"] == "feed" and gpt["image"] == "https://img.example/gpt9.jpg"
    assert gpt["word_count"] > 100 and "\n\n" in gpt["content"]
    story = arts["https://news.example/story-1"]
    assert story["fulltext_status"] == "ok" and "AgentCo" in story["content"]
    assert story["image"] == "https://img.example/og.jpg"
    assert arts["https://lab.example/robots"]["image"] == "https://img.example/robot.png"
    assert arts["https://closed.example/llm"]["fulltext_status"] == "blocked"
    assert arts["https://news.ycombinator.com/item?id=1"]["fulltext_status"] == "blocked"
    assert story["ai_status"] == "ok" and story["ai_tldr"].startswith("A startup")
    assert len(json.loads(story["ai_points"])) == 3

    src = {r["name"]: r for r in rows("SELECT * FROM sources")}
    assert src["Broken Feed"]["last_error"] and src["Test Lab"]["last_error"] is None
    assert "Disabled" not in src

    m = rows("SELECT * FROM models WHERE id='anthropic/claude-test-4.5'")[0]
    assert m["price_in"] == 3.0 and m["price_out"] == 15.0 and m["price_cached"] == 0.3
    assert rows("SELECT price_in FROM models WHERE id='openrouter/auto'")[0][0] == -1
    assert len(rows("SELECT * FROM price_history")) == 3

    # second refresh: price change recorded, nothing duplicated, summaries not redone
    with respx.mock(assert_all_called=False) as router:
        calls = mock_internet(router, price_in="0.0000025")
        result2 = asyncio.run(collector.run_refresh())
    assert result2["new_articles"] == 0 and result2["price_changes"] == 1
    assert calls["anthropic"].call_count == 0 or result2["summaries"] == calls["anthropic"].call_count
    assert len(rows("SELECT * FROM price_history WHERE model_id='anthropic/claude-test-4.5'")) == 2


def test_failed_summary_is_retried(env):
    with respx.mock(assert_all_called=False) as router:
        mock_internet(router)
        router.post("https://api.anthropic.com/v1/messages").respond(529, json={"error": "overloaded"})
        asyncio.run(collector.run_refresh())
    assert rows("SELECT COUNT(*) FROM articles WHERE ai_status='failed'")[0][0] > 0
    with respx.mock(assert_all_called=False) as router:
        mock_internet(router)
        r = asyncio.run(collector.run_refresh())
    assert r["summaries"] > 0
    assert rows("SELECT COUNT(*) FROM articles WHERE ai_status='failed'")[0][0] == 0


def test_no_second_refresh_while_running(env):
    lock = collector.RefreshLock()
    assert lock.acquire()
    try:
        assert collector.refresh_running()
        assert asyncio.run(collector.run_refresh()) is None
    finally:
        lock.release()
    assert not collector.refresh_running()


def test_startup_does_not_refresh(env):
    from fastapi.testclient import TestClient
    import app as portal
    with respx.mock(assert_all_called=False) as router:
        mock_internet(router)
        with TestClient(portal.app) as client:
            r = client.get("/")
            assert r.status_code == 200
            assert "Refresh now" in r.text and "load your first briefing" in r.text
            time.sleep(0.5)
            s = client.get("/api/status").json()
        assert router.calls.call_count == 0  # no outgoing HTTP at all
    assert s["running"] is False and s["last_refresh"] is None


def test_status_endpoint_and_pages(env):
    from fastapi.testclient import TestClient
    import app as portal
    with respx.mock(assert_all_called=False) as router:
        mock_internet(router, slow_models=1.5)
        with TestClient(portal.app) as client:
            r = client.post("/refresh", headers={"accept": "application/json"})
            assert r.json()["started"] is True
            # second click while running starts nothing
            assert client.post("/refresh", headers={"accept": "application/json"}).json()["started"] is False
            seen_running, messages = False, set()
            deadline = time.time() + 30
            while time.time() < deadline:
                s = client.get("/api/status").json()
                if s["running"]:
                    seen_running = True
                    messages.add(s["message"])
                elif seen_running:
                    break
                time.sleep(0.1)
            assert seen_running and s["running"] is False
            assert "Updating model prices" in messages
            assert s["last_result"].startswith("Refresh done")
            assert s["last_refresh_ago"] == "just now"

            # result message shown once on the next page load
            home = client.get("/")
            assert "Refresh done" in home.text and "Broken Feed" in home.text
            assert "Refresh done" not in client.get("/").text
            assert "Updated just now" in home.text

            art = rows("SELECT id FROM articles WHERE url='https://news.example/story-1'")[0][0]
            blocked = rows("SELECT id FROM articles WHERE url LIKE 'https://closed.example/%'")[0][0]
            pages = ["/", "/?category=Labs", "/?source=Test+News", "/?q=AgentCo", "/?q=%25_'\"<script>",
                     "/?page=2", f"/article/{art}", f"/article/{blocked}",
                     "/models", "/models?sort=smartest&free=1", "/models?sort=cheapest_in&provider=Anthropic",
                     "/models?sort=DROP+TABLE", "/models?q=claude&sort=context", "/models?sort=name",
                     "/model/anthropic/claude-test-4.5", "/model/meta/llama-free:free", "/model/openrouter/auto",
                     "/benchmarks", "/benchmarks?sort=fastest", "/benchmarks?sort=cheapest&q=llama",
                     "/benchmarks?sort=coding", "/benchmarks?sort=agentic", "/static/style.css"]
            for p in pages:
                resp = client.get(p)
                assert resp.status_code == 200, p
                assert "<script>\"" not in resp.text  # escaped

            assert "AgentCo" in client.get("/?q=AgentCo").text
            a = client.get(f"/article/{art}").text
            assert "TL;DR" in a and "Read on Test News" in a and "More in News" in a
            assert "blocks automated readers" in client.get(f"/article/{blocked}").text
            md = client.get("/model/anthropic/claude-test-4.5").text
            assert "GPQA Diamond" in md and "81.0%" in md and "Artificial Analysis" in md
            assert "price-chart" in md and "In the news" in md
            assert "Today" in md
            bm = client.get("/benchmarks").text
            assert 'href="/model/anthropic/claude-test-4.5"' in bm and "artificialanalysis.ai" in bm

            for p in ["/article/doesnotexist", "/model/no/such-model", "/nothing-here"]:
                assert client.get(p).status_code == 404, p

            # form POST without JS redirects back
            r = client.post("/refresh?next=/models", follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/models"
            r = client.post("/refresh?next=//evil.example", follow_redirects=False)
            assert r.headers["location"] == "/"
            # wait for that refresh to finish before the mock goes away
            deadline = time.time() + 30
            while client.get("/api/status").json()["running"] and time.time() < deadline:
                time.sleep(0.1)


def test_old_database_is_upgraded(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE articles (id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT)")
    conn.execute("INSERT INTO articles VALUES ('a', 'https://x.example/a', 'Old')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("PORTAL_DB", str(path))
    db.init_db()
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(articles)")}
    assert {"ai_tldr", "word_count", "fulltext_status", "image"} <= cols
    assert rows("SELECT title, word_count FROM articles")[0]["title"] == "Old"


def test_env_file_changes_apply_without_restart(tmp_path, monkeypatch):
    import settings
    env_file = tmp_path / ".env"
    monkeypatch.setattr(settings, "ENV_FILE", env_file)
    monkeypatch.setattr(settings, "_file_stamp", settings._NEVER)
    monkeypatch.delenv("AA_API_KEY", raising=False)
    assert settings.aa_key() == ""
    env_file.write_text("AA_API_KEY= aa_first # comment\n")
    assert settings.aa_key() == "aa_first"
    env_file.write_text('# edited\nAA_API_KEY="aa_second_key"\n')
    assert settings.aa_key() == "aa_second_key"
    monkeypatch.setenv("AA_API_KEY", "from_env")  # real environment wins
    assert settings.aa_key() == "from_env"


def test_refresh_csrf_check(env):
    """Browsers under a no-referrer policy send Origin: null for same-site POSTs."""
    from fastapi.testclient import TestClient
    import app as portal
    with respx.mock(assert_all_called=False) as router:
        mock_internet(router)
        with TestClient(portal.app) as client:
            refused = [
                {"sec-fetch-site": "cross-site", "origin": "https://evil.example"},
                {"sec-fetch-site": "same-site", "origin": "https://other.local:8801"},
                {"origin": "https://evil.example"},  # old browser, no Sec-Fetch-Site
            ]
            for h in refused:
                r = client.post("/refresh", headers={"accept": "application/json", **h})
                assert r.status_code == 403, h
            assert not collector.refresh_running()
            # what a real browser sends when the user clicks the button
            r = client.post("/refresh?next=/", follow_redirects=False,
                            headers={"origin": "null", "sec-fetch-site": "same-origin"})
            assert r.status_code == 303
            deadline = time.time() + 30
            while client.get("/api/status").json()["running"] and time.time() < deadline:
                time.sleep(0.1)
            assert client.get("/favicon.ico", follow_redirects=True).status_code == 200


# ------------------------------------------------------------------ weekly
def test_tooltip_buttons_are_kept_in_text():
    import enrich
    html = ('<p>L\u2019<button type="button" class="glossary-tooltips" data-definition="x">'
            '<span class="glossary-tooltips-title">IA</span></button> conversationnelle</p>'
            '<button class="menu">Menu</button>')
    out = enrich.unwrap_tooltips(html)
    assert "<button" not in out.split("</p>")[0]
    assert enrich.html_to_text(out.split("</p>")[0] + "</p>") == "L\u2019IA conversationnelle"
    assert "<button class=\"menu\">Menu</button>" in out  # ordinary buttons untouched


def test_weekly_pages(env):
    import weekly
    from fastapi.testclient import TestClient
    import app as portal
    d = env / "weekly"
    d.mkdir()
    (d / "2026-W46.md").write_text(
        "---\ntitle: Older week\nweek: 2026-W46\n---\n\nFirst paragraph of the older week.\n")
    (d / "2026-W47.md").write_text(
        "---\ntitle: Big week <b>bold</b>\nweek: 2026-W47\nauthor: Tester\n---\n\n"
        "Overview with a [source](https://news.example/story-1).\n\n"
        "## Regulation and policy\n\n- **CNIL.** Item. <script>alert(1)</script>\n"
        "- [bad](javascript:alert(1))\n")
    (d / "notes.md").write_text("not a week file")
    assert [x["week"] for x in weekly.list_all()] == ["2026-W47", "2026-W46"]
    assert weekly.period_label("2026-W47") == "16\u201322 November 2026"
    assert weekly.complete_weeks(weekly.date(2026, 11, 23)) == ["2026-W47", "2026-W46"]

    with TestClient(portal.app) as client:
        r = client.get("/weekly")
        assert r.status_code == 200 and "Big week &lt;b&gt;bold&lt;/b&gt;" in r.text
        assert "Overview with a source." in r.text  # excerpt as plain text
        r = client.get("/weekly/2026-W47")
        assert r.status_code == 200
        assert '<h2>Regulation and policy</h2>' in r.text
        assert 'href="https://news.example/story-1" target="_blank" rel="noopener noreferrer"' in r.text
        assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text
        assert 'href="javascript:' not in r.text
        assert 'href="/weekly/2026-W46"' in r.text  # link to the older week
        for bad in ("/weekly/2026-W48", "/weekly/2026-W99", "/weekly/notes", "/weekly/..%2Fsettings"):
            assert client.get(bad).status_code == 404, bad


def test_weekly_summary_written_on_refresh(env):
    import weekly
    week = weekly.complete_weeks()[0]
    monday, _ = weekly.week_bounds(week)
    conn = db.connect()
    for i in range(weekly.MIN_ARTICLES + 2):
        conn.execute(
            "INSERT INTO articles(id, url, title, summary, source, category, published, fetched) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"w{i}", f"https://news.example/w{i}", f"Story {i}", "Something happened.", "News Site",
             "News", f"{monday.isoformat()}T10:00:00Z", db.now_ts()))
    conn.commit()
    reply_md = ("# Headline of the week\n\nA quiet week. " + "Filler sentence. " * 20 +
                "\n\n## Models and products\n\n- **Real.** Item. [News Site](https://news.example/w1)\n"
                "- **Made up.** Item. [Fake](https://invented.example/x)\n")
    with respx.mock(assert_all_called=False) as router:
        def side_effect(request):
            body = json.loads(request.content)
            assert "Never invent" in body["system"] and "https://news.example/w3" in body["messages"][0]["content"]
            return httpx.Response(200, json={"content": [{"type": "text", "text": reply_md}],
                                             "stop_reason": "end_turn"})
        route = router.post("https://api.anthropic.com/v1/messages").mock(side_effect=side_effect)
        async def go():
            async with httpx.AsyncClient() as client:
                return await weekly.write_missing(conn, client, lambda m: None)
        out = asyncio.run(go())
        assert out["written"] == [week] and route.call_count == 1
        again = asyncio.run(go())  # existing file is never rewritten
        assert again["written"] == [] and route.call_count == 1
    conn.close()
    dg = weekly.load(week)
    assert dg["title"] == "Headline of the week" and "claude-haiku-5-5" in dg["author"]
    assert "[News Site](https://news.example/w1)" in dg["body"]
    assert "invented.example" not in dg["body"] and "Fake" in dg["body"]


# ------------------------------------------------------------------ language
def test_every_interface_string_has_a_french_translation():
    import re as _re
    import app as portal
    import i18n
    found = set()
    for path in (ROOT / "templates").glob("*.html"):
        text = path.read_text()
        for m in _re.finditer(r"_n?\(\s*(?:[^'\")]*?,\s*)?'((?:[^'\\]|\\.)*)'(?:\s*,\s*'((?:[^'\\]|\\.)*)')?", text):
            for g in m.groups():
                if g:
                    found.add(g.replace("\\'", "'"))
    found |= set(portal.SORT_LABELS.values()) | set(portal.BENCH_SORT_LABELS.values())
    found |= set(portal.CATEGORY_ORDER) | {"Intelligence", "Coding", "Agentic"}
    missing = sorted(s for s in found if s not in i18n.FR)
    assert not missing, missing


def test_language_switch(env):
    from fastapi.testclient import TestClient
    import app as portal
    with respx.mock(assert_all_called=False) as router:
        mock_internet(router)
        asyncio.run(collector.run_refresh())
    with TestClient(portal.app) as client:
        r = client.get("/", headers={"accept-language": "en-GB,en;q=0.9"})
        assert '<html lang="en">' in r.text and "Refresh now" in r.text and "Your AI briefing" in r.text

        # the browser's language is used when no choice was made
        r = client.get("/", headers={"accept-language": "fr-FR,fr;q=0.9,en;q=0.8"})
        assert '<html lang="fr">' in r.text and "Actualiser" in r.text and "Votre briefing IA" in r.text
        assert "Aujourd'hui" in r.text or "Hier" in r.text or "Lab launches GPT-9" in r.text

        # the switch stores the choice and returns to the same page, only on this site
        r = client.get("/lang/fr?next=/models%3Fsort%3Dname", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/models?sort=name"
        assert "lang=fr" in r.headers["set-cookie"]
        assert client.get("/lang/fr?next=//evil.example", follow_redirects=False).headers["location"] == "/"
        assert "set-cookie" not in client.get("/lang/de", follow_redirects=False).headers

        client.cookies.set("lang", "fr")
        r = client.get("/models", headers={"accept-language": "en"})
        assert "Modèles et prix" in r.text and "Plus récents" in r.text
        assert client.get("/benchmarks").text.count("Classement des benchmarks") == 1
        assert "Introuvable" in client.get("/nope").text
        assert "Actualisation terminée" in client.get("/api/status").json()["last_result"]

        client.cookies.set("lang", "en")
        assert "Refresh done:" in client.get("/api/status").json()["last_result"]
        assert "Models &amp; prices" in client.get("/models").text


def test_french_dates_and_progress():
    import i18n
    from datetime import date
    i18n.set_lang("fr")
    try:
        assert i18n.date_long(date(2026, 11, 5)) == "5 novembre 2026"
        assert i18n.weekday_label(date(2026, 11, 5), with_year=False) == "jeudi 5 novembre"
        assert i18n.progress("Reading feeds 3/21") == "Lecture des flux 3/21"
        assert i18n.tr("{n} days ago", n=3) == "il y a 3 jours"
        assert str(i18n.ngettext(0, "{n} model", "{n} models")) == "0 modèle"
        assert str(i18n.gettext("Search: {q}", q="<b>")) == "Recherche : &lt;b&gt;"  # values are escaped
    finally:
        i18n.set_lang("en")
    assert i18n.date_long(date(2026, 11, 5)) == "5 November 2026"


# ------------------------------------------------------------------ Groq
def test_groq_summaries(env, monkeypatch):
    import enrich
    import settings
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("GROQ_MODEL", "")  # default model
    monkeypatch.setenv("SUMMARY_PROVIDER", "")
    assert settings.summary_provider() == "groq"
    real_sleep = asyncio.sleep
    monkeypatch.setattr(enrich.asyncio, "sleep", lambda s: real_sleep(0))  # no real waiting on 429
    calls = {"n": 0}

    def groq(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, json={"error": "rate limit"})
        assert request.headers["authorization"] == "Bearer gsk-test"
        body = json.loads(request.content)
        assert body["model"] == "openai/gpt-oss-120b" and body["reasoning_effort"] == "low"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert "Never invent" in body["messages"][0]["content"]
        reply = json.dumps({"tldr": "A startup raised money.", "key_points": ["$50M", "AI agents", "Seed"],
                            "why_it_matters": "Agents attract money."})
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]})

    with respx.mock(assert_all_called=False) as router:
        mock_internet(router)
        anth = router.post("https://api.anthropic.com/v1/messages").respond(500)
        router.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=groq)
        result = asyncio.run(collector.run_refresh())
    assert result["summaries"] > 0 and result["summary_failures"] == 0, (result, calls)
    assert not anth.called
    story = rows("SELECT * FROM articles WHERE url='https://news.example/story-1'")[0]
    assert story["ai_status"] == "ok" and story["ai_model"] == "openai/gpt-oss-120b", (dict(story)["fulltext_status"], [(r["url"], r["ai_status"], r["fulltext_status"]) for r in rows("SELECT * FROM articles")])

    monkeypatch.setenv("SUMMARY_PROVIDER", "anthropic")  # forced, but no Anthropic key
    assert settings.summary_provider() is None


# ------------------------------------------------------------------ content translation
def test_content_translation(env, monkeypatch):
    import translate
    import weekly
    from fastapi.testclient import TestClient
    import app as portal
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("GROQ_MODEL", "")
    monkeypatch.setenv("TRANSLATE_LIMIT", "100")
    (env / "weekly").mkdir()
    (env / "weekly" / "2026-W47.md").write_text(
        "---\ntitle: Big week\nweek: 2026-W47\n---\n\nOverview of the week, long enough. " * 3 +
        "\n\n- **Item.** Text. [TechCrunch](https://news.example/story-1)\n")

    def groq(request):
        body = json.loads(request.content)
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        if "titles and teasers" in system:
            items = json.loads(user)["items"]
            out = [{"id": it["id"], "lang": "en", "title_en": it["title"], "title_fr": "FR " + it["title"],
                    "teaser_en": it["teaser"], "teaser_fr": ("FR " + it["teaser"]) if it["teaser"] else ""}
                   for it in items]
            content = json.dumps({"items": out})
        elif "weekly AI news briefing" in system:
            content = ("# Grosse semaine\n\n" + "Aperçu de la semaine, assez long. " * 6 +
                       "\n\n- **Point.** Texte. [TechCrunch](https://news.example/story-1)"
                       " [Inventé](https://invented.example/x)\n")
        elif "Translate the user's text into French" in system:
            content = "TRADUIT " + user
        else:  # summary
            content = json.dumps({"tldr": "A startup raised money.", "key_points": ["$50M"],
                                  "why_it_matters": "Money.", "tldr_fr": "Une start-up a levé des fonds.",
                                  "key_points_fr": ["50 M$"], "why_it_matters_fr": "De l'argent."})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    with respx.mock(assert_all_called=False) as router:
        mock_internet(router)
        router.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=groq)
        result = asyncio.run(collector.run_refresh())
        assert result["translations"] == 5 and result["weekly_translated"] == ["2026-W47"]
        assert "5 titles translated" in result["message"]

        story = rows("SELECT * FROM articles WHERE url='https://news.example/story-1'")[0]
        assert story["title_fr"] == "FR Startup raises money for AI agents" and story["src_lang"] == "en"
        assert story["ai_tldr_fr"] == "Une start-up a levé des fonds."
        fr = weekly.translation_path("2026-W47", "fr").read_text()
        assert "Grosse semaine" in fr and "invented.example" not in fr and "news.example/story-1" in fr

        with TestClient(portal.app) as client:
            client.cookies.set("lang", "fr")
            home = client.get("/").text
            assert "FR Startup raises money for AI agents" in home
            assert "Une start-up a levé des fonds." in home  # French AI summary on the card
            page = client.get(f"/article/{story['id']}").text
            assert "Traduire l&#39;article en français" in page or "Traduire l'article en français" in page
            r = client.post(f"/article/{story['id']}/translate?to=fr", follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == f"/article/{story['id']}"
            page = client.get(f"/article/{story['id']}").text
            assert "TRADUIT" in page and "Voir l&#39;original" in page or "Voir l'original" in page
            assert "TRADUIT" not in client.get(f"/article/{story['id']}?original=1").text
            assert "Grosse semaine" in client.get("/weekly/2026-W47").text
            assert "Grosse semaine" in client.get("/weekly").text

            client.cookies.set("lang", "en")
            assert "FR Startup" not in client.get("/").text
            assert "Big week" in client.get("/weekly/2026-W47").text
            # cross-site requests cannot trigger a (paid) translation
            r = client.post(f"/article/{story['id']}/translate?to=fr",
                            headers={"sec-fetch-site": "cross-site"}, follow_redirects=False)
            assert r.status_code == 403
