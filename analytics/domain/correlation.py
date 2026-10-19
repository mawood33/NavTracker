"""TACREP messages, activity states, and pair-match correlation.

``https://github.com/mawood33/TransloadOracle`` still returned 404 from
this environment, including branch ``feat/python-services`` at
``3f473a8``. The rules in this module follow the owner's extracted
description of ``apps/processing`` and are the stand-in until that tree
can be read directly.

A TACREP line is ``KIND-seq//time//lat,lon//key=value...``. Kinds are
``O`` (launch), ``I`` (imagery), ``J`` (movement), ``E`` (electronic),
and ``UNKNOWN``. Times are ISO-8601 UTC.

Activities move ``pre_launch_hide -> launch -> hide_after_launch ->
transload -> hide_after_transload``. ``closed`` is not used. An ``O``
message enters ``launch``. A gap longer than ``hide_gap_minutes`` moves
``launch`` to ``hide_after_launch`` and ``transload`` to
``hide_after_transload``. A ``J``, ``I``, or ``E`` message enters
``transload`` only from ``hide_after_launch`` when it shares a BMOA.
Accepted evidence is 1.0 and rejected evidence is 0.0.

Pair score is ``0.45 * same_subject + 0.35 * spatial + 0.20 * time``.
Spatial is 1.0 in the same H3 cell, 0.8 for a shared BMOA, and 0.6 for
a shared MSR. Time is ``1 - gap / window``. A group is ``probable`` when
it has at least three source ids and a best score of at least 0.70.
``minimum_transload_speed_mps`` is stored and is not used.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from analytics.domain.reference import Evidence

ACTIVITY_STATES = (
    "pre_launch_hide",
    "launch",
    "hide_after_launch",
    "transload",
    "hide_after_transload",
)
_KINDS = frozenset({"O", "I", "J", "E", "UNKNOWN"})
_TRANSLOAD_KINDS = frozenset({"J", "I", "E"})
_NEXT_HIDE = {"launch": "hide_after_launch", "transload": "hide_after_transload"}


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
class Correlation:
    """Messages that share an H3 cell and a 45-minute window."""

    cell_id: str
    window_start: datetime
    label: str
    best_score: float
    source_ids: tuple[str, ...]
    site_types: frozenset[str]
    activity: Activity
    messages: tuple[TacrepMessage, ...]


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
) -> TacrepMessage | None:
    """Parse one ``KIND-seq//time//lat,lon//key=value`` line."""

    body = text.strip()
    if "//" not in body:
        return None
    parts = [part.strip() for part in body.split("//")]
    if len(parts) < 3 or "-" not in parts[0]:
        return None
    kind, sequence = parts[0].split("-", 1)
    kind = kind.strip().upper()
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
    fields = _fields(parts[3:])
    subject = fields.get("subject") or fields.get("id") or ""
    message_id = fields.get("id") or fallback_message_id or sequence.strip()
    return TacrepMessage(
        kind=kind,
        sequence=sequence.strip(),
        observed_at=observed,
        latitude=latitude,
        longitude=longitude,
        source_id=source_id,
        source_message_id=message_id,
        subject=subject,
        speed_mps=_optional_float(fields.get("speed")),
        heading=_optional_float(fields.get("heading")),
        length_m=_optional_float(fields.get("length")),
        width_m=_optional_float(fields.get("width")),
        reported_confidence=_optional_float(fields.get("confidence")),
        bmoa=fields.get("bmoa", ""),
        msr=fields.get("msr", ""),
        raw=body,
    )


def message_from_event(
    payload: Mapping[str, object],
    *,
    source_id: str,
    fallback_message_id: str = "",
) -> TacrepMessage | None:
    """Accept a JSON event or a JSON object that wraps a TACREP line."""

    wrapped = payload.get("tacrep", payload.get("message", payload.get("body")))
    if isinstance(wrapped, str) and "//" in wrapped:
        source = str(payload.get("source_id", source_id) or source_id)
        message_id = str(payload.get("source_message_id", fallback_message_id) or "")
        return parse_tacrep_line(
            wrapped,
            source_id=source,
            fallback_message_id=message_id,
        )
    kind = str(payload.get("kind", payload.get("type", "")) or "").strip().upper()
    if kind not in _KINDS:
        return None
    observed = _parse_time(str(payload.get("time", payload.get("timestamp", "")) or ""))
    latitude = _optional_float(payload.get("latitude", payload.get("lat")))
    longitude = _optional_float(payload.get("longitude", payload.get("lon")))
    if observed is None or latitude is None or longitude is None:
        return None
    message_id = str(
        payload.get("source_message_id", payload.get("id", fallback_message_id)) or ""
    )
    subject = str(payload.get("subject", payload.get("id", "")) or "")
    return TacrepMessage(
        kind=kind,
        sequence=str(payload.get("sequence", "") or ""),
        observed_at=observed,
        latitude=latitude,
        longitude=longitude,
        source_id=str(payload.get("source_id", source_id) or source_id),
        source_message_id=message_id,
        subject=subject,
        speed_mps=_optional_float(payload.get("speed")),
        heading=_optional_float(payload.get("heading")),
        length_m=_optional_float(payload.get("length")),
        width_m=_optional_float(payload.get("width")),
        reported_confidence=_optional_float(payload.get("confidence")),
        bmoa=str(payload.get("bmoa", "") or ""),
        msr=str(payload.get("msr", "") or ""),
    )


def dedupe_messages(messages: Sequence[TacrepMessage]) -> tuple[TacrepMessage, ...]:
    """Keep the first copy of each source id and source message id."""

    found: dict[tuple[str, str], TacrepMessage] = {}
    for message in messages:
        found.setdefault((message.source_id, message.source_message_id), message)
    return tuple(found.values())


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
    return (
        rules.subject_weight * subject
        + rules.spatial_weight * spatial
        + rules.time_weight * time_score
    )


def correlate(
    messages: Sequence[TacrepMessage],
    policy: CorrelationPolicy | None = None,
) -> tuple[Correlation, ...]:
    """Group by H3 cell and window, then label each group."""

    rules = policy or CorrelationPolicy()
    grouped: dict[tuple[str, datetime], list[TacrepMessage]] = {}
    for message in messages:
        if not message.cell_id:
            continue
        key = (message.cell_id, window_start(message.observed_at, rules.window_minutes))
        grouped.setdefault(key, []).append(message)
    results: list[Correlation] = []
    for (cell_id, start), bucket in sorted(grouped.items()):
        ordered = tuple(sorted(bucket, key=lambda item: item.observed_at))
        best = 0.0
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                best = max(best, pair_score(left, right, rules, same_cell=True))
        sources = tuple(dict.fromkeys(item.source_id for item in ordered if item.source_id))
        label = "possible"
        if len(sources) >= rules.minimum_sources and best >= rules.minimum_correlation_score:
            label = "probable"
        bmoa = next((item.bmoa for item in ordered if item.bmoa), "")
        activity = replay_activity(
            ordered,
            cell_id=cell_id,
            bmoa=bmoa,
            hide_gap_minutes=rules.hide_gap_minutes,
            window_minutes=rules.window_minutes,
        )
        results.append(
            Correlation(
                cell_id=cell_id,
                window_start=start,
                label=label,
                best_score=best,
                source_ids=sources,
                site_types=frozenset({state_site_type(activity.state)}),
                activity=activity,
                messages=ordered,
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

    ordered = tuple(sorted(messages, key=lambda item: item.observed_at))
    first = ordered[0]
    activity = Activity(
        activity_id=f"{cell_id}:{window_start(first.observed_at, window_minutes).isoformat()}",
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
    return (
        Evidence(
            kind="activity",
            weight=1.0 if accepted else 0.0,
            detail=f"state {correlation.activity.state}",
            site_type=site_type,
            observed_at=correlation.activity.updated_at,
            source="tacrep",
            layer=correlation.cell_id,
            message_id=correlation.activity.activity_id,
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
        ),
    )


def window_start(when: datetime, window_minutes: float) -> datetime:
    moment = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    size = window_minutes * 60
    elapsed = (moment - epoch).total_seconds()
    bucket = (elapsed // size) * size
    return epoch + timedelta(seconds=bucket)


def _spatial(left: TacrepMessage, right: TacrepMessage, same_cell: bool | None) -> float:
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
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


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
