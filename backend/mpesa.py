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
        if str(data.get("ResponseCode", "")) == "0":
            return {"ok": True, "response": data}
        return {"ok": False,
                "error": data.get("ResponseDescription") or data}
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
