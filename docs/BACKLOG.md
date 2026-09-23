# Backlog

Items noted during the build that are out of scope for the phase in which they came up.

## Open

- **Remove the temporary dev dashboard (Phase 9).** `/dev` in `desk/api/dev.py` + `dev.html`, the `desk-dev-dashboard` logon task (`deploy/dev/run-dev-dashboard.ps1`: second API on 127.0.0.1:8010 plus an SSH reverse tunnel to the Beelink), and the Beelink containers `desk-dev-caddy` and `desk-dev-tunnel` (`deploy/beelink/dev-dashboard-up.sh`). The quick tunnel's URL changes whenever its container restarts, and it uses basic auth rather than Cloudflare Access; the Phase 9 setup replaces both with a named tunnel behind Access.
- **Service control without admin (any phase).** Restarting `desk-*` services needs an elevated shell. A one-time `sc sdset` granting the user account start/stop rights on those services would let deploys restart them remotely.

- **shifts table (Phase 2).** `artifacts.shift_id` and `job_runs.shift_id` are plain UUIDs with no foreign key. Add a `shifts` table with the scheduler and a foreign key from both columns.
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
