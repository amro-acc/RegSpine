import { GitMerge, ShieldAlert, ArrowRightLeft, Link2 } from "lucide-react";
import type { ObligationRelation, RelationType } from "../types";

interface ObligationRelationsPanelProps {
  obligationsComparedCount: number;
  relations: ObligationRelation[];
}

const RELATION_TYPE_STYLE: Record<Exclude<RelationType, "unrelated">, { icon: typeof GitMerge; className: string }> = {
  overlaps: { icon: GitMerge, className: "bg-blue-100 text-blue-800" },
  conflicts_with: { icon: ShieldAlert, className: "bg-red-100 text-red-800" },
  supersedes: { icon: ArrowRightLeft, className: "bg-amber-100 text-amber-800" },
  implements: { icon: Link2, className: "bg-green-100 text-green-800" },
};

const SEVERITY_STYLE: Record<string, string> = {
  high: "bg-red-100 text-red-800",
  medium: "bg-amber-100 text-amber-800",
  low: "bg-blue-100 text-blue-800",
};

function RelationCard({ relation }: { relation: ObligationRelation }) {
  // "unrelated" relations are filtered out server-side (ObligationRelationAgent.compare
  // returns None for them) -- every relation reaching this panel is a real finding.
  const { icon: Icon, className } = RELATION_TYPE_STYLE[relation.relation_type as Exclude<RelationType, "unrelated">];
  const isConflict = relation.relation_type === "conflicts_with";

  return (
    <div className={`rounded border p-2 text-xs ${isConflict ? "border-red-300 bg-red-50" : "border-gray-200 bg-white"}`}>
      <div className="flex items-center justify-between gap-1.5">
        <span className={`flex items-center gap-1 rounded px-1.5 py-0.5 font-semibold uppercase ${className}`}>
          <Icon size={11} />
          {relation.relation_type.replace(/_/g, " ")}
        </span>
        {relation.severity && (
          <span className={`rounded px-1.5 py-0.5 font-medium ${SEVERITY_STYLE[relation.severity] ?? "bg-gray-100 text-gray-700"}`}>
            {relation.severity}
          </span>
        )}
      </div>

      {relation.dimension && <div className="mt-1.5 font-medium text-gray-800">{relation.dimension}</div>}

      <div className="mt-1.5 space-y-0.5 text-gray-500">
        <div className="truncate" title={relation.obligation_a_text}>A: {relation.obligation_a_text}</div>
        <div className="truncate" title={relation.obligation_b_text}>B: {relation.obligation_b_text}</div>
      </div>

      <div className="mt-1.5 text-gray-700">{relation.rationale}</div>

      {relation.resolution_hint && (
        <div className="mt-1.5 italic text-gray-500">Suggested: {relation.resolution_hint}</div>
      )}
    </div>
  );
}

export default function ObligationRelationsPanel({ obligationsComparedCount, relations }: ObligationRelationsPanelProps) {
  if (relations.length === 0) {
    return (
      <div className="text-xs text-gray-400">
        No overlaps, conflicts, or dependencies found across {obligationsComparedCount} compared obligations.
      </div>
    );
  }

  const conflictCount = relations.filter((r) => r.relation_type === "conflicts_with").length;

  return (
    <div className="space-y-2">
      <div className="text-xs text-gray-600">
        {relations.length} relation{relations.length === 1 ? "" : "s"} found across {obligationsComparedCount} obligations
        {conflictCount > 0 && <span className="font-semibold text-red-700"> · {conflictCount} conflict(s)</span>}
      </div>

      <div className="max-h-72 space-y-2 overflow-y-auto">
        {relations.map((relation) => (
          <RelationCard key={relation.id} relation={relation} />
        ))}
      </div>
    </div>
  );
}
