# Rufescent PMS test scope

## Repository findings

The sibling `../../rufescent` repository was inspected through file reads only. Its application, tests, database configuration and `.env` were not executed or loaded. No Rufescent files were changed. This harness has no imports, database connections, API calls or runtime dependency on that repository.

Rufescent uses FastAPI, SQLAlchemy/PostgreSQL, guest authentication, Stripe/wallet payments and local stay inventory. Its main WISH use case is **Book a Stay**, rather than the hourly meeting-room booking flow. Touché POS sync is a separate integration; its credentials and endpoints must not be reused for WISH.

| Evidence in Rufescent | Consequence for this harness |
| --- | --- |
| `routes/book_stay_booking.py`: quote, creation, availability, check-in/out, upgrade and cancellation routes | Test the stay lifecycle and availability, rather than just generic hotel masters |
| `models/book_stay_booking.py`: UUID, readable ID, guest owner, staying guest, statuses, payment state, frozen totals | Keep external reference, PMS reservation ID and PMS guest ID separate; preserve snapshot amounts |
| `schemas/book_stay_booking_room.py`: multiple rooms and per-room packages | Include a multi-room scenario with different packages and explicit package/rate mappings |
| `models/book_stay_booking_room.py`: adult/child allocation and per-room monetary snapshots | Validate room sums and occupancy totals; never charge children or reprice packages in the adapter |
| `services/book_stay_primary_guest.py`: member versus `friends_and_family`, sponsor versus staying guest | Translate the stored classification; do not perform identity lookups or infer eligibility here |
| `services/book_stay_pricing.py`: Decimal pricing, offers, package amounts, charges and VAT | Preserve caller-supplied snapshots and flag PMS price/tax reconciliation as unresolved |
| `models/book_stay_booking_room_night.py`: nightly rates and provenance | Night-by-night rate export remains a live-write blocker; an average nightly rate is insufficient |
| `models/book_stay_room.py`: AVAILABLE, RESERVED, OCCUPIED, BLOCKED, INACTIVE | Do not equate PMS room-type booking limits with physical-room operational status |
| `routes/book_stay_booking.py`: Dubai display and UTC storage | Require offset-aware timestamps; derive PMS dates in Dubai UTC+04, preserving approved early-arrival times |
| `docs/stay/2026-10/02_decision_log.md`, D04 | PMS integration is deferred; Rufescent remains authoritative until an explicit ownership decision changes this |
| `services/stay_rates/source.py`, `activation.py`, `service.py` | Reference-hotel rate pricing has its own provider/activation boundary. WISH property rates do not automatically satisfy it |

Older narrative documentation may describe prior behavior. In particular, the current code supports per-adult package snapshots, guest classification and rate activation machinery beyond the original September guest-flow document. This harness uses those current code concepts without copying the business engine.

## What can be exercised now

1. JSON property/room-type masters and guest summary/details/folio reads, plus a PMS booking lookup.
2. SOAP `GetHotelAvailability` and `GetHotelRatePlan`, using explicit room/rate mapping and configurable WS-Security credentials/action URIs.
3. An independent, reduced **persisted booking snapshot** contract. This is not a drop-in replacement for Rufescent's `BookStayBookingCreate`: a future integration must assemble the snapshot server-side after its own validation.
4. Reservation translation previews and in-memory create/modify/cancel simulations, including duplicate references and terminal cancellations.

Previews never transmit. The candidate create XML intentionally remains incomplete: legal name components, nightly rates, multi-room semantics, pricing/taxes and identity conventions need the vendor contract. Modify/cancel XML is not guessed. The simulation implements local test rules, not a claim about WISH's idempotency or reservation behavior.

SOAP responses retain restrictions, occupancy and tax attributes in a structured diagnostic tree. They are not converted into sellable inventory or binding quotes. Full reads request `UpdatedAvailability=N` / `UpdatedRate=N`; no acknowledgement method is called. Confirm the installation's full-versus-delta and inclusive/exclusive end-date semantics before enabling polling.

The snapshot's stored `status=confirmed` is the trial eligibility signal; payment is not re-evaluated. Rufescent permits admin confirmation as well as confirmation after settlement. Do not substitute `is_payment_done=true` for all confirmation cases in a future adapter.

## Decisions required before actual integration

- Assign ownership separately for prices, offers, packages, taxes, availability, physical-room status, reservation status, guest identity and documents.
- Agree the sync trigger: reservation creation, payment settlement, admin confirmation, cancellation, upgrade and checkout each have different consequences.
- Establish how one Rufescent multi-room booking maps to WISH records, and whether each room needs its own guest/reservation link.
- Verify the installed SOAP WSDL/XSD, source ID, hotel code, rate/package/audience mappings, reservation ID types and cancellation rules.
- Verify authorization for JSON reads and whether guest IDs and booking IDs coincide; never assume they do.
- Compare frozen Rufescent nightly amounts, discounts, charges and VAT against one vendor-accepted test booking/folio. Decide who calculates taxes and how existing Stripe/wallet receipts are reconciled without double collection.
- Obtain distinct vendor operations for actual check-in/check-out, early check-in approval, room assignment, room blocking and registration cards. `getinitiatecheckin` alone does not prove that a guest has been checked in.
- Implement a durable outbox, persistent ID links, reconciliation of unknown write outcomes and audit redaction in the eventual integration. This local simulator loses state at restart and has no shared state across workers.

No live WISH property, payment service, production database or notification service was contacted during this setup.

## Lifecycle UI added for the next integration trial

The local UI at `/` now demonstrates linking a stay to a PMS property, creating a reservation immediately for a pending or confirmed booking, and forwarding confirmation, check-in, check-out and cancellation status changes. This is a **persistent simulator** separate from the earlier preview endpoints. Its local SQLite database records each event, attempt and simulated WISH identifier.

Cancellation and refunds are separate operations. A successful simulated refund emits a refund-sync event; a failed processor attempt does not. PMS outages leave local booking/payment results intact, and explicit retries reconcile already-applied events before draining the booking's queue. Refund keys and booking references protect local replay, including concurrent requests. Refund amounts cannot exceed the recorded paid balance.

This trial does not decide the real vendor's write operations, refund posting mechanism, payment provider callbacks or cancellation policy. In particular, a PMS refund sync must eventually record a processor refund that already happened; it must not trigger a second refund. Actual production writes and production outbox/worker wiring remain unimplemented. Rufescent remains unchanged.
