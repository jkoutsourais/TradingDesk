"""From an approved idea to a sized plan: trader choice (deep model), then the risk desk.

The model sees the menu and a level list as placeholders and returns ids; code copies
every price into the TradePlan, sizes it with `risk.rules.evaluate` and stores the
RiskDecision with every check. A plain-English risk note is added for spreads, leveraged
funds and futures.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from desk.artifacts.trade import CheckRecord, RiskDecision, TradePlan
from desk.config import ChatModel
from desk.desks.analyst.debate import ask
from desk.desks.idea.levels import Level
from desk.desks.risk.config import RiskConfig
from desk.desks.risk.rules import AccountState, Decision, OpenRisk, PlanInput, evaluate
from desk.desks.trader.menu import Choice, Priced
from desk.llm.client import OllamaChat, Usage
from desk.llm.facts import Fact, FactTable
from desk.llm.prompts import load_prompt

NOTE_STRUCTURES = frozenset({"debit_spread", "levered_etf", "future"})


@dataclass(frozen=True, slots=True)
class Idea:
    """What the trader expresses: a pursued thesis or a Buy/Add holding rating."""

    source_id: UUID  # thesis or rating id
    source_kind: Literal["thesis", "rating"]
    extra_parents: tuple[UUID, ...]  # the verdict behind a thesis plan
    subject: str
    direction: Literal["long", "short"]
    summary: str
    origin: str
    conviction: int
    stop: Decimal
    stop_ref: str
    thesis_hard: Decimal | None
    review_by: date | None
    catalyst_names: tuple[str, ...]


class Rejected(BaseModel):
    choice_id: str
    reason: str = Field(min_length=1, max_length=300)


class TraderReply(BaseModel):
    choice_id: str
    target_level_id: str | None = None
    rationale: str = Field(min_length=1, max_length=800)
    rejected: list[Rejected] = Field(default_factory=list, max_length=8)


def menu_facts(choices: list[Choice], levels: list[Level], idea: Idea) -> list[Fact]:
    facts = []
    for choice in choices:
        facts.append(
            Fact(
                f"{choice.id}_cost",
                choice.unit_cost,
                "USD",
                f"${choice.unit_cost:,.2f}",
                f"{choice.id} cost per unit",
                f"menu:{choice.instrument}",
            )
        )
        facts.append(
            Fact(
                f"{choice.id}_loss",
                choice.unit_max_loss,
                "USD",
                f"${choice.unit_max_loss:,.2f}",
                f"{choice.id} loss per unit at the stop",
                f"menu:{choice.instrument}",
            )
        )
        if choice.spread_pct is not None:
            facts.append(
                Fact(
                    f"{choice.id}_spread",
                    Decimal(f"{choice.spread_pct:.2f}"),
                    "%",
                    f"{choice.spread_pct:.1f}%",
                    f"{choice.id} bid-ask spread",
                    f"menu:{choice.instrument}",
                )
            )
        if choice.open_interest is not None:
            facts.append(
                Fact(
                    f"{choice.id}_oi",
                    Decimal(choice.open_interest),
                    "contracts",
                    f"{choice.open_interest:,}",
                    f"{choice.id} open interest",
                    f"menu:{choice.instrument}",
                )
            )
    for level in levels:
        facts.append(
            Fact(level.id, level.value, "USD", f"${level.value:,}", level.label, level.ref)
        )
    facts.append(
        Fact("stop", idea.stop, "USD", f"${idea.stop:,}", f"{idea.subject} stop", idea.stop_ref)
    )
    return facts


def menu_text(choices: list[Choice]) -> str:
    lines = []
    for choice in choices:
        extra = ""
        if choice.spread_pct is not None:
            extra += f", spread {{{choice.id}_spread}}"
        if choice.open_interest is not None:
            extra += f", open interest {{{choice.id}_oi}}"
        name = choice.structure.replace("_", " ")
        lines.append(
            f"- {choice.id}: {name} {choice.instrument}; cost {{{choice.id}_cost}}, "
            f"loss at the stop {{{choice.id}_loss}} per unit{extra}"
        )
    return "\n".join(lines)


def check_reply(
    choices: list[Choice], levels: list[Level], entry: Decimal, idea: Idea, table: FactTable
) -> Any:
    ids = {c.id for c in choices}
    by_id = {level.id: level for level in levels}

    def check(reply: TraderReply) -> list[str]:
        problems = []
        if reply.choice_id not in ids:
            problems.append(f"choice_id must be one of {sorted(ids)}")
        if reply.target_level_id is not None:
            level = by_id.get(reply.target_level_id)
            if level is None:
                problems.append("target_level_id must be a level id (lvl_N) or null")
            elif (level.value <= entry) if idea.direction == "long" else (level.value >= entry):
                side = "above" if idea.direction == "long" else "below"
                problems.append(f"the target level must be {side} the entry")
        problems += [f"rationale: {p}" for p in table.violations(reply.rationale)]
        for item in reply.rejected:
            if item.choice_id not in ids:
                problems.append(f"rejected choice {item.choice_id} is not on the menu")
            problems += [f"rejected {item.choice_id}: {p}" for p in table.violations(item.reason)]
        return problems

    return check


def build_plan(
    idea: Idea,
    choice: Choice,
    reply: TraderReply,
    entry: Priced,
    levels: list[Level],
    table: FactTable,
    account_ref: str,
    usage: Usage,
    prompt_version: str,
    runtime_ms: int,
    shift_id: UUID | None,
) -> TradePlan:
    target = next((level for level in levels if level.id == reply.target_level_id), None)
    rejected = tuple(f"{r.choice_id}: {table.render(r.reason)}" for r in reply.rejected)
    return TradePlan(
        produced_by="trader",
        runtime_ms=runtime_ms,
        shift_id=shift_id,
        parents=(idea.source_id, *idea.extra_parents),
        model=usage.model,
        prompt_version=prompt_version,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        thesis_id=idea.source_id if idea.source_kind == "thesis" else None,
        rating_id=idea.source_id if idea.source_kind == "rating" else None,
        account_ref=account_ref,
        subject=idea.subject,
        instrument=choice.instrument,
        structure=choice.structure,
        direction=idea.direction,
        legs=choice.legs,
        entry=entry.price,
        entry_ref=entry.ref,
        stop=idea.stop,
        stop_ref=idea.stop_ref,
        target=target.value if target else None,
        target_ref=target.ref if target else None,
        expiry=choice.expiry,
        unit_cost=choice.unit_cost,
        unit_max_loss=choice.unit_max_loss,
        leverage=choice.leverage,
        spread_pct=choice.spread_pct,
        open_interest=choice.open_interest,
        conviction=idea.conviction,
        rationale=table.render(reply.rationale),
        alternatives_rejected=rejected,
    )


def plan_input(
    plan: TradePlan, idea: Idea, today: date, earnings: list[date], majors: list[tuple[date, str]]
) -> PlanInput:
    return PlanInput(
        structure=plan.structure,
        subject=plan.subject,
        instrument=plan.instrument,
        direction=plan.direction,
        entry=plan.entry,
        stop=plan.stop,
        target=plan.target,
        thesis_hard=idea.thesis_hard,
        unit_cost=plan.unit_cost,
        unit_max_loss=plan.unit_max_loss,
        conviction=plan.conviction,
        today=today,
        review_by=idea.review_by,
        leverage=float(plan.leverage),
        spread_pct=plan.spread_pct,
        open_interest=plan.open_interest,
        earnings=earnings,
        major_events=majors,
        catalyst_names=idea.catalyst_names,
    )


def decision_facts(plan: TradePlan, decision: Decision) -> list[Fact]:
    ref = f"trade_plan:{plan.id}"
    facts = [
        Fact("size", Decimal(decision.size), "units", f"{decision.size}", "units in the plan", ref),
        Fact(
            "max_loss",
            decision.max_loss,
            "USD",
            f"${decision.max_loss:,.2f}",
            "maximum planned loss",
            ref,
        ),
        Fact("cost", decision.cost, "USD", f"${decision.cost:,.2f}", "total cost", ref),
        Fact(
            "stop",
            plan.stop or Decimal(0),
            "USD",
            f"${plan.stop or 0:,}",
            f"{plan.subject} stop",
            plan.stop_ref or ref,
        ),
        Fact(
            "entry",
            plan.entry,
            "USD",
            f"${plan.entry:,}",
            f"{plan.subject} entry price",
            plan.entry_ref,
        ),
    ]
    if plan.expiry is not None:
        facts.append(
            Fact(
                "expiry",
                plan.expiry.isoformat(),
                "",
                f"{plan.expiry:%b} {plan.expiry.day}, {plan.expiry.year}",
                "option expiry",
                ref,
            )
        )
    if plan.leverage != 1:
        facts.append(
            Fact("leverage", plan.leverage, "x", f"{plan.leverage:g}x", "daily-reset leverage", ref)
        )
    return facts


class NoteReply(BaseModel):
    note: str = Field(min_length=1, max_length=900)


async def risk_note(
    chat: OllamaChat, model: ChatModel, plan: TradePlan, decision: Decision
) -> tuple[str | None, Usage | None]:
    if plan.structure not in NOTE_STRUCTURES or decision.decision == "vetoed":
        return None, None
    table = FactTable(decision_facts(plan, decision))
    summary = (
        f"{plan.structure.replace('_', ' ')} on {plan.subject} via {plan.instrument}, "
        f"{plan.direction}; size {{size}}, cost {{cost}}, maximum loss {{max_loss}}, "
        f"stop on {plan.subject} at {{stop}}"
        + (", expiring {expiry}" if plan.expiry else "")
        + (", leverage {leverage}" if plan.leverage != 1 else "")
    )
    prompt = load_prompt("risk_note", {"plan": summary, "facts": table.prompt_listing()})
    result = await ask(chat, model, prompt, NoteReply, lambda r: table.violations(r.note))
    if result.value is None:
        return None, result.usage
    return table.render(result.value.note), result.usage


def risk_decision(
    plan: TradePlan,
    decision: Decision,
    note: str | None,
    shift_id: UUID | None,
    model: str | None,
) -> RiskDecision:
    return RiskDecision(
        produced_by="risk",
        runtime_ms=0,
        shift_id=shift_id,
        parents=(plan.id,),
        model=model if note else None,
        prompt_version="risk_note.v1" if note else None,
        plan_id=plan.id,
        decision=decision.decision,
        tier_pct=decision.tier_pct,
        cap=decision.cap,
        requested_size=decision.requested_size,
        size=decision.size,
        max_loss=decision.max_loss,
        cost=decision.cost,
        funding_needed=decision.funding_needed,
        checks=tuple(
            CheckRecord(name=c.name, result=c.result, detail=c.detail) for c in decision.checks
        ),
        veto_reasons=decision.veto_reasons,
        risk_note=note,
    )


async def plan_idea(
    chat: OllamaChat,
    model: ChatModel,
    idea: Idea,
    choices: list[Choice],
    levels: list[Level],
    entry: Priced,
    account: AccountState,
    open_risks: list[OpenRisk],
    combined_equity: Decimal,
    risk_config: RiskConfig,
    today: date,
    earnings: list[date],
    majors: list[tuple[date, str]],
    shift_id: UUID | None,
) -> tuple[TradePlan | None, RiskDecision | None, str | None]:
    """Plan and risk decision for one idea, or an error when the trader reply failed."""
    table = FactTable(menu_facts(choices, levels, idea))
    prompt = load_prompt(
        "trader",
        {
            "origin": idea.origin,
            "idea": idea.summary,
            "menu": menu_text(choices),
            "facts": table.prompt_listing(),
        },
    )
    result = await ask(
        chat, model, prompt, TraderReply, check_reply(choices, levels, entry.price, idea, table)
    )
    if result.value is None:
        return None, None, f"{idea.subject}: trader failed: {result.error}"
    choice = next(c for c in choices if c.id == result.value.choice_id)
    plan = build_plan(
        idea,
        choice,
        result.value,
        entry,
        levels,
        table,
        account.ref,
        result.usage,
        prompt.version,
        result.usage.total_ms,
        shift_id,
    )
    decision = evaluate(
        plan_input(plan, idea, today, earnings, majors),
        account,
        open_risks,
        combined_equity,
        risk_config,
    )
    note, _ = await risk_note(chat, model, plan, decision)
    return plan, risk_decision(plan, decision, note, shift_id, model.model), None
