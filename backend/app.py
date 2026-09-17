import os
from datetime import datetime, date, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

from models import db, Consumer, MeterReading
from billing import (
    compute_amount,
    compute_consumption,
    get_consumer_status,
    get_reading_bill_status,
)
from sheets import sync_consumer_to_sheet
from reminders import start_scheduler


# ─── Paths ───
BACKEND_DIR  = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(os.path.dirname(BACKEND_DIR), "frontend")


# ─── App setup ───
app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
CORS(app)


# ─── Database ───
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///simon_freshwater.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-me")

db.init_app(app)


# ═══════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════

def _previous_reading(consumer_id: int, exclude_id: int | None = None):
    """Most recent reading for a consumer, optionally excluding one id."""
    q = MeterReading.query.filter_by(consumer_id=consumer_id)
    if exclude_id is not None:
        q = q.filter(MeterReading.id != exclude_id)
    return q.order_by(
        MeterReading.reading_date.desc(),
        MeterReading.id.desc(),
    ).first()


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


@app.route("/style.css")
def styles():
    return send_from_directory(FRONTEND_DIR, "style.css")


@app.route("/app.js")
def appjs():
    return send_from_directory(FRONTEND_DIR, "app.js")


# ═══════════════════════════════════════════════
#  API ROUTES
# ═══════════════════════════════════════════════

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})


@app.route("/api/search")
def search_consumer():
    """Search by Cust. Name OR Meter Acc. No. Parameterised — SQL-injection safe."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []}), 400

    pattern = f"%{q}%"
    consumers = (
        Consumer.query
        .filter(db.or_(
            Consumer.cust_name.ilike(pattern),
            Consumer.meter_acc_no.ilike(pattern),
        ))
        .limit(20)
        .all()
    )

    results = []
    for c in consumers:
        readings = (
            MeterReading.query
            .filter_by(consumer_id=c.id)
            .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
            .all()
        )
        info = get_consumer_status(readings)
        results.append({
            "id": c.id,
            "cust_name": c.cust_name,
            "acc_name": c.acc_name,
            "meter_acc_no": c.meter_acc_no,
            "status": info["status"],
            "status_label": info["label"],
        })

    return jsonify({"results": results})


@app.route("/api/consumer/<int:consumer_id>")
def get_consumer_details(consumer_id):
    """Full consumer details + last 5 readings + overall status."""
    consumer = Consumer.query.get_or_404(consumer_id)

    # Fetch all readings (ascending for consumption computation), then slice.
    all_readings = (
        MeterReading.query
        .filter_by(consumer_id=consumer.id)
        .order_by(MeterReading.reading_date.asc(), MeterReading.id.asc())
        .all()
    )

    # Build a lookup: id -> consumption (current − previous)
    consumption_by_id = {}
    prev_m3 = None
    for r in all_readings:
        consumption_by_id[r.id] = compute_consumption(r.reading_m3, prev_m3)
        prev_m3 = r.reading_m3

    # Latest 5 for display (descending)
    latest5 = list(reversed(all_readings))[:5]
    info = get_consumer_status(latest5)

    return jsonify({
        "consumer": {
            "id": consumer.id,
            "cust_name": consumer.cust_name,
            "acc_name": consumer.acc_name,
            "meter_acc_no": consumer.meter_acc_no,
            "contact": consumer.contact,
            "email": consumer.email,
            "address": consumer.address,
        },
        "readings": [
            {
                "id": r.id,
                "reading_m3": r.reading_m3,
                "consumption_m3": consumption_by_id.get(r.id, 0.0),
                "reading_date": r.reading_date.isoformat(),
                "amount_kes": r.amount_kes,
                "amount_paid": r.amount_paid,
                "balance": r.balance,
                "bill_status": get_reading_bill_status(r),
            }
            for r in latest5
        ],
        "overall_status": info,
    })


@app.route("/api/reading/<int:reading_id>")
def get_reading_bill(reading_id):
    """Individual water-bill view for a specific meter reading."""
    reading = MeterReading.query.get_or_404(reading_id)
    consumer = reading.consumer

    # Compute consumption for this specific reading
    prev = _previous_reading(consumer.id, exclude_id=reading.id)
    prev_m3 = None
    if prev and (prev.reading_date < reading.reading_date
                 or (prev.reading_date == reading.reading_date
                     and prev.id < reading.id)):
        prev_m3 = prev.reading_m3
    consumption = compute_consumption(reading.reading_m3, prev_m3)

    return jsonify({
        "consumer": {
            "cust_name": consumer.cust_name,
            "acc_name": consumer.acc_name,
            "meter_acc_no": consumer.meter_acc_no,
            "contact": consumer.contact,
            "address": consumer.address,
        },
        "reading": {
            "id": reading.id,
            "reading_m3": reading.reading_m3,
            "consumption_m3": consumption,
            "reading_date": reading.reading_date.isoformat(),
            "amount_kes": reading.amount_kes,
            "amount_paid": reading.amount_paid,
            "balance": reading.balance,
            "bill_status": get_reading_bill_status(reading),
        },
    })


@app.route("/api/reading", methods=["POST"])
def submit_reading():
    """
    Accept new cumulative meter reading.
    Bill = (current − previous) × KES 90. Server computes amount.
    Rejects if new reading <= previous reading (meter cannot roll backwards).
    """
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

    # ── Fetch previous reading (most recent) ──
    prev = _previous_reading(consumer.id)
    prev_m3 = prev.reading_m3 if prev else None

    # ── Enforce monotonic increase ──
    if prev_m3 is not None and reading_m3 <= prev_m3:
        return jsonify({
            "error": (
                f"New reading must be greater than the previous reading "
                f"({prev_m3} M³). You entered {reading_m3} M³."
            )
        }), 400

    # ── Compute consumption & bill server-side ──
    consumption = compute_consumption(reading_m3, prev_m3)
    amount = compute_amount(reading_m3, prev_m3)

    new_reading = MeterReading(
        consumer_id=consumer.id,
        reading_m3=reading_m3,
        reading_date=date.today(),
        amount_kes=amount,
        amount_paid=0.0,
    )
    db.session.add(new_reading)
    db.session.commit()

    # Best-effort Google Sheets sync
    try:
        latest = (
            MeterReading.query
            .filter_by(consumer_id=consumer.id)
            .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
            .limit(5)
            .all()
        )
        sync_consumer_to_sheet(consumer, latest)
    except Exception as e:
        app.logger.warning(f"[Sheets sync] {e}")

    return jsonify({
        "message": "Reading recorded",
        "reading": {
            "id": new_reading.id,
            "reading_m3": new_reading.reading_m3,
            "consumption_m3": consumption,
            "reading_date": new_reading.reading_date.isoformat(),
            "amount_kes": new_reading.amount_kes,
            "balance": new_reading.balance,
        },
    }), 201


# ═══════════════════════════════════════════════
#  ADMIN — protected seed endpoint
# ═══════════════════════════════════════════════

# Cumulative meter readings (monotonic ascending when sorted oldest→newest).
# Tuple = (meter_value, days_ago, amount_paid_kes)
SEED_DATA = [
    {
        "cust_name": "John Kamau",
        "acc_name": "Kamau Household",
        "meter_acc_no": "MTR-0012",
        "contact": "0712345678",
        "email": "john.kamau@example.com",
        "address": "Kikuyu Town, Kiambu County",
        # oldest → newest: 21 → 25 → 28 → 32 → 35 → 36
        "readings": [
            (21.0, 140, 0.0),
            (25.0, 110, 360.0),
            (28.0, 80,  270.0),
            (32.0, 50,  360.0),
            (35.0, 20,  0.0),   # unpaid → status DUE (age 20d)
            (36.0, 2,   0.0),   # unpaid → status DUE (age 2d) — most recent
        ],
    },
    {
        "cust_name": "Mary Wanjiku",
        "acc_name": "Wanjiku Residence",
        "meter_acc_no": "MTR-0045",
        "contact": "0723456789",
        "email": "mary.w@example.com",
        "address": "Kikuyu, Near PCEA Church",
        # 38 → 40 (diff 2 → KES 180), fully paid
        "readings": [
            (38.0, 75, 0.0),
            (40.0, 45, 180.0),   # paid in full → CLEARED
        ],
    },
    {
        "cust_name": "Peter Mwangi",
        "acc_name": "Mwangi Family",
        "meter_acc_no": "MTR-0078",
        "contact": "0734567890",
        "email": "peter.m@example.com",
        "address": "Kikuyu, Gitaru Road",
        # 20 → 22 (diff 2 → KES 180), paid 100 (55%) older than 30 days
        "readings": [
            (20.0, 80, 0.0),
            (22.0, 50, 100.0),   # OVERDUE_APPROACHING
        ],
    },
    {
        "cust_name": "Grace Njeri",
        "acc_name": "Njeri Home",
        "meter_acc_no": "MTR-0102",
        "contact": "0745678901",
        "email": "grace.n@example.com",
        "address": "Kikuyu, Thogoto",
        # 15 → 18 (diff 3 → KES 270), overpaid → PREPAYMENT
        "readings": [
            (15.0, 70, 0.0),
            (18.0, 40, 2000.0),  # PREPAYMENT (credit = 2000 − 270 = 1730)
        ],
    },
    {
        "cust_name": "Samuel Ochieng",
        "acc_name": "Ochieng Apartments",
        "meter_acc_no": "MTR-0203",
        "contact": "0756789012",
        "email": "sam.o@example.com",
        "address": "Kikuyu, Ondiri",
        # 50 → 55 (diff 5 → KES 450), paid 400 (88.9%), older than 30 days
        "readings": [
            (50.0, 75, 0.0),
            (55.0, 45, 400.0),   # OVERDUE
        ],
    },
]


@app.route("/api/admin/seed", methods=["POST"])
def admin_seed():
    """
    One-time seed — protected by X-Admin-Secret header.
    Query param ?replace=1 wipes all consumers and reseeds from scratch.
    """
    secret = request.headers.get("X-Admin-Secret", "")
    expected = os.environ.get("ADMIN_SEED_SECRET", "")
    if not expected or secret != expected:
        return jsonify({"error": "Unauthorized"}), 401

    replace = request.args.get("replace") == "1"

    if replace:
        MeterReading.query.delete()
        Consumer.query.delete()
        db.session.commit()

    created = 0
    skipped = 0

    for spec in SEED_DATA:
        if Consumer.query.filter_by(meter_acc_no=spec["meter_acc_no"]).first():
            skipped += 1
            continue

        c = Consumer(
            cust_name=spec["cust_name"],
            acc_name=spec["acc_name"],
            meter_acc_no=spec["meter_acc_no"],
            contact=spec["contact"],
            email=spec["email"],
            address=spec["address"],
        )
        db.session.add(c)
        db.session.flush()

        # Sort oldest → newest (larger days_ago = older)
        ordered = sorted(spec["readings"], key=lambda x: -x[1])

        prev_m3 = None
        for m3, days_ago, paid in ordered:
            amount = compute_amount(m3, prev_m3)
            db.session.add(MeterReading(
                consumer_id=c.id,
                reading_m3=m3,
                reading_date=date.today() - timedelta(days=days_ago),
                amount_kes=amount,
                amount_paid=paid,
            ))
            prev_m3 = m3

        created += 1

    db.session.commit()
    return jsonify({"created": created, "skipped": skipped, "replaced": replace}), 200


# ═══════════════════════════════════════════════
#  INIT — DB + Scheduler
# ═══════════════════════════════════════════════

with app.app_context():
    db.create_all()
    try:
        start_scheduler(app, Consumer, MeterReading)
    except Exception as e:
        app.logger.warning(f"[Scheduler] failed to start: {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
