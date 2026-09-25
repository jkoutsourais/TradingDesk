"""The scorekeeper: positions from fills, shadow scores from bars, real scores at exit.

Runs as code at the start of the post-market shift. Each subject is scored once per
horizon (1, 5 and 20 trading days, or at exit for real positions) as soon as the daily
bars exist, and every Score carries the attribution its rollups group by.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, text

from desk.artifacts.analyst import AnalystView, DebateVerdict, HoldingRating
from desk.artifacts.base import ArtifactBase
from desk.artifacts.scoring import Fill, LinkStatus, Position, Score
from desk.artifacts.store import append_artifact, get_artifact
from desk.artifacts.thesis import Thesis
from desk.artifacts.trade import RiskDecision, TradePlan
from desk.scoring.positions import Built, FillRow, build_positions
from desk.scoring.shadow import (
    HORIZONS,
    PathScore,
    adherence,
    rating_hit,
    score_path,
    stance_hit,
    verdict_hit,
)
from desk.watch.rules import DailyBar
from desk.watch.scan import load_daily_bars

logger = logging.getLogger(__name__)

AUTO_LINK_WINDOW = timedelta(days=7)  # about five trading days
SESSION_CLOSE = time(16, 0)


@dataclass
class ScoringOutcome:
    scores: int = 0
    positions: int = 0
    notes: list[str] = field(default_factory=list)


# --- Loading ----------------------------------------------------------------------------


def load_kind(conn: Connection, kind: str) -> list[Any]:
    ids = conn.execute(
        text("SELECT id FROM artifacts WHERE kind = :k AND status = 'ok' ORDER BY created_at"),
        {"k": kind},
    ).scalars()
    return [get_artifact(conn, artifact_id) for artifact_id in ids]


def scored(conn: Connection) -> set[tuple[str, str]]:
    rows = conn.execute(
        text(
            "SELECT payload->>'subject_id', payload->>'horizon' FROM artifacts WHERE kind = 'score'"
        )
    )
    return {(row[0], row[1]) for row in rows}


class Bars:
    """Daily bars per symbol, loaded once per run."""

    def __init__(self, conn: Connection, tz: ZoneInfo, today: date) -> None:
        self._conn = conn
        self._tz = tz
        self._through = today + timedelta(days=1)
        self._cache: dict[str, list[DailyBar]] = {}

    def get(self, symbol: str) -> list[DailyBar]:
        if symbol not in self._cache:
            self._cache[symbol] = load_daily_bars(
                self._conn, [symbol], self._through, self._tz
            ).get(symbol, [])
        return self._cache[symbol]

    def entry(self, symbol: str, when: datetime) -> tuple[DailyBar, list[DailyBar]] | None:
        """The last close already known at `when`, and the bars after it.

        Before the 4pm close that is the previous session's close, so a call made
        pre-market is measured from the price it saw.
        """
        local = when.astimezone(self._tz)
        day = local.date() if local.time() >= SESSION_CLOSE else local.date() - timedelta(days=1)
        history = self.get(symbol)
        before = [b for b in history if b.day <= day]
        if not before:
            return None
        anchor = before[-1]
        return anchor, [b for b in history if b.day > anchor.day]


# --- Shadow scores ------------------------------------------------------------------------


def _score(
    subject: ArtifactBase,
    kind: str,
    horizon: str,
    symbol: str,
    anchor: DailyBar,
    path: PathScore,
    hit: bool | None,
    attribution: dict[str, str],
    extra_parents: tuple[UUID, ...] = (),
    entry: tuple[Decimal, str] | None = None,
) -> Score:
    """A shadow score; `entry` overrides the anchor close (a plan's own entry price)."""
    entry_price, entry_ref = entry or (
        Decimal(str(anchor.close)),
        f"price_bars:yahoo:{symbol}:1d:{anchor.day}",
    )
    return Score(
        produced_by="scoring",
        runtime_ms=0,
        parents=(subject.id, *extra_parents),
        subject_kind=kind,
        subject_id=subject.id,
        horizon=horizon,
        shadow=True,
        entry_day=anchor.day,
        entry_price=entry_price,
        entry_ref=entry_ref,
        exit_day=path.exit_day,
        exit_price=Decimal(str(path.exit_price)),
        return_pct=path.return_pct,
        mae_pct=path.mae_pct,
        mfe_pct=path.mfe_pct,
        first_hit=path.first_hit,
        r_multiple=path.r_multiple,
        hit=hit,
        attribution={k: v for k, v in attribution.items() if v},
    )


def instrument_class(symbol: str) -> str:
    return "future" if symbol.startswith("/") else "equity"


def thesis_roots(theses: list[Thesis]) -> dict[UUID, Thesis]:
    """Map every thesis version id to the version scored for its chain.

    Desk theses are scored from their first version; Jon's from the version he confirmed.
    """
    by_id = {t.id: t for t in theses}
    roots: dict[UUID, Thesis] = {}
    for thesis in theses:
        current = thesis
        chain = [current]
        while current.previous_id is not None and current.previous_id in by_id:
            current = by_id[current.previous_id]
            chain.append(current)
        active = [t for t in reversed(chain) if t.state != "draft"]
        if active:
            roots[thesis.id] = active[0]
    return roots


def thesis_attribution(thesis: Thesis) -> dict[str, str]:
    return {
        "lane": "" if thesis.origin == "jon" else thesis.origin,
        "origin": "jon" if thesis.origin == "jon" else "desk",
        "instrument_class": instrument_class(thesis.primary_instrument),
    }


def thesis_paths(
    roots: dict[UUID, Thesis], bars: Bars
) -> dict[tuple[UUID, str], tuple[DailyBar, PathScore]]:
    """Thesis outcome per root and horizon, shared by thesis, view and verdict scores."""
    paths = {}
    for root in {r.id: r for r in roots.values()}.values():
        if root.direction == "relative" or root.invalidation is None:
            continue
        found = bars.entry(root.primary_instrument, root.created_at)
        if found is None:
            continue
        anchor, after = found
        for horizon, days in HORIZONS.items():
            path = score_path(
                root.direction,
                Decimal(str(anchor.close)),
                root.invalidation.hard.level,
                None,
                after,
                days,
            )
            if path is not None:
                paths[(root.id, horizon)] = (anchor, path)
    return paths


@dataclass
class ShadowContext:
    conn: Connection
    bars: Bars
    done: set[tuple[str, str]]
    roots: dict[UUID, Thesis]
    paths: dict[tuple[UUID, str], tuple[DailyBar, PathScore]]

    def fresh(self, subject_id: UUID, horizon: str) -> bool:
        return (str(subject_id), horizon) not in self.done


def _thesis_scores(ctx: ShadowContext) -> list[Score]:
    by_id = {r.id: r for r in ctx.roots.values()}
    scores = []
    for (root_id, horizon), (anchor, path) in ctx.paths.items():
        root = by_id[root_id]
        if ctx.fresh(root_id, horizon):
            scores.append(
                _score(
                    root,
                    "thesis",
                    horizon,
                    root.primary_instrument,
                    anchor,
                    path,
                    path.return_pct > 0,
                    thesis_attribution(root),
                )
            )
    return scores


def _outcome_scores(ctx: ShadowContext) -> list[Score]:
    """Verdicts and specialist views, scored against their thesis's outcome."""
    scores = []
    for item in load_kind(ctx.conn, "debate_verdict") + load_kind(ctx.conn, "analyst_view"):
        if isinstance(item, AnalystView) and (item.role != "view" or item.thesis_id is None):
            continue
        thesis_id = item.thesis_id
        root = ctx.roots.get(thesis_id) if thesis_id else None
        if root is None:
            continue
        for horizon in HORIZONS:
            if (root.id, horizon) not in ctx.paths or not ctx.fresh(item.id, horizon):
                continue
            anchor, path = ctx.paths[(root.id, horizon)]
            if isinstance(item, DebateVerdict):
                kind, hit = "verdict", verdict_hit(item.verdict, path.return_pct)
                extra = {"verdict": item.verdict}
            else:
                kind, hit = "view", stance_hit(item.stance, path.return_pct)
                extra = {"persona": item.persona}
            scores.append(
                _score(
                    item,
                    kind,
                    horizon,
                    root.primary_instrument,
                    anchor,
                    path,
                    hit,
                    {**thesis_attribution(root), **extra},
                    (root.id,),
                )
            )
    return scores


def _rating_scores(ctx: ShadowContext) -> list[Score]:
    scores = []
    for rating in load_kind(ctx.conn, "holding_rating"):
        assert isinstance(rating, HoldingRating)
        found = ctx.bars.entry(rating.subject, rating.created_at)
        if found is None:
            continue
        anchor, after = found
        persona = next((name for p in rating.parents if (name := _persona_of(ctx.conn, p))), "")
        for horizon, days in HORIZONS.items():
            if not ctx.fresh(rating.id, horizon):
                continue
            path = score_path("long", Decimal(str(anchor.close)), None, None, after, days)
            if path is None:
                continue
            scores.append(
                _score(
                    rating,
                    "rating",
                    horizon,
                    rating.subject,
                    anchor,
                    path,
                    rating_hit(rating.rating, path.return_pct),
                    {
                        "rating": rating.rating,
                        "persona": persona,
                        "origin": "holdings",
                        "instrument_class": instrument_class(rating.subject),
                    },
                )
            )
    return scores


def _plan_scores(ctx: ShadowContext) -> list[Score]:
    decisions = {
        d.plan_id: d for d in load_kind(ctx.conn, "risk_decision") if isinstance(d, RiskDecision)
    }
    scores = []
    for plan in load_kind(ctx.conn, "trade_plan"):
        assert isinstance(plan, TradePlan)
        decision = decisions.get(plan.id)
        found = ctx.bars.entry(plan.subject, plan.created_at)
        if found is None or decision is None:
            continue
        anchor, after = found
        root = ctx.roots.get(plan.thesis_id) if plan.thesis_id else None
        attribution = {
            **(thesis_attribution(root) if root else {"origin": "holdings"}),
            "decision": decision.decision,
            "tier": f"{decision.tier_pct:g}%",
            "structure": plan.structure,
            "veto_reason": _veto_reason(decision),
        }
        for horizon, days in HORIZONS.items():
            if not ctx.fresh(plan.id, horizon):
                continue
            # A plan is measured from the subject price it was sized on.
            path = score_path(plan.direction, plan.entry, plan.stop, plan.target, after, days)
            if path is None:
                continue
            scores.append(
                _score(
                    plan,
                    "plan",
                    horizon,
                    plan.subject,
                    anchor,
                    path,
                    path.return_pct > 0,
                    attribution,
                    (decision.id,),
                    (plan.entry, plan.entry_ref),
                )
            )
    return scores


def shadow_scores(conn: Connection, tz: ZoneInfo, today: date) -> list[Score]:
    bars = Bars(conn, tz, today)
    roots = thesis_roots([t for t in load_kind(conn, "thesis") if isinstance(t, Thesis)])
    ctx = ShadowContext(conn, bars, scored(conn), roots, thesis_paths(roots, bars))
    return _thesis_scores(ctx) + _outcome_scores(ctx) + _rating_scores(ctx) + _plan_scores(ctx)


def _persona_of(conn: Connection, artifact_id: UUID) -> str:
    row = conn.execute(
        text(
            "SELECT payload->>'persona' FROM artifacts WHERE id = :id "
            "AND kind = 'analyst_view' AND payload->>'role' = 'keep'"
        ),
        {"id": artifact_id},
    ).scalar()
    return row or ""


def _veto_reason(decision: RiskDecision) -> str:
    failed = [c.name for c in decision.checks if c.result == "fail"]
    return failed[0] if failed else ""


# --- Positions ----------------------------------------------------------------------------


def fill_rows(fills: list[Fill]) -> list[FillRow]:
    return [
        FillRow(
            exec_id=f.exec_id,
            account_ref=f.account_ref,
            symbol=f.symbol,
            contract=f.contract,
            side=f.side,
            quantity=f.quantity,
            price=f.price,
            multiplier=f.multiplier,
            fees=f.fees,
            executed_at=f.executed_at,
        )
        for f in fills
    ]


def latest_positions(conn: Connection) -> dict[tuple[str, str, datetime], Position]:
    """Latest version per (account, contract, opened_at)."""
    latest: dict[tuple[str, str, datetime], Position] = {}
    for position in load_kind(conn, "position"):
        assert isinstance(position, Position)
        latest[(position.account_ref, position.contract, position.opened_at)] = position
    return latest


def link_for(
    built: Built,
    plans: list[TradePlan],
    decisions: dict[UUID, RiskDecision],
    theses: list[Thesis],
) -> tuple[UUID | None, UUID | None, Literal["none", "suggested", "auto"], Decimal | None]:
    """Auto-link an exact plan match; otherwise suggest a plan or thesis on the symbol."""
    opened = built.opened_at
    assert opened is not None
    for plan in sorted(plans, key=lambda p: p.created_at, reverse=True):
        decision = decisions.get(plan.id)
        if decision is None or decision.decision == "vetoed":
            continue
        recent = timedelta(0) <= opened - plan.created_at <= AUTO_LINK_WINDOW
        if recent and plan.instrument == built.contract:
            return plan.id, plan.thesis_id, "auto", decision.max_loss
        if recent and plan.subject == built.symbol:
            return plan.id, plan.thesis_id, "suggested", decision.max_loss
    for thesis in theses:
        if thesis.primary_instrument == built.symbol and thesis.state not in ("draft",):
            return None, thesis.id, "suggested", None
    return None, None, "none", None


def build_position_versions(conn: Connection) -> list[Position]:
    fills = [f for f in load_kind(conn, "fill") if isinstance(f, Fill)]
    if not fills:
        return []
    by_exec = {(f.account_ref, f.exec_id): f for f in fills}
    plans = [p for p in load_kind(conn, "trade_plan") if isinstance(p, TradePlan)]
    decisions = {
        d.plan_id: d for d in load_kind(conn, "risk_decision") if isinstance(d, RiskDecision)
    }
    theses = [t for t in load_kind(conn, "thesis") if isinstance(t, Thesis)]
    existing = latest_positions(conn)
    versions = []
    for built in build_positions(fill_rows(fills)):
        assert built.opened_at is not None
        fill_ids = tuple(by_exec[(built.account_ref, f.exec_id)].id for f in built.fills)
        previous = existing.get((built.account_ref, built.contract, built.opened_at))
        status: LinkStatus
        if previous is not None and previous.link_status in ("confirmed", "auto"):
            plan_id, thesis_id, status, planned = (
                previous.plan_id,
                previous.thesis_id,
                previous.link_status,
                previous.planned_max_loss,
            )
        else:
            plan_id, thesis_id, new_status, planned = link_for(built, plans, decisions, theses)
            status = new_status
        if (
            previous is not None
            and previous.fill_ids == fill_ids
            and (previous.plan_id, previous.thesis_id, previous.link_status)
            == (plan_id, thesis_id, status)
        ):
            continue
        links = tuple(i for i in (plan_id, thesis_id) if i is not None)
        versions.append(
            Position(
                produced_by="scoring.positions",
                runtime_ms=0,
                parents=tuple(
                    dict.fromkeys((*fill_ids, *links, *((previous.id,) if previous else ())))
                ),
                account_ref=built.account_ref,
                symbol=built.symbol,
                contract=built.contract,
                direction=built.direction,
                state=built.state,
                quantity=built.quantity,
                max_quantity=built.max_quantity,
                avg_entry=built.avg_entry,
                avg_exit=built.avg_exit,
                realized_pnl=built.realized_pnl,
                opened_at=built.opened_at,
                closed_at=built.closed_at,
                fill_ids=fill_ids,
                plan_id=plan_id,
                thesis_id=thesis_id,
                link_status=status,
                planned_max_loss=planned,
                previous_id=previous.id if previous else None,
            )
        )
    return versions


def relink(
    conn: Connection, position_id: UUID, plan_id: UUID | None, thesis_id: UUID | None
) -> Position:
    """Jon's confirmed link, written as a new position version."""
    position = get_artifact(conn, position_id)
    if not isinstance(position, Position):
        raise ValueError(f"{position_id} is not a position")
    planned = None
    if plan_id is not None:
        plan = get_artifact(conn, plan_id)
        if not isinstance(plan, TradePlan):
            raise ValueError(f"{plan_id} is not a trade plan")
        thesis_id = thesis_id or plan.thesis_id
        decision_id = conn.execute(
            text(
                "SELECT id FROM artifacts WHERE kind = 'risk_decision' AND payload->>'plan_id' = :p"
            ),
            {"p": str(plan_id)},
        ).scalar()
        decision = get_artifact(conn, decision_id) if decision_id else None
        planned = decision.max_loss if isinstance(decision, RiskDecision) else None
    elif thesis_id is not None and not isinstance(get_artifact(conn, thesis_id), Thesis):
        raise ValueError(f"{thesis_id} is not a thesis")
    links = tuple(i for i in (plan_id, thesis_id) if i is not None)
    fields = position.model_dump(
        exclude={
            "id",
            "created_at",
            "parents",
            "plan_id",
            "thesis_id",
            "link_status",
            "planned_max_loss",
            "previous_id",
        }
    )
    return Position.model_validate(
        {
            **fields,
            "parents": tuple(dict.fromkeys((*position.fill_ids, *links, position.id))),
            "plan_id": plan_id,
            "thesis_id": thesis_id,
            "link_status": "confirmed" if links else "none",
            "planned_max_loss": planned,
            "previous_id": position.id,
        }
    )


# --- Real scores --------------------------------------------------------------------------


def exit_scores(conn: Connection, tz: ZoneInfo, today: date) -> list[Score]:
    done = scored(conn)
    bars = Bars(conn, tz, today)
    theses = {t.id: t for t in load_kind(conn, "thesis") if isinstance(t, Thesis)}
    roots = thesis_roots(list(theses.values()))
    scores = []
    for position in latest_positions(conn).values():
        if position.state != "closed" or (str(position.id), "exit") in done:
            continue
        if position.avg_entry is None or position.avg_exit is None or position.closed_at is None:
            continue
        opened = position.opened_at.astimezone(tz).date()
        closed = position.closed_at.astimezone(tz).date()
        held = [b for b in bars.get(position.symbol) if opened <= b.day <= closed]
        sign = 1 if position.direction == "long" else -1
        entry, exit_ = float(position.avg_entry), float(position.avg_exit)
        plan = get_artifact(conn, position.plan_id) if position.plan_id else None
        plan = plan if isinstance(plan, TradePlan) else None
        decision_size = None
        if plan is not None:
            decision_id = conn.execute(
                text(
                    "SELECT id FROM artifacts WHERE kind = 'risk_decision' "
                    "AND payload->>'plan_id' = :p"
                ),
                {"p": str(plan.id)},
            ).scalar()
            decision = get_artifact(conn, decision_id) if decision_id else None
            decision_size = decision.size if isinstance(decision, RiskDecision) else None
        thesis = theses.get(position.thesis_id) if position.thesis_id else None
        invalidated = _invalidated_on(theses, position.thesis_id, tz)
        checks = adherence(
            position.max_quantity,
            decision_size,
            plan.stop
            if plan
            else (thesis.invalidation.hard.level if thesis and thesis.invalidation else None),
            position.direction,
            {b.day: Decimal(str(b.low)) for b in held},
            {b.day: Decimal(str(b.high)) for b in held},
            closed,
            invalidated,
        )
        root = roots.get(thesis.id) if thesis else None
        attribution = {
            **(thesis_attribution(root) if root else {"origin": "unlinked"}),
            "link": position.link_status,
        }
        scores.append(
            Score(
                produced_by="scoring",
                runtime_ms=0,
                parents=(position.id,),
                subject_kind="position",
                subject_id=position.id,
                horizon="exit",
                shadow=False,
                entry_day=opened,
                entry_price=position.avg_entry,
                entry_ref=f"position:{position.id}:avg_entry",
                exit_day=closed,
                exit_price=position.avg_exit,
                return_pct=round(sign * (exit_ / entry - 1) * 100, 4),
                mae_pct=_excursion(held, entry, position.direction, worst=True),
                mfe_pct=_excursion(held, entry, position.direction, worst=False),
                r_multiple=round(float(position.realized_pnl / position.planned_max_loss), 4)
                if position.planned_max_loss
                else None,
                hit=position.realized_pnl > 0,
                realized_pnl=position.realized_pnl,
                adherence=checks,
                attribution={k: v for k, v in attribution.items() if v},
            )
        )
    return scores


def _excursion(bars: list[DailyBar], entry: float, direction: str, worst: bool) -> float | None:
    if not bars or entry <= 0:
        return None
    if direction == "long":
        move = (
            min(b.low for b in bars) / entry - 1 if worst else max(b.high for b in bars) / entry - 1
        )
    else:
        move = (
            1 - max(b.high for b in bars) / entry if worst else 1 - min(b.low for b in bars) / entry
        )
    return round((min(move, 0.0) if worst else max(move, 0.0)) * 100, 4)


def _invalidated_on(
    theses: dict[UUID, Thesis], thesis_id: UUID | None, tz: ZoneInfo
) -> date | None:
    if thesis_id is None:
        return None
    for thesis in theses.values():
        if thesis.state == "invalidated" and _descends(theses, thesis, thesis_id):
            return thesis.created_at.astimezone(tz).date()
    return None


def _descends(theses: dict[UUID, Thesis], thesis: Thesis, ancestor: UUID) -> bool:
    current: Thesis | None = thesis
    while current is not None:
        if current.id == ancestor:
            return True
        current = theses.get(current.previous_id) if current.previous_id else None
    return False


# --- Entry point --------------------------------------------------------------------------


def run_scoring(engine: Engine, tz: ZoneInfo, now: datetime) -> ScoringOutcome:
    outcome = ScoringOutcome()
    today = now.astimezone(tz).date()
    with engine.begin() as conn:
        for position in build_position_versions(conn):
            append_artifact(conn, position)
            outcome.positions += 1
    with engine.begin() as conn:
        scores = shadow_scores(conn, tz, today) + exit_scores(conn, tz, today)
        for score in scores:
            append_artifact(conn, score)
    outcome.scores = len(scores)
    outcome.notes.append(f"scoring: {outcome.positions} position versions, {outcome.scores} scores")
    return outcome
