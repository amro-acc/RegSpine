"""Grounded span verification (spec.md §8.2, hard invariants #1-#2).

Deterministic string work only — no model call anywhere in this module.
This is the load-bearing anti-hallucination control: a citation that cannot
be traced to an exact (or near-exact, extraction-artifact-tolerant) location
in its source text must never reach `accepted`/`span_verified=True`.

Named verify_citation_span(source_text, citation_text) rather than the
verify_span(quote, source_text) pseudocode in spec.md §8.2 — that section
predates any actual code; this is now the authoritative signature and
spec.md has been updated to match rather than left to drift.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PIPELINE_CONFIG_PATH = REPO_ROOT / "config" / "pipeline.yaml"


@lru_cache(maxsize=1)
def _fuzzy_floor() -> float:
    """config/pipeline.yaml's span_verifier.fuzzy_floor — not hardcoded here
    a second time (CLAUDE.md §5: config over constants)."""
    with open(PIPELINE_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)["span_verifier"]["fuzzy_floor"]


def _normalize_with_offsets(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace runs to a single space and lowercase, while
    tracking which original-text index each normalized character came from
    — needed to map a match found in normalized space back to real
    start/end character offsets in the caller's actual source_text."""
    normalized_chars: list[str] = []
    offset_map: list[int] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        # Line-wrap hyphenation artifact ("with-\nin" -> "within"): a hyphen
        # immediately followed by whitespace is dropped entirely, along with
        # the whitespace run, rather than kept as a literal "-" plus a space
        # — otherwise "with- in" never gets close enough to "within" for the
        # fuzzy floor, defeating the whole point of this tier (spec.md §8.2).
        if ch == "-" and i + 1 < n and text[i + 1].isspace():
            i += 1
            while i < n and text[i].isspace():
                i += 1
            continue
        if ch.isspace():
            normalized_chars.append(" ")
            offset_map.append(i)
            while i < n and text[i].isspace():
                i += 1
        else:
            normalized_chars.append(ch.lower())
            offset_map.append(i)
            i += 1
    return "".join(normalized_chars), offset_map


def _fuzzy_best_window(source_text: str, citation_text: str) -> dict:
    """Find where citation_text best aligns inside source_text and score
    just that region, using difflib's own matching-block alignment rather
    than a hand-rolled sliding window.

    (An earlier version slid a fixed-length window of size
    `len(citation) + tolerance` across the source — always *longer* than the
    citation — which systematically penalized every candidate's ratio,
    including the correctly-aligned one. Caught by testing against a
    realistic single-character OCR substitution, which should score close
    to 1.0 and didn't. Matching blocks find the aligned region directly,
    regardless of exactly how long it turns out to be.)
    """
    source_norm, offset_map = _normalize_with_offsets(source_text)
    citation_norm, _ = _normalize_with_offsets(citation_text)

    n, m = len(source_norm), len(citation_norm)
    if m == 0 or n == 0:
        return {"ratio": 0.0, "start_char": 0, "end_char": 0}

    matcher = SequenceMatcher(None, source_norm, citation_norm, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size > 0]
    if not blocks:
        return {"ratio": 0.0, "start_char": 0, "end_char": 0}

    region_start = blocks[0].a
    region_end = blocks[-1].a + blocks[-1].size

    window = source_norm[region_start:region_end]
    best_ratio = SequenceMatcher(None, window, citation_norm).ratio()
    best_start, best_end = region_start, region_end

    start_char = offset_map[best_start] if best_start < len(offset_map) else len(source_text)
    end_idx = min(max(best_end - 1, best_start), len(offset_map) - 1)
    end_char = (offset_map[end_idx] + 1) if offset_map else len(source_text)

    return {"ratio": best_ratio, "start_char": start_char, "end_char": end_char}


def verify_citation_span(source_text: str, citation_text: str) -> dict:
    """Hard gate. Deterministic: no model call in this path (hard invariants
    #1-#2). Returns:
        {
            "verified": bool,
            "match_ratio": float,
            "start_char": int,
            "end_char": int,
            "method": "exact" | "normalized" | "fuzzy_extraction" | "none",
        }
    `method` is additive beyond what this step asked for — it's the field
    src/core/schemas.py's span_verify_method column expects, and it's the
    difference between "the demo can show which tier caught this" and "we
    just have a boolean."
    """
    if not citation_text:
        return {"verified": False, "match_ratio": 0.0, "start_char": 0, "end_char": 0, "method": "none"}

    # Tier 1: exact substring
    idx = source_text.find(citation_text)
    if idx != -1:
        return {
            "verified": True,
            "match_ratio": 1.0,
            "start_char": idx,
            "end_char": idx + len(citation_text),
            "method": "exact",
        }

    # Tier 2: whitespace-normalized substring
    source_norm, offset_map = _normalize_with_offsets(source_text)
    citation_norm, _ = _normalize_with_offsets(citation_text)
    idx_norm = source_norm.find(citation_norm)
    if idx_norm != -1 and citation_norm:
        end_norm_idx = min(idx_norm + len(citation_norm) - 1, len(offset_map) - 1)
        start_char = offset_map[idx_norm]
        end_char = offset_map[end_norm_idx] + 1
        return {
            "verified": True,
            "match_ratio": 0.99,
            "start_char": start_char,
            "end_char": end_char,
            "method": "normalized",
        }

    # Tier 3: fuzzy match tolerating extraction artifacts (hyphenation,
    # line-wrap) — deliberately tight floor (config/pipeline.yaml), absorbs
    # noise without licensing paraphrase.
    best = _fuzzy_best_window(source_text, citation_text)
    if best["ratio"] >= _fuzzy_floor():
        return {
            "verified": True,
            "match_ratio": best["ratio"],
            "start_char": best["start_char"],
            "end_char": best["end_char"],
            "method": "fuzzy_extraction",
        }

    return {
        "verified": False,
        "match_ratio": best["ratio"],
        "start_char": 0,
        "end_char": 0,
        "method": "none",
    }
