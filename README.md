# 🐺 CryptoHound

> **FIND · VALIDATE · RECONCILE · REPORT**  
> Evidence-driven digital-asset accounting, holdings reconstruction, tax intelligence, and AI-assisted investigation.

![CryptoHound banner](cryptohound/static/cryptohound-banner.png)

**Release:** `v1.0.17-hf1`  
**Architecture:** Flask · SQLite · Python · deterministic evidence/accounting engine  
**Doctrine:** **AI can investigate, explain, research and advise. It cannot rewrite the accounting truth.**

---

## What is CryptoHound?

CryptoHound is a WolfPack evidence-engineering application built to turn messy digital-asset records into a traceable, reviewable accounting picture.

Instead of trusting a single exchange export or silently filling gaps, CryptoHound preserves the source artifact, normalizes supported evidence into a canonical ledger, reconstructs holdings and tax lots, identifies missing or contradictory evidence, reconciles broker tax documents, and blocks tax exports when integrity checks fail.

Think of it as an **evidence pipeline with highly motivated raccoons inside**: heterogeneous records go in; preserved evidence, normalized facts, reconciliation findings, holdings, and tax intelligence come out. 🦝⌨️

## Mission

```text
SOURCE EVIDENCE
      ↓
PRESERVE + HASH
      ↓
NORMALIZE
      ↓
CANONICAL LEDGER
      ↓
RECONSTRUCT HOLDINGS / TAX LOTS
      ↓
VALIDATE + RECONCILE
      ↓
REPORT / INVESTIGATE / EXPORT
```

CryptoHound follows four operating principles:

1. **Preserve the receipt.** Original evidence is retained and identified by SHA-256.
2. **Normalize separately.** Derived accounting fields never replace the original source row.
3. **Show uncertainty.** Unknown or partial basis stays unknown/partial until evidence resolves it.
4. **One accounting truth.** Deterministic accounting outranks AI inference.

---

## Highlights

### 📥 Evidence Import
- CSV, XLSX/XLS, PDF, Markdown, and text ingestion.
- Original artifacts preserved under `data/imports`.
- SHA-256 duplicate protection.
- First-class Coinbase transaction CSV handling, including Coinbase preambles.
- Uphold transaction classification by economic meaning.
- Conservative Coinbase and Uphold Form 1099-DA parsing.
- Generic evidence preservation/mapping for additional providers such as Robinhood, SoFi, and E*TRADE.
- Explicit **Purge Evidence** control for removing a bad import and all rows derived from it before a clean re-import.

### 🧾 Canonical Ledger
Every normalized event retains provenance back to its imported source and original row JSON.

Ledger filters include:
- date range;
- provider;
- transaction type;
- asset.

Low-confidence mappings remain reviewable instead of being silently promoted to truth.

### 💰 Holdings
The Holdings engine reconstructs open inventory from the canonical ledger using deterministic FIFO lot consumption.

- Current Holdings is the primary operational view.
- Transfers move custody without becoming sales when the evidence supports that interpretation.
- Fiat is excluded from digital-asset holdings.
- Known remaining basis and average cost are calculated from reconstructed lots.
- Basis gaps are counted explicitly.
- **Holdings Evidence Review** is collapsed by default and shows the unresolved issue count.
- **Open Tax Lots / Basis Evidence** is collapsed by default for forensic drill-down.
- Observed value, source-lot basis, partial basis, and unknown basis remain visibly distinct.

### 🧮 Tax Intelligence
- Tax year is kept separate from filing year.
- Broker tax evidence is parsed into row-level records where supported.
- Decimal-based tax aggregation avoids binary floating-point summation drift.
- Row-level math/date/asset validation.
- Broker-document reconciliation against reported totals.
- Short-term / long-term classification where evidence supports it.
- Tax CSV and experimental TXF exports are **blocked unless the evidence-integrity gate passes**.

### 🤖 AI Advisor
CryptoHound includes a read-only AI investigation layer.

- **Luna** for fast/routine analysis.
- **Sol** for deeper investigation.
- Missions for evidence gaps, tax readiness, portfolio review, reconciliation, and investment research.
- Optional current-web research.
- Curated evidence context includes holdings, open lots, evidence gaps, ledger activity, tax records, reconciliation, and import provenance.
- AI cannot modify ledger rows, tax lots, basis, tax records, or source evidence.

The API key is read from `OPENAI_API_KEY`; it is not stored in the CryptoHound database.

### 🛡️ Evidence Integrity
CryptoHound deliberately prefers **“I don't know yet”** over fabricated certainty.

Examples:
- A transferred-in asset may have an observed broker value but still lack authoritative acquisition basis.
- A crypto-to-crypto acquisition can preserve the consumed source-lot basis as evidence without pretending it is automatically the replacement asset's FMV/basis.
- Ambiguous transfer/swap events are excluded from automatic holdings math and surfaced for review.

---

## Dashboard — the SITREP

The Mission Dashboard gives the high-level picture:

| Area | Meaning |
|---|---|
| Imports | Evidence artifacts ingested |
| Ledger Events | Normalized canonical events |
| Tax Records | Parsed tax-evidence rows |
| Review Queue | Items requiring attention |
| Reported Proceeds | Proceeds represented by tax evidence |
| Reported Basis | Basis represented by tax evidence |
| Realized P&L | Reported proceeds minus basis |
| Active / Net Positions | Summary of reconstructed current ownership |
| Liquidated / Tax Evidence | Disposed assets and tax results supported by imported tax evidence |

For the detailed accounting picture, use **Holdings**. For tax-document integrity, use **Tax Intelligence**.

---

## Install

### Linux

```bash
./install_linux.sh
./start_linux.sh
```

Open `http://127.0.0.1:8092`.

For a persistent Linux installation, a user-level systemd service can run the application directly from its virtual environment. See [`USER_GUIDE.md`](USER_GUIDE.md).

### Windows 11

1. Extract the release ZIP to a permanent directory such as `C:\WolfPack\CryptoHound`.
2. Run `install_windows.bat`.
3. Run `start_windows.bat`.
4. Open `http://127.0.0.1:8092`.

The launchers call the virtual-environment Python interpreter directly; manual venv activation is not required.

---

## Updating CryptoHound

Use **Setup / Admin → Update From ZIP**.

The updater:
- creates a pre-update code backup;
- preserves `data/`, `backups/`, and `.venv/`;
- blocks ZIP path traversal;
- replaces application files;
- requires an application restart afterward.

Typical systemd restart:

```bash
systemctl --user restart cryptohound
```

---

## AI configuration

Recommended Linux environment file:

```bash
mkdir -p ~/.config/cryptohound
chmod 700 ~/.config/cryptohound
printf '%s\n' "OPENAI_API_KEY=replace_me" > ~/.config/cryptohound/cryptohound.env
chmod 600 ~/.config/cryptohound/cryptohound.env
```

Keep secrets out of source control. The application database stores AI settings and chat history, **not the API key**.

---

## Data & safety boundaries

CryptoHound is an evidence/reconciliation and accounting-support application. It is not a substitute for professional tax, legal, or investment advice.

Important boundaries:
- Generic provider mappings are conservative and should be validated against real provider exports.
- Unknown basis is not guessed.
- “Observed value” is evidence, not automatically authoritative tax basis.
- TXF compatibility varies by tax-software product/version and remains experimental.
- Validate tax outputs against the underlying evidence before filing.
- Purge Evidence is intentionally destructive: it removes the selected source artifact and all derived rows.

---

## Repository hygiene

Never commit private financial evidence, databases, API secrets, tax documents, exports, or runtime data.

Recommended exclusions include:

```text
/data/
/backups/
.venv/
.env
*.db
*.sqlite*
*.csv
*.xlsx
*.xls
*.pdf
*.txf
```

A public repository should contain application source, documentation, safe static assets, and configuration examples only.

---

## Current release lineage

| Version | Major change |
|---|---|
| 1.0.10 | Deterministic Holdings / FIFO open-lot reconstruction |
| 1.0.11 | Read-only AI Advisor |
| 1.0.12 | Coinbase CSV adapter + Purge Evidence |
| 1.0.13 | Uphold custody-chain/fiat classification + Holdings authority |
| 1.0.14 | Basis evidence stack: known / partial / unknown |
| 1.0.15 | Collapsible Holdings Evidence Review with issue count |
| 1.0.16 | Canonical Ledger filters |
| 1.0.17 | Current Holdings promoted; tax-lot detail collapsed by default |
| **1.0.17-hf1** | **Documentation hotfix: rebuilt README + detailed USER_GUIDE** |

---

## Documentation

📘 **Start here:** [`USER_GUIDE.md`](USER_GUIDE.md) — installation, workflow, screen-by-screen operation, evidence doctrine, AI setup, backups, updates, troubleshooting, and tax-export controls.

---

## WolfPack doctrine

> **Transactions establish history. Lots establish basis. Custody establishes location. Tax forms establish what the broker reported. CryptoHound reconciles the evidence and preserves uncertainty instead of inventing certainty.**

**Semper Evidence Engineering. 🐺🧾**
