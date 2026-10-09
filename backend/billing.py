from datetime import date, datetime, timedelta, timezone

# ─── Nairobi timezone (UTC+3, no DST) ───
# Age computed against Nairobi calendar date so overdue reflects Kenya local time.
_NAIROBI_TZ = timezone(timedelta(hours=3), name="Africa/Nairobi")


def _nairobi_today() -> date:
    """Today's date in Africa/Nairobi."""
    return datetime.now(_NAIROBI_TZ).date()

RATE_PER_M3 = 150.0  # KES 150 per M³


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
    Overall water bill status — driven solely by the latest reading.

      - PREPAYMENT : latest reading balance is negative (overpaid).
      - CLEARED    : latest reading balance is exactly 0.
      - DUE        : latest reading balance > 0 and age <= 20 days.
      - OVERDUE    : latest reading balance > 0 and age > 20 days.

    The 20-day window runs from the reading date.
    """
    if not readings:
        return {
            "status": "NO_READINGS", "label": "No Readings",
            "total_due": 0.0, "total_prepaid": 0.0,
            "last_reading_date": None, "age_days": 0, "latest_balance": 0.0,
        }

    latest = readings[0]
    balance = latest.balance
    age_days = (_nairobi_today() - latest.reading_date).days

    total_credit = sum(max(r.amount_paid - r.amount_kes, 0.0) for r in readings)
    total_outstanding = sum(max(r.balance, 0.0) for r in readings)

    if balance < 0:
        status, label = "PREPAYMENT", "Prepayment"
    elif balance == 0:
        status, label = "CLEARED", "Cleared"
    elif age_days <= 20:
        status, label = "DUE", "Due"
    else:
        status, label = "OVERDUE", "Overdue"

    return {
        "status": status, "label": label,
        "total_due": round(total_outstanding, 2),
        "total_prepaid": round(total_credit, 2),
        "last_reading_date": latest.reading_date.isoformat(),
        "age_days": age_days,
        "latest_balance": round(balance, 2),
    }


def get_reading_bill_status(reading, balance_override: float | None = None) -> str:
    """Per-reading bill label (used inside the individual bill view and the
    consumer page's readings table).

    balance_override — pass a display balance (post carry-forward) to have
    the label reflect the effective status rather than the raw
    amount_kes − amount_paid. Defaults to the raw balance.
    """
    balance = balance_override if balance_override is not None else reading.balance
    if balance < 0:
        return "Prepayment"
    if balance == 0:
        return "Cleared"
    age_days = (_nairobi_today() - reading.reading_date).days
    if age_days <= 20:
        return "Due"
    return "Overdue"
