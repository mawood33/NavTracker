import asyncio

import httpx
import pytest
from app.application.supervisor import IngestionSupervisor
from app.domain.errors import TrackerDiscoveryError
from app.infrastructure.config import load_settings
from app.infrastructure.navixy_api import NavixyApiClient
from app.infrastructure.navixy_parse import parse_tracker_list

from test_settings import _scrub


def test_parse_tracker_list_returns_ids_in_order():
    assert parse_tracker_list(
        {"success": True, "list": [{"id": 2}, {"id": 2}, {"id": 9}]}
    ) == [2, 9]


def test_parse_tracker_list_allows_an_empty_list():
    assert parse_tracker_list({"success": True, "list": []}) == []


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"success": False, "list": [{"id": 1}], "detail": "secret-body"},
        {"success": True},
        {"success": True, "list": ["nope"]},
        {"success": True, "list": [{"id": True}]},
        {"success": True, "list": [{"id": "4"}]},
        {"success": True, "list": [{"label": "car"}]},
    ],
)
def test_parse_tracker_list_rejects_a_bad_payload_without_the_body(payload):
    with pytest.raises(TrackerDiscoveryError) as caught:
        parse_tracker_list(payload)
    assert "secret-body" not in str(caught.value)


class _StubResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.example/v2/tracker/list")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                "bad",
                request=request,
                response=response,
            )

    def json(self):
        return self._payload


class _StubHttp:
    def __init__(self, payload=None, error=None, status_code=200):
        self.payload = payload
        self.error = error
        self.status_code = status_code
        self.calls = []

    async def post(self, url, json):
        self.calls.append((url, json))
        if self.error is not None:
            raise self.error
        return _StubResponse(self.payload, self.status_code)

    async def aclose(self):
        return None


async def _client(stub):
    client = NavixyApiClient("https://api.example/v2", "secret-hash")
    await client.client.aclose()
    client.client = stub
    return client


def test_list_tracker_ids_posts_the_tracker_list_endpoint():
    async def scenario():
        stub = _StubHttp({"success": True, "list": [{"id": 7}, {"id": 8}]})
        client = await _client(stub)
        assert await client.list_tracker_ids() == [7, 8]
        assert stub.calls == [
            (
                "https://api.example/v2/tracker/list",
                {"hash": "secret-hash"},
            )
        ]
        await client.close()

    asyncio.run(scenario())


def test_list_tracker_ids_hides_transport_errors():
    async def scenario():
        stub = _StubHttp(error=httpx.ConnectError("secret-hash down"))
        client = await _client(stub)
        with pytest.raises(TrackerDiscoveryError) as caught:
            await client.list_tracker_ids()
        assert str(caught.value) == "Navixy tracker list request failed"
        assert "secret-hash" not in str(caught.value)
        await client.close()

    asyncio.run(scenario())


def test_tracker_ids_reject_non_integers(monkeypatch):
    _scrub(monkeypatch)
    monkeypatch.setenv("TRACKER_IDS", "1,abc")
    with pytest.raises(RuntimeError, match="Expected comma-separated integers"):
        load_settings()


def test_tracker_ids_parse_spaced_integers(monkeypatch):
    _scrub(monkeypatch)
    monkeypatch.setenv("TRACKER_IDS", "1, 2")
    assert load_settings().tracker_ids == [1, 2]


def test_empty_tracker_ids_mean_discovery(monkeypatch):
    _scrub(monkeypatch)
    monkeypatch.setenv("TRACKER_IDS", "")
    settings = load_settings()
    assert settings.tracker_ids == []
    supervisor = IngestionSupervisor(
        reader=None,
        stream=None,
        publisher=None,
        tracker_ids=settings.tracker_ids,
        mode=settings.navixy_mode,
        poll_interval_seconds=settings.poll_interval_seconds,
        policy=settings.failover,
        catalog=object(),
    )
    assert supervisor._uses_discovery is True


def test_discovery_interval_defaults_and_rejects_negative(monkeypatch):
    _scrub(monkeypatch)
    assert load_settings().tracker_discovery_interval_seconds == 300
    monkeypatch.setenv("NAVIXY_TRACKER_DISCOVERY_INTERVAL_SECONDS", "0")
    assert load_settings().tracker_discovery_interval_seconds == 0
    monkeypatch.setenv("NAVIXY_TRACKER_DISCOVERY_INTERVAL_SECONDS", "-1")
    with pytest.raises(
        RuntimeError,
        match="Invalid NAVIXY_TRACKER_DISCOVERY_INTERVAL_SECONDS='-1'",
    ):
        load_settings()
