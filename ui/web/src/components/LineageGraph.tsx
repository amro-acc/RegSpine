import { useMemo, useCallback } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  Handle,
  Position,
  type Node,
  type Edge,
  type NodeProps,
  type NodeTypes,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type {
  LineageResponse,
  NodeKind,
  Obligation,
  Control,
  Gap,
  Remediation,
  SelectedNode,
} from "../types";

// Left-to-right DAG, one fixed x-offset per pipeline stage (traceability
// graph: Regulations -> Obligations -> Controls -> Gaps, plus Remediations
// as the terminal stage). y is staggered per-column below so nodes in the
// same stage don't overlap.
const COLUMN_X: Record<NodeKind, number> = {
  obligation: 0,
  control: 320,
  gap: 680,
  remediation: 1040,
};
const ROW_HEIGHT = 150;

interface Badge {
  text: string;
  className: string;
}

interface TraceNodeData extends Record<string, unknown> {
  kind: NodeKind;
  title: string;
  subtitle: string;
  badges: Badge[];
  entity: Obligation | Control | Gap | Remediation;
}

const KIND_STYLES: Record<NodeKind, { border: string; header: string }> = {
  obligation: { border: "border-blue-500", header: "bg-blue-500" },
  control: { border: "border-green-500", header: "bg-green-500" },
  gap: { border: "border-red-500", header: "bg-red-500" },
  remediation: { border: "border-purple-500", header: "bg-purple-500" },
};

function badgeClass(color: "gray" | "green" | "red" | "amber" | "blue" | "purple"): string {
  const map: Record<string, string> = {
    gray: "bg-gray-100 text-gray-700",
    green: "bg-green-100 text-green-800",
    red: "bg-red-100 text-red-800",
    amber: "bg-amber-100 text-amber-800",
    blue: "bg-blue-100 text-blue-800",
    purple: "bg-purple-100 text-purple-800",
  };
  return `px-1.5 py-0.5 rounded text-[10px] font-medium ${map[color]}`;
}

function TraceNode({ data }: NodeProps<Node<TraceNodeData>>) {
  const style = KIND_STYLES[data.kind];
  return (
    <div className={`w-64 rounded-md border-2 bg-white shadow-sm ${style.border}`}>
      <Handle type="target" position={Position.Left} />
      <div className={`rounded-t-sm px-2 py-1 text-[11px] font-semibold uppercase text-white ${style.header}`}>
        {data.kind}
      </div>
      <div className="px-2 py-2">
        <div className="line-clamp-2 text-sm font-medium text-gray-900">{data.title}</div>
        {data.subtitle && <div className="mt-0.5 line-clamp-1 text-xs text-gray-500">{data.subtitle}</div>}
        <div className="mt-2 flex flex-wrap gap-1">
          {data.badges.map((badge, i) => (
            <span key={i} className={badge.className}>
              {badge.text}
            </span>
          ))}
        </div>
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes: NodeTypes = { trace: TraceNode };

function riskBadgeColor(band: string): "red" | "amber" {
  return band === "CRITICAL" || band === "HIGH" ? "red" : "amber";
}

function reviewStateBadge(reviewState: string): Badge {
  const color = reviewState === "accepted" ? "green" : reviewState === "rejected" ? "red" : "gray";
  return { text: reviewState, className: badgeClass(color) };
}

function buildGraph(lineage: LineageResponse): { nodes: Node<TraceNodeData>[]; edges: Edge[] } {
  const nodes: Node<TraceNodeData>[] = [];
  const edges: Edge[] = [];
  const columnCounts: Record<NodeKind, number> = { obligation: 0, control: 0, gap: 0, remediation: 0 };
  const seenControlNodeIds = new Set<string>();

  const nextPosition = (kind: NodeKind) => {
    const y = columnCounts[kind] * ROW_HEIGHT;
    columnCounts[kind] += 1;
    return { x: COLUMN_X[kind], y };
  };

  for (const obligation of lineage.obligations) {
    const obligationNodeId = `obligation-${obligation.id}`;
    nodes.push({
      id: obligationNodeId,
      type: "trace",
      position: nextPosition("obligation"),
      data: {
        kind: "obligation",
        title: obligation.obligation_text,
        subtitle: obligation.obligation_type,
        badges: [reviewStateBadge(obligation.review_state), { text: obligation.modality, className: badgeClass("blue") }],
        entity: obligation,
      },
    });

    const controlNodeIdsForThisObligation: string[] = [];

    for (const mapping of obligation.mappings) {
      if (!mapping.control) continue;
      const controlNodeId = `control-${mapping.control_id}-${obligation.id}`;
      controlNodeIdsForThisObligation.push(controlNodeId);

      if (!seenControlNodeIds.has(controlNodeId)) {
        seenControlNodeIds.add(controlNodeId);
        nodes.push({
          id: controlNodeId,
          type: "trace",
          position: nextPosition("control"),
          data: {
            kind: "control",
            title: mapping.control.title,
            subtitle: mapping.control.control_ref,
            badges: [
              reviewStateBadge(mapping.control.review_state),
              { text: `coverage: ${mapping.coverage_level}`, className: badgeClass(mapping.coverage_level === "full" ? "green" : mapping.coverage_level === "partial" ? "amber" : "red") },
            ],
            entity: mapping.control,
          },
        });
      }

      edges.push({
        id: `edge-${obligationNodeId}-${controlNodeId}`,
        source: obligationNodeId,
        target: controlNodeId,
        label: mapping.coverage_level,
      });
    }

    for (const gap of obligation.gaps) {
      const gapNodeId = `gap-${gap.id}`;
      nodes.push({
        id: gapNodeId,
        type: "trace",
        position: nextPosition("gap"),
        data: {
          kind: "gap",
          title: gap.gap_class,
          subtitle: gap.narrative,
          badges: [
            { text: gap.status, className: badgeClass(gap.status === "disputed" ? "red" : "gray") },
            { text: gap.risk_band, className: badgeClass(riskBadgeColor(gap.risk_band)) },
          ],
          entity: gap,
        },
      });

      if (controlNodeIdsForThisObligation.length > 0) {
        for (const controlNodeId of controlNodeIdsForThisObligation) {
          edges.push({ id: `edge-${controlNodeId}-${gapNodeId}`, source: controlNodeId, target: gapNodeId });
        }
      } else {
        // no_control gap -- nothing to route through, connect straight from
        // the obligation (matches audit_agent.py's mapping=None handling).
        edges.push({ id: `edge-${obligationNodeId}-${gapNodeId}`, source: obligationNodeId, target: gapNodeId });
      }

      for (const remediation of gap.remediations) {
        const remediationNodeId = `remediation-${remediation.id}`;
        nodes.push({
          id: remediationNodeId,
          type: "trace",
          position: nextPosition("remediation"),
          data: {
            kind: "remediation",
            title: remediation.action,
            subtitle: remediation.owner_role ?? "",
            badges: [
              { text: remediation.status, className: badgeClass("gray") },
              { text: remediation.action_type ?? "OTHER", className: badgeClass("purple") },
            ],
            entity: remediation,
          },
        });
        edges.push({ id: `edge-${gapNodeId}-${remediationNodeId}`, source: gapNodeId, target: remediationNodeId });
      }
    }
  }

  return { nodes, edges };
}

interface LineageGraphProps {
  lineage: LineageResponse | null;
  onNodeSelect: (selected: SelectedNode) => void;
}

export default function LineageGraph({ lineage, onNodeSelect }: LineageGraphProps) {
  const { nodes, edges } = useMemo(
    () => (lineage ? buildGraph(lineage) : { nodes: [], edges: [] }),
    [lineage]
  );

  const handleNodeClick = useCallback(
    (_event: React.MouseEvent, node: Node<TraceNodeData>) => {
      onNodeSelect({ kind: node.data.kind, data: node.data.entity });
    },
    [onNodeSelect]
  );

  if (!lineage) {
    return (
      <div className="flex h-full w-full items-center justify-center text-sm text-gray-400">
        Run a compliance audit to see the traceability graph.
      </div>
    );
  }

  return (
    <div className="h-full w-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={handleNodeClick}
        fitView
        minZoom={0.1}
      >
        <Background />
        <Controls />
        <MiniMap pannable zoomable />
      </ReactFlow>
    </div>
  );
}
