"""Read-only broker snapshots: IBKR via the Flex Web Service, tastytrade via its API.

Nothing here can place or change orders. The Flex token travels in the query string, so
every HTTP error goes through describe_http_error, and account numbers are reduced to
"<broker>:<last four>" before anything leaves this module.

The IBKR Flex statement covers the last completed business day, which makes it the
reconciled source of truth for the Roth; tastytrade balances and positions are live.
"""

import asyncio
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from tastytrade import Account

from desk.collectors.base import (
    AccountSnapshot,
    CollectResult,
    PositionRow,
    account_ref,
    make_client,
)
from desk.collectors.tastytrade_session import TastytradeConnection
from desk.symbols import from_tastytrade

FLEX_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
FLEX_POLL_INTERVAL_S = 5
FLEX_POLL_ATTEMPTS = 24
# Flex reports NAV at the close of the statement's last day.
MARKET_CLOSE = time(16, 0)

IBKR_ASSET_CLASSES = {
    "STK": "equity",
    "OPT": "option",
    "FUT": "future",
    "FOP": "future_option",
    "CRYPTO": "crypto",
}
TASTYTRADE_ASSET_CLASSES = {
    "Equity": "equity",
    "Equity Option": "option",
    "Future": "future",
    "Future Option": "future_option",
    "Cryptocurrency": "crypto",
}


class FlexError(RuntimeError):
    pass


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _ibkr_symbol(element: ET.Element) -> str:
    """Canonical underlying: IBKR writes share classes with a space ("BRK B")."""
    category = element.get("assetCategory", "")
    if category in ("OPT", "FOP"):
        raw = element.get("underlyingSymbol") or element.get("symbol", "")
    else:
        raw = element.get("symbol", "")
    symbol = raw.strip().replace(" ", ".")
    return f"/{symbol}" if category in ("FUT", "FOP") and not symbol.startswith("/") else symbol


def parse_flex_statement(xml_bytes: bytes, tz: ZoneInfo) -> list[AccountSnapshot]:
    root = ET.fromstring(xml_bytes)  # noqa: S314 - IBKR response over TLS, no external entities
    fetched_at = datetime.now(UTC)
    snapshots = []
    for statement in root.iter("FlexStatement"):
        account_number = statement.get("accountId", "")
        raw_to_date = statement.get("toDate", "")
        to_date = date(int(raw_to_date[:4]), int(raw_to_date[4:6]), int(raw_to_date[6:8]))
        nav_rows = [
            row
            for row in statement.iter("EquitySummaryByReportDateInBase")
            if row.get("reportDate") == to_date.strftime("%Y%m%d")
        ]
        nav = nav_rows[-1] if nav_rows else None
        cash_row = next(
            (
                c
                for c in statement.iter("CashReportCurrency")
                if c.get("currency") == "BASE_SUMMARY"
            ),
            None,
        )
        positions = []
        for element in statement.iter("OpenPosition"):
            # Lot-level rows repeat the summary row; only summaries describe the holding.
            if element.get("levelOfDetail", "SUMMARY") != "SUMMARY":
                continue
            quantity = _decimal(element.get("position")) or Decimal(0)
            multiplier = _decimal(element.get("multiplier")) or Decimal(1)
            cost_basis = _decimal(element.get("costBasisMoney"))
            positions.append(
                PositionRow(
                    symbol=_ibkr_symbol(element),
                    contract=element.get("description") or element.get("symbol", ""),
                    asset_class=IBKR_ASSET_CLASSES.get(element.get("assetCategory", ""), "other"),
                    quantity=quantity,
                    multiplier=multiplier,
                    avg_cost=cost_basis / (quantity * multiplier)
                    if cost_basis is not None and quantity
                    else None,
                    cost_basis=cost_basis,
                    mark_price=_decimal(element.get("markPrice")),
                    market_value=_decimal(element.get("positionValue")),
                    currency=element.get("currency", "USD"),
                )
            )
        snapshots.append(
            AccountSnapshot(
                source="ibkr_flex",
                broker="ibkr",
                account_ref=account_ref("ibkr", account_number),
                as_of=datetime.combine(to_date, MARKET_CLOSE, tzinfo=tz).astimezone(UTC),
                fetched_at=fetched_at,
                net_liquidation=_decimal(nav.get("total")) if nav is not None else None,
                cash=_decimal(cash_row.get("endingCash")) if cash_row is not None else None,
                settled_cash=_decimal(cash_row.get("endingSettledCash"))
                if cash_row is not None
                else None,
                buying_power=None,
                currency="USD",
                positions=tuple(positions),
            )
        )
    return snapshots


class IbkrFlexPositions:
    name = "ibkr_flex"

    def __init__(
        self,
        token: str,
        query_id: str,
        tz: ZoneInfo,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval_s: float = FLEX_POLL_INTERVAL_S,
    ) -> None:
        self._token = token
        self._query_id = query_id
        self._tz = tz
        self._http = make_client(transport=transport)
        self._poll_interval_s = poll_interval_s

    async def _get_xml(self, url: str, reference: str) -> ET.Element:
        response = await self._http.get(url, params={"t": self._token, "q": reference, "v": "3"})
        response.raise_for_status()
        return ET.fromstring(response.content)  # noqa: S314 - see parse_flex_statement

    async def collect(self) -> CollectResult:
        request = await self._get_xml(f"{FLEX_BASE}/SendRequest", self._query_id)
        if request.findtext("Status") != "Success":
            raise FlexError(
                f"SendRequest {request.findtext('ErrorCode')}: {request.findtext('ErrorMessage')}"
            )
        reference = request.findtext("ReferenceCode") or ""
        statement_url = request.findtext("Url") or f"{FLEX_BASE}/GetStatement"
        for _ in range(FLEX_POLL_ATTEMPTS):
            statement = await self._get_xml(statement_url, reference)
            if statement.tag != "FlexStatementResponse":
                return CollectResult(
                    accounts=parse_flex_statement(ET.tostring(statement), self._tz)
                )
            # 1019: statement generation in progress. Anything else is a real failure.
            if statement.findtext("ErrorCode") != "1019":
                raise FlexError(
                    f"GetStatement {statement.findtext('ErrorCode')}: "
                    f"{statement.findtext('ErrorMessage')}"
                )
            await asyncio.sleep(self._poll_interval_s)
        raise FlexError("statement not ready after polling")

    async def aclose(self) -> None:
        await self._http.aclose()


def tastytrade_snapshot(account: Any, balances: Any, positions: list[Any]) -> AccountSnapshot:
    now = datetime.now(UTC)
    rows = []
    for position in positions:
        sign = Decimal(-1) if position.quantity_direction == "Short" else Decimal(1)
        quantity = position.quantity * sign
        instrument_type = str(getattr(position.instrument_type, "value", position.instrument_type))
        underlying = position.underlying_symbol or position.symbol
        mark = position.mark_price if position.mark_price is not None else position.close_price
        multiplier = position.multiplier or Decimal(1)
        rows.append(
            PositionRow(
                symbol=from_tastytrade(underlying),
                contract=position.symbol,
                asset_class=TASTYTRADE_ASSET_CLASSES.get(instrument_type, "other"),
                quantity=quantity,
                multiplier=multiplier,
                avg_cost=position.average_open_price,
                cost_basis=position.average_open_price * quantity * multiplier,
                mark_price=mark,
                market_value=mark * quantity * multiplier if mark is not None else None,
                currency="USD",
            )
        )
    return AccountSnapshot(
        source="tastytrade",
        broker="tastytrade",
        account_ref=account_ref("tastytrade", account.account_number),
        as_of=now,
        fetched_at=now,
        net_liquidation=balances.net_liquidating_value,
        cash=balances.cash_balance,
        settled_cash=balances.cash_available_to_withdraw,
        buying_power=balances.equity_buying_power,
        currency="USD",
        positions=tuple(rows),
    )


class TastytradePositions:
    name = "tastytrade_positions"

    def __init__(self, connection: TastytradeConnection) -> None:
        self._connection = connection

    async def collect(self) -> CollectResult:
        session = await self._connection.session()
        snapshots = []
        for account in await Account.get(session):
            balances = await account.get_balances(session)
            positions = await account.get_positions(session)
            snapshots.append(tastytrade_snapshot(account, balances, positions))
        return CollectResult(accounts=snapshots)

    async def aclose(self) -> None:
        return None
