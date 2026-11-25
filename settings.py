"""Tiny .env loader and typed settings accessors.

Values set in the process environment win over the .env file. The .env file
is re-read whenever it changes, so edits take effect without a restart.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
_file_values: dict[str, str] = {}
_NEVER = object()
_file_stamp: object = _NEVER  # (mtime, size) of the last read, None = no file


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def _refresh_file_values() -> None:
    """Re-read .env if it was created, changed or deleted since the last read."""
    global _file_values, _file_stamp
    try:
        st = ENV_FILE.stat()
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        stamp = None
    if stamp == _file_stamp:
        return
    _file_stamp = stamp
    try:
        _file_values = parse_env(ENV_FILE.read_text(encoding="utf-8")) if stamp else {}
    except OSError:
        _file_values = {}


def get(key: str, default: str = "") -> str:
    if key in os.environ:
        return os.environ[key].strip()
    _refresh_file_values()
    return _file_values.get(key, default).strip()


def get_int(key: str, default: int) -> int:
    try:
        return int(get(key, str(default)))
    except ValueError:
        return default


def db_path() -> Path:
    return Path(get("PORTAL_DB", str(BASE_DIR / "portal.db")))


def feeds_path() -> Path:
    return Path(get("FEEDS_FILE", str(BASE_DIR / "feeds.toml")))


DEFAULT_ANTHROPIC_MODEL = "claude-haiku-5-5"
# Groq production model with strict structured output (console.groq.com/docs/models, 2026-11-25)
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_URL = "https://api.groq.com/openai/v1"
PROVIDERS = ("anthropic", "groq", "ollama")


def _configured(provider: str) -> bool:
    return bool(get({"anthropic": "ANTHROPIC_API_KEY", "groq": "GROQ_API_KEY",
                     "ollama": "OLLAMA_MODEL"}[provider]))


def summary_provider() -> str | None:
    """'anthropic', 'groq', 'ollama' or None when AI summaries are not configured.

    SUMMARY_PROVIDER picks one explicitly; otherwise the first configured one
    in the order Anthropic, Groq, Ollama is used.
    """
    wanted = get("SUMMARY_PROVIDER").lower()
    if wanted in PROVIDERS:
        return wanted if _configured(wanted) else None
    return next((p for p in PROVIDERS if _configured(p)), None)


def groq_model() -> str:
    return get("GROQ_MODEL") or DEFAULT_GROQ_MODEL


def anthropic_model() -> str:
    return get("ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL


def ollama_url() -> str:
    return (get("OLLAMA_URL") or "http://localhost:11434").rstrip("/")


def aa_key() -> str:
    return get("AA_API_KEY")


def translate_limit() -> int:
    """Articles whose title and teaser are translated per refresh."""
    return get_int("TRANSLATE_LIMIT", 200)


def fulltext_limit() -> int:
    return get_int("FULLTEXT_LIMIT", 60)


def summary_limit() -> int:
    return get_int("SUMMARY_LIMIT", 30)


def user_agent() -> str:
    return get("USER_AGENT") or (
        "Mozilla/5.0 (X11; Linux x86_64) ai-portal/1.0 (personal feed reader)"
    )
