"""Read-only SOAP calls. Vendor WSDL verification is required before real use."""

from datetime import UTC, datetime
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import httpx

from app.pms import PmsError
from app.stay_schemas import StayQuery

SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
OTA = "http://www.opentravel.org/OTA/2003/05"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
for prefix, uri in [("soap", SOAP), ("ota", OTA), ("wsse", WSSE), ("wsa", WSA)]:
    ET.register_namespace(prefix, uri)


def node(parent, name, attrs=None, text=None, ns=OTA):
    child = ET.SubElement(parent, f"{{{ns}}}{name}" if ns else name, attrs or {})
    child.text = text
    return child


def xml_response(content: bytes, expected: str) -> dict:
    # Block DTD/entity expansion and non-UTF8 encodings, including UTF16 DTD bypasses.
    if len(content) > 2_000_000:
        raise PmsError("SOAP response exceeds test harness limit")
    try:
        raw = content.decode("utf-8-sig")
        if "\x00" in raw or "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
            raise ValueError()
        root = ET.fromstring(raw)
    except (ValueError, ET.ParseError):
        raise PmsError("PMS returned invalid or unsafe SOAP XML") from None
    body = root.find(f"{{{SOAP}}}Body")
    if root.tag != f"{{{SOAP}}}Envelope" or body is None:
        raise PmsError("PMS returned an unexpected SOAP envelope")
    if body.find(f"{{{SOAP}}}Fault") is not None:
        raise PmsError("PMS returned a SOAP fault")
    result = body.find(f"{{{OTA}}}{expected}")
    if result is None or result.find(f"{{{OTA}}}Errors") is not None:
        raise PmsError("PMS returned an unexpected or unsuccessful SOAP response")
    if result.find(f"{{{OTA}}}Success") is None:
        raise PmsError("PMS SOAP response is missing Success")

    # Preserve restrictions/occupancy/tax context; never turn fragments into a sellable price.
    def structured(element):
        return {
            "element": element.tag.rsplit("}", 1)[-1],
            "attributes": dict(element.attrib),
            "text": (element.text or "").strip(),
            "children": [structured(child) for child in element],
        }

    return structured(result)


class HtngClient:
    def __init__(self, config, transport=None):
        self.config = config
        self.transport = transport

    async def read(self, operation: str, query: StayQuery, mapping: dict):
        config = self.config
        mock = config.mode == "mock"
        endpoint = (
            "http://mock-pms/htng/ARIAndReservationPullService.asmx"
            if mock
            else config.soap_pull_url
        )
        if not endpoint or (
            not mock
            and (
                not config.soap_username.get_secret_value()
                or not config.soap_password.get_secret_value()
            )
        ):
            raise PmsError("Configure PMS_SOAP_PULL_URL and SOAP credentials first", 503)
        parts = urlsplit(endpoint)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise PmsError("Invalid configured SOAP endpoint", 503)
        method = "GetHotelAvailability" if operation == "availability" else "GetHotelRatePlan"
        action = config.soap_actions.get(method, f"{endpoint}/{method}")
        root = ET.Element(f"{{{SOAP}}}Envelope")
        header = node(root, "Header", ns=SOAP)
        security = node(header, "Security", {f"{{{SOAP}}}mustUnderstand": "1"}, ns=WSSE)
        token = node(security, "UsernameToken", ns=WSSE)
        node(
            token,
            "Username",
            text="mock" if mock else config.soap_username.get_secret_value(),
            ns=WSSE,
        )
        node(
            token,
            "Password",
            {
                "Type": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText"
            },
            text="mock" if mock else config.soap_password.get_secret_value(),
            ns=WSSE,
        )
        node(header, "To", text=endpoint, ns=WSA)
        node(header, "Action", text=action, ns=WSA)
        body = node(root, "Body", ns=SOAP)
        request_name = (
            "OTA_HotelAvailGetRQ" if operation == "availability" else "OTA_HotelRatePlanRQ"
        )
        request = node(
            body,
            request_name,
            {
                "Version": "1.000",
                "Target": "Test",
                "EchoToken": "rufescent-test",
                "TimeStamp": datetime.now(UTC).isoformat(),
            },
        )
        node(node(node(request, "POS"), "Source"), "RequestorID", {"ID": config.source_id})
        dates = {"Start": query.arrival.isoformat(), "End": query.departure.isoformat()}
        if operation == "availability":
            item = node(node(request, "HotelAvailRequests"), "HotelAvailRequest")
            node(item, "HotelRef", {"HotelCode": config.property_code})
            node(item, "DateRange", dates)
            node(
                node(item, "RoomTypeCandidates"),
                "RoomTypeCandidate",
                {"RoomTypeCode": mapping["wish_room_type_code"]},
            )
            extensions = node(item, "TPA_Extensions")
            node(extensions, "UpdatedAvailability", text="N", ns="")
            node(extensions, "GetTimeSlotAvailability", text="N", ns="")
        else:
            item = node(node(request, "RatePlans"), "RatePlan")
            node(item, "DateRange", dates)
            node(
                node(item, "RatePlanCandidates"),
                "RatePlanCandidate",
                {"RatePlanCode": mapping["wish_rate_plan_code"]},
            )
            node(item, "HotelRef", {"HotelCode": config.property_code})
            node(node(item, "TPA_Extensions"), "UpdatedRate", text="N", ns="")
        selected = self.transport
        if selected is None and mock:
            selected = httpx.MockTransport(
                lambda req: mock_soap(req, query, mapping, config.property_code)
            )
        try:
            async with httpx.AsyncClient(
                transport=selected,
                timeout=config.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as http:
                response = await http.post(
                    endpoint,
                    content=ET.tostring(root, encoding="utf-8"),
                    headers={
                        "Content-Type": "text/xml; charset=utf-8",
                        "SOAPAction": f'"{action}"',
                    },
                )
        except httpx.TimeoutException:
            raise PmsError("PMS SOAP request timed out", 504) from None
        except httpx.RequestError:
            raise PmsError("Unable to connect to PMS SOAP service") from None
        if not response.is_success:
            raise PmsError(f"PMS SOAP returned HTTP {response.status_code}")
        expected = "OTA_HotelAvailGetRS" if operation == "availability" else "OTA_HotelRatePlanRS"
        return {
            "mode": config.mode,
            "mapping": mapping,
            "response": xml_response(response.content, expected),
            "notice": "Diagnostic data only; restrictions, taxes and date boundaries need vendor verification. No acknowledgement sent.",
        }


def mock_soap(request, query, mapping, property_code):
    root = ET.Element(f"{{{SOAP}}}Envelope")
    body = node(root, "Body", ns=SOAP)
    availability = "GetHotelAvailability" in request.headers.get("SOAPAction", "")
    result = node(body, "OTA_HotelAvailGetRS" if availability else "OTA_HotelRatePlanRS")
    node(result, "Success")
    if availability:
        entry = node(
            node(result, "AvailStatusMessages", {"HotelCode": property_code}),
            "AvailStatusMessage",
            {"BookingLimit": "3", "BookingLimitMessageType": "SetLimit"},
        )
        node(
            entry,
            "StatusApplicationControl",
            {
                "Start": query.arrival.isoformat(),
                "End": query.departure.isoformat(),
                "InvTypeCode": mapping["wish_room_type_code"],
            },
        )
        node(entry, "RestrictionStatus", {"Restriction": "Master", "Status": "Open"})
    else:
        plan = node(
            node(result, "RatePlans", {"HotelCode": property_code}),
            "RatePlan",
            {"RatePlanCode": mapping["wish_rate_plan_code"]},
        )
        rate = node(
            node(plan, "Rates"),
            "Rate",
            {
                "Start": query.arrival.isoformat(),
                "End": query.departure.isoformat(),
                "CurrencyCode": "AED",
                "InvTypeCode": mapping["wish_room_type_code"],
            },
        )
        node(
            node(rate, "BaseByGuestAmts"),
            "BaseByGuestAmt",
            {"NumberOfGuests": "2", "AmountBeforeTax": "500.00"},
        )
    return httpx.Response(200, content=ET.tostring(root, encoding="utf-8"))
