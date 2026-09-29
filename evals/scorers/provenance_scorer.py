"""Verifies extracted obligations' verbatim_quote/snippet_hash actually
trace back to real substrings in their source text -- hallucination rate
(evals/golden/golden_obligations.json). Reuses the same deterministic
verify_citation_span() the live pipeline gates on, run independently here
rather than trusting IngestionAgent's own self-reported span_verified flag
-- a scorer that just echoes the thing it's supposed to be checking isn't
actually verifying anything.
"""

from __future__ import annotations

import hashlib
import uuid

from evals.scorers._common import load_golden
from src.agents.ingestion_agent import IngestionAgent
from src.llm.gateway import LLMGateway
from src.verify.span import verify_citation_span


def score_provenance(gateway: LLMGateway | None = None) -> dict:
    gateway = gateway or LLMGateway()
    ingestion_agent = IngestionAgent(gateway)
    cases = load_golden("golden_obligations.json")

    total = 0
    verified_count = 0
    hash_matches = 0
    inconsistent_self_report = 0
    per_case: list[dict] = []

    for case in cases:
        extracted = ingestion_agent.extract_obligations(
            source_text=case["source_text"],
            clause_id=uuid.uuid4(),
            source_file=f"evals/golden/{case['id']}",
            page_number=1,
            run_id=uuid.uuid4(),
        )
        for obligation in extracted:
            total += 1
            span_result = verify_citation_span(case["source_text"], obligation.verbatim_quote)
            independently_verified = span_result["verified"]
            expected_hash = hashlib.sha256(obligation.verbatim_quote.encode("utf-8")).hexdigest()
            hash_ok = obligation.provenance.snippet_hash == expected_hash

            if independently_verified:
                verified_count += 1
            if hash_ok:
                hash_matches += 1
            if independently_verified != obligation.span_verified:
                inconsistent_self_report += 1

            per_case.append(
                {
                    "case_id": case["id"],
                    "obligation_text": obligation.obligation_text,
                    "independently_verified": independently_verified,
                    "agent_reported_verified": obligation.span_verified,
                    "match_ratio": span_result["match_ratio"],
                    "method": span_result["method"],
                    "hash_ok": hash_ok,
                }
            )

    grounding_rate = (verified_count / total * 100) if total else 0.0

    return {
        "total_obligations": total,
        "verified_count": verified_count,
        "grounding_rate_pct": grounding_rate,
        "hallucination_rate_pct": 100.0 - grounding_rate,
        "hash_integrity_rate_pct": (hash_matches / total * 100) if total else 0.0,
        "self_report_inconsistencies": inconsistent_self_report,
        "cases": per_case,
    }


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    print(json.dumps(score_provenance(), indent=2, default=str))
