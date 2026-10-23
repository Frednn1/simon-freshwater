"""Q2 — /today returns yesterday_open + yesterday_iso."""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login
from models import (
    DispenseMeterReading, DispenseSale, DispensePayment,
    DispenseRate, TenantProfile, db,
)
from datetime import timedelta


def _admin_login(client, tag):
    name = f"admin_yo_{tag}_{uuid.uuid4().hex[:6]}"
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


def test_today_reports_yesterday_open_true(client):
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "yo1")
    yesterday = app_module._nairobi_today() - timedelta(days=1)
    with app_module.app.app_context():
        db.session.add(DispenseMeterReading(
            tenant_id=1, reading_date=yesterday,
            opening_reading_m3=100, closing_reading_m3=100,
            consumption_m3=0, is_closed=False, recorded_by="test"))
        db.session.commit()
    r = client.get("/api/admin/dispense/today")
    assert r.status_code == 200
    d = r.get_json()
    assert d["yesterday_open"] is True
    assert d["yesterday_iso"] == yesterday.isoformat()


def test_today_reports_yesterday_open_false_when_closed(client):
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "yo2")
    yesterday = app_module._nairobi_today() - timedelta(days=1)
    with app_module.app.app_context():
        db.session.add(DispenseMeterReading(
            tenant_id=1, reading_date=yesterday,
            opening_reading_m3=100, closing_reading_m3=150,
            consumption_m3=50, is_closed=True, recorded_by="test"))
        db.session.commit()
    r = client.get("/api/admin/dispense/today")
    d = r.get_json()
    assert d["yesterday_open"] is False
    assert d["yesterday_iso"] == yesterday.isoformat()


def test_today_reports_yesterday_open_false_when_no_reading(client):
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "yo3")
    r = client.get("/api/admin/dispense/today")
    d = r.get_json()
    assert d["yesterday_open"] is False
