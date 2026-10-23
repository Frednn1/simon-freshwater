"""Dispensing M-Pesa configuration + registration tests."""
import os, sys, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login
from models import TenantProfile, Tenant, db


def _admin_login(client, tag):
    name = f"admin_mp_{tag}_{uuid.uuid4().hex[:6]}"
    make_admin(name)
    login(client, name)
    client.post("/api/admin/profile/switch", json={"to": "dispensing"})
    return name


def _enable_dispensing(tenant_id=1):
    with app_module.app.app_context():
        for pname in ("billing", "dispensing"):
            row = TenantProfile.query.filter_by(
                tenant_id=tenant_id, profile_name=pname).first()
            if row: row.enabled = True
            else: db.session.add(TenantProfile(
                tenant_id=tenant_id, profile_name=pname, enabled=True))
        db.session.commit()


def _set_dispensing_paybill(tenant_id=1, paybill="TESTDISP99"):
    with app_module.app.app_context():
        t = Tenant.query.get(tenant_id)
        if not t:
            t = Tenant(id=tenant_id, slug="test-t1",
                       org_name="Test", is_active=True)
            db.session.add(t)
        t.dispensing_paybill = paybill
        db.session.commit()


def test_dispensing_config_endpoint_returns_paybill(client):
    _enable_dispensing()
    _set_dispensing_paybill(paybill="TESTDISP99")
    _admin_login(client, "cfg")
    r = client.get("/api/admin/mpesa/dispensing/config")
    assert r.status_code == 200
    d = r.get_json()
    assert d.get("paybill") == "TESTDISP99"
    # env vars not set in test env → configured=false
    assert d.get("configured") is False


def test_register_requires_dispensing_paybill(client):
    _enable_dispensing()
    _set_dispensing_paybill(paybill=None)
    _admin_login(client, "nopb")
    r = client.post("/api/admin/mpesa/dispensing/register")
    assert r.status_code == 400
    body = r.get_json()
    assert "paybill" in (body.get("error") or "").lower()


def test_register_requires_env_vars(client):
    _enable_dispensing()
    _set_dispensing_paybill(paybill="TESTDISP99")
    _admin_login(client, "noenv")
    # Note: env vars are not set in pytest — this should return the guidance error
    r = client.post("/api/admin/mpesa/dispensing/register")
    assert r.status_code == 400
    body = r.get_json()
    err = (body.get("error") or "").lower()
    assert "dispensing_consumer_key" in err or "dispensing_consumer_secret" in err


def test_register_endpoint_route_exists(client):
    """Verify the endpoint exists and rejects invalid auth (401/200 with admin)."""
    _enable_dispensing()
    _admin_login(client, "route")
    # Should return something (not 404) — 400 due to missing env, or 502 if called Safaricom
    r = client.post("/api/admin/mpesa/dispensing/register")
    assert r.status_code in (400, 502), f"unexpected status: {r.status_code}"


def test_dispensing_page_route_exists(client):
    _enable_dispensing()
    _admin_login(client, "pg")
    r = client.get("/admin/mpesa/dispensing")
    assert r.status_code == 200
    assert b"Dispensing Paybill" in r.data
    assert b"Register Dispensing URLs" in r.data


def test_employee_without_mpesa_permission_cannot_access_config(client):
    """Employee without 'mpesa' permission → 403."""
    _enable_dispensing()
    # Create employee without any permission (all off)
    import uuid as _u
    uname = f"emp_{_u.uuid4().hex[:6]}"
    make_admin(uname, role="employee", profiles='["dispensing"]',
               permissions='{"settings": false, "analytics": false, "reports": false, "bulk_sms": false, "mpesa": false}')
    login(client, uname)
    r = client.get("/api/admin/mpesa/dispensing/config")
    assert r.status_code == 403, f"expected 403, got {r.status_code}"

# ══════════════════════════════════════════════════════════════════════
# Dispensing env — explicit > tenant > global priority
# ══════════════════════════════════════════════════════════════════════

def test_dispensing_env_explicit_overrides(monkeypatch):
    """Explicit dispensing env var wins over tenant + global env."""
    from mpesa import credentials_for_tenant_dispensing
    monkeypatch.setenv("MPESA_DEMO_TENANT_1_DISPENSING_CONSUMER_KEY", "k")
    monkeypatch.setenv("MPESA_DEMO_TENANT_1_DISPENSING_CONSUMER_SECRET", "s")
    monkeypatch.setenv("MPESA_DEMO_TENANT_1_DISPENSING_ENV", "production")
    monkeypatch.setenv("MPESA_DEMO_TENANT_1_ENV", "sandbox")
    monkeypatch.setenv("MPESA_ENV", "sandbox")
    creds = credentials_for_tenant_dispensing("demo-tenant-1")
    assert creds["configured"] is True
    assert creds["env"] == "production"
    assert creds["env_source"] == "explicit"


def test_dispensing_env_falls_back_to_tenant(monkeypatch):
    """When explicit dispensing env absent, tenant env is used."""
    from mpesa import credentials_for_tenant_dispensing
    monkeypatch.setenv("MPESA_DEMO_TENANT_2_DISPENSING_CONSUMER_KEY", "k")
    monkeypatch.setenv("MPESA_DEMO_TENANT_2_DISPENSING_CONSUMER_SECRET", "s")
    monkeypatch.delenv("MPESA_DEMO_TENANT_2_DISPENSING_ENV", raising=False)
    monkeypatch.setenv("MPESA_DEMO_TENANT_2_ENV", "production")
    creds = credentials_for_tenant_dispensing("demo-tenant-2")
    assert creds["configured"] is True
    assert creds["env"] == "production"
    assert creds["env_source"] == "tenant"

