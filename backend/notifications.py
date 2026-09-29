"""
Notification senders.

  SMS       → SMSGate (Android SMS Gateway) — JWT auth, cloud or private server
  WhatsApp  → Meta WhatsApp Cloud API (graph.facebook.com)
"""
import os
import time
import threading
import requests


# ─── SMS: SMSGate ───
# SMSGATE_API_BASE must be the FULL API base, including the /3rdparty/v1 part.
#   Cloud   : https://api.sms-gate.app/3rdparty/v1
#   Private : https://your-private-server.com/api/3rdparty/v1
# Defaults to the cloud server when unset.
SMSGATE_API_BASE = os.environ.get(
    "SMSGATE_API_BASE",
    "https://api.sms-gate.app/3rdparty/v1",
).rstrip("/")

SMSGATE_USERNAME   = os.environ.get("SMSGATE_USERNAME", "")
SMSGATE_PASSWORD   = os.environ.get("SMSGATE_PASSWORD", "")
SMSGATE_DEVICE_ID  = os.environ.get("SMSGATE_DEVICE_ID", "")
SMSGATE_SIM_NUMBER = os.environ.get("SMSGATE_SIM_NUMBER", "").strip()

# ─── JWT token cache ───
_token_lock         = threading.Lock()
_access_token       = None
_refresh_token      = None
_access_expires_at  = 0.0        # unix epoch
_REFRESH_SAFETY     = 60         # refresh 60 s before expiry
_jwt_supported      = True       # flipped to False if server lacks JWT


def _api(path: str) -> str:
    return f"{SMSGATE_API_BASE}{path}"


def _parse_expiry(expires_at):
    """Parse expires_at (ISO string or missing) → unix epoch. Fallback now+14m."""
    if not expires_at:
        return time.time() + 14 * 60
    try:
        from datetime import datetime, timezone
        s = expires_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return time.time() + 14 * 60


def _fetch_token_pair() -> dict:
    """Exchange Basic auth for a JWT access + refresh pair."""
    global _jwt_supported
    r = requests.post(
        _api("/auth/token"),
        auth=(SMSGATE_USERNAME, SMSGATE_PASSWORD),
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    if r.status_code in (404, 405):
        _jwt_supported = False
        raise RuntimeError("server does not support JWT (endpoint missing)")
    r.raise_for_status()
    return r.json()


def _refresh_access_token() -> dict:
    """Rotate the access token using the refresh token."""
    r = requests.post(
        _api("/auth/token/refresh"),
        headers={"Authorization": f"Bearer {_refresh_token}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _get_access_token(force: bool = False) -> str:
    global _access_token, _refresh_token, _access_expires_at
    with _token_lock:
        now = time.time()
        if (not force and _access_token
                and now < _access_expires_at - _REFRESH_SAFETY):
            return _access_token

        if _refresh_token:
            try:
                data = _refresh_access_token()
                _access_token      = data["access_token"]
                _refresh_token     = data.get("refresh_token", _refresh_token)
                _access_expires_at = _parse_expiry(data.get("expires_at"))
                return _access_token
            except Exception:
                pass

        data = _fetch_token_pair()
        _access_token      = data["access_token"]
        _refresh_token     = data["refresh_token"]
        _access_expires_at = _parse_expiry(data.get("expires_at"))
        return _access_token


# ─── WhatsApp: Meta Cloud API ───
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_ACCESS_TOKEN    = os.environ.get("WA_ACCESS_TOKEN", "")
WA_TEMPLATE_NAME   = os.environ.get("WA_TEMPLATE_NAME", "")
WA_TEMPLATE_LANG   = os.environ.get("WA_TEMPLATE_LANG", "en")
WA_API_URL = (
    f"https://graph.facebook.com/v21.0/{WA_PHONE_NUMBER_ID}/messages"
    if WA_PHONE_NUMBER_ID else ""
)


def get_available_channels() -> list:
    channels = []
    if SMSGATE_USERNAME and SMSGATE_PASSWORD:
        channels.append("sms")

    wa_toggle = os.environ.get("WA_ENABLED", "false").strip().lower()
    wa_on = wa_toggle in ("1", "true", "yes", "on")
    if wa_on and WA_PHONE_NUMBER_ID and WA_ACCESS_TOKEN:
        channels.append("whatsapp")

    return channels


# ─── Phone normalisation ───
def _normalize_phone_ke(phone: str) -> str:
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


def _normalize_phone_wa(phone: str) -> str:
    p = "".join(c for c in (phone or "") if c.isdigit())
    if not p:
        return ""
    if p.startswith("0") and len(p) == 10:
        return "254" + p[1:]
    if p.startswith("254"):
        return p
    if len(p) == 9:
        return "254" + p
    return p


# ─── SMS sender ───
def send_sms(to_phone: str, message: str) -> dict:
    """Send SMS via SMSGate using JWT auth. Returns {ok, provider_id?, error?}."""
    if not SMSGATE_USERNAME or not SMSGATE_PASSWORD:
        return {"ok": False,
                "error": "SMS provider not configured (set SMSGATE_USERNAME & SMSGATE_PASSWORD)"}

    recipient = _normalize_phone_ke(to_phone)
    if not recipient:
        return {"ok": False, "error": "Invalid phone number"}

    payload = {"textMessage": {"text": message}, "phoneNumbers": [recipient]}
    if SMSGATE_DEVICE_ID:
        payload["deviceId"] = SMSGATE_DEVICE_ID
    if SMSGATE_SIM_NUMBER:
        try:
            payload["simNumber"] = int(SMSGATE_SIM_NUMBER)
        except ValueError:
            pass

    for attempt in (1, 2):
        try:
            # If the server lacks JWT support, fall back to Basic auth
            if not _jwt_supported:
                r = requests.post(
                    _api("/messages"),
                    json=payload,
                    auth=(SMSGATE_USERNAME, SMSGATE_PASSWORD),
                    timeout=20,
                )
            else:
                token = _get_access_token(force=(attempt == 2))
                r = requests.post(
                    _api("/messages"),
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=20,
                )

            if r.status_code == 401 and attempt == 1 and _jwt_supported:
                continue

            if r.status_code not in (200, 201, 202):
                return {"ok": False,
                        "error": f"HTTP {r.status_code}: {r.text[:200]}"}

            body = r.json()
            return {"ok": True, "provider_id": body.get("id", "")}

        except Exception as e:
            if attempt == 2:
                return {"ok": False, "error": str(e)}

    return {"ok": False, "error": "send failed after retry"}


# ─── WhatsApp sender (unchanged) ───
def send_whatsapp(to_phone: str, message: str) -> dict:
    if not WA_PHONE_NUMBER_ID or not WA_ACCESS_TOKEN:
        return {"ok": False,
                "error": "WhatsApp provider not configured (set WA_PHONE_NUMBER_ID & WA_ACCESS_TOKEN)"}

    recipient = _normalize_phone_wa(to_phone)
    if not recipient:
        return {"ok": False, "error": "Invalid phone number"}

    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    if WA_TEMPLATE_NAME:
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "template",
            "template": {
                "name": WA_TEMPLATE_NAME,
                "language": {"code": WA_TEMPLATE_LANG},
                "components": [
                    {"type": "body",
                     "parameters": [{"type": "text", "text": message}]}
                ],
            },
        }
    else:
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": False, "body": message},
        }

    try:
        r = requests.post(WA_API_URL, json=payload, headers=headers, timeout=20)
        if r.status_code in (200, 201):
            body = r.json()
            msgs = body.get("messages", [])
            provider_id = msgs[0].get("id") if msgs else ""
            return {"ok": True, "provider_id": provider_id}

        try:
            err_body = r.json()
            err = err_body.get("error", {}) or {}
            msg = (err.get("error_user_msg")
                   or err.get("message")
                   or f"HTTP {r.status_code}")
        except Exception:
            msg = f"HTTP {r.status_code}: {r.text[:200]}"
        return {"ok": False, "error": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── Message builder (unchanged) ───
def build_bill_message(consumer, status_info: dict, channel: str = "sms") -> dict:
    amount  = status_info.get("total_due", 0.0)
    label   = status_info.get("label", "Due")
    cons    = status_info.get("consumption_m3", 0.0)
    paybill = os.environ.get("PAYBILL", "XXXXXX")

    if channel == "sms":
        current_m3        = status_info.get("current_reading_m3", 0.0)
        previous_m3       = status_info.get("previous_reading_m3", 0.0)
        current_charges   = status_info.get("current_charges", 0.0)
        prev_outstanding  = status_info.get("previous_outstanding", 0.0)
        total_outstanding = status_info.get("total_outstanding", 0.0)

        bill_month = status_info.get("bill_month", "")
        pcf        = status_info.get("payment_carried_forward", 0.0)
        is_cleared = status_info.get("is_current_cleared", False)

        pcf_line = ""
        if not is_cleared:
            pcf_line = f"{'Payment Carried Fwd':<23}: KES {pcf:,.2f}\n"

        body = (
            f"SIMON FRESH WATER - Gikambura\n"
            f"Water Bill Due: {bill_month}\n"
            f"--------------------------\n"
            f"Dear {consumer.cust_name},\n"
            f"\n"
            f"{'Account':<23}: {consumer.meter_acc_no}\n"
            f"{'Status':<23}: {label}\n"
            f"{'Current (MTR)':<23}: {current_m3:.2f} M3\n"
            f"{'Previous (MTR)':<23}: {previous_m3:.2f} M3\n"
            f"{'Current Charges':<23}: KES {current_charges:,.2f}\n"
            f"{pcf_line}"
            f"{'Prev Outstanding':<23}: KES {prev_outstanding:,.2f}\n"
            f"{'Total Outstanding':<23}: KES {total_outstanding:,.2f}\n"
            f"\n"
            f"Pay via Paybill {paybill}\n"
            f"Account No        : {consumer.meter_acc_no}\n"
            f"\n"
            f"--------------------------\n"
            f"Water charges payable on or before 20 days\n"
            f"from the meter reading date.\n"
            f"Please settle any outstanding balance to\n"
            f"continue enjoying uninterrupted water service.\n"
            f"Thank you."
        )
    else:
        body = (
            f"SIMON FRESH WATER - Kikuyu\n"
            f"Dear {consumer.cust_name},\n"
            f"Your water bill ({consumer.meter_acc_no}) status: {label}. "
            f"Outstanding: KES {amount:,.2f}. "
            f"Pay via Paybill {paybill}, Acc {consumer.meter_acc_no}. "
            f"Thank you."
        )
    return {"body": body}


# ─── WhatsApp document sender (unchanged) ───
def send_whatsapp_document(to_phone: str,
                           pdf_public_url: str,
                           filename: str,
                           body_params: list,
                           template_name: str = None,
                           language: str = None) -> dict:
    if not WA_PHONE_NUMBER_ID or not WA_ACCESS_TOKEN:
        return {"ok": False,
                "error": "WhatsApp provider not configured (set WA_PHONE_NUMBER_ID & WA_ACCESS_TOKEN)"}

    tpl_name = template_name or WA_TEMPLATE_NAME
    tpl_lang = language or WA_TEMPLATE_LANG

    if not tpl_name:
        return {"ok": False,
                "error": "WhatsApp template name not configured (set WA_TEMPLATE_NAME)"}

    recipient = _normalize_phone_wa(to_phone)
    if not recipient:
        return {"ok": False, "error": "Invalid phone number"}

    body_components = [
        {"type": "text", "text": str(v if v not in (None, "") else "-")}
        for v in body_params
    ]

    payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "template",
        "template": {
            "name": tpl_name,
            "language": {"code": tpl_lang},
            "components": [
                {
                    "type": "header",
                    "parameters": [
                        {
                            "type": "document",
                            "document": {
                                "link": pdf_public_url,
                                "filename": filename,
                            },
                        }
                    ],
                },
                {
                    "type": "body",
                    "parameters": body_components,
                },
            ],
        },
    }

    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(WA_API_URL, json=payload, headers=headers, timeout=30)
        if r.status_code in (200, 201):
            body = r.json()
            msgs = body.get("messages", [])
            provider_id = msgs[0].get("id") if msgs else ""
            return {"ok": True, "provider_id": provider_id}

        try:
            err_body = r.json()
            err = err_body.get("error", {}) or {}
            msg = (err.get("error_user_msg")
                   or err.get("message")
                   or f"HTTP {r.status_code}")
            code = err.get("code")
            if code:
                msg = f"[{code}] {msg}"
        except Exception:
            msg = f"HTTP {r.status_code}: {r.text[:200]}"
        return {"ok": False, "error": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── SMS retry wrapper (unchanged) ───
def send_sms_with_retry(to_phone: str, message: str, max_attempts: int = 3) -> dict:
    import time as _time
    last = {"ok": False, "error": "no attempts"}
    for attempt in range(1, max_attempts + 1):
        last = send_sms(to_phone, message)
        if last.get("ok"):
            return last
        if attempt < max_attempts:
            _time.sleep(2 ** attempt)
    return last
