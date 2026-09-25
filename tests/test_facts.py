from decimal import Decimal

import pytest

from desk.llm.facts import Fact, FactTable, UnknownFactError


def table() -> FactTable:
    return FactTable(
        [
            Fact(
                "AEP.change_pct",
                Decimal("-1.23"),
                "%",
                "-1.23%",
                "AEP day change",
                "computed:quotes_latest:AEP/price_bars:yahoo:AEP:1d",
            ),
            Fact(
                "AEP.value",
                Decimal("1200.30"),
                "USD",
                "$1,200.30",
                "AEP position value",
                "position_snapshots:ibkr:0000",
            ),
            Fact(
                "DGS10.last",
                Decimal("4.96"),
                "%",
                "4.96%",
                "10-year Treasury yield",
                "series_observations:fred:DGS10:2026-09-21",
            ),
            Fact(
                "news_1",
                "Utility stocks slide as 10-year yield climbs",
                "",
                "Utility stocks slide as 10-year yield climbs",
                "headline",
                "raw_record:abc",
            ),
        ]
    )


def test_render_replaces_placeholders_with_code_formatted_values() -> None:
    text = "AEP moved {AEP.change_pct} to {AEP.value}."
    assert table().render(text) == "AEP moved -1.23% to $1,200.30."


def test_render_rejects_unknown_placeholder() -> None:
    with pytest.raises(UnknownFactError):
        table().render("Gold rose {GC.change_pct}.")


def test_clean_prose_passes() -> None:
    text = "The {DGS10.last} 10-year yield weighed on utilities; see {news_1}."
    assert table().violations(text) == []


def test_bare_number_from_the_model_is_rejected() -> None:
    problems = table().violations("AEP fell 1.2% overnight to about $1,200.")
    assert any("1.2" in p for p in problems)
    assert any("1,200" in p for p in problems)


def test_numbers_inside_fact_labels_are_allowed_as_words() -> None:
    # "10-year" appears in a fact label, so the model may name the instrument.
    assert table().violations("The 10-year yield rose to {DGS10.last}.") == []


def test_unknown_placeholder_is_a_violation() -> None:
    problems = table().violations("Gold rose {GC.change_pct}.")
    assert problems == ["unknown fact {GC.change_pct}"]


def test_numbers_in_placeholder_ids_are_not_counted() -> None:
    assert table().violations("{news_1}") == []


def test_duplicate_fact_ids_rejected() -> None:
    fact = Fact("x", Decimal(1), "", "1", "x", "ref")
    with pytest.raises(ValueError, match="duplicate"):
        FactTable([fact, fact])


def test_fact_needs_source() -> None:
    with pytest.raises(ValueError, match="source"):
        Fact("x", Decimal(1), "", "1", "x", "")


def test_render_normalizes_typographic_hyphens_and_spaces() -> None:
    from desk.llm.facts import FactTable

    table = FactTable([])
    assert table.render("20" + chr(0x2011) + "day" + chr(0x202F) + "range") == "20-day range"


def test_bare_fact_ids_are_rejected() -> None:
    from desk.llm.facts import Fact, FactTable

    table = FactTable(
        [
            Fact("claim_3", "s", "", "Orders rose.", "verified claim", "vc:1"),
            Fact("lvl_2", 1, "USD", "$1.00", "X last close plus 3 ATR", "levels:x"),
        ]
    )
    assert any("{claim_3}" in p for p in table.violations("Grounded in the break (claim_3)."))
    assert table.violations("Grounded in the break {claim_3}.") == []
