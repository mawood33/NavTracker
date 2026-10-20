"""Choose ChatSurfer rooms for a confidence phrase and a CAT.

Rules are ordered. The first rule whose constraints all match supplies
the rooms. A later rule is not consulted. Several phrases may name the
same room. When no rule matches, the default rooms are used. An empty
table publishes nothing.

CAT constraints use the category number. CAT I is 1. A rule that sets a
CAT range does not match a notice that has no CAT.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from analytics.domain.confidence_text import DEFAULT_CONFIDENCE_TERMS, ConfidenceTerm

PHRASE_RANK: dict[str, int] = {
    "POSS LOW": 1,
    "POSS HIGH": 2,
    "PROB": 3,
    "PROBABLE": 3,
    "CONFIRMED": 4,
    "CERTAIN": 4,
    "VERIFIED": 4,
}


class PublishTableError(ValueError):
    """The room routing table cannot be read."""


@dataclass(frozen=True)
class PublishRule:
    """One ordered route. Empty constraint lists match any value."""

    rooms: tuple[str, ...]
    confidences: tuple[str, ...] = ()
    cat_min: int | None = None
    cat_max: int | None = None
    types: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class PublishTable:
    """Publish routes. ``enabled`` is false when every room list is empty."""

    rules: tuple[PublishRule, ...] = ()
    default_rooms: tuple[str, ...] = ()
    republish_on_higher_level: bool = True

    def enabled(self) -> bool:
        if self.default_rooms:
            return True
        return any(rule.rooms for rule in self.rules)


@dataclass(frozen=True)
class PublishNotice:
    """One site, correlation, or TACREP that may be sent to a room."""

    identity: str
    confidence: str
    cat: int | None
    types: tuple[str, ...]
    sources: tuple[str, ...]
    body: str
    level_rank: int


def select_rooms(notice: PublishNotice, table: PublishTable) -> tuple[str, ...]:
    """Return the rooms for one notice. The first matching rule wins."""

    rule = first_rule(notice, table)
    if rule is not None:
        return rule.rooms
    return table.default_rooms


def first_rule(notice: PublishNotice, table: PublishTable) -> PublishRule | None:
    """Return the first matching rule, or None when every rule misses."""

    for rule in table.rules:
        if _matches(notice, rule):
            return rule
    return None


def should_publish(
    previous: int | None,
    rank: int,
    *,
    republish_on_higher_level: bool,
) -> bool:
    """Publish the first time, and again only when the level rises."""

    if previous is None:
        return True
    if not republish_on_higher_level:
        return False
    return rank > previous


def phrase_for_confidence(
    confidence: float,
    probable_at: float,
    confirmed_at: float,
    high_at: float,
    terms: Sequence[ConfidenceTerm] | None = None,
) -> str:
    """Map a numeric confidence onto the coord-card phrase for that band."""

    table = terms if terms is not None else DEFAULT_CONFIDENCE_TERMS
    if confidence >= confirmed_at:
        return phrase_for_level("confirmed", table)
    if confidence >= probable_at:
        return phrase_for_level("probable", table)
    if confidence >= high_at:
        return phrase_for_level("possible", table, band="high")
    return phrase_for_level("possible", table, band="low")


def phrase_for_level(
    level: str,
    terms: Sequence[ConfidenceTerm] | None = None,
    *,
    band: str = "",
) -> str:
    """Return the operator phrase for an assessed level."""

    table = terms if terms is not None else DEFAULT_CONFIDENCE_TERMS
    normalized = "confirmed" if level == "verified" else level
    want = band.strip().lower()
    if normalized == "possible" and not want:
        want = "low"
    for term in table:
        if term.level != normalized:
            continue
        if want and term.band and term.band != want:
            continue
        return term.phrase
    if normalized == "confirmed":
        return "CONFIRMED"
    if normalized == "probable":
        return "PROB"
    if want == "high":
        return "POSS HIGH"
    return "POSS LOW"


def phrase_key(text: str) -> str:
    """Fold aliases so CERTAIN and VERIFIED route like CONFIRMED."""

    token = " ".join(text.strip().upper().split())
    if token == "PROBABLE":
        return "PROB"
    if token in {"CERTAIN", "VERIFIED", "CONFIRMED"}:
        return "CONFIRMED"
    return token


def canonicalize_phrase(text: str) -> str:
    """Return one of the four routing phrases."""

    token = phrase_key(text)
    if token not in {"POSS LOW", "POSS HIGH", "PROB", "CONFIRMED"}:
        raise PublishTableError(
            "Unknown confidence "
            f"{text!r}. Expected POSS LOW, POSS HIGH, PROB, or CONFIRMED."
        )
    return token


def table_from_mapping(value: object) -> PublishTable:
    """Read ``{default, republish_on_higher_level, rules}``."""

    if value is None:
        return PublishTable()
    if not isinstance(value, dict):
        raise PublishTableError("publish routes must be a mapping.")
    default = _rooms(value.get("default", value.get("default_rooms", ())))
    flag = value.get("republish_on_higher_level", True)
    if not isinstance(flag, bool):
        text = str(flag).strip().lower()
        if text in {"1", "true", "yes"}:
            flag = True
        elif text in {"0", "false", "no", ""}:
            flag = False
        else:
            raise PublishTableError("republish_on_higher_level must be true or false.")
    raw_rules = value.get("rules", ())
    if raw_rules is None:
        raw_rules = ()
    if not isinstance(raw_rules, list):
        raise PublishTableError("publish rules must be a list.")
    rules = tuple(_rule(item) for item in raw_rules)
    return PublishTable(
        rules=rules,
        default_rooms=default,
        republish_on_higher_level=flag,
    )


def table_from_rules_text(
    text: str,
    *,
    default_rooms: tuple[str, ...] = (),
    republish: bool = True,
) -> PublishTable:
    """Read ``confidence:cat:types:sources:rooms`` rules separated by ``;``.

    ``POSS LOW,POSS HIGH:1-2:ELINT:room-a:precise`` is one rule. An empty
    field matches any value. CAT is ``1-2``, ``1``, or empty.
    """

    rules: list[PublishRule] = []
    for part in text.split(";"):
        piece = part.strip()
        if not piece:
            continue
        fields = [item.strip() for item in piece.split(":")]
        if len(fields) != 5:
            raise PublishTableError(
                "A publish rule is confidence:cat:types:sources:rooms."
            )
        cat_min, cat_max = _cat_span(fields[1])
        rules.append(
            PublishRule(
                rooms=_csv_rooms(fields[4]),
                confidences=_phrases(fields[0]),
                cat_min=cat_min,
                cat_max=cat_max,
                types=_names(fields[2]),
                sources=_names(fields[3]),
            )
        )
    return PublishTable(
        rules=tuple(rules),
        default_rooms=default_rooms,
        republish_on_higher_level=republish,
    )


def describe_table(table: PublishTable) -> str:
    """One line for the room-subscription log."""

    rules = ["+".join(rule.rooms) for rule in table.rules]
    return (
        f"default={list(table.default_rooms)} rules={rules} "
        f"republish_on_higher_level={table.republish_on_higher_level}"
    )


def _matches(notice: PublishNotice, rule: PublishRule) -> bool:
    if rule.confidences and phrase_key(notice.confidence) not in {
        phrase_key(item) for item in rule.confidences
    }:
        return False
    if rule.cat_min is not None or rule.cat_max is not None:
        if notice.cat is None:
            return False
        if rule.cat_min is not None and notice.cat < rule.cat_min:
            return False
        if rule.cat_max is not None and notice.cat > rule.cat_max:
            return False
    if rule.types:
        have = {item.upper() for item in notice.types}
        if not have & {item.upper() for item in rule.types}:
            return False
    if rule.sources:
        have = set(notice.sources)
        if not have & set(rule.sources):
            return False
    return bool(rule.rooms)


def _rule(value: object) -> PublishRule:
    if not isinstance(value, dict):
        raise PublishTableError("Each publish rule must be a mapping.")
    rooms = _rooms(value.get("rooms", ()))
    if not rooms:
        raise PublishTableError("A publish rule needs at least one room.")
    cat_min = _optional_cat(value.get("cat_min"))
    cat_max = _optional_cat(value.get("cat_max"))
    if cat_min is not None and cat_max is not None and cat_min > cat_max:
        raise PublishTableError("cat_min is after cat_max.")
    return PublishRule(
        rooms=rooms,
        confidences=_phrases(value.get("confidence", value.get("confidences", ()))),
        cat_min=cat_min,
        cat_max=cat_max,
        types=_names(value.get("types", value.get("type", ()))),
        sources=_names(value.get("sources", value.get("source", ()))),
    )


def _phrases(value: object) -> tuple[str, ...]:
    found: list[str] = []
    for token in _tokens(value):
        phrase = canonicalize_phrase(token)
        if phrase not in found:
            found.append(phrase)
    return tuple(found)


def _names(value: object) -> tuple[str, ...]:
    return tuple(dict.fromkeys(token for token in _tokens(value)))


def _rooms(value: object) -> tuple[str, ...]:
    rooms = tuple(dict.fromkeys(token for token in _tokens(value) if token and token != "-"))
    return rooms


def _csv_rooms(text: str) -> tuple[str, ...]:
    if not text or text == "-":
        raise PublishTableError("A publish rule needs at least one room.")
    return _rooms(text.replace("|", ","))


def _tokens(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        raw = value.replace("|", ",").replace(";", ",")
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise PublishTableError("Room and confidence lists must be strings or lists.")


def _optional_cat(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise PublishTableError(f"CAT bound {value!r} must be an integer.")
    try:
        number = int(value)
    except ValueError:
        raise PublishTableError(f"CAT bound {value!r} must be an integer.") from None
    if number <= 0:
        raise PublishTableError("CAT bounds start at 1.")
    return number


def _cat_span(text: str) -> tuple[int | None, int | None]:
    token = text.strip()
    if not token:
        return None, None
    if "-" in token:
        left, right = token.split("-", 1)
        return _optional_cat(left), _optional_cat(right)
    level = _optional_cat(token)
    return level, level
