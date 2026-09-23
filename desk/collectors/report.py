"""Coverage report for the Data desk: `uv run python -m desk.collectors.report [--hours 48]`.

A gap is a stretch longer than twice a collector's interval, counted only while its window
is open, with no successful run. Failed runs do not count as coverage. Exits with status 1
when any enabled collector has a gap, so the Phase 1 check can be scripted.
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from desk.config import CollectorSchedule, ScheduleConfig, load_schedule
from desk.db import make_engine
from desk.settings import Settings

GAP_FACTOR = 2


@dataclass(frozen=True, slots=True)
class Gap:
    start: datetime
    end: datetime
    active_seconds: float


def find_gaps(
    success_times: list[datetime],
    period_start: datetime,
    period_end: datetime,
    schedule: CollectorSchedule,
    tz: ZoneInfo,
) -> list[Gap]:
    limit = GAP_FACTOR * schedule.interval_seconds
    points = [period_start, *sorted(t for t in success_times if period_start <= t <= period_end)]
    points.append(period_end)
    gaps = []
    for start, end in pairwise(points):
        if schedule.window is None:
            active = (end - start).total_seconds()
        else:
            active = schedule.window.active_seconds(start, end, tz)
        if active > limit:
            gaps.append(Gap(start, end, active))
    return gaps


@dataclass(frozen=True, slots=True)
class CollectorCoverage:
    collector: str
    enabled: bool
    disabled_reason: str | None
    ok_runs: int
    failed_runs: int
    gaps: list[Gap]
    last_error: str | None


def coverage(
    conn: Connection, schedule: ScheduleConfig, period_start: datetime, period_end: datetime
) -> list[CollectorCoverage]:
    health = {
        row.collector: row
        for row in conn.execute(
            text("SELECT collector, enabled, disabled_reason, last_error FROM collector_health")
        )
    }
    results = []
    for name, collector_schedule in sorted(schedule.collectors.items()):
        runs = conn.execute(
            text(
                "SELECT started_at, status FROM job_runs "
                "WHERE desk = 'data' AND job = :job AND started_at BETWEEN :start AND :end"
            ),
            {"job": name, "start": period_start, "end": period_end},
        ).all()
        ok_times = [row.started_at for row in runs if row.status == "ok"]
        row = health.get(name)
        results.append(
            CollectorCoverage(
                collector=name,
                enabled=bool(row.enabled) if row else False,
                disabled_reason=row.disabled_reason if row else "never registered",
                ok_runs=len(ok_times),
                failed_runs=sum(1 for r in runs if r.status == "failed"),
                gaps=find_gaps(ok_times, period_start, period_end, collector_schedule, schedule.tz),
                last_error=row.last_error if row else None,
            )
        )
    return results


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.0f}m"


def main() -> None:
    parser = argparse.ArgumentParser(description="Data desk coverage and gap report")
    parser.add_argument("--hours", type=float, default=48.0)
    args = parser.parse_args()

    schedule = load_schedule()
    engine = make_engine(Settings())
    period_end = datetime.now(UTC)
    period_start = period_end - timedelta(hours=args.hours)
    with engine.connect() as conn:
        results = coverage(conn, schedule, period_start, period_end)
    engine.dispose()

    tz = schedule.tz
    stamp = "%Y-%m-%d %H:%M"
    print(
        f"Coverage {period_start.astimezone(tz):{stamp}} to {period_end.astimezone(tz):{stamp}} ET"
    )
    print(f"{'collector':<36} {'state':<9} {'ok':>5} {'fail':>5} {'gaps':>5} {'longest':>8}")
    any_gap = False
    for item in results:
        state = "enabled" if item.enabled else "disabled"
        longest = max((g.active_seconds for g in item.gaps), default=0.0)
        print(
            f"{item.collector:<36} {state:<9} {item.ok_runs:>5} {item.failed_runs:>5} "
            f"{len(item.gaps):>5} {_minutes(longest) if item.gaps else '-':>8}"
        )
        if item.enabled and item.gaps:
            any_gap = True
    for item in results:
        if not item.enabled:
            print(f"  {item.collector}: disabled ({item.disabled_reason})")
            continue
        for gap in item.gaps:
            print(
                f"  {item.collector}: no successful run {gap.start.astimezone(tz):%m-%d %H:%M} "
                f"to {gap.end.astimezone(tz):%m-%d %H:%M} ET "
                f"({_minutes(gap.active_seconds)} active)"
            )
        if item.gaps and item.last_error:
            print(f"    last error: {item.last_error[:300]}")
    sys.exit(1 if any_gap else 0)


if __name__ == "__main__":
    main()
