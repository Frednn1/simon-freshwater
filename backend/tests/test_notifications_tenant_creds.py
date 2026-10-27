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

