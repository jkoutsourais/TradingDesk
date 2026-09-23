from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from desk.artifacts.brief import Brief, BriefSection, FactSnapshot, SnapshotFact, TriageLabel
from desk.artifacts.registry import artifact_class

START = datetime(2026, 9, 22, 20, 0, tzinfo=UTC)
END = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def snapshot() -> FactSnapshot:
    return FactSnapshot(
        produced_by="front_office.briefing",
        runtime_ms=40,
        purpose="morning_brief",
        facts=(
            SnapshotFact(
                id="AEP.change_pct",
                value="-1.23",
                unit="%",
                display="-1.23%",
                label="AEP day change",
                source_ref="computed:x",
            ),
        ),
    )


def test_kinds_registered() -> None:
    assert artifact_class("fact_snapshot") is FactSnapshot
    assert artifact_class("brief") is Brief
    assert artifact_class("triage_label") is TriageLabel


def test_fact_snapshot_ids_unique() -> None:
    fact = snapshot().facts[0]
    with pytest.raises(ValidationError, match="duplicate"):
        FactSnapshot(produced_by="x", runtime_ms=0, purpose="p", facts=(fact, fact))


def test_brief_must_cite_its_fact_snapshot() -> None:
    snap = snapshot()
    brief = Brief(
        produced_by="front_office.briefing",
        runtime_ms=9000,
        model="gpt-oss:20b",
        prompt_version="briefing.v1",
        parents=(snap.id,),
        brief_kind="morning",
        covers_from=START,
        covers_to=END,
        fact_snapshot_id=snap.id,
        sections=(BriefSection(title="Holdings", lines=("AEP moved -1.23%.",)),),
    )
    assert brief.fact_snapshot_id in brief.parents
    with pytest.raises(ValidationError, match="parents"):
        Brief(
            produced_by="x",
            runtime_ms=0,
            brief_kind="morning",
            covers_from=START,
            covers_to=END,
            fact_snapshot_id=uuid4(),
            sections=(),
        )


def test_brief_window_must_be_ordered() -> None:
    snap_id = uuid4()
    with pytest.raises(ValidationError, match="covers"):
        Brief(
            produced_by="x",
            runtime_ms=0,
            parents=(snap_id,),
            brief_kind="morning",
            covers_from=END,
            covers_to=START,
            fact_snapshot_id=snap_id,
            sections=(),
        )


def test_triage_label_values() -> None:
    subject = uuid4()
    label = TriageLabel(
        produced_by="watch.triage",
        runtime_ms=5,
        parents=(subject,),
        subject_id=subject,
        label="relevant",
        reason="Touches a holding",
    )
    assert label.subject_id in label.parents
    with pytest.raises(ValidationError):
        TriageLabel(
            produced_by="x",
            runtime_ms=0,
            parents=(subject,),
            subject_id=subject,
            label="maybe",
            reason="x",
        )


def test_snapshot_value_is_string_or_decimal() -> None:
    fact = SnapshotFact(
        id="v", value=Decimal("1.5"), unit="", display="1.5", label="v", source_ref="r"
    )
    assert fact.value == "1.5"
