"""Phase 1 (GAP-1) — retroactive past-day create/close."""
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
    name = f"admin_rt_{tag}_{uuid.uuid4().hex[:6]}"
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


def _seed_closed(days_ago, opening=100.0, closing=150.0, tenant_id=1):
    with app_module.app.app_context():
        d = app_module._nairobi_today() - timedelta(days=days_ago)
        db.session.add(DispenseMeterReading(
            tenant_id=tenant_id, reading_date=d,
            opening_reading_m3=opening, closing_reading_m3=closing,
            consumption_m3=closing - opening,
            is_closed=True, recorded_by="test"))
        db.session.commit()


def test_retroactive_create_succeeds(client):
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "rt1")
    _seed_closed(days_ago=5, opening=100.0, closing=150.0)
    target = app_module._nairobi_today() - timedelta(days=3)
    r = client.post("/api/admin/dispense/reading", json={
        "reading_date": target.isoformat(),
        "opening_reading_m3": 150.0,
        "closing_reading_m3": 200.0,
        "force": True, "reason": "retroactive-create-test"})
    assert r.status_code == 200, f"body={r.get_json()}"
    d = r.get_json()
    assert d["action"] == "created" or d["action"] == "closed"
    assert d["is_closed"] is True
    # Verify row persisted
    with app_module.app.app_context():
        row = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=target).first()
        assert row is not None
        assert row.is_closed is True


def test_retroactive_create_requires_both_values(client):
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "rt2")
    _seed_closed(days_ago=20, opening=100.0, closing=150.0)
    target = app_module._nairobi_today() - timedelta(days=25)

    # Guarantee no row at target before assertion
    with app_module.app.app_context():
        DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=target).delete()
        db.session.commit()

    # Missing closing → 400
    r = client.post("/api/admin/dispense/reading", json={
        "reading_date": target.isoformat(),
        "opening_reading_m3": 150.0,
        "force": True, "reason": "test-missing-closing"})
    assert r.status_code == 400, (
        f"missing closing: expected 400, got {r.status_code}: {r.get_json()}")

    # Confirm still no row (request was rejected)
    with app_module.app.app_context():
        row = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=target).first()
        assert row is None, "Row created despite missing closing"

    # Missing opening → 400
    r = client.post("/api/admin/dispense/reading", json={
        "reading_date": target.isoformat(),
        "closing_reading_m3": 200.0,
        "force": True, "reason": "test-missing-opening"})
    assert r.status_code == 400, (
        f"missing opening: expected 400, got {r.status_code}: {r.get_json()}")


def test_retroactive_create_requires_force_and_reason(client):
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "rt3")
    _seed_closed(days_ago=5, opening=100.0, closing=150.0)
    target = app_module._nairobi_today() - timedelta(days=3)
    # No force
    r = client.post("/api/admin/dispense/reading", json={
        "reading_date": target.isoformat(),
        "opening_reading_m3": 150.0, "closing_reading_m3": 200.0,
        "reason": "test"})
    assert r.status_code == 400
    assert "force" in (r.get_json().get("error") or "").lower()
    # Force but no reason
    r = client.post("/api/admin/dispense/reading", json={
        "reading_date": target.isoformat(),
        "opening_reading_m3": 150.0, "closing_reading_m3": 200.0,
        "force": True})
    assert r.status_code == 400
    assert "reason" in (r.get_json().get("error") or "").lower()


def test_retroactive_close_existing_open_still_works(client):
    """Regression: existing open past day still closable (unchanged behavior)."""
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "rt4")
    _seed_closed(days_ago=5, opening=100.0, closing=150.0)
    target = app_module._nairobi_today() - timedelta(days=3)
    # Seed open row
    with app_module.app.app_context():
        db.session.add(DispenseMeterReading(
            tenant_id=1, reading_date=target,
            opening_reading_m3=150.0, closing_reading_m3=150.0,
            consumption_m3=0.0, is_closed=False, recorded_by="test"))
        db.session.commit()
    r = client.post("/api/admin/dispense/reading", json={
        "reading_date": target.isoformat(),
        "closing_reading_m3": 200.0,
        "force": True, "reason": "close-open-test"})
    assert r.status_code == 200, f"body={r.get_json()}"
    assert r.get_json()["is_closed"] is True
