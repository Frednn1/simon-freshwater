"""Phase 2 — auto-derive opening across empty gaps; manual on open gaps."""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login
from models import (
    DispenseMeterReading, DispenseRate, DispenseSale, DispensePayment,
    TenantProfile, db,
)
from datetime import timedelta


def _admin_login(client, tag):
    name = f"admin_p2_{tag}_{uuid.uuid4().hex[:6]}"
    make_admin(name)
    login(client, name)
    client.post("/api/admin/profile/switch", json={"to": "dispensing"})
    return name


def _enable(tenant_id=1):
    with app_module.app.app_context():
        for pname in ("billing", "dispensing"):
            row = TenantProfile.query.filter_by(
                tenant_id=tenant_id, profile_name=pname).first()
            if row: row.enabled = True
            else: db.session.add(TenantProfile(
                tenant_id=tenant_id, profile_name=pname, enabled=True))
        db.session.commit()


def _ensure_rate(tenant_id=1, size=20, price=50.0):
    with app_module.app.app_context():
        r = DispenseRate.query.filter_by(
            tenant_id=tenant_id, container_size_L=size).first()
        if r: r.is_active = True; r.price_kes = price
        else: db.session.add(DispenseRate(
            tenant_id=tenant_id, container_size_L=size,
            price_kes=price, is_active=True))
        db.session.commit()


def _wipe(tenant_id=1):
    with app_module.app.app_context():
        DispenseMeterReading.query.filter_by(tenant_id=tenant_id).delete()
        DispenseSale.query.filter_by(tenant_id=tenant_id).delete()
        DispensePayment.query.filter_by(tenant_id=tenant_id).delete()
        db.session.commit()


def _seed(tenant_id, days_ago, opening, closing, is_closed):
    with app_module.app.app_context():
        d = app_module._nairobi_today() - timedelta(days=days_ago)
        db.session.add(DispenseMeterReading(
            tenant_id=tenant_id, reading_date=d,
            opening_reading_m3=opening,
            closing_reading_m3=closing,
            consumption_m3=(closing - opening) if is_closed else 0.0,
            is_closed=is_closed, recorded_by="test"))
        db.session.commit()


def test_suggested_opening_from_closed_across_empty_gap(client):
    """Day1 closed, Day2 empty -> /today?date=today suggests Day1 closing."""
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "p2a")
    _seed(1, days_ago=10, opening=100.0, closing=200.0, is_closed=True)
    _seed(1, days_ago=8, opening=200.0, closing=250.0, is_closed=True)
    # View today (all intermediate days empty)
    r = client.get("/api/admin/dispense/today")
    assert r.status_code == 200
    d = r.get_json()
    assert d["manual_opening_required"] is False
    assert d["suggested_opening_m3"] == 250.0
    # Source should be the most recent closed row
    expected_src = (app_module._nairobi_today() - timedelta(days=8)).isoformat()
    assert d["suggested_opening_source_iso"] == expected_src
    assert d["derivation_reason"] is None


def test_suggested_opening_none_when_open_gap(client):
    """Day10 closed, Day5 open, view today -> manual, open_gap_date=Day5."""
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "p2b")
    _seed(1, days_ago=10, opening=100.0, closing=200.0, is_closed=True)
    _seed(1, days_ago=5, opening=200.0, closing=200.0, is_closed=False)
    r = client.get("/api/admin/dispense/today")
    d = r.get_json()
    assert d["manual_opening_required"] is True
    assert d["suggested_opening_m3"] is None
    assert d["derivation_reason"] == "open_gap"
    expected_gap = (app_module._nairobi_today() - timedelta(days=5)).isoformat()
    assert d["open_gap_date"] == expected_gap


def test_suggested_opening_none_when_bootstrap(client):
    """No rows ever -> manual, reason=bootstrap."""
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "p2c")
    r = client.get("/api/admin/dispense/today")
    d = r.get_json()
    assert d["manual_opening_required"] is True
    assert d["suggested_opening_m3"] is None
    assert d["derivation_reason"] == "bootstrap"


def test_reading_post_uses_derived_opening(client):
    """POST /reading without opening -> derives from most recent closed row."""
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "p2d")
    _seed(1, days_ago=10, opening=100.0, closing=200.0, is_closed=True)
    # Close today (no opening submitted, just closing)
    r = client.post("/api/admin/dispense/reading",
                    json={"closing_reading_m3": 300.0})
    assert r.status_code == 200, f"body={r.get_json()}"
    d = r.get_json()
    assert d["opening_reading_m3"] == 200.0  # derived from Day10
    assert d["is_closed"] is True


def test_reading_post_ignores_client_opening_when_derivable(client):
    """Step 1: opening is immutable — client-supplied opening_reading_m3 is ignored."""
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "p2e")
    _seed(1, days_ago=10, opening=100.0, closing=200.0, is_closed=True)
    # Client submits opening=999 but derivation works from prior closed row (200.0)
    r = client.post("/api/admin/dispense/reading",
                    json={"opening_reading_m3": 999.0,
                          "closing_reading_m3": 1099.0})
    assert r.status_code == 200, f"body={r.get_json()}"
    # Opening should be 200.0 (derived), NOT 999.0 (client value ignored)
    assert r.get_json()["opening_reading_m3"] == 200.0



def test_reading_requires_manual_on_open_gap(client):
    """Open gap in between -> backend demands opening explicitly."""
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "p2f")
    _seed(1, days_ago=10, opening=100.0, closing=200.0, is_closed=True)
    _seed(1, days_ago=5, opening=200.0, closing=200.0, is_closed=False)
    # Without opening -> 400
    r = client.post("/api/admin/dispense/reading",
                    json={"closing_reading_m3": 300.0})
    assert r.status_code == 400
    # With opening -> 200
    r = client.post("/api/admin/dispense/reading",
                    json={"opening_reading_m3": 200.0,
                          "closing_reading_m3": 300.0})
    assert r.status_code == 200

def test_retroactive_create_only_needs_closing(client):
    """Step 2: derivation available → only closing required (opening derived)."""
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "p2g")
    _seed(1, days_ago=15, opening=100.0, closing=250.0, is_closed=True)
    target = app_module._nairobi_today() - timedelta(days=10)
    r = client.post("/api/admin/dispense/reading", json={
        "reading_date": target.isoformat(),
        "closing_reading_m3": 300.0,
        "force": True, "reason": "retroactive-no-opening"})
    assert r.status_code == 200, f"body={r.get_json()}"
    d = r.get_json()
    assert d["is_closed"] is True
    assert d["opening_reading_m3"] == 250.0  # derived from day-15
    assert d["closing_reading_m3"] == 300.0

