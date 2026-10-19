"""Persistent lifecycle ledger: mock simulation or vendor-contract-driven live PMS delivery."""

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

from app.live import DeliveryError
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
    vendor_fields: dict = Field(default_factory=dict)


class ChangeStatus(StrictModel):
    status: Literal["confirmed", "checked_in", "checked_out", "cancelled"]
    sync_failure: Failure = "none"
    vendor_fields: dict = Field(default_factory=dict)


class Refund(StrictModel):
    idempotency_key: str = Field(min_length=1, max_length=100)
    amount: Money = Field(gt=0)
    simulate_payment_failure: bool = False
    sync_failure: Failure = "none"
    processor_reference: str | None = Field(default=None, min_length=1, max_length=255)
    vendor_fields: dict = Field(default_factory=dict)


class Lifecycle:
    def __init__(self, config, harness, gateway=None):
        self.config = config
        self.harness = harness
        self.gateway = gateway
        self.state_id = 2 if config.mode == "live" else 1
        config.lifecycle_db.parent.mkdir(parents=True, exist_ok=True)
        self.path = config.lifecycle_db
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS lifecycle (id INTEGER PRIMARY KEY, data TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO lifecycle VALUES (?, ?)",
                (
                    self.state_id,
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
            state = json.loads(
                db.execute("SELECT data FROM lifecycle WHERE id=?", (self.state_id,)).fetchone()[0]
            )
            yield state
            db.execute("UPDATE lifecycle SET data=? WHERE id=?", (json.dumps(state), self.state_id))

    def require_operation(self, operation, failure="none"):
        if self.config.mode == "mock":
            return
        if failure != "none":
            raise HTTPException(422, "Failure injection is unavailable for live operations")
        reasons = self.gateway.reasons(operation)
        if reasons:
            raise HTTPException(503, "; ".join(reasons))

    def link(self, payload):
        if self.config.mode == "live" and (
            payload.pms_property_code != self.config.property_code
            or payload.source_id != self.config.source_id
        ):
            raise HTTPException(
                422, "Live property link must use the configured property and assigned source ID"
            )
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
        if self.config.mode == "live":
            return  # Live requests are delivered only after the local event transaction commits.
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
        self.require_operation("create", payload.sync_failure)
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
                    "vendor_fields": payload.vendor_fields,
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
                    "vendor_fields": payload.vendor_fields,
                }
                self.enqueue(
                    state,
                    key,
                    "booking.created",
                    {"status": payload.booking.status},
                    payload.sync_failure,
                )
                self.drain(state, key)
        if self.config.mode == "live":
            self.drain_live(key)
        return self.view()

    @staticmethod
    def booking(state, key):
        if key not in state["bookings"]:
            raise HTTPException(404, "Booking not found")
        return state["bookings"][key]

    def change_status(self, key, payload):
        self.require_operation(payload.status, payload.sync_failure)
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
                    {"status": payload.status, "vendor_fields": payload.vendor_fields},
                    payload.sync_failure,
                )
                self.drain(state, key)
        if self.config.mode == "live":
            self.drain_live(key)
        return self.view()

    def refund(self, key, payload):
        self.require_operation("refund_record", payload.sync_failure)
        if self.config.mode == "live" and (
            payload.simulate_payment_failure
            or not payload.processor_reference
            or payload.processor_reference.startswith("MOCK-")
        ):
            raise HTTPException(
                422,
                "Supply the reference of an externally completed refund; this app does not issue live payments",
            )
        amount = cents(payload.amount)
        with self.transaction() as state:
            booking = self.booking(state, key)
            if booking["status"] != "cancelled":
                raise HTTPException(409, "Cancel the booking before sending a refund")
            previous = state["refunds"].get(payload.idempotency_key)
            if self.config.mode == "live":
                for existing in state["refunds"].values():
                    if (
                        existing.get("processor_reference") == payload.processor_reference
                        and existing["idempotency_key"] != payload.idempotency_key
                    ):
                        raise HTTPException(
                            409, "This external refund reference was already recorded"
                        )
                if previous and previous.get("processor_reference") != payload.processor_reference:
                    raise HTTPException(
                        409, "Refund key was used with a different processor reference"
                    )
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
                    refund["processor_reference"] = (
                        payload.processor_reference
                        if self.config.mode == "live"
                        else f"MOCK-REFUND-{refund['id'][:8]}"
                    )
                    refund["payment_source"] = (
                        "external_reference_reported_by_operator"
                        if self.config.mode == "live"
                        else "simulated_processor"
                    )
                    booking["refunded_cents"] += amount
                    self.enqueue(
                        state,
                        key,
                        "refund.completed",
                        {
                            "refund_id": refund["id"],
                            "amount_cents": amount,
                            "processor_reference": refund["processor_reference"],
                            "amount": money(amount),
                            "vendor_fields": payload.vendor_fields,
                        },
                        payload.sync_failure,
                    )
                    self.drain(state, key)
        if self.config.mode == "live":
            self.drain_live(key)
        return self.view()

    def retry(self, key):
        if self.config.mode == "live":
            self.drain_live(key, retry=True)
            return self.view()
        with self.transaction() as state:
            self.booking(state, key)
            self.drain(state, key, retry=True)
        return self.view()

    def drain_live(self, key, retry=False):
        while True:
            with self.transaction() as state:
                local = self.booking(state, key)
                event = next(
                    (
                        e
                        for e in state["events"]
                        if e["booking_id"] == key and e["status"] != "synced"
                    ),
                    None,
                )
                if event is None:
                    return
                if event["status"] in {"in_flight", "reconciliation_required"}:
                    return  # Do not replay writes with unknown outcomes, including after a crash.
                if event["status"] == "failed" and not retry:
                    return
                operation = (
                    "create"
                    if event["kind"] == "booking.created"
                    else (
                        "refund_record"
                        if event["kind"] == "refund.completed"
                        else event["payload"]["status"]
                    )
                )
                context = {
                    "booking": deepcopy(local),
                    "event": deepcopy(event),
                    "property_code": local["property_link"]["pms_property_code"],
                    "source_id": local["property_link"]["source_id"],
                    "pms_reservation_id": state["pms"].get(key, {}).get("id"),
                    "vendor_fields": event["payload"].get(
                        "vendor_fields", local.get("vendor_fields", {})
                    ),
                }
                if "status" in event["payload"]:
                    context["booking"]["snapshot"]["status"] = event["payload"]["status"]
                try:
                    prepared = self.gateway.prepare(operation, context)
                except DeliveryError as error:
                    event.update(status="failed", last_error=str(error))
                    return
                event.update(status="in_flight", last_error=None)
                event["attempts"] += 1
                event_id = event["id"]
            # The intent is durable before any HTTP write. No database lock spans the network call.
            try:
                result = self.gateway.send(prepared)
                error = None
            except DeliveryError as delivery_error:
                result = None
                error = delivery_error
            with self.transaction() as state:
                event = next(e for e in state["events"] if e["id"] == event_id)
                if error:
                    event.update(
                        status="reconciliation_required" if error.uncertain else "failed",
                        last_error=str(error),
                    )
                else:
                    local = state["bookings"][key]
                    if operation == "create":
                        state["pms"][key] = {
                            "id": result["id"],
                            "guest_id": result.get("guest_id"),
                            "property_code": local["property_link"]["pms_property_code"],
                            "status": event["payload"]["status"],
                            "refunded_cents": 0,
                            "total_amount": local["snapshot"]["total_amount"],
                            "record_source": "live_vendor_acknowledgement",
                        }
                    elif operation == "refund_record":
                        state["pms"][key]["refunded_cents"] += event["payload"]["amount_cents"]
                    else:
                        state["pms"][key]["status"] = event["payload"]["status"]
                    event.update(status="synced", last_error=None)
                event["history"].append(
                    {
                        "at": now(),
                        "attempt": event["attempts"],
                        "result": event["status"],
                        "error": event["last_error"],
                    }
                )
            if error:
                return

    def view(self):
        with self.transaction() as state:
            result = deepcopy(state)
        bookings = []
        for booking in result["bookings"].values():
            events = [e for e in result["events"] if e["booking_id"] == booking["id"]]
            booking["sync_status"] = (
                "failed"
                if any(e["status"] in {"failed", "reconciliation_required"} for e in events)
                else (
                    "pending"
                    if any(e["status"] in {"pending", "in_flight"} for e in events)
                    else "synced"
                )
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
            "simulation": self.config.mode == "mock",
            "writes_enabled": self.config.mode == "mock"
            or any(cap["ready"] for cap in self.gateway.capabilities().values()),
            "property_links_enabled": True,
            "capabilities": self.gateway.capabilities() if self.config.mode == "live" else {},
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
