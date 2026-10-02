import { useState, useCallback, useEffect } from "react";
import { Play, Sparkles, ShieldCheck, Loader2, AlertTriangle, GitCompare, Scale, Plus, X, ClipboardCheck } from "lucide-react";
import LineageGraph from "./components/LineageGraph";
import ProvenanceInspector from "./components/ProvenanceInspector";
import ChangeDiffPanel from "./components/ChangeDiffPanel";
import ObligationRelationsPanel from "./components/ObligationRelationsPanel";
import ReviewQueuePanel from "./components/ReviewQueuePanel";
import {
  runAudit,
  getLineage,
  diffChanges,
  findObligationRelations,
  listBankEntities,
  listReviewQueue,
  submitReview,
} from "./api";
import type {
  AuditResponse,
  BankEntitySummary,
  ChangeDiffResponse,
  LineageResponse,
  ObligationRelationGroupRequest,
  ObligationRelationsResponse,
  ReviewQueueItem,
  ReviewSubmission,
  SelectedNode,
} from "./types";

type RunStatus = "idle" | "running" | "completed" | "failed";

// Sample DORA (ICT incident reporting) + an internal control that only
// partially meets it -- deliberately chosen so a real audit run produces a
// partial_coverage gap and a remediation, not a full-coverage no-op.
// bank_profile_id is populated from GET /api/v1/bank_entities (see
// bankEntities state below) rather than hardcoded here -- it must be a
// real bank_entities.id row already seeded in Supabase
// (scripts/seed_supabase.py), which is environment-specific.
const SAMPLE_REGULATION_TEXT =
  "Financial entities shall report major ICT-related incidents to the competent authority " +
  "within 24 hours of detection, in accordance with Article 19 of Regulation (EU) 2022/2554 (DORA).";
const SAMPLE_POLICY_TEXT =
  "Control INC-07: the bank's ICT Incident Response team logs and triages security incidents, " +
  "escalating major incidents to senior management within 48 hours of detection.";

const MODEL_BADGES = [
  { role: "EXTRACTOR", model: "gpt-5.1" },
  { role: "REASONER", model: "gpt-5.1" },
  { role: "JUDGE", model: "dynamic (cross-family) — gemini-3.8-flash today" },
];

const STATUS_STYLES: Record<RunStatus, string> = {
  idle: "bg-gray-100 text-gray-600",
  running: "bg-amber-100 text-amber-800",
  completed: "bg-green-100 text-green-800",
  failed: "bg-red-100 text-red-800",
};

// Backend error details (src/api/main.py's HTTPException(detail=...)) are
// often long and can embed technical text (a stringified exception,
// occasionally a nested Python dict repr from an upstream API error) --
// wrapping/scrolling here keeps that readable instead of overflowing the
// 320px sidebar or getting silently clipped.
function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-1.5 rounded border border-red-200 bg-red-50 p-2 text-xs text-red-700">
      <AlertTriangle size={14} className="mt-0.5 flex-shrink-0" />
      <span className="max-h-40 overflow-y-auto whitespace-pre-wrap break-words">{message}</span>
    </div>
  );
}

function App() {
  const [regulationText, setRegulationText] = useState(SAMPLE_REGULATION_TEXT);
  const [policyText, setPolicyText] = useState(SAMPLE_POLICY_TEXT);
  const [bankProfileId, setBankProfileId] = useState("");
  const [bankEntities, setBankEntities] = useState<BankEntitySummary[]>([]);

  const [runStatus, setRunStatus] = useState<RunStatus>("idle");
  const [auditResponse, setAuditResponse] = useState<AuditResponse | null>(null);
  const [lineage, setLineage] = useState<LineageResponse | null>(null);
  const [selectedNode, setSelectedNode] = useState<SelectedNode | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Diffs a pasted "v2" regulation text against the obligations/mappings
  // already produced by the run above, via POST /api/v1/changes/diff.
  const [newRegulationText, setNewRegulationText] = useState("");
  const [diffStatus, setDiffStatus] = useState<RunStatus>("idle");
  const [diffResponse, setDiffResponse] = useState<ChangeDiffResponse | null>(null);
  const [diffError, setDiffError] = useState<string | null>(null);

  // Cross-regulation intelligence: surfaces overlaps and contradictions
  // between obligations across separate audit runs, via
  // POST /api/v1/obligations/relations.
  // Each group is a prior audit run (run_id) labeled with the regulator it
  // came from; the backend only compares obligations across groups, never
  // within one, so at least 2 groups are required.
  const [relationGroups, setRelationGroups] = useState<ObligationRelationGroupRequest[]>([
    { run_id: "", regulator: "" },
    { run_id: "", regulator: "" },
  ]);
  const [relationsStatus, setRelationsStatus] = useState<RunStatus>("idle");
  const [relationsResponse, setRelationsResponse] = useState<ObligationRelationsResponse | null>(null);
  const [relationsError, setRelationsError] = useState<string | null>(null);

  // HITL review queue, via GET/POST /api/v1/review -- gaps sitting in
  // review_state="needs_review" that a human needs to accept/reject/amend.
  // Global across runs (not scoped to the run currently shown), so it's
  // loaded on mount and refreshed after every submission, not tied to
  // runComplianceAudit the way the diff/relations panels are.
  const [reviewQueue, setReviewQueue] = useState<ReviewQueueItem[]>([]);
  const [reviewQueueError, setReviewQueueError] = useState<string | null>(null);
  const [submittingReviewId, setSubmittingReviewId] = useState<string | null>(null);

  const refreshReviewQueue = useCallback(() => {
    listReviewQueue()
      .then(setReviewQueue)
      .catch((err) => setReviewQueueError(err instanceof Error ? err.message : "Unknown error"));
  }, []);

  useEffect(() => {
    listBankEntities()
      .then(setBankEntities)
      .catch(() => setBankEntities([]));
    refreshReviewQueue();
  }, [refreshReviewQueue]);

  const handleSubmitReview = useCallback(
    async (submission: ReviewSubmission) => {
      setSubmittingReviewId(submission.gap_id);
      setReviewQueueError(null);
      try {
        await submitReview(submission);
        // Reviewed gap leaves the "needs_review" queue either way (accept,
        // reject, and amend all resolve it to "accepted"/"rejected"), and
        // the lineage graph's node badges (LineageGraph's reviewStateBadge)
        // need the same fresh data to show the new colour immediately.
        refreshReviewQueue();
        if (auditResponse) {
          const lineageResult = await getLineage(auditResponse.run_id);
          setLineage(lineageResult);
        }
      } catch (err) {
        setReviewQueueError(err instanceof Error ? err.message : "Unknown error");
      } finally {
        setSubmittingReviewId(null);
      }
    },
    [auditResponse, refreshReviewQueue]
  );

  const loadSampleScenario = useCallback(() => {
    setRegulationText(SAMPLE_REGULATION_TEXT);
    setPolicyText(SAMPLE_POLICY_TEXT);
    if (bankEntities.length > 0) {
      setBankProfileId(bankEntities[0].id);
    }
  }, [bankEntities]);

  const runComplianceAudit = useCallback(async () => {
    setRunStatus("running");
    setErrorMessage(null);
    setSelectedNode(null);
    try {
      const audit = await runAudit({
        regulation_text: regulationText,
        policy_text: policyText,
        bank_profile_id: bankProfileId,
      });
      setAuditResponse(audit);
      setRelationGroups((groups) => {
        const next = [...groups];
        next[0] = { ...next[0], run_id: audit.run_id };
        return next;
      });

      const lineageResult = await getLineage(audit.run_id);
      setLineage(lineageResult);
      setRunStatus("completed");
    } catch (err) {
      setRunStatus("failed");
      const message = err instanceof Error ? err.message : "Unknown error";
      setErrorMessage(message);
    }
  }, [regulationText, policyText, bankProfileId]);

  const runChangeDiff = useCallback(async () => {
    if (!auditResponse) return;
    setDiffStatus("running");
    setDiffError(null);
    try {
      const result = await diffChanges({
        run_id: auditResponse.run_id,
        new_regulation_text: newRegulationText,
      });
      setDiffResponse(result);
      setDiffStatus("completed");
    } catch (err) {
      setDiffStatus("failed");
      const message = err instanceof Error ? err.message : "Unknown error";
      setDiffError(message);
    }
  }, [auditResponse, newRegulationText]);

  const updateRelationGroup = useCallback((index: number, field: keyof ObligationRelationGroupRequest, value: string) => {
    setRelationGroups((groups) => groups.map((g, i) => (i === index ? { ...g, [field]: value } : g)));
  }, []);

  const addRelationGroup = useCallback(() => {
    setRelationGroups((groups) => [...groups, { run_id: "", regulator: "" }]);
  }, []);

  const removeRelationGroup = useCallback((index: number) => {
    setRelationGroups((groups) => groups.filter((_, i) => i !== index));
  }, []);

  const runObligationRelations = useCallback(async () => {
    const groups = relationGroups.filter((g) => g.run_id.trim() !== "" && g.regulator.trim() !== "");
    setRelationsStatus("running");
    setRelationsError(null);
    try {
      const result = await findObligationRelations({ groups });
      setRelationsResponse(result);
      setRelationsStatus("completed");
    } catch (err) {
      setRelationsStatus("failed");
      const message = err instanceof Error ? err.message : "Unknown error";
      setRelationsError(message);
    }
  }, [relationGroups]);

  const canRun = regulationText.trim() !== "" && policyText.trim() !== "" && bankProfileId.trim() !== "";
  const canDiff = auditResponse !== null && newRegulationText.trim() !== "";
  const canFindRelations =
    relationGroups.filter((g) => g.run_id.trim() !== "" && g.regulator.trim() !== "").length >= 2;

  return (
    <div className="flex h-screen w-screen flex-col bg-gray-50">
      {/* Header */}
      <header className="flex items-center justify-between border-b border-gray-200 bg-white px-4 py-2.5">
        <div className="flex items-center gap-2">
          <ShieldCheck className="text-indigo-600" size={22} />
          <h1 className="text-lg font-semibold text-gray-900">RegSpine</h1>
          <span className="text-xs text-gray-400">Autonomous regulatory compliance engine</span>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1.5">
            {MODEL_BADGES.map((badge) => (
              <span
                key={badge.role}
                title={badge.model}
                className="rounded bg-indigo-50 px-2 py-0.5 text-[11px] font-medium text-indigo-700"
              >
                {badge.role}: {badge.model}
              </span>
            ))}
          </div>
          <span className={`rounded px-2 py-1 text-xs font-semibold ${STATUS_STYLES[runStatus]}`}>
            {runStatus === "running" && <Loader2 className="mr-1 inline animate-spin" size={12} />}
            run: {runStatus}
            {auditResponse && runStatus === "completed" ? ` (${auditResponse.run_id.slice(0, 8)})` : ""}
          </span>
        </div>
      </header>

      <div className="flex flex-1 overflow-hidden">
        {/* Left sidebar */}
        <aside className="flex w-80 flex-shrink-0 flex-col gap-3 overflow-y-auto border-r border-gray-200 bg-white p-4">
          <button
            onClick={loadSampleScenario}
            className="flex items-center justify-center gap-1.5 rounded border border-indigo-200 bg-indigo-50 py-1.5 text-sm font-medium text-indigo-700 hover:bg-indigo-100"
          >
            <Sparkles size={14} />
            Load Sample DORA/AML Scenario
          </button>

          <label className="text-xs font-medium text-gray-600">
            Regulation text
            <textarea
              value={regulationText}
              onChange={(e) => setRegulationText(e.target.value)}
              rows={6}
              className="mt-1 w-full rounded border border-gray-300 p-2 text-sm focus:border-indigo-400 focus:outline-none"
              placeholder="Paste regulation clause text..."
            />
          </label>

          <label className="text-xs font-medium text-gray-600">
            Policy text
            <textarea
              value={policyText}
              onChange={(e) => setPolicyText(e.target.value)}
              rows={6}
              className="mt-1 w-full rounded border border-gray-300 p-2 text-sm focus:border-indigo-400 focus:outline-none"
              placeholder="Paste internal policy/control text..."
            />
          </label>

          <label className="text-xs font-medium text-gray-600">
            Bank profile
            <select
              value={bankProfileId}
              onChange={(e) => setBankProfileId(e.target.value)}
              className="mt-1 w-full rounded border border-gray-300 bg-white p-2 text-sm focus:border-indigo-400 focus:outline-none"
            >
              <option value="" disabled>
                {bankEntities.length === 0 ? "No banks seeded yet" : "Select a bank..."}
              </option>
              {bankEntities.map((entity) => (
                <option key={entity.id} value={entity.id}>
                  {entity.name}
                </option>
              ))}
            </select>
          </label>

          <button
            onClick={runComplianceAudit}
            disabled={!canRun || runStatus === "running"}
            className="mt-1 flex items-center justify-center gap-1.5 rounded bg-indigo-600 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-gray-300"
          >
            {runStatus === "running" ? <Loader2 className="animate-spin" size={16} /> : <Play size={16} />}
            Run Compliance Audit
          </button>

          {errorMessage && <ErrorBanner message={errorMessage} />}

          {auditResponse && (
            <div className="rounded border border-gray-200 bg-gray-50 p-2 text-xs text-gray-600">
              <div className="mb-1 font-semibold text-gray-800">Run summary</div>
              <div>Obligations: {auditResponse.obligations_count}</div>
              <div>Controls: {auditResponse.controls_count}</div>
              <div>Applicable obligations: {auditResponse.applicable_obligations_count}</div>
              <div>Mappings: {auditResponse.mappings_count}</div>
              <div>Gaps: {auditResponse.gaps_count}</div>
              <div>Remediations: {auditResponse.remediations_count}</div>
            </div>
          )}

          {auditResponse && (
            <div className="mt-2 border-t border-gray-200 pt-3">
              <div className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-gray-800">
                <GitCompare size={14} className="text-indigo-600" />
                Regulatory change intelligence
              </div>
              <label className="text-xs font-medium text-gray-600">
                New regulation text (e.g. DORA v2)
                <textarea
                  value={newRegulationText}
                  onChange={(e) => setNewRegulationText(e.target.value)}
                  rows={4}
                  className="mt-1 w-full rounded border border-gray-300 p-2 text-sm focus:border-indigo-400 focus:outline-none"
                  placeholder="Paste the updated regulation text to diff against the run above..."
                />
              </label>
              <button
                onClick={runChangeDiff}
                disabled={!canDiff || diffStatus === "running"}
                className="mt-1.5 flex w-full items-center justify-center gap-1.5 rounded bg-indigo-50 py-1.5 text-sm font-medium text-indigo-700 hover:bg-indigo-100 disabled:cursor-not-allowed disabled:bg-gray-100 disabled:text-gray-400"
              >
                {diffStatus === "running" ? <Loader2 className="animate-spin" size={14} /> : <GitCompare size={14} />}
                Diff against this run
              </button>

              {diffError && (
                <div className="mt-1.5">
                  <ErrorBanner message={diffError} />
                </div>
              )}

              {diffResponse && (
                <div className="mt-2">
                  <ChangeDiffPanel
                    changes={diffResponse.changes}
                    newObligationsCount={diffResponse.new_obligations_count}
                    gaps={diffResponse.gaps}
                  />
                </div>
              )}
            </div>
          )}

          <div className="mt-2 border-t border-gray-200 pt-3">
            <div className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-gray-800">
              <Scale size={14} className="text-indigo-600" />
              Cross-regulation intelligence
            </div>
            <div className="space-y-1.5">
              {relationGroups.map((group, index) => (
                <div key={index} className="flex items-center gap-1">
                  <input
                    value={group.run_id}
                    onChange={(e) => updateRelationGroup(index, "run_id", e.target.value)}
                    placeholder="run_id"
                    className="w-1/2 rounded border border-gray-300 p-1.5 text-xs focus:border-indigo-400 focus:outline-none"
                  />
                  <input
                    value={group.regulator}
                    onChange={(e) => updateRelationGroup(index, "regulator", e.target.value)}
                    placeholder="regulator (e.g. DORA)"
                    className="w-1/2 rounded border border-gray-300 p-1.5 text-xs focus:border-indigo-400 focus:outline-none"
                  />
                  {relationGroups.length > 2 && (
                    <button
                      onClick={() => removeRelationGroup(index)}
                      className="flex-shrink-0 text-gray-400 hover:text-red-600"
                      aria-label="Remove group"
                    >
                      <X size={14} />
                    </button>
                  )}
                </div>
              ))}
            </div>
            <button
              onClick={addRelationGroup}
              className="mt-1.5 flex items-center gap-1 text-xs font-medium text-indigo-700 hover:text-indigo-900"
            >
              <Plus size={12} />
              Add another run
            </button>
            <button
              onClick={runObligationRelations}
              disabled={!canFindRelations || relationsStatus === "running"}
              className="mt-1.5 flex w-full items-center justify-center gap-1.5 rounded bg-indigo-50 py-1.5 text-sm font-medium text-indigo-700 hover:bg-indigo-100 disabled:cursor-not-allowed disabled:bg-gray-100 disabled:text-gray-400"
            >
              {relationsStatus === "running" ? <Loader2 className="animate-spin" size={14} /> : <Scale size={14} />}
              Find overlaps &amp; conflicts
            </button>

            {relationsError && (
              <div className="mt-1.5">
                <ErrorBanner message={relationsError} />
              </div>
            )}

            {relationsResponse && (
              <div className="mt-2">
                <ObligationRelationsPanel
                  obligationsComparedCount={relationsResponse.obligations_compared_count}
                  relations={relationsResponse.relations}
                />
              </div>
            )}
          </div>

          <div className="mt-2 border-t border-gray-200 pt-3">
            <div className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-gray-800">
              <ClipboardCheck size={14} className="text-indigo-600" />
              Human review queue
              {reviewQueue.length > 0 && (
                <span className="rounded-full bg-indigo-100 px-1.5 py-0.5 text-[10px] font-semibold text-indigo-700">
                  {reviewQueue.length}
                </span>
              )}
            </div>

            {reviewQueueError && (
              <div className="mb-1.5">
                <ErrorBanner message={reviewQueueError} />
              </div>
            )}

            <ReviewQueuePanel items={reviewQueue} submittingId={submittingReviewId} onSubmit={handleSubmitReview} />
          </div>
        </aside>

        {/* Main canvas */}
        <main className="relative flex-1">
          <LineageGraph lineage={lineage} onNodeSelect={setSelectedNode} />
          <ProvenanceInspector selected={selectedNode} onClose={() => setSelectedNode(null)} />
        </main>
      </div>
    </div>
  );
}

export default App;
