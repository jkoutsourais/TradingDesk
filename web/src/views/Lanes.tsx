import { Background, type Node, ReactFlow } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useMemo, useState } from "react";

import { baseTextStyle, Chart, token } from "../components/Chart";
import { count, duration, num, time } from "../format";
import { useApi } from "../hooks";

interface Card {
  id: string;
  lane: string;
  instrument: string;
  score: number;
  driver: string;
  stage: string;
  dropped: string | null;
  thesis_id: string | null;
}

interface LaneData {
  lane: string;
  health: string;
  candidates: number;
  stage_counts: Record<string, number>;
  cards: Card[];
  hidden: number;
  funnel: { stages: Record<string, number>; drops: Record<string, number> };
  metrics: {
    scored?: number;
    hit_rate?: number;
    avg_return?: number;
    avg_r?: number;
    model_calls?: number;
    tokens?: number;
    runtime_ms?: number;
  };
}

interface LanesData {
  selections: { id: string; created_at: string }[];
  selection: string | null;
  stages: string[];
  lanes: LaneData[];
}

const TITLES: Record<string, string> = {
  catalyst: "Catalyst",
  screen: "Screen",
  commodity: "Commodity",
  power_grid: "Power + grid",
  policy: "Policy",
  macro: "Macro",
  event_additions: "Event additions",
};
const STAGE_TITLES: Record<string, string> = {
  candidate: "Candidates",
  promoted: "Promoted",
  verified: "Verified",
  thesis: "Thesis",
  debated: "Debated",
  planned: "Planned",
  approved: "Approved",
  vetoed: "Vetoed",
};
const HEADER_W = 190;
const COL_W = 170;
const CARD_H = 58;
const ROW_GAP = 18;
const MAX_STACK = 4;

// Drops leave the pipeline at the stage where the selection stopped them.
const DROP_FROM: Record<string, string> = {
  held: "candidate",
  duplicate: "candidate",
  "below the research cut": "candidate",
  "an open thesis": "candidate",
  "no verified evidence": "promoted",
  "below the thesis cut": "verified",
};

function healthDot(state: string) {
  return state === "ok" || state === "idle" ? "ok" : state === "failing" ? "fail" : "warn";
}

function CardNode({ data }: { data: Card }) {
  const tone =
    data.stage === "approved" ? "var(--borderColor-success-emphasis)" : data.stage === "vetoed" ? "var(--borderColor-danger-emphasis)" : "var(--borderColor-default)";
  return (
    <a
      href={`#/trace/${data.thesis_id ?? data.id}`}
      title={data.dropped ? `Dropped: ${data.dropped}` : data.driver}
      style={{
        display: "block",
        width: COL_W - 14,
        height: CARD_H - 8,
        padding: "4px 8px",
        border: `1px solid ${tone}`,
        borderRadius: 6,
        background: "var(--bgColor-default)",
        color: data.dropped ? "var(--fgColor-muted)" : "var(--fgColor-default)",
        overflow: "hidden",
        textDecoration: "none",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between" }}>
        <b>{data.instrument}</b>
        <span className="num">{num(data.score)}</span>
      </div>
      <div style={{ fontSize: 11, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {data.dropped ? data.dropped : data.driver}
      </div>
    </a>
  );
}

function LaneHeader({ data }: { data: LaneData }) {
  const m = data.metrics;
  return (
    <div style={{ width: HEADER_W - 12, padding: "4px 8px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span className={`dot ${healthDot(data.health)}`} title={`sources ${data.health}`} />
        <b>{TITLES[data.lane] ?? data.lane}</b>
      </div>
      <div className="muted" style={{ fontSize: 11 }}>
        {count(data.candidates)} candidates
        {m.hit_rate !== undefined ? ` · hit ${num(m.hit_rate * 100, 0)}%` : ""}
      </div>
      {data.hidden > 0 && <div className="muted" style={{ fontSize: 11 }}>{data.hidden} lower-ranked not shown</div>}
    </div>
  );
}

function StageHeader({ data }: { data: { title: string; total: number } }) {
  return (
    <div style={{ width: COL_W - 14, fontWeight: 600, color: "var(--fgColor-muted)", fontSize: 12 }}>
      {data.title} <span className="num">{data.total}</span>
    </div>
  );
}

const nodeTypes = { card: CardNode, lane: LaneHeader, stage: StageHeader };

function boardNodes(data: LanesData): { nodes: Node[]; height: number } {
  const nodes: Node[] = [];
  data.stages.forEach((stage, i) => {
    const total = data.lanes.reduce((sum, lane) => sum + (lane.stage_counts[stage] ?? 0), 0);
    nodes.push({
      id: `stage-${stage}`,
      type: "stage",
      position: { x: HEADER_W + i * COL_W, y: 0 },
      data: { title: STAGE_TITLES[stage] ?? stage, total },
      draggable: false,
      selectable: false,
    });
  });
  let y = 28;
  for (const lane of data.lanes) {
    const byStage: Record<string, Card[]> = {};
    for (const card of lane.cards) (byStage[card.stage] ??= []).push(card);
    const tallest = Math.min(MAX_STACK, Math.max(1, ...Object.values(byStage).map((cards) => cards.length)));
    nodes.push({ id: `lane-${lane.lane}`, type: "lane", position: { x: 0, y }, data: lane as unknown as Record<string, unknown>, draggable: false, selectable: false });
    data.stages.forEach((stage, i) => {
      (byStage[stage] ?? []).slice(0, MAX_STACK).forEach((card, j) => {
        nodes.push({
          id: card.id,
          type: "card",
          position: { x: HEADER_W + i * COL_W, y: y + j * CARD_H },
          data: card as unknown as Record<string, unknown>,
          draggable: false,
        });
      });
    });
    y += tallest * CARD_H + ROW_GAP;
  }
  return { nodes, height: y };
}

function funnelOption(lane: LaneData) {
  const { stages, drops } = lane.funnel;
  const order = ["candidate", "promoted", "verified", "thesis", "debated", "planned"];
  const links: { source: string; target: string; value: number }[] = [];
  order.slice(1).forEach((stage, i) => {
    const value = stages[stage] ?? 0;
    const previous = order[i];
    if (value > 0 && previous) links.push({ source: STAGE_TITLES[previous] ?? previous, target: STAGE_TITLES[stage] ?? stage, value });
  });
  for (const end of ["approved", "vetoed"]) {
    const value = stages[end] ?? 0;
    if (value > 0) links.push({ source: "Planned", target: STAGE_TITLES[end] ?? end, value });
  }
  for (const [reason, value] of Object.entries(drops)) {
    const from = DROP_FROM[reason] ?? "candidate";
    links.push({ source: STAGE_TITLES[from] ?? from, target: `Dropped: ${reason}`, value });
  }
  const names = new Set(links.flatMap((l) => [l.source, l.target]));
  const accent = token("--fgColor-accent");
  const muted = token("--fgColor-muted");
  const success = token("--fgColor-success");
  const danger = token("--fgColor-danger");
  return {
    textStyle: baseTextStyle(),
    tooltip: { trigger: "item" },
    series: [
      {
        type: "sankey",
        left: 8,
        right: 160,
        nodeGap: 10,
        nodeWidth: 10,
        emphasis: { focus: "adjacency" },
        label: { color: token("--fgColor-default"), fontSize: 12 },
        lineStyle: { color: "source", opacity: 0.25 },
        data: [...names].map((name) => ({
          name,
          itemStyle: {
            color: name.startsWith("Dropped") ? muted : name === "Approved" ? success : name === "Vetoed" ? danger : accent,
            borderWidth: 0,
          },
        })),
        links,
      },
    ],
  };
}

type SortKey = "lane" | "candidates" | "scored" | "hit_rate" | "avg_return" | "avg_r" | "model_calls" | "tokens" | "runtime_ms";

export function Lanes() {
  const [selection, setSelection] = useState<string | null>(null);
  const path = selection ? `/api/lanes?selection=${selection}` : "/api/lanes";
  const { data, error } = useApi<LanesData>(path, ["idea_selection", "thesis", "debate_verdict", "risk_decision"]);
  const [funnelLane, setFunnelLane] = useState<string | null>(null);
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "candidates", desc: true });
  const board = useMemo(() => (data ? boardNodes(data) : null), [data]);

  if (error) return <div className="content"><div className="box"><div className="empty">{error}</div></div></div>;
  if (!data || !board) return <div className="content"><div className="empty">Loading</div></div>;
  if (!data.selection) return <div className="content"><div className="empty">No idea selection yet; the first appears after a post-market shift.</div></div>;

  const selections = [...data.selections].reverse();
  const index = selections.findIndex((s) => s.id === data.selection);
  const shown = data.lanes.find((l) => l.lane === funnelLane) ?? [...data.lanes].sort((a, b) => b.candidates - a.candidates)[0];
  const value = (lane: LaneData, key: SortKey): number | string =>
    key === "lane" ? lane.lane : key === "candidates" ? lane.candidates : (lane.metrics[key] ?? -Infinity);
  const rows = [...data.lanes].sort((a, b) => {
    const [x, y] = [value(a, sort.key), value(b, sort.key)];
    const order = x < y ? -1 : x > y ? 1 : 0;
    return sort.desc ? -order : order;
  });
  const header = (key: SortKey, title: string, numeric = true) => (
    <th className={numeric ? "num" : ""} style={{ cursor: "pointer" }} onClick={() => setSort({ key, desc: sort.key === key ? !sort.desc : true })}>
      {title}
      {sort.key === key ? (sort.desc ? " ↓" : " ↑") : ""}
    </th>
  );

  return (
    <div className="content">
      <div className="box">
        <div className="box-header">
          Shift
          <input
            type="range"
            min={0}
            max={Math.max(selections.length - 1, 0)}
            value={Math.max(index, 0)}
            onChange={(event) => setSelection(selections[Number(event.target.value)]?.id ?? null)}
            style={{ flex: 1, maxWidth: 360 }}
          />
          <span className="mono">{time(selections[index]?.created_at)}</span>
          <span className="muted">
            {index + 1} of {selections.length}
          </span>
        </div>
        <div style={{ height: Math.min(board.height + 40, 900) }}>
          <ReactFlow
            nodes={board.nodes}
            edges={[]}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.05 }}
            nodesConnectable={false}
            panOnScroll
            zoomOnScroll={false}
            minZoom={0.3}
            maxZoom={1.5}
          >
            <Background gap={COL_W} color="var(--borderColor-muted)" />
          </ReactFlow>
        </div>
      </div>
      <div className="grid-2">
        <div className="box">
          <div className="box-header">
            Funnel, last 30 days
            <span className="spacer" />
            <select className="input" style={{ width: "auto" }} value={shown?.lane} onChange={(e) => setFunnelLane(e.target.value)}>
              {data.lanes.map((l) => (
                <option key={l.lane} value={l.lane}>
                  {TITLES[l.lane] ?? l.lane}
                </option>
              ))}
            </select>
          </div>
          {shown && Object.keys(shown.funnel.stages).length ? <Chart option={funnelOption(shown)} height={320} /> : <div className="empty">No candidates in 30 days.</div>}
        </div>
        <div className="box">
          <div className="box-header">Lane metrics, 30 days (5-day scores)</div>
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  {header("lane", "Lane", false)}
                  {header("candidates", "Now")}
                  {header("scored", "Scored")}
                  {header("hit_rate", "Hit")}
                  {header("avg_return", "Avg ret")}
                  {header("avg_r", "Avg R")}
                  {header("model_calls", "Calls")}
                  {header("tokens", "Tokens")}
                  {header("runtime_ms", "Model time")}
                </tr>
              </thead>
              <tbody>
                {rows.map((lane) => (
                  <tr key={lane.lane}>
                    <td>
                      <span className={`dot ${healthDot(lane.health)}`} /> {TITLES[lane.lane] ?? lane.lane}
                    </td>
                    <td className="num">{count(lane.candidates)}</td>
                    <td className="num">{count(lane.metrics.scored)}</td>
                    <td className="num">{lane.metrics.hit_rate === undefined ? "-" : `${num(lane.metrics.hit_rate * 100, 0)}%`}</td>
                    <td className={`num ${(lane.metrics.avg_return ?? 0) < 0 ? "neg" : lane.metrics.avg_return ? "pos" : ""}`}>
                      {lane.metrics.avg_return === undefined ? "-" : `${num(lane.metrics.avg_return)}%`}
                    </td>
                    <td className="num">{num(lane.metrics.avg_r)}</td>
                    <td className="num">{count(lane.metrics.model_calls)}</td>
                    <td className="num">{count(lane.metrics.tokens)}</td>
                    <td className="num">{duration(lane.metrics.runtime_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
