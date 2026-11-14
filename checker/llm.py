"""Calls the AI model: Gemini (Google, default) or Claude (Anthropic).

Keys, as environment variables or in Streamlit secrets:
    GEMINI_API_KEY      for Gemini models (free tier from Google AI Studio)
    ANTHROPIC_API_KEY   for Claude models (paid, optional, for comparison)
Set CHECKER_DEMO=1 to run without a key: it returns a canned answer for the
built-in sample agreement, so you can show the app or test the UI.
"""
import json
import os
import time
from pathlib import Path

MODELS = {
    "Gemini 3.8 Flash (free tier)": "gemini-3.8-flash",
    "Gemini 2.5 Flash (free tier)": "gemini-2.5-flash",
    "Claude Sonnet 5.5 (paid)": "claude-sonnet-5-5",
    "Claude Opus 5.5 (paid)": "claude-opus-5-5",
    "GPT-OSS 20B on Groq (free tier)": "groq:openai/gpt-oss-20b",
    # Last-resort backup for the app. Its daily quota is shared with the eval judge, and evals
    # should not use it as the checker (it is the judge model).
    "GPT-OSS 120B on Groq (free tier, backup)": "groq:openai/gpt-oss-120b",
}
DEFAULT_MODEL = "gemini-3.8-flash"

KEY_NAMES = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "groq": "GROQ_API_KEY"}


def provider(model: str) -> str:
    if model.startswith("groq:"):
        return "groq"
    return "gemini" if model.startswith("gemini") else "anthropic"


def key_name(model: str) -> str:
    """Name of the secret / environment variable that holds the key for this model."""
    return KEY_NAMES[provider(model)]


def demo_mode() -> bool:
    return os.environ.get("CHECKER_DEMO", "") == "1"


def call(system: str, user: str, model: str = DEFAULT_MODEL, api_key: str | None = None,
         max_tokens: int = 8000) -> tuple[str, dict]:
    """Return (reply_text, usage) where usage has tokens, seconds and stop_reason.

    stop_reason "max_tokens" means the reply was cut off before it finished.
    """
    if demo_mode():
        path = Path(__file__).resolve().parent.parent / "evals" / "demo_reply.json"
        return path.read_text(encoding="utf-8"), {"input_tokens": 0, "output_tokens": 0, "seconds": 0.0,
                                                    "model": "demo", "stop_reason": "end_turn"}
    api_key = api_key or os.environ.get(key_name(model)) or _key_from_secrets_file(key_name(model))
    start = time.time()
    if provider(model) == "gemini":
        text, usage = _call_gemini(system, user, model, api_key, max_tokens)
    elif provider(model) == "groq":
        text, usage = _call_groq(system, user, model.removeprefix("groq:"), api_key, max_tokens)
    else:
        text, usage = _call_claude(system, user, model, api_key, max_tokens)
    usage.update({"seconds": round(time.time() - start, 1), "model": model})
    return text, usage


def _key_from_secrets_file(name: str) -> str | None:
    """Read a key from .streamlit/secrets.toml, so the evals work with the same key as the app."""
    import tomllib

    path = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")).get(name)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _call_gemini(system, user, model, api_key, max_tokens):
    from google import genai
    from google.genai import types

    # Retries rate limits (429, common on the free tier) and server errors with backoff.
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
        api_version="v1beta",  # system instructions are rejected on "v1" for some models
        timeout=180_000,  # milliseconds
        retry_options=types.HttpRetryOptions(attempts=4, http_status_codes=[429, 500, 502, 503, 504])))
    resp = client.models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            # Gemini counts its internal "thinking" against this limit, so leave extra room.
            max_output_tokens=max_tokens * 2,
            response_mime_type="application/json",  # JSON-only replies
        ),
    )
    finish = resp.candidates[0].finish_reason if resp.candidates else None
    meta = resp.usage_metadata
    usage = {
        "input_tokens": (meta.prompt_token_count or 0) if meta else 0,
        # Thinking tokens are billed as output, so count them.
        "output_tokens": ((meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)) if meta else 0,
        "stop_reason": "max_tokens" if finish == types.FinishReason.MAX_TOKENS else str(finish),
    }
    return resp.text or "", usage


def _call_groq(system, user, model, api_key, max_tokens):
    """Groq's free tier, through its OpenAI-compatible API (pip install openai)."""
    import re

    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1", timeout=180.0, max_retries=4)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens * 2,  # reasoning models spend tokens thinking first
        response_format={"type": "json_object"},
    )
    text = resp.choices[0].message.content or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # drop visible reasoning, if any
    usage = {
        "input_tokens": resp.usage.prompt_tokens if resp.usage else 0,
        "output_tokens": resp.usage.completion_tokens if resp.usage else 0,
        "stop_reason": "max_tokens" if resp.choices[0].finish_reason == "length" else resp.choices[0].finish_reason,
    }
    return text, usage


def _call_claude(system, user, model, api_key, max_tokens):
    import anthropic

    # The SDK retries rate limits, overloads and connection errors (with backoff) on its own.
    client = anthropic.Anthropic(api_key=api_key, timeout=180.0, max_retries=3)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "stop_reason": resp.stop_reason,
    }
    return text, usage


def as_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)
