"""ChatSurfer event publish and confidence/CAT room dispatch."""

import json
import logging

from analytics.application.dispatch import (
    active_publish_table,
    destination_rooms,
    phrase_for_message,
)
from analytics.application.events import event_document
from analytics.application.query import QueryService
from analytics.application.state import AnalyticsState
from analytics.domain.confidence_text import ConfidenceTerm
from analytics.domain.outputs import (
    diff_documents,
)
from analytics.domain.publish import (
    PHRASE_RANK,
    PublishNotice,
    PublishTable,
    phrase_for_confidence,
    should_publish,
)
from navtracker_common.errors import PublicationError

logger = logging.getLogger(__name__)


class PublishService:
    def __init__(self, state: AnalyticsState, query: QueryService) -> None:
        self._state = state
        self._query = query

    async def publish_if_configured(self) -> None:
        state = self._state
        client = state.chat
        if client is None or not state.settings.publish_events:
            return
        document = event_document(
            state._slowdowns, state._stops, state._mills, state._areas
        )
        text = json.dumps(document, separators=(",", ":"))
        if text == state._published:
            return
        try:
            await client.publish_text(text)
        except PublicationError:
            logger.exception("Analytics publication failed")
            return
        state._published = text

    async def publish_routed(self) -> None:
        """Publish confidence and CAT notices. No rooms means no publish."""
        state = self._state

        publisher = state._publisher
        if publisher is None:
            return
        table = active_publish_table(state._coord_card, state.settings)
        high_at = _high_at(state._terms())
        for notice, rooms in self._notices(table, high_at):
            if not rooms:
                continue
            if not should_publish(
                state._published_levels.get(notice.identity),
                notice.level_rank,
                republish_on_higher_level=table.republish_on_higher_level,
            ):
                continue
            logger.info("Publishing %s to %s", notice.identity, list(rooms))
            for room in rooms:
                await publisher.publish_to(room, notice.body)
            state._published_levels[notice.identity] = notice.level_rank

    def publish_output(self) -> dict[str, object] | None:
        """Store a snapshot and broadcast it, or a later diff.

        The first call broadcasts the snapshot. A later call broadcasts
        only when sites, cells, or corridor reports changed.
        """
        state = self._state

        current = self._query.output_snapshot()
        previous = state._output
        state._output = current
        state.hub.set_snapshot(current)
        if previous is None:
            state.hub.broadcast(current)
            return current
        update = diff_documents(previous, current)
        if update is None:
            return None
        state.hub.broadcast(update)
        return update

    def _notices(
        self,
        table: PublishTable,
        high_at: float,
    ) -> list[tuple[PublishNotice, tuple[str, ...]]]:
        state = self._state
        probable = state.settings.confidence.probable_at
        confirmed = state.settings.confidence.confirmed_at
        found: list[tuple[PublishNotice, tuple[str, ...]]] = []
        for message in state._messages:
            phrase = phrase_for_message(
                message,
                probable_at=probable,
                confirmed_at=confirmed,
                high_at=high_at,
            )
            identity = message.serial_full or message.source_message_id
            notice = PublishNotice(
                identity=identity,
                confidence=phrase,
                cat=message.cat_level,
                types=(message.tacrep_type,) if message.tacrep_type else (),
                sources=(message.source_id,) if message.source_id else (),
                body=message.raw or identity,
                level_rank=PHRASE_RANK.get(phrase, 0),
            )
            rooms = destination_rooms(
                message.tacrep_type,
                confidence=phrase,
                cat=message.cat_level,
                sources=notice.sources,
                report_room=message.report_room,
                card=state._coord_card,
                settings=state.settings,
                table=table,
            )
            found.append((notice, rooms))
        for site in state._sites:
            phrase = phrase_for_confidence(site.confidence, probable, confirmed, high_at)
            notice = PublishNotice(
                identity=f"site:{site.name}",
                confidence=phrase,
                cat=None,
                types=tuple(sorted(site.site_types)),
                sources=("site",),
                body=f"site {site.name} {phrase}",
                level_rank=PHRASE_RANK.get(phrase, 0),
            )
            rooms = destination_rooms(
                "",
                confidence=phrase,
                cat=None,
                sources=notice.sources,
                report_room="",
                card=state._coord_card,
                settings=state.settings,
                table=table,
            )
            found.append((notice, rooms))
        return found


def _high_at(terms: tuple[ConfidenceTerm, ...]) -> float:
    for term in terms:
        if term.phrase == "POSS HIGH":
            return term.weight
    return 0.45

