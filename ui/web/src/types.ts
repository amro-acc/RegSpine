// Mirrors src/core/schemas.py (Pydantic, model_dump(mode="json")) and the
// shapes returned by src/api/main.py. Field names are snake_case throughout
// to match the API wire format exactly -- no client-side renaming layer.

export interface Provenance {
  source_file: string;
  page_number: number;
  snippet_hash: string;
}

export type ReviewState = "proposed" | "accepted" | "rejected" | "needs_review";
export type Modality = "must" | "should" | "may" | "prohibited";
export type CoverageLevel = "full" | "partial" | "none";
export type RiskBand = "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";
export type ControlType = "preventive" | "detective" | "corrective";
export type Automation = "manual" | "semi" | "automated";
export type ActionType =
  | "POLICY_UPDATE"
  | "NEW_CONTROL"
  | "CONTROL_ENHANCEMENT"
  | "PROCESS_CHANGE"
  | "TRAINING"
  | "OTHER";

// ============ Core spine entities ============

export interface Obligation {
  id: string;
  clause_id: string;
  obligation_text: string;
  verbatim_quote: string;
  modality: Modality;
  actor: string | null;
  trigger_condition: string | null;
  deadline_spec: string | null;
  obligation_type: string;
  confidence: number;
  span_verified: boolean;
  span_verify_method: string | null;
  review_state: ReviewState;
  created_by_agent: string;
  model_id: string;
  prompt_version: string;
  run_id: string;
  provenance: Provenance;
}

export interface Control {
  id: string;
  bank_id: string;
  control_ref: string;
  title: string;
  design_description: string | null;
  owner: string | null;
  control_type: ControlType | null;
  automation: Automation | null;
  frequency: string | null;
  verbatim_quote: string | null;
  span_verified: boolean;
  span_verify_method: string | null;
  confidence: number;
  review_state: ReviewState;
  created_by_agent: string;
  model_id: string;
  prompt_version: string;
  run_id: string;
  provenance: Provenance;
}

export interface Mapping {
  id: string;
  obligation_id: string;
  control_id: string;
  coverage_level: CoverageLevel;
  rationale: string;
  cited_control_span: string | null;
  confidence: number;
  review_state: ReviewState;
  created_by_agent: string;
  model_id: string;
  prompt_version: string;
  run_id: string;
  provenance: Provenance;
  // Nested by GET /api/v1/lineage/{run_id} -- null for a mapping whose
  // control row wasn't found (shouldn't normally happen, but the API
  // doesn't assume it can't).
  control: Control | null;
}

export interface Gap {
  id: string;
  obligation_id: string;
  entity_id: string | null;
  gap_class: string;
  narrative: string;
  risk_factors: Record<string, number>;
  risk_score: number;
  risk_band: RiskBand;
  status: string;
  confidence: number;
  review_state: ReviewState;
  created_by_agent: string;
  model_id: string;
  prompt_version: string;
  run_id: string;
  provenance: Provenance;
  // Nested by GET /api/v1/lineage/{run_id}.
  remediations: Remediation[];
}

export interface Remediation {
  id: string;
  gap_id: string;
  action: string;
  action_type: ActionType | null;
  control_design_delta: string | null;
  owner_role: string | null;
  effort_estimate: string | null;
  target_date: string | null;
  test_plan: string | null;
  monitoring_metric: string | null;
  status: string;
  confidence: number;
  review_state: ReviewState;
  created_by_agent: string;
  model_id: string;
  prompt_version: string;
  run_id: string;
  provenance: Provenance;
}

// Obligation as returned inside the lineage hierarchy -- carries its own
// mappings/gaps nested, matching GET /api/v1/lineage/{run_id}'s response
// shape (src/api/main.py's get_lineage).
export interface LineageObligation extends Obligation {
  mappings: Mapping[];
  gaps: Gap[];
}

export interface LineageResponse {
  run_id: string;
  obligations: LineageObligation[];
}

// Mirrors src/core/schemas.py's ClauseChange, as returned (nested inside
// text_diff) by POST /api/v1/changes/diff.
export type ChangeType = "added" | "removed" | "amended" | "renumbered" | "unchanged";
export type Materiality = "high" | "medium" | "low" | "editorial";

export interface ClauseChange {
  id: string;
  from_version_id: string;
  to_version_id: string;
  old_clause_id: string | null;
  new_clause_id: string | null;
  change_type: ChangeType;
  materiality: Materiality | null;
  text_diff: {
    old_span?: string;
    new_span?: string;
    affected_obligation_id?: string;
    affected_obligation_ids?: string[];
    breaks_control?: boolean;
    obligation_delta?: string;
    new_obligation_ids?: string[];
    new_gap_ids?: string[];
    gap_id?: string | null;
    run_id?: string;
  } | null;
  rationale: string | null;
}

// Delta gap: a GapFinding produced by re-running mapping/audit against an
// amended or newly added obligation (src/agents/change_watcher_agent.py) —
// same shape the main audit pipeline produces, trimmed to what the diff
// panel actually displays.
export interface DeltaGap {
  id: string;
  obligation_id: string;
  gap_class: string;
  narrative: string;
  risk_band: string;
  risk_score: number;
}

// ============ API request/response contracts ============

// GET /api/v1/bank_entities -- backs the bank-profile dropdown.
export interface BankEntitySummary {
  id: string;
  name: string;
}

export interface AuditRequest {
  regulation_text: string;
  policy_text: string;
  bank_profile_id: string;
}

export interface AuditResponse {
  run_id: string;
  status: string;
  obligations_count: number;
  controls_count: number;
  applicable_obligations_count: number;
  mappings_count: number;
  gaps_count: number;
  remediations_count: number;
}

export interface ChangeDiffRequest {
  run_id: string;
  new_regulation_text: string;
}

export interface ChangeDiffResponse {
  diff_run_id: string;
  from_version_id: string;
  to_version_id: string;
  changes: ClauseChange[];
  new_obligations_count: number;
  gaps: DeltaGap[];
  gaps_count: number;
}

// Cross-regulation intelligence + regulatory contradiction detection --
// POST /api/v1/obligations/relations.
export type RelationType = "unrelated" | "overlaps" | "conflicts_with" | "supersedes" | "implements";

export interface ObligationRelationGroupRequest {
  run_id: string;
  regulator: string;
}

export interface ObligationRelationsRequest {
  groups: ObligationRelationGroupRequest[];
}

export interface ObligationRelation {
  id: string;
  obligation_a: string;
  obligation_b: string;
  relation_type: RelationType;
  dimension: string | null;
  rationale: string;
  severity: string | null;
  resolution_hint: string | null;
  confidence: number;
  obligation_a_text: string;
  obligation_b_text: string;
}

export interface ObligationRelationsResponse {
  obligations_compared_count: number;
  relations: ObligationRelation[];
  relations_count: number;
}

// ============ UI-only types ============

export type NodeKind = "obligation" | "control" | "gap" | "remediation";

// What LineageGraph hands back on node click -- the raw entity plus which
// kind it is, so ProvenanceInspector can render kind-specific fields.
export interface SelectedNode {
  kind: NodeKind;
  data: Obligation | Control | Gap | Remediation;
}
