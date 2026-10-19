"""TACREP messages, activity sessions, and pair-match correlation.

A TACREP line is ``KIND-seq//time//lat,lon//key=value``. Kinds are
``O`` (launch), ``I`` (imagery), ``J`` (movement), ``E`` (electronic),
and ``UNKNOWN``. Times are ISO-8601 UTC.

Serial ids match ``^[A-Z]-\\d{3}[A-Z]?$``. ``I-103A`` corrects ``I-103``.
The base id is the identity, scoped by source and UTC day. The highest
suffix wins, and an older suffix does not replace it. Ids that do not
match the pattern use a hash of the source and the raw line.

Exercise block ranges come from a coord card, not from hardcoded
numbers. The card's type name, kind letter, and range classify the id.
``O``, ``J``, and ``E`` still use their TransloadOracle type names when
the card does not list that letter. A serial outside its block, or a
kind letter that contradicts the declared kind, is accepted and flagged.

Activities move ``pre_launch_hide -> launch -> hide_after_launch ->
transload -> hide_after_transload``. ``closed`` is unused. An ``O``
message enters ``launch``. A gap longer than ``hide_gap_minutes`` moves
to the next hide state. A ``J``, ``I``, or ``E`` message enters
``transload`` from ``hide_after_launch`` when it shares a BMOA.

Sessions group messages whose span from the first message stays inside
the correlation window and whose cells are the anchor cell or an H3
neighbor. Pair score is ``0.45 * same_subject + 0.35 * spatial +
0.20 * time``. The subject term counts only an explicit subject.
``minimum_transload_speed_mps`` is stored and is not used.
"""

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from analytics.domain.confidence_text import (
    DEFAULT_CONFIDENCE_TERMS,
    ConfidenceTerm,
    read_confidence_token,
    split_reported_confidence,
)
from analytics.domain.positions import point_within_ellipse, point_within_radius
from analytics.domain.reference import Evidence
from analytics.domain.timeutil import ensure_utc, valid_position

ACTIVITY_STATES = (
    "pre_launch_hide",
    "launch",
    "hide_after_launch",
    "transload",
    "hide_after_transload",
)
_KINDS = frozenset({"O", "I", "J", "E", "G", "F", "UNKNOWN"})
_TRANSLOAD_KINDS = frozenset({"J", "I", "E", "G", "F"})
_NEXT_HIDE = {"launch": "hide_after_launch", "transload": "hide_after_transload"}
_SERIAL_RE = re.compile(r"^([A-Z])-(\d{3})([A-Z]?)$")
_KIND_TYPES = {
    "O": "launch",
    "I": "imagery",
    "J": "movement",
    "E": "electronic",
    "G": "ELINT",
    "F": "FUSED",
}
_EVENT_KINDS = {
    "slowing": "slowdown",
    "slowdown": "slowdown",
    "stopping": "stop",
    "stop": "stop",
    "halt": "stop",
    "milling": "mill",
    "mill": "mill",
    "rotator": "rotator",
    "mover": "movement",
    "movement": "movement",
}
_UPDATE_RE = re.compile(r"^UPDATE\s+([A-Z]-\d{3}[A-Z]?)$")
NeighborLookup = Callable[[str, int], set[str]]


@dataclass(frozen=True)
class TacrepBlock:
    """One configured serial range for a kind letter."""

    kind: str
    start: int
    end: int
    tacrep_type: str


DEFAULT_BLOCKS: tuple[TacrepBlock, ...] = ()


@dataclass(frozen=True)
class CorrelationPolicy:
    """Defaults copied from the extracted TransloadOracle rules."""

    window_minutes: float = 45.0
    hide_gap_minutes: float = 20.0
    minimum_correlation_score: float = 0.70
    minimum_sources: int = 3
    minimum_transload_speed_mps: float = 0.5
    subject_weight: float = 0.45
    spatial_weight: float = 0.35
    time_weight: float = 0.20
    neighbor_k: int = 1
    blocks: tuple[TacrepBlock, ...] = DEFAULT_BLOCKS
    explicit_link_weight: float = 0.5


@dataclass(frozen=True)
class TacrepMessage:
    """One parsed TACREP. ``cell_id`` is filled after the hex index runs."""

    kind: str
    sequence: str
    observed_at: datetime
    latitude: float
    longitude: float
    source_id: str
    source_message_id: str
    subject: str = ""
    speed_mps: float | None = None
    heading: float | None = None
    length_m: float | None = None
    width_m: float | None = None
    reported_confidence: float | None = None
    bmoa: str = ""
    msr: str = ""
    cell_id: str = ""
    raw: str = ""
    serial_base: str = ""
    serial_full: str = ""
    correction_rank: int = -1
    tacrep_type: str = ""
    block_label: str = ""
    block_warning: str = ""
    event: str = ""
    callsign: str = ""
    mgrs_text: str = ""
    attributes: tuple[tuple[str, str], ...] = ()
    semi_major_m: float | None = None
    semi_minor_m: float | None = None
    orientation_deg: float | None = None
    cat: str = ""
    cat_weight: float | None = None
    vehicle_count: int | None = None
    correlates_to: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    bullseye: str = ""
    confidence_phrase: str = ""
    confidence_level: str = ""
    confidence_band: str = ""
    cat_level: int | None = None
    ce_radius_m: float | None = None
    equipment: tuple[str, ...] = ()
    report_room: str = ""
    classification: str = ""
    vacant: bool = False
    vacant_weight: float = 0.25
    history: tuple["TacrepMessage", ...] = ()
    chat_message_id: str = ""


@dataclass(frozen=True)
class Activity:
    """One tracked activity. ``acceptance`` is 1.0 or 0.0."""

    activity_id: str
    state: str
    cell_id: str
    bmoa: str
    latitude: float
    longitude: float
    opened_at: datetime
    updated_at: datetime
    subject: str
    acceptance: float
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SuggestedFusion:
    """A fused correlation inferred by analytics, not a TACREP that was sent."""

    anchor_id: str
    linked_ids: tuple[str, ...]
    reason: str
    tacrep_type: str = "FUSED"


@dataclass(frozen=True)
class Correlation:
    """Messages that share a session inside the correlation window."""

    cell_id: str
    window_start: datetime
    label: str
    best_score: float
    source_ids: tuple[str, ...]
    site_types: frozenset[str]
    activity: Activity
    messages: tuple[TacrepMessage, ...]
    link_weight: float = 0.0
    suggested_fusion: SuggestedFusion | None = None


def state_site_type(state: str) -> str:
    """Map an activity state onto a NavTracker site type."""

    if state == "launch":
        return "launch"
    if state == "transload":
        return "transload"
    return "hide"


def parse_tacrep_line(
    text: str,
    *,
    source_id: str,
    fallback_message_id: str = "",
    policy: CorrelationPolicy | None = None,
    terms: Sequence[ConfidenceTerm] | None = None,
) -> TacrepMessage | None:
    """Parse one ``KIND-seq//time//lat,lon//key=value`` line."""

    del fallback_message_id
    body = text.strip()
    if "//" not in body:
        return None
    parts = [part.strip() for part in body.split("//")]
    if len(parts) < 3 or "-" not in parts[0]:
        return None
    kind, sequence = parts[0].split("-", 1)
    kind = kind.strip().upper()
    sequence = sequence.strip()
    if kind not in _KINDS:
        return None
    observed = _parse_time(parts[1])
    lat_lon = parts[2].split(",")
    if observed is None or len(lat_lon) != 2:
        return None
    try:
        latitude = float(lat_lon[0].strip())
        longitude = float(lat_lon[1].strip())
    except ValueError:
        return None
    if not valid_position(latitude, longitude):
        return None
    fields = _fields(parts[3:])
    rules = policy or CorrelationPolicy()
    identity = _identity(kind, sequence, fields.get("id", ""), source_id, body, rules)
    active_terms = terms or DEFAULT_CONFIDENCE_TERMS
    weight, phrase, level, band = read_confidence_token(fields.get("confidence", ""), active_terms)
    subject, phrase, level, band, weight = _subject_confidence(
        fields.get("subject", ""),
        phrase,
        level,
        band,
        weight,
        active_terms,
    )
    return TacrepMessage(
        kind=kind,
        sequence=sequence,
        observed_at=observed,
        latitude=latitude,
        longitude=longitude,
        source_id=source_id,
        source_message_id=identity[0],
        subject=subject,
        speed_mps=_optional_float(fields.get("speed")),
        heading=_optional_float(fields.get("heading")),
        length_m=_optional_float(fields.get("length")),
        width_m=_optional_float(fields.get("width")),
        reported_confidence=weight,
        confidence_phrase=phrase,
        confidence_level=level,
        confidence_band=band,
        bmoa=fields.get("bmoa", ""),
        msr=fields.get("msr", ""),
        raw=body,
        serial_base=identity[1],
        serial_full=identity[2],
        correction_rank=identity[3],
        tacrep_type=identity[4],
        block_label=identity[5],
        block_warning=identity[6],
    )


def message_from_event(
    payload: Mapping[str, object],
    *,
    source_id: str,
    fallback_message_id: str = "",
    policy: CorrelationPolicy | None = None,
    terms: Sequence[ConfidenceTerm] | None = None,
) -> TacrepMessage | None:
    """Accept a JSON event or a JSON object that wraps a TACREP line."""

    wrapped = payload.get("tacrep", payload.get("message", payload.get("body")))
    if isinstance(wrapped, str) and "//" in wrapped:
        source = str(payload.get("source_id", source_id) or source_id)
        return parse_tacrep_line(
            wrapped,
            source_id=source,
            fallback_message_id=fallback_message_id,
            policy=policy,
            terms=terms,
        )
    kind = str(payload.get("kind", payload.get("type", "")) or "").strip().upper()
    if kind not in _KINDS:
        return None
    observed = _parse_time(str(payload.get("time", payload.get("timestamp", "")) or ""))
    latitude = _optional_float(payload.get("latitude", payload.get("lat")))
    longitude = _optional_float(payload.get("longitude", payload.get("lon")))
    if (
        observed is None
        or latitude is None
        or longitude is None
        or not valid_position(latitude, longitude)
    ):
        return None
    rules = policy or CorrelationPolicy()
    sequence = str(payload.get("sequence", "") or "")
    explicit_id = str(payload.get("source_message_id", payload.get("id", "")) or "")
    raw = str(payload.get("raw", "") or "")
    identity = _identity(kind, sequence, explicit_id, source_id, raw or explicit_id, rules)
    subject = payload.get("subject", "")
    subject_text = subject.strip() if isinstance(subject, str) else ""
    active_terms = terms or DEFAULT_CONFIDENCE_TERMS
    raw_confidence = payload.get("confidence")
    if isinstance(raw_confidence, str):
        weight, phrase, level, band = read_confidence_token(raw_confidence, active_terms)
    else:
        weight, phrase, level, band = _optional_float(raw_confidence), "", "", ""
    subject_text, phrase, level, band, weight = _subject_confidence(
        subject_text,
        phrase,
        level,
        band,
        weight,
        active_terms,
    )
    return TacrepMessage(
        kind=kind,
        sequence=sequence,
        observed_at=observed,
        latitude=latitude,
        longitude=longitude,
        source_id=str(payload.get("source_id", source_id) or source_id),
        source_message_id=identity[0],
        subject=subject_text,
        speed_mps=_optional_float(payload.get("speed")),
        heading=_optional_float(payload.get("heading")),
        length_m=_optional_float(payload.get("length")),
        width_m=_optional_float(payload.get("width")),
        reported_confidence=weight,
        confidence_phrase=phrase,
        confidence_level=level,
        confidence_band=band,
        bmoa=str(payload.get("bmoa", "") or ""),
        msr=str(payload.get("msr", "") or ""),
        raw=raw,
        serial_base=identity[1],
        serial_full=identity[2],
        correction_rank=identity[3],
        tacrep_type=identity[4],
        block_label=identity[5],
        block_warning=identity[6],
    )


def dedupe_messages(messages: Sequence[TacrepMessage]) -> tuple[TacrepMessage, ...]:
    """Collapse exact copies and let the newest correction replace older ones."""

    winners: dict[tuple[str, str, str], TacrepMessage] = {}
    order: list[tuple[str, str, str]] = []
    for message in messages:
        key = _identity_key(message)
        current = winners.get(key)
        if current is None:
            winners[key] = message
            order.append(key)
            continue
        winners[key] = _prefer(current, message)
    return tuple(winners[key] for key in order)


def pair_score(
    left: TacrepMessage,
    right: TacrepMessage,
    policy: CorrelationPolicy | None = None,
    *,
    same_cell: bool | None = None,
) -> float:
    """Score one pair. Speed is ignored, including ``minimum_transload_speed_mps``."""

    rules = policy or CorrelationPolicy()
    subject = 1.0 if left.subject and left.subject == right.subject else 0.0
    spatial = _spatial(left, right, same_cell)
    gap_minutes = abs((left.observed_at - right.observed_at).total_seconds()) / 60
    time_score = 1.0 - gap_minutes / rules.window_minutes
    if time_score < 0:
        time_score = 0.0
    score = (
        rules.subject_weight * subject
        + rules.spatial_weight * spatial
        + rules.time_weight * time_score
    )
    if explicitly_linked(left, right):
        score += rules.explicit_link_weight
    if score > 1:
        return 1.0
    return score


def correlate(
    messages: Sequence[TacrepMessage],
    policy: CorrelationPolicy | None = None,
    *,
    neighbors: NeighborLookup | None = None,
) -> tuple[Correlation, ...]:
    """Sessionize by gap from the first message, including H3 neighbors."""

    rules = policy or CorrelationPolicy()
    ordered = tuple(
        sorted(
            (message for message in messages if message.cell_id),
            key=lambda item: item.observed_at,
        )
    )
    sessions: list[list[TacrepMessage]] = []
    for message in ordered:
        placed = False
        for session in reversed(sessions):
            if _joins(session, message, rules, neighbors):
                session.append(message)
                placed = True
                break
        if not placed:
            sessions.append([message])
    results: list[Correlation] = []
    for session in sessions:
        bucket = tuple(session)
        best = 0.0
        for index, left in enumerate(bucket):
            for right in bucket[index + 1 :]:
                same = left.cell_id == right.cell_id
                best = max(best, pair_score(left, right, rules, same_cell=same))
        linked = any(
            explicitly_linked(left, right)
            for index, left in enumerate(bucket)
            for right in bucket[index + 1 :]
        )
        sources = tuple(dict.fromkeys(item.source_id for item in bucket if item.source_id))
        label = "possible"
        if len(sources) >= rules.minimum_sources and best >= rules.minimum_correlation_score:
            label = "probable"
        anchor = bucket[0]
        bmoa = next((item.bmoa for item in bucket if item.bmoa), "")
        activity = replay_activity(
            bucket,
            cell_id=anchor.cell_id,
            bmoa=bmoa,
            hide_gap_minutes=rules.hide_gap_minutes,
        )
        results.append(
            Correlation(
                cell_id=anchor.cell_id,
                window_start=anchor.observed_at,
                label=label,
                best_score=best,
                source_ids=sources,
                site_types=frozenset({state_site_type(activity.state)}),
                activity=activity,
                messages=bucket,
                link_weight=rules.explicit_link_weight if linked else 0.0,
                suggested_fusion=_suggest(bucket),
            )
        )
    return tuple(results)


def replay_activity(
    messages: Sequence[TacrepMessage],
    *,
    cell_id: str,
    bmoa: str,
    hide_gap_minutes: float,
    window_minutes: float = 45,
) -> Activity:
    """Replay messages in time order onto a new activity."""

    del window_minutes
    ordered = tuple(sorted(messages, key=lambda item: item.observed_at))
    first = ordered[0]
    activity = Activity(
        activity_id=f"{cell_id}:{first.observed_at.isoformat()}",
        state="pre_launch_hide",
        cell_id=cell_id,
        bmoa=bmoa or first.bmoa,
        latitude=first.latitude,
        longitude=first.longitude,
        opened_at=first.observed_at,
        updated_at=first.observed_at,
        subject=first.subject,
        acceptance=0.0,
        source_ids=tuple(dict.fromkeys(item.source_id for item in ordered if item.source_id)),
    )
    for message in ordered:
        activity = apply_message(activity, message, hide_gap_minutes)
    return activity


def apply_message(
    activity: Activity,
    message: TacrepMessage,
    hide_gap_minutes: float,
) -> Activity:
    """Advance for a time gap, then accept or reject this message."""

    activity = advance_for_gap(activity, message.observed_at, hide_gap_minutes)
    if message.kind == "O":
        return replace(
            activity,
            state="launch",
            updated_at=message.observed_at,
            acceptance=1.0,
            subject=message.subject or activity.subject,
            bmoa=activity.bmoa or message.bmoa,
        )
    if message.kind in _TRANSLOAD_KINDS:
        shared = bool(activity.bmoa) and message.bmoa == activity.bmoa
        if shared and activity.state == "hide_after_launch":
            return replace(
                activity,
                state="transload",
                bmoa=activity.bmoa or message.bmoa,
                updated_at=message.observed_at,
                acceptance=1.0,
            )
        return replace(activity, updated_at=message.observed_at, acceptance=0.0)
    return replace(activity, updated_at=message.observed_at, acceptance=0.0)


def advance_for_gap(activity: Activity, as_of: datetime, hide_gap_minutes: float) -> Activity:
    """Move launch or transload to the next hide state after a long gap."""

    gap_minutes = (as_of - activity.updated_at).total_seconds() / 60
    if gap_minutes <= hide_gap_minutes:
        return activity
    nxt = _NEXT_HIDE.get(activity.state)
    if nxt is None:
        return activity
    return replace(activity, state=nxt, updated_at=as_of, acceptance=1.0)


def manual_advance(activity: Activity, as_of: datetime) -> Activity:
    """Move one step forward. The unused ``closed`` state is not entered."""

    index = ACTIVITY_STATES.index(activity.state)
    if index + 1 >= len(ACTIVITY_STATES):
        return activity
    return replace(
        activity,
        state=ACTIVITY_STATES[index + 1],
        updated_at=as_of,
        acceptance=1.0,
    )


def correlation_evidence(correlation: Correlation) -> tuple[Evidence, ...]:
    """Explainable evidence. Acceptance stays 1.0 or 0.0 on the activity row."""

    accepted = correlation.activity.acceptance >= 1.0
    site_type = state_site_type(correlation.activity.state) if accepted else ""
    tacrep_type, block, warning, superseded = session_labels(correlation.messages)
    links = linked_ids(correlation.messages)
    detail = f"state {correlation.activity.state}"
    if warning:
        detail = f"{detail}; {warning}"
    if superseded:
        detail = f"{detail}; supersedes {', '.join(superseded)}"
    if links:
        detail = f"{detail}; links {', '.join(links)}"
    rows = [
        Evidence(
            kind="activity",
            weight=1.0 if accepted else 0.0,
            detail=detail,
            site_type=site_type,
            observed_at=correlation.activity.updated_at,
            source="tacrep",
            layer=correlation.cell_id,
            message_id=correlation.activity.activity_id,
            tacrep_type=tacrep_type,
            block=block,
            warning=warning,
            linked_ids=links,
        ),
        Evidence(
            kind="correlation",
            weight=correlation.best_score,
            detail=(
                f"{correlation.label}; score {correlation.best_score:.2f}; "
                f"sources {len(correlation.source_ids)}"
            ),
            observed_at=correlation.window_start,
            source="correlation",
            layer=correlation.cell_id,
            message_id=correlation.activity.activity_id,
            tacrep_type=tacrep_type,
            block=block,
            warning=warning,
            linked_ids=links,
        ),
    ]
    for message in correlation.messages:
        for word in _message_events(message):
            evidence_kind = _EVENT_KINDS.get(word.strip().lower())
            if evidence_kind is None:
                continue
            rows.append(
                Evidence(
                    kind=evidence_kind,
                    weight=1.0,
                    detail=f"event {word}",
                    observed_at=message.observed_at,
                    source=message.source_id or "tacrep",
                    layer=message.cell_id,
                    message_id=message.source_message_id,
                    tacrep_type=message.tacrep_type,
                    block=message.block_label,
                    warning=message.block_warning,
                    linked_ids=message.correlates_to,
                )
            )
    if correlation.link_weight > 0 and links:
        rows.append(
            Evidence(
                kind="explicit_link",
                weight=correlation.link_weight,
                detail=f"links {', '.join(links)}",
                observed_at=correlation.window_start,
                source="correlation",
                layer=correlation.cell_id,
                message_id=correlation.activity.activity_id,
                tacrep_type=tacrep_type,
                block=block,
                warning=warning,
                linked_ids=links,
            )
        )
    for message in correlation.messages:
        if message.vacant and message.vacant_weight:
            rows.append(
                Evidence(
                    kind="vacant",
                    weight=-abs(message.vacant_weight),
                    detail="TAI vacant",
                    observed_at=message.observed_at,
                    source=message.source_id or "tacrep",
                    layer=message.cell_id,
                    message_id=message.source_message_id,
                    tacrep_type=message.tacrep_type,
                    block=message.block_label,
                    warning=message.block_warning,
                )
            )
        for key, value in message.attributes:
            if key != "responds_to" or not value:
                continue
            rows.append(
                Evidence(
                    kind="response",
                    weight=0.2,
                    detail=f"responds to {value}",
                    observed_at=message.observed_at,
                    source=message.source_id or "tacrep",
                    layer=message.cell_id,
                    message_id=message.source_message_id,
                    tacrep_type=message.tacrep_type,
                    block=message.block_label,
                )
            )
    return tuple(rows)


def window_start(when: datetime, window_minutes: float) -> datetime:
    moment = ensure_utc(when)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    size = window_minutes * 60
    elapsed = (moment - epoch).total_seconds()
    bucket = (elapsed // size) * size
    return epoch + timedelta(seconds=bucket)


def classify_serial(
    kind_letter: str,
    serial: int,
    declared_kind: str,
    blocks: Sequence[TacrepBlock],
) -> tuple[str, str, str]:
    """Return TACREP type, block label, and a warning. Never drops the id."""

    warnings: list[str] = []
    letter = kind_letter.upper()
    if declared_kind and declared_kind not in {letter, "UNKNOWN"}:
        warnings.append(f"id kind {letter} contradicts declared kind {declared_kind}")
    configured = [block for block in blocks if block.kind.upper() == letter]
    matching = [block for block in configured if block.start <= serial <= block.end]
    if matching:
        block = matching[0]
        label = _block_label(block)
        return block.tacrep_type, label, "; ".join(warnings)
    tacrep_type = _KIND_TYPES.get(letter, "")
    if configured:
        label = ", ".join(_block_label(block) for block in configured)
        warnings.append(f"serial {letter}-{serial:03d} outside configured block {label}")
        if not tacrep_type:
            tacrep_type = configured[0].tacrep_type
        return tacrep_type, label, "; ".join(warnings)
    return tacrep_type, "", "; ".join(warnings)


def _joins(
    session: Sequence[TacrepMessage],
    message: TacrepMessage,
    policy: CorrelationPolicy,
    neighbors: NeighborLookup | None,
) -> bool:
    anchor = session[0]
    span = (message.observed_at - anchor.observed_at).total_seconds() / 60
    if span < 0 or span > policy.window_minutes:
        return False
    if _near_cell(anchor.cell_id, message.cell_id, policy.neighbor_k, neighbors):
        return True
    return any(
        within_ellipse(existing, message) or explicitly_linked(existing, message)
        for existing in session
    )


def _near_cell(
    anchor: str,
    cell_id: str,
    neighbor_k: int,
    neighbors: NeighborLookup | None,
) -> bool:
    if cell_id == anchor:
        return True
    if neighbor_k <= 0 or neighbors is None:
        return False
    return cell_id in neighbors(anchor, neighbor_k)


def _identity(
    kind: str,
    sequence: str,
    explicit_id: str,
    source_id: str,
    raw: str,
    policy: CorrelationPolicy,
) -> tuple[str, str, str, int, str, str, str]:
    """Return message id, base, full, rank, type, block, warning."""

    declared = kind
    explicit = parse_serial(explicit_id)
    prefix = parse_serial(f"{kind}-{sequence}") if kind != "UNKNOWN" else None
    if explicit is not None:
        chosen_serial = explicit
    elif prefix is not None:
        chosen_serial = prefix
    else:
        digest = hashlib.sha256(f"{source_id}\n{raw}".encode()).hexdigest()
        return digest[:32], "", "", -1, "", "", ""
    tacrep_type, block, warning = classify_serial(
        chosen_serial.kind,
        chosen_serial.serial,
        declared,
        policy.blocks,
    )
    return (
        chosen_serial.full_id,
        chosen_serial.base_id,
        chosen_serial.full_id,
        chosen_serial.rank,
        tacrep_type,
        block,
        warning,
    )


def parse_serial(token: str) -> "SerialId | None":
    """Parse ``I-103``, ``I-103A``, or ``UPDATE G-501``.

    A trailing letter is a correction. ``UPDATE`` supersedes corrections.
    """

    text = token.strip().upper()
    update = _UPDATE_RE.fullmatch(text)
    body = update.group(1) if update is not None else text
    match = _SERIAL_RE.fullmatch(body)
    if match is None:
        return None
    letter, digits, suffix = match.groups()
    rank = 0 if not suffix else ord(suffix) - ord("A") + 1
    full_id = f"{letter}-{digits}{suffix}"
    if update is not None:
        rank += 100
        full_id = f"UPDATE {full_id}"
    return SerialId(
        kind=letter,
        serial=int(digits),
        suffix=suffix,
        base_id=f"{letter}-{digits}",
        full_id=full_id,
        rank=rank,
        updated=update is not None,
    )


@dataclass(frozen=True)
class SerialId:
    kind: str
    serial: int
    suffix: str
    base_id: str
    full_id: str
    rank: int
    updated: bool = False


def _identity_key(message: TacrepMessage) -> tuple[str, str, str]:
    if message.serial_base:
        day = ensure_utc(message.observed_at).date().isoformat()
        return message.source_id, message.serial_base, day
    return message.source_id, message.source_message_id, ""


def _prefer(current: TacrepMessage, incoming: TacrepMessage) -> TacrepMessage:
    if incoming.correction_rank < 0 or incoming.serial_full == current.serial_full:
        return current
    if incoming.correction_rank > current.correction_rank:
        prior = replace(current, history=())
        return replace(incoming, history=current.history + (prior,))
    prior = replace(incoming, history=())
    return replace(current, history=current.history + (prior,))


def session_labels(
    messages: Sequence[TacrepMessage],
) -> tuple[str, str, str, tuple[str, ...]]:
    types = list(dict.fromkeys(item.tacrep_type for item in messages if item.tacrep_type))
    blocks = list(dict.fromkeys(item.block_label for item in messages if item.block_label))
    warnings = list(dict.fromkeys(item.block_warning for item in messages if item.block_warning))
    superseded: list[str] = []
    for item in messages:
        for prior in item.history:
            label = prior.serial_full or prior.source_message_id
            if label and label not in superseded:
                superseded.append(label)
    return ",".join(types), ",".join(blocks), "; ".join(warnings), tuple(superseded)


def _block_label(block: TacrepBlock) -> str:
    return f"{block.kind.upper()}-{block.start}..{block.kind.upper()}-{block.end}"


def _spatial(left: TacrepMessage, right: TacrepMessage, same_cell: bool | None) -> float:
    if within_ellipse(left, right):
        return 1.0
    shared_cell = same_cell
    if shared_cell is None:
        shared_cell = bool(left.cell_id) and left.cell_id == right.cell_id
    if shared_cell:
        return 1.0
    if left.bmoa and left.bmoa == right.bmoa:
        return 0.8
    if left.msr and left.msr == right.msr:
        return 0.6
    return 0.0


def _fields(parts: Sequence[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        found[key.strip().lower()] = value.strip()
    return found


def parse_time(text: str) -> datetime | None:
    """Parse an ISO-8601 timestamp. Naive values are UTC."""

    return _parse_time(text)


def _parse_time(text: str) -> datetime | None:
    token = text.strip()
    if not token:
        return None
    if token.endswith("Z"):
        token = token[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(token)
    except ValueError:
        return None
    return ensure_utc(parsed)


def within_ellipse(left: TacrepMessage, right: TacrepMessage) -> bool:
    """True when either point lies inside the other's ellipse or CE radius."""

    return _covers(left, right) or _covers(right, left)


def explicitly_linked(left: TacrepMessage, right: TacrepMessage) -> bool:
    """True when either message lists the other's serial."""

    left_ids = {item for item in (left.serial_base, left.serial_full) if item}
    right_ids = {item for item in (right.serial_base, right.serial_full) if item}
    return bool(set(left.correlates_to) & right_ids or set(right.correlates_to) & left_ids)


def linked_ids(messages: Sequence[TacrepMessage]) -> tuple[str, ...]:
    """Serials named by ``CORRELATES TO``, plus the message that names them."""

    found: list[str] = []
    for message in messages:
        if not message.correlates_to:
            continue
        for link in (message.serial_base, *message.correlates_to):
            if link and link not in found:
                found.append(link)
    return tuple(found)


def _covers(owner: TacrepMessage, point: TacrepMessage) -> bool:
    if owner.semi_major_m is not None and owner.semi_minor_m is not None:
        if owner.orientation_deg is None:
            return point_within_radius(
                point.latitude,
                point.longitude,
                owner.latitude,
                owner.longitude,
                owner.semi_major_m,
            )
        return point_within_ellipse(
            point.latitude,
            point.longitude,
            owner.latitude,
            owner.longitude,
            owner.semi_major_m,
            owner.semi_minor_m,
            owner.orientation_deg,
        )
    if owner.ce_radius_m is not None:
        return point_within_radius(
            point.latitude,
            point.longitude,
            owner.latitude,
            owner.longitude,
            owner.ce_radius_m,
        )
    return False


def _suggest(messages: Sequence[TacrepMessage]) -> SuggestedFusion | None:
    """Name the highest-accuracy TACREP when one ellipse contains another."""

    involved: dict[str, TacrepMessage] = {}
    reasons: list[str] = []
    for index, left in enumerate(messages):
        for right in messages[index + 1 :]:
            if not within_ellipse(left, right):
                continue
            for message in (left, right):
                key = message.serial_base or message.source_message_id
                if key:
                    involved[key] = message
            owner, point = (left, right) if _covers(left, right) else (right, left)
            kind = "CE" if owner.semi_major_m is None else "ellipse"
            reasons.append(f"{_id_label(point)} within {_id_label(owner)} {kind}")
    if len(involved) < 2:
        return None
    anchor = min(involved.values(), key=_accuracy_key)
    return SuggestedFusion(
        anchor_id=anchor.serial_base or anchor.source_message_id,
        linked_ids=tuple(sorted(involved)),
        reason="; ".join(dict.fromkeys(reasons)),
    )


def _accuracy_key(message: TacrepMessage) -> tuple[float, float, str]:
    """Lower is more accurate: lowest CAT, then smallest ellipse area or CE."""

    cat = float(message.cat_level) if message.cat_level is not None else 1_000_000_000.0
    if message.semi_major_m is not None and message.semi_minor_m is not None:
        size = message.semi_major_m * message.semi_minor_m
    elif message.ce_radius_m is not None:
        size = message.ce_radius_m ** 2
    else:
        size = 1_000_000_000_000_000_000.0
    return (cat, size, message.serial_base)


def _subject_confidence(
    subject: str,
    phrase: str,
    level: str,
    band: str,
    weight: float | None,
    terms: Sequence[ConfidenceTerm],
) -> tuple[str, str, str, str, float | None]:
    """Strip a confidence phrase from the subject when the field did not set one."""

    term, rest = split_reported_confidence(subject, terms)
    if term is None:
        return subject, phrase, level, band, weight
    if phrase:
        return rest, phrase, level, band, weight
    return rest, term.phrase, term.level, term.band, term.weight


def _id_label(message: TacrepMessage) -> str:
    return message.serial_base or message.serial_full or message.source_message_id


def _message_events(message: TacrepMessage) -> tuple[str, ...]:
    if message.events:
        return message.events
    if message.event.strip():
        return (message.event,)
    return ()


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None
