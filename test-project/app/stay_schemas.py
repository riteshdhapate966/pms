"""Independent contracts based on Rufescent snapshots; never import the real application."""

from datetime import date, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]
Status = Literal["pending", "confirmed", "checked_in", "checked_out", "completed", "cancelled"]
# Dubai has no DST; explicit UTC+04 avoids a system tzdata dependency on Windows.
DUBAI = timezone(timedelta(hours=4), name="Asia/Dubai")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StayQuery(StrictModel):
    check_in_date: AwareDatetime
    check_out_date: AwareDatetime
    room_type_id: UUID
    package_id: UUID | None = None
    rate_audience: Literal["member", "friends_and_family"] = "member"

    @model_validator(mode="after")
    def dates(self):
        if self.check_out_date <= self.check_in_date:
            raise ValueError("check_out_date must follow check_in_date")
        if self.departure <= self.arrival:
            raise ValueError("A stay must include at least one Dubai calendar night")
        if (self.departure - self.arrival).days > 366:
            raise ValueError("Test requests are limited to 366 nights")
        return self

    @property
    def arrival(self) -> date:
        return self.check_in_date.astimezone(DUBAI).date()

    @property
    def departure(self) -> date:
        return self.check_out_date.astimezone(DUBAI).date()


class RoomSnapshot(StrictModel):
    id: UUID
    room_id: UUID
    room_type_id: UUID
    package_id: UUID | None = None
    number_of_adults: int = Field(ge=1)
    number_of_children: int = Field(ge=0, default=0)
    subtotal_amount: Money
    discount_amount: Money = Decimal(0)
    charges_amount: Money = Decimal(0)
    tax_amount: Money = Decimal(0)
    total_amount: Money

    @model_validator(mode="after")
    def totals(self):
        if self.subtotal_amount + self.charges_amount + self.tax_amount != self.total_amount:
            raise ValueError("Room total must equal net subtotal + charges + tax")
        return self


class BookingSnapshot(StrictModel):
    id: UUID
    readable_id: str = Field(min_length=1, max_length=100)
    check_in_date: AwareDatetime
    check_out_date: AwareDatetime
    status: Status
    guest_id: UUID  # Rufescent booking owner, NOT the PMS guest ID
    guest_name: str = Field(min_length=1, max_length=200)
    primary_guest_is_member: bool = True
    primary_guest_name: str | None = Field(default=None, min_length=1, max_length=200)
    rate_audience: Literal["member", "friends_and_family"] = "member"
    number_of_adults: int = Field(ge=1)
    number_of_children: int = Field(ge=0, default=0)
    subtotal_amount: Money
    discount_amount: Money = Decimal(0)
    charges_amount: Money = Decimal(0)
    tax_amount: Money = Decimal(0)
    total_amount: Money
    currency: Literal["AED"] = "AED"
    booking_rooms: list[RoomSnapshot] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def consistency(self):
        # Reuse date validation without importing Rufescent or normalizing away approved times.
        StayQuery(
            check_in_date=self.check_in_date,
            check_out_date=self.check_out_date,
            room_type_id=self.booking_rooms[0].room_type_id,
        )
        if not self.primary_guest_is_member and not self.primary_guest_name:
            raise ValueError("Non-member primary guest needs primary_guest_name")
        for field in (
            "number_of_adults",
            "number_of_children",
            "subtotal_amount",
            "discount_amount",
            "charges_amount",
            "tax_amount",
            "total_amount",
        ):
            if sum(getattr(room, field) for room in self.booking_rooms) != getattr(self, field):
                raise ValueError(f"Booking {field} must equal the room snapshot sum")
        for field in ("id", "room_id"):
            values = [getattr(room, field) for room in self.booking_rooms]
            if len(set(values)) != len(values):
                raise ValueError(f"Duplicate booking-room {field}")
        return self


class ReservationTrial(StrictModel):
    operation: Literal["create", "modify", "cancel"] = "create"
    booking: BookingSnapshot
    pms_reservation_id: str | None = Field(default=None, min_length=1, max_length=100)
    pms_guest_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def operation_rules(self):
        if self.operation != "create" and not self.pms_reservation_id:
            raise ValueError("Modify/cancel requires an explicit PMS reservation ID")
        if self.operation in {"create", "modify"} and self.booking.status != "confirmed":
            raise ValueError("Only confirmed pre-arrival snapshots can be created/modified here")
        if self.operation == "cancel" and self.booking.status != "cancelled":
            raise ValueError("Cancel trial requires a cancelled Rufescent snapshot")
        return self
