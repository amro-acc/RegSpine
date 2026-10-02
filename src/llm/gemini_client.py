"""Thin Gemini client. Called only by src/llm/gateway.py — never directly by
agents. Reads GOOGLE_API_KEY from the environment and passes it straight to
google.genai.Client.

We call the official google-genai SDK directly instead of going through
langchain_google_genai. Google moved API keys from the old "AIza..." format
to a new "AQ...." one, and a freshly-issued AQ key works fine against the
real API — but langchain's ChatGoogleGenerativeAI wrapper rejected it with
"API key not valid", seemingly still hardcoded around the old format. Going
straight to the SDK sidesteps that instead of fighting it.

temperature is left unset by default (Gemini's own default, 1.0) unless a
config entry says otherwise — Gemini 3 models (the whole gemini-3.x family,
including judge_pool's gemini-3.8-flash) warn that going below 1.0 can
cause looping or worse reasoning, so there's no reason to override it.

GOOGLE_API_KEY can hold a comma-separated list of keys. Worth having: the
free tier only gives 20 requests/day per key/project, not per account, and
judge_pool burns through that fast once it's actually in use. We stick to
one key until it hits a 429 (quota exhausted), then move to the next —
see _is_quota_exhausted for why a 503 doesn't trigger the same switch.
"""

from __future__ import annotations

import os

_current_key_index = 0


def _api_keys() -> list[str]:
    raw = os.environ["GOOGLE_API_KEY"]
    keys = [key.strip() for key in raw.split(",") if key.strip()]
    if not keys:
        raise RuntimeError("GOOGLE_API_KEY is set but empty after parsing")
    return keys


def _is_quota_exhausted(exc: Exception) -> bool:
    """429 RESOURCE_EXHAUSTED means this specific key is out of quota for
    the day — rotating fixes it. A 503 "high demand" is Google itself being
    overloaded, which every key would hit equally, so that's gateway.py's
    retry/backoff job instead, not ours."""
    return getattr(exc, "code", None) == 429 and getattr(exc, "status", "") == "RESOURCE_EXHAUSTED"


def call(model: str, prompt: str, temperature: float | None = None) -> dict:
    """Returns {"content": <model text output>}. Raises on API failure —
    the caller (gateway.py) decides what to do with that; this function does
    not catch or retry, except to rotate GOOGLE_API_KEY on a quota-exhausted
    key specifically (see module docstring)."""
    global _current_key_index

    from google import genai
    from google.genai import types

    config_kwargs: dict = {
        # No tools are passed here, so automatic function calling never
        # applies — disabling it silences the SDK's harmless "use AFC in
        # Chat.send_message instead" warning rather than leaving noise
        # that looks like it needs investigating.
        "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
    }
    if temperature is not None:
        config_kwargs["temperature"] = temperature
    config = types.GenerateContentConfig(**config_kwargs)

    keys = _api_keys()
    last_exc: Exception | None = None
    for _attempt in range(len(keys)):
        key_index = _current_key_index % len(keys)
        try:
            client = genai.Client(api_key=keys[key_index])
            response = client.models.generate_content(model=model, contents=prompt, config=config)
            return {"content": response.text}
        except Exception as exc:  # noqa: BLE001 - re-raised below when not a quota-rotation case
            if not _is_quota_exhausted(exc) or len(keys) == 1:
                raise
            last_exc = exc
            _current_key_index = key_index + 1  # sticky on the next key going forward, not just this call

    assert last_exc is not None  # loop always either returns or raises; reached only if every key was exhausted
    raise last_exc
