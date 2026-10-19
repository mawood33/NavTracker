"""CAT/CE, confidence phrases, templates, and ChatSurfer room routing."""

import logging
from datetime import UTC, datetime

from tests.test_analytics_reports import _settings

from analytics.application.dispatch import destination_rooms, ingest_room_list
from analytics.composition import wire_service as AnalyticsService
from analytics.domain.coordcard import parse_coord_card_text
from analytics.domain.correlation import correlate, correlation_evidence
from analytics.domain.positions import NAUTICAL_MILE_METERS
from analytics.domain.publish import (
    PublishNotice,
    should_publish,
    table_from_rules_text,
)
from analytics.domain.rooms import resolve_rooms
from analytics.domain.tacrep_parse import parse_exercise_text, parse_tacrep_body
from analytics.infrastructure.room_publish import ChatRoomPublisher
from navtracker_common.errors import PublicationError

T0 = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
ADHOC = """\
SECRET
***START ADHOC 19OCT2601***
PRI: 2
1. deny, disrupt
2. HAWK, UAS
3. TEL, TGT-9
4. N6919 W11919, 120
5. TEL, launcher
6. LTIOV 1400Z. Report (TAI) NORTH - VACANT in ALPHA (Chatsurfer)
***END ADHOC 19OCT2601***
SECRET
"""
SITREP = """\
UNCLASSIFIED
SITREP: HAWK1, TEL at X20, N6919 W11919, 1400Z, ACTIVE
UNCLASSIFIED
"""


def _card_text() -> str:
    return """
types:
  - name: MTI
    kind: J
    start: 400
    end: 499
    fields: [callsign, id, activity, bullseye, link16, location, tai_msr, cat, time, fillins]
    rooms:
      ingest: [room-mti-ingest]
      publish: [room-mti-publish]
  - name: IMINT
    kind: I
    start: 100
    end: 199
    fields: [callsign, id, equipment, location, elevation, tai, time, cat]
  - name: ELINT
    kinds: [E]
    start: 500
    end: 599
    fields:
      [callsign, id, entity, activity, location, tai, ellipse, orientation, cat, time, fillins]
templates:
  - name: ADHOC
    detect: '(?i)\\*\\*\\*START\\s+ADHOC'
    separator: lines
    evidence: tasking
    classification: true
    rooms:
      ingest: [room-adhoc-ingest]
      publish: [room-adhoc-publish]
    fields:
      - {name: effects, type: effects, line: 1}
      - {name: assets, type: assets, line: 2}
      - {name: target, type: name_id, line: 3}
      - {name: location, type: location, line: 4}
      - {name: eei, type: equipment, line: 5}
      - {name: reporting, type: reporting, line: 6}
  - name: SITREP
    detect: '(?i)\\bSITREP\\b'
    separator: comma
    evidence: report
    classification: true
    rooms:
      publish: [room-sitrep-publish]
    fields:
      - {name: callsign, type: text}
      - {name: equipment, type: equipment_at}
      - {name: location, type: location}
      - {name: time, type: time}
      - {name: frequency, type: frequency}
      - {name: heading, type: heading}
      - {name: activity, type: activity}
ellipse_unit: nmi
rooms:
  ingest: [room-card-default]
  publish:
    default: [room-card-publish]
    rules:
      - confidence: [POSS HIGH]
        cat_min: 1
        cat_max: 2
        rooms: [room-precise]
"""


def test_room_precedence_is_card_type_then_env_type_then_defaults():
    assert resolve_rooms(
        "ADHOC",
        card_type=("card-type",),
        env_type=("env-type",),
        card_global=("card-default",),
        env_default=("env-default",),
    ) == ("card-type",)
    assert resolve_rooms(
        "ADHOC",
        card_type=None,
        env_type=("env-type",),
        card_global=("card-default",),
        env_default=("env-default",),
    ) == ("env-type",)
    assert resolve_rooms(
        "ADHOC",
        card_type=None,
        env_type=None,
        card_global=("card-default",),
        env_default=("env-default",),
    ) == ("card-default",)
    assert resolve_rooms(
        "ADHOC",
        card_type=None,
        env_type=None,
        card_global=None,
        env_default=("env-default",),
    ) == ("env-default",)
    assert resolve_rooms(
        "ADHOC",
        card_type=(),
        env_type=("env-type",),
        card_global=("card-default",),
        env_default=("env-default",),
    ) == ()


def test_dash_and_blank_are_null_and_a_missing_orientation_is_a_circle():
    card = parse_coord_card_text(_card_text(), suffix=".yaml")
    dashed = parse_tacrep_body(
        "HAWK // J-410 // - // - // - // 10.0 20.0 // - // - // 1919Z // -",
        source_id="room",
        observed_at=T0,
        card=card,
    )[0]
    assert dashed.bullseye == ""
    assert dashed.cat_level is None
    assert dashed.bmoa == ""
    assert all(value != "-" for _key, value in dashed.attributes)
    km = parse_coord_card_text(
        _card_text().replace("ellipse_unit: nmi", "ellipse_unit: km"),
        suffix=".yaml",
    )
    ellipse = parse_tacrep_body(
        "HAWK // E-501 // RADAR // ACTIVE // 10.0, 20.0 // TAI // 2/1 // - // CAT I // 1919Z //",
        source_id="room",
        observed_at=T0,
        card=km,
    )[0]
    assert ellipse.semi_major_m == 2000
    assert ellipse.semi_minor_m == 1000
    assert ellipse.orientation_deg is None
    assert ellipse.ce_radius_m == 6
    near = parse_tacrep_body(
        "HAWK // E-502 // RADAR // ACTIVE // 10.004, 20.0 // TAI // 0.05/0.01 km // 0 // "
        "CAT IV // 1919Z //",
        source_id="room",
        observed_at=T0,
        card=km,
    )[0]
    from dataclasses import replace

    from analytics.domain.correlation import within_ellipse

    owner = replace(ellipse, semi_major_m=1000, semi_minor_m=100, orientation_deg=None)
    assert within_ellipse(owner, replace(near, latitude=10.004, longitude=20.0))
    nautical = parse_tacrep_body(
        "HAWK // E-503 // RADAR // ACTIVE // 10, 20 // TAI // 1/1 // 0 // CAT II // 1919Z //",
        source_id="room",
        observed_at=T0,
        card=card,
    )[0]
    assert nautical.semi_major_m == NAUTICAL_MILE_METERS


def test_adhoc_and_sitrep_templates_link_responses_and_keep_banners(caplog):
    card = parse_coord_card_text(_card_text(), suffix=".yaml")
    hidden = ADHOC.replace("START", "ST\u200bART")
    with caplog.at_level(logging.DEBUG):
        messages, taskings = parse_exercise_text(
            hidden,
            source_id="room-adhoc-ingest",
            observed_at=T0,
            card=card,
        )
    assert taskings and taskings[0].task_id == "19OCT2601"
    assert taskings[0].priority == 2
    assert taskings[0].effects == ("deny", "disrupt")
    assert taskings[0].assets == ("HAWK", "UAS")
    assert taskings[0].target_name == "TEL"
    assert taskings[0].target_id == "TGT-9"
    assert taskings[0].equipment == ("TEL", "launcher")
    assert taskings[0].tai == "NORTH"
    assert taskings[0].report_room == "ALPHA"
    assert taskings[0].vacant is True
    assert taskings[0].classification == "SECRET"
    assert "SECRET" not in " ".join(
        record.message for record in caplog.records if record.levelno >= logging.INFO
    )
    debug = [record.message for record in caplog.records if record.levelno < logging.INFO]
    assert any("SECRET" in message for message in debug)
    assert messages and messages[0].classification == "SECRET"
    assert messages[0].report_room == "ALPHA"
    assert "SECRET" in messages[0].raw
    mismatched = ADHOC.replace("***END ADHOC 19OCT2601***", "***END ADHOC 19OCT2602***")
    _rows, warned = parse_exercise_text(mismatched, source_id="room", observed_at=T0, card=card)
    assert "differ" in warned[0].warning
    sitrep, _none = parse_exercise_text(SITREP, source_id="room-sitrep", observed_at=T0, card=card)
    assert sitrep[0].tacrep_type == "SITREP"
    assert sitrep[0].callsign == "HAWK1"
    assert sitrep[0].equipment == ("TEL",)
    assert sitrep[0].bmoa == "X20"
    assert sitrep[0].events == ("active",)
    assert sitrep[0].classification == "UNCLASSIFIED"
    linked = parse_exercise_text(SITREP, source_id="room-sitrep", observed_at=T0, card=card)[0]
    from analytics.domain.templates import link_responses

    answered = link_responses(linked, taskings, match_meters=1000)
    assert ("responds_to", "19OCT2601") in answered[0].attributes


def test_vacant_evidence_lowers_confidence_and_a_response_is_recorded():
    card = parse_coord_card_text(_card_text(), suffix=".yaml")
    message = parse_exercise_text(ADHOC, source_id="adhoc", observed_at=T0, card=card)[0][0]
    from dataclasses import replace

    placed = replace(message, cell_id="cell", vacant_weight=0.25)
    group = correlate((placed,))[0]
    vacant = next(item for item in correlation_evidence(group) if item.kind == "vacant")
    assert vacant.weight == -0.25


def test_rules_pick_a_room_and_types_fall_back_without_publishing_when_unset():
    table = table_from_rules_text(
        "POSS HIGH:1-2:ELINT::room-precise;CERTAIN::::room-certain",
        default_rooms=("room-default",),
    )
    precise = PublishNotice("e", "POSS HIGH", 1, ("ELINT",), ("room",), "", 2)
    from analytics.domain.publish import first_rule

    assert first_rule(precise, table) is not None
    assert first_rule(precise, table).rooms == ("room-precise",)  # type: ignore[union-attr]
    assert should_publish(None, 2, republish_on_higher_level=True) is True
    assert should_publish(2, 2, republish_on_higher_level=True) is False
    assert should_publish(1, 2, republish_on_higher_level=True) is True
    assert should_publish(1, 2, republish_on_higher_level=False) is False
    card = parse_coord_card_text(_card_text(), suffix=".yaml")
    settings = _settings(
        room_publish=(("IMINT", ("room-from-env",)),),
        default_publish_rooms=("room-env-default",),
    )
    assert destination_rooms(
        "MTI",
        confidence="",
        cat=None,
        sources=(),
        report_room="",
        card=card,
        settings=settings,
        table=table,
    ) == ("room-mti-publish",)
    assert destination_rooms(
        "IMINT",
        confidence="CERTAIN",
        cat=4,
        sources=(),
        report_room="",
        card=card,
        settings=settings,
        table=table,
    ) == ("room-certain",)
    assert "room-mti-ingest" in ingest_room_list(card, settings)
    assert "room-adhoc-ingest" in ingest_room_list(card, settings)
    bare = _settings()
    empty = parse_coord_card_text(
        "types:\n- {name: MTI, kind: J, start: 400, end: 499, fields: [id, location]}\n",
        suffix=".yaml",
    )
    assert destination_rooms(
        "MTI",
        confidence="PROB",
        cat=2,
        sources=(),
        report_room="",
        card=empty,
        settings=bare,
        table=table_from_rules_text(""),
    ) == ()


class _Publisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def publish_to(self, room: str, text: str) -> None:
        self.sent.append((room, text))


def test_service_publishes_by_rule_reloads_rooms_and_keeps_a_bad_card(caplog, tmp_path):
    publisher = _Publisher()
    caplog.set_level(logging.INFO)
    service = AnalyticsService(
        _settings(reference_dir=str(tmp_path), room_ingest=(("IMINT", ("env-imint",)),)),
        None,
        publisher=publisher,
    )
    service.save_coord_card("coord-card.yaml", _card_text().encode())
    assert "room-mti-ingest" in caplog.text
    rooms = ingest_room_list(service._coord_card, service.settings)
    assert "room-mti-ingest" in rooms
    assert "env-imint" in rooms
    assert "room-adhoc-ingest" in rooms
    service.ingest_tacrep_body(
        "HAWK // I-150 // POSS HIGH TEL // 10.0, 20.0 // 100 // TAI // 1919Z // CAT II",
        "env-imint",
    )
    import asyncio

    caplog.clear()
    asyncio.run(service.publish_routed())
    assert publisher.sent
    assert publisher.sent[0][0] == "room-precise"
    assert "POSS HIGH TEL" in publisher.sent[0][1]
    assert "SECRET" not in caplog.text
    again = len(publisher.sent)
    asyncio.run(service.publish_routed())
    assert len(publisher.sent) == again
    try:
        service.save_coord_card("coord-card.yaml", b"types: []\n")
    except ValueError:
        pass
    else:
        raise AssertionError("expected the empty card to be rejected")
    assert service._coord_card.types[0].name == "MTI"


def test_room_publisher_retries_then_drops(caplog):
    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        async def publish_text(self, text: str, *, room_name: str | None = None) -> dict[str, str]:
            self.calls += 1
            raise PublicationError("down")

    delays: list[float] = []

    async def _sleep(seconds: float) -> None:
        delays.append(seconds)

    publisher = ChatRoomPublisher(_Client(), sleep=_sleep)  # type: ignore[arg-type]
    import asyncio

    with caplog.at_level(logging.ERROR):
        asyncio.run(publisher.publish_to("room-a", "body"))
    assert delays == [0.5, 1.0]
    assert "dropped" in caplog.text
    assert "body" not in caplog.text
