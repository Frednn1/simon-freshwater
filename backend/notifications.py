"""
Notification senders.

  SMS       → SMSGate (Android SMS Gateway) via api.sms-gate.app
  WhatsApp  → Meta WhatsApp Cloud API (graph.facebook.com)
"""
import os
import requests


# ─── SMS: SMSGate (Android SMS Gateway) ───
SMSGATE_USERNAME   = os.environ.get("SMSGATE_USERNAME", "")
SMSGATE_PASSWORD   = os.environ.get("SMSGATE_PASSWORD", "")
SMSGATE_DEVICE_ID  = os.environ.get("SMSGATE_DEVICE_ID", "")
SMSGATE_SIM_NUMBER = os.environ.get("SMSGATE_SIM_NUMBER", "").strip()
SMSGATE_URL = "https://api.sms-gate.app/3rdparty/v1/messages"


# ─── WhatsApp: Meta Cloud API ───
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_ACCESS_TOKEN    = os.environ.get("WA_ACCESS_TOKEN", "")
WA_TEMPLATE_NAME   = os.environ.get("WA_TEMPLATE_NAME", "")   # optional
WA_TEMPLATE_LANG   = os.environ.get("WA_TEMPLATE_LANG", "en")
WA_API_URL = (
    f"https://graph.facebook.com/v21.0/{WA_PHONE_NUMBER_ID}/messages"
    if WA_PHONE_NUMBER_ID else ""
)


def get_available_channels() -> list:
    """
    Return ['sms'] and/or ['whatsapp'] depending on configured providers.

    The WhatsApp channel is additionally gated by the WA_ENABLED environment
    variable — set it to 'true'/'1'/'yes' to expose the WhatsApp button, or
    to 'false'/'0'/'no' (or omit it) to hide it entirely.
    """
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
    """Kenyan local (0712…) → E.164 with + prefix (used for SMSGate)."""
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
    """Meta Cloud API expects digits only, including country code, no '+'."""
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
    """Send SMS via SMSGate. Returns {ok, provider_id?, error?}."""
    if not SMSGATE_USERNAME or not SMSGATE_PASSWORD:
        return {"ok": False, "error": "SMS provider not configured (set SMSGATE_USERNAME & SMSGATE_PASSWORD)"}

    recipient = _normalize_phone_ke(to_phone)
    if not recipient:
        return {"ok": False, "error": "Invalid phone number"}

    payload = {"textMessage": {"text": message}, "phoneNumbers": [recipient]}
    if SMSGATE_DEVICE_ID:
        payload["deviceId"] = SMSGATE_DEVICE_ID

    # Optional: pin the sending SIM slot (1-based). When unset, SMSGate
    # falls back to its own setting (OS Default / Round Robin / Random).
    if SMSGATE_SIM_NUMBER:
        try:
            payload["simNumber"] = int(SMSGATE_SIM_NUMBER)
        except ValueError:
            # Ignore invalid values rather than failing the send
            pass

    try:
        r = requests.post(SMSGATE_URL, json=payload,
                          auth=(SMSGATE_USERNAME, SMSGATE_PASSWORD), timeout=20)
        if r.status_code not in (200, 201, 202):
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        body = r.json()
        return {"ok": True, "provider_id": body.get("id", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── WhatsApp sender ───
def send_whatsapp(to_phone: str, message: str) -> dict:
    """
    Send WhatsApp message via Meta Cloud API.

    Behaviour:
      • If WA_TEMPLATE_NAME is set → send as template (works anytime).
      • Otherwise → send as plain text (only deliverable within the 24h
        customer-service window; Meta will reject it outside that window
        with a specific error which we return as-is).

    Returns {ok, provider_id?, error?}.
    """
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

        # Extract a readable error from Meta's response
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


# ─── Message builder ───
def build_bill_message(consumer, status_info: dict, channel: str = "sms") -> dict:
    """
    Return {'body': ...} for both SMS and WhatsApp. The message body is
    identical across channels — only the transport differs.
    """
    amount = status_info.get("total_due", 0.0)
    label = status_info.get("label", "Due")
    paybill = os.environ.get("PAYBILL", "XXXXXX")

    body = (
        f"SIMON FRESH WATER - Kikuyu\n"
        f"Dear {consumer.cust_name},\n"
        f"Your water bill ({consumer.meter_acc_no}) status: {label}. "
        f"Outstanding: KES {amount:,.2f}. "
        f"Pay via Paybill {paybill}, Acc {consumer.meter_acc_no}. "
        f"Thank you."
    )
    return {"body": body}


# ─── WhatsApp document sender (PDF bill via template) ───
def send_whatsapp_document(to_phone: str,
                           pdf_public_url: str,
                           filename: str,
                           body_params: list,
                           template_name: str = None,
                           language: str = None) -> dict:
    """
    Send a WhatsApp message with a Document header via an approved template.

    Args:
        to_phone        : recipient phone (any Kenyan format)
        pdf_public_url  : publicly reachable HTTPS URL of the PDF
        filename        : what WhatsApp should show as the file name
        body_params     : list of strings for the template body variables
                          e.g. ["John Kamau", "MTR-0012"]
        template_name   : overrides WA_TEMPLATE_NAME if provided
        language        : overrides WA_TEMPLATE_LANG if provided

    Returns {ok, provider_id?, error?}.
    """
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

        # Extract readable error
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
