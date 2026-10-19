import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client():
    with TestClient(create_app(Settings(_env_file=None))) as test_client:
        yield test_client


def test_mock_workflows(client):
    assert client.get("/health").json() == {"status": "ok", "mode": "mock"}
    assert client.get("/api/masters/PROPERTY_INFO").json()["properties"][0]["name"] == "Demo Hotel"
    assert client.get("/api/masters/ROOM_TYPE").json()["room_types"][0]["code"] == "DLX"
    for operation in ["booking-summary", "personal-details", "folio"]:
        response = client.get(f"/api/guests/TEST-GUEST-001/{operation}")
        assert response.status_code == 200
        assert response.json()["mock"] is True
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_invalid_input_and_unknown_guest(client):
    assert client.get("/api/masters/INVALID").status_code == 422
    assert client.get("/api/guests/TEST-GUEST-001/invalid").status_code == 422
    assert client.get("/api/guests/unknown/booking-summary").status_code == 502


def test_live_request_contract():
    def handler(request):
        assert str(request.url) == "https://example.test/WebAPI/api/bi/WishBI/getMasters"
        assert request.method == "POST"
        assert request.headers["X-Client"] == "client"
        assert request.headers["X-Auth"] == "secret"
        assert json.loads(request.content) == {
            "MastersEnum": "ROOM_TYPE",
            "hotel_code": "HOTEL1",
            "source_id": "APP1",
        }
        return httpx.Response(200, json={"status_code": "200"})

    settings = Settings(
        _env_file=None,
        mode="live",
        base_url="https://example.test/WebAPI/api/",
        property_code="HOTEL1",
        source_id="APP1",
        client_id="client",
        auth_code="secret",
        client_id_header="X-Client",
        auth_code_header="X-Auth",
    )
    with TestClient(create_app(settings, httpx.MockTransport(handler))) as client:
        assert client.get("/api/masters/ROOM_TYPE").status_code == 200


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, text="sensitive upstream message"),
        httpx.Response(200, text="invalid JSON"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"status_code": "500", "secret": "do-not-expose"}),
        httpx.Response(302, headers={"location": "https://other.test"}),
    ],
)
def test_upstream_failures_are_sanitized(response):
    with TestClient(
        create_app(
            Settings(_env_file=None),
            httpx.MockTransport(lambda request: response),
        )
    ) as client:
        result = client.get("/api/masters/PROPERTY_INFO")
        assert result.status_code == 502
        assert "sensitive" not in result.text
        assert "do-not-expose" not in result.text


@pytest.mark.parametrize("error, status", [(httpx.ReadTimeout, 504), (httpx.ConnectError, 502)])
def test_network_failures(error, status):
    def handler(request):
        raise error("private connection details", request=request)

    with TestClient(create_app(Settings(_env_file=None), httpx.MockTransport(handler))) as client:
        result = client.get("/api/masters/PROPERTY_INFO")
        assert result.status_code == status
        assert "private" not in result.text


def test_live_missing_credentials_allow_ui_but_block_requests(tmp_path):
    settings = Settings(_env_file=None, mode="live", lifecycle_db=tmp_path / "live.sqlite3")
    calls = []
    with TestClient(
        create_app(settings, transport=httpx.MockTransport(lambda request: calls.append(request)))
    ) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/masters/PROPERTY_INFO").status_code == 503
        assert calls == []
