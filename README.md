# <img width="1670" height="942" alt="image" src="https://github.com/user-attachments/assets/c8c14cda-0e03-4083-af89-c6e34b1b91e4" />

# CryptoHound v1.0.9

WolfPack evidence-driven digital-asset ledger and tax-intelligence prototype.

## What v1 does

- Imports CSV, XLSX/XLS, PDF, Markdown and text evidence.
- Preserves every original upload and deduplicates by SHA-256.
- Includes an Uphold transaction adapter with buy/transfer/swap classification.
- Includes conservative Coinbase and Uphold 1099-DA PDF parsing.
- Maintains a canonical SQLite ledger with original row JSON and confidence/review flags.
- Dashboard for net positions, liquidated/tax evidence, review queue and evidence health.
- Tax Intelligence view with annual totals, broker evidence and reconciliation hints.
- CSV export and experimental TXF export.
- Setup/Admin tab with DB backup and **Update From ZIP**.
- Read-only `/api/ai/context` endpoint plus stored AI provider/model/endpoint settings.
- Windows and Linux launchers invoke the virtual-environment interpreter directly.

## Important v1 boundaries

CryptoHound v1.0.4 is an evidence/reconciliation prototype, not tax advice and not yet a complete tax-lot accounting engine. Generic Robinhood, SoFi and E*TRADE exports are preserved and imported with conservative field mapping, but provider-specific adapters should be added from real samples. PDF parsing is deliberately conservative: ambiguous data is kept for review rather than silently asserted as tax truth.

The current dashboard position calculation is a net-flow view, not a finalized lot/basis engine. TXF support varies by tax software/version; the TXF exporter is therefore labeled experimental. Validate exports before filing.

## Install — Windows 11

1. Extract the ZIP to a permanent folder, e.g. `C:\WolfPack\CryptoHound`.
2. Run `install_windows.bat`.
3. Run `start_windows.bat`.
4. Browse to `http://127.0.0.1:8092`.

The launcher calls `.venv\Scripts\python.exe` directly, so venv activation is not required.

## Install — Linux

```bash
./install_linux.sh
./start_linux.sh
```

Browse to `http://127.0.0.1:8092`.

## Update From ZIP

Setup / Admin → Update From ZIP. CryptoHound backs up current application code, preserves `data/`, `backups/`, and `.venv/`, validates archive paths, applies code files, and asks for a restart.

## Data model doctrine

Transactions establish history. Lots establish basis. Custody establishes location. Tax forms establish what the broker reported. CryptoHound reconciles the evidence and preserves uncertainty instead of inventing certainty.


## v1.0.2 hotfix
- CH-001 rejects/quarantines numeric or malformed asset tokens (kills the UFO rows).
- CH-002 validates acquisition date <= disposition date.
- CH-003 enforces proceeds - basis ≈ reported P&L at row level.
- CH-004 reconciles parsed rows back to broker 1099-DA document totals.
- CH-005 derives short/long holding term deterministically when dates are available and preserves broker section term when acquisition is VARIOUS.
- CH-006 blocks tax CSV/TXF export unless evidence integrity passes. Raw source evidence remains preserved.
- CH-007 attributes evidence to the 1099 tax year / disposition year, never the import or filing year. Tax Intelligence defaults to the newest imported tax year and displays the normal filing season separately.
- Existing Coinbase/Uphold PDF imports are automatically reparsed once on first v1.0.2 startup; an automatic pre-migration DB backup is created.


## v1.0.2 hotfix
- Decimal-based canonical tax aggregation (no binary-float summation)
- Adaptive, comma-grouped crypto unit display without scientific notation
- Full imported precision remains in the database/source evidence
- Tax cards and short/long buckets share one deterministic aggregation path


## v1.0.3 branding update
- Centers the CryptoHound application brand in the top header.
- Adds the new CryptoHound wolf/XRP/BTC/ETH banner to the Mission Dashboard.
- Banner tagline: **FIND · VALIDATE · RECONCILE · REPORT**.
- Keeps tax/evidence claims out of the artwork tagline while retaining the existing application evidence-engineering language.

## v1.0.4 compact header branding
- Moves the CryptoHound wolf/crypto artwork into the centered application header.
- Removes the large dashboard hero banner to restore dashboard working space.
- Keeps the version / Evidence Engineering indicator on the right and navigation immediately below.
- Responsive header artwork scales down on narrow screens.

## v1.0.5 branding refresh

- Replaces the compact header artwork with the approved unified Wolfie + XRP/BTC/ETH + bullish chart composition.
- Header remains centered and responsive; application behavior and tax/reconciliation logic are unchanged from v1.0.4.


## v1.0.6 header readability

- Enlarges the centered CryptoHound header artwork so the FIND / VALIDATE / RECONCILE / REPORT line remains readable on desktop displays.
- Keeps the version / Evidence Engineering indicator on the right.
- No tax, ledger, import, reconciliation, or persistence logic changes.


## v1.0.7 navigation polish

- Adds compact vector icons to Dashboard, Import, Ledger, Tax Intelligence, and Setup / Admin.
- Adds an active-tab treatment with subtle blue fill and underline.
- Keeps inactive navigation muted so the header remains clean and operational.
- Preserves all v1.0.6 tax, ledger, import, reconciliation, update, and persistence behavior.


## v1.0.8 visual navigation / header hardening

- Reworks the application header to match the approved mockup: large left-anchored CryptoHound banner and version/status on the right.
- Adds durable SVG nav icons and a blue active-tab pill/underline.
- Adds cache-busting to the stylesheet and brand image so ZIP updates cannot leave an old browser CSS/image cached.
- Adds dashboard metric icons without changing tax/ledger/import logic.
- No database schema or tax calculation changes.


## v1.0.9 mission-card single-row refinement

- Keeps all seven Mission Dashboard metrics, including Realized P&L.
- Fits all seven cards on one row on normal desktop-width layouts by tightening card padding, icon size, gaps, and metric typography.
- Preserves responsive wrapping on narrower screens.
- No tax, ledger, import, reconciliation, or persistence logic changed.


## v1.0.10
- Added **Holdings** between Ledger and Tax Intelligence.
- Reconstructs open crypto inventory from the canonical ledger using deterministic FIFO lot consumption.
- Recognized transfers change custody/location without being treated as sales.
- Unmatched transfers preserve quantity but explicitly flag unknown basis; CryptoHound never fabricates basis.
- Holdings roll-up shows quantity, remaining basis, average cost, lot count, custody/location and basis completeness.
- Open-lot evidence view shows acquisition date, original/remaining basis, unit cost, current ST/LT holding period, provider and source import.


## v1.0.11
- Adds **AI Advisor** between Holdings and Tax Intelligence.
- Luna (`gpt-5.6-luna`) is the default fast/economical model; Sol (`gpt-5.6-sol`) is selectable for deeper analysis.
- Missions: evidence investigation, tax readiness, portfolio review, reconciliation, investment review, and free-form full-service chat.
- Optional current-web research toggle uses OpenAI Responses API web search.
- AI receives a curated read-only evidence packet from CryptoHound; it cannot modify ledger, lots, basis, tax records, or source evidence.
- Chat history is stored locally in SQLite and can be cleared independently of evidence/accounting data.
- API key is read from `OPENAI_API_KEY`; CryptoHound does not store API secrets in its database.
