"""Room precedence and publish notices. No infrastructure imports."""

from collections.abc import Sequence

from analytics.application.settings import AnalyticsSettings
from analytics.domain.coordcard import CoordCard, card_default_rooms, type_side
from analytics.domain.correlation import TacrepMessage
from analytics.domain.publish import (
    PHRASE_RANK,
    PublishNotice,
    PublishTable,
    first_rule,
    phrase_for_confidence,
    table_from_rules_text,
)
from analytics.domain.rooms import lookup_rooms, resolve_rooms
from navtracker_common.chatsurfer import select_chat_rooms


def known_types(card: CoordCard, settings: AnalyticsSettings) -> tuple[str, ...]:
    """Type and template names the card or the environment mentions."""

    names: list[str] = [item.name for item in card.types]
    for template in card.templates:
        label = getattr(template, "name", "")
        if isinstance(label, str) and label and label not in names:
            names.append(label)
    for pairs in (settings.room_ingest, settings.room_publish):
        for name, _rooms in pairs:
            if name and name not in names:
                names.append(name)
    return tuple(names)


def ingest_room_list(card: CoordCard, settings: AnalyticsSettings) -> tuple[str, ...]:
    """Rooms to poll. Empty means this process reads no report rooms."""

    names = known_types(card, settings)
    if not names:
        return settings.default_ingest_rooms or settings.tacrep_rooms
    found: list[str] = []
    for name in names:
        for room in _resolved(card, settings, name, "ingest"):
            if room not in found:
                found.append(room)
    return tuple(found)


def active_publish_table(card: CoordCard, settings: AnalyticsSettings) -> PublishTable:
    """Card publish rules replace the env table when the card sets them."""

    if card.rooms is not None and card.rooms.publish is not None:
        return card.rooms.publish
    if not settings.publish_rules_text and not settings.default_publish_rooms:
        return PublishTable(republish_on_higher_level=settings.republish_on_higher_level)
    return table_from_rules_text(
        settings.publish_rules_text,
        default_rooms=settings.default_publish_rooms,
        republish=settings.republish_on_higher_level,
    )


def destination_rooms(
    type_name: str,
    *,
    confidence: str,
    cat: int | None,
    sources: Sequence[str],
    report_room: str,
    card: CoordCard,
    settings: AnalyticsSettings,
    table: PublishTable,
) -> tuple[str, ...]:
    """Rule rooms when one matches, otherwise the per-type publish fallback."""

    notice = PublishNotice(
        identity=type_name,
        confidence=confidence,
        cat=cat,
        types=(type_name,) if type_name else (),
        sources=tuple(sources),
        body="",
        level_rank=PHRASE_RANK.get(confidence, 0),
    )
    rule = first_rule(notice, table)
    chosen = rule.rooms if rule is not None else _resolved(card, settings, type_name, "publish")
    if report_room and report_room not in chosen:
        chosen = chosen + (report_room,)
    return chosen


def phrase_for_message(
    message: TacrepMessage,
    *,
    probable_at: float,
    confirmed_at: float,
    high_at: float,
) -> str:
    """Use a parsed phrase, or map a numeric confidence onto one."""

    if message.confidence_phrase:
        return message.confidence_phrase
    if message.reported_confidence is None:
        return ""
    return phrase_for_confidence(
        message.reported_confidence,
        probable_at,
        confirmed_at,
        high_at,
    )


def truth_read_rooms(settings: AnalyticsSettings) -> tuple[str, ...]:
    """GPS truth rooms analytics reads from ChatSurfer.

    The encryption flag selects ``room_name_encrypted`` for those names.
    Coord-card and ``ANALYTICS_ROOMS_*`` rooms are not in this list.
    """

    return select_chat_rooms(
        settings.read_rooms,
        encryption_required=settings.chatsurfer_encryption_required,
        room_name=settings.chatsurfer_room_name,
        room_name_encrypted=settings.chatsurfer_room_name_encrypted,
    )


def watched_chat_rooms(card: CoordCard, settings: AnalyticsSettings) -> tuple[str, ...]:
    """Truth reads plus report rooms. Report rooms keep the names the card or env set."""

    found: list[str] = []
    for room in (*truth_read_rooms(settings), *ingest_room_list(card, settings)):
        if room and room not in found:
            found.append(room)
    return tuple(found)


def _resolved(
    card: CoordCard,
    settings: AnalyticsSettings,
    type_name: str,
    side: str,
) -> tuple[str, ...]:
    env_pairs = settings.room_ingest if side == "ingest" else settings.room_publish
    env_default = (
        settings.default_ingest_rooms or settings.tacrep_rooms
        if side == "ingest"
        else settings.default_publish_rooms
    )
    return resolve_rooms(
        type_name,
        card_type=type_side(card, type_name, side),
        env_type=lookup_rooms(env_pairs, type_name),
        card_global=card_default_rooms(card, side),
        env_default=env_default,
    )
