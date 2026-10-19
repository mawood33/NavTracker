from dataclasses import dataclass
from enum import Enum


class IngestionMode(Enum):
    WEBSOCKET = "websocket"
    POLL = "poll"


DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_INITIAL_SECONDS = 1.0
DEFAULT_BACKOFF_CAP_SECONDS = 30.0
DEFAULT_PROBE_INTERVAL_SECONDS = 60.0
DEFAULT_HEALTHY_WINDOW_SECONDS = 30.0


@dataclass(frozen=True)
class FailoverPolicy:
    """Websocket retry and poll-to-websocket recovery.

    ``max_attempts`` is the number of consecutive Navixy socket failures
    before switching to API polling. A connection that opens resets the
    streak. Publish failures are not socket failures.

    Backoff after failure n (1-based) is
    min(cap, initial * 2^(n-1)) seconds.
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_initial_seconds: float = DEFAULT_BACKOFF_INITIAL_SECONDS
    backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS
    probe_interval_seconds: float = DEFAULT_PROBE_INTERVAL_SECONDS
    healthy_window_seconds: float = DEFAULT_HEALTHY_WINDOW_SECONDS

    def backoff_seconds(self, consecutive_failures: int) -> float:
        power = max(consecutive_failures, 1) - 1
        delay = self.backoff_initial_seconds * (1 << power)
        if delay < self.backoff_cap_seconds:
            return delay
        return self.backoff_cap_seconds


def parse_navixy_mode(value: str, *, source: str = "NAVIXY_MODE") -> IngestionMode:
    text = value.strip().lower()
    if text == IngestionMode.WEBSOCKET.value:
        return IngestionMode.WEBSOCKET
    if text == IngestionMode.POLL.value:
        return IngestionMode.POLL
    raise RuntimeError(
        f"Invalid {source}={value!r}. Expected one of: websocket, poll."
    )


def resolve_navixy_mode(
    env_value: str | None,
    yaml_value: object | None,
) -> IngestionMode:
    """Env wins. A blank env and a missing yaml value default to websocket."""

    if env_value is not None and env_value.strip() != "":
        return parse_navixy_mode(env_value, source="NAVIXY_MODE")
    if yaml_value is not None and str(yaml_value).strip() != "":
        return parse_navixy_mode(str(yaml_value), source="navixy.mode")
    return IngestionMode.WEBSOCKET


def build_failover_policy(
    *,
    max_attempts: object = DEFAULT_MAX_ATTEMPTS,
    backoff_initial_seconds: object = DEFAULT_BACKOFF_INITIAL_SECONDS,
    backoff_cap_seconds: object = DEFAULT_BACKOFF_CAP_SECONDS,
    probe_interval_seconds: object = DEFAULT_PROBE_INTERVAL_SECONDS,
    healthy_window_seconds: object = DEFAULT_HEALTHY_WINDOW_SECONDS,
) -> FailoverPolicy:
    attempts = _positive_int("NAVIXY_FAILOVER_MAX_ATTEMPTS", max_attempts)
    initial = _positive_float(
        "NAVIXY_FAILOVER_BACKOFF_INITIAL_SECONDS",
        backoff_initial_seconds,
    )
    cap = _positive_float(
        "NAVIXY_FAILOVER_BACKOFF_CAP_SECONDS",
        backoff_cap_seconds,
    )
    probe = _positive_float(
        "NAVIXY_FAILOVER_PROBE_INTERVAL_SECONDS",
        probe_interval_seconds,
    )
    window = _positive_float(
        "NAVIXY_FAILOVER_HEALTHY_WINDOW_SECONDS",
        healthy_window_seconds,
    )
    if cap < initial:
        raise RuntimeError(
            "Invalid NAVIXY_FAILOVER_BACKOFF_CAP_SECONDS="
            f"{cap}. It must be greater than or equal to "
            "NAVIXY_FAILOVER_BACKOFF_INITIAL_SECONDS="
            f"{initial}."
        )
    return FailoverPolicy(
        max_attempts=attempts,
        backoff_initial_seconds=initial,
        backoff_cap_seconds=cap,
        probe_interval_seconds=probe,
        healthy_window_seconds=window,
    )


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool):
        raise RuntimeError(
            f"Invalid {name}={value!r}. Expected an integer >= 1."
        )
    if isinstance(value, float):
        if not value.is_integer():
            raise RuntimeError(
                f"Invalid {name}={value!r}. Expected an integer >= 1."
            )
        value = int(value)
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise RuntimeError(
            f"Invalid {name}={value!r}. Expected an integer >= 1."
        ) from None
    if number < 1:
        raise RuntimeError(
            f"Invalid {name}={number}. Expected an integer >= 1."
        )
    return number


def _positive_float(name: str, value: object) -> float:
    if isinstance(value, bool):
        raise RuntimeError(
            f"Invalid {name}={value!r}. Expected a number greater than 0."
        )
    number: float
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            raise RuntimeError(
                f"Invalid {name}={value!r}. Expected a number greater than 0."
            ) from None
    else:
        raise RuntimeError(
            f"Invalid {name}={value!r}. Expected a number greater than 0."
        )
    if number <= 0:
        raise RuntimeError(
            f"Invalid {name}={value!r}. Expected a number greater than 0."
        )
    return number
