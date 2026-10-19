# WISH PMS API Test Project — FastAPI

Python 3.12+ test harness adapted to Rufescent's **Book a Stay** workflow: multi-room bookings, packages, member/friends-and-family guests, Dubai dates, frozen totals and reservation IDs. Rufescent is read-only reference material; this project never imports or calls it.

## Setup and run (PowerShell)

```powershell
cd C:\Users\RiteshDhapate\dynasas\pms\test-project
uv sync
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

Open **http://127.0.0.1:8010/** for the test UI, or **http://127.0.0.1:8010/docs** for Swagger. Port 8010 avoids Rufescent's usual port 8000. Defaults work without `.env` or vendor credentials.

## Test the booking lifecycle in the UI

1. **Link your property:** give the local stay an identifier and name, then enter the simulated PMS property code/source ID. Click **Link property**. Links become immutable once used by a booking.
2. **Create a test stay:** select the property, member/friends-and-family scenario, dates, guest and initial status. The demo uses a frozen two-room snapshot; its total does not reprice when dates change. Enter the amount already paid in the simulation and click **Create booking & sync**. Both local and simulated PMS records appear, with separate IDs.
3. Select a booking and use **Confirm booking → Check in → Check out**, or **Cancel booking** before check-in. Local and PMS statuses and the event history are displayed separately. Repeating the same status is a no-op.
4. After cancellation, enter a partial/full refund and click **Send test refund**. Cancellation alone does not refund money. Only a successful simulated processor result creates a PMS refund-sync event. Refunds cannot exceed the unrefunded amount paid.
5. Test **Fail before PMS receives it** or **Timeout after PMS applies it**. Local changes remain saved; **Retry PMS sync** reconciles applied events and processes the per-booking queue in order. It never reissues a payment refund. You can also fail and retry the simulated refund processor independently.

The UI's lifecycle writes are **local simulations**, including confirmation, check-in/out, cancellation and refunds. They never call WISH or a payment processor, even if live credentials are configured. Lifecycle writes are disabled in `PMS_MODE=live`; existing live JSON/SOAP read routes stay separate. WISH write contracts for the complete lifecycle still need vendor verification.

`data/lifecycle.sqlite3` persists property links, bookings, PMS records, refund references, idempotency keys and event attempt history across restarts. `PMS_LIFECYCLE_DB` can select another local file. This database is independent of Rufescent. The simulator processes events on each action or explicit retry, with no background worker. It does not enforce real room availability, cancellation policy, payment eligibility or live pricing; those remain Rufescent responsibilities.

Repeated booking creation uses the booking UUID and original request fingerprint. Refund retries must reuse the same `idempotency_key`; a key with different booking/amount is rejected. Unknown PMS outcomes reconcile against applied event IDs. These are simulator guarantees and a model for future integration, not verified WISH behavior.

The UI intentionally preserves simulated payment completion if PMS sync fails. It displays both refund amounts so an outstanding PMS update remains visible. No automatic refund is issued after cancellation because the actual refundable policy and payment ownership must be decided first.

## Test the Rufescent flow

1. `GET /api/rufescent/scenarios`: inspect synthetic availability and booking snapshots. All UUIDs, codes, names and prices are test data.
2. `POST /api/rufescent/availability` and `/rates`: execute the provided example. Mock mode returns SOAP fixtures; live mode performs configured SOAP reads. Responses are diagnostic trees, not prices or availability ready for sale.
3. `GET /api/rufescent/mappings`: inspect room type → WISH room code and audience/package → rate plan mappings.
4. `POST /api/rufescent/reservations/preview`: select the member multi-room or friends-and-family example. Inspect Dubai dates, preserved total, owner/staying guest, room mappings and live-write blockers. Candidate create XML is incomplete and never transmitted.
5. `POST /api/rufescent/reservations/simulate`: create a local reservation and save returned `pms_reservation_id`. Re-submit the identical snapshot to test replay handling.
6. For modify, use the same booking UUID and returned PMS reservation ID, set `operation=modify` and edit the snapshot. Monetary room/booking sums must remain consistent. For cancel, use `operation=cancel` and `booking.status=cancelled`. Repeat cancellation to test replay. These operations simulate locally only.

Simulation state is in memory and resets on restart. It does not represent vendor idempotency. Simulated IDs are not real WISH IDs and do not feed the independent JSON read fixtures.

## JSON read endpoints

| Local endpoint | Example value | WISH upstream operation |
| --- | --- | --- |
| `GET /health` | — | Local health/mode only |
| `GET /api/masters/{kind}` | `PROPERTY_INFO` or `ROOM_TYPE` | POST `bi/WishBI/getMasters` |
| `GET /api/guests/{guest_id}/{operation}` | `TEST-GUEST-001`, `booking-summary` | POST `selfservices/getbookingsummary` |
| Same guest route | `personal-details` | POST `selfservices/getpersonaldetails` |
| Same guest route | `folio` | POST `selfservices/getfolio` |
| `GET /api/rufescent/pms-bookings/{pms_booking_id}` | `TEST-PMS-BOOKING-001` | POST `cform/reservation/getbooking` |

Mock responses are synthetic and their collection fields are illustrative, not a replacement for the vendor schema. An unknown mock guest deliberately returns a failed application status to exercise error handling.

## Connect to a real test PMS

Copy `.env.example` to `.env`. Set `PMS_MODE=live`, the actual base URL ending in `/WebAPI/api/`, property/source codes and credentials. Confirm credential placement and header names with Prologic First; this adapter supports configurable header authentication. Mock mode makes no network calls to WISH even when live configuration is present.

Restart the server after changing configuration. Use the same Swagger endpoints with real guest identifiers. Credentials stay in the backend and `.env` is ignored. This local harness has no caller authentication; bind to loopback as shown when using guest data. No database is needed.

For SOAP reads, configure `PMS_SOAP_PULL_URL`, `PMS_SOAP_USERNAME`, `PMS_SOAP_PASSWORD` and, if required by the installed WSDL, `PMS_SOAP_ACTIONS`. Copy `fixtures/mappings.json` to `fixtures/live-mappings.json`, replace every demo mapping with vendor-confirmed codes, set `synthetic=false`, and set `PMS_MAPPINGS_FILE=fixtures/live-mappings.json`. Synthetic mappings are blocked for live SOAP reads. Credential values must be suitable for HTTP/XML encoding.

Full reads request `UpdatedAvailability=N` and `UpdatedRate=N`; no acknowledgements are sent. Confirm SOAP actions, namespaces, date range boundaries, full/delta behavior and response wrapping against your installation before treating responses as verified. There is no live reservation write endpoint, and simulation is blocked in live mode.

## Verify

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Tests cover JSON request translation and credentials, SOAP request structure, mock workflows, multi-room/package mapping, primary guest separation, Dubai midnight boundaries, frozen total consistency, lifecycle/replay, invalid input, HTTP/application failures, unsafe XML, malformed JSON and connection errors/timeouts. Lifecycle tests also cover property links, every status transition, partial/full refunds, concurrent/replayed requests, restart recovery, failure before/after PMS application, refund processor failure and ensuring PMS retries do not repeat refunds. They never contact a real PMS. HTTP failures map to 502 and upstream timeouts to 504; upstream bodies are excluded from error messages. No network requests are retried automatically.

## Structure and scope

`app/main.py` owns client lifecycle and generic routes; `app/pms.py` is the JSON adapter; `app/htng.py` handles SOAP reads; `app/stay.py` defines Rufescent trial routes, mappings, previews and simulation; `app/stay_schemas.py` validates independent booking snapshots. `fixtures/` holds synthetic data and `tests/` contains pytest tests. Use four-space indentation and Ruff formatting. `uv.lock` records resolved dependencies.

`app/lifecycle.py` implements the SQLite-backed lifecycle simulator and `/api/lifecycle` endpoints; `app/static/` contains the local UI with no build step or CDN dependencies.

See [Rufescent findings and integration decisions](docs/rufescent-use-cases.md) for code evidence, coverage boundaries and unresolved ownership questions. This is a reduced persisted-snapshot contract, not a replacement for Rufescent's booking-create schema or pricing engine. Real check-in/out, room blocking, payments, registration cards and live SOAP writes remain separate work. The local queue demonstrates recovery; production outbox/worker integration remains future work. Vendor prerequisites are in `../pms-integration-notes.md`.

Implementation references: [FastAPI testing](https://fastapi.tiangolo.com/tutorial/testing/), [lifespan testing](https://fastapi.tiangolo.com/advanced/testing-events/), and [HTTPX mock transports](https://www.python-httpx.org/advanced/transports/).
