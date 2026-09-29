import { X, CheckCircle2, XCircle, FileText, Hash, Gauge } from "lucide-react";
import type { SelectedNode, Obligation, Control, Gap, Remediation } from "../types";

interface ProvenanceInspectorProps {
  selected: SelectedNode | null;
  onClose: () => void;
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  if (value === null || value === undefined || value === "") return null;
  return (
    <div className="py-1.5">
      <div className="text-[11px] font-medium uppercase tracking-wide text-gray-400">{label}</div>
      <div className="text-sm text-gray-800">{value}</div>
    </div>
  );
}

/** Prominent, distinct-background section proving zero-hallucination
 * grounding: exactly the file/page/snippet-hash/verbatim-quote/span-verified
 * combination the demo needs to show judges. */
function ProvenancePanel({ entity, kind }: { entity: SelectedNode["data"]; kind: SelectedNode["kind"] }) {
  const provenance = entity.provenance;
  const hasSpanVerification = kind === "obligation" || kind === "control";
  const verbatimQuote = (entity as Obligation | Control).verbatim_quote ?? null;
  const spanVerified = hasSpanVerification ? (entity as Obligation | Control).span_verified : null;
  const spanVerifyMethod = hasSpanVerification ? (entity as Obligation | Control).span_verify_method : null;

  return (
    <div className="mt-3 rounded-md border border-indigo-200 bg-indigo-50 p-3">
      <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-indigo-700">
        <FileText size={14} />
        Provenance & grounding
      </div>

      <div className="grid grid-cols-2 gap-2 text-sm">
        <div>
          <div className="text-[11px] text-indigo-500">Source file</div>
          <div className="truncate font-mono text-xs text-indigo-900">{provenance.source_file}</div>
        </div>
        <div>
          <div className="text-[11px] text-indigo-500">Page</div>
          <div className="font-mono text-xs text-indigo-900">{provenance.page_number}</div>
        </div>
      </div>

      <div className="mt-2 flex items-center gap-1.5">
        <Hash size={12} className="text-indigo-400" />
        <span className="truncate font-mono text-[11px] text-indigo-700" title={provenance.snippet_hash}>
          {provenance.snippet_hash}
        </span>
      </div>

      {verbatimQuote && (
        <blockquote className="mt-3 border-l-2 border-indigo-300 pl-2 text-sm italic text-indigo-900">
          &ldquo;{verbatimQuote}&rdquo;
        </blockquote>
      )}

      {hasSpanVerification && (
        <div className="mt-3 flex items-center gap-1.5">
          {spanVerified ? (
            <>
              <CheckCircle2 size={16} className="text-green-600" />
              <span className="text-sm font-medium text-green-700">
                Verified against source{spanVerifyMethod ? ` (${spanVerifyMethod})` : ""}
              </span>
            </>
          ) : (
            <>
              <XCircle size={16} className="text-red-600" />
              <span className="text-sm font-medium text-red-700">Not span-verified — needs review</span>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function ObligationDetails({ entity }: { entity: Obligation }) {
  return (
    <>
      <Field label="Obligation text" value={entity.obligation_text} />
      <Field label="Obligation type" value={entity.obligation_type} />
      <Field label="Modality" value={entity.modality} />
      <Field label="Actor" value={entity.actor} />
      <Field label="Trigger condition" value={entity.trigger_condition} />
      <Field label="Deadline" value={entity.deadline_spec} />
    </>
  );
}

function ControlDetails({ entity }: { entity: Control }) {
  return (
    <>
      <Field label="Title" value={entity.title} />
      <Field label="Control ref" value={entity.control_ref} />
      <Field label="Design description" value={entity.design_description} />
      <Field label="Owner" value={entity.owner} />
      <Field label="Control type" value={entity.control_type} />
      <Field label="Automation" value={entity.automation} />
      <Field label="Frequency" value={entity.frequency} />
    </>
  );
}

function GapDetails({ entity }: { entity: Gap }) {
  return (
    <>
      <Field label="Gap class" value={entity.gap_class} />
      <Field label="Narrative" value={entity.narrative} />
      <Field label="Risk band" value={entity.risk_band} />
      <Field label="Risk score" value={`${entity.risk_score} / 100`} />
      <Field
        label="Risk factors"
        value={
          <ul className="list-inside list-disc">
            {Object.entries(entity.risk_factors).map(([factor, score]) => (
              <li key={factor}>
                {factor.replace(/_/g, " ")}: {score}/5
              </li>
            ))}
          </ul>
        }
      />
      <Field label="Status" value={entity.status} />
    </>
  );
}

function RemediationDetails({ entity }: { entity: Remediation }) {
  return (
    <>
      <Field label="Action" value={entity.action} />
      <Field label="Action type" value={entity.action_type} />
      <Field label="Control design delta" value={entity.control_design_delta} />
      <Field label="Owner role" value={entity.owner_role} />
      <Field label="Effort estimate" value={entity.effort_estimate} />
      <Field label="Test plan" value={entity.test_plan} />
      <Field label="Monitoring metric" value={entity.monitoring_metric} />
      <Field label="Status" value={entity.status} />
    </>
  );
}

export default function ProvenanceInspector({ selected, onClose }: ProvenanceInspectorProps) {
  return (
    <div
      className={`absolute right-0 top-0 h-full w-96 transform border-l border-gray-200 bg-white shadow-xl transition-transform duration-200 ${
        selected ? "translate-x-0" : "translate-x-full"
      }`}
    >
      {selected && (
        <div className="flex h-full flex-col overflow-y-auto p-4">
          <div className="flex items-center justify-between">
            <span className="rounded bg-gray-100 px-2 py-0.5 text-xs font-semibold uppercase text-gray-600">
              {selected.kind}
            </span>
            <button onClick={onClose} aria-label="Close inspector" className="text-gray-400 hover:text-gray-700">
              <X size={18} />
            </button>
          </div>

          <div className="mt-3 flex items-center gap-1.5 text-xs text-gray-400">
            <Gauge size={13} />
            confidence: {(selected.data.confidence * 100).toFixed(0)}% · review_state: {selected.data.review_state} ·
            model: {selected.data.model_id}
          </div>

          <div className="mt-3 divide-y divide-gray-100">
            {selected.kind === "obligation" && <ObligationDetails entity={selected.data as Obligation} />}
            {selected.kind === "control" && <ControlDetails entity={selected.data as Control} />}
            {selected.kind === "gap" && <GapDetails entity={selected.data as Gap} />}
            {selected.kind === "remediation" && <RemediationDetails entity={selected.data as Remediation} />}
          </div>

          <ProvenancePanel entity={selected.data} kind={selected.kind} />
        </div>
      )}
    </div>
  );
}
