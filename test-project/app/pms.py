import json

import httpx


class PmsError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class PmsClient:
    def __init__(self, http: httpx.AsyncClient, property_code: str, source_id: str, config=None):
        self.http = http
        self.property_code = property_code
        self.source_id = source_id
        self.config = config

    def path(self, name, default):
        value = self.config.json_paths.get(name, default) if self.config else default
        if (
            not value
            or value.startswith("/")
            or any(token in value for token in (":", "..", "?", "#"))
        ):
            raise PmsError("Configured JSON operation path must be relative to PMS_BASE_URL", 503)
        return value

    async def post(self, path: str, payload: dict) -> dict:
        if (
            self.config
            and self.config.mode == "live"
            and (
                not self.config.client_id.get_secret_value()
                or not self.config.auth_code.get_secret_value()
                or not self.property_code.strip()
                or not self.source_id.strip()
                or "mock-pms" in self.config.base_url
            )
        ):
            raise PmsError(
                "Configure real JSON URL, property/source IDs and clientID/authCODE before live reads",
                503,
            )
        try:
            response = await self.http.post(path, json=payload)
        except httpx.TimeoutException:
            raise PmsError("PMS request timed out", 504) from None
        except httpx.RequestError:
            raise PmsError("Unable to connect to PMS") from None
        if not response.is_success:
            raise PmsError(f"PMS returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise PmsError("PMS returned invalid JSON") from None
        if not isinstance(data, dict) or str(data.get("status_code")) != "200":
            raise PmsError("PMS returned an unsuccessful or missing application status")
        return data

    async def masters(self, kind: str):
        return await self.post(
            self.path("masters", "bi/WishBI/getMasters"),
            {
                "MastersEnum": kind,
                "hotel_code": self.property_code,
                "source_id": self.source_id,
            },
        )

    async def guest(self, operation: str, guest_id: str):
        return await self.post(
            self.path(operation, f"selfservices/{operation}"),
            {
                "property_id": self.property_code,
                "guest_id": guest_id,
            },
        )

    async def booking(self, pms_booking_id: str):
        return await self.post(
            self.path("booking", "cform/reservation/getbooking"),
            {
                "hotel_code": self.property_code,
                "booking_id": pms_booking_id,
            },
        )

    async def search_bookings(self, query):
        return await self.post(
            self.path("search_bookings", "cform/reservation/searchbookings"),
            {
                "hotel_code": self.property_code,
                "booking_id": "0",
                "confirmation_number": query.confirmation_number,
                "room_type": "",
                "guest_name": query.guest_name,
                "date_arrive": "",
                "date_depart": "",
                "contact_number": "",
                "email_id": "",
                "date_from": query.date_from.isoformat() if query.date_from else "",
                "date_to": query.date_to.isoformat() if query.date_to else "",
                "search_dates_in": "",
                "search_for": query.search_for,
            },
        )


def mock_handler(request: httpx.Request) -> httpx.Response:
    """Synthetic fixtures, not authoritative vendor response schemas."""
    body = json.loads(request.content)
    if request.method != "POST":
        return httpx.Response(405)
    if request.url.path == "/WebAPI/api/cform/reservation/searchbookings":
        return httpx.Response(
            200,
            json={
                "status_code": "200",
                "mock": True,
                "bookings": [
                    {
                        "hotel_code": body["hotel_code"],
                        "booking_id": "TEST-PMS-BOOKING-001",
                        "guest_name": "Demo Guest",
                        "reserve_status": body["search_for"],
                    }
                ],
            },
        )
    if request.url.path == "/WebAPI/api/cform/reservation/getbooking":
        if body.get("booking_id") != "TEST-PMS-BOOKING-001":
            return httpx.Response(200, json={"status_code": "404"})
        return httpx.Response(
            200,
            json={
                "status_code": "200",
                "mock": True,
                "booking_id": "TEST-PMS-BOOKING-001",
                "hotel_code": body["hotel_code"],
                "notice": "Synthetic read fixture, independent of reservation simulation state",
            },
        )
    if request.url.path == "/WebAPI/api/bi/WishBI/getMasters":
        fixtures = {
            "PROPERTY_INFO": {
                "properties": [{"hotel_code": body["hotel_code"], "name": "Demo Hotel"}]
            },
            "ROOM_TYPE": {"room_types": [{"code": "DLX", "name": "Deluxe"}]},
        }
        data = fixtures.get(body.get("MastersEnum"))
        if data is not None:
            return httpx.Response(200, json={"status_code": "200", "mock": True, **data})
    if request.url.path.startswith("/WebAPI/api/selfservices/"):
        if body.get("guest_id") != "TEST-GUEST-001":
            return httpx.Response(200, json={"status_code": "404"})
        fixtures = {
            "getbookingsummary": {
                "confirmation_no": "TEST-1001",
                "guest_name": "Demo Guest",
                "reserve_status": "IH",
            },
            "getpersonaldetails": {"guest_name": "Demo Guest"},
            "getfolio": {
                "charge_line_items": [{"description": "Demo room charge", "amount": "100.00"}]
            },
        }
        data = fixtures.get(request.url.path.rsplit("/", 1)[-1])
        if data is not None:
            return httpx.Response(200, json={"status_code": "200", "mock": True, **data})
    return httpx.Response(404)
