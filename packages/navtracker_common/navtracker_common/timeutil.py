"""UTC normalization shared by the worker and analytics."""

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_account_zone: tzinfo = UTC


def ensure_utc(moment: datetime) -> datetime:
    """Treat a naive timestamp as UTC. Aware timestamps convert to UTC.

    Navixy strings are not passed through this directly. The Navixy parser
    attaches the account timezone before calling it, so a value with no
    offset is the account clock rather than UTC.
    """

    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def format_zulu(moment: datetime) -> str:
    """Format an instant as ``YYYY-MM-DDTHH:MM:SSZ`` with no fractions."""

    utc = ensure_utc(moment).replace(microsecond=0)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def account_timezone() -> tzinfo:
    """Timezone for Navixy values that still arrive without an offset."""

    return _account_zone


def set_account_timezone(zone: tzinfo) -> None:
    """Remember the account timezone for the rest of the process."""

    global _account_zone
    _account_zone = zone


def zone_from_name(name: str, *, source: str) -> tzinfo:
    """Parse an IANA timezone name. The error names ``source``."""

    text = name.strip()
    if not text:
        raise RuntimeError(
            f"Invalid {source}=. Expected an IANA timezone name."
        )
    try:
        return ZoneInfo(text)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(
            f"Invalid {source}={text!r}. Expected an IANA timezone name."
        ) from exc
