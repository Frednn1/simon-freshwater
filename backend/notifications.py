"""
Notification senders.

  SMS       → SMSGate (Android SMS Gateway) — JWT auth, cloud or private server
  WhatsApp  → Meta WhatsApp Cloud API (graph.facebook.com)
"""
import os
import json
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
# Phase 3 (GAP-CRIT-5): per-username token cache. Different tenants
# authenticate against their own SMSGate accounts, so tokens cannot be
# shared. Keyed by username; protected by _token_lock.
_tokens             = {}         # {username: {access_token, refresh_token, expires_at}}
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


def _fetch_token_pair(username: str, password: str) -> dict:
    """Exchange Basic auth for a JWT access + refresh pair (per-account)."""
    global _jwt_supported
    r = requests.post(
        _api("/auth/token"),
        auth=(username, password),
        headers={"Content-Type": "application/json"},
        json={"scopes": ["messages:send"]},
        timeout=15,
    )
    if r.status_code in (404, 405):
        _jwt_supported = False
        raise RuntimeError("server does not support JWT (endpoint missing)")
    r.raise_for_status()
    return r.json()


def _refresh_access_token(refresh_token: str) -> dict:
    """Rotate the access token using the given refresh token."""
    r = requests.post(
        _api("/auth/token/refresh"),
        headers={"Authorization": f"Bearer {refresh_token}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _get_access_token(username: str, password: str, force: bool = False) -> str:
    """Get (or refresh) the SMSGate JWT access token for a specific username.
    Per-username cache — different tenants maintain independent tokens.
    Protected by _token_lock for thread safety."""
    with _token_lock:
        now = time.time()
        cached = _tokens.get(username) or {}
        if (not force and cached.get("access_token")
                and now < cached.get("expires_at", 0) - _REFRESH_SAFETY):
            return cached["access_token"]

        if cached.get("refresh_token"):
            try:
                data = _refresh_access_token(cached["refresh_token"])
                _tokens[username] = {
                    "access_token":  data["access_token"],
                    "refresh_token": data.get("refresh_token", cached["refresh_token"]),
                    "expires_at":    _parse_expiry(data.get("expires_at")),
                }
                return data["access_token"]
            except Exception:
                pass

        data = _fetch_token_pair(username, password)
        _tokens[username] = {
            "access_token":  data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at":    _parse_expiry(data.get("expires_at")),
        }
        return data["access_token"]


# ─── WhatsApp: Meta Cloud API ───
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_ACCESS_TOKEN    = os.environ.get("WA_ACCESS_TOKEN", "")
WA_TEMPLATE_NAME   = os.environ.get("WA_TEMPLATE_NAME", "")
WA_TEMPLATE_LANG   = os.environ.get("WA_TEMPLATE_LANG", "en")
WA_API_URL = (
    f"https://graph.facebook.com/v21.0/{WA_PHONE_NUMBER_ID}/messages"
    if WA_PHONE_NUMBER_ID else ""
)


def _wa_api_url_for(phone_number_id: str) -> str:
    """Build the Meta Cloud API URL for a given phone_number_id.
    Phase 2 (GAP-CRIT-1): replaces reliance on the module-level
    WA_API_URL constant so per-tenant phone_number_id works correctly.
    Returns "" if phone_number_id is falsy."""
    if not phone_number_id:
        return ""
    return f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"


def _auto_detect_tenant_slug() -> str:
    """Best-effort: get current request's tenant slug from Flask g.
    Returns None when not in a request context or g.tenant is unset.

    Phase 3 (GAP-NEW-1): enables tenant-aware credential resolution in
    send_sms / send_whatsapp / send_whatsapp_document without requiring
    every caller to pass tenant_slug. Background callers (OTP, scheduled
    tasks) get None -> global credentials, which is correct for those
    admin-level flows. Never raises.
    """
    try:
        from flask import g, has_request_context
        if not has_request_context():
            return None
        t = getattr(g, "tenant", None)
        return (getattr(t, "slug", None) if t is not None else None)
    except Exception:
        return None


def _slug_to_env_suffix(slug: str) -> str:
    """Convert tenant slug to env var suffix: 'demo-tenant' -> 'DEMO_TENANT'.
    Returns '' for falsy/empty input. Never raises."""
    if not slug or not isinstance(slug, str):
        return ""
    try:
        return slug.strip().upper().replace("-", "_")
    except Exception:
        return ""


def smsgate_credentials_for_tenant(slug: str = None) -> dict:
    """Resolve SMSGate credentials for a tenant, with global fallback.

    Per-tenant env vars (only when BOTH user+pass are set):
      SMSGATE_<SLUG_UPPER>_USERNAME
      SMSGATE_<SLUG_UPPER>_PASSWORD
      SMSGATE_<SLUG_UPPER>_DEVICE_ID   (optional; falls back to global)

    SMSGATE_SIM_NUMBER is always global (physical hardware per tenant — D5).
    SMSGATE_API_BASE is always global (D5).

    Returns {username, password, device_id, sim_number, source}.
    source = "tenant" | "global". Never raises.
    """
    globals_ = {
        "username": SMSGATE_USERNAME,
        "password": SMSGATE_PASSWORD,
        "device_id": SMSGATE_DEVICE_ID,
        "sim_number": SMSGATE_SIM_NUMBER,
        "source": "global",
    }
    if not slug:
        return globals_
    try:
        suffix = _slug_to_env_suffix(slug)
        if not suffix:
            return globals_
        t_user = os.environ.get(f"SMSGATE_{suffix}_USERNAME", "").strip()
        t_pass = os.environ.get(f"SMSGATE_{suffix}_PASSWORD", "").strip()
        t_dev  = os.environ.get(f"SMSGATE_{suffix}_DEVICE_ID", "").strip()
        t_sim  = os.environ.get(f"SMSGATE_{suffix}_SIM_NUMBER", "").strip()
        if t_user and t_pass:
            return {
                "username": t_user,
                "password": t_pass,
                "device_id": t_dev or globals_["device_id"],
                "sim_number": t_sim or globals_["sim_number"],
                "source": "tenant",
            }
    except Exception:
        pass
    return globals_


def wa_credentials_for_tenant(slug: str = None) -> dict:
    """Resolve WhatsApp credentials for a tenant, with global fallback.

    Per-tenant env vars (only when BOTH token+phone_id are set):
      WA_<SLUG_UPPER>_ACCESS_TOKEN
      WA_<SLUG_UPPER>_PHONE_NUMBER_ID

    WA_ENABLED, WA_TEMPLATE_NAME, WA_TEMPLATE_LANG stay global (D3/D5).
    Per-tenant overrides for these live in Tenant DB columns (Phase 2).

    Returns {access_token, phone_number_id, source}.
    source = "tenant" | "global". Never raises.
    """
    globals_ = {
        "access_token": WA_ACCESS_TOKEN,
        "phone_number_id": WA_PHONE_NUMBER_ID,
        "source": "global",
    }
    if not slug:
        return globals_
    try:
        suffix = _slug_to_env_suffix(slug)
        if not suffix:
            return globals_
        t_token = os.environ.get(f"WA_{suffix}_ACCESS_TOKEN", "").strip()
        t_phone = os.environ.get(f"WA_{suffix}_PHONE_NUMBER_ID", "").strip()
        if t_token and t_phone:
            return {
                "access_token": t_token,
                "phone_number_id": t_phone,
                "source": "tenant",
            }
    except Exception:
        pass
    return globals_


def get_available_channels(tenant_slug: str = None,
                          wa_enabled_override=None) -> list:
    """List channels available for the (optional) tenant.

    Backward compatible: get_available_channels() with no args returns the
    global behaviour (env-only WA_ENABLED + global creds).

    Phase 2: callers that know the current tenant should pass:
      - tenant_slug (for per-tenant credential resolution)
      - wa_enabled_override (True/False from Tenant.wa_enabled; None = env fallback)
    """
    channels = []
    # Phase 4: SMS availability uses per-tenant resolver
    # (mirrors the WhatsApp check below). Backward-compatible: with
    # tenant_slug=None, smsgate_credentials_for_tenant returns the globals,
    # so tenants without per-tenant creds behave exactly as before.
    _sms_creds = smsgate_credentials_for_tenant(tenant_slug)
    if _sms_creds["username"] and _sms_creds["password"]:
        channels.append("sms")

    if wa_enabled_override is None:
        wa_toggle = os.environ.get("WA_ENABLED", "false").strip().lower()
        wa_on = wa_toggle in ("1", "true", "yes", "on")
    else:
        wa_on = bool(wa_enabled_override)

    _creds = wa_credentials_for_tenant(tenant_slug)
    if wa_on and _creds["access_token"] and _creds["phone_number_id"]:
        channels.append("whatsapp")

    return channels


# ─── Payment info (paybill + account for the current mode) ───
def _payment_info(consumer, tenant=None) -> dict:
    """
    Return {'paybill': '...', 'account': '...'}.

    Resolution order:

      1. Tenant with a paybill set on its record:
         - payment_account set on tenant → paybill + payment_account
           (fixed account shared by all consumers of that tenant).
         - payment_account empty → paybill + consumer.meter_acc_no
           (per-consumer account).

         This applies uniformly to every tenant, including the default
         'simon-freshwater' tenant. There is no hard-coded 'always fixed'
         branch any more — the tenant row is the single source of truth.

      2. Fallback (no tenant, or tenant with no paybill):
         - PAYMENT_MODE=fixed → FIXED_PAYBILL / FIXED_PAYMENT_ACCOUNT
         - Else → PAYBILL env + consumer.meter_acc_no
    """
    _FIXED_PB     = os.environ.get("FIXED_PAYBILL", "").strip() or "—"
    _FIXED_ACCT   = os.environ.get("FIXED_PAYMENT_ACCOUNT", "").strip() or "—"

    # 1. Tenant with a paybill on its record (uniform across all tenants)
    t_pb   = (getattr(tenant, "paybill", None) or "").strip() if tenant is not None else ""
    t_acct = (getattr(tenant, "payment_account", None) or "").strip() if tenant is not None else ""

    if t_pb:
        return {
            "paybill": t_pb,
            "account": t_acct or consumer.meter_acc_no,
        }

    # 2. Fallback
    mode = os.environ.get("PAYMENT_MODE", "").strip().lower()
    if mode == "fixed":
        return {"paybill": _FIXED_PB, "account": _FIXED_ACCT}

    return {
        "paybill": os.environ.get("PAYBILL", "XXXXXX"),
        "account": consumer.meter_acc_no,
    }


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
def send_sms(to_phone: str, message: str, tenant_slug: str = None) -> dict:
    """Send SMS via SMSGate using JWT auth. Returns {ok, provider_id?, error?}.

    Phase 3 (GAP-CRIT-2 + GAP-NEW-1 + D4): resolves credentials via
    smsgate_credentials_for_tenant(tenant_slug). If tenant_slug is None,
    auto-detects from Flask g.tenant (when in a request context); falls
    back to global env credentials when no tenant context exists (e.g.
    background OTP flows).
    """
    if tenant_slug is None:
        tenant_slug = _auto_detect_tenant_slug()
    creds = smsgate_credentials_for_tenant(tenant_slug)
    if not creds["username"] or not creds["password"]:
        return {"ok": False,
                "error": "SMS provider not configured (set SMSGATE_USERNAME & SMSGATE_PASSWORD)"}

    recipient = _normalize_phone_ke(to_phone)
    if not recipient:
        return {"ok": False, "error": "Invalid phone number"}

    payload = {"textMessage": {"text": message}, "phoneNumbers": [recipient]}
    if creds["device_id"]:
        payload["deviceId"] = creds["device_id"]
    if creds["sim_number"]:
        try:
            payload["simNumber"] = int(creds["sim_number"])
        except (ValueError, TypeError):
            pass

    for attempt in (1, 2):
        try:
            if not _jwt_supported:
                r = requests.post(
                    _api("/messages"),
                    json=payload,
                    auth=(creds["username"], creds["password"]),
                    timeout=20,
                )
            else:
                token = _get_access_token(creds["username"], creds["password"],
                                           force=(attempt == 2))
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
def send_whatsapp(to_phone: str, message: str, tenant_slug: str = None,
                  template_lang_override: str = None) -> dict:
    # Phase 3 (GAP-NEW-1): auto-detect tenant when not passed explicitly.
    if tenant_slug is None:
        tenant_slug = _auto_detect_tenant_slug()
    # Phase 2: per-tenant credential resolution (falls back to global).
    creds = wa_credentials_for_tenant(tenant_slug)
    if not creds["phone_number_id"] or not creds["access_token"]:
        return {"ok": False,
                "error": "WhatsApp provider not configured (set WA_PHONE_NUMBER_ID & WA_ACCESS_TOKEN)"}

    recipient = _normalize_phone_wa(to_phone)
    if not recipient:
        return {"ok": False, "error": "Invalid phone number"}

    _wa_url = _wa_api_url_for(creds["phone_number_id"])
    _lang = template_lang_override or WA_TEMPLATE_LANG
    headers = {
        "Authorization": f"Bearer {creds['access_token']}",
        "Content-Type": "application/json",
    }

    if WA_TEMPLATE_NAME:
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "template",
            "template": {
                "name": WA_TEMPLATE_NAME,
                "language": {"code": _lang},
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
        r = requests.post(_wa_url, json=payload, headers=headers, timeout=20)
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
def build_bill_message(consumer, status_info: dict, channel: str = "sms", tenant=None) -> dict:
    amount  = status_info.get("total_due", 0.0)
    label   = status_info.get("label", "Due")
    cons    = status_info.get("consumption_m3", 0.0)
    pay     = _payment_info(consumer, tenant=tenant)

    # Brand name + location for the message header. With tenant info, the
    # org name replaces the hardcoded "SIMON FRESH WATER" and the optional
    # location field replaces the channel-specific defaults. Without tenant
    # info, or when a field is unset, the current values are preserved.
    _org_upper = "SIMON FRESH WATER"
    _loc_sms   = "Gikambura"
    _loc_wa    = "Kikuyu"
    if tenant is not None:
        _t_org = (getattr(tenant, "org_name", None) or "").strip()
        if _t_org:
            _org_upper = _t_org.upper()
        _t_loc = (getattr(tenant, "location", None) or "").strip()
        if _t_loc:
            _loc_sms = _t_loc
            _loc_wa  = _t_loc

    if channel == "sms":
        current_m3        = status_info.get("current_reading_m3", 0.0)
        previous_m3       = status_info.get("previous_reading_m3", 0.0)
        current_charges   = status_info.get("current_charges", 0.0)
        prev_outstanding  = status_info.get("previous_outstanding", 0.0)
        total_outstanding = status_info.get("total_outstanding", 0.0)

        bill_month = status_info.get("bill_month", "")
        pcf        = status_info.get("payment_carried_forward", 0.0)
        paid       = status_info.get("paid", 0.0)

        body = (
            f"{_org_upper} - {_loc_sms}\n"
            f"Water Bill Due: {bill_month}\n"
            f"--------------------------\n"
            f"Dear {consumer.cust_name},\n"
            f"\n"
            f"{'Account':<23}: {consumer.meter_acc_no}\n"
            f"{'Status':<23}: {label}\n"
            f"{'Current (MTR)':<23}: {current_m3:.2f} M3\n"
            f"{'Previous (MTR)':<23}: {previous_m3:.2f} M3\n"
            f"{'Current Charges':<23}: KES {current_charges:,.2f}\n"
            f"{'Payment Carried Fwd':<23}: KES {pcf:,.2f}\n"
            f"{'Paid':<23}: KES {paid:,.2f}\n"
            f"{'Prev Outstanding':<23}: KES {prev_outstanding:,.2f}\n"
            f"{'Total Outstanding':<23}: KES {total_outstanding:,.2f}\n"
            f"\n"
            f"Pay via Paybill {pay['paybill']}\n"
            f"Account No        : {pay['account']}\n"
            f"\n"
            f"--------------------------\n"
            f"Please settle any outstanding balance to\n"
            f"continue enjoying uninterrupted water service.\n"
            f"Thank you."
        )
    else:
        body = (
            f"{_org_upper} - {_loc_wa}\n"
            f"Dear {consumer.cust_name},\n"
            f"Your water bill ({consumer.meter_acc_no}) status: {label}. "
            f"Outstanding: KES {amount:,.2f}. "
            f"Pay via Paybill {pay['paybill']}, Acc {pay['account']}. "
            f"Thank you."
        )
    return {"body": body}


# ─── WhatsApp document sender (unchanged) ───
def send_whatsapp_document(to_phone: str,
                           pdf_public_url: str,
                           filename: str,
                           body_params: list,
                           template_name: str = None,
                           language: str = None,
                           tenant_slug: str = None,
                           template_lang_override: str = None) -> dict:
    # Phase 3 (GAP-NEW-1): auto-detect tenant when not passed explicitly.
    if tenant_slug is None:
        tenant_slug = _auto_detect_tenant_slug()
    # Phase 2: per-tenant credential resolution (falls back to global).
    creds = wa_credentials_for_tenant(tenant_slug)
    if not creds["phone_number_id"] or not creds["access_token"]:
        return {"ok": False,
                "error": "WhatsApp provider not configured (set WA_PHONE_NUMBER_ID & WA_ACCESS_TOKEN)"}

    tpl_name = template_name or WA_TEMPLATE_NAME
    # Language precedence: explicit arg > caller override > env default
    tpl_lang = language or template_lang_override or WA_TEMPLATE_LANG

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

    _wa_url = _wa_api_url_for(creds["phone_number_id"])
    headers = {
        "Authorization": f"Bearer {creds['access_token']}",
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(_wa_url, json=payload, headers=headers, timeout=30)
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
            # WA_ERROR_DIAGNOSTIC — full Meta error details for debugging
            print("WA_ERROR_DIAGNOSTIC:", json.dumps(err_body))
        except Exception:
            msg = f"HTTP {r.status_code}: {r.text[:200]}"
        return {"ok": False, "error": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── SMS retry wrapper (unchanged) ───
def send_sms_with_retry(to_phone: str, message: str, max_attempts: int = 3,
                         tenant_slug: str = None) -> dict:
    import time as _time
    last = {"ok": False, "error": "no attempts"}
    for attempt in range(1, max_attempts + 1):
        last = send_sms(to_phone, message, tenant_slug=tenant_slug)
        if last.get("ok"):
            return last
        if attempt < max_attempts:
            _time.sleep(2 ** attempt)
    return last
