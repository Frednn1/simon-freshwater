"""
Bulk SMS sender.

SMSGate has no dedicated bulk endpoint — we send one message per recipient
through the same /messages API, sequentially with a configurable delay
between each. This keeps the API happy and avoids rate-limit blocks.

Usage (from app.py):
    items = [{"consumer": <Consumer>, "info": {...}}, ...]
    result = send_bulk_sms(items, operator="admin")
"""
import os
import time
import logging

from models import db, NotificationLog
from notifications import send_sms, build_bill_message


BULK_SMS_DELAY = float(os.environ.get("BULK_SMS_DELAY_SECONDS", "0.5"))
BULK_SMS_MAX_BATCH = int(os.environ.get("BULK_SMS_MAX_BATCH", "30"))

_log = logging.getLogger(__name__)


def send_bulk_sms(items: list, operator: str = "admin") -> dict:
    """
    Send the bill-reminder SMS to each item in `items`, with a delay between.

    items = [
        {"consumer": <Consumer object>, "info": <status dict>},
        ...
    ]

    Returns:
        {
          "sent": N,
          "failed": M,
          "results": [
              {"consumer_id": 1, "cust_name": "...", "ok": true, "error": null},
              ...
          ]
        }
    """
    results = []
    sent = failed = 0

    for idx, item in enumerate(items):
        c = item["consumer"]
        info = item["info"]

        # Skip if no contact on file
        if not c.contact:
            results.append({
                "consumer_id": c.id,
                "cust_name": c.cust_name,
                "meter_acc_no": c.meter_acc_no,
                "ok": False,
                "error": "No contact number on file",
            })
            failed += 1
            continue

        # Build the message (same template as the individual SMS button)
        payload = build_bill_message(c, info, channel="sms")
        body = payload["body"]

        # Send
        try:
            res = send_sms(c.contact, body)
        except Exception as e:
            res = {"ok": False, "error": str(e)}

        ok = bool(res.get("ok"))
        err = res.get("error") if not ok else None

        # Log to NotificationLog so this send appears in the audit trail
        try:
            log = NotificationLog(
                consumer_id=c.id,
                channel="sms",
                status="sent" if ok else "failed",
                detail=("BULK · " + (err or res.get("provider_id") or ""))[:255],
            )
            db.session.add(log)
            db.session.commit()
        except Exception as e:
            _log.warning(f"[BulkSMS] log write failed for {c.id}: {e}")
            db.session.rollback()

        results.append({
            "consumer_id": c.id,
            "cust_name": c.cust_name,
            "meter_acc_no": c.meter_acc_no,
            "ok": ok,
            "error": err,
        })

        if ok:
            sent += 1
        else:
            failed += 1

        # Delay between messages (skip delay after the last one)
        if idx < len(items) - 1:
            time.sleep(BULK_SMS_DELAY)

    return {
        "sent": sent,
        "failed": failed,
        "results": results,
        "operator": operator,
    }
