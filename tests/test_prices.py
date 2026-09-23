import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from desk.collectors import prices
from desk.collectors.base import CollectResult, PriceBar, QuoteSnapshot
from desk.collectors.ingest import ingest
from desk.collectors.prices import TastytradeMetrics, YahooDailyBars, frame_to_bars
from desk.collectors.tastytrade_stream import CandleBook, CandleUpdate, QuoteBook
from desk.collectors.universe import parse_constituents
from desk.symbols import from_tastytrade, to_tastytrade, to_yahoo

NY = ZoneInfo("America/New_York")
TODAY = date(2026, 9, 23)


# --- Symbols and universe -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("canonical", "yahoo", "tastytrade"),
    [
        ("NVDA", "NVDA", "NVDA"),
        ("BRK.B", "BRK-B", "BRK/B"),
        ("/GC", "GC=F", "/GC"),
        ("^VIX", "^VIX", "^VIX"),
        ("DX-Y.NYB", "DX-Y.NYB", "DX-Y/NYB"),
    ],
)
def test_symbol_spellings(canonical: str, yahoo: str, tastytrade: str) -> None:
    assert to_yahoo(canonical) == yahoo
    if canonical != "DX-Y.NYB":  # Yahoo-only symbol; never sent to tastytrade
        assert to_tastytrade(canonical) == tastytrade
        assert from_tastytrade(tastytrade) == canonical


def test_parse_constituents_reads_only_the_constituents_table() -> None:
    html = """
    <table id="other"><tr><td>NOPE</td></tr></table>
    <table class="wikitable" id="constituents">
      <tr><th>Symbol</th><th>Security</th></tr>
      <tr><td><a href="x">MMM</a></td><td>3M</td></tr>
      <tr><td><a href="y">BRK.B</a>
      </td><td>Berkshire Hathaway</td></tr>
      <tr><td>AAPL</td><td><table><tr><td>nested</td></tr></table></td></tr>
    </table>
    """
    assert parse_constituents(html) == ["AAPL", "BRK.B", "MMM"]


# --- Yahoo daily bars ---------------------------------------------------------------------


def yahoo_frame(data: dict[str, list[tuple[str, float | None]]]) -> pd.DataFrame:
    """Multi-ticker frame shaped like yf.download(group_by="ticker")."""
    dates = sorted({d for rows in data.values() for d, _ in rows})
    index = pd.DatetimeIndex([pd.Timestamp(d) for d in dates], name="Date")
    columns = pd.MultiIndex.from_product(
        [list(data), ["Open", "High", "Low", "Close", "Adj Close", "Volume"]]
    )
    frame = pd.DataFrame(index=index, columns=columns, dtype=float)
    for ticker, rows in data.items():
        for day, close in rows:
            if close is None:
                continue
            frame.loc[pd.Timestamp(day), ticker] = [close, close + 1, close - 1, close, close, 1000]
    return frame


def test_frame_to_bars_skips_today_and_missing_rows() -> None:
    frame = yahoo_frame(
        {
            "GC=F": [("2026-09-21", 4383.9), ("2026-09-22", 4395.5), ("2026-09-23", 4400.0)],
            "BRK-B": [("2026-09-21", None), ("2026-09-22", 480.25)],
        }
    )
    bars, empty = frame_to_bars(frame, {"GC=F": "/GC", "BRK-B": "BRK.B", "XYZ": "XYZ"}, TODAY, NY)
    assert empty == {"XYZ"}
    assert [(b.symbol, b.ts.astimezone(NY).date()) for b in bars] == [
        ("/GC", date(2026, 9, 21)),
        ("/GC", date(2026, 9, 22)),
        ("BRK.B", date(2026, 9, 22)),
    ]
    assert bars[0].close == Decimal("4383.9000")
    assert bars[0].ts == datetime(2026, 9, 21, 4, 0, tzinfo=UTC)  # midnight ET during EDT


def test_yahoo_collector_backfills_new_symbols_and_retries_empties() -> None:
    calls: list[tuple[tuple[str, ...], str]] = []

    def download(symbols: list[str], period: str) -> pd.DataFrame:
        calls.append((tuple(symbols), period))
        if symbols == ["NEW"] and len(calls) == 1:  # first attempt drops the ticker
            return yahoo_frame({"OTHER": [("2026-09-22", 1.0)]})
        return yahoo_frame({s: [("2026-09-22", 10.0)] for s in symbols if s != "GONE"})

    collector = YahooDailyBars(
        symbols=lambda: ("NEW", "OLD", "GONE"),
        latest_bar_dates=lambda: {"OLD": date(2026, 9, 21), "GONE": date(2026, 9, 1)},
        download=download,
        tz=NY,
        clock=lambda: datetime(2026, 9, 23, 20, 0, tzinfo=UTC),
    )
    result = asyncio.run(collector.collect())
    assert calls[0] == (("NEW",), prices.BACKFILL_PERIOD)
    assert calls[1] == (("NEW",), prices.BACKFILL_PERIOD)  # retry of the dropped ticker
    assert (("OLD", "GONE"), prices.UPDATE_PERIOD) in calls
    assert sorted(b.symbol for b in result.bars) == ["NEW", "OLD"]
    assert result.errors == ["no Yahoo data for: GONE"]


# --- tastytrade metrics -------------------------------------------------------------------


class FakeConnection:
    async def session(self) -> object:
        return object()


def test_metrics_become_observations(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[list[str]] = []

    async def fake_metrics(session: object, symbols: list[str]) -> list[Any]:
        requested.append(symbols)
        return [
            SimpleNamespace(
                symbol="BRK/B",
                implied_volatility_index=Decimal("0.18"),
                implied_volatility_index_5_day_change=None,
                tw_implied_volatility_index_rank=Decimal("0.42"),
                implied_volatility_percentile="0.30",
                implied_volatility_30_day=Decimal("18.1"),
                historical_volatility_30_day=None,
                historical_volatility_90_day=None,
                liquidity_rating=3,
            )
        ]

    monkeypatch.setattr(prices, "get_market_metrics", fake_metrics)
    collector = TastytradeMetrics(FakeConnection(), lambda: ("BRK.B", "/GC"), NY)  # type: ignore[arg-type]
    result = asyncio.run(collector.collect())
    assert requested == [["BRK/B", "/GC"]]
    values = {o.series_id: o.value for o in result.observations}
    assert values == {
        "BRK.B.implied_volatility_index": Decimal("0.18"),
        "BRK.B.tw_implied_volatility_index_rank": Decimal("0.42"),
        "BRK.B.implied_volatility_percentile": Decimal("0.30"),
        "BRK.B.implied_volatility_30_day": Decimal("18.1"),
        "BRK.B.liquidity_rating": Decimal("3"),
    }
    assert result.errors == []


# --- Stream bookkeeping -------------------------------------------------------------------


def candle(minute: datetime, close: str, symbol: str = "SPY") -> CandleUpdate:
    price = Decimal(close)
    return CandleUpdate(
        symbol, int(minute.timestamp() * 1000), price, price, price, price, Decimal(5)
    )


def test_candle_book_releases_only_final_minutes_with_latest_values() -> None:
    book = CandleBook()
    t0 = datetime(2026, 9, 23, 14, 30, tzinfo=UTC)
    book.update(candle(t0, "100.00"))
    book.update(candle(t0, "100.50"))  # same minute updated
    book.update(candle(t0 + timedelta(minutes=1), "101.00"))
    book.update(candle(t0 + timedelta(minutes=1), "0", symbol="QQQ"))  # empty candle ignored

    assert book.drain_completed(t0 + timedelta(seconds=70)) == []  # inside grace period
    drained = book.drain_completed(t0 + timedelta(seconds=76))
    assert [(b.ts, b.close) for b in drained] == [(t0, Decimal("100.50"))]
    later = book.drain_completed(t0 + timedelta(minutes=5))
    assert [b.close for b in later] == [Decimal("101.00")]
    assert book.drain_completed(t0 + timedelta(minutes=10)) == []


def test_quote_book_keeps_latest_per_symbol() -> None:
    book = QuoteBook()
    stamp = datetime(2026, 9, 23, 14, 30, tzinfo=UTC)
    for bid in ("1.00", "1.01"):
        book.update(QuoteSnapshot("SPY", "tastytrade", Decimal(bid), None, None, None, stamp))
    assert [q.bid for q in book.drain()] == [Decimal("1.01")]
    assert book.drain() == []


# --- Database writes ----------------------------------------------------------------------


def bar(symbol: str, ts: datetime, close: str) -> PriceBar:
    price = Decimal(close)
    return PriceBar("yahoo", symbol, "1d", ts, price, price, price, price, None, datetime.now(UTC))


def test_bars_insert_once_and_are_append_only(db_conn: Connection) -> None:
    ts = datetime(2026, 9, 22, 4, tzinfo=UTC)
    first = ingest(db_conn, CollectResult(bars=[bar("TSTA", ts, "10"), bar("TSTB", ts, "20")]))
    assert first.bars_added == 2
    again = ingest(db_conn, CollectResult(bars=[bar("TSTA", ts, "11")]))
    assert again.bars_added == 0
    stored = db_conn.execute(
        text("SELECT close FROM price_bars WHERE symbol = 'TSTA'")
    ).scalar_one()
    assert stored == Decimal("10")
    with pytest.raises(DBAPIError, match="append-only"):
        db_conn.execute(text("UPDATE price_bars SET close = 0 WHERE symbol = 'TSTA'"))


def test_quotes_never_move_backwards(db_conn: Connection) -> None:
    newer = datetime(2026, 9, 23, 14, 31, tzinfo=UTC)
    older = newer - timedelta(minutes=1)

    def quote(bid: str, at: datetime) -> QuoteSnapshot:
        return QuoteSnapshot("TSTQ", "tastytrade", Decimal(bid), None, None, None, at)

    assert ingest(db_conn, CollectResult(quotes=[quote("2.00", newer)])).quotes_updated == 1
    assert ingest(db_conn, CollectResult(quotes=[quote("1.00", older)])).quotes_updated == 0
    bid = db_conn.execute(text("SELECT bid FROM quotes_latest WHERE symbol = 'TSTQ'")).scalar_one()
    assert bid == Decimal("2.00")
