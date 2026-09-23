"""Federal Register: published documents and the public inspection desk (no key needed).

Public inspection lists documents a day or more before publication, which is the earliest
public view of executive orders and agency rules.
"""

from datetime import UTC, datetime
from typing import Any

import httpx

from desk.artifacts.raw_record import RawRecord, content_hash
from desk.collectors.base import CollectResult, make_client

BASE_URL = "https://www.federalregister.gov/api/v1"
DOCUMENT_FIELDS = (
    "document_number",
    "title",
    "type",
    "subtype",
    "abstract",
    "html_url",
    "publication_date",
    "signing_date",
    "executive_order_number",
    "agencies",
)


def _agency_names(item: dict[str, Any]) -> list[str]:
    return [agency.get("name") or agency.get("raw_name") for agency in item.get("agencies") or []]


def _parse_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def _type_tag(document_type: str | None) -> str:
    return (document_type or "unknown").lower().replace(" ", "_")


class FederalRegisterDocuments:
    name = "federal_register_documents"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = make_client(base_url=BASE_URL, transport=transport)

    async def collect(self) -> CollectResult:
        params: list[tuple[str, str]] = [("per_page", "100"), ("order", "newest")]
        params += [("fields[]", field) for field in DOCUMENT_FIELDS]
        response = await self._http.get("/documents.json", params=params)
        response.raise_for_status()
        records = []
        for item in response.json().get("results") or []:
            number = item["document_number"]
            records.append(
                RawRecord(
                    produced_by="data.federal_register_documents",
                    runtime_ms=0,
                    source="federal_register.document",
                    source_id=number,
                    url=item.get("html_url"),
                    fetched_at=datetime.now(UTC),
                    published_at=None,
                    tags=("policy", _type_tag(item.get("type"))),
                    payload={
                        "title": item.get("title"),
                        "type": item.get("type"),
                        "subtype": item.get("subtype"),
                        "abstract": item.get("abstract"),
                        "publication_date": item.get("publication_date"),
                        "signing_date": item.get("signing_date"),
                        "executive_order_number": item.get("executive_order_number"),
                        "agencies": _agency_names(item),
                    },
                    content_hash=content_hash("federal_register.document", number),
                )
            )
        return CollectResult(records=records)

    async def aclose(self) -> None:
        await self._http.aclose()


class FederalRegisterPublicInspection:
    name = "federal_register_public_inspection"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = make_client(base_url=BASE_URL, transport=transport)

    async def collect(self) -> CollectResult:
        response = await self._http.get("/public-inspection-documents/current.json")
        response.raise_for_status()
        records = []
        for item in response.json().get("results") or []:
            number = item["document_number"]
            records.append(
                RawRecord(
                    produced_by="data.federal_register_public_inspection",
                    runtime_ms=0,
                    source="federal_register.public_inspection",
                    source_id=number,
                    url=item.get("html_url"),
                    fetched_at=datetime.now(UTC),
                    published_at=_parse_timestamp(item.get("filed_at")),
                    tags=("policy", "public_inspection", _type_tag(item.get("type"))),
                    payload={
                        "title": item.get("title"),
                        "type": item.get("type"),
                        "subjects": [
                            s
                            for s in (item.get(f"subject_{i}") for i in (1, 2, 3))
                            if s is not None
                        ],
                        "publication_date": item.get("publication_date"),
                        "filing_type": item.get("filing_type"),
                        "agencies": _agency_names(item),
                    },
                    content_hash=content_hash("federal_register.public_inspection", number),
                )
            )
        return CollectResult(records=records)

    async def aclose(self) -> None:
        await self._http.aclose()
