"""
Notification senders — SMS via Africa's Talking, Email via SMTP.
Both providers are configured through environment variables only.
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests


# ─── SMS provider: Africa's Talking ───
AT_USERNAME = os.environ.get("AT_USERNAME", "")
AT_API_KEY  = os.environ.get("AT_API_KEY", "")
AT_SENDER   = os.environ.get("AT_SENDER", "")   # optional short code / sender ID

AT_URL = "https://api.africastalking.com/version1/messaging"


# ─── Email provider: SMTP (Gmail / Zoho / Outlook / any SMTP) ───
SMTP_HOST      = os.environ.get("SMTP_HOST", "")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER      = os.environ.get("SMTP_USER", "")
SMTP_PASS      = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM      = os.environ.get("SMTP_FROM", SMTP_USER)
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Simon Fresh Water")


def _normalize_phone_ke(phone: str) -> str:
    """Kenyan local (0712…) → E.164 (+254712…)."""
    p = "".join(c for c in (phone or "") if c.isdigit())
    if not p:
        return ""
    if p.startswith("0") and len(p) == 10:
        return "+254" + p[1:]
    if p.startswith("254"):
        return "+" + p
    if p.startswith("+"):
        return p
    return "+" + p


def send_sms(to_phone: str, message: str) -> dict:
    """Send SMS via Africa's Talking. Returns {ok, provider_id?, error?}."""
    if not AT_USERNAME or not AT_API_KEY:
        return {"ok": False, "error": "SMS provider not configured (set AT_USERNAME & AT_API_KEY)"}

    recipient = _normalize_phone_ke(to_phone)
    if not recipient:
        return {"ok": False, "error": "Invalid phone number"}

    data = {"username": AT_USERNAME, "to": recipient, "message": message}
    if AT_SENDER:
        data["from"] = AT_SENDER

    headers = {
        "apiKey": AT_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        r = requests.post(AT_URL, data=data, headers=headers, timeout=20)
        if r.status_code not in (200, 201):
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        body = r.json()
        recipients = body.get("SMSMessageData", {}).get("Recipients", [])
        if recipients and recipients[0].get("status") == "Success":
            return {"ok": True, "provider_id": recipients[0].get("messageId", "")}
        err = recipients[0].get("status", "unknown") if recipients else "no recipients"
        return {"ok": False, "error": err}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_email(to_email: str, subject: str, body_text: str,
               body_html: str | None = None) -> dict:
    """Send email via SMTP. Returns {ok, error?}."""
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        return {"ok": False, "error": "Email provider not configured (set SMTP_HOST/USER/PASSWORD)"}
    if not to_email or "@" not in to_email:
        return {"ok": False, "error": "Invalid email address"}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM}>"
    msg["To"] = to_email

    msg.attach(MIMEText(body_text, "plain"))
    if body_html:
        msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def build_bill_message(consumer, status_info: dict, channel: str = "sms") -> dict:
    """Return {'body': ...} for SMS, or {'subject', 'body_text', 'body_html'} for email."""
    amount = status_info.get("total_due", 0.0)
    label = status_info.get("label", "Due")
    paybill = os.environ.get("PAYBILL", "XXXXXX")

    if channel == "sms":
        body = (
            f"SIMON FRESH WATER - Kikuyu\n"
            f"Dear {consumer.cust_name},\n"
            f"Your water bill ({consumer.meter_acc_no}) status: {label}. "
            f"Outstanding: KES {amount:,.2f}. "
            f"Pay via Paybill {paybill}, Acc {consumer.meter_acc_no}. "
            f"Thank you."
        )
        return {"body": body}

    subject = f"Water Bill Reminder - {label} - {consumer.meter_acc_no}"
    body_text = (
        f"Dear {consumer.cust_name},\n\n"
        f"Your water account {consumer.meter_acc_no} is currently {label}.\n"
        f"Outstanding balance: KES {amount:,.2f}\n\n"
        f"Please settle to continue enjoying uninterrupted water service.\n\n"
        f"Payment:\n"
        f"  Paybill: {paybill}\n"
        f"  Account: {consumer.meter_acc_no}\n\n"
        f"Thank you for choosing Simon Fresh Water.\n"
        f"- Kikuyu, Kenya"
    )
    body_html = (
        '<div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;'
        'padding:0;background:#f8f9fa;">'
        '<div style="background:#0d47a1;color:#fff;padding:20px;'
        'border-radius:14px 14px 0 0;">'
        '<h2 style="margin:0;font-size:1.1rem;">Simon Fresh Water</h2>'
        '<p style="margin:4px 0 0;opacity:.85;font-size:.85rem;">Kikuyu, Kenya</p>'
        '</div>'
        '<div style="background:#fff;padding:22px;border-radius:0 0 14px 14px;">'
        f'<p>Dear <strong>{consumer.cust_name}</strong>,</p>'
        f'<p>Your water account <strong>{consumer.meter_acc_no}</strong> '
        f'is currently <strong>{label}</strong>.</p>'
        f'<p style="font-size:1.2rem;color:#0d47a1;">Outstanding: '
        f'<strong>KES {amount:,.2f}</strong></p>'
        '<p>Please settle your bill to continue enjoying uninterrupted '
        'water service.</p>'
        '<div style="background:#e3f2fd;padding:14px;border-radius:9px;font-size:.9rem;">'
        f'<strong>Payment details</strong><br>Paybill: <strong>{paybill}</strong><br>'
        f'Account: <strong>{consumer.meter_acc_no}</strong></div>'
        '<p style="margin-top:18px;color:#455a64;font-size:.85rem;">'
        'Thank you for choosing Simon Fresh Water.</p>'
        '</div></div>'
    )
    return {"subject": subject, "body_text": body_text, "body_html": body_html}
