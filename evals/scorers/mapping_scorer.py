"""Top-1 accuracy, Top-3 recall (embed+rerank retrieval), coverage-level
classification agreement, and cosine-vs-reranker top-1 alignment for control
mapping (evals/golden/golden_mappings.json).

Uses a disposable Chroma collection per run (never the real internal_controls
collection) -- same isolation convention as tests/test_orchestrator.py's
isolated_controls_collection fixture, just done manually here since this
module runs standalone via evals/run_benchmarks.py, not under pytest.
"""

from __future__ import annotations

import uuid

from evals.scorers._common import control_from_golden, load_golden, obligation_from_golden
from src.agents.mapping_agent import MappingAgent
from src.database import vector_store
from src.llm.gateway import LLMGateway


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    return dot / (norm_a * norm_b + 1e-9)


def _cosine_top1_ref(obligation_text: str, controls: list) -> str | None:
    """Raw embedding-cosine ranking, independent of the cross-encoder
    reranker -- lets the scorer report how often the two scoring methods
    agree on the top pick, not just whether the final pick was correct."""
    model = vector_store.get_embedding_model()
    obligation_vec = model.encode([obligation_text]).tolist()[0]
    control_texts = [f"{c.title}. {c.design_description or ''}".strip() for c in controls]
    control_vecs = model.encode(control_texts).tolist()
    scored = [(controls[i].control_ref, _cosine(obligation_vec, vec)) for i, vec in enumerate(control_vecs)]
    scored.sort(key=lambda row: row[1], reverse=True)
    return scored[0][0] if scored else None


def score_mapping(gateway: LLMGateway | None = None) -> dict:
    gateway = gateway or LLMGateway()
    mapping_agent = MappingAgent(gateway)
    cases = load_golden("golden_mappings.json")

    collection_name = f"eval_controls_{uuid.uuid4().hex[:8]}"
    top1_correct = 0
    top3_correct = 0
    classification_agreements = 0
    needs_review_count = 0
    cosine_reranker_alignment = 0
    per_case: list[dict] = []

    try:
        for case in cases:
            bank_id = uuid.uuid4()
            controls = [control_from_golden(c, bank_id=bank_id) for c in case["candidate_controls"]]

            vector_store.upsert_documents(
                collection_name,
                [
                    {
                        "id": str(control.id),
                        "text": f"{control.title}. {control.design_description or ''}".strip(),
                        "metadata": {"control_ref": control.control_ref},
                    }
                    for control in controls
                ],
            )

            ranked = vector_store.query_with_rerank(
                collection_name, case["obligation_text"], top_k=len(controls), rerank_top_n=len(controls)
            )
            id_to_ref = {str(c.id): c.control_ref for c in controls}
            ranked_refs = [id_to_ref.get(row["id"]) for row in ranked]

            expected_ref = case["expected_control_id"]
            top1_hit = top3_hit = None
            if expected_ref is not None:
                top1_hit = ranked_refs[:1] == [expected_ref]
                top3_hit = expected_ref in ranked_refs[:3]
                top1_correct += int(top1_hit)
                top3_correct += int(top3_hit)

            cosine_ref = _cosine_top1_ref(case["obligation_text"], controls)
            reranker_top1_ref = ranked_refs[0] if ranked_refs else None
            if cosine_ref == reranker_top1_ref:
                cosine_reranker_alignment += 1

            obligation = obligation_from_golden(case)
            mapping = mapping_agent.map(obligation, controls, run_id=uuid.uuid4())

            if mapping is None:
                predicted_coverage = "none"
                classification_match = case["expected_coverage_level"] == "none"
            else:
                predicted_coverage = mapping.coverage_level.value
                classification_match = predicted_coverage == case["expected_coverage_level"]
                if mapping.review_state.value == "needs_review":
                    needs_review_count += 1
            classification_agreements += int(classification_match)

            per_case.append(
                {
                    "id": case["id"],
                    "ranked_refs": ranked_refs,
                    "expected_control_id": expected_ref,
                    "top1_hit": top1_hit,
                    "top3_hit": top3_hit,
                    "cosine_top1_ref": cosine_ref,
                    "reranker_top1_ref": reranker_top1_ref,
                    "predicted_coverage": predicted_coverage,
                    "expected_coverage": case["expected_coverage_level"],
                    "classification_match": classification_match,
                }
            )
    finally:
        try:
            vector_store.get_chroma_client().delete_collection(collection_name)
        except Exception:  # noqa: BLE001 - best-effort cleanup of a disposable eval-only collection
            pass

    ranked_cases = sum(1 for c in cases if c["expected_control_id"] is not None)
    total_cases = len(cases)

    return {
        "top1_accuracy": (top1_correct / ranked_cases) if ranked_cases else 0.0,
        "top3_recall": (top3_correct / ranked_cases) if ranked_cases else 0.0,
        "classification_agreement": (classification_agreements / total_cases) if total_cases else 0.0,
        "needs_review_rate": (needs_review_count / total_cases) if total_cases else 0.0,
        "cosine_reranker_alignment_rate": (cosine_reranker_alignment / total_cases) if total_cases else 0.0,
        "cases": per_case,
    }


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    print(json.dumps(score_mapping(), indent=2, default=str))
