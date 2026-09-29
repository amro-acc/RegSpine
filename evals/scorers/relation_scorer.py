"""Relation-type classification accuracy for ObligationRelationAgent
(evals/golden/golden_relations.json) -- roadmap features 13 "cross-regulation
intelligence" and 14 "regulatory contradiction detection", reopened
2026-09-29.

Calls ObligationRelationAgent.compare() directly on one curated pair at a
time (not compare_across_groups' full cross-product), since each golden
case already specifies the exact pair to judge. A "no relation" verdict is
scored as "unrelated" even though compare() returns None for it (never a
fabricated ObligationRelation row) -- "unrelated" is the correct-rejection
case this golden set explicitly includes (GR-05), same convention as
mapping_scorer.py treating MappingAgent.map()'s None as coverage "none".
"""

from __future__ import annotations

from evals.scorers._common import load_golden, obligation_from_text
from src.agents.obligation_relation_agent import ObligationRelationAgent
from src.llm.gateway import LLMGateway


def score_relation(gateway: LLMGateway | None = None) -> dict:
    gateway = gateway or LLMGateway()
    agent = ObligationRelationAgent(gateway)
    cases = load_golden("golden_relations.json")

    correct = 0
    per_case: list[dict] = []

    for case in cases:
        obligation_a = obligation_from_text(case["obligation_a_text"])
        obligation_b = obligation_from_text(case["obligation_b_text"])
        relation = agent.compare(
            obligation_a, obligation_b, case["obligation_a_regulator"], case["obligation_b_regulator"]
        )

        predicted = relation.relation_type.value if relation is not None else "unrelated"
        expected = case["expected_relation_type"]
        match = predicted == expected
        correct += int(match)

        per_case.append(
            {
                "id": case["id"],
                "expected_relation_type": expected,
                "predicted_relation_type": predicted,
                "match": match,
                "rationale": relation.rationale if relation is not None else None,
            }
        )

    total = len(cases)
    return {
        "accuracy": (correct / total) if total else 0.0,
        "correct": correct,
        "total": total,
        "cases": per_case,
    }


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    print(json.dumps(score_relation(), indent=2, default=str))
