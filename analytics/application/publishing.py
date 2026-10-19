"""ChatSurfer event publish and confidence/CAT room dispatch."""

import json
import logging

from analytics.application.dispatch import (
    active_publish_table,
    destination_rooms,
)
from analytics.application.events import event_document
from analytics.application.query import QueryService
from analytics.application.state import AnalyticsState
from analytics.domain.level_assessment import (
    annotate_report,
    render_level,
)
from analytics.domain.outputs import (
    diff_documents,
)
from analytics.domain.publish import (
    PHRASE_RANK,
    PublishNotice,
    PublishTable,
    phrase_key,
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
        for notice, rooms in self._notices(table):
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
            state._published_phrases[notice.identity] = notice.confidence

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
    ) -> list[tuple[PublishNotice, tuple[str, ...]]]:
        state = self._state
        catalog = self._query.assessment_catalog()
        found: list[tuple[PublishNotice, tuple[str, ...]]] = []
        for message in state._messages:
            view = catalog.for_message(message)
            phrase = view.assessed_phrase
            identity = message.serial_full or message.source_message_id
            previous = state._published_phrases.get(identity, "")
            remark = render_level(view, catalog.wording, previous=previous)
            notice = PublishNotice(
                identity=identity,
                confidence=phrase,
                cat=message.cat_level,
                types=(message.tacrep_type,) if message.tacrep_type else (),
                sources=(message.source_id,) if message.source_id else (),
                body=annotate_report(message.raw or identity, remark),
                level_rank=PHRASE_RANK.get(phrase_key(phrase), 0),
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
            view = catalog.for_site(site)
            phrase = view.assessed_phrase
            identity = f"site:{site.name}"
            previous = state._published_phrases.get(identity, "")
            remark = render_level(view, catalog.wording, previous=previous)
            notice = PublishNotice(
                identity=identity,
                confidence=phrase,
                cat=None,
                types=tuple(sorted(site.site_types)),
                sources=("site",),
                body=f"site {site.name} {remark}",
                level_rank=PHRASE_RANK.get(phrase_key(phrase), 0),
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

