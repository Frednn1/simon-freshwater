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
    """True when global M-Pesa credentials (consumer key + secret) exist.
    Shortcode is tenant-persistent (Tenant.mpesa_shortcode); do NOT
    require MPESA_SHORTCODE env var here."""
    return bool(MPESA_CONSUMER_KEY and MPESA_CONSUMER_SECRET)


def get_mpesa_config() -> dict:
    """Non-secret configuration info for diagnostics.
    shortcode is None unless MPESA_SHORTCODE env var is set — normally
    it will be None because shortcode is tenant-persistent (DB field)."""
    return {
        "env": MPESA_ENV,
        "base_url": BASE_URL,
        "shortcode": MPESA_SHORTCODE or None,
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


def simulate_c2b_payment(amount: int, msisdn: str, bill_ref_number: str,
                         creds: dict = None) -> dict:
    """
    Sandbox only: ask Safaricom to fire a test C2B confirmation at our URL.
    Production does not expose this endpoint.

    When creds is provided (tenant-scoped), uses creds['shortcode'] and
    creds['base_url'] — env vars are only a fallback for legacy callers.
    """
    if creds:
        _env = creds.get("env", "sandbox")
        _shortcode = creds.get("shortcode")
        _base_url = creds.get("base_url", BASE_URL)
    else:
        _env = MPESA_ENV
        _shortcode = MPESA_SHORTCODE
        _base_url = BASE_URL

    if _env == "production":
        return {"ok": False, "error": "Simulation is sandbox-only."}
    if not _shortcode:
        return {"ok": False,
                "error": "No shortcode configured. Set mpesa_shortcode on the tenant."}

    if creds:
        tok = get_access_token_for(creds)
    else:
        tok = get_access_token()
    if not tok.get("ok"):
        return tok

    url = f"{_base_url}/mpesa/c2b/v1/simulate"
    headers = {
        "Authorization": f"Bearer {tok['token']}",
        "Content-Type": "application/json",
    }
    digits = "".join(c for c in str(msisdn) if c.isdigit())
    payload = {
        "ShortCode": _shortcode,
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



def get_mpesa_config_for_tenant(tenant) -> dict:
    """Non-secret M-Pesa config resolved for a specific tenant.

    Reflects exactly what admin_mpesa_register would actually use:
    env vars + Tenant.mpesa_shortcode + Tenant.mpesa_env overrides.

    Exposes env_source so the admin UI can show where the resolved value
    came from — mirroring get_mpesa_config_dispensing's behavior.
    """
    base = get_mpesa_config()
    if tenant is None:
        base["env_source"] = "global"
        return base

    creds = credentials_for_tenant(tenant.slug)
    env = creds.get("env") or MPESA_ENV
    # Step 1: shortcode is DB-only — no env fallback. If neither mpesa_shortcode
    # nor paybill is set on the tenant, shortcode stays None.
    shortcode = None

    sources = []
    if getattr(tenant, "mpesa_shortcode", None):
        shortcode = tenant.mpesa_shortcode
        sources.append("db-shortcode")
    elif getattr(tenant, "paybill", None):
        shortcode = tenant.paybill
        sources.append("db-paybill")
    if getattr(tenant, "mpesa_env", None):
        env = tenant.mpesa_env
        sources.append("db-env")

    base_url = ("https://api.safaricom.co.ke" if env == "production"
                else "https://sandbox.safaricom.co.ke")

    # TEMP DIAGNOSTIC (safe, wrapped): exposes env-var NAMES visible to
    # this process and value LENGTHS (never values). Remove after
    # production env issue is diagnosed.
    _diag = {"available": False}
    try:
        _slug_d = tenant.slug if tenant else None
        _prefix_d = _slug_d.upper().replace("-", "_") if _slug_d else None
        _key_name_d = f"MPESA_{_prefix_d}_CONSUMER_KEY" if _prefix_d else None
        _sec_name_d = f"MPESA_{_prefix_d}_CONSUMER_SECRET" if _prefix_d else None
        _env_name_d = f"MPESA_{_prefix_d}_ENV" if _prefix_d else None
        _all_mpesa_keys = sorted([k for k in os.environ.keys()
                                  if k.startswith("MPESA_")])
        _diag = {
            "available": True,
            "tenant_slug": _slug_d,
            "computed_prefix": _prefix_d,
            "expected_key_name": _key_name_d,
            "expected_secret_name": _sec_name_d,
            "expected_env_name": _env_name_d,
            "key_present": (_key_name_d in os.environ) if _key_name_d else False,
            "key_length": len(os.environ.get(_key_name_d, "")) if _key_name_d else 0,
            "secret_present": (_sec_name_d in os.environ) if _sec_name_d else False,
            "secret_length": len(os.environ.get(_sec_name_d, "")) if _sec_name_d else 0,
            "env_present": (_env_name_d in os.environ) if _env_name_d else False,
            "env_value": (os.environ.get(_env_name_d, "") if _env_name_d else ""),
            "all_mpesa_env_names_visible_to_process": _all_mpesa_keys,
        }
    except Exception as _e:
        _diag = {"available": False, "error": str(_e)[:100]}

    return {
        "env": env,
        "base_url": base_url,
        "shortcode": shortcode,
        "configured": bool(
            shortcode
            and creds.get("consumer_key")
            and creds.get("consumer_secret")
        ),
        "env_source": "+".join(sources) if sources else "unconfigured",
        "consumer_key_source": creds.get("consumer_key_source", "global"),
        "debug_env": _diag,
    }


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
        "consumer_key_source": "global",
    }
    if not slug:
        return globals_
    p = _slug_prefix(slug)
    env_  = os.environ.get(f"MPESA_{p}_ENV", "").strip().lower()
    key   = os.environ.get(f"MPESA_{p}_CONSUMER_KEY", "").strip()
    sec   = os.environ.get(f"MPESA_{p}_CONSUMER_SECRET", "").strip()
    short = os.environ.get(f"MPESA_{p}_SHORTCODE", "").strip()
    # Step 2: per-tenant shortcode is DB-persistent (Tenant.mpesa_shortcode).
    # Only consumer key + secret are needed for per-tenant credential override.
    # The caller sets creds["shortcode"] from DB (admin_mpesa_register /
    # admin_mpesa_simulate). Requiring short env var here would silently
    # fall back to globals for tenants that use DB shortcode.
    if not (key and sec):
        return globals_
    if env_ not in ("sandbox", "production"):
        env_ = MPESA_ENV
    base = ("https://api.safaricom.co.ke" if env_ == "production"
            else "https://sandbox.safaricom.co.ke")
    return {
        "env": env_,
        "consumer_key": key,
        "consumer_secret": sec,
        "shortcode": short or "",
        "base_url": base,
        "consumer_key_source": "tenant-env",
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
