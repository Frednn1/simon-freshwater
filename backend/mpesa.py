"""
M-Pesa Daraja API client — C2B (Customer to Business).

Environment variables:
  MPESA_ENV             "sandbox" (default) or "production"
  MPESA_CONSUMER_KEY    Daraja app consumer key
  MPESA_CONSUMER_SECRET Daraja app consumer secret
  MPESA_SHORTCODE       Paybill number (sandbox default: 174379)
"""
import os
import time
import base64
import requests
from threading import Lock


MPESA_ENV             = os.environ.get("MPESA_ENV", "sandbox").strip().lower()
MPESA_CONSUMER_KEY    = os.environ.get("MPESA_CONSUMER_KEY", "").strip()
MPESA_CONSUMER_SECRET = os.environ.get("MPESA_CONSUMER_SECRET", "").strip()
MPESA_SHORTCODE       = os.environ.get("MPESA_SHORTCODE", "").strip()

BASE_URL = (
    "https://api.safaricom.co.ke" if MPESA_ENV == "production"
    else "https://sandbox.safaricom.co.ke"
)

_token_cache = {"token": None, "expires_at": 0}
_token_lock = Lock()


def _configured() -> bool:
    return bool(MPESA_CONSUMER_KEY and MPESA_CONSUMER_SECRET and MPESA_SHORTCODE)


def get_mpesa_config() -> dict:
    """Non-secret configuration info for diagnostics."""
    return {
        "env": MPESA_ENV,
        "base_url": BASE_URL,
        "shortcode": MPESA_SHORTCODE,
        "configured": _configured(),
    }


def get_access_token(force: bool = False) -> dict:
    """
    Fetch (or return cached) OAuth access token.
    Returns {ok, token?, error?}. Tokens cached for expires_in - 60s.
    """
    if not _configured():
        return {"ok": False,
                "error": "M-Pesa not configured (set MPESA_CONSUMER_KEY, "
                         "MPESA_CONSUMER_SECRET, MPESA_SHORTCODE)"}

    with _token_lock:
        now = time.time()
        if not force and _token_cache["token"] and now < _token_cache["expires_at"]:
            return {"ok": True, "token": _token_cache["token"]}

        url = f"{BASE_URL}/oauth/v1/generate?grant_type=client_credentials"
        auth = base64.b64encode(
            f"{MPESA_CONSUMER_KEY}:{MPESA_CONSUMER_SECRET}".encode()
        ).decode()
        headers = {"Authorization": f"Basic {auth}"}

        try:
            r = requests.get(url, headers=headers, timeout=20)
            if r.status_code != 200:
                return {"ok": False,
                        "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            data = r.json()
            token = data.get("access_token")
            expires = int(data.get("expires_in", 3599))
            if not token:
                return {"ok": False, "error": "No token in response"}
            _token_cache["token"] = token
            _token_cache["expires_at"] = now + max(expires - 60, 60)
            return {"ok": True, "token": token}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def register_c2b_urls(confirmation_url: str, validation_url: str) -> dict:
    """
    Register C2B callback URLs with Safaricom.
    ResponseType=Completed: Safaricom accepts the payment automatically even
    if the ValidationURL is unreachable (recommended for live payments).
    """
    tok = get_access_token()
    if not tok.get("ok"):
        return tok

    url = f"{BASE_URL}/mpesa/c2b/v1/registerurl"
    headers = {
        "Authorization": f"Bearer {tok['token']}",
        "Content-Type": "application/json",
    }
    payload = {
        "ShortCode": MPESA_SHORTCODE,
        "ResponseType": "Completed",
        "ConfirmationURL": confirmation_url,
        "ValidationURL": validation_url,
    }

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        if r.status_code not in (200, 201):
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        data = r.json()
        # Safaricom returns ResponseCode in multiple formats on success:
        # "0", "00", "00000000" (most common for /registerurl) — or int 0.
        # Normalize: any all-zeros string counts as success.
        # Fallback: if ResponseCode missing, trust "Success" description.
        _raw_code = str(data.get("ResponseCode", "")).strip()
        _raw_desc = str(data.get("ResponseDescription", "")).strip()
        if _raw_code:
            _is_success = (_raw_code.lstrip("0") == "")
        else:
            _is_success = _raw_desc.lower() in ("success", "successful")
        if _is_success:
            return {"ok": True, "response": data}
        return {"ok": False, "error": _raw_desc or _raw_code or data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def simulate_c2b_payment(amount: int, msisdn: str, bill_ref_number: str) -> dict:
    """
    Sandbox only: ask Safaricom to fire a test C2B confirmation at our URL.
    Production does not expose this endpoint.
    """
    if MPESA_ENV == "production":
        return {"ok": False, "error": "Simulation is sandbox-only."}

    tok = get_access_token()
    if not tok.get("ok"):
        return tok

    url = f"{BASE_URL}/mpesa/c2b/v1/simulate"
    headers = {
        "Authorization": f"Bearer {tok['token']}",
        "Content-Type": "application/json",
    }
    digits = "".join(c for c in str(msisdn) if c.isdigit())
    payload = {
        "ShortCode": MPESA_SHORTCODE,
        "CommandID": "CustomerPayBillOnline",
        "Amount": int(amount),
        "Msisdn": digits,
        "BillRefNumber": (bill_ref_number or "").strip() or "TEST",
    }

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        if r.status_code not in (200, 201):
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return {"ok": True, "response": r.json()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════════════
#  PER-TENANT CREDENTIALS (multi-tenant)
# ═══════════════════════════════════════════════
# Each tenant has its own Daraja app. Credentials live in Render env vars
# named after the tenant slug:
#
#   MPESA_<SLUG_UPPER>_ENV             sandbox | production
#   MPESA_<SLUG_UPPER>_CONSUMER_KEY    ...
#   MPESA_<SLUG_UPPER>_CONSUMER_SECRET ...
#   MPESA_<SLUG_UPPER>_SHORTCODE       ...
#
# <SLUG_UPPER> is the slug uppercased with '-' replaced by '_'.
# Example: "demo-tenant-1" → "DEMO_TENANT_1"
#
# When a tenant has no per-tenant vars, or when no tenant is supplied,
# the module-level globals (MPESA_*, Simon's current setup) are used —
# so nothing changes for the existing deployment.


def _slug_prefix(slug: str) -> str:
    return (slug or "").strip().upper().replace("-", "_")


def credentials_for_tenant(slug: str = None) -> dict:
    """Return the M-Pesa credentials to use for the given tenant slug.

    Falls back to the global MPESA_* env vars when the tenant has no
    per-tenant configuration. Never raises.
    """
    globals_ = {
        "env": MPESA_ENV,
        "consumer_key": MPESA_CONSUMER_KEY,
        "consumer_secret": MPESA_CONSUMER_SECRET,
        "shortcode": MPESA_SHORTCODE,
        "base_url": BASE_URL,
    }
    if not slug:
        return globals_
    p = _slug_prefix(slug)
    env_  = os.environ.get(f"MPESA_{p}_ENV", "").strip().lower()
    key   = os.environ.get(f"MPESA_{p}_CONSUMER_KEY", "").strip()
    sec   = os.environ.get(f"MPESA_{p}_CONSUMER_SECRET", "").strip()
    short = os.environ.get(f"MPESA_{p}_SHORTCODE", "").strip()
    if not (key and sec and short):
        return globals_
    if env_ not in ("sandbox", "production"):
        env_ = MPESA_ENV
    base = ("https://api.safaricom.co.ke" if env_ == "production"
            else "https://sandbox.safaricom.co.ke")
    return {
        "env": env_,
        "consumer_key": key,
        "consumer_secret": sec,
        "shortcode": short,
        "base_url": base,
    }


_token_cache_by_key = {}


def _token_cache_key(creds: dict) -> str:
    return f"{creds.get('env')}|{creds.get('shortcode')}|{creds.get('consumer_key', '')[:8]}"


def get_access_token_for(creds: dict, force: bool = False) -> dict:
    """Like get_access_token(), but keyed by a credential set."""
    key = _token_cache_key(creds)
    now = time.time()
    with _token_lock:
        entry = _token_cache_by_key.get(key)
        if not force and entry and now < entry["expires_at"]:
            return {"ok": True, "token": entry["token"]}

        if not (creds.get("consumer_key") and creds.get("consumer_secret")):
            return {"ok": False, "error": "M-Pesa not configured for this tenant."}

        url = f"{creds['base_url']}/oauth/v1/generate?grant_type=client_credentials"
        auth = base64.b64encode(
            f"{creds['consumer_key']}:{creds['consumer_secret']}".encode()
        ).decode()
        try:
            r = requests.get(url,
                             headers={"Authorization": f"Basic {auth}"},
                             timeout=20)
            if r.status_code != 200:
                return {"ok": False,
                        "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            data = r.json()
            token = data.get("access_token")
            expires = int(data.get("expires_in", 3599))
            if not token:
                return {"ok": False, "error": "No token in response"}
            _token_cache_by_key[key] = {
                "token": token,
                "expires_at": now + max(expires - 60, 60),
            }
            return {"ok": True, "token": token}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def credentials_for_tenant_dispensing(slug: str = None) -> dict:
    """Return the DISPENSING M-Pesa credentials for a tenant.

    Reads env vars MPESA_<SLUG_UPPER>_DISPENSING_CONSUMER_KEY / _SECRET.
    Deliberately does NOT fall back to billing credentials — the two
    accounts are separate by design. Returns {'configured': False} if
    dispensing credentials are not configured.
    """
    if not slug:
        return {"configured": False}
    p = _slug_prefix(slug)
    key = os.environ.get(f"MPESA_{p}_DISPENSING_CONSUMER_KEY", "").strip()
    sec = os.environ.get(f"MPESA_{p}_DISPENSING_CONSUMER_SECRET", "").strip()
    if not (key and sec):
        return {"configured": False}
    # Priority chain for env resolution:
    #   1. MPESA_{SLUG}_DISPENSING_ENV  -> explicit dispensing env (recommended)
    #   2. MPESA_{SLUG}_ENV             -> tenant-level env (shared with billing)
    #   3. MPESA_ENV                    -> global env
    #   4. "sandbox"                    -> hardcoded default (never reached in practice)
    env_explicit = os.environ.get(f"MPESA_{p}_DISPENSING_ENV", "").strip().lower()
    if env_explicit in ("sandbox", "production"):
        env_ = env_explicit
        env_source = "explicit"
    else:
        env_tenant = os.environ.get(f"MPESA_{p}_ENV", "").strip().lower()
        if env_tenant in ("sandbox", "production"):
            env_ = env_tenant
            env_source = "tenant"
        else:
            env_ = MPESA_ENV
            env_source = "global"
    base = ("https://api.safaricom.co.ke" if env_ == "production"
            else "https://sandbox.safaricom.co.ke")
    return {
        "configured": True,
        "env": env_,
        "env_source": env_source,
        "consumer_key": key,
        "consumer_secret": sec,
        "base_url": base,
        # shortcode is resolved by caller from tenant.dispensing_paybill
    }


def get_mpesa_config_dispensing(tenant) -> dict:
    """Non-secret dispensing M-Pesa config for admin display."""
    if tenant is None:
        return {"configured": False, "paybill": None, "env": None}
    creds = credentials_for_tenant_dispensing(tenant.slug)
    return {
        "configured": creds.get("configured", False),
        "paybill": tenant.dispensing_paybill,
        "env": creds.get("env"),
        "env_source": creds.get("env_source"),
    }


def register_c2b_urls_for(creds: dict,
                          confirmation_url: str,
                          validation_url: str) -> dict:
    """Register C2B URLs using a specific credential set."""
    tok = get_access_token_for(creds)
    if not tok.get("ok"):
        return tok
    if not creds.get("shortcode"):
        return {"ok": False, "error": "No shortcode configured for this tenant."}
    url = f"{creds['base_url']}/mpesa/c2b/v1/registerurl"
    payload = {
        "ShortCode": creds["shortcode"],
        "ResponseType": "Completed",
        "ConfirmationURL": confirmation_url,
        "ValidationURL": validation_url,
    }
    try:
        r = requests.post(
            url, json=payload,
            headers={
                "Authorization": f"Bearer {tok['token']}",
                "Content-Type": "application/json",
            },
            timeout=20,
        )
        if r.status_code not in (200, 201):
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        data = r.json()
        # Safaricom returns ResponseCode in multiple formats on success:
        # "0", "00", "00000000" (most common for /registerurl) — or int 0.
        # Normalize: any all-zeros string counts as success.
        # Fallback: if ResponseCode missing, trust "Success" description.
        _raw_code = str(data.get("ResponseCode", "")).strip()
        _raw_desc = str(data.get("ResponseDescription", "")).strip()
        if _raw_code:
            _is_success = (_raw_code.lstrip("0") == "")
        else:
            _is_success = _raw_desc.lower() in ("success", "successful")
        if _is_success:
            return {"ok": True, "response": data}
        return {"ok": False, "error": _raw_desc or _raw_code or data}
    except Exception as e:
        return {"ok": False, "error": str(e)}
