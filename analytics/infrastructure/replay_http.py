"""Small HTTP poster for the development replay scripts. Stdlib only."""

import json
from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def post_text(
    url: str,
    body: str,
    *,
    token: str = "",
    source: str = "",
) -> tuple[int, dict[str, object]]:
    """POST UTF-8 text. Returns status and a JSON object, or an error object."""

    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if source:
        headers["X-Source-Id"] = source
    return _post(url, body.encode("utf-8"), headers)


def post_json(
    url: str,
    payload: Mapping[str, object],
    *,
    token: str = "",
) -> tuple[int, dict[str, object]]:
    """POST a JSON object."""

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload).encode("utf-8")
    return _post(url, body, headers)


def _post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, object]]:
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
            status = int(response.status)
    except HTTPError as exc:
        raw = exc.read()
        status = int(exc.code)
    except URLError as exc:
        return 0, {"error": str(exc.reason)}
    if not raw:
        return status, {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        return status, {"body": raw.decode("utf-8", errors="replace")}
    if isinstance(payload, dict):
        return status, payload
    return status, {"body": payload}
