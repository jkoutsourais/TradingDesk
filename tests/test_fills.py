"""Fill capture. All account numbers, executions and prices below are invented."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from desk.collectors.base import CollectResult
from desk.collectors.brokers import parse_flex_trades, tastytrade_fills
from desk.collectors.ingest import ingest

NY = ZoneInfo("America/New_York")

TRADES_XML = b"""<FlexQueryResponse queryName="flex1" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U9876543" fromDate="20260924" toDate="20260924" period="LastBusinessDay">
<Trades>
  <Trade accountId="U9876543" assetCategory="STK" symbol="XYZ" description="XYZ CORP"
    buySell="BUY" quantity="10" tradePrice="30.50" ibCommission="-1.00" multiplier="1"
    dateTime="20260924;103015" ibExecID="0001.aa" tradeID="111" levelOfDetail="EXECUTION"/>
  <Trade accountId="U9876543" assetCategory="STK" symbol="XYZ" description="XYZ CORP"
    buySell="BUY" quantity="10" tradePrice="30.50" levelOfDetail="ORDER"
    dateTime="20260924;103015" tradeID="112"/>
  <Trade accountId="U9876543" assetCategory="OPT" symbol="XYZ   261218C00130000"
    underlyingSymbol="XYZ" description="XYZ 18DEC26 130 C" buySell="SELL" quantity="-1"
    tradePrice="2.10" ibCommission="-0.65" multiplier="100" dateTime="20260924;153000"
    ibExecID="0002.bb" levelOfDetail="EXECUTION"/>
</Trades>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""


def test_parse_flex_trades_keeps_executions_only() -> None:
    fills = parse_flex_trades(TRADES_XML, NY)
    assert [f.exec_id for f in fills] == ["0001.aa", "0002.bb"]
    share, option = fills
    assert share.account_ref == "ibkr:6543" and share.side == "buy"
    assert share.quantity == Decimal(10) and share.fees == Decimal("1.00")
    assert share.executed_at == datetime(2026, 9, 24, 14, 30, 15, tzinfo=UTC)
    assert option.symbol == "XYZ" and option.side == "sell" and option.quantity == Decimal(1)
    assert option.asset_class == "option" and option.multiplier == Decimal(100)


def test_flex_without_trades_section_yields_no_fills() -> None:
    xml = (
        b"<FlexQueryResponse><FlexStatements><FlexStatement accountId='U1'/>"
        b"</FlexStatements></FlexQueryResponse>"
    )
    assert parse_flex_trades(xml, NY) == []


def test_tastytrade_fills_skip_non_trades() -> None:
    trade = SimpleNamespace(
        id=5,
        exec_id="tt-1",
        transaction_type="Trade",
        action=SimpleNamespace(value="Sell to Close"),
        instrument_type=SimpleNamespace(value="Equity Option"),
        symbol="SPY   261218P00500000",
        underlying_symbol="SPY",
        quantity=Decimal(1),
        price=Decimal("1.10"),
        commission=Decimal("-1.00"),
        clearing_fees=Decimal("-0.10"),
        regulatory_fees=None,
        proprietary_index_option_fees=None,
        executed_at=datetime(2026, 9, 24, 15, tzinfo=UTC),
    )
    transfer = SimpleNamespace(transaction_type="Money Movement", quantity=None, price=None)
    fills = tastytrade_fills("5WX12345", [trade, transfer])
    assert len(fills) == 1
    fill = fills[0]
    assert fill.account_ref == "tastytrade:2345" and fill.side == "sell"
    assert fill.fees == Decimal("1.10") and fill.multiplier == Decimal(100)


def test_ingest_dedupes_fills_on_exec_id(db_conn: Connection) -> None:
    fills = parse_flex_trades(TRADES_XML, NY)
    ingest(db_conn, CollectResult(fills=fills))
    ingest(db_conn, CollectResult(fills=parse_flex_trades(TRADES_XML, NY)))
    stored = db_conn.execute(
        text(
            "SELECT count(*) FROM artifacts WHERE kind = 'fill' AND payload->>'exec_id' LIKE '000%'"
        )
    ).scalar_one()
    assert stored == 2
