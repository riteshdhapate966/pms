from copy import deepcopy
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.htng import OTA, SOAP, xml_response
from app.main import create_app
from app.pms import PmsError


@pytest.fixture
def client():
    with TestClient(create_app(Settings(_env_file=None, mode="mock"))) as client:
        yield client


def scenario(client, name="member_multi_room"):
    return client.get("/api/rufescent/scenarios").json()[name]


def test_mult_room_translation_preserves_totals_and_dubai_dates(client):
    trial = scenario(client)
    response = client.post("/api/rufescent/reservations/preview", json=trial)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["arrival_time_dubai"] == "2026-12-01T14:00:00+04:00"
    assert data["night_count"] == 2
    assert data["preserved_total"] == "2541.00"
    assert data["ready_for_live_write"] is False
    assert data["transmitted"] is False
    xml = ET.fromstring(data["candidate_create_body_xml"])
    rooms = xml.findall(f".//{{{OTA}}}RoomStay")
    assert len(rooms) == 2
    assert rooms[1].find(f".//{{{OTA}}}RoomRate").get("RatePlanCode") == "DEMO_MEMBER_HB"
    assert xml.find(f".//{{{OTA}}}UniqueID").get("ID") == trial["booking"]["id"]


def test_staying_guest_is_not_sponsor(client):
    response = client.post(
        "/api/rufescent/reservations/preview", json=scenario(client, "friends_and_family")
    )
    data = response.json()
    assert data["staying_guest_name"] == "Demo Staying Guest"
    assert data["booking_owner_name"] == "Demo Booking Member"
    assert data["room_mappings"][0]["wish_rate_plan_code"] == "DEMO_FAMILY_RO"


def test_mock_lifecycle_and_replay(client):
    trial = scenario(client)
    url = "/api/rufescent/reservations/simulate"
    created = client.post(url, json=trial).json()
    replay = client.post(url, json=trial).json()
    assert replay["replayed"] is True
    assert replay["pms_reservation_id"] == created["pms_reservation_id"]
    conflict = deepcopy(trial)
    conflict["booking"]["readable_id"] = "different"
    assert client.post(url, json=conflict).status_code == 409
    trial.update(operation="modify", pms_reservation_id=created["pms_reservation_id"])
    trial["booking"]["check_out_date"] = "2026-12-04T08:00:00Z"
    assert client.post(url, json=trial).json()["departure_date"] == "2026-12-04"
    trial.update(operation="cancel")
    trial["booking"]["status"] = "cancelled"
    assert client.post(url, json=trial).json()["status"] == "cancelled"
    assert client.post(url, json=trial).json()["replayed"] is True
    trial.update(operation="modify")
    trial["booking"]["status"] = "confirmed"
    assert client.post(url, json=trial).status_code == 409


@pytest.mark.parametrize(
    "mutation",
    [
        {"status": "pending"},
        {"status": "checked_in"},
        {"check_in_date": "2026-12-01T14:00:00"},
        {"check_out_date": "2026-12-01T10:30:00Z"},
        {"total_amount": "0.00"},
        {"number_of_adults": 99},
        {"primary_guest_is_member": False},
    ],
)
def test_inconsistent_snapshots_rejected(client, mutation):
    trial = scenario(client)
    trial["booking"].update(mutation)
    assert client.post("/api/rufescent/reservations/preview", json=trial).status_code == 422


def test_missing_mappings_and_wrong_identifiers(client):
    trial = scenario(client)
    trial["booking"]["booking_rooms"][0]["package_id"] = "99999999-9999-4999-8999-999999999999"
    assert client.post("/api/rufescent/reservations/preview", json=trial).status_code == 422
    trial = scenario(client)
    trial["operation"] = "modify"
    assert client.post("/api/rufescent/reservations/simulate", json=trial).status_code == 422
    trial["pms_reservation_id"] = "wrong"
    assert client.post("/api/rufescent/reservations/simulate", json=trial).status_code == 409


def test_soap_mock_reads_and_json_lookup(client):
    query = scenario(client, "availability_request")
    for operation in ("availability", "rates"):
        response = client.post(f"/api/rufescent/{operation}", json=query)
        assert response.status_code == 200, response.text
        assert response.json()["mode"] == "mock"
        assert response.json()["response"]["children"][0]["element"] == "Success"
    assert client.get("/api/rufescent/pms-bookings/TEST-PMS-BOOKING-001").status_code == 200
    assert client.get("/api/rufescent/pms-bookings/local-uuid").status_code == 502


def test_dubai_midnight_boundary(client):
    query = scenario(client, "availability_request")
    query.update(check_in_date="2026-12-01T22:00:00Z", check_out_date="2026-12-02T22:00:00Z")
    # Both timestamps cross midnight in Dubai. Request dates must be Dec 2 and Dec 3.
    calls = []

    def handler(request):
        calls.append(ET.fromstring(request.content))
        return httpx.Response(
            200,
            content=f'<s:Envelope xmlns:s="{SOAP}"><s:Body><r:OTA_HotelAvailGetRS xmlns:r="{OTA}"><r:Success/></r:OTA_HotelAvailGetRS></s:Body></s:Envelope>',
        )

    with TestClient(
        create_app(
            Settings(_env_file=None, mode="mock"), soap_transport=httpx.MockTransport(handler)
        )
    ) as app:
        assert app.post("/api/rufescent/availability", json=query).status_code == 200
    dates = calls[0].find(f".//{{{OTA}}}DateRange")
    assert dates.attrib == {"Start": "2026-12-02", "End": "2026-12-03"}
    assert calls[0].find(".//{}UpdatedAvailability").text == "N"


@pytest.mark.parametrize(
    "body",
    [
        b"not xml",
        b'<!DOCTYPE x [<!ENTITY a "expanded">]><x>&a;</x>',
        f'<s:Envelope xmlns:s="{SOAP}"><s:Body><s:Fault><faultstring>private</faultstring></s:Fault></s:Body></s:Envelope>'.encode(),
        f'<s:Envelope xmlns:s="{SOAP}"><s:Body><o:OTA_HotelAvailGetRS xmlns:o="{OTA}"/></s:Body></s:Envelope>'.encode(),
    ],
)
def test_soap_failure_responses(body):
    with pytest.raises(PmsError):
        xml_response(body, "OTA_HotelAvailGetRS")


def test_live_mode_blocks_simulation_and_synthetic_mapping():
    settings = Settings(
        _env_file=None,
        mode="live",
        base_url="https://example.test/WebAPI/api/",
        client_id="client",
        auth_code="secret",
    )
    with TestClient(create_app(settings)) as client:
        trial = scenario(client)
        assert client.post("/api/rufescent/reservations/simulate", json=trial).status_code == 409
        assert (
            client.post(
                "/api/rufescent/availability", json=scenario(client, "availability_request")
            ).status_code
            == 409
        )


def test_simulation_state_is_isolated_per_app(client):
    trial = scenario(client)
    created = client.post("/api/rufescent/reservations/simulate", json=trial).json()
    with TestClient(create_app(Settings(_env_file=None, mode="mock"))) as other:
        trial.update(operation="modify", pms_reservation_id=created["pms_reservation_id"])
        assert other.post("/api/rufescent/reservations/simulate", json=trial).status_code == 409


def test_live_soap_request_contract_and_no_acknowledgement(tmp_path, client):
    mapping = client.get("/api/rufescent/mappings").json()["mappings"]
    mapping["synthetic"] = False
    path = tmp_path / "mappings.json"
    import json

    path.write_text(json.dumps(mapping), encoding="utf-8")
    calls = []

    def handler(request):
        calls.append(request)
        root = ET.fromstring(request.content)
        assert str(request.url) == "https://example.test/htng/pull.asmx"
        assert request.headers["SOAPAction"] == '"urn:vendor:availability"'
        assert request.headers["Content-Type"] == "text/xml; charset=utf-8"
        assert "clientid" not in request.headers
        assert root.find(".//{*}Username").text == "soap-user"
        assert root.find(".//{*}Password").text == "soap&secret"
        assert root.find(f".//{{{OTA}}}HotelRef").get("HotelCode") == "HOTEL1"
        assert root.find(f".//{{{OTA}}}RequestorID").get("ID") == "APP1"
        return httpx.Response(
            200,
            content=f'<s:Envelope xmlns:s="{SOAP}"><s:Body><o:OTA_HotelAvailGetRS xmlns:o="{OTA}"><o:Success/></o:OTA_HotelAvailGetRS></s:Body></s:Envelope>',
        )

    settings = Settings(
        _env_file=None,
        mode="live",
        base_url="https://example.test/WebAPI/api/",
        client_id="json-client",
        auth_code="json-secret",
        property_code="HOTEL1",
        source_id="APP1",
        mappings_file=path,
        soap_pull_url="https://example.test/htng/pull.asmx",
        soap_username="soap-user",
        soap_password="soap&secret",
        soap_actions={"GetHotelAvailability": "urn:vendor:availability"},
    )
    with TestClient(create_app(settings, soap_transport=httpx.MockTransport(handler))) as live:
        result = live.post(
            "/api/rufescent/availability", json=scenario(client, "availability_request")
        )
        assert result.status_code == 200
        assert "soap&secret" not in result.text
        assert len(calls) == 1


def test_openapi_has_executable_stay_examples(client):
    schema = client.get("/openapi.json").json()
    route = schema["paths"]["/api/rufescent/reservations/preview"]["post"]
    example = route["requestBody"]["content"]["application/json"]["examples"]["member"]["value"]
    assert client.post("/api/rufescent/reservations/preview", json=example).status_code == 200
