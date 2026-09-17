"""
Seed the LOCAL database (SQLite) with test consumers and readings.

For PRODUCTION seeding, use the protected endpoint instead:
    curl -X POST "$SERVICE/api/admin/seed?replace=1" \
         -H "X-Admin-Secret: $ADMIN_SEED_SECRET"

Run locally:  python seed.py
Idempotent — re-running skips existing meter_acc_no.
Use `python seed.py --replace` to wipe and reseed from scratch.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, timedelta
from app import app
from models import db, Consumer, MeterReading
from billing import compute_amount


# ─────────────────────────────────────────────
#  Test consumers — cumulative meter readings
#  Tuple: (meter_value_m3, days_ago, amount_paid_kes)
#  Sorted oldest → newest by days_ago DESC within code.
# ─────────────────────────────────────────────
TEST_CONSUMERS = [
    {
        "cust_name": "John Kamau",
        "acc_name": "Kamau Household",
        "meter_acc_no": "MTR-0012",
        "contact": "0712345678",
        "email": "john.kamau@example.com",
        "address": "Kikuyu Town, Kiambu County",
        # 21 → 25 → 28 → 32 → 35 → 36  (diffs: —, 4, 3, 4, 3, 1)
        "readings": [
            (21.0, 140, 0.0),
            (25.0, 110, 360.0),
            (28.0, 80,  270.0),
            (32.0, 50,  360.0),
            (35.0, 20,  0.0),     # unpaid, 20 days → DUE
            (36.0, 2,   0.0),     # unpaid, 2 days  → DUE (latest)
        ],
    },
    {
        "cust_name": "Mary Wanjiku",
        "acc_name": "Wanjiku Residence",
        "meter_acc_no": "MTR-0045",
        "contact": "0723456789",
        "email": "mary.w@example.com",
        "address": "Kikuyu, Near PCEA Church",
        # 38 → 40  (diff 2 → KES 180), paid in full → CLEARED
        "readings": [
            (38.0, 75, 0.0),
            (40.0, 45, 180.0),
        ],
    },
    {
        "cust_name": "Peter Mwangi",
        "acc_name": "Mwangi Family",
        "meter_acc_no": "MTR-0078",
        "contact": "0734567890",
        "email": "peter.m@example.com",
        "address": "Kikuyu, Gitaru Road",
        # 20 → 22  (diff 2 → KES 180), paid 100 (55%) > 30 days → OVERDUE_APPROACHING
        "readings": [
            (20.0, 80, 0.0),
            (22.0, 50, 100.0),
        ],
    },
    {
        "cust_name": "Grace Njeri",
        "acc_name": "Njeri Home",
        "meter_acc_no": "MTR-0102",
        "contact": "0745678901",
        "email": "grace.n@example.com",
        "address": "Kikuyu, Thogoto",
        # 15 → 18  (diff 3 → KES 270), overpaid → PREPAYMENT
        "readings": [
            (15.0, 70, 0.0),
            (18.0, 40, 2000.0),
        ],
    },
    {
        "cust_name": "Samuel Ochieng",
        "acc_name": "Ochieng Apartments",
        "meter_acc_no": "MTR-0203",
        "contact": "0756789012",
        "email": "sam.o@example.com",
        "address": "Kikuyu, Ondiri",
        # 50 → 55  (diff 5 → KES 450), paid 400 (88.9%) > 30 days → OVERDUE
        "readings": [
            (50.0, 75, 0.0),
            (55.0, 45, 400.0),
        ],
    },
]


def _existing_readings(consumer_id: int):
    return (
        MeterReading.query
        .filter_by(consumer_id=consumer_id)
        .order_by(MeterReading.reading_date.asc(), MeterReading.id.asc())
        .all()
    )


def _seed_consumer(spec: dict) -> bool:
    """Insert one consumer + readings. Returns True if created, False if skipped."""
    if Consumer.query.filter_by(meter_acc_no=spec["meter_acc_no"]).first():
        print(f"  · skip {spec['meter_acc_no']} (exists)")
        return False

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

    print(f"  ✓ {spec['cust_name']} ({spec['meter_acc_no']})")
    return True


def run(replace: bool = False):
    with app.app_context():
        db.create_all()

        if replace:
            print("→ Wiping consumers and readings (replace mode)...")
            MeterReading.query.delete()
            Consumer.query.delete()
            db.session.commit()

        created = 0
        for spec in TEST_CONSUMERS:
            if _seed_consumer(spec):
                created += 1

        db.session.commit()
        print(f"\nDone. Created {created} consumer(s).")


if __name__ == "__main__":
    replace_flag = "--replace" in sys.argv
    run(replace=replace_flag)
