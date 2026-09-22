# Backlog

Items noted during the build that are out of scope for the phase in which they came up.

## Open

- **shifts table (Phase 2).** `artifacts.shift_id` and `job_runs.shift_id` are plain UUIDs with no foreign key. Add a `shifts` table with the scheduler and a foreign key from both columns.
- **Starlette TestClient deprecation (any phase).** The test suite warns that Starlette's TestClient on `httpx` is deprecated in favor of `httpx2`. Switch when FastAPI's TestClient does.
- **Secret scanning in CI (Phase 9 or earlier).** GitHub push protection covers pushes to the public repo. A local pre-commit secret scan (e.g. gitleaks) would add a second layer; it needs approval as a new tool.
- **Lineage walk cost (Phase 4).** `get_lineage` uses a recursive CTE with `UNION` dedup. Re-check its query plan once real dossiers with many claims exist.
