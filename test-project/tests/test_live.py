import json
import sqlite3
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.htng import SOAP
from app.main import create_app
from app.stay import scenarios


@pytest.fixture
def settings(tmp_path):
    mapping = json.loads((Path(__file__).parents[1] / "fixtures/mappings.json").read_text())
    mapping["synthetic"] = False
    mappings_path = tmp_path / "mappings.json"
    mappings_path.write_text(json.dumps(mapping))
    contracts = {}
    for name in ["create", "confirmed", "checked_in", "checked_out", "cancelled", "refund_record"]:
        contracts[name] = {
            "verified": True,
            "protocol": "json",
            "url": f"https://example.test/{name}",
            "request": {
                "external_reference": "{{booking.id}}",
                "reservation_id": "{{pms_reservation_id}}",
                "operation": name,
                "event_id": "{{event.id}}",
                "property": "{{property_code}}",
                "source": "{{source_id}}",
                "payload": "{{event.payload}}",
            },
            "success_path": "status_code",
            "success_value": "200",
        }
        if name == "create":
            contracts[name]["reservation_id_path"] = "booking_id"
    contracts_path = tmp_path / "contracts.json"
    contracts_path.write_text(json.dumps({"version": 1, "operations": contracts}))
    return Settings(
        _env_file=None,
        mode="live",
        base_url="https://example.test/WISHAPI/",
        property_code="RUF01",
        source_id="ASSIGNED_SOURCE",
        client_id="private-client",
        auth_code="private-auth",
        mappings_file=mappings_path,
        live_contracts_file=contracts_path,
        lifecycle_db=tmp_path / "state.sqlite3",
    )


def body():
    return {
        "stay_id": "rufescent",
        "booking": scenarios()["member_multi_room"]["booking"],
        "paid_amount": "2541.00",
    }


def link(client):
    assert (
        client.post(
            "/api/lifecycle/property-links",
            json={
                "stay_id": "rufescent",
                "stay_name": "Rufescent",
                "pms_property_code": "RUF01",
                "source_id": "ASSIGNED_SOURCE",
            },
        ).status_code
        == 200
    )


def success(request):
    return httpx.Response(200, json={"status_code": "200", "booking_id": "REAL-RES-1001"})


def test_live_full_flow_makes_real_transport_calls_and_saves_external_ids(settings):
    calls = []

    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        assert request.headers["clientid"] == "private-client"
        return success(request)

    with TestClient(create_app(settings, live_transport=httpx.MockTransport(handler))) as client:
        link(client)
        payload = body()
        payload["booking"]["status"] = "pending"
        key = payload["booking"]["id"]
        result = client.post("/api/lifecycle/bookings", json=payload)
        assert result.status_code == 200, result.text
        assert result.json()["simulation"] is False
        assert result.json()["bookings"][0]["pms"]["id"] == "REAL-RES-1001"
        # Original create replay does not repeat the request.
        client.post("/api/lifecycle/bookings", json=payload)
        assert len(calls) == 1
        for status in ["confirmed", "checked_in", "checked_out"]:
            result = client.post(f"/api/lifecycle/bookings/{key}/status", json={"status": status})
            assert result.status_code == 200
            assert result.json()["bookings"][0]["pms"]["status"] == status
        assert [call[0] for call in calls] == [
            "/create",
            "/confirmed",
            "/checked_in",
            "/checked_out",
        ]
        assert calls[-1][1]["reservation_id"] == "REAL-RES-1001"
        assert calls[-1][1]["property"] == "RUF01"
        assert client.get("/api/rufescent/scenarios").status_code == 409


def test_live_external_refund_is_recorded_not_issued_and_deduplicated(settings):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return success(request)

    with TestClient(create_app(settings, live_transport=httpx.MockTransport(handler))) as client:
        link(client)
        key = body()["booking"]["id"]
        client.post("/api/lifecycle/bookings", json=body())
        client.post(f"/api/lifecycle/bookings/{key}/status", json={"status": "cancelled"})
        refund = {
            "idempotency_key": "refund-key",
            "amount": "100.00",
            "processor_reference": "external-refund-abc",
        }
        assert (
            client.post(
                f"/api/lifecycle/bookings/{key}/refunds",
                json={"idempotency_key": "missing", "amount": "100.00"},
            ).status_code
            == 422
        )
        result = client.post(f"/api/lifecycle/bookings/{key}/refunds", json=refund).json()
        assert result["bookings"][0]["pms"]["refunded_amount"] == "100.00"
        client.post(f"/api/lifecycle/bookings/{key}/refunds", json=refund)
        assert calls.count("/refund_record") == 1
        refund["idempotency_key"] = "other-key"
        assert client.post(f"/api/lifecycle/bookings/{key}/refunds", json=refund).status_code == 409


def test_unknown_live_write_outcome_is_not_retried_even_after_restart(settings):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("private upstream message", request=request)

    with TestClient(create_app(settings, live_transport=httpx.MockTransport(handler))) as client:
        link(client)
        key = body()["booking"]["id"]
        result = client.post("/api/lifecycle/bookings", json=body()).json()
        assert result["events"][0]["status"] == "reconciliation_required"
        assert result["bookings"][0]["pms"] is None
        assert "private upstream" not in json.dumps(result)
        client.post(f"/api/lifecycle/bookings/{key}/retry-sync", json={})
        assert len(calls) == 1
    with TestClient(create_app(settings, live_transport=httpx.MockTransport(handler))) as client:
        client.post(f"/api/lifecycle/bookings/{key}/retry-sync", json={})
        assert len(calls) == 1


def test_connection_failure_can_be_explicitly_retried(settings):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("private connection details", request=request)
        return success(request)

    with TestClient(create_app(settings, live_transport=httpx.MockTransport(handler))) as client:
        link(client)
        key = body()["booking"]["id"]
        assert (
            client.post("/api/lifecycle/bookings", json=body()).json()["events"][0]["status"]
            == "failed"
        )
        result = client.post(f"/api/lifecycle/bookings/{key}/retry-sync", json={}).json()
        assert result["events"][0]["status"] == "synced"
        assert len(calls) == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"status_code": "500", "secret": "private-response"}),
        httpx.Response(200, json={"status_code": "200"}),
        httpx.Response(200, text="bad JSON"),
        httpx.Response(500),
        httpx.Response(200, json={"status_code": "200", "booking_id": "MOCK-FAKE"}),
    ],
)
def test_http_or_application_failure_never_creates_fake_success(settings, response):
    with TestClient(
        create_app(settings, live_transport=httpx.MockTransport(lambda request: response))
    ) as client:
        link(client)
        result = client.post("/api/lifecycle/bookings", json=body()).json()
        assert result["events"][0]["status"] == "reconciliation_required"
        assert result["bookings"][0]["pms"] is None
        assert "private-response" not in json.dumps(result)


def test_mock_ledger_does_not_appear_in_live_views(settings):
    mock = settings.model_copy(update={"mode": "mock"})
    with TestClient(create_app(mock)) as client:
        link(client)
        assert client.post("/api/lifecycle/bookings", json=body()).status_code == 200
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/lifecycle/state").json()["bookings"] == []
        assert client.get("/api/lifecycle/state").json()["property_links"] == []


def test_soap_contract_escapes_values_authenticates_and_parses_id(settings):
    contract = {
        "verified": True,
        "protocol": "soap",
        "url": "https://example.test/push.asmx",
        "action": "urn:create",
        "request": '<r:Create xmlns:r="urn:vendor"><r:Name>{{vendor_fields.name}}</r:Name></r:Create>',
        "success_path": ".//{urn:vendor}Success",
        "success_value": True,
        "reservation_id_path": ".//{urn:vendor}Reservation/@id",
    }
    settings.live_contracts_file.write_text(
        json.dumps({"version": 1, "operations": {"create": contract}})
    )
    settings = settings.model_copy(
        update={"soap_username": SecretStr("user"), "soap_password": SecretStr("secret")}
    )

    def handler(request):
        root = ET.fromstring(request.content)
        assert root.find(".//{urn:vendor}Name").text == 'A & B "Guest"'
        assert root.find(".//{*}Password").text == "secret"
        assert request.headers["SOAPAction"] == '"urn:create"'
        return httpx.Response(
            200,
            content=f'<s:Envelope xmlns:s="{SOAP}"><s:Body><v:Result xmlns:v="urn:vendor"><v:Success/><v:Reservation id="1001"/></v:Result></s:Body></s:Envelope>',
        )

    with TestClient(create_app(settings, live_transport=httpx.MockTransport(handler))) as client:
        link(client)
        payload = body()
        payload["vendor_fields"] = {"name": 'A & B "Guest"'}
        result = client.post("/api/lifecycle/bookings", json=payload).json()
        assert result["bookings"][0]["pms"]["id"] == "1001"


def test_live_search_does_not_substitute_local_bookings(settings):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        assert request.url.path == "/WISHAPI/cform/reservation/searchbookings"
        return httpx.Response(200, json={"status_code": "200", "bookings": []})

    with TestClient(create_app(settings, transport=httpx.MockTransport(handler))) as client:
        result = client.post(
            "/api/rufescent/pms-bookings/search",
            json={"search_for": "I", "date_from": "2026-12-01", "date_to": "2026-12-03"},
        )
        assert result.status_code == 200
        assert result.json()["bookings"] == []
        assert calls[0]["hotel_code"] == "RUF01"
        assert calls[0]["search_for"] == "I"


def test_live_intent_is_committed_before_http_delivery(settings):
    def handler(request):
        with sqlite3.connect(settings.lifecycle_db) as db:
            state = json.loads(db.execute("SELECT data FROM lifecycle WHERE id=2").fetchone()[0])
            assert state["events"][0]["status"] == "in_flight"
            assert state["events"][0]["attempts"] == 1
        return success(request)

    with TestClient(create_app(settings, live_transport=httpx.MockTransport(handler))) as client:
        link(client)
        assert (
            client.post("/api/lifecycle/bookings", json=body()).json()["events"][0]["status"]
            == "synced"
        )


def test_unconfigured_operation_is_blocked_without_mutating_state_or_sending(settings):
    settings.live_contracts_file.write_text(json.dumps({"version": 1, "operations": {}}))
    calls = []
    with TestClient(
        create_app(
            settings, live_transport=httpx.MockTransport(lambda request: calls.append(request))
        )
    ) as client:
        link(client)
        response = client.post("/api/lifecycle/bookings", json=body())
        assert response.status_code == 503
        assert client.get("/api/lifecycle/state").json()["bookings"] == []
        assert calls == []


def test_in_flight_crash_state_is_preserved_and_not_marked_synced(settings):
    with TestClient(create_app(settings, live_transport=httpx.MockTransport(success))) as client:
        link(client)
        key = body()["booking"]["id"]
        client.post("/api/lifecycle/bookings", json=body())
        with client.app.state.lifecycle.transaction() as data:
            data["events"][0]["status"] = "in_flight"
        assert client.get("/api/lifecycle/state").json()["bookings"][0]["sync_status"] == "pending"
    calls = []
    with TestClient(
        create_app(
            settings, live_transport=httpx.MockTransport(lambda request: calls.append(request))
        )
    ) as client:
        client.post(f"/api/lifecycle/bookings/{key}/retry-sync", json={})
        assert calls == []


def test_live_failure_injection_is_rejected_before_delivery(settings):
    with TestClient(create_app(settings)) as client:
        link(client)
        payload = body()
        payload["sync_failure"] = "before_apply"
        assert client.post("/api/lifecycle/bookings", json=payload).status_code == 422
        assert client.get("/api/lifecycle/state").json()["events"] == []


def test_invalid_contract_url_does_not_crash_readiness(settings):
    data = json.loads(settings.live_contracts_file.read_text())
    data["operations"]["create"]["url"] = "https://[broken"
    settings.live_contracts_file.write_text(json.dumps(data))
    with TestClient(create_app(settings)) as client:
        result = client.get("/api/workspace/config")
        assert result.status_code == 200
        assert result.json()["capabilities"]["create"]["ready"] is False
