"""Phase 2b — auto-close wiring into /today and /window."""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login
from models import (
    DispenseMeterReading, DispenseSale, DispenseRate,
    TenantProfile, db,
)
from datetime import timedelta


def _admin_login(client, tag):
    name = f"admin_na2b_{tag}_{uuid.uuid4().hex[:6]}"
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


def _wipe(tenant_id=1):
    with app_module.app.app_context():
        DispenseMeterReading.query.filter_by(tenant_id=tenant_id).delete()
        DispenseSale.query.filter_by(tenant_id=tenant_id).delete()
        db.session.commit()


def _seed_closed(tenant_id=1, days_ago=3, closing=100.0):
    with app_module.app.app_context():
        d = app_module._nairobi_today() - timedelta(days=days_ago)
        db.session.add(DispenseMeterReading(
            tenant_id=tenant_id, reading_date=d,
            opening_reading_m3=closing, closing_reading_m3=closing,
            consumption_m3=0.0, is_closed=True, auto_closed=False,
            recorded_by="test"))
        db.session.commit()
        return d


def _seed_open(tenant_id=1, days_ago=1, opening=100.0):
    with app_module.app.app_context():
        d = app_module._nairobi_today() - timedelta(days=days_ago)
        db.session.add(DispenseMeterReading(
            tenant_id=tenant_id, reading_date=d,
            opening_reading_m3=opening, closing_reading_m3=opening,
            consumption_m3=0.0, is_closed=False, auto_closed=False,
            recorded_by="test"))
        db.session.commit()
        return d


def test_today_view_triggers_auto_close(client):
    """GET /today (today-view) auto-closes no-activity past days."""
    _enable_dispensing(); _wipe()
    _admin_login(client, "tc1")
    today = app_module._nairobi_today()
    _seed_closed(days_ago=3, closing=100.0)
    day_2 = today - timedelta(days=2)
    day_1 = today - timedelta(days=1)
    _seed_open(days_ago=2, opening=100.0)
    _seed_open(days_ago=1, opening=100.0)

    r = client.get("/api/admin/dispense/today")
    assert r.status_code == 200

    with app_module.app.app_context():
        r2 = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=day_2).first()
        r1 = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=day_1).first()
        assert r2 is not None and r2.is_closed is True
        assert r2.auto_closed is True
        assert r1 is not None and r1.is_closed is True
        assert r1.auto_closed is True


def test_window_triggers_auto_close(client):
    """GET /window auto-closes no-activity past days."""
    _enable_dispensing(); _wipe()
    _admin_login(client, "tc2")
    today = app_module._nairobi_today()
    _seed_closed(days_ago=3, closing=100.0)
    day_1 = today - timedelta(days=1)
    _seed_open(days_ago=1, opening=100.0)

    r = client.get("/api/admin/dispense/window?days=7")
    assert r.status_code == 200

    with app_module.app.app_context():
        r1 = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=day_1).first()
        assert r1 is not None and r1.is_closed is True
        assert r1.auto_closed is True


def test_today_past_day_view_does_not_auto_close(client):
    """GET /today?date=<past> does NOT auto-close (mutation guard)."""
    _enable_dispensing(); _wipe()
    _admin_login(client, "tc3")
    today = app_module._nairobi_today()
    _seed_closed(days_ago=5, closing=100.0)
    past_view = today - timedelta(days=3)
    _seed_open(days_ago=3, opening=100.0)

    r = client.get(f"/api/admin/dispense/today?date={past_view.isoformat()}")
    assert r.status_code == 200

    with app_module.app.app_context():
        r3 = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=past_view).first()
        # Past-day view must NOT close the row — mutation guard
        assert r3.is_closed is False
        assert r3.auto_closed is False


def test_today_view_idempotent_double_call(client):
    """Two GET /today calls — same result, no duplicates, no drift."""
    _enable_dispensing(); _wipe()
    _admin_login(client, "tc4")
    today = app_module._nairobi_today()
    _seed_closed(days_ago=2, closing=100.0)
    day_1 = today - timedelta(days=1)
    _seed_open(days_ago=1, opening=100.0)

    r1 = client.get("/api/admin/dispense/today")
    assert r1.status_code == 200

    with app_module.app.app_context():
        c1 = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=day_1).first()
        assert c1 is not None and c1.is_closed is True
        assert c1.auto_closed is True
        c1_id = c1.id
        c1_closing = float(c1.closing_reading_m3)

    r2 = client.get("/api/admin/dispense/today")
    assert r2.status_code == 200

    with app_module.app.app_context():
        c2 = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=day_1).first()
        assert c2.id == c1_id
        assert float(c2.closing_reading_m3) == c1_closing
        count = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=day_1).count()
        assert count == 1
