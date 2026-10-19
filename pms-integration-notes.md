# WISH PMS Integration Setup

## Scope and evidence

This guide is based on the four supplied PDFs, not a running WISH installation. The directory currently contains specifications and contributor guidance; there is no application to configure yet. Service versions, licensing, network access, credentials, and supported operations must be confirmed with the hotel and Prologic First.

`WISHAPI.pdf` has 100 printed pages; `PMS-WISHAPI.PDF` has 99. They overlap but should not be assumed interchangeable. Page references below use `WISHAPI.pdf` unless stated otherwise.

## Choose the interface by workflow

| Interface | Purpose | Authentication described |
| --- | --- | --- |
| `/WebAPI/api/selfservices/...` | Booking summary, guest details, folio, pre-check-in information, feedback, payment initiation/finalization | `clientID` and `authCODE`; confirm exact placement with vendor |
| `/WebAPI/api/cform/reservation/...` | Search bookings, retrieve a booking, update booking information | `clientID` and `authCODE` |
| `/WebAPI/api/bi/WishBI/getMasters` | Property, room types, market segments, business sources and other masters | `clientID` and `authCODE` |
| `/WebAPI/api/bi/WishBI/getBIDetails` | Business on books and reporting datasets | `clientID` and `authCODE` |
| `/WISHAPI/api/ARUpdate/`, `ARDelete/`, `GLUpdate/`, `GLDelete/` | Accounts receivable and general ledger master maintenance | Configured authentication header; exact name is unspecified |
| HTNG `ARIAndReservationPullService` | Read availability, rate plans and reservations | SOAP WS-Security UsernameToken |
| HTNG `ARIAndReservationPushService` | Send reservation create/modify/cancel and rate create/modify into WISH | SOAP WS-Security UsernameToken |

JSON read operations are documented as **POST**, despite names beginning with `get`. AR/GL APIs use XML data parameters alongside `PropertyID` and `UserId`; confirm the outer request encoding. SOAP services use HTNG/OpenTravel XML, not these JSON bodies.

Sources: API pp. 1–4, 14, 24–25, 34–46, 64–65; HTNG Pull and Push p. 1.

## How data moves

Recommended application arrangement:

```text
Application UI -> Our backend -> WISH JSON/SOAP service -> WISH PMS
                     |
                     +-> Local ID mappings, synchronization state and audit records
```

For a read, our backend authenticates, sends the property and search parameters, checks the transport and application result, then maps the result into our application's model.

For a reservation write, it translates local room/rate/source codes, constructs the required SOAP message, sends it to the push service, and saves returned WISH identifiers. Modify/cancel must use the identifier strategy configured for that installation. The push specification documents reservation ID versus confirmation number selection, plus different behavior when multi-stay is enabled (Push pp. 1–2).

“Push” in these documents describes external-system-to-WISH calls. A WISH-to-application webhook is not established by these specifications.

For queued availability/rate pulls, save and process received data durably before acknowledging it. The pull specification says acknowledgements delete previously retrieved records; premature acknowledgement could lose changes. Verify exact acknowledgement method spelling in the live service contract (Pull pp. 14–15, 22).

## What the hotel/vendor must provide

1. An enabled WISH integration environment, supported API version, and any required module/license activation.
2. Actual test and production JSON base URLs, SOAP endpoints, WSDLs and XML schemas. `localhost`, `baseURL`, `DEMO`, and sample credentials are placeholders.
3. JSON credentials, configured AR/GL header details if needed, and SOAP username/password. Confirm access scope for each property and operation.
4. Property/hotel codes, source/requestor IDs, WISH user ID, room types, rates, currencies, market/business sources, and other required master codes.
5. Network connectivity: public secured endpoint or a private network/VPN path, firewall allowlists, and TLS configuration as appropriate for the deployment.
6. Confirmed date formats, timezone/business-date rules, tax ownership, request limits, polling interval, and retry/duplicate handling behavior.
7. Test bookings and an agreed validation process for reads, create/modify/cancel, and any payment workflow in scope.

## Configuration belongs on two sides

**WISH service side:** The HTNG specifications show `Web.Config` settings for `Server`, `Database`, `PropertyId`, `SourceId`, `UserId`, `HTNGMappingXML`, and `SecurityXML`. The vendor/hotel administrator must configure those for its WISH service. These are not instructions to connect our application directly to the PMS database.

`HTNGMapping.xml` translates external codes into WISH codes, including room types, rates, market segments, business sources and currencies. Complete the mappings needed for the chosen operations. Confirm reservation identifiers and `AllowMultiStay` before implementing updates (Pull/Push pp. 1–7).

**Our backend:** Store endpoint URLs, property/source codes, and secret references in deployment configuration. Keep credentials server-side. Implement separate JSON and SOAP adapters, input validation, consistent error handling, timeout handling, ID mapping, and redacted diagnostics. Add a synchronization worker and durable checkpoints if scheduled synchronization is needed.

HTTP 200 alone is insufficient: JSON responses can contain `status_code`; SOAP responses have success/error or result flags; AR/GL documentation also defines application-level result values. Validate the relevant response contract for each operation.

Retry reads with bounded backoff when appropriate. For writes with an unknown outcome, reconcile using the external reference/WISH identifier before retrying; the PDFs do not establish a universal idempotency-key mechanism.

## First connection and rollout

1. Confirm which feature is needed: reporting, guest services, reservation synchronization, or accounting masters.
2. Establish access to a test property and make a read-only request. For JSON, a documented starting body for `POST /WebAPI/api/bi/WishBI/getMasters` is:

   ```json
   {
     "MastersEnum": "PROPERTY_INFO",
     "hotel_code": "<assigned-property-code>",
     "source_id": "<assigned-source-id>"
   }
   ```

   Supply authentication according to the vendor-confirmed contract. Next fetch `ROOM_TYPE` if needed (API pp. 46, 53).

3. Compare the result with WISH. Test incorrect authentication, invalid properties and unavailable service behavior.
4. Implement the smallest required workflow. For reservations: read availability/rates, create one test reservation, retrieve it, modify it, then cancel it. Verify each state in WISH and test duplicate submissions and timeouts.
5. For reporting, select `BIEnum` and date range for `getBIDetails`; for example, `BUSINESS_ON_BOOKS` (API pp. 64–65). Confirm report dates and reconciliation expectations.
6. For guest payments, the documented flow is `initiatepayment` -> returned `payment_url` -> configured return route -> backend `finalizepayment` using `track_id`. Confirm `transaction_status` rather than treating the browser redirect as payment success (API pp. 24–25).
7. Validate against production configuration before enabling real writes.

## Open questions before implementation

- Where is the actual application source, and which backend framework does it use?
- Which PMS workflows must the application support first?
- Is WISH hosted on the hotel's network or accessible from our backend?
- Has Prologic First supplied the enabled endpoints, service contracts and test access?

No live calls or production changes have been made as part of this document review.
