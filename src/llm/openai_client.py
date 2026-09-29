"""Thin OpenAI client. Called only by src/llm/gateway.py — never directly by
agents (spec.md §15). Reads OPENAI_API_KEY from the environment via
langchain-openai's default behavior. Used for both gpt-5.1 (REASONER primary)
and gpt-5.6-luna (REASONER fallback) — same client, different model string.

AZURE_OPENAI_BASE_URL (optional): set this when OPENAI_API_KEY is actually a
Microsoft Foundry / Azure OpenAI key rather than a platform api.openai.com
key — the two are not interchangeable, a Foundry key against api.openai.com
fails auth outright (confirmed via scripts/debug_audit.py: Foundry-issued
keys don't match the sk-... platform format and the request 400s). Per
Azure's v1 GA API (generally available since August 2025 — see "Azure OpenAI
in Microsoft Foundry Models v1 API," learn.microsoft.com/azure/foundry/openai/
api-version-lifecycle), a plain OpenAI()-shaped client works against Foundry
with just base_url + api_key — no api-version parameter, no AzureOpenAI()
client, no Azure SDK dependency needed. Deliberately read directly here
rather than threaded through config/models.yaml: both REASONER roles resolve
to provider "openai" and the same single Foundry resource, so one env var
covers both without duplicating the endpoint per role.
"""

from __future__ import annotations

import os


def call(model: str, prompt: str, reasoning_effort: str | None = None) -> dict:
    """Returns {"content": <model text output>}. Raises on API failure —
    the caller (gateway.py) decides what to do with that; this function does
    not catch or retry.

    `reasoning_effort` vs `temperature`: spec.md §7.1 flags that GPT-5.1/
    GPT-5.6-Luna may take reasoning_effort instead of temperature — this is
    passed through as an extra kwarg to ChatOpenAI rather than assumed;
    verify on day 1 that the SDK version installed actually accepts it.
    """
    from langchain_openai import ChatOpenAI

    kwargs = {}
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort

    base_url = os.environ.get("AZURE_OPENAI_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url

    llm = ChatOpenAI(model=model, **kwargs)
    response = llm.invoke(prompt)
    return {"content": response.content}
