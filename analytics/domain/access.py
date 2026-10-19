"""Request authorization for the analytics HTTP API and websocket."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthPolicy:
    """Empty tokens fail closed. The write token also grants read."""

    read_token: str = ""
    write_token: str = ""
    origins: tuple[str, ...] = ()


_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def authorize(
    policy: AuthPolicy,
    method: str,
    path: str,
    headers: Mapping[str, str],
    query: Mapping[str, str],
) -> int | None:
    """Return 401 when the token is missing or wrong. Health stays open."""

    if method == "GET" and path == "/health":
        return None
    token = bearer_token(headers, query)
    if method in _WRITE_METHODS:
        if policy.write_token and token == policy.write_token:
            return None
        return 401
    if token and (
        (policy.read_token and token == policy.read_token)
        or (policy.write_token and token == policy.write_token)
    ):
        return None
    return 401


def origin_allowed(policy: AuthPolicy, headers: Mapping[str, str]) -> bool:
    """A missing Origin is allowed. A present Origin must be listed."""

    origin = headers.get("origin", "").strip()
    if not origin:
        return True
    return origin in policy.origins


def bearer_token(headers: Mapping[str, str], query: Mapping[str, str]) -> str:
    """Read a bearer token from the header, a websocket protocol, or the query."""

    header = headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    protocol = headers.get("sec-websocket-protocol", "")
    for item in protocol.split(","):
        token = item.strip()
        if token.lower().startswith("bearer."):
            return token.split(".", 1)[1].strip()
    return query.get("token", "").strip()
