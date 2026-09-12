# CryptoHound User Guide

**Applies to:** CryptoHound `v1.0.17-hf1`  
**Purpose:** Operator guide for importing evidence, reviewing the canonical ledger, reconstructing holdings, investigating basis gaps, reconciling tax evidence, using the AI Advisor, and maintaining the application.

---

## 1. CryptoHound in Barney Style

CryptoHound answers four different questions without mixing them together:

1. **What receipts did I give you?** → **Import**
2. **What events do those receipts say happened?** → **Canonical Ledger**
3. **What crypto do I still own, where is it, and what basis can we prove?** → **Holdings**
4. **What did the brokers report for tax purposes, and does it reconcile?** → **Tax Intelligence**

The **Dashboard** is the SITREP. The **AI Advisor** is the investigator sitting above the evidence. It can explain and research; it cannot change accounting truth.

---

## 2. Evidence doctrine

### 2.1 Preserve first
An imported artifact is evidence. CryptoHound stores the original source and identifies duplicate evidence by SHA-256. Normalization creates derived records; it does not rewrite the original source.

### 2.2 Evidence and accounting are different layers
A source can tell CryptoHound that an asset was worth a certain amount at a particular event without proving the asset's original acquisition basis. CryptoHound therefore distinguishes:

- **Known basis** — sufficient evidence supports the accounting basis.
- **Partial basis** — useful basis evidence exists, but a required component is missing.
- **Unknown basis** — authoritative basis cannot yet be established.
- **Observed value** — a value reported/derived from the source event; not automatically tax basis.
- **Source-lot basis** — basis associated with assets consumed in an exchange; preserved as evidence rather than automatically substituted for replacement-asset FMV.

### 2.3 Never invent certainty
If a transfer arrives without upstream acquisition history, CryptoHound keeps the quantity but flags the basis gap. If an event is ambiguous, it can be excluded from automatic holdings math and placed in review.

### 2.4 AI is subordinate to evidence
The deterministic ledger, lot engine, validation rules, and tax integrity gate are authoritative. AI output is analysis, not a database correction.

---

## 3. Installation

### 3.1 Linux quick install
From the extracted CryptoHound release directory:

```bash
./install_linux.sh
./start_linux.sh
```

Then browse to:

```text
http://127.0.0.1:8092
```

### 3.2 Windows 11 quick install
1. Extract the ZIP to a permanent directory, for example `C:\WolfPack\CryptoHound`.
2. Run `install_windows.bat`.
3. Run `start_windows.bat`.
4. Browse to `http://127.0.0.1:8092`.

### 3.3 Linux systemd user service
For an installation at `/opt/wolfpack/cryptohound`, a user service can keep CryptoHound manageable without activating the venv manually.

Example `~/.config/systemd/user/cryptohound.service`:

```ini
[Unit]
Description=CryptoHound
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/wolfpack/cryptohound
ExecStart=/opt/wolfpack/cryptohound/.venv/bin/python /opt/wolfpack/cryptohound/run.py
EnvironmentFile=-%h/.config/cryptohound/cryptohound.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Enable it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now cryptohound
```

Useful commands:

```bash
systemctl --user status cryptohound
systemctl --user restart cryptohound
journalctl --user -u cryptohound -f
```

If the service must start at boot without an interactive login, the host administrator can enable user lingering.

---

## 4. Recommended operating workflow

A clean evidence cycle is:

```text
1. IMPORT
2. VERIFY IMPORT STATUS
3. REVIEW LEDGER
4. REVIEW HOLDINGS + EVIDENCE GAPS
5. IMPORT SUPPORTING EVIDENCE
6. REVIEW TAX INTELLIGENCE
7. RECONCILE
8. EXPORT ONLY WHEN INTEGRITY PASSES
9. BACK UP
```

Do not “fix” an unexplained number by forcing a manual value into the accounting layer. Find the missing receipt or identify the classification problem.

---

## 5. Mission Dashboard

The Dashboard is the executive SITREP rather than the forensic workspace.

### Mission cards
- **Imports** — number of evidence imports.
- **Ledger Events** — normalized events in the canonical ledger.
- **Tax Records** — parsed row-level tax records.
- **Review Queue** — records requiring attention.
- **Reported Proceeds** — proceeds represented by imported tax evidence.
- **Reported Basis** — basis represented by imported tax evidence.
- **Realized P&L** — proceeds minus basis represented by tax evidence.

### Active / Net Positions
This is the summary answer to **“What crypto do I still own?”** It presents asset, reconstructed quantity, and observed custody/location information. The Holdings tab is the detailed authoritative drill-down.

It is not live market valuation.

### Evidence Health
Shows evidence/reconciliation findings and recent imports.

### Liquidated / Tax Evidence
This is the **“stuff I disposed of, with tax receipts”** summary. For each asset it shows:

```text
Proceeds - Basis = Gain/Loss
```

It summarizes imported tax evidence; Tax Intelligence provides the detailed reconciliation view.

---

## 6. Evidence Import

Open **Import** from the main navigation.

### Supported files
- `.csv`
- `.xlsx`
- `.xls`
- `.pdf`
- `.md`
- `.txt`

### Provider selection
Use **Auto-detect** when appropriate, or explicitly select a provider when you know the source. Current provider choices include Uphold, Coinbase, Robinhood, SoFi, E*TRADE, and Generic.

Provider-specific logic is strongest where CryptoHound has been built and tested against actual source formats. Generic imports intentionally map conservatively.

### Duplicate protection
CryptoHound hashes source artifacts. Re-importing the identical artifact is blocked rather than silently duplicating its events.

### Import History
Import History records source file, provider, source type, status, and import time.

### Purge Evidence — destructive control
**Purge Evidence** permanently removes:
- the selected imported source artifact;
- canonical ledger rows derived from it;
- tax records/documents/issues derived from it;
- the import record itself.

Use Purge Evidence when a source was parsed incorrectly and you need a clean re-import. The UI requires confirmation. This is intentionally destructive; back up first if the evidence may be needed later.

---

## 7. Canonical Ledger

The Canonical Ledger is CryptoHound's normalized event history.

Each imported event retains its original row as JSON so the normalized interpretation can be traced back to source evidence.

### Ledger columns
- **Time** — normalized event time currently displayed by the ledger.
- **Provider** — source provider.
- **Type** — normalized economic/event classification.
- **Asset** — affected digital asset.
- **Qty** — normalized quantity.
- **USD Value** — source-derived fiat value when available.
- **Fee** — fee quantity and fee asset.
- **Origin → Destination** — custody/economic movement evidence.
- **Confidence** — parser/mapping confidence.

### Filters
The Ledger supports combinable server-side filters:
- **From** date;
- **To** date;
- **Provider**;
- **Type**;
- **Asset**.

Click **Apply Filters** to query the canonical ledger. **Clear** resets the filter set. The page displays matching-event and active-filter counts.

### Timestamp rule
The evidence doctrine is: **preserve the timestamp/time-zone information supplied by the official source; normalize separately for correlation when needed; never invent a timezone the source did not provide.**

The current ledger display should therefore be interpreted in conjunction with the imported provider evidence. A future display refinement may make source timezone/offset provenance more explicit; do not infer a timezone solely because the column is labeled `Time`.

---

## 8. Holdings

Holdings is the main current-inventory feature.

The engine reconstructs open inventory from canonical ledger events using deterministic FIFO lot consumption and custody-aware transfer handling.

### Summary cards
- **Assets Held** — distinct reconstructed assets with open quantity.
- **Open Lots** — number of reconstructed open tax lots.
- **Known Remaining Basis** — sum of basis CryptoHound can currently support for open lots.
- **Basis Gaps** — open lots for which authoritative remaining basis is incomplete/unknown.

### Holdings Evidence Review
This panel is **collapsed by default**. Its badge tells you how many issues need resolution. Expand it to see the exact findings, such as:
- ambiguous transfer/swap events;
- sales exceeding reconstructed inventory;
- missing upstream inventory;
- other custody/accounting inconsistencies.

The warning list is recomputed from the evidence. Resolve the underlying evidence/classification and the warning can disappear without manually editing the warning.

### Current Holdings
This is the primary table on the Holdings tab.

Columns:
- **Asset**
- **Quantity Remaining**
- **Known Remaining Basis**
- **Avg Cost / Unit**
- **Open Lots**
- **Custody / Location**
- **Basis Status**

`COMPLETE` means the reconstructed open lots have known remaining basis. `INCOMPLETE (N)` means one or more open lots contain a basis gap.

### Open Tax Lots / Basis Evidence
This forensic section is **collapsed by default** so Current Holdings remains the main feature. The header shows the open-lot count. Expand it when you need lot-level proof.

The detail can include:
- asset and remaining quantity;
- acquisition date;
- original basis;
- remaining basis;
- cost per unit;
- observed value;
- source lots consumed;
- known/partial/unknown basis status;
- reason basis is missing;
- current holding period;
- custody;
- provider;
- import/source evidence.

### Transfers and custody
When evidence supports a self-owned transfer, CryptoHound moves custody rather than treating the movement as a sale. Acquisition history and basis stay attached to the lot as it moves.

An unmatched transfer-in can create an open lot with unknown basis. This is correct behavior when the upstream acquisition evidence is absent.

### Crypto-to-crypto events
A crypto-to-crypto exchange is not treated like a simple custody move. CryptoHound can preserve the basis of source lots consumed as evidence while leaving the replacement asset's authoritative acquisition basis partial until the required FMV/value evidence is available.

---

## 9. Tax Intelligence

Tax Intelligence is the filing-evidence and reconciliation workspace.

### Tax year vs. filing year
CryptoHound keeps the transaction/disposition **tax year** separate from the later **filing year**. Select the desired tax year on the page.

### Summary cards
- Proceeds
- Cost Basis
- Realized P&L
- Short-Term
- Long-Term

### Tax Evidence Integrity
This is the gatekeeper.

CryptoHound checks supported tax evidence for issues including:
- malformed asset identifiers;
- acquisition dates after disposition dates;
- row math where `proceeds - basis` does not reconcile to gain/loss;
- parsed row totals that do not reconcile to broker-document totals;
- tax-year/disposition-year inconsistencies;
- quarantined parser evidence.

### Export gate
If integrity fails, **Export CSV** and **Export TXF** are blocked.

If integrity passes, exports become available. TXF remains experimental because tax-software support varies.

### Broker Tax Evidence
Displays row-level broker tax records with provider, asset, units, acquisition/disposition dates, proceeds, basis, gain/loss, and term.

### Reconciliation Intelligence
Surfaces comparisons between broker/tax evidence and the canonical ledger so discrepancies can be investigated before filing.

---

## 10. AI Advisor

The AI Advisor is a read-only analyst over CryptoHound's curated evidence context.

### Status
The page shows:
- API readiness;
- default model;
- evidence-gap count;
- current tax year.

### Advisor Missions
Built-in missions include:
- **Investigate Evidence Gaps**
- **Tax Readiness SITREP**
- **Portfolio Review**
- **Reconciliation Review**
- **Investment Review**

### Modes
- Full-Service Advisor
- Accounting & Tax
- Portfolio Advisor
- Investment Research
- Evidence Investigator
- Reconciliation

### Models
- `gpt-5.6-luna` — fast/economical default.
- `gpt-5.6-sol` — deeper analysis.

### Web research
Enable **Current web research** when the question requires current market/news/public information. Evidence from CryptoHound and current external research should remain conceptually separate.

### What the AI can see
The curated packet can include dashboard metrics, holdings/open lots, evidence gaps, recent ledger events, tax records, 1099 reconciliation, and import provenance.

### What the AI cannot do
It cannot modify:
- source evidence;
- canonical ledger rows;
- lots;
- basis;
- tax records.

### Chat history
AI chat history is stored locally in SQLite and can be cleared independently of accounting evidence.

---

## 11. AI API-key configuration

CryptoHound reads the API secret from the environment variable `OPENAI_API_KEY`.

Recommended Linux setup:

```bash
mkdir -p ~/.config/cryptohound
chmod 700 ~/.config/cryptohound
cat > ~/.config/cryptohound/cryptohound.env <<'EOF'
OPENAI_API_KEY=replace_me
EOF
chmod 600 ~/.config/cryptohound/cryptohound.env
```

If using the systemd example in this guide, restart afterward:

```bash
systemctl --user restart cryptohound
```

Check **Setup / Admin → AI Integration** for `READY`.

Never commit the real key to Git.

---

## 12. Setup / Admin

### System
Displays application version, import/transaction/tax-record counts, tax-document/parser issue counts, and database size.

### Create DB Backup
Use **Create DB Backup** before major evidence changes, purges, migrations, or upgrades.

### Update From ZIP
1. Obtain the intended CryptoHound update ZIP.
2. Open **Setup / Admin**.
3. Select the ZIP under **Update From ZIP**.
4. Click **Apply Update ZIP**.
5. Restart CryptoHound.

The updater creates a pre-update code backup and preserves `data/`, `backups/`, and `.venv/` while blocking archive path traversal.

Systemd restart:

```bash
systemctl --user restart cryptohound
```

### AI Integration settings
Setup/Admin also stores non-secret AI settings such as provider, default model, endpoint, and advisor/tax-profile notes. The API secret remains environment-based.

---

## 13. Backups and recovery discipline

Before a risky operation:

1. Create a DB backup in Setup/Admin.
2. Preserve the original provider evidence outside CryptoHound as well.
3. Confirm the update/import artifact is the intended file.
4. Perform the change.
5. Restart if required.
6. Re-check Dashboard, Holdings, and Tax Intelligence.

CryptoHound also creates automatic pre-migration backups for certain parser migrations.

---

## 14. Troubleshooting

### The version changed on disk but the UI still shows the old version
The running Flask process still has the old code loaded. Restart it:

```bash
systemctl --user restart cryptohound
```

Then refresh the browser.

### The same evidence file will not import again
SHA-256 duplicate protection is working. If the previous import was bad and you intentionally need to start over, use **Purge Evidence** for that import, then re-import the corrected artifact.

### Holdings shows a basis gap
Do not guess the number. Expand **Open Tax Lots / Basis Evidence** and inspect:
- source filename/import ID;
- observed value;
- source-lot basis evidence;
- missing-basis reason;
- custody chain.

Then import the missing upstream transaction history or other supporting evidence.

### A sale exceeds reconstructed inventory
CryptoHound has evidence for a disposition but cannot reconstruct enough prior inventory. Typical causes include missing earlier account history, transferred-in assets without their origin records, or a classification issue. Find the upstream evidence rather than creating an artificial acquisition.

### A transfer is being treated ambiguously
Review origin, destination, source asset, destination asset, and raw provider evidence. CryptoHound intentionally avoids guessing whether an unclear event was a custody transfer or an exchange.

### Tax export is blocked
Open **Tax Intelligence → Tax Evidence Integrity**. Resolve the listed parser/reconciliation issues. The gate is designed to prevent a questionable evidence set from being exported as filing-ready.

### AI says API OFFLINE
Confirm `OPENAI_API_KEY` exists in the environment seen by the CryptoHound process and restart the service/application.

### AI gives a number that conflicts with CryptoHound
The deterministic CryptoHound accounting value wins. Treat the AI statement as analysis to investigate, not as an accounting correction.

---

## 15. Privacy and repository safety

CryptoHound contains highly sensitive financial evidence. Keep runtime evidence out of public source control.

Do not commit:
- `data/`;
- `backups/`;
- SQLite databases;
- tax PDFs;
- transaction CSV/XLSX files;
- exports;
- `.env` files;
- API keys.

A safe public repository contains source code, documentation, safe static assets, `.env.example`, and security guidance—not personal evidence.

---

## 16. Tax and investment boundaries

CryptoHound is designed to preserve and reconcile evidence and assist with accounting/tax preparation. It does not replace a qualified tax professional, attorney, or investment adviser.

Before filing:
- review the underlying source evidence;
- verify the selected tax year;
- require the Tax Evidence Integrity gate to pass;
- inspect exported records;
- resolve known basis gaps that affect reportable dispositions;
- confirm tax-software import behavior, especially for experimental TXF output.

---

## 17. Operator cheat sheet

| I want to… | Go to… |
|---|---|
| Add exchange/tax evidence | Import |
| See exactly what events were normalized | Ledger |
| Find one event | Ledger filters |
| See what I own now | Holdings → Current Holdings |
| See why basis is incomplete | Holdings → Open Tax Lots / Basis Evidence |
| See unresolved inventory issues | Holdings → Holdings Evidence Review |
| See what I sold and the tax result | Dashboard → Liquidated / Tax Evidence |
| Reconcile broker tax documents | Tax Intelligence |
| Export tax rows | Tax Intelligence, after integrity PASS |
| Ask “what is wrong here?” | AI Advisor → Evidence Investigator |
| Get a tax readiness brief | AI Advisor → Tax Readiness SITREP |
| Back up the DB | Setup / Admin |
| Apply a hotfix/release | Setup / Admin → Update From ZIP |

---

## 18. Final doctrine

> **Transactions establish history. Lots establish basis. Custody establishes location. Tax forms establish what the broker reported. CryptoHound reconciles the evidence and preserves uncertainty instead of inventing certainty.**

When in doubt: **keep the receipt, expose the gap, and make the evidence prove the answer.**

**Semper Evidence Engineering. 🐺🧾🦝**
