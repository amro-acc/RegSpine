"""RemediationAgent: drafts a RemediationAction for a ratified GapFinding.

Returns a single RemediationAction, not a list — matches the single-object
prompt schema this agent actually uses.

DB-agnostic like every other agent in this package: returns a typed object,
does not call src/database/supabase_client.py itself.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from src.agents.ingestion_agent import parse_json_response
from src.core.schemas import ActionType, GapFinding, InternalControl, RegulatoryObligation, RemediationAction, ReviewState
from src.llm.gateway import LLMGateway

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPT_PATH = REPO_ROOT / "src" / "prompts" / "remediation" / "v1.md"


def _load_prompt_template() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


class RemediationAgent:
    PROMPT_VERSION = "v1"

    def __init__(self, gateway: LLMGateway | None = None):
        self.gateway = gateway or LLMGateway()
        self._prompt_template = _load_prompt_template()

    def _model_id(self) -> str:
        return self.gateway.config["extractor"]["model"]

    def _render_prompt(
        self, gap_finding: GapFinding, obligation: RegulatoryObligation, control: InternalControl | None
    ) -> str:
        prompt = self._prompt_template
        replacements = {
            "{{obligation_text}}": obligation.obligation_text,
            "{{control_title}}": control.title if control else "(no control — no_control gap)",
            "{{gap_class}}": gap_finding.gap_class,
            "{{narrative}}": gap_finding.narrative,
            "{{severity}}": gap_finding.risk_band.value,
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        return prompt

    def remediate(
        self,
        gap_finding: GapFinding,
        obligation: RegulatoryObligation,
        control: InternalControl | None,
        run_id: uuid.UUID,
    ) -> RemediationAction:
        prompt = self._render_prompt(gap_finding, obligation, control)
        response = self.gateway.call(role="EXTRACTOR", prompt=prompt, schema_version="remediation_v1")
        parsed = parse_json_response(response.get("content", ""))

        action_type_raw = parsed.get("action_type")
        try:
            action_type = ActionType(action_type_raw) if action_type_raw else None
        except ValueError:
            # A model-invented action_type is a bug in the output, not
            # something to trust silently — same principle as audit_agent's
            # gap_class validation.
            action_type = ActionType.OTHER

        return RemediationAction(
            gap_id=gap_finding.id,
            action=parsed["action"],
            action_type=action_type,
            control_design_delta=parsed.get("control_design_delta"),
            owner_role=parsed.get("owner_role"),
            effort_estimate=parsed.get("effort_estimate"),
            test_plan=parsed.get("test_plan"),
            monitoring_metric=parsed.get("monitoring_metric"),
            status="proposed",
            confidence=parsed.get("confidence", 0.7),
            review_state=ReviewState.PROPOSED,
            created_by_agent="remediation_agent",
            model_id=self._model_id(),
            prompt_version=self.PROMPT_VERSION,
            run_id=run_id,
            provenance={
                "source_file": obligation.provenance.source_file,
                "page_number": obligation.provenance.page_number,
                "snippet_hash": obligation.provenance.snippet_hash,
            },
        )
