import os
from datetime import datetime, date
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

from models import db, Consumer, MeterReading
from billing import compute_amount, get_consumer_status, get_reading_bill_status
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
#  FRONTEND ROUTES (serve static HTML)
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
            .order_by(MeterReading.reading_date.desc())
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

    readings = (
        MeterReading.query
        .filter_by(consumer_id=consumer.id)
        .order_by(MeterReading.reading_date.desc())
        .limit(5)
        .all()
    )
    info = get_consumer_status(readings)

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
                "reading_date": r.reading_date.isoformat(),
                "amount_kes": r.amount_kes,
                "amount_paid": r.amount_paid,
                "balance": r.balance,
                "bill_status": get_reading_bill_status(r),
            }
            for r in readings
        ],
        "overall_status": info,
    })


@app.route("/api/reading/<int:reading_id>")
def get_reading_bill(reading_id):
    """Individual water-bill view for a specific meter reading."""
    reading = MeterReading.query.get_or_404(reading_id)
    consumer = reading.consumer

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
            "reading_date": reading.reading_date.isoformat(),
            "amount_kes": reading.amount_kes,
            "amount_paid": reading.amount_paid,
            "balance": reading.balance,
            "bill_status": get_reading_bill_status(reading),
        },
    })


@app.route("/api/reading", methods=["POST"])
def submit_reading():
    """Accept new meter reading. Server computes amount — client cannot tamper."""
    data = request.get_json(silent=True) or {}
    consumer_id = data.get("consumer_id")
    reading_m3 = data.get("reading_m3")

    if not consumer_id or reading_m3 is None:
        return jsonify({"error": "consumer_id and reading_m3 are required"}), 400

    try:
        reading_m3 = float(reading_m3)
    except (ValueError, TypeError):
        return jsonify({"error": "reading_m3 must be a number"}), 400
    if reading_m3 < 0 or reading_m3 > 100000:
        return jsonify({"error": "reading_m3 out of range"}), 400

    consumer = Consumer.query.get(consumer_id)
    if not consumer:
        return jsonify({"error": "Consumer not found"}), 404

    amount = compute_amount(reading_m3)   # server-side

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
            .order_by(MeterReading.reading_date.desc())
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
            "reading_date": new_reading.reading_date.isoformat(),
            "amount_kes": new_reading.amount_kes,
            "balance": new_reading.balance,
        },
    }), 201


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
