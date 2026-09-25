from uuid import uuid4

from desk.watch.triage import Item, TriageEntry, TriageReply, check_reply, salvage_reply


def items() -> list[Item]:
    return [
        Item(uuid4(), "trigger", "watch hit, tier 1: NVDA up 3.1 ATR from prior close"),
        Item(uuid4(), "news", "finnhub.company_news (AEP): Utility raises guidance"),
    ]


def reply(*entries: tuple[int, str]) -> TriageReply:
    return TriageReply(
        labels=[TriageEntry(index=i, label="relevant", reason=r) for i, r in entries]
    )


def test_valid_reply_passes() -> None:
    check = check_reply(items())
    assert check(reply((1, "Watchlist name moving 3.1 ATR"), (2, "Held utility guidance"))) == []


def test_tier_references_are_not_data() -> None:
    check = check_reply(items())
    assert check(reply((1, "Tier 1 watchlist move"), (2, "Held tier 0 name"))) == []


def test_invented_numbers_are_rejected() -> None:
    problems = check_reply(items())(reply((1, "Up 5 percent"), (2, "Guidance up 12%")))
    assert any("item 1" in p and "5" in p for p in problems)
    assert any("item 2" in p and "12" in p for p in problems)


def test_missing_or_extra_indexes_are_rejected() -> None:
    problems = check_reply(items())(reply((1, "ok"), (3, "ok")))
    assert any("exactly one entry" in p for p in problems)


def test_salvage_withholds_reasons_with_invented_numbers() -> None:
    salvaged = salvage_reply(items(), reply((1, "Watchlist name moving 3.1 ATR"), (2, "Up 12%")))
    assert salvaged is not None
    assert salvaged.labels[0].reason == "Watchlist name moving 3.1 ATR"
    assert salvaged.labels[1].label == "relevant"
    assert "withheld" in salvaged.labels[1].reason


def test_salvage_needs_every_index() -> None:
    assert salvage_reply(items(), reply((1, "ok"), (3, "ok"))) is None
