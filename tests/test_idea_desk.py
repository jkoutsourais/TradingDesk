from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from desk.artifacts.idea import LaneCandidate
from desk.artifacts.intake import IntakeMessage
from desk.artifacts.thesis import Catalyst, Thesis
from desk.desks.idea.levels import Level
from desk.desks.idea.status import invalidation_note, new_version
from desk.desks.idea.writer import (
    DraftDriver,
    ThesisDraft,
    WriterContext,
    build_thesis,
    check_draft,
    conviction_from_score,
    review_by,
)
from desk.front_office.briefing import thesis_line
from desk.front_office.intake import (
    IntakeReply,
    build_draft,
    check_reply,
    date_mentioned,
    review,
)
from desk.llm.client import Usage
from desk.llm.facts import Fact

TODAY = date(2026, 9, 23)
NEW_YORK = ZoneInfo("America/New_York")


# --- Writer -------------------------------------------------------------------------------


def context() -> WriterContext:
    candidate = LaneCandidate(
        produced_by="idea.lanes",
        runtime_ms=0,
        lane="commodity",
        instrument="SLV",
        driver="SLV broke above its 20-day range",
        score=0.65,
        ingredients=[{"name": "importance", "value": 0.65, "source_ref": "trigger:x"}],
    )
    ctx = WriterContext(candidate, last_close=Decimal("30.00"))
    verified = uuid4()
    ctx.claims["claim_1"] = verified
    ctx.facts.append(
        Fact("claim_1", "s", "", "Silver ETF inflows rose.", "verified claim on SLV", "vc:x")
    )
    for level_id, name, value in (
        ("lvl_1", "last_close", "30.00"),
        ("lvl_2", "low_20d", "28.00"),
        ("lvl_3", "close_minus_1atr", "29.20"),
        ("lvl_4", "high_20d", "31.50"),
    ):
        level = Level(level_id, "SLV", name, f"SLV {name}", Decimal(value), f"levels:SLV:{name}")
        ctx.levels[level_id] = level
        ctx.facts.append(Fact(level_id, level.value, "USD", f"${value}", level.label, level.ref))
    ctx.events["cal_1"] = Catalyst(name="CPI", on=date(2026, 10, 14), ref="calendar_events:cpi")
    ctx.facts.append(Fact("cal_1", "CPI", "", "Oct 14 CPI", "scheduled release", "cal:cpi"))
    return ctx


def draft(**overrides: object) -> ThesisDraft:
    fields: dict[str, object] = {
        "statement": "Silver keeps its breakout while inflows hold.",
        "direction": "long",
        "horizon": "weeks",
        "drivers": [
            DraftDriver(statement="ETF inflows continue", metric="SLV shares", source="yahoo")
        ],
        "evidence_ids": ["claim_1"],
        "catalyst_ids": ["cal_1"],
        "warning_level_id": "lvl_3",
        "hard_level_id": "lvl_2",
        "entry_conditions": ["A close above {lvl_4}"],
    }
    fields.update(overrides)
    return ThesisDraft.model_validate(fields)


def test_writer_check_accepts_valid_draft() -> None:
    assert check_draft(context())(draft()) == []


def test_writer_check_rejects_bad_ids_levels_and_numbers() -> None:
    check = check_draft(context())
    assert any("evidence" in p for p in check(draft(evidence_ids=["claim_9"])))
    assert any("lvl_N" in p for p in check(draft(hard_level_id="28.00")))
    wrong_side = check(draft(warning_level_id="lvl_2", hard_level_id="lvl_3"))
    assert any("for a long" in p for p in wrong_side)
    assert any("for a short" in p for p in check(draft(direction="short")))
    assert any("'32'" in p for p in check(draft(statement="Silver runs to 32.")))


def test_build_thesis_uses_code_values() -> None:
    ctx = context()
    thesis = build_thesis(draft(), ctx, TODAY, Usage(model="deep"), "thesis.v1", 5, None)
    assert thesis.state == "active" and thesis.origin == "commodity"
    assert thesis.invalidation is not None
    assert thesis.invalidation.hard.level == Decimal("28.00")
    assert thesis.invalidation.hard.measure == "daily_close"
    assert thesis.invalidation.warning.level_ref == "levels:SLV:close_minus_1atr"
    assert thesis.entry_conditions == ("A close above $31.50",)
    assert thesis.evidence == (ctx.claims["claim_1"],)
    assert thesis.review_by == review_by("weeks", TODAY)
    assert thesis.conviction == conviction_from_score(0.65) == 3
    assert thesis.catalysts[0].name == "CPI"


# --- Status -------------------------------------------------------------------------------


def active_thesis() -> Thesis:
    return build_thesis(draft(), context(), TODAY, Usage(model="deep"), "thesis.v1", 5, None)


def test_invalidation_by_hard_line_and_time_limit() -> None:
    thesis = active_thesis()
    created = thesis.created_at.astimezone(NEW_YORK).date()
    assert invalidation_note(thesis, (created, Decimal("28.50")), created, NEW_YORK) is None
    note = invalidation_note(thesis, (created, Decimal("27.90")), created, NEW_YORK)
    assert note is not None and note.startswith("hard line crossed")
    assert thesis.invalidation is not None and thesis.invalidation.time_limit is not None
    late = thesis.invalidation.time_limit
    after = date.fromordinal(late.toordinal() + 1)
    assert invalidation_note(thesis, None, after, NEW_YORK) == f"time limit {late} passed"
    # A close from before the thesis existed does not count.
    old_close = (date(2020, 1, 2), Decimal("1.00"))
    assert invalidation_note(thesis, old_close, created, NEW_YORK) is None


def test_new_version_chains_to_previous() -> None:
    thesis = active_thesis()
    later = new_version(thesis, "idea.status", "hard line crossed", state="invalidated")
    assert later.previous_id == thesis.id
    assert later.parents[0] == thesis.id
    assert later.state == "invalidated" and later.evidence == thesis.evidence
    assert later.created_at >= thesis.created_at


# --- Intake -------------------------------------------------------------------------------


def message(text_value: str) -> IntakeMessage:
    return IntakeMessage(produced_by="jon", runtime_ms=0, text=text_value)


def reply(**overrides: object) -> IntakeReply:
    fields: dict[str, object] = {
        "statement": "Long silver as real yields fall.",
        "instruments": ["slv", "/SI"],
        "direction": "long",
        "drivers": [
            {"statement": "Real yields keep falling", "metric": "DFII10", "source": "fred"}
        ],
        "hard_level": "28",
        "warning_level": "$29.50",
        "horizon": "weeks",
        "conviction": 4,
    }
    fields.update(overrides)
    return IntakeReply.model_validate(fields)


JON = (
    "Long SLV for a few weeks, real yields are rolling over. "
    "Out below 28, warn me at 29.50. Conviction 4."
)


def test_intake_check_numbers_come_from_jon() -> None:
    check = check_reply(JON)
    assert check(reply()) == []
    assert any("hard_level" in p for p in check(reply(hard_level="27")))
    assert any("conviction" in p for p in check(reply(conviction=5)))
    assert any("one price" in p for p in check(reply(warning_level="soon")))
    assert check(reply(hard_level="close below 28", warning_level="around 29.50")) == []
    assert any("one price" in p for p in check(reply(hard_level="28 or 29.50")))
    futures = check_reply(JON, frozenset({"/SI"}))
    assert futures(reply()) == []
    assert any("/SLV" in p for p in futures(reply(instruments=["/SLV"])))
    assert any("['35']" in p for p in check(reply(statement="Silver to 35.")))
    assert any("time_limit" in p for p in check(reply(time_limit="2026-10-30")))
    assert check_reply(JON + " Done by Oct 30.")(reply(time_limit="2026-10-30")) == []


def test_date_mentioned() -> None:
    assert date_mentioned(date(2026, 10, 30), "by October 30")
    assert date_mentioned(date(2026, 10, 30), "by 10/30")
    assert not date_mentioned(date(2026, 10, 30), "by the 30th of next month")


def test_complete_draft_has_no_questions() -> None:
    messages = [message(JON)]
    result = build_draft(reply(), messages, TODAY, Usage(model="small"), "intake.v1", 3, None)
    thesis = result.thesis
    assert result.missing == [] and thesis.state == "draft" and thesis.origin == "jon"
    assert thesis.instruments == ("SLV", "/SI")
    assert thesis.invalidation is not None
    assert thesis.invalidation.hard.level == Decimal("28")
    assert thesis.invalidation.hard.level_ref == f"intake_message:{messages[0].id}"
    assert thesis.review_by == review_by("weeks", TODAY)


def test_missing_fields_become_questions() -> None:
    messages = [message("Long SLV, real yields are rolling over.")]
    partial = reply(hard_level=None, warning_level=None, horizon=None, conviction=None)
    result = build_draft(partial, messages, TODAY, Usage(model="small"), "intake.v1", 3, None)
    assert result.missing == ["invalidation", "horizon", "conviction"]
    assert len(result.questions) == 3
    inverted = reply(hard_level="29.50", warning_level="28")
    result = build_draft(
        inverted, [message(JON)], TODAY, Usage(model="small"), "intake.v1", 3, None
    )
    assert "invalidation_order" in result.missing
    assert review(result.thesis).missing[0] == "invalidation"


def test_follow_up_draft_chains_to_previous() -> None:
    first = [message("Long SLV, real yields are rolling over.")]
    partial = reply(hard_level=None, warning_level=None, horizon=None, conviction=None)
    earlier = build_draft(partial, first, TODAY, Usage(model="small"), "intake.v1", 3, None).thesis
    follow = [*first, message("Out below 28, warn at 29.50, weeks, conviction 4")]
    later = build_draft(reply(), follow, TODAY, Usage(model="small"), "intake.v1", 3, earlier)
    assert later.thesis.previous_id == earlier.id
    assert later.missing == []
    assert later.thesis.invalidation is not None
    assert later.thesis.invalidation.hard.level_ref == f"intake_message:{follow[1].id}"
    assert datetime.now(UTC) >= later.thesis.created_at


def test_briefing_thesis_line_uses_stored_values() -> None:
    line = thesis_line(active_thesis())
    assert line.startswith("SLV long: Silver keeps its breakout")
    assert "daily close below $28.00" in line and "warning below $29.20" in line
    assert "Conviction 3/5." in line
