"""Reported confidence versus the assessed level on messages and documents."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.test_analytics_reports import _settings

from analytics.composition import wire_service as AnalyticsService
from analytics.domain.assessment_text import AssessmentWording, wording_from_mapping
from analytics.domain.confidence_text import (
    ConfidenceTerm,
    read_confidence_token,
    terms_from_mapping,
)
from analytics.domain.coordcard import CoordCardError, parse_coord_card_text
from analytics.domain.correlation import TacrepMessage, dedupe_messages
from analytics.domain.level_assessment import annotate_report, assess_message, render_level
from analytics.domain.outputs import site_fields
from analytics.domain.publish import phrase_for_confidence, phrase_for_level
from analytics.domain.reference import Evidence, Site
from analytics.domain.tacrep_parse import parse_tacrep_body
from analytics.infrastructure.http_api import route_request

T0 = datetime(2026, 10, 19, 19, 19, tzinfo=UTC)
CARD = "config/analytics/coord_card.example.yaml"


def _message(
    serial: str,
    phrase: str,
    level: str,
    *,
    source: str,
    rank: int = 0,
    base: str = "",
    raw: str = "",
) -> TacrepMessage:
    body = raw or f"HAWK // {serial} // {phrase} TEL // 10.0, 20.0 // CAT IV"
    return TacrepMessage(
        kind=serial[0],
        sequence=serial.split("-", 1)[1],
        observed_at=T0,
        latitude=10.0,
        longitude=20.0,
        source_id=source,
        source_message_id=serial,
        subject="TEL",
        cell_id="hex",
        raw=body,
        serial_full=serial,
        serial_base=base or serial,
        correction_rank=rank,
        confidence_phrase=phrase,
        confidence_level=level,
        confidence_band="low" if phrase == "POSS LOW" else "",
        reported_confidence=0.25 if level == "possible" else 1.0,
        tacrep_type="ELINT",
    )


def test_certain_and_verified_parse_as_confirmed() -> None:
    card = parse_coord_card_text(Path(CARD).read_text(encoding="utf-8"), suffix=".yaml")
    certain = parse_tacrep_body(
        "HAWK // I-150 // CERTAIN TEL // 10.0, 20.0 // 100 // TAI // 1919Z // CAT II",
        source_id="imint",
        observed_at=T0,
        card=card,
    )[0]
    verified = parse_tacrep_body(
        "HAWK // I-151 // VERIFIED TEL // 10.0, 20.0 // 100 // TAI // 1919Z // CAT II",
        source_id="imint",
        observed_at=T0,
        card=card,
    )[0]
    assert certain.confidence_phrase == "CONFIRMED"
    assert certain.confidence_level == "confirmed"
    assert verified.confidence_phrase == "CONFIRMED"
    assert verified.confidence_level == "confirmed"
    weight, phrase, level, _band = read_confidence_token("CERTAIN")
    assert (weight, phrase, level) == (1.0, "CONFIRMED", "confirmed")
    mapped = terms_from_mapping([{"phrase": "CERTAIN", "level": "verified", "weight": 1.0}])
    assert mapped[0].level == "confirmed"


def test_numeric_confidence_uses_the_card_phrase() -> None:
    assert phrase_for_confidence(0.2, 0.55, 0.8, 0.45) == "POSS LOW"
    assert phrase_for_confidence(0.5, 0.55, 0.8, 0.45) == "POSS HIGH"
    assert phrase_for_confidence(0.7, 0.55, 0.8, 0.45) == "PROB"
    assert phrase_for_confidence(0.9, 0.55, 0.8, 0.45) == "CONFIRMED"
    terms = (
        ConfidenceTerm("POSS LOW", "possible", "low", 0.25),
        ConfidenceTerm("LIKELY", "probable", "", 0.70),
        ConfidenceTerm("CONFIRMED", "confirmed", "", 1.0),
    )
    assert phrase_for_level("probable", terms) == "LIKELY"
    exported = site_fields(Site(name="A", status="verified", confidence=1.0))
    assert exported["assessment"] == "confirmed"


def test_single_source_combined_correction_and_disagreement() -> None:
    lone = _message("G-501", "POSS LOW", "possible", source="elint")
    alone = assess_message(lone, (lone,), "possible", ())
    assert alone.basis == "single_source"
    assert alone.disagreement is False
    assert render_level(alone) == "SINGLE SOURCE POSS LOW"
    confirmed = _message("G-501", "CONFIRMED", "confirmed", source="elint")
    stated = assess_message(confirmed, (confirmed,), "possible", ())
    assert stated.assessed_phrase == "CONFIRMED"
    assert stated.reported_level == "confirmed"
    assert stated.disagreement is False

    older = _message("I-103", "POSS LOW", "possible", source="imint", rank=0, base="I-103")
    newer = _message("I-103A", "POSS LOW", "possible", source="imint", rank=1, base="I-103")
    kept = dedupe_messages((older, newer))
    assert [item.serial_full for item in kept] == ["I-103A"]
    group = (
        _message("G-501", "POSS LOW", "possible", source="elint"),
        _message("J-401", "POSS LOW", "possible", source="mti"),
        kept[0],
    )
    raised = assess_message(group[0], group, "probable", ())
    assert raised.basis == "correlated"
    assert raised.assessed_phrase == "PROB"
    assert raised.contributor_ids == ("G-501", "J-401", "I-103A")
    assert "I-103" not in raised.contributor_ids
    remark = render_level(raised)
    assert remark == "ASSESSED PROB (FROM POSS LOW G-501 + J-401 + I-103A)"
    raw = group[0].raw
    body = annotate_report(raw, remark)
    assert body.count("//") == raw.count("//")
    assert body.endswith("CAT IV ASSESSED PROB (FROM POSS LOW G-501 + J-401 + I-103A)")
    assert "POSS LOW TEL" in body
    again = render_level(raised, previous="POSS LOW")
    assert again == (
        "WAS POSS LOW NOW ASSESSED PROB (FROM POSS LOW G-501 + J-401 + I-103A)"
    )

    conflict_group = (
        _message("G-501", "CONFIRMED", "confirmed", source="elint"),
        _message("J-401", "POSS LOW", "possible", source="mti"),
    )
    conflict = assess_message(conflict_group[0], conflict_group, "probable", ())
    assert conflict.disagreement is True
    assert conflict.reported_level == "confirmed"
    assert conflict.assessed_level == "probable"
    assert render_level(conflict) == "REPORTED CONFIRMED ASSESSED PROB (FROM G-501 + J-401)"

    custom = AssessmentWording(correlated="ASSESSED/CORRELATED {phrase} ({count}) {ids}")
    assert render_level(raised, custom) == "ASSESSED/CORRELATED PROB (3) G-501 + J-401 + I-103A"
    sitrep = annotate_report("SITREP: HAWK, TEL at X20", "SINGLE SOURCE POSS LOW")
    assert "//" not in sitrep
    assert sitrep.endswith(", SINGLE SOURCE POSS LOW")
    with pytest.raises(ValueError):
        wording_from_mapping({"single": "KEEP // LAYOUT"})
    with pytest.raises(CoordCardError):
        parse_coord_card_text(
            "types:\n- {name: MTI, kind: J, start: 1, end: 2, fields: [id]}\n"
            "assessment_text: {single: 'KEEP // LAYOUT'}\n",
            suffix=".yaml",
        )


class _Publisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def publish_to(self, room: str, text: str) -> None:
        self.sent.append((room, text))


def test_publish_and_documents_distinguish_single_source_from_combined() -> None:
    publisher = _Publisher()
    service = AnalyticsService(_settings(), None, publisher=publisher)
    service._coord_card = parse_coord_card_text(
        """
types:
  - name: ELINT
    kinds: [G, J, I]
    start: 100
    end: 599
    fields: [id, entity]
rooms:
  publish:
    default: [room-out]
    republish_on_higher_level: true
    rules:
      - rooms: [room-out]
""",
        suffix=".yaml",
    )
    first = _message("G-501", "POSS LOW", "possible", source="elint")
    service._messages = (first,)
    service._refresh_correlations()
    asyncio.run(service.publish_routed())
    assert publisher.sent[0][0] == "room-out"
    assert "SINGLE SOURCE POSS LOW" in publisher.sent[0][1]
    assert publisher.sent[0][1].count("//") == first.raw.count("//")
    assert "POSS LOW TEL" in publisher.sent[0][1]

    newer = _message("I-103A", "POSS LOW", "possible", source="imint", rank=1, base="I-103")
    older = _message("I-103", "POSS LOW", "possible", source="imint", rank=0, base="I-103")
    service._messages = dedupe_messages(
        (
            first,
            _message("J-401", "POSS LOW", "possible", source="mti"),
            older,
            newer,
        )
    )
    service._refresh_correlations()
    asyncio.run(service.publish_routed())
    republished = [
        text
        for room, text in publisher.sent
        if "G-501" in text and "WAS POSS LOW" in text
    ]
    assert republished
    assert "ASSESSED PROB (FROM POSS LOW G-501 + J-401 + I-103A)" in republished[0]
    assert republished[0].count("//") == first.raw.count("//")

    service._sites = (
        Site(
            name="A",
            status="probable",
            confidence=0.7,
            latitude=10.0,
            longitude=20.0,
            evidence=(
                Evidence(
                    kind="correlation",
                    weight=0.5,
                    detail="grouped",
                    message_id="G-501",
                    linked_ids=("J-401", "I-103A"),
                ),
            ),
        ),
    )
    snapshot = service.output_snapshot(T0)
    site = snapshot["sites"][0]
    assert site["assessment"] == "probable"
    assert site["reported_phrase"] == "POSS LOW"
    assert site["assessed_phrase"] == "PROB"
    assert site["basis"] == "correlated"
    assert site["contributor_ids"] == ["G-501", "J-401", "I-103A"]
    assert site["disagreement"] is False
    evidence = snapshot["evidence"][0]
    assert evidence["assessed_level"] == "probable"
    assert evidence["basis"] == "correlated"
    assert "I-103A" in evidence["contributor_ids"]
    listed = route_request("GET", "/api/sites", b"", {}, service)[1]
    assert listed["sites"][0]["assessed_phrase"] == "PROB"
    page = route_request("GET", "/sites", b"", {}, service)[1]
    assert page["sites"][0]["contributor_ids"] == ["G-501", "J-401", "I-103A"]
    assert page["sites"][0]["evidence"][0]["reported_phrase"] == "POSS LOW"
    groups = route_request("GET", "/v1/events/correlations", b"", {}, service)[1]
    assert groups["correlations"][0]["assessed_level"] == "probable"
    assert groups["correlations"][0]["basis"] == "correlated"
