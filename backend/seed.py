"""
Seed the database with test consumers and readings.
Run once:  python seed.py
Idempotent — re-running skips existing meter_acc_no.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, timedelta
from app import app
from models import db, Consumer, MeterReading
from billing import compute_amount


TEST_CONSUMERS = [
    {
        "cust_name": "John Kamau",
        "acc_name": "Kamau Household",
        "meter_acc_no": "MTR-0012",
        "contact": "0712345678",
        "email": "john.kamau@example.com",
        "address": "Kikuyu Town, Kiambu County",
        "readings": [
            (32.5, 5,  0.0),     # recent, unpaid       → DUE
            (28.0, 35, 2520.0),  # old, fully paid
            (25.0, 65, 2250.0),
        ],
    },
    {
        "cust_name": "Mary Wanjiku",
        "acc_name": "Wanjiku Residence",
        "meter_acc_no": "MTR-0045",
        "contact": "0723456789",
        "email": "mary.w@example.com",
        "address": "Kikuyu, Near PCEA Church",
        "readings": [
            (40.0, 45, 3600.0),  # old, 100% paid      → CLEARED
            (38.0, 75, 3420.0),
        ],
    },
    {
        "cust_name": "Peter Mwangi",
        "acc_name": "Mwangi Family",
        "meter_acc_no": "MTR-0078",
        "contact": "0734567890",
        "email": "peter.m@example.com",
        "address": "Kikuyu, Gitaru Road",
        "readings": [
            (22.0, 50, 1500.0),  # old, 75.8% paid     → OVERDUE_APPROACHING
            (20.0, 80, 1800.0),
        ],
    },
    {
        "cust_name": "Grace Njeri",
        "acc_name": "Njeri Home",
        "meter_acc_no": "MTR-0102",
        "contact": "0745678901",
        "email": "grace.n@example.com",
        "address": "Kikuyu, Thogoto",
        "readings": [
            (18.0, 40, 2000.0),  # old, overpaid       → PREPAYMENT
            (15.0, 70, 1350.0),
        ],
    },
    {
        "cust_name": "Samuel Ochieng",
        "acc_name": "Ochieng Apartments",
        "meter_acc_no": "MTR-0203",
        "contact": "0756789012",
        "email": "sam.o@example.com",
        "address": "Kikuyu, Ondiri",
        "readings": [
            (55.0, 45, 4400.0),  # old, 88.9% paid     → OVERDUE
            (50.0, 75, 4500.0),
        ],
    },
]


def run():
    with app.app_context():
        db.create_all()
        created = 0
        for spec in TEST_CONSUMERS:
            existing = Consumer.query.filter_by(
                meter_acc_no=spec["meter_acc_no"]
            ).first()
            if existing:
                print(f"  · skip {spec['meter_acc_no']} (exists)")
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

            for m3, days_ago, paid in spec["readings"]:
                db.session.add(MeterReading(
                    consumer_id=c.id,
                    reading_m3=m3,
                    reading_date=date.today() - timedelta(days=days_ago),
                    amount_kes=compute_amount(m3),
                    amount_paid=paid,
                ))
            created += 1
            print(f"  ✓ {spec['cust_name']} ({spec['meter_acc_no']})")

        db.session.commit()
        print(f"\nDone. Created {created} consumer(s).")


if __name__ == "__main__":
    run()
