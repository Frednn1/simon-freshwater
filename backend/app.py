import os
import json
import secrets
import ipaddress
from datetime import datetime, date, timedelta
from collections import deque
from threading import Lock
from time import time as _time_now

from flask import (
    Flask, request, jsonify, send_from_directory, session, send_file,
    redirect,
)
from flask_cors import CORS
from sqlalchemy import text, inspect

from models import (
    db, Consumer, MeterReading, NotificationLog,
    AdminUser, PaymentLog, MpesaLog,
)
from billing import (
    compute_amount, compute_consumption,
    get_consumer_status, get_reading_bill_status,
)
from sheets import sync_consumer_to_sheet
from reminders import start_scheduler
from notifications import (
    send_sms, send_sms_with_retry, send_whatsapp, send_whatsapp_document,
    build_bill_message, get_available_channels,
)
from admin_auth import (
    create_first_admin, login as admin_login_fn,
    request_password_reset, confirm_password_reset,
)
from werkzeug.security import check_password_hash, generate_password_hash
from receipts import generate_receipt_pdf
from water_bill import generate_water_bill_pdf
from report import generate_report_pdf
from bulk_sms import send_bulk_sms, BULK_SMS_MAX_BATCH
from mpesa import (
    get_mpesa_config, register_c2b_urls, simulate_c2b_payment,
)
from statement import generate_statement_pdf


BACKEND_DIR   = os.path.dirname(os.path.abspath(__file__))
REPO_DIR      = os.path.dirname(BACKEND_DIR)
FRONTEND_DIR  = os.path.join(REPO_DIR, "frontend")
TEMPLATES_DIR = os.path.join(REPO_DIR, "templates")

NOTIFY_COOLDOWN_SECONDS = int(os.environ.get("NOTIFY_COOLDOWN_SECONDS", "300"))


app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
CORS(app, supports_credentials=True)


DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///simon_freshwater.db")

# ─── Cloudflare D1 credentials (build the URL from parts if not already set) ───
CF_ACCOUNT_ID    = os.environ.get("CF_ACCOUNT_ID", "")
CF_D1_DATABASE_ID = os.environ.get("CF_D1_DATABASE_ID", "")
CF_API_TOKEN     = os.environ.get("CF_API_TOKEN", "")

# If a cloudflare_d1:// URL is provided, use it as-is.
# Otherwise, if the three CF_* values are set, build the D1 URL.
if not DATABASE_URL.startswith("cloudflare_d1://"):
    if CF_ACCOUNT_ID and CF_D1_DATABASE_ID and CF_API_TOKEN:
        DATABASE_URL = f"cloudflare_d1://{CF_ACCOUNT_ID}:{CF_API_TOKEN}@{CF_D1_DATABASE_ID}"
        app.logger.info("[DB] Using Cloudflare D1 database")
    elif DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        app.logger.info("[DB] Using PostgreSQL database")

# SQLAlchemy 2.x needs the dialect imported before use for D1
if DATABASE_URL.startswith("cloudflare_d1://"):
    import sqlalchemy_cloudflare_d1  # noqa: F401

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-me")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
SESSION_IDLE_MINUTES = 30
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))

# Connection-pool options only apply to real socket-based DBs (PostgreSQL).
# D1 uses stateless HTTP requests — no pool to manage.
if DATABASE_URL.startswith("postgres"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 180,
        "pool_size": 5,
        "max_overflow": 5,
        "pool_timeout": 30,
    }

db.init_app(app)


# ═══════════════════════════════════════════════
#  SECURITY HEADERS
# ═══════════════════════════════════════════════

@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(self)")
    return resp


# ═══════════════════════════════════════════════
#  RATE LIMITER
# ═══════════════════════════════════════════════

_rate_buckets = {}
_rate_lock = Lock()


def _client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _ip_in_allowlist(ip_str: str, allowlist_csv: str) -> bool:
    """
    Return True if `ip_str` is in the comma-separated CIDR list.
    Fail-open when the allowlist is empty (safe for initial rollout).
    """
    if not allowlist_csv or not allowlist_csv.strip():
        return True
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for cidr in allowlist_csv.split(","):
        cidr = cidr.strip()
        if not cidr:
            continue
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def _mpesa_ip_ok() -> bool:
    """True if the current request's client IP is allowed to call M-Pesa endpoints."""
    allowed = os.environ.get("MPESA_ALLOWED_IPS", "").strip()
    return _ip_in_allowlist(_client_ip(), allowed)


def _rate_limit(scope: str, limit: int, window: int) -> bool:
    key = f"{scope}:{_client_ip()}"
    now = _time_now()
    with _rate_lock:
        dq = _rate_buckets.setdefault(key, deque())
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
    return True


def _too_many(retry_after: int = 60):
    r = jsonify({"error": "Too many requests. Please slow down and try again shortly."})
    r.status_code = 429
    r.headers["Retry-After"] = str(retry_after)
    return r


# ═══════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════

def _previous_reading(consumer_id, exclude_id=None):
    q = MeterReading.query.filter_by(consumer_id=consumer_id)
    if exclude_id is not None:
        q = q.filter(MeterReading.id != exclude_id)
    return q.order_by(MeterReading.reading_date.desc(),
                      MeterReading.id.desc()).first()


def _all_readings(consumer_id):
    return (MeterReading.query
            .filter_by(consumer_id=consumer_id)
            .order_by(MeterReading.reading_date.desc(),
                      MeterReading.id.desc()).all())


def _total_uncleared_consumption(consumer) -> float:
    """
    Sum of consumption (M³) across readings whose bill is NOT fully paid.
    Cleared bills are excluded.

    Consumption for each reading = reading − previous reading
    (for the very first reading: reading − initial_meter_reading).
    """
    readings_asc = list(reversed(_all_readings(consumer.id)))  # oldest → newest
    initial = float(consumer.meter_initial_reading_m3 or 0.0)
    total = 0.0
    prev_m3 = None
    for r in readings_asc:
        cons = compute_consumption(r.reading_m3, prev_m3, initial)
        balance = (r.amount_kes or 0.0) - (r.amount_paid or 0.0)
        if balance > 0:
            total += cons
        prev_m3 = r.reading_m3
    return round(total, 2)


def _current_admin():
    """Return the authenticated AdminUser or None. Enforces idle timeout."""
    aid = session.get("admin_id")
    if not aid:
        return None

    # Idle-timeout enforcement (server-side — cannot be bypassed by client)
    last = session.get("last_activity")
    now = datetime.utcnow()
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
        except (ValueError, TypeError):
            session.clear()
            return None
        if now - last_dt > timedelta(minutes=SESSION_IDLE_MINUTES):
            session.clear()
            return None

    a = AdminUser.query.get(aid)
    if not a or not a.is_active:
        session.clear()
        return None

    # Roll the activity timestamp forward on each authenticated request
    session["last_activity"] = now.isoformat()
    return a


def _require_admin():
    if _current_admin():
        return None
    secret = request.headers.get("X-Admin-Secret", "")
    expected = os.environ.get("ADMIN_SEED_SECRET", "")
    if expected and secret and secret == expected:
        return None
    return jsonify({"error": "Unauthorized. Please log in."}), 401


def _fmt_money(n):
    try:
        return f"{float(n or 0):,.2f}"
    except (ValueError, TypeError):
        return "0.00"


def _fmt_date(d):
    if isinstance(d, str):
        return d
    return d.strftime("%d %b %Y") if d else "—"


def _ensure_all_columns():
    """Idempotent migration for consumers + payment_log.
    Uses dialect-aware SQL types so it works on both SQLite (D1) and PostgreSQL."""
    insp = inspect(db.engine)
    dialect = db.engine.dialect.name  # 'sqlite' or 'postgresql'
    tables = set(insp.get_table_names())

    if dialect == "sqlite":
        REAL_T = "REAL"
        TIME_T = "TEXT"
    else:
        REAL_T = "DOUBLE PRECISION"
        TIME_T = "TIMESTAMP"

    if "consumers" in tables:
        existing = {c["name"] for c in insp.get_columns("consumers")}
        alters = []
        if "latitude" not in existing:
            alters.append(f"ADD COLUMN latitude {REAL_T} NULL")
        if "longitude" not in existing:
            alters.append(f"ADD COLUMN longitude {REAL_T} NULL")
        if "is_active" not in existing:
            if dialect == "sqlite":
                alters.append("ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
            else:
                alters.append("ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE")
        if "terminated_at" not in existing:
            alters.append(f"ADD COLUMN terminated_at {TIME_T} NULL")
        if "meter_initial_reading_m3" not in existing:
            alters.append(f"ADD COLUMN meter_initial_reading_m3 {REAL_T} NOT NULL DEFAULT 0")
        for alt in alters:
            db.session.execute(text(f"ALTER TABLE consumers {alt}"))
        if alters:
            db.session.commit()
            app.logger.info(f"[Migration] Applied {len(alters)} column(s) to consumers")

    if "payment_log" in tables:
        existing = {c["name"] for c in insp.get_columns("payment_log")}
        if "receipt_json" not in existing:
            db.session.execute(text("ALTER TABLE payment_log ADD COLUMN receipt_json TEXT NULL"))
            db.session.commit()
            app.logger.info("[Migration] Added receipt_json to payment_log")


# ═══════════════════════════════════════════════
#  FRONTEND ROUTES
# ═══════════════════════════════════════════════

@app.route("/")
def home():
    return send_from_directory(FRONTEND_DIR, "index.html")

@app.route("/consumer")
def consumer_page():
    return send_from_directory(FRONTEND_DIR, "consumer.html")

@app.route("/bill")
def bill_page():
    return send_from_directory(FRONTEND_DIR, "bill.html")

@app.route("/admin/login")
def admin_login_page():
    return send_from_directory(FRONTEND_DIR, "admin_login.html")

@app.route("/admin/setup")
def admin_setup_page():
    return send_from_directory(FRONTEND_DIR, "admin_setup.html")

@app.route("/admin/forgot")
def admin_forgot_page():
    return send_from_directory(FRONTEND_DIR, "admin_forgot.html")

@app.route("/admin/settings")
def admin_settings_page():
    return send_from_directory(FRONTEND_DIR, "admin_settings.html")

@app.route("/admin/mpesa")
def admin_mpesa_page():
    return send_from_directory(FRONTEND_DIR, "admin_mpesa.html")

@app.route("/admin/reports")
def admin_reports_page():
    return send_from_directory(FRONTEND_DIR, "admin_reports.html")

@app.route("/admin/bulk_sms")
def admin_bulk_sms_page():
    return send_from_directory(FRONTEND_DIR, "admin_bulk_sms.html")


# ═══════════════════════════════════════════════
#  ADMIN — MONTHLY BILLING REPORT
# ═══════════════════════════════════════════════

def _parse_ym_from_request():
    """Extract (year, month) from query string; default = current month."""
    today = date.today()
    try:
        year = int(request.args.get("year", today.year))
    except (ValueError, TypeError):
        year = today.year
    try:
        month = int(request.args.get("month", today.month))
    except (ValueError, TypeError):
        month = today.month
    if not (1 <= month <= 12):
        month = today.month
    if not (2000 <= year <= 2100):
        year = today.year
    return year, month


def _build_report_snapshot(year: int, month: int) -> dict:
    """Collect the report data for the given month from D1."""
    month_names = ["January","February","March","April","May","June",
                   "July","August","September","October","November","December"]

    consumers = Consumer.query.order_by(Consumer.cust_name.asc()).all()

    total_consumers = len(consumers)
    active_consumers = sum(1 for c in consumers if c.is_active)
    total_consumption = 0.0
    total_billed = 0.0
    total_paid = 0.0
    total_outstanding = 0.0

    # per-consumer data
    rows = []
    for c in consumers:
        if not c.is_active:
            continue

        all_readings = (MeterReading.query
                        .filter_by(consumer_id=c.id)
                        .order_by(MeterReading.reading_date.asc(),
                                  MeterReading.id.asc())
                        .all())

        initial = float(c.meter_initial_reading_m3 or 0.0)
        cons_by_id = {}
        prev_m3 = None
        for r in all_readings:
            cons_by_id[r.id] = compute_consumption(r.reading_m3, prev_m3, initial)
            prev_m3 = r.reading_m3

        # Readings in the given month
        month_readings = [r for r in all_readings
                          if r.reading_date.year == year
                          and r.reading_date.month == month]

        cnt = len(month_readings)
        cm3 = round(sum(cons_by_id[r.id] for r in month_readings), 2)
        amt = round(sum(float(r.amount_kes or 0.0) for r in month_readings), 2)

        # Payments in the given month
        all_payments = PaymentLog.query.filter_by(consumer_id=c.id).all()
        month_payments = []
        for p in all_payments:
            dt = p.created_at
            if dt and dt.year == year and dt.month == month:
                month_payments.append(p)
        paid = round(sum(float(p.amount_kes or 0.0) for p in month_payments), 2)

        # Overall status + outstanding balance (all-time)
        info = get_consumer_status(list(reversed(all_readings)))
        outstanding = float(info.get("total_due", 0.0))

        total_consumption += cm3
        total_billed += amt
        total_paid += paid
        total_outstanding += outstanding

        rows.append({
            "name": c.cust_name,
            "meter": c.meter_acc_no,
            "cnt": cnt,
            "cm3": f"{cm3:.2f}",
            "amt": _fmt_money(amt),
            "paid": _fmt_money(paid),
            "bal": f"{_fmt_money(outstanding)} · {info['label']}",
        })

    truncated = len(rows) > 20
    rows = rows[:20]

    snapshot = {
        "report_no": f"RPT-{year}{month:02d}",
        "report_month": month_names[month - 1],
        "report_year": str(year),
        "generated_date": date.today().strftime("%d %b %Y"),
        "total_consumers": str(total_consumers),
        "active_consumers": str(active_consumers),
        "total_consumption": f"{total_consumption:.2f}",
        "total_billed": _fmt_money(total_billed),
        "total_paid": _fmt_money(total_paid),
        "total_outstanding": _fmt_money(total_outstanding),
    }

    # Row placeholders r1_*..r20_*
    for idx in range(1, 21):
        row = rows[idx - 1] if idx - 1 < len(rows) else None
        snapshot[f"r{idx}_name"]  = row["name"]  if row else ""
        snapshot[f"r{idx}_meter"] = row["meter"] if row else ""
        snapshot[f"r{idx}_cnt"]   = str(row["cnt"]) if row else ""
        snapshot[f"r{idx}_cm3"]   = row["cm3"]   if row else ""
        snapshot[f"r{idx}_amt"]   = row["amt"]   if row else ""
        snapshot[f"r{idx}_paid"]  = row["paid"]  if row else ""
        snapshot[f"r{idx}_bal"]   = row["bal"]   if row else ""

    return snapshot, truncated


@app.route("/api/admin/report.pdf")
def download_billing_report():
    """Generate the monthly billing report PDF (admin-only)."""
    u = _require_admin()
    if u: return u
    if not _rate_limit("report", 10, 60):
        return _too_many(60)

    year, month = _parse_ym_from_request()

    try:
        snapshot, _truncated = _build_report_snapshot(year, month)
    except Exception as e:
        app.logger.exception("[Report] snapshot failed")
        return jsonify({"error": f"Report data failed: {e}"}), 500

    try:
        pdf_buf = generate_report_pdf(snapshot)
    except Exception as e:
        app.logger.exception("[Report] PDF generation failed")
        return jsonify({"error": f"PDF generation failed: {e}"}), 500

    filename = f"SimonWater_Report_{year}-{month:02d}.pdf"
    return send_file(
        pdf_buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/admin/report/preview")
def billing_report_preview():
    """Return the report data as JSON for the admin preview page."""
    u = _require_admin()
    if u: return u
    if not _rate_limit("report_preview", 20, 60):
        return _too_many(60)

    year, month = _parse_ym_from_request()
    try:
        snapshot, truncated = _build_report_snapshot(year, month)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "year": year,
        "month": month,
        "report_no": snapshot["report_no"],
        "summary": {
            "total_consumers": snapshot["total_consumers"],
            "active_consumers": snapshot["active_consumers"],
            "total_consumption": snapshot["total_consumption"],
            "total_billed": snapshot["total_billed"],
            "total_paid": snapshot["total_paid"],
            "total_outstanding": snapshot["total_outstanding"],
        },
        "truncated": truncated,
    })

@app.route("/template/report")
def report_template_view():
    """Serve the report template HTML for manual Chrome -> Save as PDF."""
    if not _current_admin():
        return redirect("/admin/login?next=/template/report")
    return send_from_directory(
        TEMPLATES_DIR,
        "SimonWater_ReportTemplate.html",
        mimetype="text/html",
    )

@app.route("/style.css")
def styles():
    return send_from_directory(FRONTEND_DIR, "style.css")

@app.route("/app.js")
def appjs():
    return send_from_directory(FRONTEND_DIR, "app.js")


@app.route("/template/statement")
def statement_template_view():
    """
    Serve the statement template HTML for manual Chrome -> Save as PDF.
    Admin-only.
    """
    if not _current_admin():
        return redirect("/admin/login?next=/template/statement")
    return send_from_directory(
        TEMPLATES_DIR,
        "SimonWater_StatementTemplate.html",
        mimetype="text/html",
    )


@app.route("/template/receipt")
def receipt_template_view():
    """
    Serve the receipt template HTML for manual Chrome -> Save as PDF.
    Admin-only; unauthenticated visitors are redirected to the login page.
    """
    if not _current_admin():
        return redirect("/admin/login?next=/template/receipt")
    return send_from_directory(
        TEMPLATES_DIR,
        "SimonWater_ReceiptTemplate.html",
        mimetype="text/html",
    )


# ═══════════════════════════════════════════════
#  API — HEALTH
# ═══════════════════════════════════════════════

@app.route("/api/public/config")
def public_config():
    """Non-secret config values safe for the frontend to read."""
    return jsonify({
        "paybill": os.environ.get("PAYBILL", ""),
        "company": "Simon Fresh Water",
    })


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})


# ═══════════════════════════════════════════════
#  API — SEARCH (admin-only)
# ═══════════════════════════════════════════════

@app.route("/api/search")
def search_consumer():
    if not _rate_limit("search", 30, 60):
        return _too_many(60)

    u = _require_admin()
    if u:
        return u

    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []}), 400

    pattern = f"%{q}%"
    consumers = (Consumer.query.filter(db.or_(
        Consumer.cust_name.ilike(pattern),
        Consumer.meter_acc_no.ilike(pattern),
    )).limit(20).all())

    out = []
    for c in consumers:
        info = get_consumer_status(_all_readings(c.id))
        out.append({
            "id": c.id, "cust_name": c.cust_name, "acc_name": c.acc_name,
            "meter_acc_no": c.meter_acc_no,
            "status": info["status"], "status_label": info["label"],
            "is_active": bool(c.is_active),
        })
    return jsonify({"results": out})


# ═══════════════════════════════════════════════
#  API — CONSUMER
# ═══════════════════════════════════════════════

@app.route("/api/consumer/<int:consumer_id>")
def get_consumer_details(consumer_id):
    consumer = Consumer.query.get_or_404(consumer_id)
    all_r = (MeterReading.query.filter_by(consumer_id=consumer.id)
             .order_by(MeterReading.reading_date.asc(),
                       MeterReading.id.asc()).all())

    initial = float(consumer.meter_initial_reading_m3 or 0.0)
    cons_by_id, prev_m3 = {}, None
    for r in all_r:
        cons_by_id[r.id] = compute_consumption(r.reading_m3, prev_m3, initial)
        prev_m3 = r.reading_m3

    latest5 = list(reversed(all_r))[:5]
    latest12 = list(reversed(all_r))[:12]   # for the trend chart
    info = get_consumer_status(latest5)

    last_sms = (NotificationLog.query
                .filter_by(consumer_id=consumer.id, channel="sms", status="sent")
                .order_by(NotificationLog.sent_at.desc()).first())
    last_email = (NotificationLog.query
                  .filter_by(consumer_id=consumer.id, channel="email", status="sent")
                  .order_by(NotificationLog.sent_at.desc()).first())

    payments_out = []
    if _current_admin():
        payments = (PaymentLog.query
                    .filter_by(consumer_id=consumer.id)
                    .order_by(PaymentLog.created_at.desc())
                    .limit(10)
                    .all())
        payments_out = [{
            "id": p.id,
            "amount_kes": p.amount_kes,
            "method": p.method,
            "reference": p.reference,
            "recorded_by": p.recorded_by,
            "created_at": p.created_at.isoformat(),
            "has_receipt": bool(p.receipt_json),
        } for p in payments]

    return jsonify({
        "consumer": {
            "id": consumer.id, "cust_name": consumer.cust_name,
            "acc_name": consumer.acc_name, "meter_acc_no": consumer.meter_acc_no,
            "contact": consumer.contact, "email": consumer.email,
            "address": consumer.address,
            "latitude": consumer.latitude, "longitude": consumer.longitude,
            "meter_initial_reading_m3": initial,
            "is_active": bool(consumer.is_active),
            "terminated_at": consumer.terminated_at.isoformat() if consumer.terminated_at else None,
        },
        "readings": [{
            "id": r.id, "reading_m3": r.reading_m3,
            "consumption_m3": cons_by_id.get(r.id, 0.0),
            "reading_date": r.reading_date.isoformat(),
            "amount_kes": r.amount_kes, "amount_paid": r.amount_paid,
            "balance": r.balance, "bill_status": get_reading_bill_status(r),
        } for r in latest5],
        "chart_readings": [{
            "reading_m3": r.reading_m3,
            "consumption_m3": cons_by_id.get(r.id, 0.0),
            "reading_date": r.reading_date.isoformat(),
        } for r in latest12],
        "payments": payments_out,
        "overall_status": info,
        "available_channels": get_available_channels(),
        "last_notifications": {
            "sms": last_sms.sent_at.isoformat() if last_sms else None,
            "email": last_email.sent_at.isoformat() if last_email else None,
        },
    })


@app.route("/api/reading/<int:reading_id>")
def get_reading_bill(reading_id):
    reading = MeterReading.query.get_or_404(reading_id)
    consumer = reading.consumer
    # Find the reading that came immediately BEFORE this one
    # (largest (reading_date, id) strictly smaller than the current one).
    prev_query = (MeterReading.query
                  .filter_by(consumer_id=consumer.id)
                  .filter(db.or_(
                      MeterReading.reading_date < reading.reading_date,
                      db.and_(
                          MeterReading.reading_date == reading.reading_date,
                          MeterReading.id < reading.id,
                      ),
                  ))
                  .order_by(MeterReading.reading_date.desc(),
                            MeterReading.id.desc()))
    prev = prev_query.first()
    prev_m3 = prev.reading_m3 if prev else None

    consumption = compute_consumption(
        reading.reading_m3, prev_m3,
        float(consumer.meter_initial_reading_m3 or 0.0),
    )

    return jsonify({
        "consumer": {
            "cust_name": consumer.cust_name, "acc_name": consumer.acc_name,
            "meter_acc_no": consumer.meter_acc_no, "contact": consumer.contact,
            "address": consumer.address,
        },
        "reading": {
            "id": reading.id, "reading_m3": reading.reading_m3,
            "consumption_m3": consumption,
            "reading_date": reading.reading_date.isoformat(),
            "amount_kes": reading.amount_kes, "amount_paid": reading.amount_paid,
            "balance": reading.balance,
            "bill_status": get_reading_bill_status(reading),
        },
    })


@app.route("/api/reading/<int:reading_id>/bill.pdf")
def download_water_bill(reading_id):
    """Generate and stream the water-bill PDF for a specific reading.
    Admin-only."""
    if not _rate_limit("water_bill", 20, 60):
        return _too_many(60)

    u = _require_admin()
    if u:
        return u

    reading = MeterReading.query.get_or_404(reading_id)
    consumer = reading.consumer

    # Consumption for this specific reading
    prev = _previous_reading(consumer.id, exclude_id=reading.id)
    prev_m3 = None
    if prev and (prev.reading_date < reading.reading_date
                 or (prev.reading_date == reading.reading_date
                     and prev.id < reading.id)):
        prev_m3 = prev.reading_m3
    consumption = compute_consumption(
        reading.reading_m3, prev_m3,
        float(consumer.meter_initial_reading_m3 or 0.0),
    )

    # Total outstanding balance across all readings
    all_r = _all_readings(consumer.id)
    info = get_consumer_status(all_r)

    snapshot = {
        "bill_no":        f"{reading.id:06d}",
        "date":           reading.reading_date.strftime("%d %b %Y"),
        "cust_name":      consumer.cust_name,
        "meter_acc_no":   consumer.meter_acc_no,
        "reading_m3":     f"{reading.reading_m3:.2f}",
        "consumption_m3": f"{_total_uncleared_consumption(consumer):.2f}",
        "amount_kes":     _fmt_money(reading.amount_kes),
        "balance":        _fmt_money(info["total_due"]),
        "status_label":   get_reading_bill_status(reading).upper(),
    }

    try:
        pdf_buf = generate_water_bill_pdf(snapshot)
    except Exception as e:
        app.logger.exception("[WaterBill] PDF generation failed")
        return jsonify({"error": f"PDF generation failed: {e}"}), 500

    filename = f"SimonWater_Bill_{reading.id:06d}.pdf"
    return send_file(
        pdf_buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/consumer/<int:consumer_id>/statement.pdf")
def download_statement(consumer_id):
    """Generate the customer accounting statement PDF (admin-only).
    Built on-the-fly from current DB state — reflects live readings + payments."""
    if not _rate_limit("statement", 20, 60):
        return _too_many(60)

    u = _require_admin()
    if u:
        return u

    consumer = Consumer.query.get_or_404(consumer_id)

    # ── Readings, oldest first ──
    readings_asc = (MeterReading.query
                    .filter_by(consumer_id=consumer.id)
                    .order_by(MeterReading.reading_date.asc(),
                              MeterReading.id.asc())
                    .all())

    initial = float(consumer.meter_initial_reading_m3 or 0.0)
    cons_by_id = {}
    prev_m3 = None
    for r in readings_asc:
        cons_by_id[r.id] = compute_consumption(r.reading_m3, prev_m3, initial)
        prev_m3 = r.reading_m3

    # ── Payments, oldest first ──
    payments_asc = (PaymentLog.query
                    .filter_by(consumer_id=consumer.id)
                    .order_by(PaymentLog.created_at.asc(),
                              PaymentLog.id.asc())
                    .all())

    # ── Build the unified ledger ──
    events = []
    for r in readings_asc:
        events.append({
            "date": r.reading_date,
            "type": "Water Bill",
            "ref":  f"Reading {r.reading_m3:.2f} M³",
            "dr":   float(r.amount_kes or 0.0),
            "cr":   0.0,
            "sort": (r.reading_date, 0, r.id),
        })
    for p in payments_asc:
        pdate = p.created_at.date() if p.created_at else date.today()
        ref = (p.method or "Payment").title()
        if p.reference:
            ref += f" · {p.reference}"
        events.append({
            "date": pdate,
            "type": "Payment",
            "ref":  ref,
            "dr":   0.0,
            "cr":   float(p.amount_kes or 0.0),
            "sort": (pdate, 1, p.id),
        })

    events.sort(key=lambda e: e["sort"])

    # Running balance (oldest → newest)
    bal = 0.0
    for e in events:
        bal += e["dr"] - e["cr"]
        e["balance"] = round(bal, 2)

    # Newest first
    events_desc = list(reversed(events))
    rows = events_desc[:20]

    # ── Totals ──
    total_billed = round(sum(float(r.amount_kes or 0.0) for r in readings_asc), 2)
    total_paid   = round(sum(float(p.amount_kes or 0.0) for p in payments_asc), 2)
    outstanding  = round(max(total_billed - total_paid, 0.0), 2)
    total_consumption_m3 = round(sum(cons_by_id.values()), 2)

    latest_r = readings_asc[-1] if readings_asc else None

    # Newest-first list for status calc
    info = get_consumer_status(list(reversed(readings_asc))) if readings_asc \
           else get_consumer_status([])

    # Period
    if events:
        period_from = events[0]["date"].strftime("%d %b %Y")
        period_to   = events[-1]["date"].strftime("%d %b %Y")
    else:
        period_from = period_to = "—"

    snapshot = {
        "statement_no":   f"STMT-{consumer.id:06d}",
        "statement_date": date.today().strftime("%d %b %Y"),
        "period_from":    period_from,
        "period_to":      period_to,
        "cust_name":      consumer.cust_name,
        "acc_name":       consumer.acc_name,
        "meter_acc_no":   consumer.meter_acc_no,
        "contact":        consumer.contact or "—",
        "email":          consumer.email or "—",
        "address":        consumer.address or "—",
        "total_consumption_m3": f"{total_consumption_m3:.2f}",
        "latest_reading_m3":    f"{latest_r.reading_m3:.2f}" if latest_r else "—",
        "latest_reading_date":  latest_r.reading_date.strftime("%d %b %Y") if latest_r else "—",
        "status_label":         info["label"],
        "total_billed":         _fmt_money(total_billed),
        "total_paid":           _fmt_money(total_paid),
        "outstanding_balance":  _fmt_money(outstanding),
    }

    for idx in range(1, 21):
        if idx - 1 < len(rows):
            e = rows[idx - 1]
            snapshot[f"t{idx}_date"] = e["date"].strftime("%d %b %Y")
            snapshot[f"t{idx}_type"] = e["type"]
            snapshot[f"t{idx}_ref"]  = e["ref"]
            snapshot[f"t{idx}_dr"]   = _fmt_money(e["dr"]) if e["dr"] > 0 else ""
            snapshot[f"t{idx}_cr"]   = _fmt_money(e["cr"]) if e["cr"] > 0 else ""
            snapshot[f"t{idx}_bal"]  = _fmt_money(e["balance"])
        else:
            snapshot[f"t{idx}_date"] = ""
            snapshot[f"t{idx}_type"] = ""
            snapshot[f"t{idx}_ref"]  = ""
            snapshot[f"t{idx}_dr"]   = ""
            snapshot[f"t{idx}_cr"]   = ""
            snapshot[f"t{idx}_bal"]  = ""

    try:
        pdf_buf = generate_statement_pdf(snapshot)
    except Exception as e:
        app.logger.exception("[Statement] PDF generation failed")
        return jsonify({"error": f"PDF generation failed: {e}"}), 500

    filename = f"SimonWater_Statement_{consumer.id:06d}.pdf"
    return send_file(
        pdf_buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/consumer/<int:consumer_id>/whatsapp_bill", methods=["POST"])
def send_whatsapp_bill(consumer_id):
    """
    Build the water bill PDF for the consumer's most recent reading,
    upload it to a public URL, and deliver it via WhatsApp template.
    Admin-only.
    """
    if not _rate_limit("whatsapp_bill", 20, 60):
        return _too_many(60)

    u = _require_admin()
    if u:
        return u

    # Honor the WA_ENABLED toggle — refuse if disabled, even if the
    # request bypasses the UI.
    wa_toggle = os.environ.get("WA_ENABLED", "false").strip().lower()
    if wa_toggle not in ("1", "true", "yes", "on"):
        return jsonify({"error": "WhatsApp sending is currently disabled."}), 403

    consumer = Consumer.query.get_or_404(consumer_id)
    if not consumer.is_active:
        return jsonify({"error": "Consumer is terminated. No reminders sent."}), 403
    if not consumer.contact:
        return jsonify({"error": "Consumer has no contact number on file."}), 400

    # Latest reading
    latest = _all_readings(consumer.id)
    if not latest:
        return jsonify({"error": "No readings on file — cannot generate bill."}), 400
    reading = latest[0]

    # Consumption for that reading
    prev = _previous_reading(consumer.id, exclude_id=reading.id)
    prev_m3 = None
    if prev and (prev.reading_date < reading.reading_date
                 or (prev.reading_date == reading.reading_date
                     and prev.id < reading.id)):
        prev_m3 = prev.reading_m3
    consumption = compute_consumption(
        reading.reading_m3, prev_m3,
        float(consumer.meter_initial_reading_m3 or 0.0),
    )

    info = get_consumer_status(latest)

    # Build water bill snapshot (same shape as /bill.pdf endpoint)
    snapshot = {
        "bill_no":        f"{reading.id:06d}",
        "date":           reading.reading_date.strftime("%d %b %Y"),
        "cust_name":      consumer.cust_name,
        "meter_acc_no":   consumer.meter_acc_no,
        "reading_m3":     f"{reading.reading_m3:.2f}",
        "consumption_m3": f"{_total_uncleared_consumption(consumer):.2f}",
        "amount_kes":     _fmt_money(reading.amount_kes),
        "balance":        _fmt_money(info["total_due"]),
        "status_label":   get_reading_bill_status(reading).upper(),
    }

    try:
        pdf_buf = generate_water_bill_pdf(snapshot)
    except Exception as e:
        app.logger.exception("[WhatsApp Bill] PDF generation failed")
        return jsonify({"error": f"PDF generation failed: {e}"}), 500

    # Save PDF into a token-addressed public slot.
    # Reuse the receipt token machinery but with a dedicated store.
    token = secrets.token_urlsafe(32)
    _PUBLIC_BILL_TOKENS[token] = bytes(pdf_buf.getvalue())

    public_url = request.host_url.rstrip("/") + f"/public/bill/{token}.pdf"
    filename = f"SimonWater_Bill_{reading.id:06d}.pdf"

    # Send via WhatsApp template with document header
    result = send_whatsapp_document(
        to_phone=consumer.contact,
        pdf_public_url=public_url,
        filename=filename,
        body_params=[consumer.cust_name, consumer.meter_acc_no],
    )

    if result.get("ok"):
        return jsonify({
            "ok": True,
            "channel": "whatsapp",
            "message": "WhatsApp bill sent successfully.",
            "to": consumer.contact,
        }), 200

    return jsonify({
        "ok": False,
        "error": result.get("error", "Send failed"),
    }), 502


@app.route("/public/bill/<token>.pdf")
def public_bill_pdf(token):
    """Serve a token-addressed water bill PDF (no auth)."""
    data = _PUBLIC_BILL_TOKENS.get(token)
    if not data:
        return "Not found.", 404
    from io import BytesIO
    return send_file(BytesIO(data), mimetype="application/pdf",
                     as_attachment=False,
                     download_name="SimonWater_Bill.pdf")


@app.route("/api/reading", methods=["POST"])
def submit_reading():
    if not _rate_limit("reading_post", 60, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    consumer_id = data.get("consumer_id")
    reading_m3 = data.get("reading_m3")
    if not consumer_id or reading_m3 is None:
        return jsonify({"error": "consumer_id and reading_m3 are required"}), 400
    try:
        reading_m3 = float(reading_m3)
    except (ValueError, TypeError):
        return jsonify({"error": "reading_m3 must be a number"}), 400
    if reading_m3 < 0 or reading_m3 > 1_000_000:
        return jsonify({"error": "reading_m3 out of range"}), 400

    consumer = Consumer.query.get(consumer_id)
    if not consumer:
        return jsonify({"error": "Consumer not found"}), 404
    if not consumer.is_active:
        return jsonify({"error": "Consumer is terminated. Reactivate to submit readings."}), 403

    initial = float(consumer.meter_initial_reading_m3 or 0.0)
    prev = _previous_reading(consumer.id)
    prev_m3 = prev.reading_m3 if prev else None
    baseline = prev_m3 if prev_m3 is not None else initial

    if reading_m3 <= baseline:
        if prev_m3 is not None:
            err = (f"New reading must be greater than the previous reading "
                   f"({prev_m3} M³). You entered {reading_m3} M³.")
        else:
            err = (f"New reading must be greater than the initial meter reading "
                   f"({initial} M³). You entered {reading_m3} M³.")
        return jsonify({"error": err}), 400

    consumption = compute_consumption(reading_m3, prev_m3, initial)
    amount = compute_amount(reading_m3, prev_m3, initial)

    new_reading = MeterReading(
        consumer_id=consumer.id, reading_m3=reading_m3,
        reading_date=date.today(), amount_kes=amount, amount_paid=0.0,
    )
    db.session.add(new_reading)
    db.session.commit()

    try:
        latest = (MeterReading.query.filter_by(consumer_id=consumer.id)
                  .order_by(MeterReading.reading_date.desc(),
                            MeterReading.id.desc()).limit(5).all())
        sync_consumer_to_sheet(consumer, latest)
    except Exception as e:
        app.logger.warning(f"[Sheets sync] {e}")

    return jsonify({"message": "Reading recorded", "reading": {
        "id": new_reading.id, "reading_m3": new_reading.reading_m3,
        "consumption_m3": consumption,
        "reading_date": new_reading.reading_date.isoformat(),
        "amount_kes": new_reading.amount_kes, "balance": new_reading.balance,
    }}), 201


@app.route("/api/consumer/<int:consumer_id>/notify", methods=["POST"])
def notify_consumer(consumer_id):
    if not _rate_limit("notify", 20, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    channel = (data.get("channel") or "").lower().strip()
    if channel not in ("sms", "whatsapp"):
        return jsonify({"error": "channel must be 'sms' or 'whatsapp'"}), 400

    consumer = Consumer.query.get_or_404(consumer_id)
    if not consumer.is_active:
        return jsonify({"error": "Consumer is terminated. No reminders sent."}), 403

    info = get_consumer_status(_all_readings(consumer.id))
    if info["status"] not in ("DUE", "OVERDUE", "OVERDUE_APPROACHING"):
        return jsonify({"error": "No outstanding balance — nothing to notify.",
                        "status": info["status"]}), 400

    cutoff = datetime.utcnow() - timedelta(seconds=NOTIFY_COOLDOWN_SECONDS)
    recent = (NotificationLog.query
              .filter_by(consumer_id=consumer.id, channel=channel, status="sent")
              .filter(NotificationLog.sent_at >= cutoff)
              .order_by(NotificationLog.sent_at.desc()).first())
    if recent:
        elapsed = int((datetime.utcnow() - recent.sent_at).total_seconds())
        wait = max(NOTIFY_COOLDOWN_SECONDS - elapsed, 1)
        return jsonify({"error": f"Please wait {wait}s before resending via {channel}."}), 429

    payload = build_bill_message(consumer, info, channel=channel)
    if channel == "sms":
        if not consumer.contact:
            return jsonify({"error": "Consumer has no contact number on file."}), 400
        result = send_sms(consumer.contact, payload["body"])
        to_value = consumer.contact
    else:  # whatsapp
        if not consumer.contact:
            return jsonify({"error": "Consumer has no contact number on file."}), 400
        result = send_whatsapp(consumer.contact, payload["body"])
        to_value = consumer.contact

    log = NotificationLog(
        consumer_id=consumer.id, channel=channel,
        status="sent" if result.get("ok") else "failed",
        detail=(result.get("error") or result.get("provider_id") or "")[:255],
    )
    db.session.add(log)
    db.session.commit()

    if result.get("ok"):
        return jsonify({"ok": True, "channel": channel,
                        "message": f"{channel.upper()} reminder sent.",
                        "to": to_value}), 200
    return jsonify({"ok": False, "error": result.get("error", "Send failed")}), 502


# ═══════════════════════════════════════════════
#  ADMIN AUTH
# ═══════════════════════════════════════════════

@app.route("/api/admin/whoami")
def admin_whoami():
    setup_required = AdminUser.query.count() == 0
    admin = _current_admin()
    if admin:
        return jsonify({"authenticated": True, "username": admin.username,
                        "setup_required": False})
    return jsonify({"authenticated": False, "setup_required": setup_required})


@app.route("/api/admin/setup", methods=["POST"])
def admin_setup_route():
    if not _rate_limit("setup", 10, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    result = create_first_admin(
        username=data.get("username", ""),
        password=data.get("password", ""),
        phone=data.get("phone", ""),
        email=data.get("email"),
        setup_code=data.get("setup_code", ""),
    )
    if result.get("ok"):
        session.clear()
        session.permanent = True
        session["admin_id"] = result["admin_id"]
        session["admin_username"] = result["username"]
        session["last_activity"] = datetime.utcnow().isoformat()
        return jsonify({"ok": True, "username": result["username"]}), 200
    return jsonify(result), 400


@app.route("/api/admin/login", methods=["POST"])
def admin_login_route():
    if not _rate_limit("login", 20, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    result = admin_login_fn(data.get("username", ""), data.get("password", ""))
    if result.get("ok"):
        session.clear()
        session.permanent = True
        session["admin_id"] = result["admin_id"]
        session["admin_username"] = result["username"]
        session["last_activity"] = datetime.utcnow().isoformat()
        return jsonify({"ok": True, "username": result["username"]}), 200
    return jsonify({"ok": False, "error": result["error"]}), 401


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout_route():
    session.clear()
    return jsonify({"ok": True}), 200


@app.route("/api/admin/forgot", methods=["POST"])
def admin_forgot_route():
    if not _rate_limit("forgot", 10, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    result = request_password_reset(data.get("identifier", ""))
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/admin/reset", methods=["POST"])
def admin_reset_route():
    if not _rate_limit("reset", 10, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    result = confirm_password_reset(
        data.get("token", ""), data.get("otp", ""), data.get("new_password", ""),
    )
    return jsonify(result), (200 if result.get("ok") else 400)


# ═══════════════════════════════════════════════
#  ADMIN — SETTINGS
# ═══════════════════════════════════════════════

@app.route("/api/admin/settings", methods=["GET"])
def admin_get_settings():
    if not _rate_limit("settings_get", 30, 60):
        return _too_many(60)
    u = _require_admin()
    if u:
        return u
    admin = _current_admin()
    return jsonify({
        "username": admin.username,
        "phone": admin.phone,
    })


@app.route("/api/admin/settings/username", methods=["POST"])
def admin_update_username():
    if not _rate_limit("settings_username", 5, 60):
        return _too_many(60)
    u = _require_admin()
    if u:
        return u
    admin = _current_admin()

    data = request.get_json(silent=True) or {}
    new_username = (data.get("new_username") or "").strip().lower()

    if len(new_username) < 3:
        return jsonify({"error": "Username must be at least 3 characters."}), 400
    if len(new_username) > 60:
        return jsonify({"error": "Username too long (max 60)."}), 400
    if not all(c.isalnum() or c in "@._-" for c in new_username):
        return jsonify({"error": "Only letters, digits, and @ . _ - are allowed."}), 400
    if new_username == (admin.username or "").lower():
        return jsonify({"error": "That is already your username."}), 400

    dup = AdminUser.query.filter_by(username=new_username).first()
    if dup and dup.id != admin.id:
        return jsonify({"error": "That username is already taken."}), 409

    admin.username = new_username
    db.session.commit()
    session["admin_username"] = new_username   # keep display in sync

    return jsonify({"ok": True, "username": new_username})


@app.route("/api/admin/settings/phone", methods=["POST"])
def admin_update_phone():
    if not _rate_limit("settings_phone", 5, 60):
        return _too_many(60)
    u = _require_admin()
    if u:
        return u
    admin = _current_admin()

    data = request.get_json(silent=True) or {}
    new_phone = (data.get("new_phone") or "").strip()

    digits = "".join(c for c in new_phone if c.isdigit())
    if not digits:
        return jsonify({"error": "Phone number is required."}), 400

    # Accept: 07XXXXXXXX, 254XXXXXXXXX, 7XXXXXXXX, +254XXXXXXXXX
    if digits.startswith("254") and len(digits) == 12:
        pass
    elif digits.startswith("0") and len(digits) == 10:
        pass
    elif len(digits) == 9:
        pass
    else:
        return jsonify({"error": "Invalid Kenyan phone number."}), 400

    admin.phone = new_phone
    db.session.commit()
    return jsonify({"ok": True, "phone": new_phone})


@app.route("/api/admin/settings/password", methods=["POST"])
def admin_update_password():
    if not _rate_limit("settings_password", 5, 60):
        return _too_many(60)
    u = _require_admin()
    if u:
        return u
    admin = _current_admin()

    data = request.get_json(silent=True) or {}
    current_pw = data.get("current_password") or ""
    new_pw = data.get("new_password") or ""

    if not check_password_hash(admin.password_hash, current_pw):
        return jsonify({"error": "Current password is incorrect."}), 401
    if len(new_pw) < 8:
        return jsonify({"error": "New password must be at least 8 characters."}), 400
    if new_pw == current_pw:
        return jsonify({"error": "New password must be different from current."}), 400

    admin.password_hash = generate_password_hash(new_pw)
    admin.failed_attempts = 0
    admin.locked_until = None
    db.session.commit()

    # Force re-login: clearing the session confirms the new password works.
    session.clear()
    return jsonify({"ok": True, "message": "Password updated. Please log in again."})


# ═══════════════════════════════════════════════
#  ADMIN — consumers
# ═══════════════════════════════════════════════

@app.route("/api/admin/consumer", methods=["POST"])
def admin_create_consumer():
    u = _require_admin()
    if u: return u

    data = request.get_json(silent=True) or {}
    cust_name = (data.get("cust_name") or "").strip()
    acc_name = (data.get("acc_name") or "").strip()
    meter_acc_no = (data.get("meter_acc_no") or "").strip()
    contact = (data.get("contact") or "").strip()
    email = (data.get("email") or "").strip()
    address = (data.get("address") or "").strip()
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    initial_raw = data.get("meter_initial_reading_m3")

    if not (cust_name and acc_name and meter_acc_no and contact):
        return jsonify({"error": "cust_name, acc_name, meter_acc_no, and contact are required."}), 400
    if Consumer.query.filter_by(meter_acc_no=meter_acc_no).first():
        return jsonify({"error": f"Meter account {meter_acc_no} already exists."}), 409

    try:
        lat = float(latitude) if latitude not in (None, "", "null") else None
        lng = float(longitude) if longitude not in (None, "", "null") else None
    except (ValueError, TypeError):
        return jsonify({"error": "latitude/longitude must be numbers."}), 400
    if lat is not None and not (-90 <= lat <= 90):
        return jsonify({"error": "latitude must be between -90 and 90."}), 400
    if lng is not None and not (-180 <= lng <= 180):
        return jsonify({"error": "longitude must be between -180 and 180."}), 400

    try:
        initial = float(initial_raw) if initial_raw not in (None, "", "null") else 0.0
    except (ValueError, TypeError):
        return jsonify({"error": "meter_initial_reading_m3 must be a number."}), 400
    if initial < 0 or initial > 1_000_000:
        return jsonify({"error": "meter_initial_reading_m3 out of range."}), 400

    c = Consumer(cust_name=cust_name, acc_name=acc_name,
                 meter_acc_no=meter_acc_no, contact=contact,
                 email=email or None, address=address or None,
                 latitude=lat, longitude=lng,
                 meter_initial_reading_m3=initial, is_active=True)
    db.session.add(c)
    db.session.commit()

    return jsonify({"ok": True, "consumer": {
        "id": c.id, "cust_name": c.cust_name, "acc_name": c.acc_name,
        "meter_acc_no": c.meter_acc_no, "contact": c.contact,
        "email": c.email, "address": c.address,
        "latitude": c.latitude, "longitude": c.longitude,
        "meter_initial_reading_m3": float(c.meter_initial_reading_m3 or 0.0),
        "is_active": True,
    }}), 201


@app.route("/api/admin/consumer/<int:consumer_id>/update", methods=["POST"])
def admin_update_consumer(consumer_id):
    u = _require_admin()
    if u: return u

    consumer = Consumer.query.get(consumer_id)
    if not consumer:
        return jsonify({"error": "Consumer not found"}), 404

    data = request.get_json(silent=True) or {}
    allowed = ("cust_name", "acc_name", "meter_acc_no", "contact",
               "email", "address", "latitude", "longitude",
               "meter_initial_reading_m3")
    updated = {}
    for f in allowed:
        if f not in data:
            continue
        v = data[f]

        if f == "meter_acc_no":
            new_no = (str(v) or "").strip()
            if not new_no:
                return jsonify({"error": "meter_acc_no cannot be empty."}), 400
            if new_no != consumer.meter_acc_no:
                dup = Consumer.query.filter_by(meter_acc_no=new_no).first()
                if dup and dup.id != consumer.id:
                    return jsonify({"error": f"Meter account {new_no} already exists."}), 409
            setattr(consumer, f, new_no)
        elif f in ("latitude", "longitude"):
            if v in (None, "", "null"):
                setattr(consumer, f, None)
            else:
                try: setattr(consumer, f, float(v))
                except (ValueError, TypeError):
                    return jsonify({"error": f"{f} must be a number."}), 400
        elif f == "meter_initial_reading_m3":
            try:
                setattr(consumer, f, float(v) if v not in (None, "", "null") else 0.0)
            except (ValueError, TypeError):
                return jsonify({"error": "meter_initial_reading_m3 must be a number."}), 400
        else:
            setattr(consumer, f, v)
        updated[f] = v

    if not updated:
        return jsonify({"error": "No valid fields to update"}), 400
    db.session.commit()
    return jsonify({"ok": True, "updated": updated, "consumer": {
        "id": consumer.id, "cust_name": consumer.cust_name,
        "acc_name": consumer.acc_name, "meter_acc_no": consumer.meter_acc_no,
        "contact": consumer.contact, "email": consumer.email,
        "address": consumer.address,
        "latitude": consumer.latitude, "longitude": consumer.longitude,
        "meter_initial_reading_m3": float(consumer.meter_initial_reading_m3 or 0.0),
        "is_active": bool(consumer.is_active),
    }})


# ═══════════════════════════════════════════════
#  PUBLIC — token-based bill PDF (used by WhatsApp)
# ═══════════════════════════════════════════════

# In-memory store: {token: payment_id}
# Tokens live for the lifetime of the process — enough for WhatsApp to fetch
# the PDF within seconds of the send request.
_PUBLIC_PDF_TOKENS = {}
_PUBLIC_BILL_TOKENS = {}


def _register_public_pdf(payment_id: int) -> str:
    """Create a one-time-style token for a payment PDF."""
    token = secrets.token_urlsafe(32)
    _PUBLIC_PDF_TOKENS[token] = payment_id
    return token


@app.route("/public/receipt/<token>.pdf")
def public_receipt_pdf(token):
    """
    Public PDF endpoint — no auth. Token is unguessable (32-byte URL-safe).
    Meta's servers call this URL to fetch the PDF for WhatsApp delivery.
    """
    payment_id = _PUBLIC_PDF_TOKENS.get(token)
    if not payment_id:
        return "Not found.", 404

    log = PaymentLog.query.get(payment_id)
    if not log or not log.receipt_json:
        return "Receipt not available.", 404

    try:
        snapshot = json.loads(log.receipt_json)
        pdf_buf = generate_receipt_pdf(snapshot)
    except Exception:
        app.logger.exception("[Public Receipt] generation failed")
        return "PDF generation failed.", 500

    filename = f"SimonWater_Receipt_{snapshot.get('receipt_no','payment')}.pdf"
    return send_file(pdf_buf, mimetype="application/pdf",
                     as_attachment=False, download_name=filename)


# ═══════════════════════════════════════════════
#  HELPERS — FIFO payment allocation (shared by admin + M-Pesa)
# ═══════════════════════════════════════════════

def _apply_fifo_payment(consumer, amount: float, method: str,
                        reference: str, recorded_by: str):
    """
    Apply a payment to a consumer's bills using FIFO (oldest unpaid first).
    Excess becomes prepayment credit on the newest reading.
    Creates a PaymentLog and receipt snapshot. Returns the PaymentLog.
    """
    readings = (MeterReading.query
                .filter_by(consumer_id=consumer.id)
                .order_by(MeterReading.reading_date.asc(),
                          MeterReading.id.asc())
                .all())
    if not readings:
        raise ValueError("Consumer has no readings on file yet.")

    previous_balance = round(sum(max(r.balance, 0.0) for r in readings), 2)

    remaining = amount
    allocations = []
    for r in readings:
        owed = round((r.amount_kes or 0.0) - (r.amount_paid or 0.0), 2)
        if owed <= 0:
            continue
        apply = round(min(remaining, owed), 2)
        if apply <= 0:
            break
        r.amount_paid = round((r.amount_paid or 0.0) + apply, 2)
        allocations.append({
            "reading_id": r.id,
            "reading_date": _fmt_date(r.reading_date),
            "reading_m3": f"{r.reading_m3:.2f}",
            "amount_kes": _fmt_money(r.amount_kes),
            "applied": _fmt_money(apply),
            "new_balance": _fmt_money(r.amount_kes - r.amount_paid),
            "note": "",
        })
        remaining = round(remaining - apply, 2)
        if remaining <= 0:
            break

    if remaining > 0:
        newest = readings[-1]
        newest.amount_paid = round((newest.amount_paid or 0.0) + remaining, 2)
        allocations.append({
            "reading_id": newest.id,
            "reading_date": _fmt_date(newest.reading_date),
            "reading_m3": f"{newest.reading_m3:.2f}",
            "amount_kes": _fmt_money(newest.amount_kes),
            "applied": _fmt_money(remaining),
            "new_balance": _fmt_money(newest.amount_kes - newest.amount_paid),
            "note": "(Prepayment credit)",
        })
        remaining = 0

    remaining_balance = round(sum(max(r.balance, 0.0) for r in readings), 2)
    prepaid_credit = round(sum(max(-r.balance, 0.0) for r in readings), 2)
    info = get_consumer_status(list(reversed(readings)))
    newest = readings[-1]

    log = PaymentLog(
        consumer_id=consumer.id,
        amount_kes=amount,
        method=method,
        reference=reference or None,
        recorded_by=recorded_by[:60],
    )
    db.session.add(log)
    db.session.flush()

    receipt_no = f"{log.id:06d}"
    created = log.created_at or datetime.utcnow()

    snapshot = {
        "receipt_no": receipt_no,
        "receipt_date": created.strftime("%d %b %Y"),
        "receipt_time": created.strftime("%H:%M"),
        "cust_name": consumer.cust_name,
        "acc_name": consumer.acc_name,
        "meter_acc_no": consumer.meter_acc_no,
        "contact": consumer.contact or "—",
        "email": consumer.email or "—",
        "address": consumer.address or "—",
        "amount_kes": _fmt_money(amount),
        "method": method.capitalize(),
        "reference": reference or "—",
        "recorded_by": recorded_by[:60],
        "previous_balance": _fmt_money(previous_balance),
        "remaining_balance": _fmt_money(remaining_balance),
        "prepaid_credit": _fmt_money(prepaid_credit),
        "status_label": info["label"],
        "last_reading_date": _fmt_date(newest.reading_date),
        "last_reading_m3": f"{newest.reading_m3:.2f}",
        "allocations": allocations,
    }
    log.receipt_json = json.dumps(snapshot)

    # Statement snapshot (last 5 readings, newest first)
    touched_ids = {a["reading_id"] for a in allocations}
    applied_by_id = {a["reading_id"]: a["applied"] for a in allocations}
    statement = []
    for r in reversed(readings[-5:]):
        statement.append({
            "reading_id": r.id,
            "reading_date": _fmt_date(r.reading_date),
            "reading_m3": f"{r.reading_m3:.2f}",
            "amount_kes": _fmt_money(r.amount_kes),
            "paid": _fmt_money(r.amount_paid),
            "applied": applied_by_id.get(r.id, "0.00"),
            "new_balance": _fmt_money(r.amount_kes - r.amount_paid),
            "touched": r.id in touched_ids,
        })
    snapshot["statement"] = statement
    log.receipt_json = json.dumps(snapshot)

    return log


# ═══════════════════════════════════════════════
#  ADMIN — PAYMENTS
# ═══════════════════════════════════════════════

@app.route("/api/admin/consumer/<int:consumer_id>/payment", methods=["POST"])
def admin_record_payment(consumer_id):
    """FIFO: oldest unpaid bill first. Excess becomes prepayment credit.
    Stores a full JSON snapshot for later receipt generation."""
    u = _require_admin()
    if u: return u

    consumer = Consumer.query.get(consumer_id)
    if not consumer:
        return jsonify({"error": "Consumer not found"}), 404

    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount_kes", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "amount_kes must be a number"}), 400
    if amount <= 0:
        return jsonify({"error": "Payment amount must be positive"}), 400
    if amount > 10_000_000:
        return jsonify({"error": "Payment amount out of range"}), 400
    amount = round(amount, 2)

    method = (str(data.get("method") or "cash")).strip().lower()[:30] or "cash"
    reference = (str(data.get("reference") or "")).strip()[:80]

    readings = (MeterReading.query
                .filter_by(consumer_id=consumer.id)
                .order_by(MeterReading.reading_date.asc(),
                          MeterReading.id.asc())
                .all())
    if not readings:
        return jsonify({
            "error": "Cannot record payment: no meter readings on file yet. "
                     "Add the first reading before accepting payment."
        }), 400

    # Snapshot of outstanding BEFORE applying
    previous_balance = round(sum(max(r.balance, 0.0) for r in readings), 2)

    remaining = amount
    allocations = []
    for r in readings:
        owed = round((r.amount_kes or 0.0) - (r.amount_paid or 0.0), 2)
        if owed <= 0:
            continue
        apply = round(min(remaining, owed), 2)
        if apply <= 0:
            break
        r.amount_paid = round((r.amount_paid or 0.0) + apply, 2)
        allocations.append({
            "reading_id": r.id,
            "reading_date": _fmt_date(r.reading_date),
            "reading_m3": f"{r.reading_m3:.2f}",
            "amount_kes": _fmt_money(r.amount_kes),
            "applied": _fmt_money(apply),
            "new_balance": _fmt_money(r.amount_kes - r.amount_paid),
            "note": "",
        })
        remaining = round(remaining - apply, 2)
        if remaining <= 0:
            break

    if remaining > 0:
        newest = readings[-1]
        newest.amount_paid = round((newest.amount_paid or 0.0) + remaining, 2)
        allocations.append({
            "reading_id": newest.id,
            "reading_date": _fmt_date(newest.reading_date),
            "reading_m3": f"{newest.reading_m3:.2f}",
            "amount_kes": _fmt_money(newest.amount_kes),
            "applied": _fmt_money(remaining),
            "new_balance": _fmt_money(newest.amount_kes - newest.amount_paid),
            "note": "(Prepayment credit)",
        })
        remaining = 0

    # State AFTER applying
    remaining_balance = round(sum(max(r.balance, 0.0) for r in readings), 2)
    prepaid_credit = round(sum(max(-r.balance, 0.0) for r in readings), 2)

    # Status AFTER applying
    info = get_consumer_status(list(reversed(readings)))  # newest first
    newest = readings[-1]

    admin = _current_admin()
    recorded_by = (admin.username if admin else "legacy-secret")[:60]

    log = PaymentLog(
        consumer_id=consumer.id,
        amount_kes=amount,
        method=method,
        reference=reference or None,
        recorded_by=recorded_by,
    )
    db.session.add(log)
    db.session.flush()   # get log.id

    receipt_no = f"{log.id:06d}"
    created = log.created_at or datetime.utcnow()

    snapshot = {
        "receipt_no": receipt_no,
        "receipt_date": created.strftime("%d %b %Y"),
        "receipt_time": created.strftime("%H:%M"),
        "cust_name": consumer.cust_name,
        "acc_name": consumer.acc_name,
        "meter_acc_no": consumer.meter_acc_no,
        "contact": consumer.contact or "—",
        "email": consumer.email or "—",
        "address": consumer.address or "—",
        "amount_kes": _fmt_money(amount),
        "method": method.capitalize(),
        "reference": reference or "—",
        "recorded_by": recorded_by,
        "previous_balance": _fmt_money(previous_balance),
        "remaining_balance": _fmt_money(remaining_balance),
        "prepaid_credit": _fmt_money(prepaid_credit),
        "status_label": info["label"],
        "last_reading_date": _fmt_date(newest.reading_date),
        "last_reading_m3": f"{newest.reading_m3:.2f}",
        "allocations": allocations,
    }
    # ── Statement: last 5 readings, newest first, with applied-from-this-payment
    touched_ids = {a["reading_id"] for a in allocations}
    applied_by_id = {a["reading_id"]: a["applied"] for a in allocations}

    statement = []
    for r in reversed(readings[-5:]):   # readings asc; take last 5, reverse → desc
        applied_amt = applied_by_id.get(r.id, "0.00")
        statement.append({
            "reading_id": r.id,
            "reading_date": _fmt_date(r.reading_date),
            "reading_m3": f"{r.reading_m3:.2f}",
            "amount_kes": _fmt_money(r.amount_kes),
            "paid": _fmt_money(r.amount_paid),
            "applied": applied_amt,
            "new_balance": _fmt_money(r.amount_kes - r.amount_paid),
            "touched": r.id in touched_ids,
        })
    snapshot["statement"] = statement

    log.receipt_json = json.dumps(snapshot)

    db.session.commit()

    return jsonify({
        "ok": True,
        "payment_id": log.id,
        "receipt_no": receipt_no,
        "amount_kes": amount,
        "method": method,
        "reference": reference or None,
        "allocations": allocations,
    }), 201


@app.route("/api/admin/payment/<int:payment_id>/receipt.pdf")
def download_receipt(payment_id):
    """Generate and stream the receipt PDF for a specific payment."""
    if not _rate_limit("receipt", 20, 60):
        return _too_many(60)

    u = _require_admin()
    if u: return u

    log = PaymentLog.query.get(payment_id)
    if not log:
        return jsonify({"error": "Payment not found"}), 404

    if not log.receipt_json:
        return jsonify({
            "error": "Receipt not available for this payment. "
                     "Only payments recorded after the receipt feature was enabled have receipts."
        }), 400

    try:
        snapshot = json.loads(log.receipt_json)
    except Exception:
        return jsonify({"error": "Receipt data corrupted."}), 500

    try:
        pdf_buf = generate_receipt_pdf(snapshot)
    except Exception as e:
        app.logger.exception("[Receipt] PDF generation failed")
        return jsonify({"error": f"PDF generation failed: {e}"}), 500

    filename = f"SimonWater_Receipt_{snapshot.get('receipt_no','payment')}.pdf"
    return send_file(
        pdf_buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/receipt/<int:payment_id>")
def receipt_browser_view(payment_id):
    """
    Convenience URL for admin to download a receipt straight from the browser.
    Admin-only — unauthenticated visitors are redirected to the login page.
    """
    if not _current_admin():
        return redirect(f"/admin/login?next=/receipt/{payment_id}")

    log = PaymentLog.query.get(payment_id)
    if not log:
        return "Payment not found.", 404
    if not log.receipt_json:
        return "Receipt not available for this payment.", 400

    try:
        snapshot = json.loads(log.receipt_json)
    except Exception:
        return "Receipt data corrupted.", 500

    try:
        pdf_buf = generate_receipt_pdf(snapshot)
    except Exception:
        app.logger.exception("[Receipt] PDF generation failed")
        return "PDF generation failed.", 500

    filename = f"SimonWater_Receipt_{snapshot.get('receipt_no','payment')}.pdf"
    return send_file(
        pdf_buf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


# ═══════════════════════════════════════════════
#  ADMIN — terminate / reactivate / delete
# ═══════════════════════════════════════════════

@app.route("/api/admin/consumer/<int:consumer_id>/terminate", methods=["POST"])
def admin_terminate_consumer(consumer_id):
    u = _require_admin()
    if u: return u

    consumer = Consumer.query.get(consumer_id)
    if not consumer:
        return jsonify({"error": "Consumer not found"}), 404
    if not consumer.is_active:
        return jsonify({"ok": True, "message": "Already terminated.", "is_active": False}), 200

    consumer.is_active = False
    consumer.terminated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True, "message": f"{consumer.cust_name} terminated.",
                    "is_active": False,
                    "terminated_at": consumer.terminated_at.isoformat()})


@app.route("/api/admin/consumer/<int:consumer_id>/reactivate", methods=["POST"])
def admin_reactivate_consumer(consumer_id):
    u = _require_admin()
    if u: return u

    consumer = Consumer.query.get(consumer_id)
    if not consumer:
        return jsonify({"error": "Consumer not found"}), 404

    consumer.is_active = True
    consumer.terminated_at = None
    db.session.commit()
    return jsonify({"ok": True, "message": f"{consumer.cust_name} reactivated.",
                    "is_active": True})


@app.route("/api/admin/consumer/<int:consumer_id>", methods=["DELETE"])
def admin_delete_consumer(consumer_id):
    u = _require_admin()
    if u: return u

    consumer = Consumer.query.get(consumer_id)
    if not consumer:
        return jsonify({"error": "Consumer not found"}), 404

    name = consumer.cust_name
    PaymentLog.query.filter_by(consumer_id=consumer.id).delete()
    NotificationLog.query.filter_by(consumer_id=consumer.id).delete()
    MeterReading.query.filter_by(consumer_id=consumer.id).delete()
    db.session.delete(consumer)
    db.session.commit()
    return jsonify({"ok": True, "message": f"{name} permanently deleted."})


# ═══════════════════════════════════════════════
#  ADMIN — seed + recompute
# ═══════════════════════════════════════════════

SEED_DATA = []


@app.route("/api/admin/seed", methods=["POST"])
def admin_seed():
    u = _require_admin()
    if u: return u

    replace = request.args.get("replace") == "1"
    if replace:
        PaymentLog.query.delete()
        NotificationLog.query.delete()
        MeterReading.query.delete()
        Consumer.query.delete()
        db.session.commit()
        return jsonify({"ok": True, "wiped": True, "created": 0}), 200

    return jsonify({"created": 0, "skipped": 0, "replaced": False}), 200


@app.route("/api/admin/recompute-amounts", methods=["POST"])
def admin_recompute_amounts():
    u = _require_admin()
    if u: return u

    updated = scanned = 0
    for c in Consumer.query.all():
        initial = float(c.meter_initial_reading_m3 or 0.0)
        readings = (MeterReading.query
                    .filter_by(consumer_id=c.id)
                    .order_by(MeterReading.reading_date.asc(),
                              MeterReading.id.asc()).all())
        prev_m3 = None
        for r in readings:
            scanned += 1
            new_amt = compute_amount(r.reading_m3, prev_m3, initial)
            if abs(new_amt - (r.amount_kes or 0.0)) > 0.001:
                r.amount_kes = new_amt
                updated += 1
            prev_m3 = r.reading_m3

    db.session.commit()
    return jsonify({"ok": True, "readings_scanned": scanned,
                    "readings_updated": updated}), 200


# ═══════════════════════════════════════════════
#  ADMIN — BULK SMS
# ═══════════════════════════════════════════════

@app.route("/api/admin/bulk_sms/overdue", methods=["GET"])
def admin_bulk_sms_overdue():
    """
    List consumers with outstanding balances.
    Query params:
      include_due=1  → also include DUE (default: only OVERDUE + OVERDUE_APPROACHING)
    """
    u = _require_admin()
    if u: return u
    if not _rate_limit("bulk_sms_list", 20, 60):
        return _too_many(60)

    include_due = request.args.get("include_due") == "1"

    allowed = {"OVERDUE", "OVERDUE_APPROACHING"}
    if include_due:
        allowed.add("DUE")

    consumers = (Consumer.query
                 .filter_by(is_active=True)
                 .order_by(Consumer.cust_name.asc())
                 .all())

    out = []
    for c in consumers:
        readings = _all_readings(c.id)
        info = get_consumer_status(readings)
        if info["status"] not in allowed:
            continue
        out.append({
            "id": c.id,
            "cust_name": c.cust_name,
            "meter_acc_no": c.meter_acc_no,
            "contact": c.contact or "",
            "status": info["status"],
            "status_label": info["label"],
            "total_due": round(float(info.get("total_due", 0.0)), 2),
        })

    return jsonify({"consumers": out, "count": len(out)})


@app.route("/api/admin/bulk_sms/send", methods=["POST"])
def admin_bulk_sms_send():
    """
    Send the bill-reminder SMS to a selected list of consumer IDs.
    Body:
      {"consumer_ids": [1, 2, 3]}
    Hard cap: BULK_SMS_MAX_BATCH (default 30) per call.
    """
    u = _require_admin()
    if u: return u
    if not _rate_limit("bulk_sms_send", 3, 300):
        return _too_many(300)

    data = request.get_json(silent=True) or {}
    ids = data.get("consumer_ids") or []

    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "consumer_ids must be a non-empty list"}), 400
    if len(ids) > BULK_SMS_MAX_BATCH:
        return jsonify({
            "error": f"Too many recipients. Max {BULK_SMS_MAX_BATCH} per batch."
        }), 400

    # Fetch consumers + compute info
    items = []
    missing = []
    for cid in ids:
        c = Consumer.query.get(cid)
        if not c or not c.is_active:
            missing.append(cid)
            continue
        readings = _all_readings(c.id)
        info = get_consumer_status(readings)
        if info["status"] not in ("DUE", "OVERDUE", "OVERDUE_APPROACHING"):
            missing.append(cid)
            continue
        items.append({"consumer": c, "info": info})

    if not items:
        return jsonify({"error": "No eligible consumers in the selection."}), 400

    admin = _current_admin()
    operator = (admin.username if admin else "admin")[:60]

    try:
        result = send_bulk_sms(items, operator=operator)
    except Exception as e:
        app.logger.exception("[BulkSMS] failed")
        return jsonify({"error": f"Send failed: {e}"}), 500

    if missing:
        result["skipped_ids"] = missing

    return jsonify(result), 200


# ═══════════════════════════════════════════════
#  M-PESA C2B — PUBLIC CALLBACKS
# ═══════════════════════════════════════════════

def _normalize_msisdn(msisdn: str) -> list:
    """Return possible local formats for a Kenyan MSISDN."""
    digits = "".join(c for c in (msisdn or "") if c.isdigit())
    if not digits:
        return []
    variants = {digits}
    if digits.startswith("254") and len(digits) == 12:
        local = "0" + digits[3:]
        variants.add(local)
        variants.add("+" + digits)
    elif digits.startswith("0") and len(digits) == 10:
        variants.add("254" + digits[1:])
        variants.add("+254" + digits[1:])
    return list(variants)


# ── Safe-path aliases for Safaricom callback registration ──
# Safaricom rejects URLs containing "mpesa", "m-pesa", "safaricom", "exe", "cmd", "sql".
# These aliases use a clean path so the URLs pass Safaricom's validation.

@app.route("/api/pay/c2b/validation", methods=["POST"])
def pay_c2b_validation():
    """Safe-path alias for mpesa_c2b_validation."""
    return mpesa_c2b_validation()


@app.route("/api/pay/c2b/confirmation", methods=["POST"])
def pay_c2b_confirmation():
    """Safe-path alias for mpesa_c2b_confirmation."""
    return mpesa_c2b_confirmation()


@app.route("/api/mpesa/c2b/validation", methods=["POST"])
def mpesa_c2b_validation():
    """
    Safaricom calls this BEFORE processing a payment.
    We always accept (ResponseCode=0) — bill matching happens post-payment.
    """
    if not _mpesa_ip_ok():
        app.logger.warning(f"[M-Pesa] Rejected validation from {_client_ip()}")
        return jsonify({"ResultCode": 1, "ResultDesc": "Forbidden"}), 403
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200


@app.route("/api/mpesa/c2b/confirmation", methods=["POST"])
def mpesa_c2b_confirmation():
    """
    Safaricom calls this AFTER a payment is processed.
    Public endpoint (Safaricom cannot authenticate). Idempotent on TransID.
    """
    if not _mpesa_ip_ok():
        app.logger.warning(f"[M-Pesa] Rejected confirmation from {_client_ip()}")
        return jsonify({"ResultCode": 1, "ResultDesc": "Forbidden"}), 403

    data = request.get_json(silent=True) or {}
    trans_id = (data.get("TransID") or "").strip()
    if not trans_id:
        return jsonify({"ResultCode": 1, "ResultDesc": "Missing TransID"}), 400

    # Idempotency: if we've seen this TransID, silently accept.
    if MpesaLog.query.filter_by(trans_id=trans_id).first():
        return jsonify({"ResultCode": 0, "ResultDesc": "Already processed"}), 200

    try:
        amount = float(data.get("TransAmount") or 0)
    except (ValueError, TypeError):
        amount = 0.0

    bill_ref = (data.get("BillRefNumber") or "").strip()
    msisdn = (data.get("MSISDN") or "").strip()

    log = MpesaLog(
        trans_id=trans_id,
        trans_time=str(data.get("TransTime") or ""),
        trans_amount=amount,
        business_shortcode=str(data.get("BusinessShortCode") or ""),
        bill_ref_number=bill_ref,
        msisdn=msisdn,
        first_name=data.get("FirstName"),
        middle_name=data.get("MiddleName"),
        last_name=data.get("LastName"),
        raw_payload=json.dumps(data),
        status="received",
    )
    db.session.add(log)
    db.session.flush()

    # ── Matching: bill_ref → meter_acc_no, then msisdn → contact ──
    consumer = None
    if bill_ref:
        consumer = Consumer.query.filter_by(meter_acc_no=bill_ref).first()
    if not consumer and msisdn:
        variants = _normalize_msisdn(msisdn)
        if variants:
            consumer = Consumer.query.filter(
                Consumer.contact.in_(variants)
            ).first()

    if not consumer:
        log.status = "unmatched"
        db.session.commit()
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200

    if not consumer.is_active:
        log.status = "unmatched"
        log.consumer_id = consumer.id
        db.session.commit()
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200

    try:
        payment = _apply_fifo_payment(
            consumer, amount, method="mpesa",
            reference=trans_id, recorded_by="M-Pesa C2B",
        )
        log.status = "matched"
        log.consumer_id = consumer.id
        log.payment_log_id = payment.id
        db.session.commit()
    except Exception as e:
        app.logger.exception("[M-Pesa] FIFO failed")
        log.status = "unmatched"
        log.consumer_id = consumer.id
        db.session.commit()
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200

    # SMS confirmation (with retry — does not block the flow)
    try:
        msg = (f"Payment received: KES {amount:,.2f} for {consumer.meter_acc_no}. "
               f"Ref {trans_id}. Thank you.")
        sms_result = send_sms_with_retry(consumer.contact, msg, max_attempts=3)
        if not sms_result.get("ok"):
            app.logger.warning(f"[M-Pesa SMS] final failure: {sms_result.get('error')}")
    except Exception as e:
        app.logger.warning(f"[M-Pesa SMS] exception: {e}")

    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200


# ═══════════════════════════════════════════════
#  M-PESA C2B — ADMIN (register URLs, simulate, list)
# ═══════════════════════════════════════════════

@app.route("/api/admin/mpesa/config", methods=["GET"])
def admin_mpesa_config():
    u = _require_admin()
    if u: return u
    return jsonify(get_mpesa_config())


@app.route("/api/admin/mpesa/register", methods=["POST"])
def admin_mpesa_register():
    u = _require_admin()
    if u: return u
    if not _rate_limit("mpesa_register", 5, 60):
        return _too_many(60)

    base = request.host_url.rstrip("/")
    # Use safe paths (no forbidden words) for Safaricom registration
    conf = f"{base}/api/pay/c2b/confirmation"
    val  = f"{base}/api/pay/c2b/validation"

    result = register_c2b_urls(conf, val)
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error")}), 502
    return jsonify({
        "ok": True,
        "confirmation_url": conf,
        "validation_url": val,
        "response": result.get("response"),
    })


@app.route("/api/admin/mpesa/simulate", methods=["POST"])
def admin_mpesa_simulate():
    u = _require_admin()
    if u: return u
    if not _rate_limit("mpesa_simulate", 10, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    try:
        amount = int(data.get("amount", 10))
    except (ValueError, TypeError):
        return jsonify({"error": "amount must be an integer"}), 400

    msisdn = str(data.get("msisdn") or "254708374149")
    bill_ref = str(data.get("bill_ref_number") or "")

    result = simulate_c2b_payment(amount, msisdn, bill_ref)
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error")}), 502
    return jsonify({"ok": True, "response": result.get("response")})


@app.route("/api/admin/mpesa/payments", methods=["GET"])
def admin_mpesa_payments():
    u = _require_admin()
    if u: return u
    logs = (MpesaLog.query
            .order_by(MpesaLog.received_at.desc())
            .limit(50)
            .all())
    return jsonify({
        "payments": [{
            "id": l.id,
            "trans_id": l.trans_id,
            "trans_time": l.trans_time,
            "amount": l.trans_amount,
            "bill_ref": l.bill_ref_number,
            "msisdn": l.msisdn,
            "first_name": l.first_name,
            "status": l.status,
            "consumer_id": l.consumer_id,
            "payment_log_id": l.payment_log_id,
            "received_at": l.received_at.isoformat() if l.received_at else None,
        } for l in logs],
    })


@app.route("/api/admin/consumers/search", methods=["GET"])
def admin_consumers_search():
    """
    Admin-only search for the M-Pesa assign form.
    Accepts q=<any substring of name, meter, or phone>.
    """
    u = _require_admin()
    if u: return u
    if not _rate_limit("consumer_search", 60, 60):
        return _too_many(60)

    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []})

    pattern = f"%{q}%"
    consumers = (Consumer.query.filter(db.or_(
        Consumer.cust_name.ilike(pattern),
        Consumer.meter_acc_no.ilike(pattern),
        Consumer.contact.ilike(pattern),
    )).limit(15).all())

    return jsonify({
        "results": [{
            "id": c.id,
            "cust_name": c.cust_name,
            "meter_acc_no": c.meter_acc_no,
            "contact": c.contact,
        } for c in consumers],
    })


@app.route("/api/admin/mpesa/assign", methods=["POST"])
def admin_mpesa_assign():
    """Manually assign an unmatched M-Pesa payment to a consumer."""
    u = _require_admin()
    if u: return u

    data = request.get_json(silent=True) or {}
    log_id = data.get("log_id")
    consumer_id = data.get("consumer_id")
    if not log_id or not consumer_id:
        return jsonify({"error": "log_id and consumer_id required"}), 400

    log = MpesaLog.query.get(log_id)
    if not log:
        return jsonify({"error": "M-Pesa log not found"}), 404
    if log.status == "matched":
        return jsonify({"error": "Already assigned"}), 409

    consumer = Consumer.query.get(consumer_id)
    if not consumer:
        return jsonify({"error": "Consumer not found"}), 404

    admin = _current_admin()
    recorded_by = (admin.username if admin else "admin")[:60]

    try:
        payment = _apply_fifo_payment(
            consumer, log.trans_amount, method="mpesa",
            reference=log.trans_id,
            recorded_by=f"{recorded_by} (manual)",
        )
        log.status = "matched"
        log.consumer_id = consumer.id
        log.payment_log_id = payment.id
        db.session.commit()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True, "payment_id": payment.id})


# ═══════════════════════════════════════════════
#  INIT
# ═══════════════════════════════════════════════

with app.app_context():
    db.create_all()
    _ensure_all_columns()
    try:
        start_scheduler(app, Consumer, MeterReading)
    except Exception as e:
        app.logger.warning(f"[Scheduler] failed to start: {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
