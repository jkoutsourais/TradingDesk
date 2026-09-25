import { GitCommitIcon } from "@primer/octicons-react";
import { Background, type Edge, Handle, type Node, Position, ReactFlow } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useEffect, useMemo, useState } from "react";

import { getJson } from "../api";
import { time } from "../format";

interface Envelope {
  id: string;
  kind: string;
  status: string;
  error: string | null;
  produced_by: string;
  created_at: string;
  model: string | null;
  prompt_version: string | null;
  parents: string[];
  [field: string]: unknown;
}

interface LineageEntry {
  depth: number;
  artifact: Envelope;
}

const ENVELOPE = new Set([
  "id", "kind", "schema_version", "status", "error", "shift_id", "produced_by", "parents",
  "created_at", "model", "prompt_version", "runtime_ms", "tokens_in", "tokens_out",
]);

/** A one-line description of an artifact from its kind-specific fields. */
export function summarize(artifact: Envelope): string {
  const pick = (...names: string[]) =>
    names.map((n) => artifact[n]).find((v) => typeof v === "string" && v.length > 0) as string | undefined;
  const payload = artifact.payload as Record<string, unknown> | undefined;
  const headline = payload && (payload.headline ?? payload.title ?? payload.form);
  return (
    pick("statement", "summary", "driver", "text", "subject", "instrument", "rationale", "reason", "verdict") ??
    (typeof headline === "string" ? headline : "") ??
    ""
  );
}

function Fields({ artifact }: { artifact: Envelope }) {
  const entries = Object.entries(artifact).filter(([key]) => !ENVELOPE.has(key));
  return (
    <div className="scroll">
      <table>
        <tbody>
          {entries.map(([key, value]) => (
            <tr key={key}>
              <td className="muted mono" style={{ width: 180 }}>{key}</td>
              <td>
                {typeof value === "object" && value !== null ? (
                  <pre className="mono" style={{ margin: 0, whiteSpace: "pre-wrap" }}>{JSON.stringify(value, null, 2)}</pre>
                ) : (
                  String(value)
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const GRAPH_COL = 230;
const GRAPH_ROW = 64;
const GRAPH_MAX_PER_DEPTH = 12;

function GraphNode({ data }: { data: Envelope }) {
  return (
    <a
      href={`#/trace/${data.id}`}
      title={summarize(data)}
      style={{
        display: "block",
        width: GRAPH_COL - 40,
        padding: "4px 8px",
        border: `1px solid ${data.status === "failed" ? "var(--borderColor-danger-emphasis)" : "var(--borderColor-default)"}`,
        borderRadius: 6,
        background: "var(--bgColor-default)",
        color: "var(--fgColor-default)",
        textDecoration: "none",
        fontSize: 12,
      }}
    >
      <div>
        <b>{data.kind}</b> <span className="muted">{data.produced_by}</span>
      </div>
      <div style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{summarize(data) || "-"}</div>
      <Handle type="target" position={Position.Right} style={{ opacity: 0 }} isConnectable={false} />
      <Handle type="source" position={Position.Left} style={{ opacity: 0 }} isConnectable={false} />
    </a>
  );
}

const graphTypes = { artifact: GraphNode };

/** Root on the right, ancestors to the left by depth; edges run from parent to child. */
function lineageGraph(entries: LineageEntry[]): { nodes: Node[]; edges: Edge[]; height: number } {
  const byDepth = new Map<number, Envelope[]>();
  for (const { depth, artifact } of entries) {
    const list = byDepth.get(depth) ?? [];
    if (list.length < GRAPH_MAX_PER_DEPTH) list.push(artifact);
    byDepth.set(depth, list);
  }
  const maxDepth = Math.max(...byDepth.keys());
  const shown = new Set<string>();
  const nodes: Node[] = [];
  let tallest = 1;
  for (const [depth, artifacts] of byDepth) {
    tallest = Math.max(tallest, artifacts.length);
    artifacts.forEach((artifact, i) => {
      shown.add(artifact.id);
      nodes.push({
        id: artifact.id,
        type: "artifact",
        position: { x: (maxDepth - depth) * GRAPH_COL, y: i * GRAPH_ROW },
        data: artifact as unknown as Record<string, unknown>,
        draggable: false,
      });
    });
  }
  const edges: Edge[] = [];
  for (const { artifact } of entries) {
    if (!shown.has(artifact.id)) continue;
    for (const parent of artifact.parents) {
      if (shown.has(parent)) edges.push({ id: `${parent}-${artifact.id}`, source: parent, target: artifact.id });
    }
  }
  return { nodes, edges, height: tallest * GRAPH_ROW + 40 };
}

export function Trace({ id }: { id?: string }) {
  const [lineage, setLineage] = useState<LineageEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState(id ?? "");

  useEffect(() => {
    if (!id) return;
    setLineage(null);
    getJson<LineageEntry[]>(`/api/lineage/${id}`)
      .then((entries) => {
        setLineage(entries);
        setError(null);
      })
      .catch((reason: unknown) => setError(String(reason)));
  }, [id]);

  const root = lineage?.find((e) => e.depth === 0)?.artifact;
  const graph = useMemo(() => (lineage && lineage.length > 1 ? lineageGraph(lineage) : null), [lineage]);
  return (
    <div className="content">
      <form
        className="box box-body"
        style={{ display: "flex", gap: 8 }}
        onSubmit={(event) => {
          event.preventDefault();
          window.location.hash = `#/trace/${input.trim()}`;
        }}
      >
        <input className="input mono" placeholder="Artifact id" value={input} onChange={(e) => setInput(e.target.value)} />
        <button className="btn" type="submit">Trace</button>
      </form>
      {error && <div className="box"><div className="empty">{error}</div></div>}
      {!id && <div className="muted">Open any card's trace, or paste an artifact id, to walk it back to raw data.</div>}
      {root && (
        <div className="box">
          <div className="box-header">
            <span className="label accent">{root.kind}</span> {root.produced_by}
            <span className="spacer" />
            <span className="muted">{time(root.created_at)}</span>
          </div>
          {root.status === "failed" && <div className="box-body neg">{root.error}</div>}
          <Fields artifact={root} />
        </div>
      )}
      {graph && (
        <div className="box">
          <div className="box-header">Lineage graph</div>
          <div style={{ height: Math.min(graph.height, 700) }}>
            <ReactFlow nodes={graph.nodes} edges={graph.edges} nodeTypes={graphTypes} fitView nodesConnectable={false} panOnScroll zoomOnScroll={false} minZoom={0.2}>
              <Background color="var(--borderColor-muted)" />
            </ReactFlow>
          </div>
        </div>
      )}
      {lineage && lineage.length > 1 && (
        <div className="box">
          <div className="box-header">
            <GitCommitIcon /> Lineage, {lineage.length - 1} ancestors
          </div>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th className="num">Depth</th>
                  <th>Kind</th>
                  <th>Produced by</th>
                  <th>Summary</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {lineage
                  .filter((e) => e.depth > 0)
                  .map(({ depth, artifact }) => (
                    <tr key={artifact.id}>
                      <td className="num">{depth}</td>
                      <td>
                        <a href={`#/trace/${artifact.id}`}>{artifact.kind}</a>
                      </td>
                      <td className="mono">{artifact.produced_by}</td>
                      <td>{summarize(artifact)}</td>
                      <td>{time(artifact.created_at)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
