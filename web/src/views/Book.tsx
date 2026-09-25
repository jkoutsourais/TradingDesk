import { useState } from "react";

import { postJson, type RatingRow } from "../api";
import { money, num, RATING, time } from "../format";
import { useApi } from "../hooks";

interface BookData {
  holdings: {
    account_ref: string;
    source: string;
    as_of: string;
    net_liquidation: number | null;
    cash: number | null;
    positions: { symbol: string; contract: string; asset_class: string; quantity: number; mark_price: number | null; market_value: number | null; cost_basis: number | null }[];
  }[];
  ratings: RatingRow[];
  positions: {
    id: string;
    account_ref: string;
    symbol: string;
    contract: string;
    direction: string;
    state: string;
    quantity: string;
    avg_entry: string | null;
    avg_exit: string | null;
    realized_pnl: string;
    opened_at: string;
    closed_at: string | null;
    plan_id: string | null;
    thesis_id: string | null;
    link_status: string;
    exit: { return_pct: number; r_multiple: number | null; adherence: Record<string, boolean | null> } | null;
  }[];
  fills: { id: string; account_ref: string; contract: string; side: string; quantity: number; price: number; fees: number; executed_at: string }[];
}

function LinkForm({ positionId, onDone }: { positionId: string; onDone: () => void }) {
  const [value, setValue] = useState("");
  const [kind, setKind] = useState<"thesis_id" | "plan_id">("thesis_id");
  const [error, setError] = useState<string | null>(null);
  return (
    <form
      style={{ display: "flex", gap: 4, marginTop: 4 }}
      onSubmit={(event) => {
        event.preventDefault();
        postJson(`/api/positions/${positionId}/link`, { [kind]: value.trim() })
          .then(onDone)
          .catch((reason: unknown) => setError(String(reason)));
      }}
    >
      <select className="input" style={{ width: "auto" }} value={kind} onChange={(e) => setKind(e.target.value as "thesis_id" | "plan_id")}>
        <option value="thesis_id">thesis</option>
        <option value="plan_id">plan</option>
      </select>
      <input className="input mono" placeholder="id to link" value={value} onChange={(e) => setValue(e.target.value)} />
      <button className="btn" type="submit">Confirm</button>
      {error && <span className="neg">{error}</span>}
    </form>
  );
}

export function Book() {
  const { data, error, reload } = useApi<BookData>("/api/book", ["fill", "position", "holding_rating", "score"]);
  if (error && !data) return <div className="content"><div className="empty">{error}</div></div>;
  if (!data) return <div className="content"><div className="empty">Loading</div></div>;
  const ratings = Object.fromEntries(data.ratings.map((r) => [r.subject, r]));
  return (
    <div className="content">
      {data.holdings.map((account) => (
        <div className="box" key={account.account_ref}>
          <div className="box-header">
            <span className="mono">{account.account_ref}</span>
            <span className="muted">
              net liquidation {money(account.net_liquidation)} · cash {money(account.cash)} · as of {time(account.as_of)}
            </span>
          </div>
          {account.positions.length === 0 ? (
            <div className="empty">No positions.</div>
          ) : (
            <div className="scroll">
              <table>
                <thead>
                  <tr><th>Holding</th><th className="num">Qty</th><th className="num">Mark</th><th className="num">Value</th><th className="num">P&amp;L</th><th>Rating</th></tr>
                </thead>
                <tbody>
                  {account.positions.map((p) => {
                    const pnl = p.market_value !== null && p.cost_basis !== null ? p.market_value - p.cost_basis : null;
                    const rating = ratings[p.symbol];
                    return (
                      <tr key={p.contract}>
                        <td><b>{p.symbol}</b> <span className="muted">{p.contract}</span></td>
                        <td className="num">{num(p.quantity, 0)}</td>
                        <td className="num">{num(p.mark_price)}</td>
                        <td className="num">{money(p.market_value)}</td>
                        <td className={`num ${pnl === null ? "" : pnl >= 0 ? "pos" : "neg"}`}>{money(pnl)}</td>
                        <td>{rating ? `${RATING[rating.rating] ?? rating.rating} (${rating.confidence})` : "-"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}
      <div className="box">
        <div className="box-header">Positions from fills</div>
        {data.positions.length === 0 ? (
          <div className="empty">No fills captured yet; they arrive with the next Flex statement after a trade.</div>
        ) : (
          <div className="scroll">
            <table>
              <thead>
                <tr><th>Position</th><th>State</th><th className="num">Entry</th><th className="num">Exit</th><th className="num">Realized</th><th className="num">R</th><th>Link</th><th>Adherence</th></tr>
              </thead>
              <tbody>
                {data.positions.map((p) => (
                  <tr key={p.id}>
                    <td><b>{p.contract}</b> {p.direction}<div className="muted">{time(p.opened_at)}{p.closed_at ? ` to ${time(p.closed_at)}` : ""}</div></td>
                    <td>{p.state}</td>
                    <td className="num">{num(p.avg_entry ? Number(p.avg_entry) : null)}</td>
                    <td className="num">{num(p.avg_exit ? Number(p.avg_exit) : null)}</td>
                    <td className={`num ${Number(p.realized_pnl) >= 0 ? "pos" : "neg"}`}>{money(Number(p.realized_pnl))}</td>
                    <td className="num">{num(p.exit?.r_multiple)}</td>
                    <td>
                      {p.link_status === "none" ? <span className="muted">unlinked</span> : (
                        <>
                          <span className={`label ${p.link_status === "suggested" ? "attention" : "success"}`}>{p.link_status}</span>{" "}
                          {p.thesis_id && <a href={`#/theses/${p.thesis_id}`}>thesis</a>} {p.plan_id && <a href={`#/plans/${p.plan_id}`}>plan</a>}
                        </>
                      )}
                      {p.link_status !== "confirmed" && <LinkForm positionId={p.id} onDone={reload} />}
                    </td>
                    <td>
                      {p.exit
                        ? Object.entries(p.exit.adherence).map(([k, v]) => (
                            <div key={k}><span className={`dot ${v === null ? "" : v ? "ok" : "fail"}`} /> {k.replace(/_/g, " ")}</div>
                          ))
                        : "-"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="box">
        <div className="box-header">Recent fills</div>
        {data.fills.length === 0 ? (
          <div className="empty">No fills yet.</div>
        ) : (
          <div className="scroll">
            <table>
              <thead><tr><th>When</th><th>Account</th><th>Contract</th><th>Side</th><th className="num">Qty</th><th className="num">Price</th><th className="num">Fees</th></tr></thead>
              <tbody>
                {data.fills.map((f) => (
                  <tr key={f.id}>
                    <td>{time(f.executed_at)}</td><td className="mono">{f.account_ref}</td><td className="mono">{f.contract}</td><td>{f.side}</td>
                    <td className="num">{num(f.quantity, 0)}</td><td className="num">{num(f.price)}</td><td className="num">{num(f.fees)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
