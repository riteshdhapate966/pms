from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.lifecycle import CreateBooking
from app.main import create_app

BASE = "/api/lifecycle"


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, mode="mock", lifecycle_db=tmp_path / "lifecycle.sqlite3")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def link(client, code="TEST-HOTEL"):
    result = client.post(
        f"{BASE}/property-links",
        json={
            "stay_id": "stay-1",
            "stay_name": "Demo Stay",
            "pms_property_code": code,
            "source_id": "STAY",
        },
    )
    assert result.status_code == 200, result.text


def payload(client, status="pending", failure="none"):
    booking = client.get("/api/rufescent/scenarios").json()["member_multi_room"]["booking"]
    booking["status"] = status
    return {
        "stay_id": "stay-1",
        "booking": booking,
        "paid_amount": "2541.00",
        "sync_failure": failure,
    }


def create(client, status="pending", failure="none"):
    link(client)
    data = payload(client, status, failure)
    result = client.post(f"{BASE}/bookings", json=data)
    assert result.status_code == 200, result.text
    return data["booking"]["id"], result.json()


def status(client, key, target, failure="none"):
    return client.post(
        f"{BASE}/bookings/{key}/status", json={"status": target, "sync_failure": failure}
    )


def refund(client, key, amount="100.00", token="refund-1", **options):
    return client.post(
        f"{BASE}/bookings/{key}/refunds",
        json={
            "idempotency_key": token,
            "amount": amount,
            **options,
        },
    )


def test_property_booking_and_stay_status_journey(client):
    key, result = create(client)
    booking = result["bookings"][0]
    assert booking["pms"]["property_code"] == "TEST-HOTEL"
    assert booking["pms"]["external_reference"] == key
    assert booking["pms"]["status"] == "pending"
    assert len(booking["pms"]["room_mappings"]) == 2
    for target in ["confirmed", "checked_in", "checked_out"]:
        result = status(client, key, target)
        assert result.status_code == 200
        booking = result.json()["bookings"][0]
        assert booking["status"] == booking["pms"]["status"] == target
        assert booking["sync_status"] == "synced"
    assert status(client, key, "cancelled").status_code == 409
    assert status(client, key, "confirmed").status_code == 409


def test_cancel_and_full_refund_are_separate_synced_operations(client):
    key, _ = create(client, "confirmed")
    cancelled = status(client, key, "cancelled").json()["bookings"][0]
    assert cancelled["refunded_amount"] == "0.00"
    result = refund(client, key, "2541.00").json()
    booking = result["bookings"][0]
    assert booking["status"] == booking["pms"]["status"] == "cancelled"
    assert booking["refunded_amount"] == booking["pms"]["refunded_amount"] == "2541.00"
    assert result["refunds"][0]["payment_status"] == "completed"
    assert [event["kind"] for event in result["events"]] == [
        "booking.created",
        "booking.status_changed",
        "refund.completed",
    ]


@pytest.mark.parametrize("failure", ["before_apply", "after_apply"])
def test_failed_creation_retries_without_duplicate_pms_booking(client, failure):
    key, result = create(client, failure=failure)
    booking = result["bookings"][0]
    assert booking["sync_status"] == "failed"
    original_pms = booking["pms"]
    assert status(client, key, "confirmed").json()["events"][-1]["status"] == "pending"
    result = client.post(f"{BASE}/bookings/{key}/retry-sync", json={}).json()
    assert result["bookings"][0]["sync_status"] == "synced"
    assert result["bookings"][0]["pms"]["status"] == "confirmed"
    assert result["events"][0]["attempts"] == 2
    if original_pms:
        assert result["bookings"][0]["pms"]["id"] == original_pms["id"]


@pytest.mark.parametrize("failure", ["before_apply", "after_apply"])
def test_refund_payment_never_repeated_on_pms_retry(client, failure):
    key, _ = create(client, "confirmed")
    status(client, key, "cancelled")
    result = refund(client, key, "100.00", sync_failure=failure).json()
    assert result["bookings"][0]["refunded_amount"] == "100.00"
    assert result["bookings"][0]["sync_status"] == "failed"
    reference = result["refunds"][0]["processor_reference"]
    result = client.post(f"{BASE}/bookings/{key}/retry-sync", json={}).json()
    assert result["bookings"][0]["pms"]["refunded_amount"] == "100.00"
    assert result["refunds"][0]["attempts"] == 1
    assert result["refunds"][0]["processor_reference"] == reference
    replay = refund(client, key, "100.00").json()
    assert replay["bookings"][0]["refunded_amount"] == "100.00"
    assert len(replay["refunds"]) == 1
    assert len(replay["events"]) == 3


def test_refund_processor_failure_has_no_pms_event_until_success(client):
    key, _ = create(client)
    status(client, key, "cancelled")
    failed = refund(client, key, simulate_payment_failure=True).json()
    assert failed["refunds"][0]["payment_status"] == "failed"
    assert failed["bookings"][0]["refunded_amount"] == "0.00"
    assert len(failed["events"]) == 2
    result = refund(client, key).json()
    assert result["refunds"][0]["attempts"] == 2
    assert result["bookings"][0]["pms"]["refunded_amount"] == "100.00"


def test_cancel_failure_preserves_order_before_refund(client):
    key, _ = create(client, "confirmed")
    status(client, key, "cancelled", "before_apply")
    result = refund(client, key).json()
    assert result["bookings"][0]["pms"]["status"] == "confirmed"
    assert result["events"][-1]["status"] == "pending"
    result = client.post(f"{BASE}/bookings/{key}/retry-sync", json={}).json()
    assert result["bookings"][0]["pms"]["status"] == "cancelled"
    assert result["bookings"][0]["pms"]["refunded_amount"] == "100.00"


def test_rejects_invalid_and_excess_refunds(client):
    key, _ = create(client)
    assert refund(client, key).status_code == 409
    assert status(client, key, "checked_in").status_code == 409
    status(client, key, "cancelled")
    assert refund(client, key, "0.00").status_code == 422
    assert refund(client, key, "-1.00").status_code == 422
    assert refund(client, key, "2541.01").status_code == 422
    assert refund(client, key, "100.00").status_code == 200
    assert refund(client, key, "101.00").status_code == 409
    assert refund(client, key, "2441.01", "refund-2").status_code == 422
    result = refund(client, key, "2441.00", "refund-2").json()
    assert result["bookings"][0]["refunded_amount"] == "2541.00"


def test_unlinked_property_create_replay_and_immutable_used_mapping(client):
    data = payload(client)
    assert client.post(f"{BASE}/bookings", json=data).status_code == 422
    link(client)
    first = client.post(f"{BASE}/bookings", json=data).json()
    again = client.post(f"{BASE}/bookings", json=data).json()
    assert len(again["events"]) == 1
    assert first["bookings"][0]["pms"]["id"] == again["bookings"][0]["pms"]["id"]
    conflicting = deepcopy(data)
    conflicting["booking"]["readable_id"] = "different"
    assert client.post(f"{BASE}/bookings", json=conflicting).status_code == 409
    assert (
        client.post(
            f"{BASE}/property-links",
            json={
                "stay_id": "stay-1",
                "stay_name": "Other Stay",
                "pms_property_code": "OTHER",
                "source_id": "STAY",
            },
        ).status_code
        == 409
    )


def test_state_and_retry_survive_restart(settings):
    with TestClient(create_app(settings)) as client:
        key, _ = create(client, failure="after_apply")
    with TestClient(create_app(settings)) as client:
        result = client.get(f"{BASE}/state").json()
        assert result["bookings"][0]["sync_status"] == "failed"
        assert (
            client.post(f"{BASE}/bookings/{key}/retry-sync", json={}).json()["bookings"][0][
                "sync_status"
            ]
            == "synced"
        )


def test_concurrent_refund_replay_is_once(client):
    key, _ = create(client)
    status(client, key, "cancelled")
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: refund(client, key), range(4)))
    assert all(response.status_code == 200 for response in responses)
    state = client.get(f"{BASE}/state").json()
    assert state["bookings"][0]["refunded_amount"] == "100.00"
    assert len(state["refunds"]) == 1


def test_ui_and_static_assets(client):
    assert "Every stay." in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/docs").status_code == 200


def test_live_mode_blocks_lifecycle_writes(settings):
    settings = settings.model_copy(update={"mode": "live"})
    with TestClient(create_app(settings)) as client:
        assert client.get(f"{BASE}/state").json()["writes_enabled"] is False
        assert client.post(f"{BASE}/bookings", json=payload(client)).status_code == 409


def test_creation_concurrency_has_one_external_reference(client):
    link(client)
    data = CreateBooking.model_validate(payload(client))
    # Exercise independent SQLite connections under concurrent create attempts.
    service = client.app.state.lifecycle
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: service.create(data), range(4)))
    assert len(service.view()["events"]) == 1
    assert len(service.view()["bookings"]) == 1


def test_refund_idempotency_key_cannot_cross_bookings(client):
    key, _ = create(client)
    status(client, key, "cancelled")
    refund(client, key)
    other = payload(client)
    other["booking"]["id"] = str(uuid4())
    client.post(f"{BASE}/bookings", json=other)
    other_key = other["booking"]["id"]
    status(client, other_key, "cancelled")
    assert refund(client, other_key).status_code == 409
