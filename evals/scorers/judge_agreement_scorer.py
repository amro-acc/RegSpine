"""Consensus vs. divergence rate between the primary reasoning model
(REASONER, currently gpt-5.1) and the cross-family judge (JUDGE ->
judge_pool, currently gemini-3.8-flash). Reuses golden_mappings.json's
gap-producing cases (expected_coverage_level != "full") since only those
produce a GapFinding for the judge to review at all.

Divergence is measured at the data level (did the judge's returned
GapFinding actually differ from what AuditAgent produced), not by parsing
the judge's raw verdict string -- judge_agent.py only returns the mutated
GapFinding, and this keeps the scorer decoupled from that agent's internals
rather than reaching into its prompt-parsing implementation.
"""

from __future__ import annotations

import uuid

from evals.scorers._common import control_from_golden, load_golden, obligation_from_golden
from src.agents.audit_agent import AuditAgent
from src.agents.judge_agent import JudgeAgent
from src.core.schemas import ControlMapping, CoverageLevel
from src.llm.gateway import LLMGateway


def score_judge_agreement(gateway: LLMGateway | None = None) -> dict:
    gateway = gateway or LLMGateway()
    audit_agent = AuditAgent(gateway)
    judge_agent = JudgeAgent(gateway)
    cases = [c for c in load_golden("golden_mappings.json") if c["expected_coverage_level"] != "full"]

    ratified = 0
    diverged = 0
    per_case: list[dict] = []

    for case in cases:
        obligation = obligation_from_golden(case)
        run_id = uuid.uuid4()

        control = None
        mapping = None
        if case["expected_control_id"] is not None:
            control_entry = next(c for c in case["candidate_controls"] if c["id"] == case["expected_control_id"])
            control = control_from_golden(control_entry)
            mapping = ControlMapping(
                obligation_id=obligation.id,
                control_id=control.id,
                coverage_level=CoverageLevel(case["expected_coverage_level"]),
                rationale="golden eval mapping",
                confidence=1.0,
                created_by_agent="golden_eval",
                model_id="golden_eval",
                prompt_version="v1",
                run_id=run_id,
                provenance=obligation.provenance,
            )

        gap = audit_agent.audit(obligation, control, mapping, run_id)
        if gap is None:
            # Full coverage + no model-found deficiency -- shouldn't happen
            # for a none/partial golden case, but if it does there's nothing
            # for the judge to review either way.
            continue

        reviewed = judge_agent.review(gap, obligation, control)
        unchanged = reviewed.risk_band == gap.risk_band and reviewed.status == gap.status
        if unchanged:
            ratified += 1
        else:
            diverged += 1

        per_case.append(
            {
                "id": case["id"],
                "gap_class": gap.gap_class,
                "producer_severity": gap.risk_band.value,
                "judge_severity": reviewed.risk_band.value,
                "producer_status": gap.status,
                "judge_status": reviewed.status,
                "ratified": unchanged,
            }
        )

    total = ratified + diverged
    return {
        "total_reviewed": total,
        "ratified": ratified,
        "diverged": diverged,
        "consensus_rate": (ratified / total) if total else 0.0,
        "divergence_rate": (diverged / total) if total else 0.0,
        "cases": per_case,
    }


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    print(json.dumps(score_judge_agreement(), indent=2, default=str))
