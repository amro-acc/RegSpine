"""ChangeWatcherAgent: detects and classifies textual changes between two
versions of the same regulatory document, and judges whether an amended
clause breaks a previously-mapped control (spec.md §7.2.11 — feature 2,
"Regulatory Change Intelligence"). Reopened from `docs/roadmap.md`'s cut
list at the user's request; scoped down from full spec fidelity below.

Scope note: spec.md §7.2.11 wants clause_ref-level alignment (with an
embedding-similarity fallback for renumbered clauses). The live pipeline has
no `clause_segmenter` (spec.md §7.2.3) — every run's regulation text is one
ad-hoc `Clause` blob (`src/api/main.py`'s `_create_ad_hoc_clause`), not real
per-article clauses with distinct `clause_ref` values. This agent instead
diffs at sentence granularity *within* that single blob, using `difflib`
deterministically first (matches spec's own "editorial reflows dominate;
diff first, model only on genuine textual change" reasoning) and only calls
REASONER for materiality/breaks-control judgement on the subset of regions
that (a) survive the deterministic editorial filter and (b) overlap an
existing tracked obligation's cited source span. Renumbered-clause alignment
is not attempted — meaningless without real `clause_ref` segmentation.

Per spec.md §7.2.11's process description, only `amended` regions get a
model materiality call; `added`/`removed` classification is deterministic.
For `added` regions this agent additionally delegates to `IngestionAgent`
(the same extractor real ingestion uses) to answer "is there a new binding
obligation in this text at all" — a deliberate extension beyond the literal
spec text, because "identify newly added obligations" is exactly what the
user asked this feature to do.

Delta gap detection (2026-09-28): beyond classifying *that* something changed,
this agent now answers "does the change actually break coverage" the same
deterministic way the main pipeline does, instead of trusting only the
change-watcher prompt's qualitative `breaks_control` narrative flag:
  - `amended` regions with a tracked obligation reuse `AuditAgent` against
    that obligation's *existing* mapped control, with the obligation's text
    swapped to the new clause wording — same "does this control still
    satisfy this obligation" question `audit_node` already asks, just fed
    the amended requirement instead of the original one.
  - `added` regions (and `replace` regions with no tracked obligation to
    re-check) run their new obligation candidates through `MappingAgent`
    against `existing_controls` (the bank's already-known control set, per
    the caller-supplied list — this agent stays DB-agnostic, so the caller
    fetches that list), then `AuditAgent`, exactly like a first-time
    ingestion run would.
Reuses the same agents/prompts the main pipeline uses rather than inventing
parallel logic — a control that can't satisfy an amended/new obligation is
the same kind of gap either way.
"""

from __future__ import annotations

import difflib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from src.agents.audit_agent import AuditAgent
from src.agents.ingestion_agent import IngestionAgent, parse_json_response
from src.agents.mapping_agent import MappingAgent
from src.core.schemas import (
    ChangeType,
    ClauseChange,
    ControlMapping,
    GapFinding,
    InternalControl,
    Materiality,
    RegulatoryObligation,
)
from src.llm.gateway import LLMGateway
from src.verify.span import verify_citation_span

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPT_PATH = REPO_ROOT / "src" / "prompts" / "change_watcher" / "v1.md"

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;:])\s+")


def _load_prompt_template() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _split_sentences(text: str) -> list[str]:
    """Coarse sentence-level segmentation — good enough to localize a diff
    region without a real clause_segmenter (see module docstring)."""
    pieces = [p for p in _SENTENCE_SPLIT_RE.split(text.strip()) if p]
    return pieces or ([text] if text.strip() else [])


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


@dataclass
class DiffRegion:
    kind: str  # "insert" | "delete" | "replace"
    old_text: str
    new_text: str
    old_start_char: int
    old_end_char: int


def find_diff_regions(old_text: str, new_text: str) -> list[DiffRegion]:
    """Deterministic, no model call (spec.md §7.2.11: "deterministic diff
    first, model only on genuine textual change"). Splits both texts into
    sentences, diffs at that granularity via `difflib`, drops opcodes whose
    old/new sides are identical once whitespace/case-normalized (pure
    editorial reflow), then re-locates each surviving region's old-side text
    back to a real character span in `old_text` using the same
    span-verification tiers the ingestion agent already trusts for exactly
    this "find substring, tolerate extraction noise" job."""
    old_sentences = _split_sentences(old_text)
    new_sentences = _split_sentences(new_text)

    matcher = difflib.SequenceMatcher(
        None,
        [_normalize(s) for s in old_sentences],
        [_normalize(s) for s in new_sentences],
        autojunk=False,
    )

    regions: list[DiffRegion] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_span = " ".join(old_sentences[i1:i2])
        new_span = " ".join(new_sentences[j1:j2])
        if _normalize(old_span) == _normalize(new_span):
            continue

        old_start, old_end = 0, 0
        if old_span:
            located = verify_citation_span(old_text, old_span)
            if located["verified"]:
                old_start, old_end = located["start_char"], located["end_char"]

        regions.append(
            DiffRegion(kind=tag, old_text=old_span, new_text=new_span, old_start_char=old_start, old_end_char=old_end)
        )
    return regions


def _obligation_span(old_text: str, obligation: RegulatoryObligation) -> tuple[int, int] | None:
    located = verify_citation_span(old_text, obligation.verbatim_quote)
    if not located["verified"]:
        return None
    return located["start_char"], located["end_char"]


class ChangeWatcherAgent:
    PROMPT_VERSION = "v1"

    def __init__(
        self,
        gateway: LLMGateway | None = None,
        ingestion_agent: IngestionAgent | None = None,
        mapping_agent: MappingAgent | None = None,
        audit_agent: AuditAgent | None = None,
    ):
        self.gateway = gateway or LLMGateway()
        self.ingestion_agent = ingestion_agent or IngestionAgent(self.gateway)
        self.mapping_agent = mapping_agent or MappingAgent(self.gateway)
        self.audit_agent = audit_agent or AuditAgent(self.gateway)
        self._prompt_template = _load_prompt_template()

    def _model_id(self) -> str:
        return self.gateway.config["reasoner"]["model"]

    def _render_prompt(
        self,
        old_span: str,
        new_span: str,
        obligation: RegulatoryObligation,
        control: InternalControl | None,
        coverage_level: str | None,
    ) -> str:
        prompt = self._prompt_template
        replacements = {
            "{{old_clause_text}}": old_span,
            "{{new_clause_text}}": new_span,
            "{{obligation_text}}": obligation.obligation_text,
            "{{obligation_modality}}": obligation.modality.value,
            "{{obligation_deadline}}": obligation.deadline_spec or "n/a",
            "{{control_title}}": control.title if control else "(no control mapped)",
            "{{control_design_description}}": str(control.design_description) if control else "n/a",
            "{{control_automation}}": str(control.automation) if control else "n/a",
            "{{control_frequency}}": str(control.frequency) if control else "n/a",
            "{{existing_coverage_level}}": coverage_level or "none",
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        return prompt

    def assess_amended_region(
        self,
        old_span: str,
        new_span: str,
        obligation: RegulatoryObligation,
        control: InternalControl | None,
        mapping: ControlMapping | None,
    ) -> dict:
        prompt = self._render_prompt(
            old_span, new_span, obligation, control, mapping.coverage_level.value if mapping else None
        )
        response = self.gateway.call(role="REASONER", prompt=prompt, schema_version="change_watcher_v1")
        parsed = parse_json_response(response.get("content", ""))

        materiality = parsed.get("materiality")
        if materiality not in {m.value for m in Materiality}:
            # Unparseable/missing model output must not silently read as "no
            # materiality" — default to MEDIUM, the honest "needs a human
            # look" middle ground, not the reassuring end of the scale.
            materiality = Materiality.MEDIUM.value

        return {
            "materiality": materiality,
            "obligation_delta": parsed.get("obligation_delta", ""),
            "breaks_control": bool(parsed.get("breaks_control", False)),
            "rationale": parsed.get("rationale", ""),
        }

    def _detect_gap_for_new_obligation(
        self, obligation: RegulatoryObligation, existing_controls: list[InternalControl], run_id: uuid.UUID
    ) -> GapFinding | None:
        """Same question a first-time ingestion run asks: does any existing
        control cover this obligation? MappingAgent picks (or fails to pick)
        a candidate from `existing_controls`; AuditAgent turns that into a
        real GapFinding, deterministic gap_class included (hard invariant
        #3 — no_control/partial_coverage come from coverage_level in code,
        not the model)."""
        mapping = self.mapping_agent.map(obligation, existing_controls, run_id)
        control = None
        if mapping is not None:
            control = next((c for c in existing_controls if c.id == mapping.control_id), None)
        return self.audit_agent.audit(obligation, control, mapping, run_id)

    def _handle_added(
        self, region: DiffRegion, new_clause_id: uuid.UUID, run_id: uuid.UUID,
        from_version_id: uuid.UUID, to_version_id: uuid.UUID, old_clause_id: uuid.UUID | None,
        existing_controls: list[InternalControl],
    ) -> tuple[ClauseChange, list[RegulatoryObligation], list[GapFinding]]:
        candidates = self.ingestion_agent.extract_obligations(
            region.new_text, new_clause_id, source_file="change_diff", page_number=1, run_id=run_id
        )
        materiality = Materiality.HIGH if candidates else Materiality.EDITORIAL
        rationale = (
            f"{len(candidates)} new obligation candidate(s) extracted from added text"
            if candidates
            else "New text added; no binding obligation detected in it"
        )

        gaps: list[GapFinding] = []
        for candidate in candidates:
            gap = self._detect_gap_for_new_obligation(candidate, existing_controls, run_id)
            if gap is not None:
                gaps.append(gap)

        change = ClauseChange(
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            old_clause_id=old_clause_id,
            new_clause_id=new_clause_id,
            change_type=ChangeType.ADDED,
            materiality=materiality,
            text_diff={
                "old_span": region.old_text,
                "new_span": region.new_text,
                "new_obligation_ids": [str(c.id) for c in candidates],
                "new_gap_ids": [str(g.id) for g in gaps],
                "run_id": str(run_id),
            },
            rationale=rationale,
        )
        return change, candidates, gaps

    def _handle_removed(
        self, region: DiffRegion, affected: list[RegulatoryObligation], old_clause_id: uuid.UUID,
        run_id: uuid.UUID, from_version_id: uuid.UUID, to_version_id: uuid.UUID,
    ) -> ClauseChange:
        if affected:
            materiality = Materiality.MEDIUM
            names = ", ".join(o.obligation_text[:80] for o in affected)
            rationale = f"Source text removed; previously tracked obligation(s) no longer have a clause to trace to: {names}"
        else:
            materiality = Materiality.EDITORIAL
            rationale = "Text removed; no tracked obligation's cited span fell inside it."
        return ClauseChange(
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            old_clause_id=old_clause_id,
            new_clause_id=None,
            change_type=ChangeType.REMOVED,
            materiality=materiality,
            text_diff={
                "old_span": region.old_text,
                "new_span": "",
                "affected_obligation_ids": [str(o.id) for o in affected],
                "run_id": str(run_id),
            },
            rationale=rationale,
        )

    def diff(
        self,
        old_text: str,
        new_text: str,
        from_version_id: uuid.UUID,
        to_version_id: uuid.UUID,
        old_clause_id: uuid.UUID,
        new_clause_id: uuid.UUID,
        obligations: list[RegulatoryObligation],
        controls_by_obligation_id: dict[uuid.UUID, InternalControl | None],
        mappings_by_obligation_id: dict[uuid.UUID, ControlMapping | None],
        existing_controls: list[InternalControl],
        run_id: uuid.UUID,
    ) -> tuple[list[ClauseChange], list[RegulatoryObligation], list[GapFinding]]:
        """Returns (clause_changes, new_obligation_candidates, gaps). Callers
        persist all three — new_obligation_candidates are real
        `RegulatoryObligation` rows (review_state=PROPOSED, unmapped), not
        just metadata, so they show up in the lineage graph like any other
        obligation once accepted; `gaps` are real `GapFinding` rows produced
        by re-running mapping/audit against `existing_controls` (the bank's
        already-known control set — caller-supplied, this agent stays
        DB-agnostic) for added obligations, and against the obligation's
        existing mapped control for amended ones."""
        regions = find_diff_regions(old_text, new_text)
        obligation_spans = {
            obligation.id: span
            for obligation in obligations
            if (span := _obligation_span(old_text, obligation)) is not None
        }

        clause_changes: list[ClauseChange] = []
        new_obligations: list[RegulatoryObligation] = []
        gaps: list[GapFinding] = []

        for region in regions:
            if region.kind == "insert":
                change, candidates, region_gaps = self._handle_added(
                    region, new_clause_id, run_id, from_version_id, to_version_id,
                    old_clause_id=None, existing_controls=existing_controls,
                )
                clause_changes.append(change)
                new_obligations.extend(candidates)
                gaps.extend(region_gaps)
                continue

            affected = [
                obligation
                for obligation in obligations
                if (span := obligation_spans.get(obligation.id)) is not None
                and _overlaps(region.old_start_char, region.old_end_char, span[0], span[1])
            ]

            if region.kind == "delete":
                clause_changes.append(
                    self._handle_removed(region, affected, old_clause_id, run_id, from_version_id, to_version_id)
                )
                continue

            # tag == "replace"
            if not affected:
                # Changed text with no tracked obligation overlapping it —
                # treat the new side as potential new content, same as a
                # pure insert.
                change, candidates, region_gaps = self._handle_added(
                    region, new_clause_id, run_id, from_version_id, to_version_id,
                    old_clause_id, existing_controls=existing_controls,
                )
                clause_changes.append(change)
                new_obligations.extend(candidates)
                gaps.extend(region_gaps)
                continue

            for obligation in affected:
                control = controls_by_obligation_id.get(obligation.id)
                mapping = mappings_by_obligation_id.get(obligation.id)
                assessment = self.assess_amended_region(region.old_text, region.new_text, obligation, control, mapping)

                # Deterministic re-check (spec.md §7.2.10's own gap-detection
                # path), not just the change-watcher prompt's qualitative
                # breaks_control flag: does the *existing* mapped control
                # still satisfy the *amended* wording of this obligation?
                amended_obligation = obligation.model_copy(
                    update={"obligation_text": region.new_text, "verbatim_quote": region.new_text}
                )
                amendment_gap = self.audit_agent.audit(amended_obligation, control, mapping, run_id)
                if amendment_gap is not None:
                    gaps.append(amendment_gap)

                clause_changes.append(
                    ClauseChange(
                        from_version_id=from_version_id,
                        to_version_id=to_version_id,
                        old_clause_id=old_clause_id,
                        new_clause_id=new_clause_id,
                        change_type=ChangeType.AMENDED,
                        materiality=Materiality(assessment["materiality"]),
                        text_diff={
                            "old_span": region.old_text,
                            "new_span": region.new_text,
                            "affected_obligation_id": str(obligation.id),
                            "breaks_control": assessment["breaks_control"],
                            "obligation_delta": assessment["obligation_delta"],
                            "gap_id": str(amendment_gap.id) if amendment_gap else None,
                            "run_id": str(run_id),
                        },
                        rationale=assessment["rationale"],
                    )
                )

        return clause_changes, new_obligations, gaps
