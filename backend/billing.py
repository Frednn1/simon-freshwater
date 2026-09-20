from datetime import date

RATE_PER_M3 = 90.0   # KES 90 per M³


def compute_consumption(current_m3: float,
                        previous_m3: float | None = None,
                        initial_m3: float = 0.0) -> float:
    """
    Consumption for a billing period.

    - If previous_m3 is provided:
          consumption = current - previous   (subsequent readings)
    - If previous_m3 is None (first reading of a consumer):
          consumption = current - initial_m3
      where initial_m3 is the meter's starting value at install time
      (0 for new meters, nonzero for pre-existing meters).
    """
    baseline = previous_m3 if previous_m3 is not None else float(initial_m3 or 0.0)
    return round(current_m3 - baseline, 4)


def compute_amount(current_m3: float,
                   previous_m3: float | None = None,
                   initial_m3: float = 0.0) -> float:
    """Bill amount = consumption × KES 90. Server-side only."""
    consumption = compute_consumption(current_m3, previous_m3, initial_m3)
    return round(consumption * RATE_PER_M3, 2)


def get_consumer_status(readings: list) -> dict:
    """
    Overall water bill status.

      - CLEARED             : last reading fully paid, no credit.
      - PREPAYMENT          : last reading cleared AND extra credit exists.
      - DUE                 : last reading has unpaid balance, age <= 1 month.
      - OVERDUE             : last reading unpaid, age > 1 month, paid >= 80%.
      - OVERDUE_APPROACHING : last reading unpaid, age > 1 month, paid < 80%.
    """
    if not readings:
        return {
            "status": "NO_READINGS", "label": "No Readings",
            "total_due": 0.0, "total_prepaid": 0.0,
            "last_reading_date": None, "age_days": 0, "latest_balance": 0.0,
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
        "status": status, "label": label,
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
