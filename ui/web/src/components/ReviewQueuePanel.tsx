import { useState } from "react";
import { CheckCircle2, XCircle, Edit3, Loader2 } from "lucide-react";
import type { ReviewQueueItem, ReviewSubmission, RiskBand } from "../types";

interface ReviewQueuePanelProps {
  items: ReviewQueueItem[];
  submittingId: string | null;
  onSubmit: (submission: ReviewSubmission) => void;
}

const RISK_BAND_STYLE: Record<string, string> = {
  CRITICAL: "bg-red-100 text-red-800",
  HIGH: "bg-orange-100 text-orange-800",
  MEDIUM: "bg-amber-100 text-amber-800",
  LOW: "bg-blue-100 text-blue-800",
};

const RISK_BANDS: RiskBand[] = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];

function ReviewCard({
  item,
  submitting,
  onSubmit,
}: {
  item: ReviewQueueItem;
  submitting: boolean;
  onSubmit: (submission: ReviewSubmission) => void;
}) {
  const [reviewer, setReviewer] = useState("");
  const [note, setNote] = useState("");
  const [amending, setAmending] = useState(false);
  const [amendedRiskBand, setAmendedRiskBand] = useState<RiskBand>(item.risk_band);
  const [amendedNarrative, setAmendedNarrative] = useState(item.narrative);

  const canSubmit = reviewer.trim() !== "" && !submitting;

  const submit = (action: "accept" | "reject" | "amend") => {
    if (!canSubmit) return;

    if (action === "amend") {
      const adjusted_fields: Record<string, string> = {};
      if (amendedRiskBand !== item.risk_band) adjusted_fields.risk_band = amendedRiskBand;
      if (amendedNarrative !== item.narrative) adjusted_fields.narrative = amendedNarrative;
      if (Object.keys(adjusted_fields).length === 0) return; // nothing actually changed, nothing to submit
      onSubmit({ gap_id: item.id, reviewer: reviewer.trim(), action, adjusted_fields, note: note.trim() || null });
      return;
    }

    onSubmit({ gap_id: item.id, reviewer: reviewer.trim(), action, note: note.trim() || null });
  };

  return (
    <div className="rounded border border-gray-200 bg-white p-2 text-xs">
      <div className="flex items-center justify-between gap-1.5">
        <span className="font-semibold uppercase text-gray-700">{item.gap_class.replace(/_/g, " ")}</span>
        <span className={`rounded px-1.5 py-0.5 font-medium ${RISK_BAND_STYLE[item.risk_band] ?? "bg-gray-100 text-gray-700"}`}>
          {item.risk_band} · {item.risk_score}
        </span>
      </div>

      <div className="mt-1.5 text-gray-700">{item.obligation_text}</div>
      <div className="mt-1 italic text-gray-500">{item.narrative}</div>
      <div className="mt-1 text-[10px] text-gray-400">confidence: {(item.confidence * 100).toFixed(0)}%</div>

      <input
        value={reviewer}
        onChange={(e) => setReviewer(e.target.value)}
        placeholder="Your name/email (required)"
        className="mt-2 w-full rounded border border-gray-300 p-1.5 text-xs focus:border-indigo-400 focus:outline-none"
      />
      <input
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Note (optional)"
        className="mt-1 w-full rounded border border-gray-300 p-1.5 text-xs focus:border-indigo-400 focus:outline-none"
      />

      {amending && (
        <div className="mt-1.5 space-y-1 rounded border border-indigo-100 bg-indigo-50 p-1.5">
          <label className="block text-[10px] font-medium text-gray-600">
            Adjusted risk band
            <select
              value={amendedRiskBand}
              onChange={(e) => setAmendedRiskBand(e.target.value as RiskBand)}
              className="mt-0.5 w-full rounded border border-gray-300 bg-white p-1 text-xs"
            >
              {RISK_BANDS.map((band) => (
                <option key={band} value={band}>
                  {band}
                </option>
              ))}
            </select>
          </label>
          <label className="block text-[10px] font-medium text-gray-600">
            Adjusted narrative
            <textarea
              value={amendedNarrative}
              onChange={(e) => setAmendedNarrative(e.target.value)}
              rows={2}
              className="mt-0.5 w-full rounded border border-gray-300 p-1 text-xs"
            />
          </label>
        </div>
      )}

      <div className="mt-1.5 flex gap-1">
        <button
          onClick={() => submit("accept")}
          disabled={!canSubmit}
          className="flex flex-1 items-center justify-center gap-1 rounded bg-green-50 py-1 font-medium text-green-700 hover:bg-green-100 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <CheckCircle2 size={12} /> Accept
        </button>
        <button
          onClick={() => submit("reject")}
          disabled={!canSubmit}
          className="flex flex-1 items-center justify-center gap-1 rounded bg-red-50 py-1 font-medium text-red-700 hover:bg-red-100 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <XCircle size={12} /> Reject
        </button>
        <button
          onClick={() => (amending ? submit("amend") : setAmending(true))}
          disabled={!canSubmit}
          className="flex flex-1 items-center justify-center gap-1 rounded bg-amber-50 py-1 font-medium text-amber-700 hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? <Loader2 size={12} className="animate-spin" /> : <Edit3 size={12} />}
          {amending ? "Submit amend" : "Amend"}
        </button>
      </div>
    </div>
  );
}

export default function ReviewQueuePanel({ items, submittingId, onSubmit }: ReviewQueuePanelProps) {
  if (items.length === 0) {
    return <div className="text-xs text-gray-400">No gaps are waiting for human review right now.</div>;
  }

  return (
    <div className="max-h-96 space-y-2 overflow-y-auto">
      {items.map((item) => (
        <ReviewCard key={item.id} item={item} submitting={submittingId === item.id} onSubmit={onSubmit} />
      ))}
    </div>
  );
}
