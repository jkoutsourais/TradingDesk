from decimal import Decimal

from desk.front_office.briefing import holding_line, holding_notes, sentence
from desk.llm.facts import Fact, FactTable


def _table() -> FactTable:
    return FactTable(
        [
            Fact("ABC.price", Decimal("12.5"), "USD", "12.50", "ABC price", "q"),
            Fact("ABC.change", Decimal("-1.2"), "%", "-1.20%", "ABC change", "q"),
            Fact("ABC.value", Decimal(250), "USD", "$250.00", "ABC position value", "p"),
            Fact("ABC.pnl", Decimal(-50), "USD", "-$50.00", "ABC gain or loss", "p"),
            Fact("ABC.pnl_pct", Decimal("-16.67"), "%", "-16.67%", "ABC gain or loss", "p"),
            Fact(
                "trig_1",
                "ABC broke below its 20-day range",
                "",
                "ABC broke below its 20-day range",
                "hit",
                "t",
            ),
            Fact("trig_2", "ABC up 3.7 ATR", "", "ABC up 3.7 ATR", "hit", "t"),
        ]
    )


def test_holding_line_labels_numbers_and_punctuates_the_note() -> None:
    line = holding_line(_table(), "ABC", "{trig_1} {trig_2}")
    assert line == (
        "ABC: 12.50, -1.20% since the prior close; position $250.00, unrealized -$50.00 "
        "(-16.67%). ABC broke below its 20-day range. ABC up 3.7 ATR."
    )


def test_holding_line_without_facts_or_note() -> None:
    assert holding_line(_table(), "XYZ") == "XYZ."


def test_holding_notes_key_by_ticker() -> None:
    notes = holding_notes(["ABC: quiet, no news.", "xyz - earnings beat"])
    assert notes == {"ABC": "quiet, no news.", "XYZ": "earnings beat"}


def test_sentence_capitalises_and_closes() -> None:
    assert sentence("hold; tighten near the low") == "Hold; tighten near the low."
    assert sentence("Done!") == "Done!"
    assert sentence("  ") == ""
