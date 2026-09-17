import os
import requests
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

from billing import get_consumer_status


# ─── Meta WhatsApp Cloud API config ───
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_ACCESS_TOKEN    = os.environ.get("WA_ACCESS_TOKEN", "")
WA_API_URL = (
    f"https://graph.facebook.com/v20.0/{WA_PHONE_NUMBER_ID}/messages"
    if WA_PHONE_NUMBER_ID else ""
)

# ─── Business payment details (edit these) ───
PAYBILL    = os.environ.get("PAYBILL", "XXXXXX")
COMPANY    = "SIMON FRESH WATER - KIKUYU"


REMINDER_TEMPLATE = """━━━━━━━━━━━━━━━━━━━━━━
💧 *{company}*
━━━━━━━━━━━━━━━━━━━━━━
📋 *Water Bill Reminder*

Dear {consumer_name},

Your account *{cust_name}* (Acc. No: *{meter_acc_no}*)
has an outstanding balance.

📅 Date: {alert_date}
💰 Amount {status_label}: *KES {amount_due:,.2f}*

We kindly remind you to settle your bill to
continue enjoying uninterrupted water service.
Your water is life — let's keep it flowing! 💧

🏦 *Payment Account:*
   Paybill: {paybill}
   Account: {meter_acc_no}

Thank you for being part of the
Simon Fresh Water family. 🙏
━━━━━━━━━━━━━━━━━━━━━━"""


def _normalize_phone(phone: str) -> str:
    """Convert Kenyan local number to E.164 without '+' (Meta API format)."""
    p = "".join(ch for ch in (phone or "") if ch.isdigit())
    if p.startswith("0"):
        p = "254" + p[1:]
    elif p.startswith("254"):
        pass
    elif len(p) == 9:
        p = "254" + p
    return p


def send_whatsapp_reminder(phone: str, message: str) -> bool:
    """
    Send a text message via Meta WhatsApp Cloud API.
    Returns True on success, False otherwise.
    """
    if not WA_API_URL or not WA_ACCESS_TOKEN:
        print("[WhatsApp] Skipped — WA_PHONE_NUMBER_ID / WA_ACCESS_TOKEN not set.")
        return False

    to = _normalize_phone(phone)
    if not to:
        print(f"[WhatsApp] Invalid phone: {phone!r}")
        return False

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(WA_API_URL, json=payload, headers=headers, timeout=20)
        if r.status_code in (200, 201):
            print(f"[WhatsApp] Sent to {to}")
            return True
        print(f"[WhatsApp] Failed {to}: {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[WhatsApp] Exception for {to}: {e}")
        return False


def _build_message(consumer, info: dict) -> str:
    return REMINDER_TEMPLATE.format(
        company=COMPANY,
        consumer_name=consumer.cust_name,
        cust_name=consumer.cust_name,
        meter_acc_no=consumer.meter_acc_no,
        alert_date=datetime.now().strftime("%d %b %Y"),
        status_label=info["label"],
        amount_due=info["total_due"],
        paybill=PAYBILL,
    )


def check_and_send_reminders(app, Consumer, MeterReading):
    """
    Scheduled job — evaluates every consumer.
      • DUE     → reminders on Monday only (1× per week)
      • OVERDUE → reminders on Monday & Thursday (2× per week)
    """
    with app.app_context():
        today_wd = datetime.now().weekday()   # Mon=0 … Sun=6

        consumers = Consumer.query.all()
        sent = 0
        skipped = 0

        for c in consumers:
            readings = (
                MeterReading.query
                .filter_by(consumer_id=c.id)
                .order_by(MeterReading.reading_date.desc())
                .all()
            )
            info = get_consumer_status(readings)
            status = info["status"]

            if status == "DUE" and today_wd != 0:
                skipped += 1
                continue
            if status == "OVERDUE" and today_wd not in (0, 3):
                skipped += 1
                continue
            if status not in ("DUE", "OVERDUE"):
                skipped += 1
                continue

            msg = _build_message(c, info)
            if send_whatsapp_reminder(c.contact, msg):
                sent += 1

        print(f"[Reminders] done — sent={sent}, skipped={skipped}")


def start_scheduler(app, Consumer, MeterReading):
    """Start background scheduler — runs daily at 09:00 server time."""
    scheduler = BackgroundScheduler(daemon=True, timezone="Africa/Nairobi")
    scheduler.add_job(
        func=lambda: check_and_send_reminders(app, Consumer, MeterReading),
        trigger="cron",
        hour=9,
        minute=0,
        id="water_bill_reminders",
        replace_existing=True,
    )
    scheduler.start()
    print("[Scheduler] Started — reminders daily at 09:00 EAT.")
    return scheduler
