"""Reported confidence versus the level NavTracker assessed.

A message keeps the phrase parsed from its own text. Correlation can
raise the assessed level. A stated CONFIRMED that the correlation does
not support stays on the report and is flagged. Wording is a coord-card
template. The remark never contains ``//``.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from analytics.domain.assessment_text import AssessmentWording, fill_template
from analytics.domain.confidence_text import ConfidenceTerm
from analytics.domain.correlation import Correlation, TacrepMessage
from analytics.domain.publish import phrase_for_level, phrase_key
from analytics.domain.reference import Evidence, Site

_RANK = {"": 0, "possible": 1, "probable": 2, "confirmed": 3}


@dataclass(frozen=True)
class LevelView:
    """One item's stated level and NavTracker's assessed level."""

    reported_phrase: str
    reported_level: str
    assessed_phrase: str
    assessed_level: str
    confidence: float
    basis: str
    contributor_ids: tuple[str, ...]
    disagreement: bool


def normalize_level(level: str) -> str:
    """Accept ``verified`` as ``confirmed``."""

    token = level.strip().lower()
    if token == "verified":
        return "confirmed"
    if token in {"possible", "probable", "confirmed"}:
        return token
    return ""


def level_document(view: LevelView) -> dict[str, object]:
    """Fields shared by sites, correlations, and evidence."""

    return {
        "reported_level": view.reported_level,
        "reported_phrase": view.reported_phrase,
        "assessed_level": view.assessed_level,
        "assessed_phrase": view.assessed_phrase,
        "assessed_confidence": view.confidence,
        "basis": view.basis,
        "contributor_ids": list(view.contributor_ids),
        "disagreement": view.disagreement,
    }


def render_level(
    view: LevelView,
    wording: AssessmentWording | None = None,
    *,
    previous: str = "",
) -> str:
    """One compact remark. A higher republish names the previous phrase."""

    template = wording or AssessmentWording()
    ids = " + ".join(view.contributor_ids)
    reported = view.reported_phrase or view.reported_level.upper()
    values = {
        "phrase": view.assessed_phrase,
        "reported": reported,
        "ids": ids,
        "count": str(len(view.contributor_ids)),
        "previous": previous,
        "text": "",
    }
    if view.disagreement:
        text = fill_template(template.disagreement, values)
    elif view.basis == "correlated":
        text = fill_template(template.correlated, values)
    else:
        text = fill_template(template.single, values)
    if previous and phrase_key(previous) != phrase_key(view.assessed_phrase):
        values["text"] = text
        text = fill_template(template.republish, values)
    return " ".join(text.replace("//", " ").split())


def annotate_report(raw: str, remark: str) -> str:
    """Append a remark without adding a ``//`` field."""

    note = " ".join(remark.replace("//", " ").split())
    if not note:
        return raw
    if "//" in raw:
        parts = raw.split("//")
        tail = parts[-1].rstrip()
        parts[-1] = f"{tail} {note}".strip() if tail else note
        return "//".join(parts)
    body = raw.rstrip()
    if not body:
        return note
    if body.endswith(","):
        return f"{body} {note}"
    return f"{body}, {note}"


def assess_message(
    focus: TacrepMessage,
    group: Sequence[TacrepMessage],
    combined_level: str,
    terms: Sequence[ConfidenceTerm],
) -> LevelView:
    """Assess one report against the messages correlated with it."""

    reported_level = normalize_level(focus.confidence_level)
    reported_phrase = focus.confidence_phrase
    members = list(group) if group else [focus]
    ids = tuple(dict.fromkeys(item for item in (_display_id(item) for item in members) if item))
    combined = normalize_level(combined_level) or reported_level or "possible"
    multi = len(ids) > 1
    raised = multi and _rank(combined) > _rank(reported_level)
    conflict = multi and reported_level == "confirmed" and combined != "confirmed"
    if conflict or raised:
        phrase = phrase_for_level(combined, terms)
        return LevelView(
            reported_phrase=reported_phrase,
            reported_level=reported_level,
            assessed_phrase=phrase,
            assessed_level=combined,
            confidence=_weight_for(phrase, terms),
            basis="correlated",
            contributor_ids=ids,
            disagreement=conflict,
        )
    phrase = reported_phrase or phrase_for_level(
        reported_level or combined,
        terms,
        band=focus.confidence_band,
    )
    level = reported_level or combined
    weight = focus.reported_confidence
    if weight is None:
        weight = _weight_for(phrase, terms)
    own = _display_id(focus)
    return LevelView(
        reported_phrase=reported_phrase,
        reported_level=reported_level,
        assessed_phrase=phrase,
        assessed_level=level,
        confidence=weight,
        basis="single_source",
        contributor_ids=(own,) if own else (),
        disagreement=False,
    )


def assess_site(
    site: Site,
    messages: Sequence[TacrepMessage],
    terms: Sequence[ConfidenceTerm],
) -> LevelView:
    """Site assessment is the stored status. Reports supply the stated level."""

    related = _related_messages(site, messages)
    assessed_level = normalize_level(site.status) or "possible"
    assessed_phrase = phrase_for_level(assessed_level, terms)
    stated = [item for item in related if normalize_level(item.confidence_level)]
    reported_level = ""
    reported_phrase = ""
    if len({normalize_level(item.confidence_level) for item in stated}) == 1:
        reported_level = normalize_level(stated[0].confidence_level)
        reported_phrase = stated[0].confidence_phrase
    elif stated:
        strongest = max(stated, key=lambda item: _rank(normalize_level(item.confidence_level)))
        reported_level = normalize_level(strongest.confidence_level)
        reported_phrase = strongest.confidence_phrase
    ids = tuple(dict.fromkeys(item for item in (_display_id(item) for item in related) if item))
    raised = len(ids) > 1 and _rank(assessed_level) > _rank(reported_level)
    conflict = reported_level == "confirmed" and assessed_level != "confirmed" and len(ids) > 1
    return LevelView(
        reported_phrase=reported_phrase,
        reported_level=reported_level,
        assessed_phrase=assessed_phrase,
        assessed_level=assessed_level,
        confidence=site.confidence,
        basis="correlated" if raised or conflict else "single_source",
        contributor_ids=ids,
        disagreement=conflict,
    )


def assess_correlation(
    correlation: Correlation,
    terms: Sequence[ConfidenceTerm],
) -> LevelView:
    """The group's label is the assessed level. The focus is the first message."""

    if not correlation.messages:
        phrase = phrase_for_level(correlation.label, terms)
        level = normalize_level(correlation.label) or "possible"
        return LevelView("", "", phrase, level, 0.0, "single_source", (), False)
    return assess_message(
        correlation.messages[0],
        correlation.messages,
        correlation.label,
        terms,
    )


class LevelCatalog:
    """Views for the messages, sites, and correlations currently loaded."""

    def __init__(
        self,
        messages: Sequence[TacrepMessage],
        correlations: Sequence[Correlation],
        terms: Sequence[ConfidenceTerm],
        wording: AssessmentWording | None = None,
    ) -> None:
        self.wording = wording or AssessmentWording()
        self._messages = tuple(messages)
        self._terms = terms
        self._by_id: dict[str, LevelView] = {}
        grouped: dict[str, tuple[TacrepMessage, ...]] = {}
        labels: dict[str, str] = {}
        for correlation in correlations:
            for message in correlation.messages:
                for key in _keys(message):
                    grouped[key] = correlation.messages
                    labels[key] = correlation.label
        for message in messages:
            keys = _keys(message)
            group = next((grouped[key] for key in keys if key in grouped), (message,))
            label = next((labels[key] for key in keys if key in labels), "")
            view = assess_message(message, group, label, terms)
            for key in keys:
                self._by_id[key] = view

    def for_message(self, message: TacrepMessage) -> LevelView:
        for key in _keys(message):
            found = self._by_id.get(key)
            if found is not None:
                return found
        return assess_message(message, (message,), "", self._terms)

    def for_site(self, site: Site) -> LevelView:
        return assess_site(site, self._messages, self._terms)

    def for_evidence(self, item: Evidence) -> LevelView | None:
        for key in (item.message_id, *item.linked_ids):
            if key and key in self._by_id:
                return self._by_id[key]
        return None


def _related_messages(
    site: Site,
    messages: Sequence[TacrepMessage],
) -> tuple[TacrepMessage, ...]:
    wanted: set[str] = set()
    for item in site.evidence:
        if item.message_id:
            wanted.add(item.message_id)
        wanted.update(item.linked_ids)
    if not wanted:
        return ()
    found: list[TacrepMessage] = []
    seen: set[str] = set()
    for message in messages:
        keys = _keys(message)
        if wanted.isdisjoint(keys):
            continue
        identity = _display_id(message)
        if identity in seen:
            continue
        seen.add(identity)
        found.append(message)
    return tuple(found)


def _display_id(message: TacrepMessage) -> str:
    return message.serial_full or message.serial_base or message.source_message_id


def _keys(message: TacrepMessage) -> tuple[str, ...]:
    values = (
        message.serial_full,
        message.serial_base,
        message.source_message_id,
        message.chat_message_id,
    )
    return tuple(dict.fromkeys(item for item in values if item))


def _rank(level: str) -> int:
    return _RANK.get(normalize_level(level), 0)


def _weight_for(phrase: str, terms: Sequence[ConfidenceTerm]) -> float:
    key = phrase_key(phrase)
    for term in terms:
        if phrase_key(term.phrase) == key:
            return term.weight
    return 0.0
