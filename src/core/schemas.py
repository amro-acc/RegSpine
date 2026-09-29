"""Pydantic schemas for the full traceability chain.

Regulations -> obligations -> applicability -> internal policies/controls ->
evidence -> testing -> gaps -> remediation -> ongoing monitoring, plus the
observability layer (runs/agent_steps/llm_calls) and the HITL review log.

Every derived row must carry provenance, no exceptions: RegulatoryObligation,
InternalControl, ControlMapping, GapFinding, and RemediationAction all carry
the same execution-provenance fields (created_by_agent, model_id,
prompt_version, run_id, confidence, review_state).

Two distinct things are both called "provenance" here, deliberately kept as
two separate fields rather than merged, because they answer different
questions and one of them (review_state, confidence) is load-bearing in a DB
CHECK constraint:
  - Execution provenance (flat columns, as above): *who/what produced this
    row* — which agent, which model, which run.
  - `provenance: SourceProvenance` (a single JSONB-backed field): *where in
    the source this row traces back to* — source_file, page_number,
    snippet_hash. Required, strict — see SourceProvenance below.

Two roadmap tables (`ClauseChange`, `ObligationRelation`) are modeled even
though no agent populates them yet — kept ready so adding them later is
additive rather than a migration.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


# ============ Enums ============


class RiskSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ComplianceStatus(str, Enum):
    COMPLIANT = "COMPLIANT"
    PARTIALLY_COMPLIANT = "PARTIALLY_COMPLIANT"
    NON_COMPLIANT = "NON_COMPLIANT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ReviewState(str, Enum):
    """HITL review state."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


class Modality(str, Enum):
    MUST = "must"
    SHOULD = "should"
    MAY = "may"
    PROHIBITED = "prohibited"


class DocClass(str, Enum):
    REGULATION = "regulation"
    GUIDELINE = "guideline"
    STANDARD = "standard"
    CIRCULAR = "circular"


class ExtractionMethod(str, Enum):
    NATIVE = "native"
    OCR = "ocr"                # roadmap, not populated yet
    VISION = "vision"          # roadmap, not populated yet
    RECONCILED = "reconciled"  # roadmap, not populated yet


class ApplicabilityDriver(str, Enum):
    GEOGRAPHY = "geography"
    LICENCE = "licence"
    PRODUCT = "product"
    THRESHOLD = "threshold"


class ControlType(str, Enum):
    PREVENTIVE = "preventive"
    DETECTIVE = "detective"
    CORRECTIVE = "corrective"


class Automation(str, Enum):
    MANUAL = "manual"
    SEMI = "semi"
    AUTOMATED = "automated"


class CoverageLevel(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"


class Sufficiency(str, Enum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class OperatingEffective(str, Enum):
    """Tri-state, not boolean: an assessment with no evidence returns
    'unknown', which is a different and more useful answer than 'false'."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class ChangeType(str, Enum):
    """Roadmap — no agent populates this yet."""

    ADDED = "added"
    REMOVED = "removed"
    AMENDED = "amended"
    RENUMBERED = "renumbered"
    UNCHANGED = "unchanged"


class Materiality(str, Enum):
    """Roadmap — no agent populates this yet."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    EDITORIAL = "editorial"


class RelationType(str, Enum):
    """Roadmap — no agent populates this yet."""

    OVERLAPS = "overlaps"
    CONFLICTS_WITH = "conflicts_with"
    SUPERSEDES = "supersedes"
    IMPLEMENTS = "implements"


class ActionType(str, Enum):
    """Structure for RemediationAction.action_type. Stored as plain TEXT in
    the DB, not a CHECK-constrained enum like coverage_level/gap_class —
    those are closed taxonomies, but action types are an open set that keeps
    growing, so a DB constraint would just create migration churn."""

    POLICY_UPDATE = "POLICY_UPDATE"
    NEW_CONTROL = "NEW_CONTROL"
    CONTROL_ENHANCEMENT = "CONTROL_ENHANCEMENT"
    PROCESS_CHANGE = "PROCESS_CHANGE"
    TRAINING = "TRAINING"
    OTHER = "OTHER"


class LLMRole(str, Enum):
    EXTRACTOR = "EXTRACTOR"
    REASONER = "REASONER"
    JUDGE = "JUDGE"
    SUMMARIZER = "SUMMARIZER"


class SourceProvenance(BaseModel):
    """Strict source-traceability provenance. A plain `dict[str, Any]` field
    would not actually enforce required keys — this is a real Pydantic model
    specifically so `source_file`/`page_number`/`snippet_hash` are mandatory,
    and it still serializes to a plain dict (`.model_dump()`) for storage in
    a JSONB column, which is what "Dict in Pydantic, JSONB in Postgres" means
    in practice.

    `page_number` is genuinely ambiguous for non-paginated sources (an xlsx
    control-register row has no "page") — for those, the team needs to decide
    a convention (e.g. row index) rather than leave it silently undefined.
    """

    source_file: str
    page_number: int
    snippet_hash: str  # sha256 of the exact source text this row was derived from


class ReviewDecision(str, Enum):
    """The `action` column on `review_actions` — named ReviewDecision, not
    ReviewAction, to avoid colliding with the ReviewAction model below."""

    ACCEPT = "accept"
    REJECT = "reject"
    AMEND = "amend"


# ============ Corpus ============


class RegulatoryDocument(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    regulator: str  # BCBS | EU | PCI SSC | ...
    jurisdiction: str  # GLOBAL | EU | US | ...
    title: str
    doc_class: DocClass
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DocumentVersion(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    document_id: uuid.UUID
    version_label: str  # 'v3.2.1' | '2024-consolidated'
    effective_date: date | None = None
    source_uri: str | None = None
    source_sha256: str
    page_count: int | None = None
    ingest_run_id: uuid.UUID | None = None


class Page(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    document_version_id: uuid.UUID
    page_no: int
    text_content: str | None = None
    # Reserved for a future OCR/vision extraction path; unused/null for now:
    ocr_content: str | None = None
    extraction_method: ExtractionMethod = ExtractionMethod.NATIVE
    reconciliation_score: float | None = None
    unreadable: bool = False
    image_uri: str | None = None


class Clause(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    document_version_id: uuid.UUID
    clause_ref: str  # 'Art. 9(4)(a)' | '8.3.6'
    parent_clause_id: uuid.UUID | None = None
    text_content: str
    page_no: int
    bbox: dict | None = None
    char_span: tuple[int, int] | None = None  # maps to int4range


# ============ Observability ============


class Run(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    pipeline: str
    status: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    total_cost_usd: float = 0
    config_snapshot: dict | None = None


class AgentStep(BaseModel):
    id: int | None = None  # bigserial
    run_id: uuid.UUID
    node: str
    agent: str
    input_hash: str | None = None
    output_hash: str | None = None
    retry_count: int = 0
    verdict: str | None = None
    latency_ms: int | None = None
    error: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LLMCall(BaseModel):
    id: int | None = None  # bigserial
    run_id: uuid.UUID
    step_id: int | None = None
    role: LLMRole
    model_id: str
    prompt_version: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    cache_hit: bool = False
    # Every fallback invocation must be logged distinctly, not blended with
    # primary calls.
    fallback_used: bool = False


class ReviewAction(BaseModel):
    """HITL review log — feeds the trainability loop."""

    id: int | None = None  # bigserial
    entity_table: str  # polymorphic — not a real FK, can point at any table
    entity_id: uuid.UUID
    reviewer: str
    action: ReviewDecision
    original_output: dict | None = None
    corrected_output: dict | None = None
    note: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ============ Obligations ============


class RegulatoryObligation(BaseModel):
    """A single extracted obligation. FKs to `Clause` rather than storing a
    denormalized source reference, since the document/clause layer already
    carries that."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    clause_id: uuid.UUID

    obligation_text: str
    verbatim_quote: str  # exact substring from the source — span verifier checks this
    modality: Modality
    actor: str | None = None
    trigger_condition: str | None = None
    deadline_spec: str | None = None
    obligation_type: str  # governance | reporting | capital | data | ict | conduct | recordkeeping

    # Execution provenance — uniform across all 5 derived entities, see
    # module docstring
    confidence: float = Field(ge=0.0, le=1.0)
    span_verified: bool = False
    span_verify_method: str | None = None  # "exact" | "normalized" | "fuzzy_extraction" | None
    review_state: ReviewState = ReviewState.PROPOSED
    created_by_agent: str
    model_id: str
    prompt_version: str
    run_id: uuid.UUID
    scenario_id: uuid.UUID | None = None  # null = real spine; set = simulation (roadmap)

    # Source provenance (distinct from the above — see module docstring)
    provenance: SourceProvenance

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ============ Bank side ============


class BankEntity(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    bank_id: uuid.UUID  # tenant/group-level id — not an FK, a partition key
    name: str
    jurisdiction: str
    licences: list[str] = Field(default_factory=list)
    product_lines: list[str] = Field(default_factory=list)
    parent_entity_id: uuid.UUID | None = None


class ObligationApplicability(BaseModel):
    """Every `applies=True` must cite a specific profile attribute —
    unsupported assertions are rejected at the application layer, not by a
    DB constraint (cited_profile_attribute is NOT NULL, but the DB can't
    verify the citation actually supports the claim)."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    obligation_id: uuid.UUID
    entity_id: uuid.UUID
    applies: bool
    driver: ApplicabilityDriver
    rationale: str
    cited_profile_attribute: str
    confidence: float = Field(ge=0.0, le=1.0)


class InternalControl(BaseModel):
    """Execution-provenance columns are uniform with the other 4 derived
    entities (see module docstring), even though control_ingest itself is
    deterministic for register rows and EXTRACTOR-based only for policy
    prose."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    bank_id: uuid.UUID  # same tenant/group partition key as BankEntity.bank_id
    control_ref: str
    title: str
    design_description: str | None = None
    owner: str | None = None
    control_type: ControlType | None = None
    automation: Automation | None = None
    frequency: str | None = None
    source_policy_version_id: uuid.UUID | None = None  # conceptually a DocumentVersion.id
    source_page_no: int | None = None

    # Span verification — the zero-hallucination gate applies to controls
    # too, not just obligations. Nullable because a control from a
    # structured register row has no free-text quote to verify.
    verbatim_quote: str | None = None
    span_verified: bool = False
    span_verify_method: str | None = None  # "exact" | "normalized" | "fuzzy_extraction" | None

    # Execution provenance — see module docstring
    confidence: float = Field(ge=0.0, le=1.0)
    review_state: ReviewState = ReviewState.PROPOSED
    created_by_agent: str
    model_id: str
    prompt_version: str
    run_id: uuid.UUID
    scenario_id: uuid.UUID | None = None

    # Source provenance — distinct from execution provenance above, see
    # module docstring
    provenance: SourceProvenance


# ============ Mapping, evidence, assessment ============


class ControlMapping(BaseModel):
    """Obligation <-> control mapping. `prompt_version` is included here for
    consistency with the other derived entities — see module docstring."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    obligation_id: uuid.UUID
    control_id: uuid.UUID

    coverage_level: CoverageLevel
    rationale: str  # must reference both obligation text and control text
    cited_control_span: str | None = None
    retrieval_rank: int | None = None
    rerank_score: float | None = None

    # Execution provenance — see module docstring
    confidence: float = Field(ge=0.0, le=1.0)
    review_state: ReviewState = ReviewState.PROPOSED
    created_by_agent: str
    model_id: str
    prompt_version: str
    run_id: uuid.UUID
    scenario_id: uuid.UUID | None = None

    # Source provenance — distinct from execution provenance above, see
    # module docstring
    provenance: SourceProvenance

    # compliance_status is layered on top of coverage_level rather than
    # replacing it — coverage_level is the field the schema actually defines.
    compliance_status: ComplianceStatus | None = None


class EvidenceArtifact(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    bank_id: uuid.UUID
    artifact_uri: str
    modality: str  # pdf|csv|xlsx|log active; png is roadmap (§6.3)
    period_start: date | None = None
    period_end: date | None = None
    extracted_facts: dict | None = None
    pii_flags: dict | None = None


class ControlEvidenceLink(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    control_id: uuid.UUID
    evidence_id: uuid.UUID
    supports: bool
    sufficiency: Sufficiency
    freshness_days: int | None = None
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)


class ControlAssessment(BaseModel):
    """`operating_effective` can never be 'true' without a 'sufficient',
    in-window `ControlEvidenceLink` — enforced in application code (not yet
    built), not by a DB constraint or by this model, since a column-level
    CHECK can't express a cross-table join."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    control_id: uuid.UUID
    design_effective: bool | None = None
    operating_effective: OperatingEffective | None = None
    basis: str
    exceptions: dict | None = None
    assessed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    run_id: uuid.UUID
    scenario_id: uuid.UUID | None = None


# ============ Gaps, remediation, monitoring ============


class GapFinding(BaseModel):
    """No `control_id` field — a gap traces back to its control (if any)
    indirectly via the `ControlMapping` for the same `obligation_id`, not a
    direct FK on this table (matches the live `gaps` table, which has no
    control_id column either).

    `provenance` (source_file/page_number/snippet_hash) is the least
    naturally well-defined of the 5 entities for this field: a gap isn't
    extracted from one specific page, it's derived from the *absence or
    weakness* of a mapping. Convention used here: point at the triggering
    obligation's source. If that's not what the team wants, this is the
    field to revisit."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    obligation_id: uuid.UUID
    entity_id: uuid.UUID | None = None

    gap_class: str  # e.g. no_control | partial_coverage | control_no_evidence | ...
    narrative: str
    risk_factors: dict[str, int]  # the 5 factors (1-5 each), config/risk.yaml's weights apply to this
    risk_score: int = Field(ge=0, le=100)  # deterministic arithmetic — never model-computed
    risk_band: RiskSeverity
    status: str = "open"

    # Execution provenance — see module docstring
    confidence: float = Field(ge=0.0, le=1.0)
    review_state: ReviewState = ReviewState.PROPOSED
    created_by_agent: str
    model_id: str
    prompt_version: str
    run_id: uuid.UUID
    scenario_id: uuid.UUID | None = None

    # Source provenance — distinct from execution provenance above, see
    # module docstring
    provenance: SourceProvenance


class RemediationAction(BaseModel):
    """`provenance` here follows the same convention as GapFinding: points at
    the source that justifies the remediation (typically the same source as
    the gap/obligation it addresses), not a new independent source."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    gap_id: uuid.UUID

    action: str
    action_type: ActionType | None = None
    control_design_delta: str | None = None
    owner_role: str | None = None
    effort_estimate: str | None = None
    target_date: date | None = None
    test_plan: str | None = None
    monitoring_metric: str | None = None  # concrete metric for ongoing monitoring (Step 6)
    status: str = "proposed"

    # Execution provenance — see module docstring
    confidence: float = Field(ge=0.0, le=1.0)
    review_state: ReviewState = ReviewState.PROPOSED
    created_by_agent: str
    model_id: str
    prompt_version: str
    run_id: uuid.UUID
    scenario_id: uuid.UUID | None = None

    # Source provenance — distinct from execution provenance above, see
    # module docstring
    provenance: SourceProvenance


class MonitoringItem(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    obligation_id: uuid.UUID | None = None
    control_id: uuid.UUID | None = None
    next_review_due: date
    trigger_conditions: dict | None = None


# ============ Change & cross-regulation — roadmap, schema kept ready ============


class ClauseChange(BaseModel):
    """Roadmap — no agent populates this yet."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    from_version_id: uuid.UUID
    to_version_id: uuid.UUID
    old_clause_id: uuid.UUID | None = None
    new_clause_id: uuid.UUID | None = None
    change_type: ChangeType
    materiality: Materiality | None = None
    text_diff: dict | None = None
    rationale: str | None = None


class ObligationRelation(BaseModel):
    """Roadmap — no agent populates this yet."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    obligation_a: uuid.UUID
    obligation_b: uuid.UUID
    relation_type: RelationType
    dimension: str | None = None  # retention|disclosure|timing|...
    rationale: str
    severity: str | None = None
    resolution_hint: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
