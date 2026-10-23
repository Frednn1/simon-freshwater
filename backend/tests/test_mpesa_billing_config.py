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


def test_config_for_tenant_global_when_no_overrides():
    """Tenant without db fields and no env vars → global fallback."""
    from mpesa import get_mpesa_config_for_tenant
    class T:
        slug = "test-slug-not-configured"
        mpesa_shortcode = None
        mpesa_env = None
    result = get_mpesa_config_for_tenant(T())
    assert result["env_source"] == "global"


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
