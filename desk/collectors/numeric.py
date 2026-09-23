"""Numeric series collectors: FRED macro series, EIA weekly inventories, CFTC COT positioning.

Values land in series_observations (permanent), not the 7-day raw-record buffer. FRED and
EIA take the API key as a query parameter, so every error message goes through
describe_http_error, which drops the query string.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from desk.collectors.base import CollectResult, SeriesObservation, describe_http_error, make_client
from desk.config import CotSources, EiaSources, FredSources

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
EIA_SERIES_URL = "https://api.eia.gov/v2/seriesid/{series_id}"
CFTC_URL = "https://publicreporting.cftc.gov/resource/{dataset}.json"


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class FredCollector:
    name = "fred"

    def __init__(
        self,
        api_key: str,
        sources: FredSources,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = make_client(
            params={"api_key": api_key, "file_type": "json"}, transport=transport
        )
        self._sources = sources

    async def collect(self) -> CollectResult:
        result = CollectResult()
        start = datetime.now(UTC).date() - timedelta(days=self._sources.lookback_days)
        fetched_at = datetime.now(UTC)
        for series_id in self._sources.series:
            try:
                response = await self._http.get(
                    FRED_URL,
                    params={"series_id": series_id, "observation_start": start.isoformat()},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                result.errors.append(f"{series_id}: {describe_http_error(exc)}")
                continue
            for row in response.json().get("observations", []):
                value = _decimal(row.get("value"))
                if value is None:  # FRED marks missing days with "."
                    continue
                result.observations.append(
                    SeriesObservation(
                        source="fred",
                        series_id=series_id,
                        period=date.fromisoformat(row["date"]),
                        value=value,
                        unit=None,
                        fetched_at=fetched_at,
                    )
                )
        return result

    async def aclose(self) -> None:
        await self._http.aclose()


class EiaCollector:
    name = "eia"

    def __init__(
        self,
        api_key: str,
        sources: EiaSources,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = make_client(params={"api_key": api_key}, transport=transport)
        self._sources = sources

    async def collect(self) -> CollectResult:
        result = CollectResult()
        since = datetime.now(UTC).date() - timedelta(weeks=self._sources.lookback_weeks)
        fetched_at = datetime.now(UTC)
        for series_id in self._sources.series:
            try:
                response = await self._http.get(EIA_SERIES_URL.format(series_id=series_id))
                response.raise_for_status()
            except httpx.HTTPError as exc:
                result.errors.append(f"{series_id}: {describe_http_error(exc)}")
                continue
            for row in response.json().get("response", {}).get("data", []):
                period = date.fromisoformat(str(row["period"])[:10])
                value = _decimal(row.get("value"))
                if period < since or value is None:
                    continue
                result.observations.append(
                    SeriesObservation(
                        source="eia",
                        series_id=series_id,
                        period=period,
                        value=value,
                        unit=row.get("units"),
                        fetched_at=fetched_at,
                    )
                )
        return result

    async def aclose(self) -> None:
        await self._http.aclose()


class CftcCotCollector:
    name = "cftc_cot"

    def __init__(
        self, sources: CotSources, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._http = make_client(transport=transport)
        self._sources = sources

    async def collect(self) -> CollectResult:
        since = datetime.now(UTC).date() - timedelta(weeks=self._sources.lookback_weeks)
        codes = ", ".join(f"'{code}'" for code in self._sources.contracts)
        response = await self._http.get(
            CFTC_URL.format(dataset=self._sources.dataset),
            params={
                "$where": (
                    f"cftc_contract_market_code in({codes}) "
                    f"AND report_date_as_yyyy_mm_dd >= '{since.isoformat()}T00:00:00'"
                ),
                "$limit": "5000",
            },
        )
        response.raise_for_status()
        fetched_at = datetime.now(UTC)
        observations = []
        for row in response.json():
            period = date.fromisoformat(row["report_date_as_yyyy_mm_dd"][:10])
            code = row["cftc_contract_market_code"]
            for field in self._sources.fields:
                value = _decimal(row.get(field))
                if value is None:
                    continue
                observations.append(
                    SeriesObservation(
                        source="cftc_cot",
                        series_id=f"{code}.{field}",
                        period=period,
                        value=value,
                        unit="contracts",
                        fetched_at=fetched_at,
                    )
                )
        return CollectResult(observations=observations)

    async def aclose(self) -> None:
        await self._http.aclose()
