"""Persistent, isolated lifecycle simulator. No vendor or payment writes are performed."""

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from app.stay_schemas import BookingSnapshot, Money, StrictModel

Failure = Literal["none", "before_apply", "after_apply"]
TRANSITIONS = {
    "pending": ["confirmed", "cancelled"],
    "confirmed": ["checked_in", "cancelled"],
    "checked_in": ["checked_out"],
    "checked_out": [],
    "cancelled": [],
}
router = APIRouter(prefix="/api/lifecycle", tags=["Stay lifecycle UI"])


def now():
    return datetime.now(UTC).isoformat()


def cents(amount):
    return int(Decimal(str(amount)) * 100)


def money(amount):
    return f"{Decimal(amount) / 100:.2f}"


class PropertyLink(StrictModel):
    stay_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    stay_name: str = Field(min_length=1, max_length=100)
    pms_property_code: str = Field(min_length=1, max_length=40)
    source_id: str = Field(min_length=1, max_length=40)


class CreateBooking(StrictModel):
    stay_id: str
    booking: BookingSnapshot
    paid_amount: Money = Decimal(0)
    sync_failure: Failure = "none"


class ChangeStatus(StrictModel):
    status: Literal["confirmed", "checked_in", "checked_out", "cancelled"]
    sync_failure: Failure = "none"


class Refund(StrictModel):
    idempotency_key: str = Field(min_length=1, max_length=100)
    amount: Money = Field(gt=0)
    simulate_payment_failure: bool = False
    sync_failure: Failure = "none"


class Lifecycle:
    def __init__(self, config, harness):
        self.config = config
        self.harness = harness
        config.lifecycle_db.parent.mkdir(parents=True, exist_ok=True)
        self.path = config.lifecycle_db
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS lifecycle (id INTEGER PRIMARY KEY, data TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO lifecycle VALUES (1, ?)",
                (
                    json.dumps(
                        {
                            "links": {},
                            "bookings": {},
                            "events": [],
                            "pms": {},
                            "refunds": {},
                        }
                    ),
                ),
            )

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            state = json.loads(db.execute("SELECT data FROM lifecycle WHERE id=1").fetchone()[0])
            yield state
            db.execute("UPDATE lifecycle SET data=? WHERE id=1", (json.dumps(state),))

    def require_mock(self):
        if self.config.mode != "mock":
            raise HTTPException(409, "Lifecycle writes are simulation-only. Switch PMS_MODE=mock.")

    def link(self, payload):
        self.require_mock()
        row = payload.model_dump()
        with self.transaction() as state:
            old = state["links"].get(payload.stay_id)
            used = any(b["stay_id"] == payload.stay_id for b in state["bookings"].values())
            if old and old != row and used:
                raise HTTPException(409, "This property link has bookings and cannot be remapped")
            state["links"][payload.stay_id] = row
        return self.view()

    def enqueue(self, state, booking_id, kind, payload, failure):
        event = {
            "id": str(uuid4()),
            "booking_id": booking_id,
            "kind": kind,
            "payload": payload,
            "status": "pending",
            "attempts": 0,
            "fault": failure,
            "fault_consumed": False,
            "created_at": now(),
            "last_error": None,
            "history": [],
        }
        state["events"].append(event)

    def apply_to_mock_pms(self, state, event):
        booking_id = event["booking_id"]
        local = state["bookings"][booking_id]
        pms = state["pms"].get(booking_id)
        if pms and event["id"] in pms["applied_events"]:
            return  # Reconcile an uncertain outcome; never repeat the operation.
        payload = event["payload"]
        if event["kind"] == "booking.created":
            if pms:
                raise RuntimeError("External reference already exists")
            pms = {
                "id": f"MOCK-RES-{uuid4().hex[:10]}",
                "guest_id": f"MOCK-GUEST-{uuid4().hex[:10]}",
                "property_code": local["property_link"]["pms_property_code"],
                "source_id": local["property_link"]["source_id"],
                "external_reference": booking_id,
                "status": payload["status"],
                "total_amount": local["snapshot"]["total_amount"],
                "refunded_cents": 0,
                "refund_ids": [],
                "applied_events": [],
            }
            pms.update(
                staying_guest_name=local["snapshot"]["primary_guest_name"]
                if local["snapshot"].get("primary_guest_name")
                else local["snapshot"]["guest_name"],
                check_in_date=local["snapshot"]["check_in_date"],
                check_out_date=local["snapshot"]["check_out_date"],
                room_mappings=local["room_mappings"],
                currency=local["snapshot"]["currency"],
            )
            state["pms"][booking_id] = pms
        elif pms is None:
            raise RuntimeError("PMS reservation has not been created")
        elif event["kind"] == "booking.status_changed":
            target = payload["status"]
            if target != pms["status"] and target not in TRANSITIONS[pms["status"]]:
                raise RuntimeError("PMS rejected an out-of-order status transition")
            pms["status"] = target
        elif event["kind"] == "refund.completed":
            if pms["status"] != "cancelled":
                raise RuntimeError("PMS cancellation must be synchronized before its refund")
            if payload["refund_id"] not in pms["refund_ids"]:
                pms["refunded_cents"] += payload["amount_cents"]
                pms["refund_ids"].append(payload["refund_id"])
        pms["applied_events"].append(event["id"])
        pms["updated_at"] = now()

    def drain(self, state, booking_id, retry=False):
        for event in state["events"]:
            if event["booking_id"] != booking_id or event["status"] == "synced":
                continue
            if event["status"] == "failed" and not retry:
                break  # Preserve per-booking order until an explicit retry.
            event["attempts"] += 1
            inject = not event["fault_consumed"] and event["fault"] != "none"
            event["fault_consumed"] = True
            try:
                if inject and event["fault"] == "before_apply":
                    raise RuntimeError("Simulated PMS connection failure before applying event")
                self.apply_to_mock_pms(state, event)
                if inject and event["fault"] == "after_apply":
                    raise RuntimeError(
                        "Simulated timeout after PMS applied event; outcome unconfirmed"
                    )
                event.update(status="synced", last_error=None)
            except RuntimeError as error:
                event.update(status="failed", last_error=str(error))
            event["history"].append(
                {
                    "at": now(),
                    "attempt": event["attempts"],
                    "result": event["status"],
                    "error": event["last_error"],
                }
            )
            if event["status"] == "failed":
                break

    def create(self, payload):
        self.require_mock()
        if payload.booking.status not in {"pending", "confirmed"}:
            raise HTTPException(422, "Create bookings in pending or confirmed status")
        if payload.paid_amount > payload.booking.total_amount:
            raise HTTPException(422, "Paid amount cannot exceed the booking total")
        room_mappings = [
            self.harness.resolve(room.room_type_id, room.package_id, payload.booking.rate_audience)
            for room in payload.booking.booking_rooms
        ]
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "booking": payload.booking.model_dump(mode="json"),
                    "stay_id": payload.stay_id,
                    "paid_amount": money(cents(payload.paid_amount)),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        key = str(payload.booking.id)
        with self.transaction() as state:
            if payload.stay_id not in state["links"]:
                raise HTTPException(422, "Link the stay to a PMS property first")
            old = state["bookings"].get(key)
            if old:
                if old["create_fingerprint"] != fingerprint:
                    raise HTTPException(
                        409, "Booking reference was already used for another request"
                    )
            else:
                state["bookings"][key] = {
                    "id": key,
                    "stay_id": payload.stay_id,
                    "snapshot": payload.booking.model_dump(mode="json"),
                    "property_link": deepcopy(state["links"][payload.stay_id]),
                    "status": payload.booking.status,
                    "paid_cents": cents(payload.paid_amount),
                    "refunded_cents": 0,
                    "created_at": now(),
                    "create_fingerprint": fingerprint,
                    "room_mappings": room_mappings,
                }
                self.enqueue(
                    state,
                    key,
                    "booking.created",
                    {"status": payload.booking.status},
                    payload.sync_failure,
                )
                self.drain(state, key)
        return self.view()

    @staticmethod
    def booking(state, key):
        if key not in state["bookings"]:
            raise HTTPException(404, "Booking not found")
        return state["bookings"][key]

    def change_status(self, key, payload):
        self.require_mock()
        with self.transaction() as state:
            booking = self.booking(state, key)
            if booking["status"] != payload.status:
                if payload.status not in TRANSITIONS[booking["status"]]:
                    raise HTTPException(409, f"Cannot move {booking['status']} to {payload.status}")
                booking["status"] = payload.status
                booking["snapshot"]["status"] = payload.status
                self.enqueue(
                    state,
                    key,
                    "booking.status_changed",
                    {"status": payload.status},
                    payload.sync_failure,
                )
                self.drain(state, key)
        return self.view()

    def refund(self, key, payload):
        self.require_mock()
        amount = cents(payload.amount)
        with self.transaction() as state:
            booking = self.booking(state, key)
            if booking["status"] != "cancelled":
                raise HTTPException(409, "Cancel the booking before sending a refund")
            previous = state["refunds"].get(payload.idempotency_key)
            if previous and (previous["booking_id"] != key or previous["amount_cents"] != amount):
                raise HTTPException(409, "Refund key was already used for another request")
            if not previous or previous["payment_status"] != "completed":
                if amount > booking["paid_cents"] - booking["refunded_cents"]:
                    raise HTTPException(422, "Refund exceeds the remaining paid amount")
                refund = previous or {
                    "id": str(uuid4()),
                    "booking_id": key,
                    "amount_cents": amount,
                    "idempotency_key": payload.idempotency_key,
                    "attempts": 0,
                    "created_at": now(),
                }
                refund["attempts"] += 1
                refund["payment_status"] = (
                    "failed" if payload.simulate_payment_failure else "completed"
                )
                state["refunds"][payload.idempotency_key] = refund
                if refund["payment_status"] == "completed":
                    refund["processor_reference"] = f"MOCK-REFUND-{refund['id'][:8]}"
                    booking["refunded_cents"] += amount
                    self.enqueue(
                        state,
                        key,
                        "refund.completed",
                        {
                            "refund_id": refund["id"],
                            "amount_cents": amount,
                            "processor_reference": refund["processor_reference"],
                        },
                        payload.sync_failure,
                    )
                    self.drain(state, key)
        return self.view()

    def retry(self, key):
        self.require_mock()
        with self.transaction() as state:
            self.booking(state, key)
            self.drain(state, key, retry=True)
        return self.view()

    def view(self):
        with self.transaction() as state:
            result = deepcopy(state)
        bookings = []
        for booking in result["bookings"].values():
            events = [e for e in result["events"] if e["booking_id"] == booking["id"]]
            booking["sync_status"] = (
                "failed"
                if any(e["status"] == "failed" for e in events)
                else ("pending" if any(e["status"] == "pending" for e in events) else "synced")
            )
            booking["paid_amount"] = money(booking.pop("paid_cents"))
            booking["refunded_amount"] = money(booking.pop("refunded_cents"))
            booking["refundable_amount"] = str(
                Decimal(booking["paid_amount"]) - Decimal(booking["refunded_amount"])
            )
            booking.pop("create_fingerprint")
            booking["pms"] = result["pms"].get(booking["id"])
            if booking["pms"]:
                booking["pms"]["refunded_amount"] = money(booking["pms"].pop("refunded_cents"))
            bookings.append(booking)
        for refund in result["refunds"].values():
            refund["amount"] = money(refund.pop("amount_cents"))
        return {
            "mode": self.config.mode,
            "simulation": True,
            "writes_enabled": self.config.mode == "mock",
            "property_links": list(result["links"].values()),
            "bookings": bookings,
            "events": result["events"],
            "refunds": list(result["refunds"].values()),
            "transitions": TRANSITIONS,
        }


@router.get("/state")
def state(request: Request):
    return request.app.state.lifecycle.view()


@router.post("/property-links")
def link_property(payload: PropertyLink, request: Request):
    return request.app.state.lifecycle.link(payload)


@router.post("/bookings")
def create_booking(payload: CreateBooking, request: Request):
    return request.app.state.lifecycle.create(payload)


@router.post("/bookings/{booking_id}/status")
def update_status(booking_id: str, payload: ChangeStatus, request: Request):
    return request.app.state.lifecycle.change_status(booking_id, payload)


@router.post("/bookings/{booking_id}/refunds")
def refund_booking(booking_id: str, payload: Refund, request: Request):
    return request.app.state.lifecycle.refund(booking_id, payload)


@router.post("/bookings/{booking_id}/retry-sync")
def retry_sync(booking_id: str, request: Request):
    return request.app.state.lifecycle.retry(booking_id)
