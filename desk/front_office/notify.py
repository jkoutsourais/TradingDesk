"""Phone pushes through ntfy, with the urgent-alert window and rate limits.

Every attempt is recorded in the pushes table, including alerts held back by the window
or the limits, so the dashboard can show what was withheld and why.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from desk.config import CONFIG_DIR

PushKind = Literal["briefing", "urgent", "test"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BriefingPush(_Frozen):
    title: str
    priority: str


class HourWindow(_Frozen):
    start: time
    end: time


class UrgentPush(_Frozen):
    enabled: bool
    title: str
    priority: str
    window: HourWindow
    max_per_hour: int = Field(gt=0)
    max_per_day: int = Field(gt=0)
    max_age_minutes: int = Field(gt=0)


class NotifyConfig(_Frozen):
    base_url: str
    briefing: BriefingPush
    urgent: UrgentPush


def load_notify_config(config_dir: Path = CONFIG_DIR) -> NotifyConfig:
    with (config_dir / "notify.yaml").open(encoding="utf-8") as handle:
        return NotifyConfig.model_validate(yaml.safe_load(handle))


@dataclass(frozen=True, slots=True)
class UrgentDecision:
    allowed: bool
    reason: str


def decide_urgent(
    config: UrgentPush,
    now: datetime,
    tz: ZoneInfo,
    sent_last_hour: int,
    sent_today: int,
    alert_time: datetime | None = None,
) -> UrgentDecision:
    """Pure decision so the window and limits are testable without a clock or database."""
    if not config.enabled:
        return UrgentDecision(False, "urgent pushes are disabled")
    if alert_time is not None and now - alert_time > timedelta(minutes=config.max_age_minutes):
        # A backlog (after a restart or outage) should not buzz the phone with old news.
        return UrgentDecision(False, f"stale: older than {config.max_age_minutes} minutes")
    local = now.astimezone(tz).time()
    if not (config.window.start <= local < config.window.end):
        return UrgentDecision(
            False, f"outside push hours {config.window.start:%H:%M}-{config.window.end:%H:%M} ET"
        )
    if sent_last_hour >= config.max_per_hour:
        return UrgentDecision(False, f"hourly limit of {config.max_per_hour} reached")
    if sent_today >= config.max_per_day:
        return UrgentDecision(False, f"daily limit of {config.max_per_day} reached")
    return UrgentDecision(True, "within hours and limits")


def urgent_counts(conn: Connection, now: datetime, tz: ZoneInfo) -> tuple[int, int]:
    local_midnight = datetime.combine(now.astimezone(tz).date(), time(0), tz)
    row = conn.execute(
        text(
            "SELECT count(*) FILTER (WHERE created_at >= :hour_ago) AS hour, "
            "count(*) FILTER (WHERE created_at >= :midnight) AS day "
            "FROM pushes WHERE kind = 'urgent' AND status = 'sent' "
            "AND created_at >= :midnight_or_hour"
        ),
        {
            "hour_ago": now - timedelta(hours=1),
            "midnight": local_midnight,
            "midnight_or_hour": min(local_midnight, now - timedelta(hours=1)),
        },
    ).one()
    return int(row.hour), int(row.day)


def record_push(
    conn: Connection,
    *,
    kind: PushKind,
    ref_id: UUID | None,
    title: str,
    message: str,
    click_url: str | None,
    priority: str,
    status: Literal["sent", "held", "failed"],
    reason: str | None = None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO pushes "
            "(kind, ref_id, title, message, click_url, priority, status, reason) "
            "VALUES (:kind, :ref_id, :title, :message, :click_url, :priority, :status, :reason)"
        ),
        {
            "kind": kind,
            "ref_id": ref_id,
            "title": title,
            "message": message,
            "click_url": click_url,
            "priority": priority,
            "status": status,
            "reason": reason,
        },
    )


class NtfyClient:
    def __init__(
        self, server: str, topic: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._url = f"{server.rstrip('/')}/{topic}"
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(15.0), transport=transport)

    async def send(self, *, title: str, message: str, priority: str, click_url: str | None) -> None:
        headers = {"Title": title, "Priority": priority}
        if click_url:
            headers["Click"] = click_url
        response = await self._http.post(
            self._url, content=message.encode("utf-8"), headers=headers
        )
        response.raise_for_status()

    async def aclose(self) -> None:
        await self._http.aclose()
