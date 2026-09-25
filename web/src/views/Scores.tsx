import { useMemo } from "react";

import { baseTextStyle, Chart, token } from "../components/Chart";
import { count, num, time } from "../format";
import { useApi, useHashRoute } from "../hooks";

interface Group {
  dimension: string;
  value: string;
  count: number;
  hit_rate: number | null;
  avg_return: number | null;
  avg_r: number | null;
}

interface ScoresData {
  horizon: string;
  scored: number;
  dimensions: Record<string, Group[]>;
  recent: { subject_id: string; kind: string; attribution: Record<string, string>; return_pct: number; r_multiple: number | null; hit: boolean | null; first_hit: string | null; created_at: string }[];
}

const HORIZONS = ["1d", "5d", "20d", "exit"];
const TITLES: Record<string, string> = {
  lane: "Lane",
  persona: "Persona",
  verdict: "Judge verdict",
  tier: "Risk tier",
  instrument_class: "Instrument class",
  origin: "Origin",
  veto_reason: "Veto reason",
  decision: "Risk decision",
  rating: "Holding rating",
  structure: "Structure",
};

function hitChart(groups: Group[]) {
  const withHits = groups.filter((g) => g.hit_rate !== null);
  return {
    textStyle: baseTextStyle(),
    grid: { left: 110, right: 24, top: 8, bottom: 24 },
    tooltip: { trigger: "axis" },
    xAxis: { type: "value", max: 100, axisLabel: { formatter: "{value}%", color: token("--fgColor-muted") }, splitLine: { lineStyle: { color: token("--borderColor-muted") } } },
    yAxis: { type: "category", data: withHits.map((g) => g.value), axisLabel: { color: token("--fgColor-default") } },
    series: [{ type: "bar", data: withHits.map((g) => Math.round((g.hit_rate ?? 0) * 100)), itemStyle: { color: token("--fgColor-accent") }, barMaxWidth: 14 }],
  };
}

export function Scores() {
  const route = useHashRoute();
  const horizon = HORIZONS.includes(route[1] ?? "") ? (route[1] as string) : "5d";
  const { data, error } = useApi<ScoresData>(`/api/scores?horizon=${horizon}`, ["score"]);
  const lanes = useMemo(() => (data ? hitChart(data.dimensions.lane ?? []) : null), [data]);
  const personas = useMemo(() => (data ? hitChart(data.dimensions.persona ?? []) : null), [data]);
  if (error && !data) return <div className="content"><div className="empty">{error}</div></div>;
  if (!data) return <div className="content"><div className="empty">Loading</div></div>;
  return (
    <div className="content">
      <div className="tabs" style={{ position: "static", padding: 0 }}>
        {HORIZONS.map((h) => (
          <a key={h} href={`#/scores/${h}`} className={h === horizon ? "active" : ""}>{h}</a>
        ))}
        <span style={{ flex: 1 }} />
        <span className="muted" style={{ padding: 8 }}>{count(data.scored)} scores</span>
      </div>
      {data.scored === 0 && <div className="box"><div className="empty">Nothing scored at this horizon yet. Scores appear once the daily bars after each call exist.</div></div>}
      <div className="grid-2">
        {(data.dimensions.lane ?? []).length > 0 && lanes && (
          <div className="box"><div className="box-header">Hit rate by lane</div><Chart option={lanes} height={220} /></div>
        )}
        {(data.dimensions.persona ?? []).length > 0 && personas && (
          <div className="box"><div className="box-header">Hit rate by persona</div><Chart option={personas} height={220} /></div>
        )}
      </div>
      <div className="grid-2">
        {Object.entries(data.dimensions)
          .filter(([, groups]) => groups.length > 0)
          .map(([dimension, groups]) => (
            <div className="box" key={dimension}>
              <div className="box-header">By {(TITLES[dimension] ?? dimension).toLowerCase()}</div>
              <div className="scroll">
                <table>
                  <thead><tr><th>{TITLES[dimension] ?? dimension}</th><th className="num">Count</th><th className="num">Hit</th><th className="num">Avg return</th><th className="num">Avg R</th></tr></thead>
                  <tbody>
                    {groups.map((g) => (
                      <tr key={g.value}>
                        <td>{g.value}</td>
                        <td className="num">{count(g.count)}</td>
                        <td className="num">{g.hit_rate === null ? "-" : `${num(g.hit_rate * 100, 0)}%`}</td>
                        <td className={`num ${(g.avg_return ?? 0) >= 0 ? "pos" : "neg"}`}>{g.avg_return === null ? "-" : `${num(g.avg_return)}%`}</td>
                        <td className="num">{num(g.avg_r)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
      </div>
      {data.recent.length > 0 && (
        <div className="box">
          <div className="box-header">Latest scores</div>
          <div className="scroll">
            <table>
              <thead><tr><th>Subject</th><th>Attribution</th><th className="num">Return</th><th className="num">R</th><th>Hit</th><th>First hit</th><th>Scored</th></tr></thead>
              <tbody>
                {data.recent.map((s) => (
                  <tr key={`${s.subject_id}-${s.created_at}`}>
                    <td><a href={`#/trace/${s.subject_id}`}>{s.kind}</a></td>
                    <td className="muted">{Object.entries(s.attribution).map(([k, v]) => `${k} ${v}`).join(" · ")}</td>
                    <td className={`num ${s.return_pct >= 0 ? "pos" : "neg"}`}>{num(s.return_pct)}%</td>
                    <td className="num">{num(s.r_multiple)}</td>
                    <td>{s.hit === null ? "-" : s.hit ? <span className="pos">yes</span> : <span className="neg">no</span>}</td>
                    <td>{s.first_hit ?? "-"}</td>
                    <td>{time(s.created_at)}</td>
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
