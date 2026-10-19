"""Coord-card templates for an assessed-level remark.

The remark is appended to a TACREP, SITREP, or FUSED line. A template
must not contain ``//``, so the field count of the line stays the same.
"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class AssessmentWording:
    """Templates. Placeholders are phrase, reported, ids, count, previous, and text."""

    single: str = "SINGLE SOURCE {phrase}"
    correlated: str = "ASSESSED {phrase} (FROM {reported} {ids})"
    disagreement: str = "REPORTED {reported} ASSESSED {phrase} (FROM {ids})"
    republish: str = "WAS {previous} NOW {text}"


def wording_from_mapping(value: object) -> AssessmentWording:
    """Read ``assessment_text`` from a coord card."""

    if not isinstance(value, dict):
        raise ValueError("assessment_text must be a mapping.")
    chosen: dict[str, str] = {}
    defaults = AssessmentWording()
    for key in ("single", "correlated", "disagreement", "republish"):
        raw = value.get(key)
        if raw is None:
            continue
        text = " ".join(str(raw).split())
        if "//" in text:
            raise ValueError("assessment_text cannot contain //.")
        if not text:
            continue
        chosen[key] = text
    return AssessmentWording(
        single=chosen.get("single", defaults.single),
        correlated=chosen.get("correlated", defaults.correlated),
        disagreement=chosen.get("disagreement", defaults.disagreement),
        republish=chosen.get("republish", defaults.republish),
    )


def fill_template(template: str, values: Mapping[str, str]) -> str:
    """Fill one template. An unknown placeholder leaves the template as written."""

    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return template
