"""AuditAgent: evaluates a mapped obligation/control pair for compliance
gaps.

Uses role=REASONER (gpt-5.1 primary, gpt-5.6-luna fallback — handled
entirely inside LLMGateway; this agent never sees or reacts to which one
actually answered).

gap_class is computed in code, not left to the LLM, wherever ControlMapping
gives enough signal to do so: coverage_level=none -> no_control, partial ->
partial_coverage, deterministically, overriding whatever the model proposes
for those cases. Only when coverage_level=full does the model's judgement
(has_gap/gap_class) stand, since there's no evidence data flowing through
this agent to compute one of the evidence-dependent classes deterministically
either.

Returns GapFinding | None: if coverage is full and the model finds nothing
wrong, we return None rather than fabricate a gap just to have something to
report.

mapping/control are Optional: when MappingAgent.map() finds zero candidate
controls at all, it returns None rather than fabricate a ControlMapping with
no control_id to point to. That's a different, more severe case than
coverage_level=NONE (which still names a specific, if inadequate, control),
and audit_node must not silently skip it — it's the most important
no_control case there is. When mapping is None this agent still calls the
model for narrative/risk_factors (there's real context to write about — the
obligation itself — even with no control to discuss), but gap_class is
forced to "no_control" deterministically, same as the coverage_level=NONE
case.

risk_score/risk_band are computed by src/risk/scoring.py from model-supplied
risk_factors — never taken directly from the model.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from src.agents.ingestion_agent import parse_json_response
from src.core.schemas import ControlMapping, CoverageLevel, GapFinding, InternalControl, RegulatoryObligation, ReviewState
from src.llm.gateway import LLMGateway
from src.risk.scoring import score_and_band

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPT_PATH = REPO_ROOT / "src" / "prompts" / "audit" / "v1.md"

_DETERMINISTIC_GAP_CLASS_BY_COVERAGE = {
    CoverageLevel.NONE: "no_control",
    CoverageLevel.PARTIAL: "partial_coverage",
}

_VALID_GAP_CLASSES = {
    "no_control",
    "partial_coverage",
    "control_no_evidence",
    "evidence_stale",
    "control_ineffective",
    "ownership_unclear",
    "jurisdictional_mismatch",
    "contradictory_requirement",
}


def _load_prompt_template() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


class AuditAgent:
    PROMPT_VERSION = "v1"

    def __init__(self, gateway: LLMGateway | None = None):
        self.gateway = gateway or LLMGateway()
        self._prompt_template = _load_prompt_template()

    def _model_id(self) -> str:
        return self.gateway.config["reasoner"]["model"]

    def _render_prompt(
        self,
        obligation: RegulatoryObligation,
        control: InternalControl | None,
        mapping: ControlMapping | None,
    ) -> str:
        prompt = self._prompt_template
        replacements = {
            "{{obligation_text}}": obligation.obligation_text,
            "{{obligation_type}}": obligation.obligation_type,
            "{{control_title}}": control.title if control else "(no candidate control found)",
            "{{control_design_description}}": str(control.design_description) if control else "n/a",
            "{{control_type}}": str(control.control_type) if control else "n/a",
            "{{control_automation}}": str(control.automation) if control else "n/a",
            "{{control_frequency}}": str(control.frequency) if control else "n/a",
            "{{coverage_level}}": mapping.coverage_level.value if mapping else CoverageLevel.NONE.value,
            "{{mapping_rationale}}": mapping.rationale if mapping else "No candidate controls existed to map against.",
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        return prompt

    def audit(
        self,
        obligation: RegulatoryObligation,
        control: InternalControl | None,
        mapping: ControlMapping | None,
        run_id: uuid.UUID,
    ) -> GapFinding | None:
        prompt = self._render_prompt(obligation, control, mapping)
        response = self.gateway.call(role="REASONER", prompt=prompt, schema_version="audit_v1")
        parsed = parse_json_response(response.get("content", ""))

        if mapping is None:
            # Zero candidate controls existed at all — the most severe
            # no_control case, and unambiguous enough to not depend on the
            # model's has_gap judgement.
            deterministic_class = "no_control"
        else:
            deterministic_class = _DETERMINISTIC_GAP_CLASS_BY_COVERAGE.get(mapping.coverage_level)

        if deterministic_class is not None:
            gap_class = deterministic_class
        elif parsed.get("has_gap"):
            gap_class = parsed.get("gap_class")
            if gap_class not in _VALID_GAP_CLASSES:
                # A model-invented class name is a bug in the model output,
                # not something to trust silently — fall back to the closest
                # honest answer rather than persist an unknown class.
                gap_class = "control_no_evidence"
        else:
            return None  # full coverage, model found nothing wrong — no gap fabricated

        risk_factors = parsed.get("risk_factors") or {
            "regulatory_severity": 3,
            "enforcement_likelihood": 3,
            "business_exposure": 3,
            "control_weakness": 3,
            "remediation_urgency": 3,
        }
        risk_score, risk_band = score_and_band(risk_factors)

        return GapFinding(
            obligation_id=obligation.id,
            gap_class=gap_class,
            narrative=parsed.get("narrative", ""),
            risk_factors=risk_factors,
            risk_score=risk_score,
            risk_band=risk_band,
            status="open",
            confidence=mapping.confidence if mapping else parsed.get("confidence", 0.9),
            review_state=ReviewState.PROPOSED,
            created_by_agent="audit_agent",
            model_id=self._model_id(),
            prompt_version=self.PROMPT_VERSION,
            run_id=run_id,
            provenance={
                "source_file": obligation.provenance.source_file,
                "page_number": obligation.provenance.page_number,
                "snippet_hash": obligation.provenance.snippet_hash,
            },
        )
