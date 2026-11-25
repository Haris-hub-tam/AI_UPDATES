# AI updates

A personal web portal for AI news, full articles, AI summaries, model specs, model prices (with history) and benchmark scores, in a card-based interface.

> **Personal use only.** The portal stores the full text of articles so you can read them comfortably. That text belongs to its publishers. Do not expose this portal on the public internet. Keep it on your own machine or your private network, behind a firewall or VPN.

## Setup

You need [uv](https://docs.astral.sh/uv/) (in `~/.local/bin`) and Python 3.11 or newer. You don't need root.

```bash
cd ~/AI_Updates
uv sync                 # create .venv and install dependencies
cp .env.example .env    # optional: add API keys (see below)
uv run uvicorn app:app --host 0.0.0.0 --port 8801
```

Or use the Makefile (`make` lists every command):

| Command | What it does |
|---|---|
| `make start` / `make stop` / `make restart` | Run the portal in the background on port 8801 (log in `portal.log`) |
| `make run` | Run it in the terminal instead (Ctrl+C to stop) |
| `make status`, `make logs` | Is it running? Follow the log |
| `make refresh` | Same as the **Refresh now** button |
| `make test` | Run the tests |

Another port: `make start PORT=9000`.

**Language.** The **EN / FR** switch in the top bar changes the language of the interface and of the content. The choice is kept in a cookie; without it, the browser's language is used. Interface texts are in `i18n.py`. The content is translated by the AI provider (`translate.py`) and stored, so each text is translated once:

- titles and teasers: during each refresh, up to `TRANSLATE_LIMIT` articles (default 200), newest first, 10 per AI call. French sources such as the CNIL also get English titles;
- AI summaries: written in English and French in the same call;
- full article text: on request, with the **Translate the article** button on the article page; **Show the original** switches back;
- weekly summaries: translated once into `weekly/<week>.fr.md`.

`make translate` translates everything not translated yet in one go (about 85 AI calls for 850 articles). Until a text is translated, it is shown in its original language.

Open http://localhost:8801 and click **Refresh now** to load your first briefing. The first refresh takes about 10 to 30 seconds without AI summaries. With summaries it takes longer, depending on the model.

## How refreshing works

Data is collected **only** when you ask for it:

- click **Refresh now** in the top bar, or
- run `uv run collector.py` in a terminal.

Nothing refreshes automatically: not on a timer, and not when the app starts. Only one refresh can run at a time. A file lock (`portal.db.lock`) covers both the button and the command line, so a second click does nothing while a refresh is running. While it runs, the button shows "Refreshing…" with live progress, and the page reloads when it's done.

Each refresh:

1. reads the feeds in `feeds.toml`, applies the keyword filter to `filter = true` feeds, removes duplicates by URL, and deletes articles older than 120 days;
2. gets full text for new articles. It uses the feed's `content:encoded` when that is long enough. Otherwise it downloads the page and extracts the text with trafilatura, if robots.txt allows it. It downloads at most `FULLTEXT_LIMIT` pages per refresh, 4 at a time;
3. writes AI summaries (TL;DR, key points, why it matters) for up to `SUMMARY_LIMIT` articles. Failed summaries are retried on the next refresh;
4. writes the weekly summary for each of the last two complete weeks that has none yet and has at least 15 articles (see "Weekly summaries" below);
5. updates models and prices from OpenRouter, and records every price change;
6. updates benchmarks from Artificial Analysis, if `AA_API_KEY` is set.

Each refresh is logged to the terminal or journal: new articles, models, price changes, full texts, summaries, failed sources and how long it took.

## .env options

Everything is optional. Features without a key are switched off, and the page tells you so.

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Turns on AI summaries through the Anthropic Messages API |
| `ANTHROPIC_MODEL` | `claude-haiku-5-5` | Model used for summaries (Anthropic's current fast, low-cost model) |
| `GROQ_API_KEY` | – | Turns on AI summaries through [Groq](https://console.groq.com/keys) (used if no Anthropic key is set) |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Groq model for summaries and weekly summaries |
| `OLLAMA_MODEL` | – | Use a local Ollama model instead (used if neither Anthropic nor Groq is set) |
| `TRANSLATE_LIMIT` | `200` | Articles whose title and teaser are translated per refresh (0 = off) |
| `SUMMARY_PROVIDER` | – | Force `anthropic`, `groq` or `ollama` when several are set |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server |
| `AA_API_KEY` | – | Free [Artificial Analysis](https://artificialanalysis.ai/) API key for benchmarks (100 requests/day; one refresh uses 1 to 3) |
| `FULLTEXT_LIMIT` | `60` | Maximum article downloads per refresh |
| `SUMMARY_LIMIT` | `30` | Maximum AI summaries per refresh |
| `PORTAL_DB` | `./portal.db` | SQLite database file |
| `FEEDS_FILE` | `./feeds.toml` | Feed list |
| `WEEKLY_DIR` | `./weekly` | Folder with the weekly summaries |
| `USER_AGENT` | `… ai-portal/1.0 (personal feed reader)` | User agent for feeds, pages and robots.txt |

Values already set in the environment override `.env`.

## Feeds

Edit `feeds.toml`. Each `[[feed]]` has `name`, `url` and `category`, and optionally `filter = true`, `limit = N` (default 25) and `enabled = false`. The `keywords` list at the top is used for the filtered feeds. Matching is case-insensitive, on whole words, over title + summary.

Notes from checking the feeds (13 November 2026):

- **VentureBeat**: `venturebeat.com/category/ai/feed/` answers 429 to feed readers, so the portal uses VentureBeat's FeedBurner feed, with the keyword filter.
- **Google AI blog**: the old URL redirects to `blog.google/innovation-and-ai/technology/ai/rss/`, which is used directly.
- **r/LocalLLaMA**: Reddit answers 403/429 to logged-out RSS requests from this network (also through old.reddit.com and openrss.org). The feed is in `feeds.toml` with `enabled = false`. Set it to `true` to try it from your network.
- **Regulation** (checked 23 November 2026):
  - **CNIL**: `cnil.fr/fr/rss.xml`, with the keyword filter. The English feed carries the same French items.
  - **EU AI Act**: `artificialintelligenceact.eu/feed/` (Future of Life Institute), no filter.
  - **European Commission**: the "Shaping Europe's digital future" feed, with the keyword filter.
  - **ANSSI**: cyber.gouv.fr has no feed and blocks automated readers, so the portal reads ANSSI's CERT-FR news and threat-report feeds, with the keyword filter. They rarely mention AI, so these two sources often show 0 entries.
  - French keywords (`IA`, `intelligence artificielle`, …) are in the `keywords` list for these feeds.
- **Hacker News** (hnrss.org) is sometimes slow or answers 502. The collector retries twice. If it still fails, the source is listed as failing until the next refresh succeeds.

Sites that block readers (reddit, news.ycombinator.com) are never downloaded. For arXiv, the abstract from the feed is kept and the page is not downloaded.

## Weekly summaries

The **Weekly** tab shows one briefing per ISO week. Each briefing is a Markdown file in `weekly/` named after its week, for example `weekly/2026-W47.md`:

```markdown
---
title: Headline of the week
week: 2026-W47
author: Me
written: 2026-11-23
---

Overview paragraph…

## Regulation and policy

- **CNIL.** What happened. [CNIL](https://www.cnil.fr/…)
```

- You can write or edit these files by hand. Raw HTML in them is not rendered.
- When AI summaries are on, each refresh writes the files for the last two complete weeks that don't have one yet. The prompt contains only that week's articles, and any link the model returns that isn't one of those articles is removed. Existing files are never overwritten, so your edits are safe. Delete a file to have it written again.
- The first three weeks (2026-W45 to W47) were written by Claude in Claude Code from the collected articles. W45 is incomplete because collection started on 13 November.

## Tests

```bash
uv run pytest -q
```

The tests mock all external HTTP (feeds, article pages, robots.txt, OpenRouter, Artificial Analysis, Anthropic). They check:

- that a full refresh works, including the price-change history;
- that starting the app makes no HTTP calls and triggers no refresh;
- that the status endpoint reports progress and a second refresh can't start;
- that every page returns 200 and unknown ids return 404;
- that an old database is upgraded.

## Run it permanently (systemd user service, no root)

Create `~/.config/systemd/user/ai-updates.service`:

```ini
[Unit]
Description=AI portal
After=network-online.target

[Service]
WorkingDirectory=%h/AI_Updates
ExecStart=%h/.local/bin/uv run uvicorn app:app --host 0.0.0.0 --port 8801
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now ai-updates
journalctl --user -u ai-updates -f      # logs, including refresh results
```

To keep the service running after you log out, an administrator has to run `loginctl enable-linger $USER` once (`sudo loginctl enable-linger <user>`). Without it, the service runs only while you are logged in.

If you only want to reach the portal from this machine, replace `--host 0.0.0.0` with `--host 127.0.0.1`.

## Files

- `app.py`: FastAPI app and pages (`/`, `/article/{id}`, `/models`, `/model/{id}`, `/benchmarks`, `/weekly`, `/weekly/{week}`, `POST /refresh`, `GET /api/status`)
- `collector.py`: feeds, OpenRouter, Artificial Analysis, refresh lock and progress (`uv run collector.py`)
- `i18n.py`: interface language (English and French)
- `translate.py`: translation of titles, teasers, full texts and weekly summaries
- `llm.py`: calls to the AI provider (Anthropic, Groq or Ollama)
- `weekly.py`: weekly summaries (Markdown files in `weekly/`, written by the refresh when AI is on)
- `enrich.py`: full-text extraction (trafilatura, robots.txt) and AI summaries (Anthropic / Ollama)
- `db.py`: SQLite schema. Missing tables and columns are added on startup.
- `settings.py`: `.env` loader
- `templates/`, `static/`: Jinja2 templates, CSS, JS (Chart.js 4.4.1 from cdnjs)

Benchmark data comes from [Artificial Analysis](https://artificialanalysis.ai/), and their license requires visible attribution: the Benchmarks and model pages show it. Model and price data comes from [OpenRouter](https://openrouter.ai/).
