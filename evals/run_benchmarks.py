"""Benchmark runner for the quantitative-reliability evaluation suite.

Runs all five scorers (evals/scorers/) against the golden sets
(evals/golden/), aggregates headline metrics, and writes both:
  - evals/results.json          -- raw numbers; tests/test_evals.py reads
                                    this rather than re-running the suite
                                    inside pytest.
  - evals/BENCHMARK_REPORT.md   -- human-readable summary.

Every scorer routes through the same LLMGateway (src/llm/gateway.py), which
already caches every call through cache.db (src/core/cache.py) -- re-running
this script after a first successful run replays instantly from cache rather
than re-hitting live providers, with no special-case caching logic needed
here.

Run: python evals/run_benchmarks.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from evals.scorers.extraction_scorer import score_extraction  # noqa: E402
from evals.scorers.judge_agreement_scorer import score_judge_agreement  # noqa: E402
from evals.scorers.mapping_scorer import score_mapping  # noqa: E402
from evals.scorers.provenance_scorer import score_provenance  # noqa: E402
from evals.scorers.relation_scorer import score_relation  # noqa: E402
from src.llm.gateway import LLMGateway  # noqa: E402

RESULTS_PATH = REPO_ROOT / "evals" / "results.json"
REPORT_PATH = REPO_ROOT / "evals" / "BENCHMARK_REPORT.md"

EXTRACTION_F1_THRESHOLD = 0.85
PROVENANCE_GROUNDING_THRESHOLD = 100.0


def _section(title: str, scorer_result: dict, rows: list[tuple[str, str]]) -> list[str]:
    """rows is [(label, formatted_value), ...] -- callers pre-format each
    value (percentages, counts, etc.) since scorers store raw 0.0-1.0
    fractions, not display strings. Renders UNAVAILABLE + the error message
    instead of a table if this scorer failed (_run_scorer's {"error": ...})."""
    lines = [f"## {title}", ""]
    if "error" in scorer_result:
        lines += [f"**UNAVAILABLE** — this scorer failed: {scorer_result['error']}", ""]
        return lines
    lines += ["| Metric | Value |", "| --- | --- |"]
    lines += [f"| {label} | {value} |" for label, value in rows]
    lines.append("")
    return lines


def _render_report(results: dict) -> str:
    h = results["headline"]
    extraction = results["extraction"]
    mapping = results["mapping"]
    provenance = results["provenance"]
    judge_agreement = results["judge_agreement"]
    relation = results["relation"]

    def _pct(value) -> str:
        return f"{value:.1f}%" if value is not None else "UNAVAILABLE"

    def _num(value) -> str:
        return f"{value:.3f}" if value is not None else "UNAVAILABLE"

    lines = [
        "# Benchmark Report",
        "",
        f"Generated: {results['generated_at']}",
        "",
        "10 golden obligation cases (`evals/golden/golden_obligations.json`), "
        "10 golden mapping cases (`evals/golden/golden_mappings.json`), and "
        "5 golden relation-classification cases (`evals/golden/golden_relations.json`), "
        "spanning DORA, NIS2, Basel III, PCI DSS, and GDPR "
        "synthetic clauses.",
        "",
        "## Headline metrics",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Extraction F1 Score | {_num(h['extraction_f1'])} |",
        f"| Mapping Accuracy | {_pct(h['mapping_accuracy_pct'])} |",
        f"| Provenance Grounding Rate | {_pct(h['provenance_grounding_rate_pct'])} |",
        f"| Fallback / Review Trigger Rate | {_pct(h['fallback_review_trigger_rate_pct'])} |",
        f"| Relation Classification Accuracy | {_pct(h['relation_classification_accuracy_pct'])} |",
        "",
    ]

    if "error" not in extraction:
        lines += _section(
            "Extraction detail (`golden_obligations.json`, 10 cases)",
            extraction,
            [
                ("Precision", _num(extraction["precision"])),
                ("Recall", _num(extraction["recall"])),
                ("F1", _num(extraction["f1"])),
                ("Mandatory-flag accuracy", _pct(extraction["mandatory_flag_accuracy"] * 100)),
                ("Applicability accuracy", _pct(extraction["applicability_accuracy"] * 100)),
                (
                    "TP / FP / FN",
                    f"{extraction['true_positives']} / {extraction['false_positives']} / {extraction['false_negatives']}",
                ),
            ],
        )
    else:
        lines += _section("Extraction detail (`golden_obligations.json`, 10 cases)", extraction, [])

    if "error" not in mapping:
        lines += _section(
            "Mapping detail (`golden_mappings.json`, 10 cases)",
            mapping,
            [
                ("Top-1 accuracy (retrieval+rerank)", _pct(mapping["top1_accuracy"] * 100)),
                ("Top-3 recall (retrieval+rerank)", _pct(mapping["top3_recall"] * 100)),
                ("Coverage-level classification agreement", _pct(mapping["classification_agreement"] * 100)),
                ("Cosine-vs-reranker top-1 alignment", _pct(mapping["cosine_reranker_alignment_rate"] * 100)),
                ("needs_review rate", _pct(mapping["needs_review_rate"] * 100)),
            ],
        )
    else:
        lines += _section("Mapping detail (`golden_mappings.json`, 10 cases)", mapping, [])

    if "error" not in provenance:
        lines += _section(
            "Provenance detail (hallucination rate)",
            provenance,
            [
                ("Total extracted obligations checked", str(provenance["total_obligations"])),
                ("Span-grounded (independently verified)", str(provenance["verified_count"])),
                ("Grounding rate", _pct(provenance["grounding_rate_pct"])),
                ("Hallucination rate", _pct(provenance["hallucination_rate_pct"])),
                ("snippet_hash integrity", _pct(provenance["hash_integrity_rate_pct"])),
                ("Agent self-report inconsistencies", str(provenance["self_report_inconsistencies"])),
            ],
        )
    else:
        lines += _section("Provenance detail (hallucination rate)", provenance, [])

    if "error" not in judge_agreement:
        lines += _section(
            "Judge agreement (cross-family review, gap-producing cases only)",
            judge_agreement,
            [
                ("Cases reviewed", str(judge_agreement["total_reviewed"])),
                ("Ratified (consensus)", str(judge_agreement["ratified"])),
                ("Diverged (adjusted/rejected)", str(judge_agreement["diverged"])),
                ("Consensus rate", _pct(judge_agreement["consensus_rate"] * 100)),
                ("Divergence rate", _pct(judge_agreement["divergence_rate"] * 100)),
            ],
        )
    else:
        lines += _section("Judge agreement (cross-family review, gap-producing cases only)", judge_agreement, [])

    if "error" not in relation:
        lines += _section(
            "Relation classification detail (`golden_relations.json`, 5 cases)",
            relation,
            [
                ("Accuracy", _pct(relation["accuracy"] * 100)),
                ("Correct / Total", f"{relation['correct']} / {relation['total']}"),
            ],
        )
    else:
        lines += _section("Relation classification detail (`golden_relations.json`, 5 cases)", relation, [])

    lines += [
        "## Gates (`tests/test_evals.py`)",
        "",
        f"- Extraction F1 >= {EXTRACTION_F1_THRESHOLD}: "
        f"{'PASS' if (h['extraction_f1'] or 0) >= EXTRACTION_F1_THRESHOLD else 'FAIL'} ({_num(h['extraction_f1'])})",
        f"- Provenance Grounding == {PROVENANCE_GROUNDING_THRESHOLD}%: "
        f"{'PASS' if h['provenance_grounding_rate_pct'] == PROVENANCE_GROUNDING_THRESHOLD else 'FAIL'} "
        f"({_pct(h['provenance_grounding_rate_pct'])})",
        "",
    ]
    return "\n".join(lines)


def _run_scorer(name: str, fn, gateway: LLMGateway) -> dict:
    """A single scorer's failure (e.g. a real Gemini free-tier daily quota
    exhaustion: 'Quota exceeded ... limit: 20') must not lose the other
    three scorers' already-paid-for results. Returns {"error": str} instead
    of raising; the report renders that scorer as UNAVAILABLE rather than
    silently omitting it."""
    print(f"Running {name} scorer...")
    try:
        return fn(gateway)
    except Exception as exc:  # noqa: BLE001 - isolate this scorer's failure from the other three
        print(f"  {name} scorer FAILED: {exc}")
        return {"error": str(exc)}


def main() -> None:
    gateway = LLMGateway()

    extraction = _run_scorer("extraction", score_extraction, gateway)
    mapping = _run_scorer("mapping", score_mapping, gateway)
    provenance = _run_scorer("provenance", score_provenance, gateway)
    judge_agreement = _run_scorer("judge agreement", score_judge_agreement, gateway)
    relation = _run_scorer("relation", score_relation, gateway)

    headline = {
        "extraction_f1": extraction.get("f1"),
        "mapping_accuracy_pct": (mapping["classification_agreement"] * 100) if "error" not in mapping else None,
        "provenance_grounding_rate_pct": provenance.get("grounding_rate_pct"),
        "fallback_review_trigger_rate_pct": (mapping["needs_review_rate"] * 100) if "error" not in mapping else None,
        "relation_classification_accuracy_pct": (relation["accuracy"] * 100) if "error" not in relation else None,
    }

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "extraction": extraction,
        "mapping": mapping,
        "provenance": provenance,
        "judge_agreement": judge_agreement,
        "relation": relation,
        "headline": headline,
    }

    RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    REPORT_PATH.write_text(_render_report(results), encoding="utf-8")

    print(f"\nWrote {RESULTS_PATH}")
    print(f"Wrote {REPORT_PATH}")

    def _fmt(value: float | None, decimals: int) -> str:
        return f"{value:.{decimals}f}" if value is not None else "UNAVAILABLE"

    f1 = headline["extraction_f1"]
    mapping_pct = headline["mapping_accuracy_pct"]
    provenance_pct = headline["provenance_grounding_rate_pct"]
    fallback_pct = headline["fallback_review_trigger_rate_pct"]
    relation_pct = headline["relation_classification_accuracy_pct"]

    print(f"\nExtraction F1: {_fmt(f1, 3)} (threshold {EXTRACTION_F1_THRESHOLD})")
    print(f"Mapping Accuracy: {_fmt(mapping_pct, 1)}%")
    print(f"Provenance Grounding Rate: {_fmt(provenance_pct, 1)}% (threshold {PROVENANCE_GROUNDING_THRESHOLD}%)")
    print(f"Fallback/Review Trigger Rate: {_fmt(fallback_pct, 1)}%")
    print(f"Relation Classification Accuracy: {_fmt(relation_pct, 1)}%")


if __name__ == "__main__":
    main()
