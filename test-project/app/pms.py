import json

import httpx


class PmsError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class PmsClient:
    def __init__(self, http: httpx.AsyncClient, property_code: str, source_id: str):
        self.http = http
        self.property_code = property_code
        self.source_id = source_id

    async def post(self, path: str, payload: dict) -> dict:
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
            "bi/WishBI/getMasters",
            {
                "MastersEnum": kind,
                "hotel_code": self.property_code,
                "source_id": self.source_id,
            },
        )

    async def guest(self, operation: str, guest_id: str):
        return await self.post(
            f"selfservices/{operation}",
            {
                "property_id": self.property_code,
                "guest_id": guest_id,
            },
        )

    async def booking(self, pms_booking_id: str):
        return await self.post(
            "cform/reservation/getbooking",
            {
                "hotel_code": self.property_code,
                "booking_id": pms_booking_id,
            },
        )


def mock_handler(request: httpx.Request) -> httpx.Response:
    """Synthetic fixtures, not authoritative vendor response schemas."""
    body = json.loads(request.content)
    if request.method != "POST":
        return httpx.Response(405)
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
