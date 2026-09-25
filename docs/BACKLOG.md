# Backlog

Items noted during the build that are out of scope for the phase in which they came up.

## Open

- **Tune briefing and triage prompts (after a week of mornings).** briefing.v1 restates numbers per holding; it should connect them (a holding's move with its sector ETF, a headline, a watch hit). triage.v2 still mislabels some tiers (ORCL called tier 2) and stretches links (IonQ news to NVDA). Review real outputs and bump prompt versions.
- **Rank event-addition promotions (Phase 2 tuning).** The 10-per-day cap fills first-come with routine 8-Ks and earnings dates; rank candidates by importance and weigh routine 8-Ks below earnings or volume spikes.

- **Plain-English explanations on request (chat, Phase 5 or 9).** Jon wants "explain like I'm five" summaries for any trigger, brief line or number, e.g. what "20-day relative-strength leader" or "3.7 sigma EIA build" means and why it matters. Answer through the chat interface; numbers still come from the artifact's facts, the model only explains.


- **Starlette TestClient deprecation (any phase).** The test suite warns that Starlette's TestClient on `httpx` is deprecated in favor of `httpx2`. Switch when FastAPI's TestClient does.
- **Secret scanning in CI (Phase 9 or earlier).** GitHub push protection covers pushes to the public repo. A local pre-commit secret scan (e.g. gitleaks) would add a second layer; it needs approval as a new tool.
- **Holiday-aware collector windows (Phase 2).** `config/schedule.yaml` windows skip weekends only. The Phase 2 market calendar should also skip market holidays and adjust half-days.
- **Tier 2 options-liquidity ranking (Phase 2).** Tier 2 is the S&P 500 snapshot; ranking and adding liquid-options names belongs with the screen lane. The daily `tastytrade_metrics` liquidity rating is already recorded for every Tier 1-2 symbol.
- **PJM grid data (Phase 1, needs a key).** The gridstatus collector covers ERCOT, MISO and CAISO without keys; PJM (AEP, Dominion, data-center load) switches on once `PJM_API_KEY` is set from a free PJM Data Miner registration. NYISO and SPP load did not work with gridstatus 0.36 and are left out.
- **Split handling for daily bars (Phase 2).** Yahoo's unadjusted Close is still split-adjusted, and `price_bars` is insert-only, so history stored before a split no longer lines up with bars fetched after it. Scanners need a corporate-actions table (or a re-based view) before they compare prices across a split.
- **Stream backfill after long outages (Phase 2).** The DXLink stream backfills 2 hours of 1-minute candles on reconnect; an outage longer than that leaves a 1-minute gap, while daily bars stay complete.
- **IBKR intraday positions via IB Gateway (Phase 8 or earlier).** IBKR runs on the daily Flex statement only, one business day behind, because IB Gateway access is not available. When it is: add `ib_async` back, connect with `readonly=True` and Gateway's Read-Only API setting, snapshot positions every 15 minutes, and compare against Flex.
- **EDGAR latest-feed live verification (Phase 1).** The per-company pull is verified live (2026-09-22); the latest-filings feed first runs at 06:00 ET on 2026-09-23. Check its first runs in the gap report.
- **EDGAR secondary tickers (Phase 4).** The SEC ticker map lists preferred shares and notes under the issuer's CIK, so filings carry tags like `ORCL-PD` next to `ORCL`. Ticker matching in research should prefer the common-share symbol.
- **Lineage walk cost (Phase 4).** `get_lineage` uses a recursive CTE with `UNION` dedup. Re-check its query plan once real dossiers with many claims exist.
- **Filing text for Tier 2 plays (Phase 4).** `edgar_filing_text` fetches 8-K text for holdings and Tier 1 only; a Tier 2 play selected by the research desk falls back to news summaries. Fetch on demand when a play is selected.
- **Same-day market moves (Phase 4).** Market-move claims are recomputed from stored Yahoo daily closes. If the post-market shift runs before that day's bar is stored, the claim is rejected with "no stored prices"; the stream's day stats could serve as the close instead.
- **Shift runtime under GPU load (Phase 4).** With other applications on the GPU, the deep model slowed to about 10 tokens/s and one research request hit the 600 s timeout. Failed subjects are recorded and the shift continues, but the 30-minute shift limit could still cut research short. Measure typical post-market runtimes and size the limit from them.
- **Book maintenance lane (Phase 7).** Holdings ratings and their risk flags exist (Phase 6); stops near and size drift against planned risk need the trader desk's plans and `config/risk.yaml`.
- **Futures research sources (Phase 5).** Futures candidates (/NG, /CL) rarely have ticker-tagged news, so research often finds no verified claims and the selection drops them. Map futures to their ETF and producer proxies when gathering research sources.
- **Intake while a shift runs (Phase 5).** Intake shares the triage tick, so a message sent during the post-market shift waits until the shift ends, which can take half an hour.
- **Fundamentals feed for skeptical value (Phase 6).** The skeptical-value persona has no valuation or balance-sheet data. Finnhub's free basic-financials endpoint could supply P/E, margins and debt as code-computed facts; a new collector needs approval.
- **Debate quality tuning (Phase 6).** The first live rating (SMR, 2026-09-23) showed the keeper misreading a negative distance from the 50-day average as above it; the bear caught it. Review a week of debates and tune persona prompts and fact labels.
- **Ratings for futures and options holdings (Phase 6).** Rating facts use the underlying's daily bars; option greeks and roll costs are not in the facts yet.
- **Research drops whole dossiers for one bad claim (Phase 4).** The first live post-market shift (2026-09-24) failed 5 of 11 dossiers on paraphrased quotes and section numbers missing from cited quotes. Proposal: after the retry, keep the claims that pass, drop the rest and record them on the dossier, instead of failing the dossier.
- **Proxy duplicates in the shortlist (Phase 5).** /NG and UNG were shortlisted together on 2026-09-24. Dedupe candidates through `config/trader.yaml` future proxies.
- **Thesis and rating wording (Phases 5-6).** The first live NRG short thesis read "pullback to the 20-day high", the XLU thesis inverted its levels twice, and the AEP rating invented a fact id. Review prompts after a week of runs.
- **Futures margin check (Phase 7).** Futures plans only fit a funded margin account; the liquidity check warns that margin is not checked.
- **Fill history before 2026-09-24 (Phase 8).** The Flex query covers the last business day, so earlier executions and the positions held before the desk started have no fills. A one-off Flex query with a 365-day period would backfill them.
- **Scoring query cost (Phase 8).** The scorekeeper loads every thesis, plan, rating and view each run. Fine at hundreds; add a scored-through marker or indexes on payload subject ids once there are thousands.
- **Lane candidates are not scored (Phase 8).** Lane rollups come from theses. Scoring dropped candidates (no direction) would need a direction rule per lane.
- **Chat answer quality (Phase 9).** The chief of staff sees a fixed context (holdings, theses, plans, briefing, hits, score rollups, named tickers). Questions about older history or other symbols get "not in the facts". Add retrieval over artifacts by embedding once real questions show what is missing.
- **Dashboard tests (Phase 9).** The frontend is checked by `tsc` and the API by pytest; there are no browser tests. Add a few Playwright smoke tests (Today, Lanes, Chat) if the UI starts to regress.
- **Shift scrubber history (Phase 9).** The Lanes scrubber replays idea selections; board positions for stages reached later (debates, plans) reflect today's state, not the state at that shift.
