"""Configurable real write transport. No guessed endpoints, fake IDs or mock fallback."""

import json
import re
from copy import deepcopy
from typing import Any, Literal
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.htng import SOAP, WSA, WSSE, node

TOKEN = re.compile(r"\{\{([a-zA-Z0-9_.]+)\}\}")
OPERATIONS = ("create", "confirmed", "checked_in", "checked_out", "cancelled", "refund_record")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verified: Literal[True]
    protocol: Literal["json", "soap"]
    url: str
    action: str | None = None
    request: dict | str
    success_path: str = Field(min_length=1)
    success_value: str | int | bool
    reservation_id_path: str | None = None
    guest_id_path: str | None = None

    @field_validator("url")
    @classmethod
    def parse_url(cls, value):
        try:
            if urlsplit(value).hostname is None:
                raise ValueError("Missing operation host")
        except ValueError:
            raise ValueError("Invalid operation URL") from None
        return value


class DeliveryError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def lookup(data, path):
    current = data
    for part in path.split("."):
        try:
            current = current[int(part)] if isinstance(current, list) else current[part]
        except (KeyError, TypeError, ValueError, IndexError):
            raise DeliveryError("Required vendor request/response field is missing") from None
    return current


def render(value, context):
    if isinstance(value, dict):
        return {key: render(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, context) for item in value]
    if not isinstance(value, str):
        return value
    match = TOKEN.fullmatch(value)
    if match:
        return deepcopy(lookup(context, match[1]))
    return TOKEN.sub(lambda match: str(lookup(context, match[1])), value)


def parse_xml(content):
    try:
        if len(content) > 2_000_000:
            raise ValueError()
        raw = content.decode("utf-8-sig")
        if "\x00" in raw or "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
            raise ValueError()
        return ET.fromstring(raw)
    except (ValueError, ET.ParseError):
        raise DeliveryError("Invalid or unsafe vendor XML") from None


def xml_value(root, path):
    selector, separator, attr = path.rpartition("/@")
    element = root.find(selector if separator else path)
    if element is None:
        return None
    return element.get(attr) if separator else ((element.text or "").strip() or True)


class LiveGateway:
    def __init__(self, config, mappings, transport=None):
        self.config = config
        self.transport = transport
        self.contracts = {}
        self.contract_error = None
        self.synthetic = mappings.get("synthetic") is not False
        if config.live_contracts_file:
            try:
                data = json.loads(config.live_contracts_file.read_text(encoding="utf-8"))
                if data.get("version") != 1:
                    raise ValueError()
                for name, value in data["operations"].items():
                    if name not in OPERATIONS:
                        raise ValueError()
                    if value.get("verified") is not True:
                        continue
                    self.contracts[name] = Contract.model_validate(value)
            except (OSError, ValueError, ValidationError, KeyError, TypeError):
                self.contract_error = (
                    "Live contracts file is missing or invalid; inspect its schema"
                )
                self.contracts.clear()

    def reasons(self, operation):
        c = self.config
        reasons = []
        if self.contract_error:
            reasons.append(self.contract_error)
        contract = self.contracts.get(operation)
        if contract is None:
            reasons.append("Vendor-verified operation contract is not configured")
        if self.synthetic:
            reasons.append("Replace synthetic room/rate/package mappings")
        if not c.property_code.strip() or not c.source_id.strip():
            reasons.append("Property code and assigned source ID are required")
        if contract:
            url = urlsplit(contract.url)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                reasons.append(
                    "A verified HTTPS operation URL without embedded credentials is required"
                )
            if contract.protocol == "json":
                if not c.client_id.get_secret_value() or not c.auth_code.get_secret_value():
                    reasons.append("JSON credentials are missing")
                if not isinstance(contract.request, dict):
                    reasons.append("JSON contract requires an object request template")
            else:
                if not c.soap_username.get_secret_value() or not c.soap_password.get_secret_value():
                    reasons.append("SOAP credentials are missing")
                if not contract.action or not isinstance(contract.request, str):
                    reasons.append("SOAP contract requires an action URI and XML body template")
            if operation == "create" and not contract.reservation_id_path:
                reasons.append("Create requires an explicit PMS reservation-ID response selector")
        return reasons

    def capabilities(self):
        return {
            operation: {"ready": not self.reasons(operation), "missing": self.reasons(operation)}
            for operation in OPERATIONS
        }

    def prepare(self, operation, context):
        missing = self.reasons(operation)
        if missing:
            raise DeliveryError("; ".join(missing))
        contract = self.contracts[operation]
        c = self.config
        if contract.protocol == "json":
            return contract, {
                "json": render(contract.request, context),
                "headers": {
                    "Accept": "application/json",
                    c.client_id_header: c.client_id.get_secret_value(),
                    c.auth_code_header: c.auth_code.get_secret_value(),
                },
            }
        # Body placeholders become escaped text/attributes; structural XML must come from the contract.
        body_xml = TOKEN.sub(
            lambda match: escape(str(lookup(context, match[1])), {'"': "&quot;", "'": "&apos;"}),
            contract.request,
        )
        payload = parse_xml(body_xml.encode())
        root = ET.Element(f"{{{SOAP}}}Envelope")
        header = node(root, "Header", ns=SOAP)
        token = node(
            node(header, "Security", {f"{{{SOAP}}}mustUnderstand": "1"}, ns=WSSE),
            "UsernameToken",
            ns=WSSE,
        )
        node(token, "Username", text=c.soap_username.get_secret_value(), ns=WSSE)
        node(
            token,
            "Password",
            {
                "Type": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText"
            },
            text=c.soap_password.get_secret_value(),
            ns=WSSE,
        )
        node(header, "To", text=contract.url, ns=WSA)
        node(header, "Action", text=contract.action, ns=WSA)
        node(root, "Body", ns=SOAP).append(payload)
        return contract, {
            "content": ET.tostring(root, encoding="utf-8"),
            "headers": {
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{contract.action}"',
            },
        }

    def send(self, prepared):
        contract, kwargs = prepared
        try:
            with httpx.Client(
                timeout=self.config.timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
                trust_env=False,
            ) as http:
                response = http.post(contract.url, **kwargs)
        except httpx.ConnectError:
            raise DeliveryError("Unable to establish a connection to the PMS") from None
        except httpx.RequestError:
            raise DeliveryError(
                "PMS write outcome is unknown; reconcile before retrying", True
            ) from None
        if not response.is_success:
            raise DeliveryError(
                f"PMS returned HTTP {response.status_code}; reconcile before retrying", True
            )
        try:
            if len(response.content) > 2_000_000:
                raise ValueError()
            if contract.protocol == "json":
                data = response.json()
                select = lambda path: lookup(data, path)
            else:
                root = parse_xml(response.content)
                body = root.find(f"{{{SOAP}}}Body")
                if (
                    root.tag != f"{{{SOAP}}}Envelope"
                    or body is None
                    or body.find(f"{{{SOAP}}}Fault") is not None
                ):
                    raise ValueError()
                select = lambda path: xml_value(body, path)
            if select(contract.success_path) != contract.success_value:
                raise ValueError()
            result = {}
            for name, path in [
                ("id", contract.reservation_id_path),
                ("guest_id", contract.guest_id_path),
            ]:
                if path:
                    value: Any = select(path)
                    if (
                        not isinstance(value, (str, int))
                        or isinstance(value, bool)
                        or not str(value).strip()
                        or str(value).startswith("MOCK-")
                    ):
                        raise ValueError()
                    result[name] = str(value)
            return result
        except (ValueError, TypeError, DeliveryError):
            raise DeliveryError(
                "PMS response did not prove the write succeeded; reconcile before retrying", True
            ) from None
