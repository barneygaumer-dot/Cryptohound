from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

import pandas as pd
from dateutil import parser as dtparser
from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, url_for
from markupsafe import Markup, escape
from pypdf import PdfReader
from werkzeug.utils import secure_filename

VERSION = "1.0.17-hf3"
APP_NAME = "CryptoHound"
FIAT_ASSETS = {"USD", "EUR", "GBP"}
UPHOLD_EXTERNAL_CUSTODY = {"xrp-ledger", "ethereum", "bitcoin", "stellar"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filename TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  source_type TEXT NOT NULL,
  provider TEXT,
  status TEXT NOT NULL DEFAULT 'ok',
  notes TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_import_sha ON imports(sha256);

CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_id INTEGER NOT NULL,
  external_id TEXT,
  event_time TEXT,
  provider TEXT,
  account TEXT,
  asset TEXT,
  quantity REAL,
  fiat_value REAL,
  fiat_currency TEXT DEFAULT 'USD',
  fee_asset TEXT,
  fee_quantity REAL,
  fee_usd REAL,
  tx_type TEXT,
  origin TEXT,
  destination TEXT,
  status TEXT,
  raw_json TEXT NOT NULL,
  confidence REAL DEFAULT 1.0,
  review_required INTEGER DEFAULT 0,
  FOREIGN KEY(import_id) REFERENCES imports(id)
);
CREATE INDEX IF NOT EXISTS ix_tx_asset ON transactions(asset);
CREATE INDEX IF NOT EXISTS ix_tx_time ON transactions(event_time);

CREATE TABLE IF NOT EXISTS tax_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_id INTEGER NOT NULL,
  provider TEXT,
  tax_year INTEGER,
  asset TEXT,
  units REAL,
  acquired_date TEXT,
  disposed_date TEXT,
  proceeds REAL,
  cost_basis REAL,
  gain_loss REAL,
  term TEXT,
  basis_reported INTEGER DEFAULT 0,
  source_note TEXT,
  raw_text TEXT,
  FOREIGN KEY(import_id) REFERENCES imports(id)
);

CREATE TABLE IF NOT EXISTS tax_documents (
  import_id INTEGER PRIMARY KEY,
  provider TEXT,
  tax_year INTEGER,
  reported_proceeds REAL,
  reported_basis REAL,
  reported_gain_loss REAL,
  parsed_proceeds REAL,
  parsed_basis REAL,
  parsed_gain_loss REAL,
  status TEXT NOT NULL DEFAULT 'unknown',
  detail TEXT,
  FOREIGN KEY(import_id) REFERENCES imports(id)
);

CREATE TABLE IF NOT EXISTS tax_record_issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  import_id INTEGER NOT NULL,
  provider TEXT,
  tax_year INTEGER,
  reason TEXT NOT NULL,
  raw_text TEXT,
  FOREIGN KEY(import_id) REFERENCES imports(id)
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS ai_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  role TEXT NOT NULL,
  mode TEXT,
  model TEXT,
  content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ai_messages_created ON ai_messages(id);
"""


def create_app() -> Flask:
    base_dir = Path(__file__).resolve().parent.parent
    app = Flask(__name__)
    app.secret_key = os.environ.get("CRYPTOHOUND_SECRET", "cryptohound-local-only-change-me")
    app.config["BASE_DIR"] = base_dir
    app.config["DATA_DIR"] = base_dir / "data"
    app.config["DB"] = app.config["DATA_DIR"] / "cryptohound.db"
    app.config["UPLOAD_DIR"] = app.config["DATA_DIR"] / "imports"
    app.config["BACKUP_DIR"] = base_dir / "backups"
    app.config["CREDENTIALS_FILE"] = base_dir / "credentials.json"
    app.config["REPORTS_DIR"] = base_dir / "reports"
    for p in (app.config["DATA_DIR"], app.config["UPLOAD_DIR"], app.config["BACKUP_DIR"], app.config["REPORTS_DIR"]):
        Path(p).mkdir(parents=True, exist_ok=True)
    init_db(app.config["DB"])
    migrate_tax_parser(app)
    migrate_ledger_parser(app)

    app.jinja_env.filters["units"] = format_units

    @app.context_processor
    def globals_():
        return {"version": VERSION, "app_name": APP_NAME}

    @app.get("/")
    def dashboard():
        conn = db(app)
        metrics = dashboard_metrics(conn)
        positions = current_positions(conn)
        liquidated = liquidated_summary(conn)
        recent = conn.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 8").fetchall()
        issues = detect_issues(conn)
        return render_template("dashboard.html", metrics=metrics, positions=positions,
                               liquidated=liquidated, recent=recent, issues=issues)

    @app.route("/imports", methods=["GET", "POST"])
    def imports():
        conn = db(app)
        if request.method == "POST":
            files = request.files.getlist("files")
            provider = (request.form.get("provider") or "auto").strip()
            if not files or not files[0].filename:
                flash("Choose at least one file.", "error")
                return redirect(url_for("imports"))
            results = []
            for f in files:
                try:
                    result = process_upload(app, conn, f, provider)
                    results.append(result)
                except Exception as e:
                    results.append(f"{f.filename}: ERROR - {e}")
            conn.commit()
            flash(" | ".join(results), "info")
            return redirect(url_for("imports"))
        rows = conn.execute("SELECT * FROM imports ORDER BY id DESC").fetchall()
        return render_template("imports.html", imports=rows)

    @app.post("/imports/<int:import_id>/purge")
    def purge_import(import_id):
        """Permanently remove one imported evidence artifact and every row derived from it."""
        conn = db(app)
        row = conn.execute("SELECT * FROM imports WHERE id=?", (import_id,)).fetchone()
        if not row:
            flash(f"Evidence import #{import_id} was not found.", "error")
            return redirect(url_for("imports"))

        counts = {
            "transactions": conn.execute("SELECT COUNT(*) FROM transactions WHERE import_id=?", (import_id,)).fetchone()[0],
            "tax_records": conn.execute("SELECT COUNT(*) FROM tax_records WHERE import_id=?", (import_id,)).fetchone()[0],
            "tax_issues": conn.execute("SELECT COUNT(*) FROM tax_record_issues WHERE import_id=?", (import_id,)).fetchone()[0],
        }
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM tax_record_issues WHERE import_id=?", (import_id,))
            conn.execute("DELETE FROM tax_documents WHERE import_id=?", (import_id,))
            conn.execute("DELETE FROM tax_records WHERE import_id=?", (import_id,))
            conn.execute("DELETE FROM transactions WHERE import_id=?", (import_id,))
            conn.execute("DELETE FROM imports WHERE id=?", (import_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Source evidence is stored with the import id prefix. Remove it only after
        # the database transaction succeeds, so a failed DB purge never destroys the original.
        leftovers = []
        for source in Path(app.config["UPLOAD_DIR"]).glob(f"{import_id:06d}-*"):
            try:
                source.unlink()
            except OSError as exc:
                leftovers.append(f"{source.name}: {exc}")

        msg = (f"Purged #{import_id} {row['filename']}: {counts['transactions']} ledger event(s), "
               f"{counts['tax_records']} tax record(s), {counts['tax_issues']} issue row(s), and source evidence removed.")
        if leftovers:
            msg += " WARNING: source cleanup failed for " + "; ".join(leftovers)
            flash(msg, "error")
        else:
            flash(msg, "info")
        return redirect(url_for("imports"))

    @app.get("/ledger")
    def ledger():
        conn = db(app)

        # v1.0.16: server-side Canonical Ledger filters.  Keep the query
        # parameterized so filter values can never become SQL.
        date_from = (request.args.get("date_from") or "").strip()
        date_to = (request.args.get("date_to") or "").strip()
        provider = (request.args.get("provider") or "").strip()
        tx_type = (request.args.get("tx_type") or "").strip()
        asset = (request.args.get("asset") or "").strip()

        clauses = []
        params: list[Any] = []
        if date_from:
            clauses.append("date(event_time) >= date(?)")
            params.append(date_from)
        if date_to:
            clauses.append("date(event_time) <= date(?)")
            params.append(date_to)
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if tx_type:
            clauses.append("tx_type = ?")
            params.append(tx_type)
        if asset:
            clauses.append("asset = ?")
            params.append(asset)

        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            "SELECT * FROM transactions" + where_sql +
            " ORDER BY event_time DESC, id DESC LIMIT 2000",
            params,
        ).fetchall()
        matching_count = conn.execute(
            "SELECT COUNT(*) FROM transactions" + where_sql, params
        ).fetchone()[0]

        # Filter menus are sourced from the complete ledger, not the current
        # result set, so users can freely move from one filter combination to
        # another without first clearing the form.
        providers = [r[0] for r in conn.execute(
            "SELECT DISTINCT provider FROM transactions "
            "WHERE provider IS NOT NULL AND trim(provider) <> '' ORDER BY provider"
        )]
        tx_types = [r[0] for r in conn.execute(
            "SELECT DISTINCT tx_type FROM transactions "
            "WHERE tx_type IS NOT NULL AND trim(tx_type) <> '' ORDER BY tx_type"
        )]
        assets = [r[0] for r in conn.execute(
            "SELECT DISTINCT asset FROM transactions "
            "WHERE asset IS NOT NULL AND trim(asset) <> '' ORDER BY asset"
        )]

        filters = {
            "date_from": date_from,
            "date_to": date_to,
            "provider": provider,
            "tx_type": tx_type,
            "asset": asset,
        }
        active_filter_count = sum(bool(v) for v in filters.values())
        return render_template(
            "ledger.html",
            rows=rows,
            providers=providers,
            tx_types=tx_types,
            assets=assets,
            filters=filters,
            active_filter_count=active_filter_count,
            matching_count=matching_count,
        )

    @app.get("/holdings")
    def holdings():
        conn = db(app)
        inventory = holdings_inventory(conn)
        return render_template("holdings.html", **inventory)

    @app.route("/ai", methods=["GET", "POST"])
    def ai_advisor():
        conn = db(app)
        settings = {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM settings")}
        default_model = settings.get("ai_model") or "gpt-5.6-luna"
        api_ready = bool(get_openai_api_key(app))

        if request.method == "POST":
            action = request.form.get("action", "ask")
            if action == "clear":
                conn.execute("DELETE FROM ai_messages")
                conn.commit()
                flash("AI Advisor conversation cleared. Evidence and accounting data were not changed.", "info")
                return redirect(url_for("ai_advisor"))

            question = (request.form.get("question") or "").strip()
            mode = (request.form.get("mode") or "general").strip()
            model = (request.form.get("model") or default_model).strip()
            allow_web = request.form.get("allow_web") == "1"
            if not question:
                flash("Give the Hound a question or mission.", "error")
                return redirect(url_for("ai_advisor"))
            if not api_ready:
                flash("OpenAI API key is not configured. Add it in Setup / Admin → AI Integration.", "error")
                return redirect(url_for("ai_advisor"))

            # Preserve the human request exactly. AI output is advisory and never writes ledger/tax truth.
            conn.execute("INSERT INTO ai_messages(created_at,role,mode,model,content) VALUES(?,?,?,?,?)",
                         (datetime.now(timezone.utc).isoformat(), "user", mode, model, question))
            conn.commit()
            try:
                context = build_ai_context(conn)
                history = [dict(r) for r in conn.execute(
                    "SELECT role,mode,model,content FROM ai_messages ORDER BY id DESC LIMIT 12").fetchall()][::-1]
                answer = call_openai_advisor(question, mode, model, allow_web, context, history, app)
                conn.execute("INSERT INTO ai_messages(created_at,role,mode,model,content) VALUES(?,?,?,?,?)",
                             (datetime.now(timezone.utc).isoformat(), "assistant", mode, model, answer))
                conn.commit()
            except Exception as e:
                flash(f"AI request failed: {e}", "error")
            return redirect(url_for("ai_advisor"))

        history_rows = conn.execute("SELECT * FROM ai_messages ORDER BY id DESC LIMIT 40").fetchall()[::-1]
        history = []
        for row in history_rows:
            item = dict(row)
            if item.get("role") == "assistant":
                item["content_html"] = render_ai_markdown(item.get("content") or "")
            history.append(item)
        context = build_ai_context(conn)
        reports = list_ai_reports(app)
        return render_template("ai.html", history=history, context=context, api_ready=api_ready,
                               default_model=default_model, reports=reports)

    @app.post("/ai/report/<int:message_id>/save")
    def save_ai_report(message_id: int):
        conn = db(app)
        try:
            artifacts = create_ai_report_artifacts(app, conn, message_id)
            flash(f"Report saved: {artifacts['pdf'].name}", "info")
        except Exception as e:
            flash(f"Report save failed: {e}", "error")
        return redirect(url_for("ai_advisor"))

    @app.post("/ai/report/<int:message_id>/export")
    def export_ai_report(message_id: int):
        conn = db(app)
        try:
            artifacts = create_ai_report_artifacts(app, conn, message_id)
            return send_file(artifacts["pdf"], mimetype="application/pdf", as_attachment=True, download_name=artifacts["pdf"].name)
        except Exception as e:
            flash(f"Report export failed: {e}", "error")
            return redirect(url_for("ai_advisor"))

    @app.get("/ai/report/file/<path:filename>")
    def download_ai_report(filename: str):
        root = Path(app.config["REPORTS_DIR"]).resolve()
        target = (root / filename).resolve()
        if root not in target.parents or not target.exists() or target.suffix.lower() not in {".pdf", ".json"}:
            return ("Report not found", 404)
        return send_file(target, as_attachment=True, download_name=target.name)

    @app.get("/tax")
    def tax():
        conn = db(app)
        requested_year = request.args.get("year")
        if requested_year:
            year = int(requested_year)
        else:
            latest = conn.execute("SELECT MAX(tax_year) FROM tax_records").fetchone()[0]
            year = int(latest or datetime.now().year)
        records = conn.execute("SELECT * FROM tax_records WHERE tax_year=? ORDER BY disposed_date, id", (year,)).fetchall()
        tx_summary = tax_summary(records)
        recon = reconcile_tax_to_ledger(conn, records)
        integrity = tax_integrity(conn, year, records)
        return render_template("tax.html", year=year, filing_year=year + 1, records=records, summary=tx_summary, recon=recon, integrity=integrity)

    @app.get("/export/csv")
    def export_csv():
        conn = db(app)
        year = int(request.args.get("year", datetime.now().year))
        rows = conn.execute("SELECT * FROM tax_records WHERE tax_year=? ORDER BY disposed_date", (year,)).fetchall()
        integrity = tax_integrity(conn, year, rows)
        if not integrity["export_ready"]:
            flash(f"Tax export blocked: {integrity['headline']}", "error")
            return redirect(url_for("tax", year=year))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Asset", "Units", "Date Acquired", "Date Sold", "Proceeds", "Cost Basis", "Gain/Loss", "Term", "Provider", "Basis Reported"])
        for r in rows:
            w.writerow([r["asset"], r["units"], r["acquired_date"], r["disposed_date"], r["proceeds"], r["cost_basis"], r["gain_loss"], r["term"], r["provider"], r["basis_reported"]])
        data = io.BytesIO(buf.getvalue().encode("utf-8"))
        return send_file(data, mimetype="text/csv", as_attachment=True, download_name=f"cryptohound-{year}-tax.csv")

    @app.get("/export/txf")
    def export_txf():
        conn = db(app)
        year = int(request.args.get("year", datetime.now().year))
        rows = conn.execute("SELECT * FROM tax_records WHERE tax_year=? ORDER BY disposed_date", (year,)).fetchall()
        integrity = tax_integrity(conn, year, rows)
        if not integrity["export_ready"]:
            flash(f"Tax export blocked: {integrity['headline']}", "error")
            return redirect(url_for("tax", year=year))
        out = ["V042", "ACryptoHound", f"D{year}1231", "^"]
        for r in rows:
            # TXF category 321 = capital gain/loss transaction in many desktop tax workflows.
            # Kept simple and clearly labeled experimental because importer support varies by product/year.
            out += ["TD", "N321", f"C1", f"L1", f"P{r['asset'] or 'Digital Asset'}",
                    f"D{_txf_date(r['acquired_date'])}", f"D{_txf_date(r['disposed_date'])}",
                    f"${r['proceeds'] or 0:.2f}", f"${r['cost_basis'] or 0:.2f}", "^"]
        data = io.BytesIO(("\r\n".join(out) + "\r\n").encode("ascii", errors="replace"))
        return send_file(data, mimetype="text/plain", as_attachment=True, download_name=f"cryptohound-{year}.txf")

    @app.route("/admin", methods=["GET", "POST"])
    def admin():
        conn = db(app)
        if request.method == "POST" and request.form.get("action") == "save_settings":
            for key in ("ai_provider", "ai_model", "ai_endpoint", "tax_profile"):
                set_setting(conn, key, request.form.get(key, ""))
            api_key = (request.form.get("openai_api_key") or "").strip()
            if api_key:
                save_openai_api_key(app, api_key)
            conn.commit()
            flash("Settings saved." + (" OpenAI API key stored in credentials.json." if api_key else ""), "info")
            return redirect(url_for("admin"))
        settings = {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM settings")}
        stats = {
            "imports": conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0],
            "transactions": conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
            "tax_records": conn.execute("SELECT COUNT(*) FROM tax_records").fetchone()[0],
            "tax_issues": conn.execute("SELECT COUNT(*) FROM tax_record_issues").fetchone()[0],
            "tax_documents": conn.execute("SELECT COUNT(*) FROM tax_documents").fetchone()[0],
            "db_size": human_bytes(Path(app.config["DB"]).stat().st_size if Path(app.config["DB"]).exists() else 0),
        }
        return render_template("admin.html", settings=settings, stats=stats,
                               openai_api_ready=bool(get_openai_api_key(app)))

    @app.post("/admin/update")
    def update_zip():
        f = request.files.get("update_zip")
        if not f or not f.filename:
            flash("Choose an update ZIP.", "error")
            return redirect(url_for("admin"))
        try:
            msg = apply_update_zip(app, f)
            flash(msg, "info")
        except Exception as e:
            flash(f"Update failed: {e}", "error")
        return redirect(url_for("admin"))

    @app.post("/admin/backup")
    def backup_db():
        src = Path(app.config["DB"])
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = Path(app.config["BACKUP_DIR"]) / f"cryptohound-{stamp}.db"
        shutil.copy2(src, dst)
        flash(f"Database backup created: {dst.name}", "info")
        return redirect(url_for("admin"))

    @app.get("/api/ai/context")
    def ai_context():
        conn = db(app)
        return jsonify(build_ai_context(conn))

    return app


def build_ai_context(conn: sqlite3.Connection) -> dict[str, Any]:
    """Curated read-only evidence packet for AI analysis; deterministic records remain authoritative."""
    inventory = holdings_inventory(conn)
    latest_year = conn.execute("SELECT MAX(tax_year) FROM tax_records").fetchone()[0]
    tax_rows = []
    tax_integrity_payload = None
    if latest_year:
        rows = conn.execute("SELECT * FROM tax_records WHERE tax_year=? ORDER BY disposed_date,id", (latest_year,)).fetchall()
        tax_rows = [dict(r) for r in rows[:250]]
        tax_integrity_payload = tax_integrity(conn, latest_year, rows)
    docs = [dict(r) for r in conn.execute("SELECT * FROM tax_documents ORDER BY tax_year DESC,provider").fetchall()]
    recent_tx = [dict(r) for r in conn.execute(
        "SELECT id,import_id,event_time,provider,account,asset,quantity,fiat_value,fee_usd,tx_type,origin,destination,status,confidence,review_required "
        "FROM transactions ORDER BY event_time DESC,id DESC LIMIT 300").fetchall()]
    imports = [dict(r) for r in conn.execute(
        "SELECT id,filename,sha256,imported_at,source_type,provider,status,notes FROM imports ORDER BY id DESC LIMIT 100").fetchall()]
    tax_profile_row = conn.execute("SELECT value FROM settings WHERE key='tax_profile'").fetchone()
    tax_profile = tax_profile_row[0] if tax_profile_row else ""
    return json_safe({
        "app": APP_NAME,
        "version": VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "advisor_profile_notes": tax_profile,
        "guardrails": [
            "AI is advisory and read-only.",
            "Deterministic CryptoHound ledger, holdings, basis, reconciliation and tax calculations are authoritative.",
            "Never invent missing basis, dates, quantities, prices, tax treatment, or transaction relationships.",
            "Clearly distinguish evidence-backed facts from inference and current-web research.",
            "Never claim that an AI suggestion changed CryptoHound records; it cannot."
        ],
        "dashboard_metrics": dashboard_metrics(conn),
        "holdings_summary": inventory.get("summary"),
        "holdings_rollups": inventory.get("rollups"),
        "open_lots": inventory.get("lots"),
        "holdings_evidence_warnings": inventory.get("warnings"),
        "general_evidence_issues": detect_issues(conn),
        "tax_year": latest_year,
        "tax_integrity": tax_integrity_payload,
        "tax_documents": docs,
        "tax_records_sample": tax_rows,
        "recent_transactions": recent_tx,
        "imports": imports,
    })


def json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, sqlite3.Row):
        return {k: json_safe(value[k]) for k in value.keys()}
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def call_openai_advisor(question: str, mode: str, model: str, allow_web: bool,
                        evidence_context: dict[str, Any], history: list[dict[str, Any]], app: Flask) -> str:
    api_key = get_openai_api_key(app)
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    mode_guidance = {
        "general": "Act as a full-service crypto financial analyst spanning evidence, accounting, tax, portfolio analysis, investing, and research.",
        "tax": "Focus on digital-asset accounting, basis, lots, dispositions, tax-document reconciliation, tax readiness, and evidence gaps. Do not present yourself as the user's CPA or attorney.",
        "portfolio": "Focus on holdings, concentration, cost basis, allocation, risk, performance, scenario analysis, and portfolio construction. State assumptions and downside risks.",
        "investing": "Focus on investment research: bull/bear thesis, catalysts, valuation/market structure where relevant, risk, sizing considerations, and what evidence would change the thesis. Avoid certainty or guaranteed-return language.",
        "evidence": "Act as an evidence investigator. Find candidate relationships, missing records, suspicious classifications, transfer matches, chronology conflicts, and the highest-value next evidence to collect. Never silently resolve uncertainty.",
        "reconcile": "Focus on broker/document/ledger reconciliation. Explain variances and propose evidence-backed next steps without altering source records.",
    }.get(mode, "Provide broad crypto financial analysis while preserving evidence discipline.")

    compact_history = []
    for h in history[-10:]:
        compact_history.append({"role": h.get("role", "user"), "content": h.get("content", "")})

    instructions = f"""You are CryptoHound AI Advisor, a read-only crypto financial intelligence analyst.
{mode_guidance}

Operating doctrine:
- Use the supplied CryptoHound evidence packet as the authoritative source for the user's imported records.
- Deterministic CryptoHound calculations outrank your own arithmetic or inference.
- You may explain, analyze, compare, investigate, suggest, and recommend; you may NOT claim to modify ledger, holdings, basis, lots, tax records, or evidence.
- Label uncertainty. Never fabricate basis, acquisition dates, transfer matches, wallet ownership, prices, tax forms, or transaction facts.
- For current market/regulatory/news claims, use web search only when it is enabled for this request and cite/link sources in the answer when the API provides them. If web search is not enabled, say current facts may be stale.
- For tax/accounting questions, distinguish general informational analysis from advice that should be confirmed with a qualified tax professional when material or ambiguous.
- For investing questions, provide decision support rather than promises; include meaningful downside/risk factors.
- Be concise but substantive. Prefer a SITREP style when useful.

CryptoHound evidence packet:
{json.dumps(evidence_context, separators=(',', ':'), ensure_ascii=False)}
"""

    if compact_history and compact_history[-1].get("role") == "user" and compact_history[-1].get("content") == question:
        user_input = compact_history
    else:
        user_input = compact_history + [{"role": "user", "content": question}]
    payload: dict[str, Any] = {
        "model": model or "gpt-5.6-luna",
        "instructions": instructions,
        "input": user_input,
        "reasoning": {"effort": "medium"},
        "max_output_tokens": 5000,
    }
    if allow_web:
        payload["tools"] = [{"type": "web_search"}]

    req = urlrequest.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:1200]
        raise RuntimeError(f"OpenAI API HTTP {e.code}: {detail}") from e
    except URLError as e:
        raise RuntimeError(f"OpenAI API connection error: {e.reason}") from e

    if data.get("output_text"):
        return data["output_text"].strip()
    chunks = []
    for item in data.get("output", []):
        for content in item.get("content", []) if isinstance(item, dict) else []:
            if isinstance(content, dict) and content.get("type") in ("output_text", "text") and content.get("text"):
                chunks.append(content["text"])
    if chunks:
        return "\n".join(chunks).strip()
    raise RuntimeError("OpenAI returned no readable text output")


def init_db(path: Path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.close()


def db(app: Flask):
    conn = sqlite3.connect(app.config["DB"])
    conn.row_factory = sqlite3.Row
    return conn


def process_upload(app: Flask, conn: sqlite3.Connection, storage, provider_hint: str) -> str:
    name = secure_filename(storage.filename)
    raw = storage.read()
    digest = hashlib.sha256(raw).hexdigest()
    existing = conn.execute("SELECT id FROM imports WHERE sha256=?", (digest,)).fetchone()
    if existing:
        return f"{name}: duplicate skipped"
    ext = Path(name).suffix.lower()
    provider = detect_provider(name, raw, provider_hint)
    cur = conn.execute("INSERT INTO imports(filename,sha256,imported_at,source_type,provider) VALUES(?,?,?,?,?)",
                       (name, digest, datetime.now(timezone.utc).isoformat(), ext.lstrip('.'), provider))
    import_id = cur.lastrowid
    saved = Path(app.config["UPLOAD_DIR"]) / f"{import_id:06d}-{name}"
    saved.write_bytes(raw)

    tx_count = tax_count = 0
    if ext == ".csv":
        tx_count = import_csv(conn, import_id, raw, provider)
    elif ext in (".xlsx", ".xls"):
        tx_count = import_excel(conn, import_id, raw, provider)
    elif ext == ".pdf":
        tax_count = import_pdf(conn, import_id, raw, provider)
    elif ext in (".md", ".txt"):
        tx_count = import_text(conn, import_id, raw, provider)
    else:
        conn.execute("UPDATE imports SET status='review', notes=? WHERE id=?", (f"Unsupported parser for {ext}; source preserved.", import_id))
    return f"{name}: {tx_count} ledger events, {tax_count} tax records"


def detect_provider(name: str, raw: bytes, hint: str) -> str:
    if hint and hint.lower() != "auto":
        return hint.lower()
    hay = (name + " " + raw[:5000].decode("utf-8", errors="ignore")).lower()
    for p in ("uphold", "coinbase", "robinhood", "sofi", "etrade", "e*trade"):
        if p in hay:
            return "etrade" if p == "e*trade" else p
    return "generic"


def import_csv(conn, import_id: int, raw: bytes, provider: str) -> int:
    # Coinbase transaction exports prepend a human-readable title/user block before
    # the actual CSV header. Locate the real header instead of assuming row 1.
    if provider == "coinbase":
        df = read_coinbase_csv(raw)
    else:
        df = pd.read_csv(io.BytesIO(raw))
    return normalize_dataframe(conn, import_id, df, provider)


def read_coinbase_csv(raw: bytes) -> pd.DataFrame:
    text = raw.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    required = {"id", "timestamp", "transaction type", "asset", "quantity transacted"}
    header_index = None
    for i, line in enumerate(lines[:50]):
        try:
            fields = next(csv.reader([line]))
        except Exception:
            continue
        lowered = {str(x).strip().lower() for x in fields}
        if required.issubset(lowered):
            header_index = i
            break
    if header_index is None:
        raise ValueError("Coinbase transaction header not found; source preserved for review.")
    return pd.read_csv(io.StringIO("\n".join(lines[header_index:])))


def import_excel(conn, import_id: int, raw: bytes, provider: str) -> int:
    sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None)
    count = 0
    for _, df in sheets.items():
        count += normalize_dataframe(conn, import_id, df, provider)
    return count


def normalize_dataframe(conn, import_id: int, df: pd.DataFrame, provider: str) -> int:
    cols = {str(c).strip().lower(): c for c in df.columns}
    count = 0

    if provider == "coinbase" and "quantity transacted" in cols and "transaction type" in cols:
        return normalize_coinbase(conn, import_id, df, cols)

    if provider == "uphold" and "destination currency" in cols and "origin currency" in cols:
        for _, row in df.iterrows():
            rec = {str(c): clean_value(row[c]) for c in df.columns}
            n = normalize_uphold_record(rec)
            conn.execute("""INSERT INTO transactions(import_id,external_id,event_time,provider,asset,quantity,fiat_value,fiat_currency,fee_asset,fee_quantity,fee_usd,tx_type,origin,destination,status,raw_json,confidence,review_required)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (import_id, rec.get("Id"), n["event_time"], provider, n["asset"], n["quantity"], n["fiat_value"],
                          n["fiat_currency"], n["fee_asset"], n["fee_quantity"], n["fee_usd"], n["tx_type"],
                          n["origin"], n["destination"], rec.get("Status"), json.dumps(rec), n["confidence"], n["review_required"]))
            count += 1
        return count

    # Generic broker adapter: map common column names; preserve all rows even when uncertain.
    date_col = first_col(cols, "date", "timestamp", "time", "transaction date", "activity date")
    asset_col = first_col(cols, "asset", "symbol", "currency", "ticker")
    qty_col = first_col(cols, "quantity", "qty", "amount", "units")
    type_col = first_col(cols, "type", "transaction type", "action", "activity")
    price_col = first_col(cols, "proceeds", "value", "total", "amount usd", "usd value")
    id_col = first_col(cols, "id", "transaction id", "txid", "reference")
    for _, row in df.iterrows():
        rec = {str(c): clean_value(row[c]) for c in df.columns}
        conn.execute("""INSERT INTO transactions(import_id,external_id,event_time,provider,asset,quantity,fiat_value,tx_type,status,raw_json,confidence,review_required)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (import_id, val(row, id_col), iso_date(val(row, date_col)), provider,
                      str(val(row, asset_col) or "").upper() or None, num(val(row, qty_col)), num(val(row, price_col)),
                      str(val(row, type_col) or "UNKNOWN").upper(), "imported", json.dumps(rec), 0.55, 1))
        count += 1
    return count



def normalize_coinbase(conn, import_id: int, df: pd.DataFrame, cols: dict) -> int:
    """Normalize Coinbase transaction-history CSV rows into CryptoHound's canonical ledger."""
    id_col = first_col(cols, "id")
    date_col = first_col(cols, "timestamp")
    type_col = first_col(cols, "transaction type")
    asset_col = first_col(cols, "asset")
    qty_col = first_col(cols, "quantity transacted")
    total_col = first_col(cols, "total (inclusive of fees and/or spread)", "total")
    subtotal_col = first_col(cols, "subtotal")
    fee_col = first_col(cols, "fees and/or spread", "fees", "fee")
    notes_col = first_col(cols, "notes")
    sender_col = first_col(cols, "sender address")
    recipient_col = first_col(cols, "recipient address")

    recognized = {
        "buy": "BUY",
        "advanced trade buy": "BUY",
        "sell": "SELL",
        "advanced trade sell": "SELL",
        "send": "TRANSFER_OUT",
        "receive": "TRANSFER_IN",
        "staking income": "INCOME",
        "reward income": "INCOME",
        "learning reward": "INCOME",
        "retail staking transfer": "SELF_TRANSFER",
        "retail unstaking transfer": "SELF_TRANSFER",
    }
    count = 0
    review_count = 0
    for _, row in df.iterrows():
        rec = {str(c): clean_value(row[c]) for c in df.columns}
        raw_type = str(val(row, type_col) or "").strip()
        typ_key = raw_type.lower()
        asset = str(val(row, asset_col) or "").strip().upper() or None
        signed_qty = num(val(row, qty_col))
        qty = abs(signed_qty) if signed_qty is not None else None

        if typ_key == "convert":
            tx_type = "CONVERT_IN" if (signed_qty or 0) > 0 else "CONVERT_OUT" if (signed_qty or 0) < 0 else "UNKNOWN"
        elif typ_key == "withdrawal":
            # Coinbase cash withdrawals are preserved as ledger evidence but do not alter crypto holdings.
            tx_type = "FIAT_WITHDRAWAL" if asset in {"USD", "EUR", "GBP"} else "TRANSFER_OUT"
        else:
            tx_type = recognized.get(typ_key, "UNKNOWN")

        total = num(val(row, total_col))
        subtotal = num(val(row, subtotal_col))
        fiat_value = abs(total) if total is not None else abs(subtotal) if subtotal is not None else None
        fee = num(val(row, fee_col))
        fee_usd = abs(fee) if fee is not None else None
        sender = str(val(row, sender_col) or "").strip() or None
        recipient = str(val(row, recipient_col) or "").strip() or None

        origin = "coinbase"
        destination = "coinbase"
        if tx_type == "TRANSFER_OUT":
            destination = recipient or "external"
        elif tx_type == "TRANSFER_IN":
            origin = sender or "external"
        elif tx_type == "FIAT_WITHDRAWAL":
            destination = "external-bank"

        review = 1 if tx_type == "UNKNOWN" else 0
        if review:
            review_count += 1
        rec["_cryptohound_coinbase_type"] = tx_type
        rec["_cryptohound_signed_quantity"] = signed_qty

        conn.execute("""INSERT INTO transactions(import_id,external_id,event_time,provider,asset,quantity,fiat_value,fiat_currency,
                      fee_asset,fee_quantity,fee_usd,tx_type,origin,destination,status,raw_json,confidence,review_required)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (import_id, val(row, id_col), iso_date(val(row, date_col)), "coinbase", asset, qty, fiat_value, "USD",
                      "USD" if fee_usd not in (None, 0) else None, fee_usd, fee_usd, tx_type, origin, destination,
                      "imported", json.dumps(rec), 0.99 if not review else 0.60, review))
        count += 1

    note = f"Coinbase transaction CSV parsed: {count} ledger event(s)"
    if review_count:
        note += f"; {review_count} unknown event(s) require review"
    conn.execute("UPDATE imports SET status=?, notes=? WHERE id=?",
                 ("review" if review_count else "ok", note, import_id))
    return count


def import_text(conn, import_id: int, raw: bytes, provider: str) -> int:
    text = raw.decode("utf-8", errors="replace")
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    count = 0
    for line in lines:
        if any(ch.isdigit() for ch in line):
            conn.execute("INSERT INTO transactions(import_id,provider,tx_type,status,raw_json,confidence,review_required) VALUES(?,?,?,?,?,?,?)",
                         (import_id, provider, "TEXT_EVIDENCE", "review", json.dumps({"line": line}), 0.25, 1))
            count += 1
    return count


def import_pdf(conn, import_id: int, raw: bytes, provider: str) -> int:
    reader = PdfReader(io.BytesIO(raw))
    pages = [(p.extract_text() or "") for p in reader.pages]
    text = "\n".join(pages)
    year = infer_year(text)
    conn.execute("DELETE FROM tax_record_issues WHERE import_id=?", (import_id,))
    conn.execute("DELETE FROM tax_documents WHERE import_id=?", (import_id,))

    if provider == "coinbase":
        records, summary, issues = parse_coinbase_1099(pages, year)
    elif provider == "uphold":
        records, summary, issues = parse_uphold_1099(pages, year)
    else:
        conn.execute("UPDATE imports SET status='review', notes=? WHERE id=?", ("PDF preserved; generic tax-document parser requires review.", import_id))
        return 0

    count = 0
    for rec in records:
        reason = validate_tax_record(rec, year)
        if reason:
            issues.append((reason, rec.get("raw_text", "")))
            continue
        insert_tax(conn, import_id, provider, year, rec["asset"], rec.get("units"), rec.get("acquired_date"),
                   rec.get("disposed_date"), rec.get("proceeds"), rec.get("cost_basis"), rec.get("gain_loss"),
                   rec.get("term") or derive_term(rec.get("acquired_date"), rec.get("disposed_date")),
                   rec.get("basis_reported", 0), rec.get("source_note", f"Parsed from {provider} 1099-DA"), rec.get("raw_text", ""))
        count += 1

    for reason, raw_text in issues:
        conn.execute("INSERT INTO tax_record_issues(import_id,provider,tax_year,reason,raw_text) VALUES(?,?,?,?,?)",
                     (import_id, provider, year, reason, raw_text[:4000]))

    parsed = conn.execute("SELECT COALESCE(SUM(proceeds),0), COALESCE(SUM(cost_basis),0), COALESCE(SUM(gain_loss),0) FROM tax_records WHERE import_id=?", (import_id,)).fetchone()
    rp, rb, rg = summary
    pp, pb, pg = parsed
    status, detail = document_reconciliation(rp, rb, rg, pp, pb, pg, len(issues))
    conn.execute("""INSERT INTO tax_documents(import_id,provider,tax_year,reported_proceeds,reported_basis,reported_gain_loss,parsed_proceeds,parsed_basis,parsed_gain_loss,status,detail)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                 (import_id, provider, year, rp, rb, rg, pp, pb, pg, status, detail))
    conn.execute("UPDATE imports SET status=?, notes=? WHERE id=?", ("ok" if status == "reconciled" else "review", detail, import_id))
    return count


def parse_coinbase_1099(pages, year):
    import re
    records = []
    issues = []
    full = "\n".join(pages)
    sm = re.search(r"Total¹?\s*\$\s*([\d,.]+)\s*\$\s*([\d,.]+)\s*-?\$?\s*([\d,.]+)", full)
    summary = (None, None, None)
    if sm:
        summary = (num(sm.group(1)), num(sm.group(2)), -abs(num(sm.group(3)) or 0))
    else:
        issues.append(("Document summary totals not found", "Coinbase 1099-DA summary"))

    header_re = re.compile(r"^([A-Z][A-Z0-9]{1,9})\s*/\s*[A-Z0-9]+$")
    row_re = re.compile(r"^(\d{2}/\d{2}/\d{2})\s+([\d,\.]+)\s+([\d,\.]+)\s+(VARIOUS|\d{2}/\d{2}/\d{2})\s+([\d,\.]+)\s+(-?[\d,\.]+)$")
    wrapped_start = re.compile(r"^(\d{2}/\d{2}/\d{2})\s+([\d,\.]+)$")
    wrapped_tail = re.compile(r"^([\d,\.]+)\s+(VARIOUS|\d{2}/\d{2}/\d{2})\s+([\d,\.]+)\s+(-?[\d,\.]+)$")

    for page in pages:
        if "Transactions With Cost Basis" not in page:
            continue
        term = "long" if "Long Term Transactions" in page else "short"
        current_asset = None
        lines = [x.strip() for x in page.splitlines() if x.strip()]
        i = 0
        while i < len(lines):
            line = lines[i]
            hm = header_re.match(line)
            if hm:
                current_asset = hm.group(1)
                i += 1
                continue
            m = row_re.match(line)
            raw_line = line
            if not m and current_asset:
                ws = wrapped_start.match(line)
                if ws and i + 2 < len(lines) and re.fullmatch(r"\d+", lines[i+1]) and wrapped_tail.match(lines[i+2]):
                    # Coinbase can wrap the final digit(s) of very large unit quantities to a separate line.
                    rebuilt = f"{ws.group(1)} {ws.group(2)}{lines[i+1]} {lines[i+2]}"
                    m = row_re.match(rebuilt)
                    raw_line = " | ".join(lines[i:i+3])
                    i += 2
            if m and current_asset:
                sold, units, proceeds, acquired, basis, gl = m.groups()
                rec = {
                    "asset": current_asset,
                    "units": num(units),
                    "acquired_date": acquired,
                    "disposed_date": sold,
                    "proceeds": num(proceeds),
                    "cost_basis": num(basis),
                    "gain_loss": num(gl),
                    "term": term,
                    "basis_reported": 0,
                    "source_note": "Coinbase 1099-DA row",
                    "raw_text": raw_line,
                }
                records.append(rec)
            i += 1
    return records, summary, issues


def parse_uphold_1099(pages, year):
    import re
    records = []
    issues = []
    full = "\n".join(pages)
    sm = re.search(r"Short-term transactions for which basis is not reported to the IRS\s+([\d,.]+)\s+([\d,.]+)\s+\(([\d,.]+)\)", full, re.I)
    summary = (None, None, None)
    if sm:
        summary = (num(sm.group(1)), num(sm.group(2)), -abs(num(sm.group(3)) or 0))
    else:
        issues.append(("Document summary totals not found", "Uphold 1099-DA summary"))

    asset_header = re.compile(r"^.+?\(([A-Z][A-Z0-9]{1,9})\)\s+-\s+[A-Z0-9]+\s*$")
    for page in pages[:2]:
        lines = [x.strip() for x in page.splitlines() if x.strip()]
        current_asset = None
        i = 0
        while i < len(lines):
            hm = asset_header.match(lines[i])
            if hm:
                current_asset = hm.group(1)
                i += 1
                continue
            if current_asset and re.fullmatch(r"[\d.]+", lines[i]):
                # PDF extraction emits each table cell on its own line, interspersed with '-' placeholders.
                units = num(lines[i])
                if i + 4 < len(lines) and re.fullmatch(r"\d{2}/\d{2}/\d{4}", lines[i+1]) and re.fullmatch(r"\d{2}/\d{2}/\d{4}", lines[i+2]):
                    acq, disp = lines[i+1], lines[i+2]
                    vals = lines[i+3].split()
                    if len(vals) >= 2 and num(vals[0]) is not None and num(vals[1]) is not None:
                        proceeds, basis = num(vals[0]), num(vals[1])
                        gl = round((proceeds or 0) - (basis or 0), 2)
                        records.append({
                            "asset": current_asset, "units": units, "acquired_date": acq, "disposed_date": disp,
                            "proceeds": proceeds, "cost_basis": basis, "gain_loss": gl, "term": derive_term(acq, disp),
                            "basis_reported": 0, "source_note": "Uphold 1099-DA row",
                            "raw_text": " | ".join(lines[i:i+4]),
                        })
                        i += 3
            i += 1
    return records, summary, issues


def validate_tax_record(rec, year):
    import re
    asset = str(rec.get("asset") or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]{1,9}", asset):
        return f"Invalid asset token quarantined: {asset or '?'}"
    disposed = parse_tax_date(rec.get("disposed_date"))
    acquired = parse_tax_date(rec.get("acquired_date"))
    if disposed and disposed.year != year:
        return f"Disposition year {disposed.year} does not match tax year {year}"
    if acquired and disposed and acquired.date() > disposed.date():
        return f"Acquisition date {rec.get('acquired_date')} occurs after disposition {rec.get('disposed_date')}"
    p, b, g = rec.get("proceeds"), rec.get("cost_basis"), rec.get("gain_loss")
    if p is not None and b is not None and g is not None and abs((p - b) - g) > 0.02:
        return f"Accounting invariant failed: proceeds-basis={(p-b):.2f}, reported P&L={g:.2f}"
    return None


def derive_term(acquired, disposed):
    a, d = parse_tax_date(acquired), parse_tax_date(disposed)
    if not a or not d:
        return "unknown"
    return "long" if (d.date() - a.date()).days > 365 else "short"


def parse_tax_date(v):
    if not v or str(v).upper() == "VARIOUS":
        return None
    try:
        return dtparser.parse(str(v))
    except Exception:
        return None


def document_reconciliation(rp, rb, rg, pp, pb, pg, issue_count):
    if None in (rp, rb, rg):
        return "review", "Reported document totals unavailable; tax export blocked."
    variance = max(abs((rp or 0)-(pp or 0)), abs((rb or 0)-(pb or 0)), abs((rg or 0)-(pg or 0)))
    if variance <= 0.02 and issue_count == 0:
        return "reconciled", "Parsed row totals reconcile to broker-reported document totals."
    return "review", f"Document reconciliation failed; max variance ${variance:.2f}; {issue_count} quarantined parser issue(s)."


def insert_tax(conn, import_id, provider, year, asset, units, acquired, disposed, proceeds, basis, gl, term, basis_reported, note, raw):
    conn.execute("""INSERT INTO tax_records(import_id,provider,tax_year,asset,units,acquired_date,disposed_date,proceeds,cost_basis,gain_loss,term,basis_reported,source_note,raw_text)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (import_id, provider, year, asset, units, acquired, disposed, proceeds, basis, gl, term, basis_reported, note, raw))

def classify_uphold(typ, origin, dest, o_cur, d_cur):
    """Classify Uphold rows by economic meaning, not by the provider's generic verb.

    Uphold uses ``transfer`` for both actual custody moves and asset conversions.
    CryptoHound therefore evaluates the currencies and endpoints together.  Fiat
    movements are kept as ledger evidence but are never treated as crypto lots.
    """
    typ = (typ or "").strip().lower()
    origin = (origin or "").strip().lower()
    dest = (dest or "").strip().lower()
    o_cur = (o_cur or "").upper()
    d_cur = (d_cur or "").upper()

    # Card funding directly into a crypto asset is an acquisition. Card funding
    # into USD is cash funding evidence, not an investment lot.
    if typ == "in" and origin == "credit-card":
        return "FIAT_DEPOSIT" if d_cur in FIAT_ASSETS else "BUY"

    # Asset returning from a blockchain/self-custody location to Uphold.
    if typ == "in" and dest == "uphold" and o_cur == d_cur and o_cur not in FIAT_ASSETS:
        return "TRANSFER_IN"

    # Uphold internal conversions.  Crypto -> fiat is a sale; fiat -> crypto is
    # a buy; crypto -> crypto is a taxable conversion with two asset legs.
    if typ == "transfer" and origin == "uphold" and dest == "uphold":
        if o_cur in FIAT_ASSETS and d_cur not in FIAT_ASSETS:
            return "BUY"
        if o_cur not in FIAT_ASSETS and d_cur in FIAT_ASSETS:
            return "SELL"
        if o_cur not in FIAT_ASSETS and d_cur not in FIAT_ASSETS and o_cur != d_cur:
            return "CRYPTO_TO_CRYPTO"
        if o_cur in FIAT_ASSETS and d_cur in FIAT_ASSETS:
            return "FIAT_TRANSFER"
        return "SELF_TRANSFER"

    # Anything leaving Uphold in fiat is a cash withdrawal, not a transfer of a
    # tax lot.  Same-asset crypto leaving Uphold is a custody transfer.
    if typ == "out" and origin == "uphold":
        if o_cur in FIAT_ASSETS:
            return "FIAT_WITHDRAWAL"
        if o_cur == d_cur or not d_cur:
            return "TRANSFER_OUT"

    # Generic same-asset crypto movements are custody transfers.
    if typ == "out" and o_cur == d_cur and o_cur not in FIAT_ASSETS:
        return "TRANSFER_OUT"
    if typ == "in" and o_cur == d_cur and o_cur not in FIAT_ASSETS:
        return "TRANSFER_IN"

    return "TRANSFER_OR_SWAP"


def choose_asset_value(o_cur, d_cur, o_amt, d_amt):
    if d_cur and d_cur not in FIAT_ASSETS:
        return d_cur, d_amt, o_amt if o_cur == "USD" else None
    if o_cur and o_cur not in FIAT_ASSETS:
        return o_cur, o_amt, d_amt if d_cur == "USD" else None
    return d_cur or o_cur or None, d_amt or o_amt, d_amt if d_cur == "USD" else o_amt if o_cur == "USD" else None


def normalize_uphold_record(rec):
    """Return canonical fields for one preserved Uphold CSV row.

    The raw row remains untouched in ``raw_json``.  This function only produces
    derived ledger fields, which makes parser migration safe and repeatable.
    """
    typ = str(rec.get("Type") or "").strip().lower()
    origin = str(rec.get("Origin") or "").strip()
    dest = str(rec.get("Destination") or "").strip()
    o_cur = str(rec.get("Origin Currency") or "").strip().upper()
    d_cur = str(rec.get("Destination Currency") or "").strip().upper()
    o_amt = num(rec.get("Origin Amount"))
    d_amt = num(rec.get("Destination Amount"))
    fee_amt = num(rec.get("Fee Amount"))
    fee_cur = str(rec.get("Fee Currency") or "").strip().upper() or None
    tx_type = classify_uphold(typ, origin, dest, o_cur, d_cur)
    asset, qty, fiat = choose_asset_value(o_cur, d_cur, o_amt, d_amt)

    # For fiat-only rows preserve the cash movement in the ledger.  Holdings and
    # tax-lot reconstruction explicitly ignore fiat assets.
    fiat_currency = "USD" if "USD" in (o_cur, d_cur) else (d_cur or o_cur or "USD")
    review = 1 if tx_type in ("TRANSFER_OR_SWAP", "UNKNOWN") else 0
    return {
        "event_time": iso_date(rec.get("Date")),
        "asset": asset,
        "quantity": qty,
        "fiat_value": fiat,
        "fiat_currency": fiat_currency,
        "fee_asset": fee_cur,
        "fee_quantity": fee_amt,
        "fee_usd": fee_amt if fee_cur == "USD" else None,
        "tx_type": tx_type,
        "origin": origin,
        "destination": dest,
        "confidence": 0.99 if not review else 0.70,
        "review_required": review,
    }


def holdings_inventory(conn):
    """Build a deterministic FIFO view of open crypto lots from the canonical ledger.

    This is a holdings/evidence view, not a tax-return engine. BUY creates inventory;
    SELL consumes FIFO inventory; known self-custody transfers move inventory without
    treating it as disposed. Unmatched inbound transfers remain holdings but carry an
    explicit unknown-basis flag so CryptoHound never invents basis.
    """
    rows = conn.execute("""
        SELECT t.*, i.filename AS source_filename
        FROM transactions t
        JOIN imports i ON i.id=t.import_id
        WHERE t.asset IS NOT NULL
        ORDER BY COALESCE(t.event_time,''), t.id
    """).fetchall()
    lots = []
    warnings = []

    def dt_key(v):
        try:
            return dtparser.parse(str(v)).timestamp() if v else 0
        except Exception:
            return 0

    def new_lot(asset, qty, acquired, basis, provider, location, row, basis_status="known", note=""):
        qty = abs(D(qty))
        if qty <= 0:
            return
        basis_d = None if basis is None else abs(D(basis))
        lots.append({
            "asset": (asset or "").upper(), "quantity": qty, "original_quantity": qty,
            "acquired_date": acquired, "original_basis": basis_d, "remaining_basis": basis_d,
            "provider": provider or "unknown", "location": location or provider or "unknown",
            "import_id": row["import_id"], "source_filename": row["source_filename"],
            "external_id": row["external_id"], "confidence": row["confidence"],
            "review_required": bool(row["review_required"]), "basis_status": basis_status,
            "observed_value": None if row["fiat_value"] is None else abs(D(row["fiat_value"])),
            "basis_evidence": [], "basis_missing_reason": "" if basis_d is not None else "Authoritative acquisition basis not established by imported evidence",
            "note": note,
        })

    def split_move(asset, qty, origin, destination, row):
        """Move FIFO units between custody locations without changing global ownership."""
        remaining = abs(D(qty))
        candidates = [x for x in lots if x["asset"] == asset and x["quantity"] > 0]
        if origin:
            exact = [x for x in candidates if (x["location"] or "").lower() == origin.lower()]
            others = [x for x in candidates if x not in exact]
            candidates = exact + others
        candidates.sort(key=lambda x: (dt_key(x["acquired_date"]), x["import_id"]))
        for lot in candidates:
            if remaining <= 0: break
            take = min(lot["quantity"], remaining)
            if take <= 0: continue
            if take == lot["quantity"]:
                lot["location"] = destination or lot["location"]
            else:
                ratio = take / lot["quantity"]
                moved_basis = None if lot["remaining_basis"] is None else lot["remaining_basis"] * ratio
                lot["quantity"] -= take
                if lot["remaining_basis"] is not None:
                    lot["remaining_basis"] -= moved_basis
                moved = lot.copy()
                moved["quantity"] = take
                moved["original_quantity"] = take
                moved["original_basis"] = moved_basis
                moved["remaining_basis"] = moved_basis
                moved["location"] = destination or lot["location"]
                moved["note"] = (moved.get("note") + "; custody transfer split").strip("; ")
                lots.append(moved)
            remaining -= take
        return remaining

    def consume(asset, qty, location_hint=None, capture=False):
        """Consume FIFO inventory. When capture=True, return the evidence consumed too.

        Captured basis is evidence about the disposed/source lots. It is deliberately
        NOT promoted to the replacement asset's tax basis unless the ledger contains
        independent USD/FMV evidence for that acquisition.
        """
        remaining = abs(D(qty))
        consumed = []
        candidates = [x for x in lots if x["asset"] == asset and x["quantity"] > 0]
        if location_hint:
            exact = [x for x in candidates if (x["location"] or "").lower() == location_hint.lower()]
            candidates = exact + [x for x in candidates if x not in exact]
        candidates.sort(key=lambda x: (dt_key(x["acquired_date"]), x["import_id"]))
        for lot in candidates:
            if remaining <= 0: break
            take = min(lot["quantity"], remaining)
            if take <= 0: continue
            ratio = take / lot["quantity"]
            basis_used = None if lot["remaining_basis"] is None else lot["remaining_basis"] * ratio
            if capture:
                consumed.append({
                    "asset": lot["asset"], "quantity": take, "basis_used": basis_used,
                    "acquired_date": lot["acquired_date"], "provider": lot["provider"],
                    "source_filename": lot["source_filename"], "import_id": lot["import_id"],
                    "basis_status": lot["basis_status"],
                })
            lot["quantity"] -= take
            if lot["remaining_basis"] is not None:
                lot["remaining_basis"] -= basis_used
            remaining -= take
        return (remaining, consumed) if capture else remaining

    for r in rows:
        asset = (r["asset"] or "").upper()
        qty = abs(D(r["quantity"] or 0))
        if not asset or qty <= 0:
            continue
        # CryptoHound Holdings is a digital-asset inventory view. Fiat deposits,
        # withdrawals and cash proceeds remain in the canonical ledger but must
        # never become open tax lots or basis gaps.
        if asset in FIAT_ASSETS:
            continue
        typ = (r["tx_type"] or "").upper()
        provider = r["provider"] or "unknown"
        origin = r["origin"] or provider
        dest = r["destination"] or provider

        if typ in ("BUY", "INCOME", "CONVERT_IN"):
            # Coinbase income and positive conversion legs create inventory just like acquisitions.
            # Broker fiat amount is evidence of basis/FMV when present; CryptoHound never invents it.
            basis = r["fiat_value"] if r["fiat_value"] is not None else None
            label = {"BUY": "BUY from canonical ledger", "INCOME": "Income/reward acquisition",
                     "CONVERT_IN": "Crypto conversion acquisition"}[typ]
            new_lot(asset, qty, r["event_time"], basis, provider, dest, r,
                    "known" if basis is not None else "unknown", label)
        elif typ in ("SELL", "CONVERT_OUT"):
            left = consume(asset, qty, origin)
            if left > Decimal("0.000000000001"):
                warnings.append(f"{asset}: sale exceeds reconstructed inventory by {format_units(left)} units.")
        elif typ == "TRANSFER_OUT":
            left = split_move(asset, qty, origin, dest, r)
            if left > Decimal("0.000000000001"):
                # Preserve ownership evidence even when the acquisition record predates imported history.
                new_lot(asset, left, r["event_time"], None, provider, dest, r, "unknown",
                        "Outbound transfer with no prior acquisition in imported evidence")
                warnings.append(f"{asset}: {format_units(left)} transferred out without a matching prior acquisition; basis remains unknown.")
        elif typ == "TRANSFER_IN":
            left = split_move(asset, qty, origin, dest, r)
            if left > Decimal("0.000000000001"):
                new_lot(asset, left, r["event_time"], None, provider, dest, r, "unknown",
                        "Inbound transfer; acquisition/basis not present in imported evidence. Any broker fiat value is retained as observed-value evidence, not asserted as basis.")
        elif typ == "SELF_TRANSFER":
            split_move(asset, qty, origin, dest, r)
        elif typ == "CRYPTO_TO_CRYPTO":
            # Uphold's normalized row represents the destination asset. Recover the source
            # asset/quantity from preserved raw evidence so inventory stays directionally sane.
            try:
                raw = json.loads(r["raw_json"] or "{}")
            except Exception:
                raw = {}
            src_asset = str(raw.get("Origin Currency") or "").upper()
            src_qty = D(raw.get("Origin Amount") or 0)
            consumed_evidence = []
            if src_asset and src_asset not in FIAT_ASSETS and src_qty > 0:
                left, consumed_evidence = consume(src_asset, src_qty, origin, capture=True)
                if left > Decimal("0.000000000001"):
                    warnings.append(f"{src_asset}: crypto-to-crypto swap exceeds reconstructed inventory by {format_units(left)} units.")
            basis = r["fiat_value"] if r["fiat_value"] is not None else None
            new_lot(asset, qty, r["event_time"], basis, provider, dest, r,
                    "known" if basis is not None else "partial" if consumed_evidence else "unknown",
                    "Crypto-to-crypto acquisition; source-lot basis preserved below; acquisition FMV/basis still requires valuation evidence" if basis is None else "Crypto-to-crypto acquisition")
            created = lots[-1] if lots and lots[-1]["asset"] == asset else None
            if created is not None:
                created["basis_evidence"] = consumed_evidence
                known_src = sum((x["basis_used"] for x in consumed_evidence if x["basis_used"] is not None), Decimal("0"))
                unknown_src = sum(1 for x in consumed_evidence if x["basis_used"] is None)
                created["source_basis_known"] = known_src
                created["source_basis_unknown_lots"] = unknown_src
                if basis is None:
                    created["basis_missing_reason"] = "Replacement-asset USD/FMV basis not present; source-lot basis is preserved as evidence but is not substituted for FMV."
        elif typ == "TRANSFER_OR_SWAP":
            warnings.append(f"{asset}: ambiguous TRANSFER_OR_SWAP event excluded from automatic holdings math pending review.")
        # UNKNOWN/TEXT_EVIDENCE intentionally do not change inventory.

        # Fees paid in the same crypto reduce inventory; fiat fees do not change unit count.
        fee_asset = (r["fee_asset"] or "").upper()
        fee_qty = D(r["fee_quantity"] or 0)
        if fee_asset == asset and fee_qty > 0:
            consume(asset, fee_qty, origin)

    open_lots = [x for x in lots if x["quantity"] > Decimal("0.000000000001")]
    today = datetime.now(timezone.utc)
    for lot in open_lots:
        lot.setdefault("source_basis_known", Decimal("0"))
        lot.setdefault("source_basis_unknown_lots", 0)
        lot.setdefault("basis_evidence", [])
        if lot["remaining_basis"] is not None and lot["quantity"] > 0:
            lot["cost_per_unit"] = lot["remaining_basis"] / lot["quantity"]
        else:
            lot["cost_per_unit"] = None
        try:
            acquired = dtparser.parse(str(lot["acquired_date"]))
            if acquired.tzinfo is None: acquired = acquired.replace(tzinfo=timezone.utc)
            lot["holding_period"] = "LONG-TERM" if (today - acquired).days > 365 else "SHORT-TERM"
            lot["acquired_display"] = acquired.date().isoformat()
        except Exception:
            lot["holding_period"] = "UNKNOWN"
            lot["acquired_display"] = lot["acquired_date"] or "—"

    roll = {}
    for lot in open_lots:
        a = lot["asset"]
        r = roll.setdefault(a, {"asset": a, "quantity": Decimal("0"), "remaining_basis": Decimal("0"),
                                "basis_complete": True, "locations": set(), "lots": 0, "unknown_basis_lots": 0})
        r["quantity"] += lot["quantity"]
        r["lots"] += 1
        r["locations"].add(lot["location"] or "unknown")
        if lot["remaining_basis"] is None:
            r["basis_complete"] = False
            r["unknown_basis_lots"] += 1
        else:
            r["remaining_basis"] += lot["remaining_basis"]
    rollups = []
    for r in roll.values():
        r["locations"] = ", ".join(sorted(r["locations"]))
        r["cost_per_unit"] = (r["remaining_basis"] / r["quantity"]) if r["basis_complete"] and r["quantity"] > 0 else None
        rollups.append(r)
    rollups.sort(key=lambda x: x["asset"])
    open_lots.sort(key=lambda x: (x["asset"], dt_key(x["acquired_date"]), x["import_id"]))

    known_basis = sum((x["remaining_basis"] for x in open_lots if x["remaining_basis"] is not None), Decimal("0"))
    return {
        "rollups": rollups, "lots": open_lots, "warnings": sorted(set(warnings)),
        "summary": {"assets": len(rollups), "lots": len(open_lots), "known_basis": known_basis,
                    "basis_gaps": sum(1 for x in open_lots if x["remaining_basis"] is None)}
    }


def current_positions(conn):
    """Dashboard position rollup sourced from the same lot-aware holdings engine.

    This deliberately avoids maintaining a second, looser net-flow algorithm.
    Ambiguous rows stay in the review queue instead of changing dashboard balances.
    """
    inventory = holdings_inventory(conn)
    return [
        {
            "asset": r["asset"],
            "quantity": float(r["quantity"]),
            "locations": r["locations"],
        }
        for r in inventory["rollups"]
        if r["quantity"] > Decimal("0.000000000001")
    ]


def liquidated_summary(conn):
    rows = conn.execute("SELECT asset, SUM(proceeds) proceeds, SUM(cost_basis) basis, SUM(gain_loss) gl FROM tax_records GROUP BY asset ORDER BY asset").fetchall()
    return rows


def dashboard_metrics(conn):
    tax = conn.execute("SELECT COALESCE(SUM(proceeds),0), COALESCE(SUM(cost_basis),0), COALESCE(SUM(gain_loss),0) FROM tax_records").fetchone()
    return {
        "imports": conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0],
        "transactions": conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
        "tax_records": conn.execute("SELECT COUNT(*) FROM tax_records").fetchone()[0],
        "proceeds": tax[0], "basis": tax[1], "realized": tax[2],
        "review": conn.execute("SELECT COUNT(*) FROM transactions WHERE review_required=1").fetchone()[0],
    }


def detect_issues(conn):
    issues = []
    n = conn.execute("SELECT COUNT(*) FROM transactions WHERE review_required=1").fetchone()[0]
    if n: issues.append(f"{n} ledger events require classification/review.")
    n = conn.execute("SELECT COUNT(*) FROM tax_records WHERE cost_basis IS NULL").fetchone()[0]
    if n: issues.append(f"{n} tax records are missing cost basis.")
    n = conn.execute("SELECT COUNT(*) FROM tax_record_issues").fetchone()[0]
    if n: issues.append(f"{n} tax parser record(s) quarantined; they are excluded from tax totals/exports.")
    n = conn.execute("SELECT COUNT(*) FROM tax_documents WHERE status!='reconciled'").fetchone()[0]
    if n: issues.append(f"{n} tax document(s) fail source-total reconciliation.")
    if not issues: issues.append("No known data-quality issues in imported evidence.")
    return issues


def D(v):
    """Canonical decimal conversion for money/quantity math; never aggregate binary floats."""
    if v is None or v == "":
        return Decimal("0")
    try:
        return Decimal(str(v).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return Decimal("0")

def money(v):
    return D(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def tax_summary(records):
    # One deterministic math path for cards, term buckets and exports.
    proceeds = sum((D(r["proceeds"]) for r in records), Decimal("0"))
    basis = sum((D(r["cost_basis"]) for r in records), Decimal("0"))
    gl = sum((D(r["gain_loss"]) if r["gain_loss"] is not None else D(r["proceeds"]) - D(r["cost_basis"]) for r in records), Decimal("0"))
    short = sum((D(r["gain_loss"]) for r in records if (r["term"] or "").lower().startswith("short")), Decimal("0"))
    longv = sum((D(r["gain_loss"]) for r in records if (r["term"] or "").lower().startswith("long")), Decimal("0"))
    return {"proceeds": money(proceeds), "basis": money(basis), "gain_loss": money(gl),
            "short": money(short), "long": money(longv)}

def format_units(v):
    if v is None or v == "":
        return "—"
    d = D(v)
    # Preserve useful precision without scientific notation or giant ungrouped values.
    if abs(d) >= Decimal("1000000"):
        places = 6
    elif abs(d) >= Decimal("1"):
        places = 8
    else:
        places = 12
    out = f"{d:,.{places}f}".rstrip("0").rstrip(".")
    return out if out else "0"


def reconcile_tax_to_ledger(conn, records):
    # v1 heuristic: asset quantities/fees visible in ledger near tax records.
    result = []
    for r in records[:200]:
        asset = r["asset"]
        if not asset or asset == "MULTI":
            result.append({"asset": asset or "?", "status": "SUMMARY", "detail": "Aggregate tax evidence; row-level reconciliation unavailable."})
            continue
        matches = conn.execute("SELECT COUNT(*) FROM transactions WHERE asset=? OR fee_asset=?", (asset, asset)).fetchone()[0]
        result.append({"asset": asset, "status": "EVIDENCE" if matches else "GAP", "detail": f"{matches} matching ledger event(s) found."})
    return result



def tax_integrity(conn, year, records=None):
    records = records if records is not None else conn.execute("SELECT * FROM tax_records WHERE tax_year=?", (year,)).fetchall()
    docs = conn.execute("SELECT * FROM tax_documents WHERE tax_year=? ORDER BY provider", (year,)).fetchall()
    issues = conn.execute("SELECT * FROM tax_record_issues WHERE tax_year=? ORDER BY id", (year,)).fetchall()
    row_math_variance = 0.0
    bad_dates = 0
    bad_year = 0
    unknown_terms = 0
    for r in records:
        p, b, g = r["proceeds"], r["cost_basis"], r["gain_loss"]
        if p is not None and b is not None and g is not None:
            row_math_variance = max(row_math_variance, abs((p-b)-g))
        a, d = parse_tax_date(r["acquired_date"]), parse_tax_date(r["disposed_date"])
        if a and d and a.date() > d.date(): bad_dates += 1
        if d and d.year != year: bad_year += 1
        if (r["term"] or "unknown") == "unknown": unknown_terms += 1
    unreconciled = [d for d in docs if d["status"] != "reconciled"]
    export_ready = bool(records) and not issues and not unreconciled and row_math_variance <= 0.02 and bad_dates == 0 and bad_year == 0
    if not records:
        headline = "No tax evidence for this tax year."
    elif export_ready:
        headline = "PASS — evidence reconciled; tax export enabled."
    else:
        headline = "HOLD — evidence integrity checks failed; tax export blocked."
    details = []
    if docs:
        for d in docs:
            details.append(f"{d['provider'].upper()}: {d['status'].upper()} — {d['detail']}")
    if issues: details.append(f"{len(issues)} parser record(s) quarantined for review.")
    if bad_dates: details.append(f"{bad_dates} row(s) have acquisition dates after disposition dates.")
    if bad_year: details.append(f"{bad_year} row(s) do not match tax year {year}.")
    if row_math_variance > 0.02: details.append(f"Maximum row accounting variance is ${row_math_variance:.2f}.")
    if unknown_terms: details.append(f"{unknown_terms} row(s) have unknown holding term (for example, VARIOUS acquisition dates).")
    return {"export_ready": export_ready, "headline": headline, "details": details, "issues": issues, "documents": docs,
            "row_math_variance": row_math_variance, "bad_dates": bad_dates, "bad_year": bad_year, "unknown_terms": unknown_terms}


def migrate_ledger_parser(app: Flask):
    """Re-derive canonical Uphold ledger fields from preserved raw evidence.

    v3 fixes the custody-chain model: fiat cash movements stay in the ledger but
    no longer masquerade as investment lots, while crypto transfers retain their
    acquisition history across custody locations.  Original imported files and
    raw_json are never modified.
    """
    parser_version = "3"
    conn = sqlite3.connect(app.config["DB"])
    conn.row_factory = sqlite3.Row
    current = conn.execute("SELECT value FROM settings WHERE key='_ledger_parser_version'").fetchone()
    if current and current[0] == parser_version:
        conn.close()
        return

    rows = conn.execute("SELECT * FROM transactions WHERE provider='uphold' ORDER BY id").fetchall()
    if rows:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = Path(app.config["BACKUP_DIR"]) / f"auto-pre-ledger-parser-v{parser_version}-{stamp}.db"
        if Path(app.config["DB"]).exists():
            shutil.copy2(app.config["DB"], backup)

    for r in rows:
        try:
            rec = json.loads(r["raw_json"] or "{}")
            n = normalize_uphold_record(rec)
        except Exception:
            # Preserve the existing normalized row if the original evidence cannot
            # be decoded. It remains visible for manual review rather than guessed.
            conn.execute("UPDATE transactions SET review_required=1 WHERE id=?", (r["id"],))
            continue
        conn.execute("""UPDATE transactions
                        SET event_time=?, asset=?, quantity=?, fiat_value=?, fiat_currency=?,
                            fee_asset=?, fee_quantity=?, fee_usd=?, tx_type=?, origin=?,
                            destination=?, confidence=?, review_required=?
                        WHERE id=?""",
                     (n["event_time"], n["asset"], n["quantity"], n["fiat_value"], n["fiat_currency"],
                      n["fee_asset"], n["fee_quantity"], n["fee_usd"], n["tx_type"], n["origin"],
                      n["destination"], n["confidence"], n["review_required"], r["id"]))

    set_setting(conn, "_ledger_parser_version", parser_version)
    conn.commit()
    conn.close()


def migrate_tax_parser(app: Flask):
    parser_version = "2"
    conn = sqlite3.connect(app.config["DB"])
    conn.row_factory = sqlite3.Row
    current = conn.execute("SELECT value FROM settings WHERE key='_tax_parser_version'").fetchone()
    if current and current[0] == parser_version:
        conn.close()
        return
    rows = conn.execute("SELECT * FROM imports WHERE source_type='pdf' AND provider IN ('coinbase','uphold') ORDER BY id").fetchall()
    if rows:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = Path(app.config["BACKUP_DIR"]) / f"auto-pre-tax-parser-v{parser_version}-{stamp}.db"
        if Path(app.config["DB"]).exists(): shutil.copy2(app.config["DB"], backup)
    for r in rows:
        candidates = list(Path(app.config["UPLOAD_DIR"]).glob(f"{r['id']:06d}-*"))
        if not candidates:
            continue
        raw = candidates[0].read_bytes()
        conn.execute("DELETE FROM tax_records WHERE import_id=?", (r["id"],))
        try:
            import_pdf(conn, r["id"], raw, r["provider"])
        except Exception as exc:
            conn.execute("UPDATE imports SET status='review', notes=? WHERE id=?", (f"Tax parser migration failed: {exc}", r["id"]))
    set_setting(conn, "_tax_parser_version", parser_version)
    conn.commit()
    conn.close()


def apply_update_zip(app: Flask, storage) -> str:
    base = Path(app.config["BASE_DIR"]).resolve()
    raw = storage.read()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = Path(app.config["BACKUP_DIR"]) / f"pre-update-{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    for item in ("cryptohound", "run.py", "requirements.txt"):
        src = base / item
        if src.exists():
            if src.is_dir(): shutil.copytree(src, backup / item)
            else: shutil.copy2(src, backup / item)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        members = z.infolist()
        # Allow package-root directory or flat zip, but never data/backups/.venv.
        names = [m.filename.replace("\\", "/") for m in members if not m.is_dir()]
        if not names: raise ValueError("ZIP contains no files")
        common_top = names[0].split("/")[0] if all("/" in n and n.split("/")[0] == names[0].split("/")[0] for n in names) else ""
        applied = 0
        for m in members:
            if m.is_dir(): continue
            name = m.filename.replace("\\", "/")
            rel = name[len(common_top)+1:] if common_top and name.startswith(common_top + "/") else name
            rel = rel.lstrip("/")
            if not rel or rel == "credentials.json" or rel.startswith(("data/", "backups/", "reports/", ".venv/")):
                continue
            target = (base / rel).resolve()
            if base not in target.parents and target != base:
                raise ValueError(f"Unsafe ZIP path: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            applied += 1
    return f"Update applied ({applied} files). Backup: {backup.name}. Restart CryptoHound to load new code."



AI_MODE_TITLES = {
    "general": "Full-Service Advisor", "tax": "Tax Readiness SITREP",
    "portfolio": "Portfolio Review", "investing": "Investment Review",
    "evidence": "Evidence Investigation", "reconcile": "Reconciliation Review",
}

def render_ai_markdown(text: str) -> Markup:
    # Small dependency-free Markdown renderer for AI prose. HTML is escaped first.
    lines = str(text or "").splitlines()
    out, list_tag = [], None
    def inline(v: str) -> str:
        v = str(escape(v))
        v = re.sub(r"`([^`]+)`", r"<code>\1</code>", v)
        v = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", v)
        v = re.sub(r"(?<!\*)\*([^*]+)\*", r"<em>\1</em>", v)
        return v
    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>"); list_tag = None
    for raw in lines:
        line = raw.strip()
        if not line:
            close_list(); continue
        hm = re.match(r"^(#{1,4})\s+(.+)$", line)
        if hm:
            close_list(); level=len(hm.group(1)); out.append(f"<h{level}>{inline(hm.group(2))}</h{level}>"); continue
        lm = re.match(r"^[-*]\s+(.+)$", line)
        om = re.match(r"^\d+[.)]\s+(.+)$", line)
        if lm or om:
            wanted = "ul" if lm else "ol"
            if list_tag != wanted:
                close_list(); list_tag=wanted; out.append(f"<{wanted}>")
            out.append(f"<li>{inline((lm or om).group(1))}</li>"); continue
        close_list(); out.append(f"<p>{inline(line)}</p>")
    close_list()
    return Markup("\n".join(out))

def _report_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value or "AI-Report").strip("-")
    return slug[:60] or "AI-Report"

def _report_month_dir(app: Flask, when: datetime) -> Path:
    d = Path(app.config["REPORTS_DIR"]) / when.strftime("%Y") / when.strftime("%m")
    d.mkdir(parents=True, exist_ok=True)
    return d

def _report_question(conn, assistant_id: int) -> str:
    row = conn.execute("SELECT content FROM ai_messages WHERE role='user' AND id < ? ORDER BY id DESC LIMIT 1", (assistant_id,)).fetchone()
    return row["content"] if row else "CryptoHound AI Advisor mission"

def create_ai_report_artifacts(app: Flask, conn, message_id: int) -> dict[str, Path]:
    row = conn.execute("SELECT * FROM ai_messages WHERE id=? AND role='assistant'", (message_id,)).fetchone()
    if not row:
        raise ValueError("Assistant report not found")
    question = _report_question(conn, message_id)
    created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")) if row["created_at"] else datetime.now(timezone.utc)
    title = AI_MODE_TITLES.get(row["mode"] or "general", "CryptoHound AI Report")
    outdir = _report_month_dir(app, created.astimezone())
    stem = f"{created.astimezone().strftime('%Y-%m-%d_%H%M%S')}_{_report_slug(title)}_m{message_id}"
    pdf_path, json_path = outdir / f"{stem}.pdf", outdir / f"{stem}.json"
    receipt = {
        "application": APP_NAME, "version": VERSION, "report_message_id": message_id,
        "created_at": row["created_at"], "exported_at": datetime.now(timezone.utc).isoformat(),
        "mission": row["mode"], "title": title, "model": row["model"],
        "question": question, "response": row["content"],
        "evidence_context": json_safe(build_ai_context(conn)),
        "doctrine": "AI analysis is advisory and read-only; deterministic CryptoHound evidence/accounting remains authoritative.",
    }
    json_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
    build_ai_report_pdf(pdf_path, receipt)
    return {"pdf": pdf_path, "json": json_path}

def build_ai_report_pdf(path: Path, receipt: dict[str, Any]) -> None:
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    except ImportError as exc:
        raise RuntimeError("PDF export requires reportlab. Run: .venv/bin/pip install -r requirements.txt") from exc
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="WolfTitle", parent=styles["Title"], fontSize=20, leading=24, spaceAfter=8))
    styles.add(ParagraphStyle(name="WolfMeta", parent=styles["Normal"], fontSize=9, leading=12, textColor="#4b6173", spaceAfter=12))
    styles.add(ParagraphStyle(name="WolfBody", parent=styles["BodyText"], fontSize=10.5, leading=15, spaceAfter=7))
    doc = SimpleDocTemplate(str(path), pagesize=letter, rightMargin=.65*inch, leftMargin=.65*inch, topMargin=.65*inch, bottomMargin=.65*inch, title=receipt["title"], author="CryptoHound")
    story = [Paragraph("CryptoHound AI Advisor", styles["WolfTitle"]), Paragraph(escape(receipt["title"]), styles["Heading2"]),
             Paragraph(f"Model: {escape(receipt.get('model') or '—')} &nbsp;&nbsp; Mission: {escape((receipt.get('mission') or 'general').upper())} &nbsp;&nbsp; Generated: {escape(receipt.get('created_at') or '')}", styles["WolfMeta"]),
             Paragraph("Operator Tasking", styles["Heading3"]), Paragraph(escape(receipt.get("question") or ""), styles["WolfBody"]), Spacer(1, 6), Paragraph("Analysis", styles["Heading2"])]
    # Lightweight Markdown-to-PDF: preserve report hierarchy without executing HTML.
    for raw in (receipt.get("response") or "").splitlines():
        line = raw.strip()
        if not line:
            story.append(Spacer(1, 5)); continue
        if line.startswith("### "): story.append(Paragraph(escape(line[4:]), styles["Heading4"])); continue
        if line.startswith("## "): story.append(Paragraph(escape(line[3:]), styles["Heading3"])); continue
        if line.startswith("# "): story.append(Paragraph(escape(line[2:]), styles["Heading2"])); continue
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", str(escape(line)))
        if re.match(r"^[-*]\s+", line): line = "• " + re.sub(r"^[-*]\s+", "", line)
        story.append(Paragraph(line, styles["WolfBody"]))
    story += [Spacer(1, 12), Paragraph("Evidence Receipt", styles["Heading3"]), Paragraph(f"Message ID: {receipt['report_message_id']} · CryptoHound {VERSION} · JSON receipt saved beside this PDF.", styles["WolfMeta"]), Paragraph("AI analysis is advisory and read-only. Verify material tax, legal, market, and accounting conclusions against the preserved source evidence.", styles["WolfMeta"])]
    doc.build(story)

def list_ai_reports(app: Flask) -> list[dict[str, Any]]:
    root = Path(app.config["REPORTS_DIR"])
    items = []
    for pdf in sorted(root.glob("**/*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        rel = pdf.relative_to(root).as_posix()
        items.append({"name": pdf.stem, "pdf": rel, "json": rel[:-4] + ".json" if pdf.with_suffix('.json').exists() else None, "modified": datetime.fromtimestamp(pdf.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
    return items

def get_openai_api_key(app: Flask) -> str:
    """Return the OpenAI key from local credentials.json, with env as a legacy fallback."""
    path = Path(app.config["CREDENTIALS_FILE"])
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            key = str(payload.get("openai_api_key") or payload.get("OPENAI_API_KEY") or "").strip()
            if key:
                return key
    except (OSError, ValueError, TypeError):
        pass
    return (os.environ.get("OPENAI_API_KEY") or "").strip()


def save_openai_api_key(app: Flask, api_key: str) -> None:
    """Persist the API key outside the database/source tree and restrict it to the owner."""
    path = Path(app.config["CREDENTIALS_FILE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"openai_api_key": api_key.strip()}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def first_col(cols, *names):
    for n in names:
        if n in cols: return cols[n]
    return None

def val(row, col): return row[col] if col is not None else None

def clean_value(v):
    if pd.isna(v): return None
    if isinstance(v, (pd.Timestamp, datetime)): return v.isoformat()
    return v.item() if hasattr(v, "item") else v

def num(v):
    if v is None or v == "": return None
    try: return float(str(v).replace(",", "").replace("$", "").replace("(", "-").replace(")", "").strip())
    except Exception: return None

def iso_date(v):
    if v is None or str(v).strip() == "": return None
    try: return dtparser.parse(str(v)).isoformat()
    except Exception: return str(v)
def infer_year(text):
    import re
    # PDF text extraction can space letters on cover pages, so prefer any explicit year tied to 1099-DA language.
    patterns = [
        r"(?:Tax year|TAX YEAR)\s*(20\d{2})",
        r"for\s+(20\d{2})\s+transactions",
        r"Summary of\s+(20\d{2})\s+Digital Asset",
        r"December\s+31,\s*(20\d{2})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m: return int(m.group(1))
    years = [int(x) for x in re.findall(r"\b20\d{2}\b", text)]
    plausible = [y for y in years if 2009 <= y <= datetime.now().year]
    return max(set(plausible), key=plausible.count) if plausible else datetime.now().year
def _txf_date(v):
    if not v or str(v).upper() == "VARIOUS": return "VARIOUS"
    try: return dtparser.parse(str(v)).strftime("%m/%d/%Y")
    except Exception: return str(v)
def human_bytes(n):
    for unit in ("B","KB","MB","GB"):
        if n < 1024: return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
