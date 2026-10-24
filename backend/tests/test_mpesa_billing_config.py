"""Billing M-Pesa config — tenant-aware resolution + env_source."""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login
from models import Tenant, TenantProfile, db


def _admin_login(client, tag):
    name = f"adm_bcfg_{tag}_{uuid.uuid4().hex[:6]}"
    make_admin(name)
    login(client, name)
    client.post("/api/admin/profile/switch", json={"to": "billing"})
    return name


def test_config_for_tenant_reflects_db_shortcode():
    """Tenant.mpesa_shortcode overrides the env shortcode."""
    from mpesa import get_mpesa_config_for_tenant
    class T:
        slug = "test-slug-not-configured"
        mpesa_shortcode = "533533"
        mpesa_env = "sandbox"
    result = get_mpesa_config_for_tenant(T())
    assert result["shortcode"] == "533533"
    assert "db-shortcode" in result["env_source"]
    assert "db-env" in result["env_source"]


def test_config_for_tenant_global_when_none():
    """Null tenant → global config + env_source='global'."""
    from mpesa import get_mpesa_config_for_tenant
    result = get_mpesa_config_for_tenant(None)
    assert result["env_source"] == "global"


def test_config_for_tenant_unconfigured_when_no_overrides():
    """Tenant without db fields → env_source is 'unconfigured' and
    shortcode is None. (Step 1: env fallback removed; shortcode is DB-only.)"""
    from mpesa import get_mpesa_config_for_tenant
    class T:
        slug = "test-slug-not-configured"
        mpesa_shortcode = None
        paybill = None
        mpesa_env = None
    result = get_mpesa_config_for_tenant(T())
    assert result["env_source"] == "unconfigured"
    assert result["shortcode"] is None


def test_admin_mpesa_config_endpoint_reflects_tenant_db_shortcode(client):
    """End-to-end: GET /api/admin/mpesa/config returns DB shortcode."""
    with app_module.app.app_context():
        t = Tenant.query.get(1)
        if not t:
            t = Tenant(id=1, slug="test-t1", org_name="Test", is_active=True)
            db.session.add(t)
        old_short = t.mpesa_shortcode
        t.mpesa_shortcode = "TEST-533533"
        db.session.commit()

    try:
        _admin_login(client, "cfg")
        r = client.get("/api/admin/mpesa/config")
        assert r.status_code == 200
        d = r.get_json()
        assert d["shortcode"] == "TEST-533533"
        assert "db-shortcode" in d.get("env_source", "")
    finally:
        with app_module.app.app_context():
            t = Tenant.query.get(1)
            t.mpesa_shortcode = old_short
            db.session.commit()

# ══════════════════════════════════════════════════════════════════════
# Step 1 — tenant-persistent M-Pesa shortcode
# ══════════════════════════════════════════════════════════════════════

def test_register_requires_tenant_shortcode(client):
    with app_module.app.app_context():
        t = Tenant.query.get(1)
        if not t:
            t = Tenant(id=1, slug="test-t1", org_name="Test", is_active=True)
            db.session.add(t)
        old_mp = t.mpesa_shortcode
        old_pb = t.paybill
        t.mpesa_shortcode = None
        t.paybill = None
        db.session.commit()
    try:
        _admin_login(client, "req")
        r = client.post("/api/admin/mpesa/register")
        assert r.status_code == 400
        assert "not set" in (r.get_json().get("error") or "").lower()
    finally:
        with app_module.app.app_context():
            t = Tenant.query.get(1)
            t.mpesa_shortcode = old_mp
            t.paybill = old_pb
            db.session.commit()


def test_register_prefers_db_shortcode_over_env(client):
    with app_module.app.app_context():
        t = Tenant.query.get(1)
        old_mp = t.mpesa_shortcode
        t.mpesa_shortcode = "DB-533533"
        db.session.commit()
    try:
        _admin_login(client, "pref")
        captured = {}
        _orig = app_module.register_c2b_urls_for
        def _fake(creds, conf, val):
            captured.update(creds)
            return {"ok": True, "response": {"ResponseCode": "00000000"}}
        app_module.register_c2b_urls_for = _fake
        try:
            r = client.post("/api/admin/mpesa/register")
            assert r.status_code == 200
            assert captured.get("shortcode") == "DB-533533"
        finally:
            app_module.register_c2b_urls_for = _orig
    finally:
        with app_module.app.app_context():
            t = Tenant.query.get(1)
            t.mpesa_shortcode = old_mp
            db.session.commit()


def test_simulate_requires_tenant_shortcode(client):
    with app_module.app.app_context():
        t = Tenant.query.get(1)
        old_mp = t.mpesa_shortcode
        old_pb = t.paybill
        t.mpesa_shortcode = None
        t.paybill = None
        db.session.commit()
    try:
        _admin_login(client, "sim")
        r = client.post("/api/admin/mpesa/simulate",
                        json={"amount": 10, "msisdn": "254700000000",
                              "bill_ref_number": "X"})
        assert r.status_code == 400
        assert "not configured" in (r.get_json().get("error") or "").lower()
    finally:
        with app_module.app.app_context():
            t = Tenant.query.get(1)
            t.mpesa_shortcode = old_mp
            t.paybill = old_pb
            db.session.commit()

# ══════════════════════════════════════════════════════════════════════
# Step 2 — per-tenant M-Pesa consumer keys (no shortcode env required)
# ══════════════════════════════════════════════════════════════════════

def test_credentials_for_tenant_uses_tenant_keys_without_shortcode(monkeypatch):
    """Per-tenant CONSUMER_KEY+SECRET, no SHORTCODE env var → uses tenant creds.
    (Shortcode is DB-only per Step 1; must not force globals fallback.)"""
    from mpesa import credentials_for_tenant
    monkeypatch.setenv("MPESA_TEST_TENANT_X_CONSUMER_KEY", "tenant-key")
    monkeypatch.setenv("MPESA_TEST_TENANT_X_CONSUMER_SECRET", "tenant-secret")
    creds = credentials_for_tenant("test-tenant-x")
    assert creds["consumer_key"] == "tenant-key"
    assert creds["consumer_secret"] == "tenant-secret"
    assert creds["shortcode"] == ""
    assert creds["consumer_key_source"] == "tenant-env"


def test_credentials_for_tenant_falls_back_on_partial_config(monkeypatch):
    """Only CONSUMER_KEY set (no secret) → falls back to globals (safe)."""
    from mpesa import credentials_for_tenant
    monkeypatch.setenv("MPESA_TEST_TENANT_Y_CONSUMER_KEY", "tenant-key")
    creds = credentials_for_tenant("test-tenant-y")
    assert creds["consumer_key_source"] == "global"


def test_config_for_tenant_shows_consumer_key_source(monkeypatch):
    """get_mpesa_config_for_tenant exposes consumer_key_source field."""
    from mpesa import get_mpesa_config_for_tenant
    class T:
        slug = "test-tenant-z"
        mpesa_shortcode = "123456"
        mpesa_env = None
        paybill = None
    monkeypatch.setenv("MPESA_TEST_TENANT_Z_CONSUMER_KEY", "k")
    monkeypatch.setenv("MPESA_TEST_TENANT_Z_CONSUMER_SECRET", "s")
    result = get_mpesa_config_for_tenant(T())
    assert result["consumer_key_source"] == "tenant-env"
    assert result["shortcode"] == "123456"

