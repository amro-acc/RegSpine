"""Precision/Recall/F1 for obligation extraction (evals/golden/
golden_obligations.json), plus mandatory-flag accuracy and applicability
accuracy. Run via evals/run_benchmarks.py, or import score_extraction()
directly.

Matching an extracted obligation against its golden ground truth uses fuzzy
text similarity (difflib), not exact equality -- the model is never expected
to reproduce the golden obligation_text verbatim, only to capture the same
requirement. Span/hallucination grounding is provenance_scorer.py's job, not
this one's.
"""

from __future__ import annotations

import uuid
from difflib import SequenceMatcher

from evals.scorers._common import load_golden
from src.agents.applicability_agent import ApplicabilityAgent
from src.agents.ingestion_agent import IngestionAgent
from src.core.schemas import Modality, RegulatoryObligation
from src.llm.gateway import LLMGateway

MATCH_THRESHOLD = 0.55  # fuzzy-match floor for "this extracted obligation IS the golden one"


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _best_match(golden_obligation: dict, extracted: list[RegulatoryObligation]) -> tuple[RegulatoryObligation | None, float]:
    best, best_score = None, 0.0
    for candidate in extracted:
        score = max(
            _similarity(golden_obligation["obligation_text"], candidate.obligation_text),
            _similarity(golden_obligation["verbatim_quote"], candidate.verbatim_quote),
        )
        if score > best_score:
            best, best_score = candidate, score
    return best, best_score


def score_extraction(gateway: LLMGateway | None = None) -> dict:
    gateway = gateway or LLMGateway()
    ingestion_agent = IngestionAgent(gateway)
    cases = load_golden("golden_obligations.json")

    true_positives = 0
    false_negatives = 0
    false_positives = 0
    mandatory_agreements = 0
    applicability_correct = 0
    applicability_total = 0
    per_case: list[dict] = []

    for case in cases:
        extracted = ingestion_agent.extract_obligations(
            source_text=case["source_text"],
            clause_id=uuid.uuid4(),
            source_file=f"evals/golden/{case['id']}",
            page_number=1,
            run_id=uuid.uuid4(),
        )
        matched_extracted_ids: set[uuid.UUID] = set()
        case_result: dict = {"id": case["id"], "matches": []}

        for golden_obligation in case["expected_obligations"]:
            match, score = _best_match(golden_obligation, extracted)
            if match is not None and score >= MATCH_THRESHOLD:
                true_positives += 1
                matched_extracted_ids.add(match.id)
                mandatory_match = (match.modality == Modality.MUST) == golden_obligation["mandatory"]
                if mandatory_match:
                    mandatory_agreements += 1
                case_result["matches"].append(
                    {"golden": golden_obligation["obligation_text"], "score": score, "mandatory_match": mandatory_match}
                )

                if case.get("applicability"):
                    applicability_total += 1
                    profile = case["applicability"]["bank_profile"]
                    applicability_agent = ApplicabilityAgent(gateway=gateway, bank_profile=profile)
                    applicable, _records = applicability_agent.evaluate([match], entity_id=uuid.uuid4())
                    applies = len(applicable) == 1
                    if applies == case["applicability"]["expected_applies"]:
                        applicability_correct += 1
            else:
                false_negatives += 1
                case_result["matches"].append(
                    {"golden": golden_obligation["obligation_text"], "score": score, "missed": True}
                )

        false_positives += sum(1 for e in extracted if e.id not in matched_extracted_ids)
        per_case.append(case_result)

    precision = true_positives / (true_positives + false_positives) if (true_positives + false_positives) else 0.0
    recall = true_positives / (true_positives + false_negatives) if (true_positives + false_negatives) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    mandatory_flag_accuracy = mandatory_agreements / true_positives if true_positives else 0.0
    applicability_accuracy = applicability_correct / applicability_total if applicability_total else 0.0

    return {
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "mandatory_flag_accuracy": mandatory_flag_accuracy,
        "applicability_accuracy": applicability_accuracy,
        "cases": per_case,
    }


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    print(json.dumps(score_extraction(), indent=2, default=str))
