from datetime import date

RATE_PER_M3 = 90.0   # KES 90 per M³


def compute_consumption(current_m3: float, previous_m3: float | None) -> float:
    """
    Consumption for a billing period =
        current cumulative meter reading − previous cumulative meter reading.

    A baseline reading (no previous) has zero consumption — no bill until the
    next reading establishes a delta.
    """
    if previous_m3 is None:
        return 0.0
    return round(current_m3 - previous_m3, 4)


def compute_amount(current_m3: float,
                   previous_m3: float | None = None) -> float:
    """
    Bill amount = consumption × KES 90.

    - Baseline reading (previous_m3 is None) → KES 0.
    - Otherwise → (current − previous) × RATE_PER_M3.

    Server-side only. Callers must supply the previous cumulative reading
    (the most recent reading stored for that consumer, ordered by date then id).
    """
    consumption = compute_consumption(current_m3, previous_m3)
    return round(consumption * RATE_PER_M3, 2)


def get_consumer_status(readings: list) -> dict:
    """
    Determine overall water bill status.

    Rules (consumption-based billing — bill incurred AFTER utility consumed):
      - CLEARED             : last reading fully paid, no credit.
      - PREPAYMENT          : last reading cleared AND extra credit exists.
      - DUE                 : last reading has unpaid balance, age <= 1 month.
      - OVERDUE             : last reading unpaid, age > 1 month, paid >= 80%.
      - OVERDUE_APPROACHING : last reading unpaid, age > 1 month, paid < 80%.
    """
    if not readings:
        return {
            "status": "NO_READINGS",
            "label": "No Readings",
            "total_due": 0.0,
            "total_prepaid": 0.0,
            "last_reading_date": None,
            "age_days": 0,
            "latest_balance": 0.0,
        }

    latest = readings[0]
    balance = latest.balance
    age_days = (date.today() - latest.reading_date).days

    total_credit = sum(max(r.amount_paid - r.amount_kes, 0.0) for r in readings)
    total_outstanding = sum(max(r.balance, 0.0) for r in readings)

    if balance <= 0 and total_credit > 0:
        status, label = "PREPAYMENT", "Prepayment"
    elif balance <= 0 and total_credit == 0:
        status, label = "CLEARED", "Cleared"
    elif age_days <= 30:
        status, label = "DUE", "Due"
    else:
        pct_paid = (latest.amount_paid / latest.amount_kes * 100
                    if latest.amount_kes > 0 else 0)
        if pct_paid >= 80:
            status, label = "OVERDUE", "Overdue"
        else:
            status, label = "OVERDUE_APPROACHING", "Overdue (Approaching)"

    return {
        "status": status,
        "label": label,
        "total_due": round(total_outstanding, 2),
        "total_prepaid": round(total_credit, 2),
        "last_reading_date": latest.reading_date.isoformat(),
        "age_days": age_days,
        "latest_balance": round(balance, 2),
    }


def get_reading_bill_status(reading) -> str:
    """Per-reading bill label (used inside the individual bill view)."""
    if reading.balance <= 0:
        return "Cleared"
    age_days = (date.today() - reading.reading_date).days
    if age_days <= 30:
        return "Due"
    pct_paid = (reading.amount_paid / reading.amount_kes * 100
                if reading.amount_kes > 0 else 0)
    return "Overdue" if pct_paid >= 80 else "Overdue (Approaching)"
