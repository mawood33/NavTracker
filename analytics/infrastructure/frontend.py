"""Push adapter for an optional frontend.

Posts the same ``navtracker.analytics`` schema 2 documents the WebSocket
sends: snapshots, diffs, and retracts. A site has operational ``status``
(``unknown``, ``active``, ``inactive``), ``assessment``
(``possible``, ``probable``, ``confirmed``), numeric ``confidence``, and
``types``. ``verified`` is accepted on input and written as ``confirmed``.
Sites, correlations, and evidence also carry the reported phrase and the
assessed phrase, basis, and contributor ids. GPS fixes are ``tracks``.
A diff ``op`` is ``upsert`` or ``remove``. It does not open a connection
until ``publish`` is called.
"""

import json
from collections.abc import Awaitable, Callable, Mapping

import httpx

Post = Callable[[str, bytes], Awaitable[None]]


class FrontendPublisher:
    def __init__(self, url: str, *, post: Post | None = None):
        self.url = url
        self._post = post
        self._client: httpx.AsyncClient | None = None

    async def publish(self, document: Mapping[str, object]) -> None:
        body = json.dumps(document, separators=(",", ":")).encode("utf-8")
        if self._post is not None:
            await self._post(self.url, body)
            return
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15)
        response = await self._client.post(
            self.url,
            content=body,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
