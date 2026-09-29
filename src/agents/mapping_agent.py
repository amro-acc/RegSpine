"""MappingAgent: adjudicates which candidate control (if any) covers a given
obligation (spec.md §7.2.7).

Uses role=EXTRACTOR per this step's explicit instruction — spec.md §7.2.7
originally assigned this to REASONER (GPT-5.1). Flagged as a real design
departure, not silently reconciled; spec.md's routing table has been updated
to match what's actually built rather than left to disagree with it.
EXTRACTOR itself moved from Gemini 3.8 Flash to GPT-5.1 on 2026-09-27 (spec.md
§14.1.3) — this agent's call still goes through the EXTRACTOR role, not a
literal model string, so nothing here changed as a result.

Implements the confidence-based downgrade rule from config/pipeline.yaml
(mapping.full_coverage_confidence_floor) that spec.md §7.2.7 always
specified but nothing had implemented yet: coverage_level="full" with
confidence below the floor is downgraded to "partial" and flagged for
review — this is what "coverage_score" in this step's request maps onto;
ControlMapping has no separate coverage_score field, `confidence` already
serves that purpose.

DB-agnostic like the Step 4 agents: returns a typed ControlMapping, does not
call src/database/supabase_client.py itself.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import yaml

from src.agents.ingestion_agent import parse_json_response
from src.core.schemas import ControlMapping, CoverageLevel, InternalControl, RegulatoryObligation, ReviewState
from src.llm.gateway import LLMGateway

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPT_PATH = REPO_ROOT / "src" / "prompts" / "mapping" / "v1.md"
PIPELINE_CONFIG_PATH = REPO_ROOT / "config" / "pipeline.yaml"


def _load_prompt_template() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _load_pipeline_config() -> dict:
    with open(PIPELINE_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


class MappingAgent:
    PROMPT_VERSION = "v1"

    def __init__(self, gateway: LLMGateway | None = None):
        self.gateway = gateway or LLMGateway()
        self._prompt_template = _load_prompt_template()
        self._full_coverage_confidence_floor = _load_pipeline_config()["mapping"][
            "full_coverage_confidence_floor"
        ]

    def _model_id(self) -> str:
        return self.gateway.config["extractor"]["model"]

    def _render_prompt(self, obligation: RegulatoryObligation, candidates: list[InternalControl]) -> str:
        candidate_lines = "\n".join(
            f"- id: {c.id}\n  title: {c.title}\n  design_description: {c.design_description}\n"
            f"  control_type: {c.control_type}\n  automation: {c.automation}\n  frequency: {c.frequency}"
            for c in candidates
        )
        prompt = self._prompt_template
        prompt = prompt.replace("{{obligation_text}}", obligation.obligation_text)
        prompt = prompt.replace("{{obligation_type}}", obligation.obligation_type)
        prompt = prompt.replace("{{candidate_controls}}", candidate_lines)
        return prompt

    def map(
        self,
        obligation: RegulatoryObligation,
        candidates: list[InternalControl],
        run_id: uuid.UUID,
    ) -> ControlMapping | None:
        """Returns None if there are no candidates to adjudicate at all —
        that's a `no_control` gap for AuditAgent to classify deterministically
        from coverage_level, not something MappingAgent should fabricate a
        mapping for."""
        if not candidates:
            return None

        prompt = self._render_prompt(obligation, candidates)
        response = self.gateway.call(role="EXTRACTOR", prompt=prompt, schema_version="mapping_v1")
        parsed = parse_json_response(response.get("content", ""))

        coverage_level = CoverageLevel(parsed["coverage_level"])
        confidence = float(parsed["confidence"])

        # spec.md §7.2.7's downgrade rule, implemented here for the first time:
        review_state = ReviewState.PROPOSED
        if coverage_level == CoverageLevel.FULL and confidence < self._full_coverage_confidence_floor:
            coverage_level = CoverageLevel.PARTIAL
            review_state = ReviewState.NEEDS_REVIEW

        control_id = uuid.UUID(parsed["control_id"])
        matched_control = next((c for c in candidates if c.id == control_id), candidates[0])

        return ControlMapping(
            obligation_id=obligation.id,
            control_id=matched_control.id,
            coverage_level=coverage_level,
            rationale=parsed["rationale"],
            cited_control_span=parsed.get("cited_control_span"),
            confidence=confidence,
            review_state=review_state,
            created_by_agent="mapping_agent",
            model_id=self._model_id(),
            prompt_version=self.PROMPT_VERSION,
            run_id=run_id,
            provenance={
                "source_file": obligation.provenance.source_file,
                "page_number": obligation.provenance.page_number,
                "snippet_hash": obligation.provenance.snippet_hash,
            },
        )
