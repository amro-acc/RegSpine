"""Gates on evals/run_benchmarks.py's quantitative-reliability output,
rather than re-running the eval suite live inside pytest -- keeps this
test hermetic/fast/deterministic like every other test in tests/: a
precomputed artifact is checked, not regenerated on every test run.

Fails loudly (not skips) if evals/results.json doesn't exist yet -- an
absent eval result is not the same as a passing one, so a missing gate
should surface as a failure, not get quietly skipped past.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

RESULTS_PATH = REPO_ROOT / "evals" / "results.json"


def _load_results() -> dict:
    if not RESULTS_PATH.exists():
        raise AssertionError(
            f"{RESULTS_PATH} does not exist -- run `python evals/run_benchmarks.py` first "
            "(it hits real LLM providers on a cache miss, then replays instantly from "
            "cache.db on every re-run)."
        )
    return json.loads(RESULTS_PATH.read_text(encoding="utf-8"))


def test_extraction_f1_meets_declared_threshold():
    results = _load_results()
    f1 = results["headline"]["extraction_f1"]
    assert f1 >= 0.85, f"Extraction F1 {f1:.3f} is below the declared 0.85 threshold"


def test_provenance_grounding_is_100_percent():
    results = _load_results()
    rate = results["headline"]["provenance_grounding_rate_pct"]
    assert rate == 100.0, f"Provenance grounding rate {rate:.1f}% is below 100% -- a hallucinated span exists"
