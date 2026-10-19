# Repository Guidelines

## Project Structure & Module Organization

This directory currently contains PDF reference documents for PMS/WISH API and front-office HTNG PDS integrations:

- `PMS-WISHAPI.PDF` and `WISHAPI.pdf`: API reference documents.
- `WISH FRONT OFFICE HTNG  PDS - PULL -  SPECIFICATION.pdf`: pull specification.
- `WISH FRONT OFFICE HTNG  PDS - PUSH -  SPECIFICATION.pdf`: push specification.

All documents reside at the repository root. There are currently no source-code, test, or asset directories. Treat similarly named PDFs as separate references until their contents and versions have been compared.

## Build, Test, and Development Commands

No build system, package manifest, development server, or automated test commands are present. Do not assume that `npm test`, `make`, or other application commands apply.

Use PowerShell to inspect the document inventory:

```powershell
Get-ChildItem -File *.pdf
```

Open PDFs in a local viewer to review their contents. If implementation code is introduced, document its setup, build, and test commands alongside it.

## Documentation Style & Naming Conventions

Use concise Markdown with descriptive headings for contributor-facing notes. Reference existing filenames exactly, including spaces and capitalization. Preserve supplied specification filenames; avoid renaming or replacing documents without explaining the reason. For new supporting notes, prefer descriptive names such as `integration-notes.md`.

## Validation Guidelines

No testing framework or coverage threshold is configured. For document changes, confirm that each PDF opens, pages render correctly, and the intended specification and version are included. Check Markdown links and filename references. When replacing a specification, explain its provenance and summarize relevant differences.

## Commit & Pull Request Guidelines

No Git metadata is available in this checkout, so an existing commit convention cannot be established. Use concise, imperative messages such as `docs: add integration notes`.

Pull requests should explain the change, identify affected documents, link any relevant issue, and describe validation performed. Include page references when discussing specification changes.

## Security & Configuration

Keep credentials, access tokens, customer records, and environment-specific connection details out of documents and examples. Confirm permission to redistribute third-party specifications before adding or updating them.
