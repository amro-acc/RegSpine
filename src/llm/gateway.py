"""LLMGateway: role-based routing, response caching, the REASONER fallback
policy, and JUDGE model-family independence.

Every live provider call is wrapped in `_call_with_retry` — transient
429/503 errors get bounded exponential backoff + jitter before giving up.
Without this, gemini-3.8-flash's real capacity throttling turned one
transient 503 into an instant, unrecoverable run failure.

Not handled here (deliberately, not an oversight):
  - Per-run cost cap / budget enforcement.
  - Persisting LLMCall rows to Postgres — the data-access layer for that
    isn't built yet. Calls are logged via the standard `logging` module for
    now.
  - Degrading to `proposed` + HITL after retries are exhausted — that
    decision needs schema-specific knowledge only the calling agent has, so
    it belongs at the node/agent level, not here.
"""

from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path

import yaml

from src.core.cache import cached_llm_call, compute_cache_key, get_cached_response, init_cache_db
from src.llm import gemini_client, openai_client

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "models.yaml"

_TRANSIENT_STATUS_CODES = {429, 503}
_MAX_RETRY_ATTEMPTS = 3
_BASE_BACKOFF_SECONDS = 1.0


def _demo_replay_enabled() -> bool:
    return os.environ.get("REGSPINE_CACHE", "off").strip().lower() == "on"


def _is_transient_provider_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code is None:
        code = getattr(exc, "status_code", None)
    return code in _TRANSIENT_STATUS_CODES


def _call_with_retry(call_fn):
    """Bounded exponential backoff + jitter on 429/503 only — any other
    error (auth, malformed request, etc.) is not transient and re-raises
    immediately rather than wasting retries on something backoff can't fix."""
    for attempt in range(_MAX_RETRY_ATTEMPTS):
        try:
            return call_fn()
        except Exception as exc:  # noqa: BLE001 - re-raised below when not transient/exhausted
            if not _is_transient_provider_error(exc) or attempt == _MAX_RETRY_ATTEMPTS - 1:
                raise
            backoff = _BASE_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, 0.5)
            logger.warning(
                "Transient provider error (%s); retrying in %.1fs (attempt %d/%d)",
                exc, backoff, attempt + 1, _MAX_RETRY_ATTEMPTS,
            )
            time.sleep(backoff)


class LLMGateway:
    """Route by role (EXTRACTOR | SUMMARIZER | REASONER), never by a literal
    model string in calling code — model IDs are read from config/models.yaml
    here, and only here."""

    def __init__(self, config_path: Path | str = DEFAULT_CONFIG_PATH):
        with open(config_path, encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

    def call(
        self,
        role: str,
        prompt: str,
        schema_version: str = "v1",
        producer_model: str | None = None,
    ) -> dict:
        role = role.lower()

        if role == "judge":
            if producer_model is None:
                raise ValueError(
                    "role='judge' requires producer_model — the gateway needs to know "
                    "what model to differ from; there is no fixed judge model to fall "
                    "back on."
                )
            judge_cfg = self._resolve_judge_model(producer_model)
            result, cache_hit = self._invoke(judge_cfg, prompt, schema_version)
            # Lets judge_agent.py log which model actually judged a finding
            # (review_actions audit trail) without re-deriving the
            # resolution itself — same "enrich the returned dict" pattern
            # used below for the reasoner-fallback path's status/
            # hitl_required. Not cached: added after _invoke() returns, same
            # as that path.
            result["model_id"] = judge_cfg["model"]
            self._log_call(role=role, model_id=judge_cfg["model"], fallback_used=False, cache_hit=cache_hit)
            return result

        if role not in self.config:
            raise ValueError(f"Unknown role '{role}' — not in config/models.yaml")

        role_cfg = self.config[role]

        try:
            result, cache_hit = self._invoke(role_cfg, prompt, schema_version)
            self._log_call(role=role, model_id=role_cfg["model"], fallback_used=False, cache_hit=cache_hit)
            return result
        except Exception as primary_error:  # noqa: BLE001 - intentionally broad: any primary failure triggers fallback logic below
            if role != "reasoner":
                # No configured fallback model for this role — the generic
                # retry/degrade ladder belongs in state_graph.py, not here.
                # Re-raise rather than pretend to handle it.
                raise

            fallback_cfg = self.config.get("reasoner_fallback")
            if fallback_cfg is None:
                raise

            logger.warning(
                "REASONER primary call failed (%s); routing to fallback %s",
                primary_error,
                fallback_cfg["model"],
            )

            result, cache_hit = self._invoke(fallback_cfg, prompt, schema_version)

            # Fallback output never auto-accepts, at any confidence. Forced
            # here, unconditionally — not a suggestion the caller can
            # override.
            result["status"] = "proposed"
            result["hitl_required"] = True

            self._log_call(role=role, model_id=fallback_cfg["model"], fallback_used=True, cache_hit=cache_hit)
            return result

    def _invoke(self, model_cfg: dict, prompt: str, schema_version: str) -> tuple[dict, bool]:
        provider = model_cfg["provider"]
        model = model_cfg["model"]

        def _call_fn() -> dict:
            if provider == "google":
                # Default is None (Gemini's own default, 1.0), not 0 — Gemini 3
                # models explicitly warn against temperature below 1.0 (see
                # gemini_client.py's module docstring). Only override if the
                # config entry says so explicitly.
                return gemini_client.call(model, prompt, temperature=model_cfg.get("temperature"))
            if provider == "openai":
                return openai_client.call(model, prompt, reasoning_effort=model_cfg.get("reasoning_effort"))
            raise ValueError(f"Unknown provider '{provider}' in config/models.yaml")

        # cache_hit is only for the log line below — cached_llm_call() itself
        # doesn't report hit/miss. init_cache_db() first since this can run
        # before cached_llm_call ever creates the table on a fresh cache.db.
        init_cache_db()
        cache_hit = get_cached_response(compute_cache_key(model, prompt, schema_version)) is not None

        # Every call goes through the cache (cheap dev iteration, offline
        # demo replay) — retry/backoff only wraps the live call, so a cache
        # hit never pays that cost. REGSPINE_CACHE=on makes this replay-only:
        # never touch the live provider, raise on a miss instead of quietly
        # falling back to a real call.
        result = cached_llm_call(
            model_name=model,
            prompt=prompt,
            schema_version=schema_version,
            call_fn=lambda: _call_with_retry(_call_fn),
            replay_only=_demo_replay_enabled(),
        )
        return result, cache_hit

    def _log_call(self, role: str, model_id: str, fallback_used: bool, cache_hit: bool = False) -> None:
        logger.info(
            "llm_call role=%s model=%s fallback_used=%s cache_hit=%s",
            role, model_id, fallback_used, cache_hit,
        )

    def _provider_for_model(self, model_name: str) -> str:
        for role_cfg in self.config.values():
            if isinstance(role_cfg, dict) and role_cfg.get("model") == model_name:
                return role_cfg["provider"]
        raise ValueError(
            f"model '{model_name}' is not any role's configured model in config/models.yaml "
            "— cannot resolve its provider family to enforce judge independence"
        )

    def _resolve_judge_model(self, producer_model: str) -> dict:
        """Resolved dynamically at call time, not a hardcoded model string,
        so this keeps working correctly if config/models.yaml's roles ever
        change."""
        producer_provider = self._provider_for_model(producer_model)
        for candidate_role in ("judge_pool", "extractor", "reasoner"):
            candidate_cfg = self.config.get(candidate_role)
            if candidate_cfg and candidate_cfg["provider"] != producer_provider:
                return candidate_cfg
        raise RuntimeError(
            f"no configured model family differs from producer '{producer_model}' — "
            "judge independence cannot be satisfied with the current config/models.yaml"
        )
