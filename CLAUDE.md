# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A workspace for integrating **Prologic First WISH PMS** with the Stay / Book a Stay module of **Rufescent** (a separate FastAPI app at `C:\Users\RiteshDhapate\dynasas\rufescent`). It holds:

- Four vendor PDFs at the root (`WISHAPI.pdf`, `PMS-WISHAPI.PDF`, and the HTNG PDS `PULL`/`PUSH` specifications, whose filenames contain double spaces). Preserve the filenames exactly. The two WISHAPI PDFs overlap but are not assumed identical.
- `pms-integration-notes.md`: what the PDFs say about each interface and the setup prerequisites.
- `context.md`: a detailed handoff of the prior conversation (user intent, decisions, open issues). **Read it before substantial work.**
- `test-project/`: a standalone Python 3.12 FastAPI test harness. This is where all the code lives.
- `AGENTS.md`: the original contributor guide. Its "no source code" inventory predates `test-project/`.

Hard boundaries from the user:
- **Rufescent is read-only.** Never modify it, run it, import its modules, or use its DB or credentials. Read it only as reference.
- Touché POS (`/ToucheAPI`) and WISH PMS (`/WISHAPI`) are separate integrations. Don't mix up their URLs or credentials.
- Never invent WISH endpoint paths, dashboard navigation ("Settings → Users"), status fields, or write contracts. Anything not in the PDFs or confirmed by the vendor must be labeled unverified.
- Don't overwrite or echo `test-project/.env`.
- An earlier Node.js harness was deliberately replaced by FastAPI. Don't recreate it.

## Commands (run from `test-project/`)

```powershell
uv sync
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8010   # UI at /, Swagger at /docs
uv run pytest
uv run pytest tests/test_lifecycle.py::test_name                     # single test
uv run pytest -k refund                                              # by keyword
uv run ruff check .
uv run ruff format --check .
node --check app/static/app.js; node --check app/static/tabs.js      # optional JS syntax check
```

Port 8010 avoids Rufescent's port 8000. Bind to loopback only, because the harness has no caller authentication. Ruff line length is 100. The frontend in `app/static/` is plain HTML/CSS/JS with no build step and no CDN.

## Architecture

`app/main.py:create_app(settings, transport, soap_transport, live_transport)` is the factory. Its lifespan builds one shared `httpx.AsyncClient` and puts the collaborators on `app.state`:

- `pms` → `PmsClient` (`app/pms.py`): JSON reads (`getMasters`, selfservices guest/folio, `cform/reservation` get/search). Upstream "get" operations are **POST**. Auth uses configurable `clientID`/`authCODE` headers. Applications fail on JSON `status_code`, not just HTTP status. `PMS_JSON_PATHS` can override the relative paths.
- `htng` → `HtngClient` (`app/htng.py`): HTNG SOAP Pull reads (availability and rate plans) with WS-Security UsernameToken. Never sends acknowledgements, because those would delete WISH queue records.
- `stay` → `StayHarness` (`app/stay.py` + `stay_schemas.py`): Rufescent-shaped scenarios, room/audience/package → WISH code mappings (`fixtures/mappings.json`), reservation previews that never transmit, and an **older in-memory** simulator under `/api/rufescent/*`.
- `live_gateway` → `LiveGateway` (`app/live.py`): real JSON/SOAP **write** transport, driven only by vendor-verified operation contracts in `PMS_LIVE_CONTRACTS_FILE`. The contract format is in `docs/live-mode.md`, and `fixtures/live-contracts.example.json` is deliberately disabled. The gateway uses `{{dotted.path}}` templating and success/reservation-ID selectors.
- `lifecycle` → `Lifecycle` (`app/lifecycle.py`): the **SQLite-backed** (`data/lifecycle.sqlite3`) property-link / booking / status / refund ledger behind `/api/lifecycle/*` and the UI. Mock and live rows are kept separate.

Mode (`PMS_MODE`, settings in `app/config.py` with the `PMS_` env prefix):
- `mock` (default): the JSON client is forced to `http://mock-pms/...` with `pms.mock_handler`, even if `.env` holds live values. Lifecycle sync is simulated, with failure injection (`before_apply` / `after_apply`).
- `live`: real reads, plus writes for each operation that has a verified contract. Missing credentials don't stop startup; readiness guards block the operation instead. Synthetic mappings are rejected for live SOAP.

Lifecycle invariants that tests depend on and that must be preserved:
- Status flow is pending → confirmed → checked_in → checked_out. Cancel is allowed only from pending/confirmed. Repeating the same status is a no-op.
- Events are delivered in order per booking. A failed event blocks the later events for that booking until an explicit retry. There's no background worker.
- Local intent is committed before the HTTP write, and the SQLite transaction is never held open over the network. Timeouts, non-success responses, and unproven success become `reconciliation_required` and are **never auto-replayed**.
- Cancellation, refund completion, and PMS refund recording are separate states. **A PMS retry must never re-issue a payment refund.** Live refunds only *record* a refund already completed externally, using its provider reference. Idempotency keys are bound to the booking and amount.
- Keep Rufescent guest UUIDs, WISH guest IDs, booking references, and WISH reservation IDs distinct. `MOCK-` IDs are rejected as real reservation IDs.

Error mapping: upstream HTTP failures return 502 and timeouts return 504. Upstream bodies are never included in error messages, and network calls are never retried automatically.

## Testing approach

Tests build the app with `create_app(Settings(_env_file=None, ...), httpx.MockTransport(handler), ...)` inside `TestClient`, so they never touch `.env` or a real PMS. Inject the SOAP and live transports the same way. Passing tests prove adapter behavior, not compatibility with the installed WISH server. The live endpoints and write contracts are still unverified.

## Reading the PDFs

```powershell
& 'C:\Program Files\Git\ucrt64\bin\pdftotext.exe' -layout '<input.pdf>' '<output.txt>'
```

Cite page numbers when discussing specifications.
