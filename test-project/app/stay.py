import hashlib
import json
from copy import deepcopy
from pathlib import Path
from threading import Lock
from uuid import uuid4
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi import Path as PathParameter

from app.htng import OTA, node
from app.stay_schemas import DUBAI, ReservationTrial, StayQuery

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
router = APIRouter(prefix="/api/rufescent", tags=["Rufescent stay trials"])


class StayHarness:
    def __init__(self, config):
        self.config = config
        with config.mappings_file.open(encoding="utf-8") as source:
            self.mappings = json.load(source)
        if config.mode == "live" and self.mappings.get("synthetic") is not False:
            self.mapping_notice = "Replace synthetic mappings before SOAP reads against WISH"
        else:
            self.mapping_notice = None
        self.reservations = {}
        self.lock = Lock()

    def resolve(self, room_type_id, package_id, audience):
        candidates = [
            row
            for row in self.mappings["room_types"]
            if row["rufescent_room_type_id"] == str(room_type_id)
        ]
        if len(candidates) != 1:
            raise HTTPException(422, "Room type needs exactly one explicit WISH mapping")
        row = candidates[0]
        key = f"{audience}:{package_id or 'none'}"
        code = row.get("rates", {}).get(key)
        if not row.get("wish_room_type_code") or not code:
            raise HTTPException(422, "No WISH room/rate mapping for this audience and package")
        return {
            "rufescent_room_type_id": str(room_type_id),
            "wish_room_type_code": row["wish_room_type_code"],
            "wish_rate_plan_code": code,
        }

    def preview(self, trial: ReservationTrial):
        booking = trial.booking
        stay = StayQuery(
            check_in_date=booking.check_in_date,
            check_out_date=booking.check_out_date,
            room_type_id=booking.booking_rooms[0].room_type_id,
        )
        mappings = [
            self.resolve(room.room_type_id, room.package_id, booking.rate_audience)
            for room in booking.booking_rooms
        ]
        staying_name = booking.primary_guest_name or booking.guest_name
        root = ET.Element(
            f"{{{OTA}}}OTA_HotelResNotifRQ",
            {"Target": "Test", "Version": "2.001", "ResStatus": "Commit"},
        )
        node(node(node(root, "POS"), "Source"), "RequestorID", {"ID": self.config.source_id})
        reservation = node(node(root, "HotelReservations"), "HotelReservation")
        node(
            reservation,
            "UniqueID",
            {"Type": "14", "ID": trial.pms_reservation_id or str(booking.id)},
        )
        rooms = node(reservation, "RoomStays")
        for room, mapping in zip(booking.booking_rooms, mappings):
            entry = node(rooms, "RoomStay")
            node(
                node(entry, "RoomRates"),
                "RoomRate",
                {
                    "NumberOfUnits": "1",
                    "RoomTypeCode": mapping["wish_room_type_code"],
                    "RatePlanCode": mapping["wish_rate_plan_code"],
                },
            )
            counts = node(entry, "GuestCounts")
            node(
                counts,
                "GuestCount",
                {"AgeQualifyingCode": "10", "Count": str(room.number_of_adults)},
            )
            node(
                counts,
                "GuestCount",
                {"AgeQualifyingCode": "8", "Count": str(room.number_of_children)},
            )
            node(
                entry,
                "TimeSpan",
                {"Start": stay.arrival.isoformat(), "End": stay.departure.isoformat()},
            )
            node(
                entry,
                "Total",
                {
                    "AmountBeforeTax": str(room.subtotal_amount + room.charges_amount),
                    "AmountAfterTax": str(room.total_amount),
                    "CurrencyCode": booking.currency,
                },
            )
            node(entry, "BasicPropertyInfo", {"HotelCode": self.config.property_code})
        customer = node(
            node(
                node(
                    node(
                        node(node(reservation, "ResGuests"), "ResGuest", {"ResGuestRPH": "0"}),
                        "Profiles",
                    ),
                    "ProfileInfo",
                ),
                "Profile",
                {"ProfileType": "1"},
            ),
            "Customer",
        )
        # Never guess a guest's legal given/family name by splitting a full name.
        node(node(customer, "PersonName"), "NamePrefix", text="")
        info = node(reservation, "ResGlobalInfo")
        node(
            node(info, "HotelReservationIDs"),
            "HotelReservationID",
            {
                "ResID_Source": self.config.source_id,
                "ResID_Type": "14",
                "ResID_Value": str(booking.id),
            },
        )
        return {
            "operation": trial.operation,
            "ready_for_live_write": False,
            "transmitted": False,
            "mode": self.config.mode,
            "external_reference": str(booking.id),
            "readable_id": booking.readable_id,
            "pms_reservation_id": trial.pms_reservation_id,
            "pms_guest_id": trial.pms_guest_id,
            "staying_guest_name": staying_name,
            "booking_owner_name": booking.guest_name,
            "arrival_date": stay.arrival,
            "departure_date": stay.departure,
            "arrival_time_dubai": booking.check_in_date.astimezone(DUBAI).isoformat(),
            "departure_time_dubai": booking.check_out_date.astimezone(DUBAI).isoformat(),
            "night_count": (stay.departure - stay.arrival).days,
            "currency": booking.currency,
            "preserved_total": str(booking.total_amount),
            "room_mappings": mappings,
            "candidate_create_body_xml": ET.tostring(root, encoding="unicode")
            if trial.operation == "create"
            else None,
            "blockers": [
                "Candidate XML is incomplete and has not been validated against the installed WSDL/XSD.",
                "Confirm reservation/confirmation identifier types and per-room/multi-stay semantics.",
                "Supply verified legal guest name components; full names are not split or guessed.",
                "Confirm nightly rates, packages, discounts, charge/tax ownership and KeepRate behavior.",
                "Confirm vendor modify/cancel message semantics; no modify/cancel XML is guessed.",
                "Check-in/out, early arrival and room status need separate verified vendor operations.",
            ],
        }

    def simulate(self, trial):
        if self.config.mode != "mock":
            raise HTTPException(
                409, "Simulation is available only in mock mode; no live writes exist"
            )
        preview = self.preview(trial)
        booking_key = str(trial.booking.id)
        fingerprint = hashlib.sha256(trial.booking.model_dump_json().encode()).hexdigest()
        with self.lock:
            existing = self.reservations.get(booking_key)
            if trial.operation == "create":
                if existing:
                    if existing["fingerprint"] != fingerprint or existing["status"] == "cancelled":
                        raise HTTPException(
                            409, "Reference already used; reconcile before resubmitting"
                        )
                    return {**existing, "replayed": True, "mock": True, "transmitted": False}
                existing = {
                    "pms_reservation_id": f"MOCK-RES-{uuid4().hex[:12]}",
                    "pms_guest_id": f"MOCK-GUEST-{uuid4().hex[:12]}",
                    "status": "confirmed",
                }
            else:
                if not existing or existing["pms_reservation_id"] != trial.pms_reservation_id:
                    raise HTTPException(
                        409, "No matching simulated WISH reservation for this reference"
                    )
                if existing["status"] == "cancelled" and trial.operation != "cancel":
                    raise HTTPException(409, "A cancelled reservation cannot be modified")
                if trial.operation == "cancel":
                    if existing["status"] == "cancelled":
                        return {**existing, "replayed": True, "mock": True, "transmitted": False}
                    existing["status"] = "cancelled"
            record = {
                **existing,
                "fingerprint": fingerprint,
                "external_reference": booking_key,
                "arrival_date": str(preview["arrival_date"]),
                "departure_date": str(preview["departure_date"]),
                "preserved_total": preview["preserved_total"],
            }
            self.reservations[booking_key] = record
            return {**record, "mock": True, "transmitted": False, "replayed": False}


@router.get("/scenarios")
def scenarios():
    """Synthetic Rufescent booking snapshots, not exports of real guest records."""
    data = json.loads((FIXTURES / "scenarios.json").read_text(encoding="utf-8"))
    family = deepcopy(data["member_multi_room"])
    family["booking"].update(
        id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        readable_id="DEMO-STAY-002",
        primary_guest_is_member=False,
        primary_guest_name="Demo Staying Guest",
        rate_audience="friends_and_family",
    )
    data["friends_and_family"] = family
    return data


@router.get("/mappings")
def mappings(request: Request):
    harness = request.app.state.stay
    return {"mappings": harness.mappings, "notice": harness.mapping_notice}


@router.get("/pms-bookings/{pms_booking_id}")
async def pms_booking(
    request: Request, pms_booking_id: str = PathParameter(min_length=1, max_length=100)
):
    """Use the PMS booking ID; do not substitute Rufescent's UUID or readable_id."""
    return await request.app.state.pms.booking(pms_booking_id)


QUERY_BODY = Body(
    openapi_examples={
        "demo": {
            "summary": "Demo stay dates and room",
            "value": scenarios()["availability_request"],
        }
    }
)
TRIAL_BODY = Body(
    openapi_examples={
        "member": {"summary": "Member multi-room stay", "value": scenarios()["member_multi_room"]},
        "family": {
            "summary": "Member books for another guest",
            "value": scenarios()["friends_and_family"],
        },
    }
)


@router.post("/availability")
async def availability(request: Request, query: StayQuery = QUERY_BODY):
    return await read_htng("availability", query, request)


@router.post("/rates")
async def rates(request: Request, query: StayQuery = QUERY_BODY):
    return await read_htng("rates", query, request)


async def read_htng(operation, query, request):
    harness = request.app.state.stay
    if harness.mapping_notice:
        raise HTTPException(409, harness.mapping_notice)
    mapping = harness.resolve(query.room_type_id, query.package_id, query.rate_audience)
    return await request.app.state.htng.read(operation, query, mapping)


@router.post("/reservations/preview")
def preview(request: Request, trial: ReservationTrial = TRIAL_BODY):
    """Local translation review only. This endpoint never transmits a reservation."""
    return request.app.state.stay.preview(trial)


@router.post("/reservations/simulate")
def simulate(request: Request, trial: ReservationTrial = TRIAL_BODY):
    """In-memory create/modify/cancel trial, not an implementation of vendor idempotency."""
    return request.app.state.stay.simulate(trial)
