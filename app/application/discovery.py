"""Startup and periodic discovery of Navixy tracker ids.

Backoff after failure n (1-based) is min(cap, initial * 2^(n-1)) seconds.
An interval of 0 discovers once and does not refresh.
"""

DEFAULT_DISCOVERY_INTERVAL_SECONDS = 300.0
DEFAULT_DISCOVERY_BACKOFF_INITIAL_SECONDS = 1.0
DEFAULT_DISCOVERY_BACKOFF_CAP_SECONDS = 30.0

_INTERVAL_NAME = "NAVIXY_TRACKER_DISCOVERY_INTERVAL_SECONDS"


def parse_discovery_interval(value: object) -> float:
    """Return seconds between tracker-list refreshes. 0 disables refresh."""

    if isinstance(value, bool):
        raise RuntimeError(
            f"Invalid {_INTERVAL_NAME}={value!r}. "
            "Expected a number greater than or equal to 0."
        )
    number: float
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            raise RuntimeError(
                f"Invalid {_INTERVAL_NAME}={value!r}. "
                "Expected a number greater than or equal to 0."
            ) from None
    else:
        raise RuntimeError(
            f"Invalid {_INTERVAL_NAME}={value!r}. "
            "Expected a number greater than or equal to 0."
        )
    if number < 0:
        raise RuntimeError(
            f"Invalid {_INTERVAL_NAME}={value!r}. "
            "Expected a number greater than or equal to 0."
        )
    return number


def discovery_backoff_seconds(
    attempt: int,
    *,
    initial: float = DEFAULT_DISCOVERY_BACKOFF_INITIAL_SECONDS,
    cap: float = DEFAULT_DISCOVERY_BACKOFF_CAP_SECONDS,
) -> float:
    power = max(attempt, 1) - 1
    delay = initial * (1 << power)
    if delay < cap:
        return delay
    return cap
