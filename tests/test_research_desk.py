from uuid import uuid4

from desk.desks.research import (
    DraftClaim,
    DraftNumber,
    DraftSection,
    ResearchDraft,
    Source,
    check_draft,
    combine_importance,
)

FILING = (
    "Acme Corp announced third quarter revenue of $4.2 billion, up 12% from a year earlier. "
    "The board approved a buyback of up to $500 million."
)


def sources() -> list[Source]:
    return [Source(uuid4(), "edgar.filing_text 8-K", FILING)]


def revenue_claim(**overrides: object) -> DraftClaim:
    fields: dict[str, object] = {
        "statement": "Quarterly revenue was $4.2 billion, up 12%.",
        "source_index": 1,
        "quote": "third quarter revenue of $4.2 billion, up 12% from a year earlier",
        "numbers": [
            DraftNumber(name="revenue", text="$4.2 billion", kind="reported"),
            DraftNumber(name="growth", text="12%", kind="reported"),
        ],
    }
    fields.update(overrides)
    return DraftClaim.model_validate(fields)


def draft(*claims: DraftClaim, section: str = "Revenue grew [c1].") -> ResearchDraft:
    return ResearchDraft(
        sections=[DraftSection(title="What happened", text=section)], claims=list(claims)
    )


def test_combine_importance_has_diminishing_returns() -> None:
    assert combine_importance([]) == 0.0
    assert combine_importance([0.5, 0.5]) == 0.75
    assert combine_importance([1.5]) == 1.0


def test_clean_draft_passes() -> None:
    check = check_draft(sources())
    assert check(draft(revenue_claim(), section="Revenue of $4.2 billion [c1].")) == []


def test_paraphrased_quote_is_rejected() -> None:
    claim = revenue_claim(quote="revenue rose 12% to $4.2 billion")
    problems = check_draft(sources())(draft(claim))
    assert any("word for word" in p for p in problems)


def test_spacing_differences_still_match() -> None:
    claim = revenue_claim(quote="third  quarter revenue of $4.2 billion,\nup 12%")
    assert check_draft(sources())(draft(claim)) == []


def test_number_missing_from_quote_is_rejected() -> None:
    claim = revenue_claim(
        numbers=[
            DraftNumber(name="revenue", text="$4.3 billion", kind="reported"),
            DraftNumber(name="growth", text="12%", kind="reported"),
        ]
    )
    problems = check_draft(sources())(draft(claim))
    assert any("$4.3 billion" in p for p in problems)


def test_statement_number_must_be_listed() -> None:
    claim = revenue_claim(numbers=[DraftNumber(name="growth", text="12%", kind="reported")])
    problems = check_draft(sources())(draft(claim))
    assert any("statement numbers ['4.2']" in p for p in problems)


def test_bad_source_index_and_market_move_without_date() -> None:
    bad_index = revenue_claim(source_index=2)
    no_date = revenue_claim(
        numbers=[
            DraftNumber(name="revenue", text="$4.2 billion", kind="reported"),
            DraftNumber(name="growth", text="12%", kind="market_move", symbol="ACME"),
        ]
    )
    problems = check_draft(sources())(draft(bad_index, no_date, section="Two claims [c2]."))
    assert any("source_index 2" in p for p in problems)
    assert any("needs symbol and as_of" in p for p in problems)


def test_section_numbers_must_come_from_cited_quotes() -> None:
    check = check_draft(sources())
    assert any("['15']" in p for p in check(draft(revenue_claim(), section="Up 15% [c1].")))
    # A number from a quote the section does not cite is still new to that section.
    assert any("['12']" in p for p in check(draft(revenue_claim(), section="Up 12%.")))
