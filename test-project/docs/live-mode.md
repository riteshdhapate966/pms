# Real PMS mode

## Current readiness

Live JSON reads and SOAP reads use real HTTP transports. Booking creation, status changes and external-refund recording now also have real JSON/SOAP write transports. They require **explicit vendor-verified operation contracts**, not guessed paths or status fields. No actual WISH write or payment refund was performed while implementing this change.

The local environment was missing credentials when checked. GET requests to the supplied WISH root, `/Help` and `/swagger/docs/v1` failed with connection errors from this execution environment. This does not establish that the hotel server is down. Live connectivity and compatibility remain unverified.

The UI starts even with missing credentials and shows what is blocked. Mock records never appear in the live ledger. Real PMS booking search is separate from bookings tracked by this test project's integration ledger. No server-wide all-properties endpoint is established by the supplied PDFs.

## Configure

1. Keep `PMS_MODE=live`. Set the real `PMS_BASE_URL`, property code `RUF01`, assigned source ID and JSON credentials. The supplied `/WISHAPI/` root is recorded, but its operation paths have not been verified.
2. If deployed JSON paths differ from the documented defaults, set `PMS_JSON_PATHS` to a JSON object. Keys are `masters`, `booking`, `search_bookings`, or the guest operation name, such as `getfolio`. Values are relative paths under the base URL. Do not use arbitrary absolute URLs or guess an extra `/api/` prefix.
3. Configure `PMS_SOAP_PULL_URL`, SOAP credentials, and action overrides for availability/rates. These remain read-only.
4. Create a real mapping file based on `fixtures/mappings.json`. Replace synthetic UUIDs and room/rate/package codes with real mapping data and set `synthetic=false`. Configure `PMS_MAPPINGS_FILE`.
5. Copy `fixtures/live-contracts.example.json` to `fixtures/live-contracts.json`. The sample is **deliberately disabled**, not an implementation of unknown WISH endpoints. Obtain complete accepted request examples and exact response selectors from Prologic First. Configure each operation and set its `verified` flag to true only after its semantics are confirmed. Operations may use JSON or SOAP independently.
6. Set `PMS_LIVE_CONTRACTS_FILE=fixtures/live-contracts.json` and restart the server. Inspect **Connection & mappings** for per-operation readiness. Readiness means required configuration is present, not that a real call passed.

No payment-provider secret or new payment-refund endpoint is configured in this project. Never reuse Touché credentials or an ordinary dashboard login without vendor confirmation.

## Operation contract format

The file has `version: 1` and an `operations` object with keys `create`, `confirmed`, `checked_in`, `checked_out`, `cancelled`, and `refund_record`.

Each enabled contract has:

- `verified: true` and `protocol: "json"` or `"soap"`.
- `url`: exact HTTPS write URL, without credentials/query/fragment. There are no default write URLs.
- `request`: a JSON object template, or an XML **body element** template for SOAP. SOAP envelopes and WS-Security headers are added by the transport. SOAP also requires `action`.
- `success_path`, `success_value`: an application-level response check. JSON uses dotted paths and optional numeric list indices. SOAP uses ElementTree selectors with expanded `{namespace}Element` names relative to the SOAP Body; `/@attribute` selects an attribute. An empty success element yields boolean `true`.
- `reservation_id_path`: required for create. It must select an actual PMS reservation identifier; missing/blank/`MOCK-` IDs are rejected. `guest_id_path` is optional.

Templates use `{{dotted.path}}`. In a JSON value, a whole placeholder preserves its type; a list/object can be supplied through a whole placeholder. XML substitutions are escaped as text or attribute content; structural room/night XML must be supplied as part of the verified template, not spliced in as unescaped text.

Available context:

```text
property_code
source_id
pms_reservation_id
booking.id
booking.snapshot.<persisted snapshot fields>
booking.room_mappings
event.id
event.kind
event.payload.status
event.payload.processor_reference
event.payload.amount
vendor_fields.<operation-specific non-secret data>
```

`vendor_fields` comes from the submitted operation. A snapshot must come from the trusted Stay backend in the eventual integration, not arbitrary untrusted clients. This local loopback UI is an operator test harness, not a production authorization layer. Do not put credentials into vendor fields.

The documented `update_for_checkout` field in `cform/reservation/updatebooking` concerns checkout registration details; it is not proof of an operational checkout API. `getinitiatecheckin` describes pre-check-in initiation. Neither is substituted for an unverified operational write. Refund recording must use the vendor's actual financial posting contract, not an invented `refund` route.

## Live UI behavior

- Properties: local stay/property mapping plus an explicit real configured-property read. Live links must match the configured property/source IDs.
- Bookings: real reserved/in-house/checked-out search and ID lookup; a separate form accepts a real persisted Stay snapshot plus vendor fields for create. No synthetic fixture is auto-filled or available from the live scenarios route.
- Status actions: enabled per verified contract. The display compares local intent with the **last accepted PMS acknowledgement**, not a continuously refreshed PMS state.
- Refunds: record a refund **already completed externally**, with its payment-provider refund reference. The app neither issues money refunds nor verifies provider settlement. Reusing the same provider reference under another idempotency key is refused. Only its PMS recording is sent.
- Sync activity: real delivery attempts from this workspace; no mock event/state is substituted.

## Delivery and recovery

Local intents are committed before sending the real HTTP write, without holding the SQLite transaction open over the network. Events are delivered in order per booking. A returned application success and, for create, a real reservation ID are required to mark the event synced.

An inability to establish a connection is retryable explicitly. Read/write timeouts, non-success HTTP responses, malformed/unproven success responses and process interruption while a request is in flight require reconciliation. They are **not automatically replayed**. Restart preserves those states. The UI disables the ordinary retry button for unknown outcomes. A vendor-backed reconciliation workflow is still required to resolve them; do not manually clear events and replay a potentially completed financial or booking write.

Missing template fields fail before network delivery. Correct contract configuration before retrying. No background worker or production deployment/availability guarantees are claimed. Actual per-room/night schema, taxes, identity handling, cancellation rules and vendor response behavior must be accepted against a sandbox before using a real property.

## Verification

Tests inject HTTPX transports for live JSON and SOAP paths, full lifecycle dispatch, external-refund replay/deduplication, durable pending intents, restart behavior, connection failure retries, uncertain outcomes and isolation from mock data. Passing these tests proves adapter behavior, not compatibility with the installed WISH server.
