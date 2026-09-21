import os
from datetime import datetime, date, timedelta
from collections import deque
from threading import Lock
from time import time as _time_now

from flask import Flask, request, jsonify, send_from_directory, session
from flask_cors import CORS
from sqlalchemy import text, inspect

from models import db, Consumer, MeterReading, NotificationLog, AdminUser
from billing import (
    compute_amount, compute_consumption,
    get_consumer_status, get_reading_bill_status,
)
from sheets import sync_consumer_to_sheet
from reminders import start_scheduler
from notifications import (
    send_sms, send_email, build_bill_message, get_available_channels,
)
from admin_auth import (
    create_first_admin, login as admin_login_fn,
    request_password_reset, confirm_password_reset,
)


BACKEND_DIR  = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(os.path.dirname(BACKEND_DIR), "frontend")

NOTIFY_COOLDOWN_SECONDS = int(os.environ.get("NOTIFY_COOLDOWN_SECONDS", "300"))


app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
CORS(app, supports_credentials=True)


DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///simon_freshwater.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# ─── Connection pool hardening ───
# pool_pre_ping: SQLAlchemy sends a lightweight SELECT 1 before handing the
#   connection to the request. If it's dead, it's discarded and a new one is
#   opened. Prevents "SSL error: decryption failed" from stale connections.
# pool_recycle:  any connection older than 3 minutes is proactively replaced,
#   staying well under Render's proxy idle timeout.
# Skipped for SQLite (local dev) — SQLite has no network so none of this applies.
if not DATABASE_URL.startswith("sqlite"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 180,
        "pool_size": 5,
        "max_overflow": 5,
        "pool_timeout": 30,
    }
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-me")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))

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
#  RATE LIMITER (in-memory, per IP + per scope)
# ═══════════════════════════════════════════════

_rate_buckets = {}
_rate_lock = Lock()


def _client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _rate_limit(scope: str, limit: int, window: int) -> bool:
    """Return True if allowed, False if the caller has exceeded the limit."""
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


def _current_admin():
    aid = session.get("admin_id")
    if not aid:
        return None
    a = AdminUser.query.get(aid)
    return a if (a and a.is_active) else None


def _require_admin():
    if _current_admin():
        return None
    secret = request.headers.get("X-Admin-Secret", "")
    expected = os.environ.get("ADMIN_SEED_SECRET", "")
    if expected and secret and secret == expected:
        return None
    return jsonify({"error": "Unauthorized. Please log in."}), 401


def _ensure_consumer_columns():
    insp = inspect(db.engine)
    existing = {c["name"] for c in insp.get_columns("consumers")}
    alters = []
    if "latitude" not in existing:
        alters.append("ADD COLUMN latitude DOUBLE PRECISION NULL")
    if "longitude" not in existing:
        alters.append("ADD COLUMN longitude DOUBLE PRECISION NULL")
    if "is_active" not in existing:
        alters.append("ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE")
    if "terminated_at" not in existing:
        alters.append("ADD COLUMN terminated_at TIMESTAMP NULL")
    if "meter_initial_reading_m3" not in existing:
        alters.append("ADD COLUMN meter_initial_reading_m3 DOUBLE PRECISION NOT NULL DEFAULT 0")
    for alt in alters:
        db.session.execute(text(f"ALTER TABLE consumers {alt}"))
    if alters:
        db.session.commit()
        app.logger.info(f"[Migration] Applied {len(alters)} column(s) to consumers")


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

@app.route("/style.css")
def styles():
    return send_from_directory(FRONTEND_DIR, "style.css")

@app.route("/app.js")
def appjs():
    return send_from_directory(FRONTEND_DIR, "app.js")


# ═══════════════════════════════════════════════
#  API — HEALTH
# ═══════════════════════════════════════════════

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
#  API — CONSUMER (public for direct links)
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
    info = get_consumer_status(latest5)

    last_sms = (NotificationLog.query
                .filter_by(consumer_id=consumer.id, channel="sms", status="sent")
                .order_by(NotificationLog.sent_at.desc()).first())
    last_email = (NotificationLog.query
                  .filter_by(consumer_id=consumer.id, channel="email", status="sent")
                  .order_by(NotificationLog.sent_at.desc()).first())

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
    if channel not in ("sms", "email"):
        return jsonify({"error": "channel must be 'sms' or 'email'"}), 400

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
    else:
        if not consumer.email:
            return jsonify({"error": "Consumer has no email on file."}), 400
        result = send_email(consumer.email, payload["subject"],
                            payload["body_text"], payload.get("body_html"))
        to_value = consumer.email

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
#  ADMIN — consumers (protected)
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
    allowed = ("cust_name", "acc_name", "contact", "email", "address",
               "latitude", "longitude", "meter_initial_reading_m3")
    updated = {}
    for f in allowed:
        if f in data:
            v = data[f]
            if f in ("latitude", "longitude"):
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
        "contact": consumer.contact, "email": consumer.email,
        "meter_initial_reading_m3": float(consumer.meter_initial_reading_m3 or 0.0),
        "is_active": bool(consumer.is_active),
    }})


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
    NotificationLog.query.filter_by(consumer_id=consumer.id).delete()
    MeterReading.query.filter_by(consumer_id=consumer.id).delete()
    db.session.delete(consumer)
    db.session.commit()
    return jsonify({"ok": True, "message": f"{name} permanently deleted."})


# ═══════════════════════════════════════════════
#  ADMIN — SEED (empty) + RECOMPUTE
# ═══════════════════════════════════════════════

SEED_DATA = []


@app.route("/api/admin/seed", methods=["POST"])
def admin_seed():
    u = _require_admin()
    if u: return u

    replace = request.args.get("replace") == "1"
    if replace:
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
#  INIT
# ═══════════════════════════════════════════════

with app.app_context():
    db.create_all()
    _ensure_consumer_columns()
    try:
        start_scheduler(app, Consumer, MeterReading)
    except Exception as e:
        app.logger.warning(f"[Scheduler] failed to start: {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
