"""JudgeAgent: independent review of a GapFinding (spec.md §7.2.15, hard
invariant #6).

CRITICAL per this step's instructions: model-family independence from
AuditAgent is enforced by LLMGateway's dynamic judge resolution (Step 5's
gateway.py update), not by hardcoding "gemini-3.8-flash" here — the producer
model is read directly from `gap_finding.model_id` (the artifact already
carries who made it; no separate parameter needed to know that), and the
gateway picks whichever configured model is a different provider family.
With the current two-family config (Google, OpenAI) this resolves to Gemini
when AuditAgent's producer was GPT-5.1, which is what was asked for — but
arrived at generically, so it stays correct if the config ever changes.

Returns an adjusted copy of the GapFinding (severity/status only — a judge
cannot change gap_class or risk_factors themselves, only ratify or adjust
the read-out).

Writes a `review_actions` audit-trail row for every verdict (ratify/adjust/
reject) — a deliberate, scoped exception to "agents stay DB-agnostic, only
state_graph.py's nodes call supabase_client" (every other agent in this
package still follows that). Done here instead of in state_graph.py's
judge_node because the snapshot this row needs (the gap's state
*immediately before* this call mutates it, plus the model's raw verdict) only
exists inside this method — pushing it out to the node would mean either
threading the pre-mutation state back out too, or reconstructing it, for no
real benefit. The write is best-effort (logged, never raised) so a
review_actions failure can never crash graph execution over what is an audit
log, not the primary artifact — the GapFinding itself is unaffected either way.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from src.agents.ingestion_agent import parse_json_response
from src.core.schemas import GapFinding, InternalControl, RegulatoryObligation, RiskSeverity
from src.database import supabase_client
from src.llm.gateway import LLMGateway

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPT_PATH = REPO_ROOT / "src" / "prompts" / "judge" / "v1.md"

# db/migrations/001_init.sql's own comment on review_actions.action:
# "accept|reject|amend" — this is the vocabulary any reviewer (human HITL or
# model judge) writes into that column, so the judge's own verdict wording
# (ratify/adjust/reject, judge/v1.md) is mapped onto it rather than stored
# verbatim. The raw model verdict is still preserved in full inside
# corrected_output below — nothing is lost, just normalized for the shared
# column.
_VERDICT_TO_ACTION = {"ratify": "accept", "adjust": "amend", "reject": "reject"}


def _load_prompt_template() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _clean_null(value: object) -> object | None:
    """Models sometimes emit the literal string "null" (or "none"/"") instead
    of JSON null for an unchanged field — the prompt's own "X, or null if
    unchanged" phrasing invites this. Treat both the same rather than let a
    stringified null reach RiskSeverity(...) and crash the run (reproduced
    live: ValueError: 'null' is not a valid RiskSeverity)."""
    if isinstance(value, str) and value.strip().lower() in {"null", "none", ""}:
        return None
    return value


class JudgeAgent:
    PROMPT_VERSION = "v1"

    def __init__(self, gateway: LLMGateway | None = None):
        self.gateway = gateway or LLMGateway()
        self._prompt_template = _load_prompt_template()

    def _render_prompt(
        self, gap_finding: GapFinding, obligation: RegulatoryObligation, control: InternalControl | None
    ) -> str:
        prompt = self._prompt_template
        replacements = {
            "{{obligation_text}}": obligation.obligation_text,
            "{{control_title}}": control.title if control else "(no control — no_control gap)",
            "{{gap_class}}": gap_finding.gap_class,
            "{{narrative}}": gap_finding.narrative,
            "{{risk_factors}}": str(gap_finding.risk_factors),
            "{{current_severity}}": gap_finding.risk_band.value,
            "{{current_status}}": gap_finding.status,
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        return prompt

    def _record_review_action(
        self,
        gap_finding: GapFinding,
        response: dict,
        verdict: str,
        parsed: dict,
    ) -> None:
        """Best-effort audit-trail write — logged and swallowed on failure,
        never raised, so a review_actions outage can't take down graph
        execution over what is an audit log, not the primary artifact."""
        try:
            payload = {
                "entity_table": "gaps",
                "entity_id": str(gap_finding.id),
                "reviewer": response.get("model_id", "unknown"),
                "action": _VERDICT_TO_ACTION.get(verdict, verdict),
                "original_output": {
                    "gap_class": gap_finding.gap_class,
                    "risk_band": gap_finding.risk_band.value,
                    "status": gap_finding.status,
                },
                "corrected_output": {
                    "raw_verdict": verdict,
                    "adjusted_severity": parsed.get("adjusted_severity"),
                    "adjusted_status": parsed.get("adjusted_status"),
                },
                "note": parsed.get("reasoning"),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            supabase_client.insert_review_action(payload)
        except Exception:  # noqa: BLE001 - audit-log write must never break the graph
            logger.exception(
                "Failed to persist review_actions row for gap_id=%s — judge verdict itself is unaffected",
                gap_finding.id,
            )

    def review(
        self,
        gap_finding: GapFinding,
        obligation: RegulatoryObligation,
        control: InternalControl | None,
    ) -> GapFinding:
        """gap_finding.model_id is the producer to differ from — carried on
        the artifact itself, not passed separately."""
        prompt = self._render_prompt(gap_finding, obligation, control)
        response = self.gateway.call(
            role="judge",
            prompt=prompt,
            schema_version="judge_v1",
            producer_model=gap_finding.model_id,
        )
        parsed = parse_json_response(response.get("content", ""))

        verdict = parsed.get("verdict", "ratify")
        self._record_review_action(gap_finding, response, verdict, parsed)

        if verdict == "reject":
            # A rejected finding is still returned (never silently dropped —
            # same principle as Step 4's failed span verification), but
            # demoted to needs_review status so it doesn't read as an
            # actionable open gap while contested.
            return gap_finding.model_copy(update={"status": "disputed"})

        updates: dict = {}
        adjusted_severity = _clean_null(parsed.get("adjusted_severity"))
        if adjusted_severity is not None:
            try:
                updates["risk_band"] = RiskSeverity(adjusted_severity)
            except ValueError:
                # A malformed/invented severity string is a model-output bug,
                # not something to trust or crash on — same "fall back rather
                # than persist unknown data" pattern as audit_agent.py's
                # gap_class validation. Treated as "no change" here.
                pass

        adjusted_status = _clean_null(parsed.get("adjusted_status"))
        if adjusted_status is not None:
            updates["status"] = adjusted_status

        if not updates:
            return gap_finding  # ratified as-is

        return gap_finding.model_copy(update=updates)
