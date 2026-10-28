"""Phase 8 — water billing rate tenant-persistent."""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
import billing as billing_mod
from helpers import make_admin, login
from models import Consumer, MeterReading, Tenant, db


def _admin_login(client, tag):
    name = f"admin_br_{tag}_{uuid.uuid4().hex[:6]}"
    make_admin(name)
    login(client, name)
    client.post("/api/admin/profile/switch", json={"to": "billing"})
    return name


def _wipe(tenant_id=1):
    with app_module.app.app_context():
        MeterReading.query.filter_by(tenant_id=tenant_id).delete()
        Consumer.query.filter_by(tenant_id=tenant_id).delete()
        db.session.commit()


def test_compute_amount_default_is_150():
    """Regression: compute_amount() with no rate -> 150.0 default."""
    amt = billing_mod.compute_amount(110.0, 100.0, 0.0)
    assert amt == 1500.0  # 10 m3 * 150
    # Explicit None also defaults
    amt2 = billing_mod.compute_amount(110.0, 100.0, 0.0, rate_per_m3=None)
    assert amt2 == 1500.0


def test_compute_amount_uses_custom_rate():
    """compute_amount respects rate_per_m3 parameter."""
    amt = billing_mod.compute_amount(110.0, 100.0, 0.0, rate_per_m3=200.0)
    assert amt == 2000.0  # 10 m3 * 200


def test_tenant_model_has_billing_rate_kes():
    """Model column exists."""
    with app_module.app.app_context():
        t = Tenant.query.filter_by(slug="simon-freshwater").first()
        assert t is not None
        assert hasattr(t, "billing_rate_kes")
        # Migration should have backfilled to 150.0
        assert t.billing_rate_kes is not None
        assert float(t.billing_rate_kes) == 150.0


def test_brand_context_exposes_billing_rate():
    """_brand_context includes formatted billing_rate_kes token."""
    with app_module.app.test_request_context("/"):
        bc = app_module._brand_context()
        assert "billing_rate_kes" in bc
        assert isinstance(bc["billing_rate_kes"], str)
        # Default tenant is backfilled to 150.0
        assert bc["billing_rate_kes"] == "150"


def test_admin_tenants_update_accepts_rate(client):
    """Update endpoint accepts billing_rate_kes and persists it."""
    from helpers import login
    # Super-admin session required — use env-secret fallback is unavailable in
    # test; make a super-admin user.
    _enable = None
    with app_module.app.app_context():
        t = Tenant.query.filter_by(slug="simon-freshwater").first()
        tid = t.id
        original = t.billing_rate_kes
    name = f"admin_super_{uuid.uuid4().hex[:6]}"
    make_admin(name, is_super=True)
    login(client, name)
    # Clear pending chooser (both profiles enabled by conftest fixture)
    client.post("/api/admin/profile/switch", json={"to": "billing"})
    r = client.post(f"/api/admin/tenants/{tid}/update",
                    json={"billing_rate_kes": 175.5})
    assert r.status_code == 200, f"body={r.get_json()}"
    d = r.get_json()
    assert d["tenant"]["billing_rate_kes"] == 175.5
    # Restore
    client.post(f"/api/admin/tenants/{tid}/update",
                json={"billing_rate_kes": float(original) if original is not None else 150.0})


def test_admin_tenants_update_rejects_negative_rate(client):
    with app_module.app.app_context():
        t = Tenant.query.filter_by(slug="simon-freshwater").first()
        tid = t.id
    name = f"admin_super2_{uuid.uuid4().hex[:6]}"
    make_admin(name, is_super=True)
    login(client, name)
    # Clear pending chooser (both profiles enabled by conftest fixture)
    client.post("/api/admin/profile/switch", json={"to": "billing"})
    r = client.post(f"/api/admin/tenants/{tid}/update",
                    json={"billing_rate_kes": -50})
    assert r.status_code == 400
    assert "must be >= 0" in (r.get_json().get("error") or "")


def test_tenant_to_dict_exposes_rate():
    with app_module.app.app_context():
        t = Tenant.query.filter_by(slug="simon-freshwater").first()
        d = app_module._tenant_to_dict(t)
        assert "billing_rate_kes" in d
        assert d["billing_rate_kes"] is not None
