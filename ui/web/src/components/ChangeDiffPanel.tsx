import { AlertTriangle, FilePlus2, FileMinus2, FileEdit, ShieldAlert } from "lucide-react";
import type { ClauseChange, ChangeType, DeltaGap, Materiality } from "../types";

interface ChangeDiffPanelProps {
  changes: ClauseChange[];
  newObligationsCount: number;
  gaps: DeltaGap[];
}

const RISK_BAND_STYLE: Record<string, string> = {
  CRITICAL: "bg-red-100 text-red-800",
  HIGH: "bg-orange-100 text-orange-800",
  MEDIUM: "bg-amber-100 text-amber-800",
  LOW: "bg-blue-100 text-blue-800",
};

function DeltaGapCard({ gap }: { gap: DeltaGap }) {
  return (
    <div className="rounded border border-red-300 bg-red-50 p-2 text-xs">
      <div className="flex items-center justify-between gap-1.5">
        <span className="flex items-center gap-1 font-semibold uppercase text-red-800">
          <ShieldAlert size={11} />
          {gap.gap_class.replace(/_/g, " ")}
        </span>
        <span className={`rounded px-1.5 py-0.5 font-medium ${RISK_BAND_STYLE[gap.risk_band] ?? "bg-gray-100 text-gray-700"}`}>
          {gap.risk_band}
        </span>
      </div>
      <div className="mt-1.5 text-gray-700">{gap.narrative}</div>
    </div>
  );
}

const CHANGE_TYPE_STYLE: Record<ChangeType, { icon: typeof FilePlus2; className: string }> = {
  added: { icon: FilePlus2, className: "bg-green-100 text-green-800" },
  removed: { icon: FileMinus2, className: "bg-gray-200 text-gray-700" },
  amended: { icon: FileEdit, className: "bg-amber-100 text-amber-800" },
  renumbered: { icon: FileEdit, className: "bg-blue-100 text-blue-800" },
  unchanged: { icon: FileEdit, className: "bg-gray-100 text-gray-500" },
};

const MATERIALITY_STYLE: Record<Materiality, string> = {
  high: "bg-red-100 text-red-800",
  medium: "bg-amber-100 text-amber-800",
  low: "bg-blue-100 text-blue-800",
  editorial: "bg-gray-100 text-gray-500",
};

function ChangeCard({ change }: { change: ClauseChange }) {
  const { icon: Icon, className } = CHANGE_TYPE_STYLE[change.change_type];
  const breaksControl = change.text_diff?.breaks_control === true;

  return (
    <div className={`rounded border p-2 text-xs ${breaksControl ? "border-red-300 bg-red-50" : "border-gray-200 bg-white"}`}>
      <div className="flex items-center justify-between gap-1.5">
        <span className={`flex items-center gap-1 rounded px-1.5 py-0.5 font-semibold uppercase ${className}`}>
          <Icon size={11} />
          {change.change_type}
        </span>
        {change.materiality && (
          <span className={`rounded px-1.5 py-0.5 font-medium ${MATERIALITY_STYLE[change.materiality]}`}>
            {change.materiality}
          </span>
        )}
      </div>

      {breaksControl && (
        <div className="mt-1.5 flex items-start gap-1 rounded bg-red-100 p-1.5 text-red-800">
          <AlertTriangle size={12} className="mt-0.5 flex-shrink-0" />
          <span className="font-medium">May break a previously compliant control</span>
        </div>
      )}

      {change.text_diff?.obligation_delta && (
        <div className="mt-1.5 text-gray-700">{change.text_diff.obligation_delta}</div>
      )}

      {(change.text_diff?.old_span || change.text_diff?.new_span) && (
        <div className="mt-1.5 space-y-0.5 font-mono text-[10px] text-gray-500">
          {change.text_diff.old_span && <div className="truncate">− {change.text_diff.old_span}</div>}
          {change.text_diff.new_span && <div className="truncate">+ {change.text_diff.new_span}</div>}
        </div>
      )}

      {change.rationale && <div className="mt-1.5 text-gray-500 italic">{change.rationale}</div>}
    </div>
  );
}

export default function ChangeDiffPanel({ changes, newObligationsCount, gaps }: ChangeDiffPanelProps) {
  if (changes.length === 0) {
    return <div className="text-xs text-gray-400">No textual differences detected against this run.</div>;
  }

  const breakingCount = changes.filter((c) => c.text_diff?.breaks_control === true).length;

  return (
    <div className="space-y-2">
      <div className="text-xs text-gray-600">
        {changes.length} change{changes.length === 1 ? "" : "s"} detected
        {breakingCount > 0 && <span className="font-semibold text-red-700"> · {breakingCount} may break coverage</span>}
        {newObligationsCount > 0 && <span> · {newObligationsCount} new obligation candidate(s)</span>}
        {gaps.length > 0 && <span className="font-semibold text-red-700"> · {gaps.length} newly detected gap(s)</span>}
      </div>

      {gaps.length > 0 && (
        <div className="space-y-1.5">
          <div className="text-xs font-semibold text-red-800">Newly detected compliance gaps</div>
          {gaps.map((gap) => (
            <DeltaGapCard key={gap.id} gap={gap} />
          ))}
        </div>
      )}

      <div className="max-h-72 space-y-2 overflow-y-auto">
        {changes.map((change) => (
          <ChangeCard key={change.id} change={change} />
        ))}
      </div>
    </div>
  );
}
