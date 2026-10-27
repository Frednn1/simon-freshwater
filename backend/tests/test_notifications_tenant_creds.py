"""Phase 1 — per-tenant credentials resolver unit tests.

These test pure functions. No HTTP, no DB. Fast, deterministic.
Behavior change: NONE — resolvers are unused by production paths in Phase 1.
"""
import sys, os
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import notifications as notif


# ══════════════════════════════════════════════════════════════════════
# SMSGate resolver
# ══════════════════════════════════════════════════════════════════════

def test_slug_to_env_suffix():
    assert notif._slug_to_env_suffix("demo-tenant") == "DEMO_TENANT"
    assert notif._slug_to_env_suffix("kikuyu") == "KIKUYU"
    assert notif._slug_to_env_suffix("") == ""
    assert notif._slug_to_env_suffix(None) == ""


def test_smsgate_no_slug_returns_global(monkeypatch):
    monkeypatch.setattr(notif, "SMSGATE_USERNAME", "g_user")
    monkeypatch.setattr(notif, "SMSGATE_PASSWORD", "g_pass")
    monkeypatch.setattr(notif, "SMSGATE_DEVICE_ID", "g_dev")
    monkeypatch.setattr(notif, "SMSGATE_SIM_NUMBER", "1")
    c = notif.smsgate_credentials_for_tenant(None)
    assert c["username"] == "g_user"
    assert c["source"] == "global"


def test_smsgate_falls_back_when_no_tenant_env(monkeypatch):
    monkeypatch.setattr(notif, "SMSGATE_USERNAME", "g_user")
    monkeypatch.setattr(notif, "SMSGATE_PASSWORD", "g_pass")
    monkeypatch.setattr(notif, "SMSGATE_DEVICE_ID", "g_dev")
    monkeypatch.delenv("SMSGATE_DEMO_USERNAME", raising=False)
    monkeypatch.delenv("SMSGATE_DEMO_PASSWORD", raising=False)
    c = notif.smsgate_credentials_for_tenant("demo")
    assert c["username"] == "g_user"
    assert c["source"] == "global"


def test_smsgate_prefers_tenant_when_both_set(monkeypatch):
    monkeypatch.setattr(notif, "SMSGATE_SIM_NUMBER", "1")
    monkeypatch.setenv("SMSGATE_DEMO_USERNAME", "t_user")
    monkeypatch.setenv("SMSGATE_DEMO_PASSWORD", "t_pass")
    monkeypatch.setenv("SMSGATE_DEMO_DEVICE_ID", "t_dev")
    c = notif.smsgate_credentials_for_tenant("demo")
    assert c["username"] == "t_user"
    assert c["password"] == "t_pass"
    assert c["device_id"] == "t_dev"
    assert c["sim_number"] == "1"           # sim always global
    assert c["source"] == "tenant"


def test_smsgate_tenant_incomplete_falls_back(monkeypatch):
    """Both user+pass required; user alone is not enough."""
    monkeypatch.setattr(notif, "SMSGATE_USERNAME", "g_user")
    monkeypatch.setattr(notif, "SMSGATE_PASSWORD", "g_pass")
    monkeypatch.setenv("SMSGATE_DEMO_USERNAME", "t_user")
    monkeypatch.delenv("SMSGATE_DEMO_PASSWORD", raising=False)
    c = notif.smsgate_credentials_for_tenant("demo")
    assert c["username"] == "g_user"
    assert c["source"] == "global"


# ══════════════════════════════════════════════════════════════════════
# WhatsApp resolver
# ══════════════════════════════════════════════════════════════════════

def test_wa_no_slug_returns_global(monkeypatch):
    monkeypatch.setattr(notif, "WA_ACCESS_TOKEN", "g_tok")
    monkeypatch.setattr(notif, "WA_PHONE_NUMBER_ID", "g_pid")
    c = notif.wa_credentials_for_tenant(None)
    assert c["access_token"] == "g_tok"
    assert c["source"] == "global"


def test_wa_prefers_tenant_when_both_set(monkeypatch):
    monkeypatch.setenv("WA_DEMO_TENANT_ACCESS_TOKEN", "t_tok")
    monkeypatch.setenv("WA_DEMO_TENANT_PHONE_NUMBER_ID", "t_pid")
    c = notif.wa_credentials_for_tenant("demo-tenant")
    assert c["access_token"] == "t_tok"
    assert c["phone_number_id"] == "t_pid"
    assert c["source"] == "tenant"


def test_wa_tenant_incomplete_falls_back(monkeypatch):
    monkeypatch.setattr(notif, "WA_ACCESS_TOKEN", "g_tok")
    monkeypatch.setattr(notif, "WA_PHONE_NUMBER_ID", "g_pid")
    monkeypatch.setenv("WA_DEMO_ACCESS_TOKEN", "t_tok")
    monkeypatch.delenv("WA_DEMO_PHONE_NUMBER_ID", raising=False)
    c = notif.wa_credentials_for_tenant("demo")
    assert c["access_token"] == "g_tok"
    assert c["source"] == "global"


def test_resolvers_never_raise(monkeypatch):
    """Defensive: hostile inputs don't crash."""
    import notifications as _n
    # Bad slug types
    assert _n.smsgate_credentials_for_tenant(123)["source"] == "global"
    assert _n.wa_credentials_for_tenant({})["source"] == "global"
    assert _n.smsgate_credentials_for_tenant("")["source"] == "global"

# ══════════════════════════════════════════════════════════════════════
# Phase 2 — WA per-call URL + per-tenant resolution
# ══════════════════════════════════════════════════════════════════════

def test_wa_api_url_per_phone_id():
    """GAP-CRIT-1: URL is built per-call from phone_number_id, not cached."""
    assert notif._wa_api_url_for("123") == "https://graph.facebook.com/v21.0/123/messages"
    assert notif._wa_api_url_for("") == ""
    assert notif._wa_api_url_for(None) == ""


def test_get_available_channels_no_args_backward_compat(monkeypatch):
    """Backward compat: get_available_channels() with no args preserves today's behavior."""
    monkeypatch.setattr(notif, "SMSGATE_USERNAME", "u")
    monkeypatch.setattr(notif, "SMSGATE_PASSWORD", "p")
    monkeypatch.setattr(notif, "WA_PHONE_NUMBER_ID", "pid")
    monkeypatch.setattr(notif, "WA_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WA_ENABLED", "true")
    # no tenant, no override -> env only
    channels = notif.get_available_channels()
    assert "sms" in channels
    assert "whatsapp" in channels


def test_get_available_channels_tenant_override_false(monkeypatch):
    """Tenant wa_enabled=False overrides env WA_ENABLED=true."""
    monkeypatch.setattr(notif, "WA_PHONE_NUMBER_ID", "pid")
    monkeypatch.setattr(notif, "WA_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WA_ENABLED", "true")
    channels = notif.get_available_channels(wa_enabled_override=False)
    assert "whatsapp" not in channels


def test_get_available_channels_tenant_override_true(monkeypatch):
    """Tenant wa_enabled=True forces WA on even when env says false."""
    monkeypatch.setattr(notif, "WA_PHONE_NUMBER_ID", "pid")
    monkeypatch.setattr(notif, "WA_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WA_ENABLED", "false")
    channels = notif.get_available_channels(wa_enabled_override=True)
    assert "whatsapp" in channels

# ══════════════════════════════════════════════════════════════════════
# Phase 3 — JWT cache per user + auto-detect + tenant threading + SIM
# ══════════════════════════════════════════════════════════════════════

def test_auto_detect_tenant_slug_no_request_context():
    """Outside request context -> None."""
    assert notif._auto_detect_tenant_slug() is None


def test_auto_detect_tenant_slug_with_context():
    """Inside request context with g.tenant -> slug."""
    from flask import g
    import app as app_module
    with app_module.app.test_request_context():
        class _T:
            slug = "demo-tenant"
        g.tenant = _T()
        assert notif._auto_detect_tenant_slug() == "demo-tenant"


def test_auto_detect_tenant_slug_context_no_tenant():
    """Inside request context, no g.tenant -> None."""
    import app as app_module
    with app_module.app.test_request_context():
        assert notif._auto_detect_tenant_slug() is None


def test_tokens_cache_is_per_username(monkeypatch):
    """GAP-CRIT-5: token cache keyed by username -> no cross-tenant leak."""
    from datetime import datetime, timezone, timedelta
    notif._tokens.clear()
    notif._jwt_supported = True

    def fake_fetch(user, pw):
        return {
            "access_token":  f"tok_{user}",
            "refresh_token": f"ref_{user}",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
    def fake_refresh(_rt):
        raise AssertionError("refresh should not fire on fresh cache")
    monkeypatch.setattr(notif, "_fetch_token_pair", fake_fetch)
    monkeypatch.setattr(notif, "_refresh_access_token", fake_refresh)

    t1 = notif._get_access_token("alice", "pw1")
    t2 = notif._get_access_token("bob", "pw2")
    assert t1 == "tok_alice"
    assert t2 == "tok_bob"

    # Cache hit for alice -> no re-fetch
    calls = []
    def spy_fetch(user, pw):
        calls.append(user); return fake_fetch(user, pw)
    monkeypatch.setattr(notif, "_fetch_token_pair", spy_fetch)
    assert notif._get_access_token("alice", "pw1") == "tok_alice"
    assert calls == []


def test_send_sms_signature_accepts_tenant_slug():
    import inspect
    assert "tenant_slug" in inspect.signature(notif.send_sms).parameters


def test_send_sms_with_retry_signature_accepts_tenant_slug():
    import inspect
    assert "tenant_slug" in inspect.signature(notif.send_sms_with_retry).parameters


def test_smsgate_sim_number_per_tenant(monkeypatch):
    """D4: SIM_NUMBER per-tenant via env suffix."""
    monkeypatch.setattr(notif, "SMSGATE_SIM_NUMBER", "0")
    monkeypatch.setenv("SMSGATE_DEMO_USERNAME", "tu")
    monkeypatch.setenv("SMSGATE_DEMO_PASSWORD", "tp")
    monkeypatch.setenv("SMSGATE_DEMO_SIM_NUMBER", "2")
    c = notif.smsgate_credentials_for_tenant("demo")
    assert c["sim_number"] == "2"
    assert c["source"] == "tenant"


def test_send_whatsapp_auto_detects_tenant(monkeypatch):
    """Phase 3: send_whatsapp auto-detects from g.tenant."""
    seen = []
    def fake_resolve(slug):
        seen.append(slug)
        return {"phone_number_id": "gpid", "access_token": "gtok", "source": "global"}
    monkeypatch.setattr(notif, "wa_credentials_for_tenant", fake_resolve)
    class _R:
        status_code = 200
        def json(self): return {"messages": [{"id": "x"}]}
    monkeypatch.setattr(notif.requests, "post", lambda *a, **k: _R())
    monkeypatch.setattr(notif, "_auto_detect_tenant_slug", lambda: "auto-slug")
    notif.send_whatsapp("+254700000000", "hi")
    assert seen == ["auto-slug"]


def test_bulk_sms_passes_tenant_slug_to_send_sms():
    """GAP-CRIT-2: verified via source inspection (avoids heavy DB mocking).

    Path uses _BACKEND (absolute) instead of "backend/..." relative —
    pytest runs with CWD=backend/, so relative "backend/..." would look
    for backend/backend/...
    """
    with open(os.path.join(_BACKEND, "bulk_sms.py")) as f:
        src = f.read()
    assert "send_sms(c.contact, body," in src
    assert "tenant_slug=(tenant.slug if tenant else None)" in src


def test_bulk_whatsapp_send_bill_passes_tenant_slug():
    """GAP-6: _bulk_whatsapp_send_bill passes tenant_slug to send_whatsapp_document.

    Path uses _BACKEND (absolute) instead of "backend/..." relative —
    pytest runs with CWD=backend/.
    """
    with open(os.path.join(_BACKEND, "app.py")) as f:
        src = f.read()
    idx = src.find("def _bulk_whatsapp_send_bill")
    assert idx > 0
    nxt = src.find("\ndef ", idx + 50)
    body = src[idx:nxt if nxt > 0 else idx+6000]
    assert "tenant_slug=(tenant.slug if tenant else None)" in body

# ══════════════════════════════════════════════════════════════════════
# Phase 4 — SMS availability per-tenant + notify_consumer WA gate
# ══════════════════════════════════════════════════════════════════════

def test_get_available_channels_sms_per_tenant_when_globals_unset(monkeypatch):
    """Phase 4: SMS detected via per-tenant env creds when globals are empty."""
    monkeypatch.setattr(notif, "SMSGATE_USERNAME", "")
    monkeypatch.setattr(notif, "SMSGATE_PASSWORD", "")
    monkeypatch.setenv("SMSGATE_DEMO_TENANT_USERNAME", "tu")
    monkeypatch.setenv("SMSGATE_DEMO_TENANT_PASSWORD", "tp")
    channels = notif.get_available_channels(tenant_slug="demo-tenant")
    assert "sms" in channels


def test_get_available_channels_sms_backward_compat_globals(monkeypatch):
    """Phase 4: with tenant_slug=None and globals set, SMS still returned (unchanged)."""
    monkeypatch.setattr(notif, "SMSGATE_USERNAME", "gu")
    monkeypatch.setattr(notif, "SMSGATE_PASSWORD", "gp")
    channels = notif.get_available_channels()
    assert "sms" in channels


def test_notify_consumer_wa_blocked_by_tenant_override(client, monkeypatch):
    """Phase 4: notify_consumer blocked when tenant wa_enabled=False even if env is true."""
    import app as app_module
    from helpers import make_admin, login
    from models import Consumer, db
    from datetime import date

    make_admin("admin_nc_blk_XX")
    login(client, "admin_nc_blk_XX")
    if client.get("/api/admin/whoami").get_json().get("pending_profile_choice"):
        client.post("/api/admin/profile/switch", json={"to": "billing"})

    with app_module.app.app_context():
        c = Consumer(
            tenant_id=1, cust_name="NC Blk XX",
            acc_name="NC Blk XX", meter_acc_no="MTR-NCBLK-XX",
            contact="0700000000", is_active=True,
        )
        db.session.add(c)
        db.session.commit()
        cid = c.id
        # Set tenant wa_enabled = False
        t = app_module.Tenant.query.get(1)
        t.wa_enabled = False
        db.session.commit()

    # Also enable WA_ENABLED env so this test proves tenant override wins
    monkeypatch.setenv("WA_ENABLED", "true")

    r = client.post(f"/api/consumer/{cid}/notify", json={"channel": "whatsapp"})
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.get_json()}"
    assert "disabled" in (r.get_json().get("error") or "").lower()


def test_notify_consumer_wa_allowed_by_tenant_override(client, monkeypatch):
    """Phase 4: notify_consumer NOT blocked by WA gate when tenant wa_enabled=True."""
    import app as app_module
    from helpers import make_admin, login
    from models import Consumer, db

    make_admin("admin_nc_ok_XX")
    login(client, "admin_nc_ok_XX")
    if client.get("/api/admin/whoami").get_json().get("pending_profile_choice"):
        client.post("/api/admin/profile/switch", json={"to": "billing"})

    with app_module.app.app_context():
        c = Consumer(
            tenant_id=1, cust_name="NC Ok XX",
            acc_name="NC Ok XX", meter_acc_no="MTR-NCOK-XX",
            contact="0700000000", is_active=True,
        )
        db.session.add(c)
        db.session.commit()
        cid = c.id
        t = app_module.Tenant.query.get(1)
        t.wa_enabled = True
        db.session.commit()

    # Env is disabled — tenant override must allow it
    monkeypatch.setenv("WA_ENABLED", "false")

    # The endpoint will likely fail later for other reasons (no readings),
    # but the WA gate must NOT return the disabled error.
    r = client.post(f"/api/consumer/{cid}/notify", json={"channel": "whatsapp"})
    # Should not be the WA-disabled gate
    err = (r.get_json() or {}).get("error", "").lower()
    assert "disabled" not in err, f"wa gate wrongly blocked: {r.get_json()}"

# ══════════════════════════════════════════════════════════════════════
# Phase 4.5 — SMSGate DB overrides (device_id + sim_number)
# ══════════════════════════════════════════════════════════════════════

def test_smsgate_resolver_db_overrides_env(monkeypatch):
    """Phase 4.5: DB values (via tenant_row) beat env-suffix."""
    monkeypatch.setenv("SMSGATE_DEMO_USERNAME", "tu")
    monkeypatch.setenv("SMSGATE_DEMO_PASSWORD", "tp")
    monkeypatch.setenv("SMSGATE_DEMO_DEVICE_ID", "env-dev")
    monkeypatch.setenv("SMSGATE_DEMO_SIM_NUMBER", "1")

    class _Row:
        smsgate_device_id = "db-dev"
        smsgate_sim_number = 2

    c = notif.smsgate_credentials_for_tenant("demo", tenant_row=_Row())
    assert c["device_id"] == "db-dev"     # DB wins
    assert c["sim_number"] == "2"          # DB wins
    assert c["username"] == "tu"           # env-suffix still wins for credentials
    assert c["source"] == "tenant"


def test_smsgate_resolver_db_overrides_global(monkeypatch):
    """Phase 4.5: DB values beat global env too."""
    monkeypatch.setattr(notif, "SMSGATE_USERNAME", "gu")
    monkeypatch.setattr(notif, "SMSGATE_PASSWORD", "gp")
    monkeypatch.setattr(notif, "SMSGATE_DEVICE_ID", "global-dev")
    monkeypatch.setattr(notif, "SMSGATE_SIM_NUMBER", "0")

    class _Row:
        smsgate_device_id = "db-dev"
        smsgate_sim_number = 1

    c = notif.smsgate_credentials_for_tenant(None, tenant_row=_Row())
    assert c["device_id"] == "db-dev"
    assert c["sim_number"] == "1"
    assert c["source"] == "tenant"


def test_smsgate_resolver_db_null_falls_back(monkeypatch):
    """Phase 4.5: NULL DB columns preserve env fallback."""
    monkeypatch.setattr(notif, "SMSGATE_USERNAME", "gu")
    monkeypatch.setattr(notif, "SMSGATE_PASSWORD", "gp")
    monkeypatch.setattr(notif, "SMSGATE_DEVICE_ID", "global-dev")
    monkeypatch.setattr(notif, "SMSGATE_SIM_NUMBER", "0")

    class _Row:
        smsgate_device_id = None
        smsgate_sim_number = None

    c = notif.smsgate_credentials_for_tenant(None, tenant_row=_Row())
    assert c["device_id"] == "global-dev"    # env used (DB NULL)
    assert c["sim_number"] == "0"
    assert c["source"] == "global"


def test_send_sms_accepts_tenant_row(monkeypatch):
    """Phase 4.5: send_sms accepts tenant_row kwarg (backward compat)."""
    import inspect
    sig = inspect.signature(notif.send_sms)
    assert "tenant_row" in sig.parameters

