"""IngestionAgent: extracts RegulatoryObligation and InternalControl
candidates from raw text chunks (spec.md §7.2.4, §7.2.6), span-verifying
every citation before constructing the final typed object (spec.md §8.2,
hard invariants #1-#2).

Scope note: `RegulatoryObligation.clause_id` is a required FK, but
clause_segmenter (spec.md §7.2.3 — the stage that would produce real
`Clause` rows from a document) isn't built yet. This agent takes `clause_id`
(and `bank_id`, for controls) as caller-supplied parameters rather than
inventing clause creation here — matching spec.md §7.2.4's own framing,
"In: clause text (+ parent context window)," which already assumes the
clause exists upstream.

Failed span verification is never a silent drop: the candidate is still
returned, with `span_verified=False` and `review_state=NEEDS_REVIEW` — a
human decides, the agent doesn't discard evidence of what the model claimed.
The full retry-then-re-extract loop (spec.md §7.2.4, up to
`max_span_reextract_retries`) is bigger scope than this step asked for and
is not built here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from src.core.schemas import InternalControl, RegulatoryObligation, ReviewState
from src.llm.gateway import LLMGateway
from src.verify.span import verify_citation_span

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPT_PATH = REPO_ROOT / "src" / "prompts" / "ingestion" / "v1.md"


def _load_prompt_template() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def parse_json_response(raw_content: str) -> dict:
    """Models sometimes wrap JSON in a ```json fence even when told not to —
    strip that defensively rather than let a cosmetic formatting choice
    break parsing. Full schema-repair-retry (spec.md §7.1) is not built here;
    a parse failure returns an empty structure rather than raising, so one
    bad chunk can't take down a whole ingestion run (spec.md §9.2's
    page/document-level isolation principle, applied at chunk granularity)."""
    text = raw_content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[len("json") :]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


class IngestionAgent:
    """Role: EXTRACTOR (spec.md §7.1). Model ID is read from
    config/models.yaml via the gateway, never hardcoded here (hard
    invariant #8) — the model actually used shows up in provenance because
    it's read back, not because it's typed as a literal in this file."""

    PROMPT_VERSION = "v1"

    def __init__(self, gateway: LLMGateway | None = None):
        self.gateway = gateway or LLMGateway()
        self._prompt_template = _load_prompt_template()

    def _model_id(self) -> str:
        return self.gateway.config["extractor"]["model"]

    def _render_prompt(self, chunk_type: str, source_text: str) -> str:
        return self._prompt_template.replace("{{chunk_type}}", chunk_type).replace(
            "{{source_text}}", source_text
        )

    def extract_obligations(
        self,
        source_text: str,
        clause_id: uuid.UUID,
        source_file: str,
        page_number: int,
        run_id: uuid.UUID,
    ) -> list[RegulatoryObligation]:
        prompt = self._render_prompt("regulation", source_text)
        response = self.gateway.call(role="EXTRACTOR", prompt=prompt, schema_version="ingestion_v1")
        parsed = parse_json_response(response.get("content", ""))

        results: list[RegulatoryObligation] = []
        for candidate in parsed.get("obligations", []):
            quote = candidate.get("verbatim_quote", "")
            span = verify_citation_span(source_text, quote)
            snippet_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()

            results.append(
                RegulatoryObligation(
                    clause_id=clause_id,
                    obligation_text=candidate["obligation_text"],
                    verbatim_quote=quote,
                    modality=candidate["modality"],
                    actor=candidate.get("actor"),
                    trigger_condition=candidate.get("trigger_condition"),
                    deadline_spec=candidate.get("deadline_spec"),
                    obligation_type=candidate["obligation_type"],
                    confidence=candidate["confidence"],
                    span_verified=span["verified"],
                    span_verify_method=span["method"] if span["method"] != "none" else None,
                    review_state=ReviewState.PROPOSED if span["verified"] else ReviewState.NEEDS_REVIEW,
                    created_by_agent="ingestion_agent",
                    model_id=self._model_id(),
                    prompt_version=self.PROMPT_VERSION,
                    run_id=run_id,
                    provenance={
                        "source_file": source_file,
                        "page_number": page_number,
                        "snippet_hash": snippet_hash,
                    },
                )
            )
        return results

    def extract_controls(
        self,
        source_text: str,
        bank_id: uuid.UUID,
        source_file: str,
        page_number: int,
        run_id: uuid.UUID,
    ) -> list[InternalControl]:
        prompt = self._render_prompt("policy", source_text)
        response = self.gateway.call(role="EXTRACTOR", prompt=prompt, schema_version="ingestion_v1")
        parsed = parse_json_response(response.get("content", ""))

        results: list[InternalControl] = []
        for candidate in parsed.get("controls", []):
            quote = candidate.get("verbatim_quote")
            if quote:
                span = verify_citation_span(source_text, quote)
                span_verified = span["verified"]
                span_verify_method = span["method"] if span["method"] != "none" else None
                review_state = ReviewState.PROPOSED if span["verified"] else ReviewState.NEEDS_REVIEW
                snippet_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()
            else:
                # Deterministic register-row extraction has no free-text
                # quote to verify (spec.md §7.2.6) — not a verification
                # failure, just nothing to check.
                span_verified, span_verify_method, review_state = False, None, ReviewState.PROPOSED
                snippet_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()

            results.append(
                InternalControl(
                    bank_id=bank_id,
                    control_ref=candidate["control_ref"],
                    title=candidate["title"],
                    design_description=candidate.get("design_description"),
                    owner=candidate.get("owner"),
                    control_type=candidate.get("control_type"),
                    automation=candidate.get("automation"),
                    frequency=candidate.get("frequency"),
                    verbatim_quote=quote,
                    span_verified=span_verified,
                    span_verify_method=span_verify_method,
                    confidence=candidate["confidence"],
                    review_state=review_state,
                    created_by_agent="ingestion_agent",
                    model_id=self._model_id(),
                    prompt_version=self.PROMPT_VERSION,
                    run_id=run_id,
                    provenance={
                        "source_file": source_file,
                        "page_number": page_number,
                        "snippet_hash": snippet_hash,
                    },
                )
            )
        return results
