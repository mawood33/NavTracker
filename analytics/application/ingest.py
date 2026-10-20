"""Fixes, TACREP, ADHOC, and SITREP commands, plus room reads."""

import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from analytics.application.dispatch import (
    ingest_room_list,
    truth_read_rooms,
)
from analytics.application.hub import OutputHub
from analytics.application.room_memory import RoomMemory, note_message
from analytics.application.state import AnalyticsState
from analytics.domain.chat_text import (
    apply_geotag,
    prepare_body,
    skip_message,
    tag_attributes,
)
from analytics.domain.correlation import (
    TacrepMessage,
    dedupe_messages,
    message_from_event,
)
from analytics.domain.outputs import SCHEMA_NAME, SCHEMA_VERSION, track_document
from analytics.domain.reference import (
    match_bmoa,
    match_msr,
    named_bmoa,
    named_msr,
)
from analytics.domain.reports import (
    Report,
    dedupe_reports,
    parse_tacrep,
)
from analytics.domain.tacrep_parse import parse_exercise_text
from analytics.domain.templates import Tasking, link_responses
from analytics.domain.timeutil import ensure_utc, valid_position
from analytics.domain.tracks import GpsFix, fix_from_mapping
from navtracker_common.chatsurfer import ChatReadError, ChatRoomMessage, backfill_window

logger = logging.getLogger(__name__)


class IngestService:
    def __init__(self, state: AnalyticsState) -> None:
        self._state = state

    def add_fixes(self, fixes: list[GpsFix]) -> None:
        state = self._state
        if not fixes:
            return
        kept: list[GpsFix] = []
        for fix in fixes:
            if not valid_position(fix.latitude, fix.longitude):
                continue
            kept.append(replace(fix, timestamp=ensure_utc(fix.timestamp)))
        state._fixes.extend(kept)
        state._fixes = _limit_fixes(
            state._fixes,
            state.settings.fix_retention_seconds,
            state.settings.max_fixes_per_vehicle,
        )

    def add_reports(self, reports: list[Report]) -> None:
        state = self._state
        if not reports:
            return
        normalized = [
            replace(report, observed_at=ensure_utc(report.observed_at)) for report in reports
        ]
        combined = dedupe_reports((*state._reports, *normalized))
        state._reports = combined[-2000:]

    def add_messages(self, messages: list[TacrepMessage]) -> None:
        state = self._state
        if not messages:
            return
        stamped: list[TacrepMessage] = []
        for message in messages:
            if not valid_position(message.latitude, message.longitude):
                continue
            cell_id = state._index.cell_id(message.latitude, message.longitude)
            bmoa = named_bmoa(message.bmoa, state._bmoas) if message.bmoa else ""
            if message.bmoa and bmoa not in {area.name for area in state._bmoas}:
                for site in state._sites:
                    if site.name.strip().upper() == message.bmoa.strip().upper():
                        bmoa = site.name
                        break
            if not bmoa:
                area = match_bmoa(message.latitude, message.longitude, state._bmoas)
                if area is not None:
                    bmoa = area.name
            msr_name = named_msr(message.msr, state._msrs) if message.msr else ""
            if not msr_name:
                route = match_msr(
                    message.latitude,
                    message.longitude,
                    state._msrs,
                    state.settings.thresholds.msr_corridor_meters,
                )
                if route is not None:
                    msr_name = route.name
            stamped.append(
                replace(
                    message,
                    observed_at=ensure_utc(message.observed_at),
                    cell_id=cell_id,
                    bmoa=bmoa,
                    msr=msr_name,
                    vacant_weight=state.settings.vacant_evidence_weight,
                )
            )
        combined = dedupe_messages((*state._messages, *stamped))[-2000:]
        state._messages = link_responses(
            combined,
            state._taskings,
            match_meters=state._coord_card.mismatch_meters,
        )
        state._refresh_correlations()

    def ingest_event(
        self,
        payload: Mapping[str, object],
        source_id: str = "",
    ) -> dict[str, object]:
        messages = self._messages_from_payload(payload, source_id)
        if not messages:
            raise ValueError("unreadable event")
        self.add_messages(messages)
        return {"accepted": len(messages)}

    def ingest_tacrep_body(self, body: str, source_id: str) -> dict[str, object]:
        messages, taskings = self._read_text(body, source_id, None)
        if not messages and not taskings:
            raise ValueError("unreadable tacrep")
        if messages:
            self.add_messages(messages)
        return {"accepted": len(messages), "taskings": len(taskings)}

    def ingest_batch(
        self,
        payload: Mapping[str, object],
        source_id: str = "",
    ) -> dict[str, object]:
        raw = payload.get("events", payload.get("messages", []))
        if not isinstance(raw, list):
            raise ValueError("events must be a list")
        batch_source = str(payload.get("source_id") or source_id)
        found: list[TacrepMessage] = []
        for item in raw:
            if isinstance(item, str):
                messages, _taskings = self._read_text(item, batch_source, None)
                found.extend(messages)
            elif isinstance(item, dict):
                found.extend(self._messages_from_payload(item, batch_source))
        if found:
            self.add_messages(found)
        return {"accepted": len(found)}

    async def poll_chat(self) -> list[GpsFix]:
        state = self._state
        enabled = state.settings.chat_read_enabled and state.settings.read_rooms
        if state.chat is None or not enabled:
            return []
        found: list[GpsFix] = []
        for room in truth_read_rooms(state.settings):
            fixes, _reports = await self._pull_room(room, state._cursors)
            found.extend(fixes)
        return found

    async def poll_tacrep(self) -> list[Report]:
        state = self._state
        rooms = ingest_room_list(state._coord_card, state.settings)
        if state.chat is None or not state.settings.chat_read_enabled or not rooms:
            return []
        found: list[Report] = []
        for room in rooms:
            _fixes, reports = await self._pull_room(room, state._report_cursors)
            found.extend(reports)
        return found

    async def poll_scrubbed(self) -> None:
        state = self._state
        client = state.chat
        if client is None or not state.settings.chat_read_enabled:
            return
        page = 1
        while page <= 1000:
            try:
                ids = await client.scrubbed_page(page, 100)
            except ChatReadError as exc:
                if exc.kind != "rate":
                    logger.warning(
                        "ChatSurfer scrub poll failed status=%s path=%s",
                        exc.status_code,
                        exc.path,
                    )
                return
            fresh = [item for item in ids if item not in state._scrubbed_ids]
            for message_id in fresh:
                self.retract(message_id)
                state._scrubbed_ids.add(message_id)
            if len(ids) < 100:
                return
            page += 1

    def retract(self, message_id: str) -> None:
        """Drop one chat message and the evidence derived from it.

        Chat text is not written to disk. The in-memory catalog, the live
        snapshot inputs, and a retract event are the stores that change.
        The log line is the id only.
        """

        if not message_id:
            return
        state = self._state
        kept: list[TacrepMessage] = []
        for item in state._messages:
            revised = _without_chat(item, message_id)
            if revised is not None:
                kept.append(revised)
        state._messages = tuple(kept)
        state._reports = tuple(
            report
            for report in state._reports
            if report.message_id != message_id and report.chat_message_id != message_id
        )
        removed = [fix for fix in state._fixes if fix.origin_id == message_id]
        state._fixes = [fix for fix in state._fixes if fix.origin_id != message_id]
        _drop_retracted_tracks(state.hub, message_id)
        state._taskings = tuple(
            task for task in state._taskings if task.chat_message_id != message_id
        )
        state._refresh_correlations()
        state.hub.broadcast(
            {
                "schema": SCHEMA_NAME,
                "schema_version": SCHEMA_VERSION,
                "type": "retract",
                "message_id": message_id,
                "tracks": [track_document(fix) for fix in removed],
            }
        )
        logger.info("scrubbed %s", message_id)

    async def poll_report_layers(self) -> list[Report]:
        state = self._state
        source = state.report_source
        if source is None:
            return []
        return await source.poll()

    async def _pull_room(
        self,
        room: str,
        cursors: dict[str, str],
    ) -> tuple[list[GpsFix], list[Report]]:
        state = self._state
        client = state.chat
        memory = state._room_memory.setdefault(room, RoomMemory())
        if client is None or not state._chat_reads.allow(room):
            return [], []
        try:
            if not memory.backfilled:
                await self._backfill(room, memory)
            since = memory.last_id or cursors.get(room) or None
            messages = await client.read_room(room, since=since)
        except ChatReadError as exc:
            if exc.kind == "empty":
                state._chat_reads.record_empty(room)
                return [], []
            if exc.kind == "rate":
                return [], []
            state._chat_reads.record_error(
                room,
                status_code=exc.status_code,
                path=exc.path or client.read_spec.path,
                kind=exc.kind,
                initial=state.settings.chat_poll_seconds,
            )
            return [], []
        state._chat_reads.record_ok(room)
        fixes, reports = self.consume_messages(room, messages)
        if memory.last_id:
            cursors[room] = memory.last_id
        return fixes, reports

    async def _backfill(self, room: str, memory: RoomMemory) -> None:
        state = self._state
        client = state.chat
        memory.backfilled = True
        if (
            client is None
            or state.settings.chat_backfill_minutes <= 0
            or not hasattr(client, "search_messages")
        ):
            return
        start, end = backfill_window(state.settings.chat_backfill_minutes, datetime.now(UTC))
        cursor = ""
        seen: set[str] = set()
        window: dict[str, object] | None = None
        while True:
            try:
                page = await client.search_messages(
                    room_name=room,
                    start=start,
                    end=end,
                    limit=state.settings.chat_page_size,
                    cursor=cursor,
                    time_filter=window,
                )
            except ChatReadError as exc:
                if exc.kind != "rate":
                    logger.warning(
                        "ChatSurfer backfill failed status=%s path=%s room=%s",
                        exc.status_code,
                        exc.path,
                        room,
                    )
                return
            raw = page.get("messages", [])
            decoded: list[ChatRoomMessage] = []
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        decoded.append(client._decode_room_message(item, room, client.read_spec))
            if decoded:
                self.consume_messages(room, decoded)
            returned = page.get("searchTimeFilter")
            if isinstance(returned, dict):
                window = {str(key): value for key, value in returned.items()}
            mark = page.get("nextCursorMark")
            text = mark if isinstance(mark, str) else ""
            if not text or text == "*" or text == cursor or text in seen:
                return
            seen.add(text)
            cursor = text

    def consume_messages(
        self,
        room: str,
        messages: list[ChatRoomMessage],
    ) -> tuple[list[GpsFix], list[Report]]:
        state = self._state
        memory = state._room_memory.setdefault(room, RoomMemory())
        fixes: list[GpsFix] = []
        reports: list[Report] = []
        pending: list[TacrepMessage] = []
        for message in messages:
            decision = note_message(memory, message, state.settings.chat_seen_limit)
            if decision == "duplicate":
                continue
            if skip_message(
                message,
                nickname=state.settings.chatsurfer_nickname,
                skip_bots=state.settings.chat_skip_bots,
                exercise_only=state.settings.chat_exercise_only,
            ):
                continue
            if decision == "edit":
                self.retract(message.message_id)
            body = prepare_body(message)
            payload = _json_object(body)
            if payload is not None:
                if not _float_present(payload) and message.geo_points:
                    latitude, longitude = message.geo_points[0]
                    payload = {**payload, "latitude": latitude, "longitude": longitude}
                fix = fix_from_mapping(
                    payload,
                    source=f"chatsurfer:{room}",
                    speed_unit=state.settings.chat_speed_unit,
                )
                if fix is not None:
                    fixes.append(replace(fix, origin_id=message.message_id))
            parsed, taskings = self._read_text(body, room, message.timestamp)
            if taskings:
                stamped_tasks = tuple(
                    replace(task, chat_message_id=message.message_id) for task in taskings
                )
                state._taskings = (*state._taskings[: -len(taskings)], *stamped_tasks)
            stamped = [
                tag_attributes(
                    apply_geotag(
                        replace(item, chat_message_id=message.message_id),
                        message.geo_points,
                        state._coord_card.mismatch_meters,
                    ),
                    message.tags,
                )
                for item in parsed
            ]
            if stamped:
                pending.extend(stamped)
                continue
            report = parse_tacrep(
                body,
                layer=room,
                message_id=message.message_id,
                received_at=message.timestamp,
                policy=state.settings.report_policy,
            )
            if report is not None:
                reports.append(replace(report, chat_message_id=message.message_id))
        if pending:
            self.add_messages(pending)
        if reports:
            self.add_reports(reports)
        if fixes:
            self.add_fixes(fixes)
        return fixes, reports

    def _messages_from_payload(
        self,
        payload: Mapping[str, object],
        source_id: str,
    ) -> list[TacrepMessage]:
        state = self._state
        source = str(payload.get("source_id") or source_id)
        wrapped = payload.get("tacrep", payload.get("message", payload.get("body")))
        if isinstance(wrapped, str):
            messages, taskings = self._read_text(wrapped, source, None)
            if messages or taskings:
                return messages
        message = message_from_event(payload, source_id=source, policy=state._policy())
        if message is None:
            return []
        return [message]

    def _read_text(
        self,
        body: str,
        source_id: str,
        observed_at: datetime | None,
    ) -> tuple[list[TacrepMessage], tuple[Tasking, ...]]:
        state = self._state
        messages, taskings = parse_exercise_text(
            body,
            source_id=source_id,
            observed_at=observed_at,
            card=state._coord_card,
            grid=state._mgrs,
            policy=state._policy(),
        )
        if taskings:
            state._taskings = (*state._taskings, *taskings)
            if state._messages:
                state._messages = link_responses(
                    state._messages,
                    state._taskings,
                    match_meters=state._coord_card.mismatch_meters,
                )
                state._refresh_correlations()
        return list(messages), taskings


def _limit_fixes(fixes: list[GpsFix], seconds: float, per_vehicle: int) -> list[GpsFix]:
    """Apply retention, then keep the newest ``per_vehicle`` fixes.

    ``seconds`` of 0 keeps every timestamp. ``per_vehicle`` of 0 keeps
    every fix that survived retention.
    """

    retained = _retain_fixes(fixes, seconds)
    return _cap_fixes(retained, per_vehicle)


def _retain_fixes(fixes: list[GpsFix], seconds: float) -> list[GpsFix]:
    """Drop fixes older than ``seconds`` before each vehicle's newest fix."""

    if seconds <= 0 or not fixes:
        return fixes
    newest: dict[str, datetime] = {}
    for fix in fixes:
        current = newest.get(fix.vehicle_id)
        if current is None or fix.timestamp > current:
            newest[fix.vehicle_id] = fix.timestamp
    kept: list[GpsFix] = []
    for fix in fixes:
        cutoff = newest[fix.vehicle_id] - timedelta(seconds=seconds)
        if fix.timestamp >= cutoff:
            kept.append(fix)
    return kept


def _cap_fixes(fixes: list[GpsFix], per_vehicle: int) -> list[GpsFix]:
    if per_vehicle <= 0 or not fixes:
        return fixes
    grouped: dict[str, list[GpsFix]] = {}
    for fix in fixes:
        grouped.setdefault(fix.vehicle_id, []).append(fix)
    allowed: set[int] = set()
    for group in grouped.values():
        ordered = sorted(group, key=lambda item: item.timestamp)
        for fix in ordered[-per_vehicle:]:
            allowed.add(id(fix))
    return [fix for fix in fixes if id(fix) in allowed]


def _drop_retracted_tracks(hub: OutputHub, message_id: str) -> None:
    """A reconnect must not receive a fix that a retract already removed."""

    snapshot = hub.latest_snapshot()
    if snapshot is None or not message_id:
        return
    tracks = snapshot.get("tracks")
    if not isinstance(tracks, list):
        return
    kept = [
        item
        for item in tracks
        if not (isinstance(item, dict) and item.get("origin_id") == message_id)
    ]
    if len(kept) == len(tracks):
        return
    patched = dict(snapshot)
    patched["tracks"] = kept
    hub.set_snapshot(patched)

def _float_present(payload: Mapping[str, object]) -> bool:
    for key in ("latitude", "lat", "longitude", "lon", "lng"):
        if payload.get(key) is not None:
            return True
    return False


def _without_chat(message: TacrepMessage, chat_id: str) -> TacrepMessage | None:
    if message.chat_message_id == chat_id:
        return None
    history = tuple(item for item in message.history if item.chat_message_id != chat_id)
    if history == message.history:
        return message
    return replace(message, history=history)


def _json_object(body: str) -> dict[str, object] | None:
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if isinstance(payload, dict):
        return payload
    return None

