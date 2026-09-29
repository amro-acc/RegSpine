"""Thin Gemini client. Called only by src/llm/gateway.py — never directly by
agents (spec.md §15). Reads GOOGLE_API_KEY from the environment explicitly
and passes it straight to google.genai.Client.

Switched off langchain_google_genai (2026-09-26): Google's Gemini API keys
transitioned from the legacy "Standard Key" format (AIza...) to the new
"Auth Key" format (AQ....) starting May 2026, with Standard keys rejected
outright by September 2026 (ai.google.dev/gemini-api/docs/api-key). Auth
keys are confirmed to work correctly against the native
generativelanguage.googleapis.com endpoint via the official `google-genai`
SDK — but a live debug session (scripts/debug_audit.py) reproduced Google's
own backend rejecting a freshly-issued, correctly-formatted AQ.-prefixed key
("API key not valid") specifically when routed through
langchain_google_genai's ChatGoogleGenerativeAI wrapper. That's a
third-party-tool auth-handling gap (matches reports of tools hardcoded
around the old AIza format), not an invalid key — calling the official SDK
directly removes that layer entirely rather than working around it.

Gemini 3 temperature guidance (ai.google.dev/gemini-api/docs/gemini-3,
checked 2026-09-27): "Changing the temperature (setting it below 1.0) may
lead to unexpected behavior, such as looping or degraded performance,
particularly in complex mathematical or reasoning tasks" — this applies to
every model in this family (gemini-3.x), including judge_pool's
gemini-3.8-flash (briefly gemini-3.1-pro-preview, reverted same day for a
free-tier quota wall — config/models.yaml). `temperature` is therefore
optional here and left unset (Gemini's own default, 1.0) unless a config
entry explicitly overrides it — never silently forced to 0 the way the old
gemini-3.8-flash EXTRACTOR role used to.

Multi-key rotation (2026-09-28): Google's free-tier quota
(generate_content_free_tier_requests) is per API key/project/model, not
per account — reproduced live: "limit: 20, model: gemini-3.8-flash" via
GenerateRequestsPerDayPerProjectPerModel-FreeTier. judge_pool is low-volume
but not zero-volume, and this is now the only Gemini-dependent role in the
system (§14.1.3), so a single key's 20/day cap is a real demo-day risk.
`GOOGLE_API_KEY` may hold a comma-separated list of keys (each from its own
Google AI Studio project, each with its own independent 20/day allotment) —
this module stays "sticky" on one key across calls (spends that key's full
daily budget before moving on, rather than spreading load pre-emptively
across keys that don't need it yet) and only rotates to the next key when
the current one specifically reports RESOURCE_EXHAUSTED (429) — a transient
503 "high demand" is a different failure mode entirely (every key would hit
the same Google-side capacity issue) and is left to gateway.py's existing
backoff-retry ladder on the same key, not a reason to rotate.
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
    """RESOURCE_EXHAUSTED (429, daily/per-key quota) — rotating to a
    different key can fix this. Distinct from a transient 503 "high demand"
    (status UNAVAILABLE), where every key would hit the same Google-side
    capacity issue and rotating wouldn't help — that stays gateway.py's
    backoff-retry job on the same key."""
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
