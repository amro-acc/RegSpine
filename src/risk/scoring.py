"""Deterministic risk scoring (spec.md §7.3, hard invariant #3).

"Arithmetic is Python, judgement is the model." The model supplies five
factors (1-5 each, with rationale — the rationale isn't captured by this
module; it lives in the GapFinding.narrative the calling agent writes). This
module turns those factors into a score using config/risk.yaml's weights.
No model call anywhere in this file.

Scope note: the deadline_override refinement (config/risk.yaml) — flooring
the band when a statutory deadline is inside N days — is not implemented
here. It needs a parsed date from RegulatoryObligation.deadline_spec, which
is a free-text field ("24 hours", "annually", "five years") with no defined
grammar; parsing that reliably is separate, nontrivial work, not something
this step asked for.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from src.core.schemas import RiskSeverity

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RISK_CONFIG_PATH = REPO_ROOT / "config" / "risk.yaml"

REQUIRED_FACTORS = (
    "regulatory_severity",
    "enforcement_likelihood",
    "business_exposure",
    "control_weakness",
    "remediation_urgency",
)


@lru_cache(maxsize=1)
def _load_config() -> dict:
    with open(RISK_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def compute_risk_score(risk_factors: dict[str, int]) -> int:
    """risk_score = round(sum(weight * factor) / 5 * 100) — spec.md §7.3.
    Each factor must be an integer 1-5; missing factors are a caller bug,
    not something to silently default (a model-supplied factor set that's
    incomplete should fail loudly, not produce a quietly-wrong score)."""
    missing = [f for f in REQUIRED_FACTORS if f not in risk_factors]
    if missing:
        raise ValueError(f"risk_factors missing required keys: {missing}")

    weights = _load_config()["weights"]
    weighted_sum = sum(weights[factor] * risk_factors[factor] for factor in REQUIRED_FACTORS)
    return round(weighted_sum / 5 * 100)


def band_for_score(risk_score: int) -> RiskSeverity:
    bands = _load_config()["bands"]
    if risk_score >= bands["critical"]:
        return RiskSeverity.CRITICAL
    if risk_score >= bands["high"]:
        return RiskSeverity.HIGH
    if risk_score >= bands["medium"]:
        return RiskSeverity.MEDIUM
    return RiskSeverity.LOW


def score_and_band(risk_factors: dict[str, int]) -> tuple[int, RiskSeverity]:
    score = compute_risk_score(risk_factors)
    return score, band_for_score(score)
