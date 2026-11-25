"""One way to call the configured AI provider: Anthropic, Groq or Ollama.

complete() sends a system prompt and a user message and returns (text, model).
With a JSON schema, the provider is asked for JSON that follows it.
"""
from __future__ import annotations

import asyncio

import httpx

import settings


class LLMError(Exception):
    pass


async def complete(client: httpx.AsyncClient, system: str, user: str, *,
                   schema: dict | None = None, max_tokens: int = 4000,
                   effort: str = "low") -> tuple[str, str]:
    provider = settings.summary_provider()
    if provider == "anthropic":
        return await _anthropic(client, system, user, schema, max_tokens, effort)
    if provider == "groq":
        return await groq_chat(client, system, user, schema=schema,
                               max_tokens=max_tokens, reasoning=effort)
    if provider == "ollama":
        return await _ollama(client, system, user, schema)
    raise LLMError("no AI provider configured")


# --------------------------------------------------------------- Anthropic
async def _anthropic(client, system, user, schema, max_tokens, effort) -> tuple[str, str]:
    model = settings.anthropic_model()
    output: dict = {"effort": effort}
    if schema:
        output["format"] = {"type": "json_schema", "schema": schema}
    r = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": settings.get("ANTHROPIC_API_KEY"),
                 "anthropic-version": "2026-10-21", "content-type": "application/json"},
        json={"model": model, "max_tokens": max_tokens, "system": system,
              "messages": [{"role": "user", "content": user}], "output_config": output},
        timeout=300,
    )
    if r.status_code != 200:
        raise LLMError(f"Anthropic HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    if data.get("stop_reason") == "refusal":
        raise LLMError("model declined")
    if data.get("stop_reason") == "max_tokens":
        raise LLMError("answer cut off (max tokens)")
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"), model


# -------------------------------------------------------------------- Groq
# Groq models that accept strict JSON-schema output; others get plain JSON mode.
GROQ_STRICT_PREFIXES = ("openai/gpt-oss-", "qwen/", "meta-models/muse-spark")


async def groq_chat(client: httpx.AsyncClient, system: str, user: str, *,
                    schema: dict | None = None, max_tokens: int = 4000,
                    reasoning: str = "low") -> tuple[str, str]:
    """One chat completion on Groq (OpenAI-compatible API). Returns (text, model).
    Waits and retries when Groq answers 429 (rate limit)."""
    model = settings.groq_model()
    body: dict = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_completion_tokens": max_tokens,
        "temperature": 0.2,
    }
    if model.startswith("openai/gpt-oss-"):
        body["reasoning_effort"] = reasoning
    if schema:
        if model.startswith(GROQ_STRICT_PREFIXES):
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "answer", "strict": True, "schema": schema}}
        else:
            body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {settings.get('GROQ_API_KEY')}",
               "Content-Type": "application/json"}
    for attempt in range(4):
        r = await client.post(settings.GROQ_URL + "/chat/completions", headers=headers,
                              json=body, timeout=180)
        if r.status_code in (429, 503) and attempt < 3:
            try:
                wait = float(r.headers.get("retry-after") or 0)
            except ValueError:
                wait = 0
            await asyncio.sleep(min(max(wait, 2 * (attempt + 1)), 60))
            continue
        break
    if r.status_code != 200:
        raise LLMError(f"Groq HTTP {r.status_code}: {r.text[:200]}")
    choice = (r.json().get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        raise LLMError("Groq answer cut off (max tokens)")
    return (choice.get("message") or {}).get("content") or "", model


# ------------------------------------------------------------------ Ollama
async def _ollama(client, system, user, schema) -> tuple[str, str]:
    model = settings.get("OLLAMA_MODEL")
    body: dict = {"model": model, "stream": False, "options": {"temperature": 0.2},
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]}
    if schema:
        body["format"] = schema
    r = await client.post(settings.ollama_url() + "/api/chat", json=body, timeout=600)
    if r.status_code != 200:
        raise LLMError(f"Ollama HTTP {r.status_code}: {r.text[:200]}")
    return r.json().get("message", {}).get("content", ""), model
