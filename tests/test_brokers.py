"""Broker snapshots. All account numbers and holdings below are invented."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from desk.collectors.base import AccountSnapshot, CollectResult, PositionRow, account_ref
from desk.collectors.brokers import (
    FlexError,
    IbkrFlexPositions,
    parse_flex_statement,
    tastytrade_snapshot,
)
from desk.collectors.holdings import HeldSymbols, held_symbols
from desk.collectors.ingest import ingest

NY = ZoneInfo("America/New_York")

FLEX_XML = b"""<FlexQueryResponse queryName="flex1" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U9876543" fromDate="20260921" toDate="20260921" period="LastBusinessDay">
<EquitySummaryInBase>
  <EquitySummaryByReportDateInBase reportDate="20260918" total="4500.00" cash="200.00"/>
  <EquitySummaryByReportDateInBase reportDate="20260921" total="4700.50" cash="222.81"/>
</EquitySummaryInBase>
<CashReport>
  <CashReportCurrency currency="BASE_SUMMARY" endingCash="222.81" endingSettledCash="200.00"/>
  <CashReportCurrency currency="USD" endingCash="222.81" endingSettledCash="200.00"/>
</CashReport>
<OpenPositions>
  <OpenPosition symbol="XYZ" assetCategory="STK" position="10" markPrice="120.03"
    positionValue="1200.3" costBasisMoney="1390.5" currency="USD" multiplier="1"
    description="XYZ CORP" levelOfDetail="SUMMARY"/>
  <OpenPosition symbol="XYZ" assetCategory="STK" position="10" markPrice="120.03"
    positionValue="1200.3" costBasisMoney="1390.5" currency="USD" multiplier="1"
    description="XYZ CORP" levelOfDetail="LOT"/>
  <OpenPosition symbol="BRK B" assetCategory="STK" position="2" markPrice="480"
    positionValue="960" costBasisMoney="900" currency="USD" multiplier="1"
    description="BERKSHIRE HATHAWAY INC-CL B" levelOfDetail="SUMMARY"/>
  <OpenPosition symbol="XYZ   261218C00130000" underlyingSymbol="XYZ" assetCategory="OPT"
    position="1" markPrice="2.10" positionValue="210" costBasisMoney="150" currency="USD"
    multiplier="100" description="XYZ 18DEC26 130 C" levelOfDetail="SUMMARY"/>
</OpenPositions>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""


def test_parse_flex_statement() -> None:
    [snapshot] = parse_flex_statement(FLEX_XML, NY)
    assert snapshot.account_ref == "ibkr:6543"
    assert snapshot.as_of == datetime(2026, 9, 21, 20, 0, tzinfo=UTC)  # 16:00 ET
    assert snapshot.net_liquidation == Decimal("4700.50")
    assert (snapshot.cash, snapshot.settled_cash) == (Decimal("222.81"), Decimal("200.00"))
    by_contract = {p.contract: p for p in snapshot.positions}
    assert len(snapshot.positions) == 3  # lot row skipped
    assert by_contract["BERKSHIRE HATHAWAY INC-CL B"].symbol == "BRK.B"
    option = by_contract["XYZ 18DEC26 130 C"]
    assert (option.symbol, option.asset_class, option.multiplier) == ("XYZ", "option", 100)
    assert option.avg_cost == Decimal("1.5")
    assert by_contract["XYZ CORP"].avg_cost == Decimal("139.05")


def flex_transport(statement_responses: list[bytes]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["t"] == "123456789012345678901234"
        if request.url.path.endswith("SendRequest"):
            assert request.url.params["q"] == "7654321"
            return httpx.Response(
                200,
                content=(
                    b"<FlexStatementResponse><Status>Success</Status>"
                    b"<ReferenceCode>555</ReferenceCode>"
                    b"<Url>https://ndcdyn.interactivebrokers.com/x/GetStatement</Url>"
                    b"</FlexStatementResponse>"
                ),
            )
        assert request.url.params["q"] == "555"
        return httpx.Response(200, content=statement_responses.pop(0))

    return httpx.MockTransport(handler)


IN_PROGRESS = (
    b"<FlexStatementResponse><Status>Warn</Status><ErrorCode>1019</ErrorCode>"
    b"<ErrorMessage>Statement generation in progress.</ErrorMessage></FlexStatementResponse>"
)


def test_flex_collector_polls_until_statement_is_ready() -> None:
    collector = IbkrFlexPositions(
        "123456789012345678901234",
        "7654321",
        NY,
        transport=flex_transport([IN_PROGRESS, FLEX_XML]),
        poll_interval_s=0,
    )
    result = asyncio.run(collector.collect())
    assert [s.account_ref for s in result.accounts] == ["ibkr:6543"]


def test_flex_collector_raises_on_real_error_without_leaking_token() -> None:
    failure = (
        b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode>"
        b"<ErrorMessage>Token has expired.</ErrorMessage></FlexStatementResponse>"
    )
    collector = IbkrFlexPositions(
        "123456789012345678901234",
        "7654321",
        NY,
        transport=flex_transport([failure]),
        poll_interval_s=0,
    )
    with pytest.raises(FlexError) as excinfo:
        asyncio.run(collector.collect())
    assert "1012" in str(excinfo.value)
    assert "123456789012345678901234" not in str(excinfo.value)


def test_tastytrade_snapshot_signs_shorts_and_masks_account() -> None:
    account = SimpleNamespace(account_number="5WX12345")
    balances = SimpleNamespace(
        net_liquidating_value=Decimal("101.20"),
        cash_balance=Decimal("80.00"),
        cash_available_to_withdraw=Decimal("75.00"),
        equity_buying_power=Decimal("80.00"),
    )
    positions = [
        SimpleNamespace(
            symbol="SPY   261218P00500000",
            underlying_symbol="SPY",
            instrument_type=SimpleNamespace(value="Equity Option"),
            quantity=Decimal(1),
            quantity_direction="Short",
            mark_price=Decimal("1.10"),
            close_price=Decimal("1.00"),
            average_open_price=Decimal("1.30"),
            multiplier=Decimal(100),
        ),
        SimpleNamespace(
            symbol="BRK/B",
            underlying_symbol="BRK/B",
            instrument_type=SimpleNamespace(value="Equity"),
            quantity=Decimal(1),
            quantity_direction="Long",
            mark_price=None,
            close_price=Decimal("480.00"),
            average_open_price=Decimal("470.00"),
            multiplier=Decimal(1),
        ),
    ]
    snapshot = tastytrade_snapshot(account, balances, positions)
    assert snapshot.account_ref == "tastytrade:2345"
    option, stock = snapshot.positions
    assert (option.symbol, option.asset_class, option.quantity) == ("SPY", "option", -1)
    assert option.market_value == Decimal("-110.00")
    assert (stock.symbol, stock.mark_price) == ("BRK.B", Decimal("480.00"))


# --- Database -----------------------------------------------------------------------------


def snapshot(ref: str, as_of: datetime, symbols: dict[str, str]) -> AccountSnapshot:
    return AccountSnapshot(
        source="ibkr_flex",
        broker="ibkr",
        account_ref=ref,
        as_of=as_of,
        fetched_at=datetime.now(UTC),
        net_liquidation=Decimal(100),
        cash=Decimal(10),
        settled_cash=Decimal(10),
        buying_power=None,
        currency="USD",
        positions=tuple(
            PositionRow(
                symbol, symbol, "equity", Decimal(qty), Decimal(1), None, None, None, None, "USD"
            )
            for symbol, qty in symbols.items()
        ),
    )


def test_snapshot_dedupes_on_as_of_and_is_append_only(db_conn: Connection) -> None:
    ref = f"ibkr:{uuid4().hex[:4]}"
    as_of = datetime(2026, 9, 21, 20, tzinfo=UTC)
    first = ingest(db_conn, CollectResult(accounts=[snapshot(ref, as_of, {"AAA": "1"})]))
    assert first.snapshots_added == 1
    again = ingest(db_conn, CollectResult(accounts=[snapshot(ref, as_of, {"AAA": "1"})]))
    assert again.snapshots_added == 0
    with pytest.raises(DBAPIError, match="append-only"):
        db_conn.execute(text("UPDATE position_snapshots SET quantity = 0"))


def test_held_symbols_use_only_each_accounts_latest_snapshot(db_conn: Connection) -> None:
    ref = f"ibkr:{uuid4().hex[:4]}"
    older = datetime(2026, 9, 18, 20, tzinfo=UTC)
    newer = datetime(2026, 9, 21, 20, tzinfo=UTC)
    ingest(
        db_conn,
        CollectResult(
            accounts=[
                snapshot(ref, older, {"SOLDX": "5", "KEEPX": "1"}),
                snapshot(ref, newer, {"KEEPX": "1", "CLOSEDX": "0", "NEWX": "3"}),
            ]
        ),
    )
    held = held_symbols(db_conn)
    assert {"KEEPX", "NEWX"} <= set(held)
    assert not {"SOLDX", "CLOSEDX"} & set(held)


def test_account_ref_keeps_last_four_only() -> None:
    assert account_ref("ibkr", "U1234567") == "ibkr:4567"


def test_held_symbols_cache_refreshes_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    from desk.collectors import holdings

    loads: list[int] = []
    monkeypatch.setattr(holdings, "held_symbols", lambda conn: (loads.append(1), ("AAA",))[1])

    class FakeEngine:
        def connect(self) -> object:
            class Ctx:
                def __enter__(self) -> None:
                    return None

                def __exit__(self, *args: object) -> None:
                    return None

            return Ctx()

    now = [0.0]
    cache = HeldSymbols(FakeEngine(), clock=lambda: now[0])  # type: ignore[arg-type]
    assert cache() == ("AAA",)
    now[0] = 100
    cache()
    assert len(loads) == 1
    now[0] = 1000
    cache()
    assert len(loads) == 2
