"""Verify /dispense HTML responses carry Cache-Control: no-store.

Critical for closing the post-logout back-navigation gap: without no-store,
browsers can serve /dispense from bfcache or HTTP cache after logout, showing
the authenticated portal shell to an unauthenticated user.
"""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login
from models import TenantProfile, db


def _enable_dispensing(tenant_id=1):
    with app_module.app.app_context():
        for pname in ("billing", "dispensing"):
            row = TenantProfile.query.filter_by(
                tenant_id=tenant_id, profile_name=pname).first()
            if row:
                row.enabled = True
            else:
                db.session.add(TenantProfile(
                    tenant_id=tenant_id, profile_name=pname, enabled=True))
        db.session.commit()


def _admin_login(client, tag):
    name = f"admin_ns_{tag}_{uuid.uuid4().hex[:6]}"
    make_admin(name)
    login(client, name)
    client.post("/api/admin/profile/switch", json={"to": "dispensing"})
    return name


def test_dispense_page_has_no_store_header(client):
    """Authenticated GET /dispense must return Cache-Control: no-store."""
    _enable_dispensing()
    _admin_login(client, "d1")
    r = client.get("/dispense")
    assert r.status_code == 200
    cc = r.headers.get("Cache-Control", "")
    assert "no-store" in cc, f"expected no-store, got {cc!r}"


def test_dispense_analytics_has_no_store_header(client):
    """Authenticated GET /dispense/analytics must return Cache-Control: no-store."""
    _enable_dispensing()
    _admin_login(client, "d2")
    r = client.get("/dispense/analytics")
    assert r.status_code == 200
    cc = r.headers.get("Cache-Control", "")
    assert "no-store" in cc, f"expected no-store, got {cc!r}"


def test_dispense_unauthenticated_still_redirects(client):
    """Regression: unauthenticated GET /dispense still returns 302."""
    r = client.get("/dispense")
    assert r.status_code == 302
    assert "/admin/login" in (r.headers.get("Location") or "")
