"""Reported-confidence words inside TACREP text.

The phrases are case-insensitive. ``POSS HIGH`` is kept distinct from
``POSS LOW``. ``PROBABLE`` is the same phrase as ``PROB``. Defaults:

- POSS LOW → possible / low, weight 0.25
- POSS HIGH → possible / high, weight 0.45
- PROB → probable, weight 0.70
- CONFIRMED → confirmed, weight 1.0

``CERTAIN`` and ``VERIFIED`` are aliases of ``CONFIRMED``. Those weights
sit on the same 0–1 scale as site confidence: POSS HIGH stays below the
default probable threshold (0.55), PROB is probable, and CONFIRMED is
confirmed. A coord card or config table can replace the phrases.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ConfidenceTerm:
    """One reported-confidence phrase and the level it maps to."""

    phrase: str
    level: str
    band: str
    weight: float
    aliases: tuple[str, ...] = ()


DEFAULT_CONFIDENCE_TERMS: tuple[ConfidenceTerm, ...] = (
    ConfidenceTerm("POSS LOW", "possible", "low", 0.25),
    ConfidenceTerm("POSS HIGH", "possible", "high", 0.45),
    ConfidenceTerm("PROB", "probable", "", 0.70, aliases=("PROBABLE",)),
    ConfidenceTerm("CONFIRMED", "confirmed", "", 1.0, aliases=("CERTAIN", "VERIFIED")),
)

_LEVELS = {"possible", "probable", "confirmed"}
_BANDS = {"", "low", "high"}


class ConfidenceTableError(ValueError):
    """The reported-confidence table cannot be read."""


def split_reported_confidence(
    text: str,
    terms: Sequence[ConfidenceTerm] = DEFAULT_CONFIDENCE_TERMS,
) -> tuple[ConfidenceTerm | None, str]:
    """Remove one confidence phrase and return it with the remaining text.

    The longest phrase wins, so ``POSS HIGH`` is not read as a shorter
    token. The match is a whole phrase, not a piece of another word.
    """

    body = " ".join(text.split())
    if not body or body == "-":
        return None, ""
    ranked: list[tuple[int, str, ConfidenceTerm]] = []
    for term in terms:
        for label in (term.phrase, *term.aliases):
            parts = label.split()
            if not parts:
                continue
            pattern = r"\b" + r"\s+".join(re.escape(part) for part in parts) + r"\b"
            ranked.append((len(label), pattern, term))
    ranked.sort(key=lambda item: item[0], reverse=True)
    for _, pattern, term in ranked:
        match = re.search(pattern, body, flags=re.IGNORECASE)
        if match is None:
            continue
        rest = " ".join((body[: match.start()] + " " + body[match.end() :]).split())
        return term, rest
    return None, body


def read_confidence_token(
    value: str,
    terms: Sequence[ConfidenceTerm] = DEFAULT_CONFIDENCE_TERMS,
) -> tuple[float | None, str, str, str]:
    """Read a numeric weight or a confidence phrase.

    Returns weight, canonical phrase, level, and band. A number keeps
    the weight and leaves the phrase empty.
    """

    term, rest = split_reported_confidence(value, terms)
    if term is not None and rest == "":
        return term.weight, term.phrase, term.level, term.band
    token = value.strip()
    if token and token != "-":
        try:
            return float(token), "", "", ""
        except ValueError:
            pass
    if term is not None:
        return term.weight, term.phrase, term.level, term.band
    return None, "", "", ""


def terms_from_mapping(value: object) -> tuple[ConfidenceTerm, ...]:
    """Read a list of ``{phrase, level, band, weight, aliases}``."""

    if not isinstance(value, list) or not value:
        raise ConfidenceTableError("confidence_terms must be a non-empty list.")
    found: list[ConfidenceTerm] = []
    for item in value:
        if not isinstance(item, dict):
            raise ConfidenceTableError("Each confidence term must be a mapping.")
        phrase = _phrase(str(item.get("phrase", "") or ""))
        level = str(item.get("level", "") or "").strip().lower()
        if level == "verified":
            level = "confirmed"
        band = str(item.get("band", "") or "").strip().lower()
        if level not in _LEVELS:
            raise ConfidenceTableError(f"{phrase} level must be possible, probable, or confirmed.")
        if band not in _BANDS:
            raise ConfidenceTableError(f"{phrase} band must be low, high, or empty.")
        weight = item.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ConfidenceTableError(f"{phrase} weight must be a number.")
        aliases = item.get("aliases", ())
        names: tuple[str, ...]
        if aliases is None or aliases == () or aliases == []:
            names = ()
        elif isinstance(aliases, str):
            names = tuple(
                _phrase(part) for part in aliases.replace(",", " ").split() if part.strip()
            )
        elif isinstance(aliases, list):
            names = tuple(_phrase(str(part)) for part in aliases if str(part).strip())
        else:
            raise ConfidenceTableError(f"{phrase} aliases must be a list.")
        found.append(
            ConfidenceTerm(
                phrase=phrase,
                level=level,
                band=band,
                weight=float(weight),
                aliases=names,
            )
        )
    return tuple(found)


def terms_from_text(text: str) -> tuple[ConfidenceTerm, ...]:
    """Read ``PHRASE:level:band:weight`` items separated by semicolons."""

    items: list[dict[str, object]] = []
    for part in text.split(";"):
        piece = part.strip()
        if not piece:
            continue
        fields = [item.strip() for item in piece.split(":")]
        if len(fields) != 4:
            raise ConfidenceTableError(
                "A confidence term is PHRASE:level:band:weight."
            )
        items.append(
            {
                "phrase": fields[0],
                "level": fields[1],
                "band": fields[2],
                "weight": float(fields[3]),
            }
        )
    return terms_from_mapping(items)


def _phrase(text: str) -> str:
    phrase = " ".join(text.strip().upper().split())
    if phrase == "PROBABLE":
        return "PROB"
    if not phrase:
        raise ConfidenceTableError("A confidence term needs a phrase.")
    return phrase
