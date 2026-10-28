"""Phase 1 — auto-close helper unit tests (isolated).

Phase 1 ships the helper UNUSED. These tests call it directly to verify
correctness in isolation. No HTTP layer involved. Behavior of the running
site is unchanged by Phase 1.
"""
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


def _seed_anchor(tenant_id=1, days_ago=2, closing=100.0):
    """Seed a user-closed anchor day."""
    with app_module.app.app_context():
        D = app_module._nairobi_today() - timedelta(days=days_ago)
        db.session.add(DispenseMeterReading(
            tenant_id=tenant_id, reading_date=D,
            opening_reading_m3=closing, closing_reading_m3=closing,
            consumption_m3=0.0, is_closed=True, auto_closed=False,
            recorded_by="test"))
        db.session.commit()
        return D


def _seed_open_row(tenant_id=1, days_ago=1, opening=200.0):
    with app_module.app.app_context():
        D = app_module._nairobi_today() - timedelta(days=days_ago)
        db.session.add(DispenseMeterReading(
            tenant_id=tenant_id, reading_date=D,
            opening_reading_m3=opening, closing_reading_m3=opening,
            consumption_m3=0.0, is_closed=False, auto_closed=False,
            recorded_by="test"))
        db.session.commit()
        return D


def test_no_anchor_returns_no_anchor(monkeypatch):
    _enable_dispensing(); _wipe()
    with app_module.app.app_context():
        today = app_module._nairobi_today()
        r = app_module._auto_close_no_activity_days(1, today)
        assert r["reason"] == "no_anchor"
        assert r["closed"] == 0
        assert r["created"] == 0


def test_up_to_date_returns_fast():
    """Anchor is yesterday -> nothing to close."""
    _enable_dispensing(); _wipe()
    _seed_anchor(days_ago=1, closing=100.0)
    with app_module.app.app_context():
        today = app_module._nairobi_today()
        r = app_module._auto_close_no_activity_days(1, today)
        assert r["reason"] == "up_to_date"
        assert r["closed"] == 0
        assert r["created"] == 0


def test_creates_row_for_eventless_missing_day():
    """Anchor 2 days ago; yesterday has no sales, no reading -> create."""
    _enable_dispensing(); _wipe()
    _seed_anchor(days_ago=2, closing=100.0)
    with app_module.app.app_context():
        today = app_module._nairobi_today()
        yesterday = today - timedelta(days=1)
        r = app_module._auto_close_no_activity_days(1, today)
        assert r["created"] == 1
        assert r["closed"] == 0
        row = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=yesterday).first()
        assert row is not None
        assert row.is_closed is True
        assert row.auto_closed is True
        assert float(row.opening_reading_m3) == 100.0
        assert float(row.closing_reading_m3) == 100.0
        assert float(row.consumption_m3) == 0.0


def test_closes_existing_open_eventless_row():
    """Anchor 3 days ago; 2 days ago has open row; yesterday missing -> both closed."""
    _enable_dispensing(); _wipe()
    _seed_anchor(days_ago=3, closing=100.0)
    _seed_open_row(days_ago=2, opening=100.0)
    with app_module.app.app_context():
        today = app_module._nairobi_today()
        r = app_module._auto_close_no_activity_days(1, today)
        # 2 days ago closed (open row), yesterday created
        assert r["closed"] == 1
        assert r["created"] == 1
        # Verify open row was closed
        two_ago = today - timedelta(days=2)
        row2 = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=two_ago).first()
        assert row2.is_closed is True
        assert row2.auto_closed is True
        assert float(row2.consumption_m3) == 0.0


def test_stops_at_day_with_sales():
    """Anchor 3 days ago; 2 days ago has sales -> stop there; yesterday not touched."""
    _enable_dispensing(); _wipe()
    _seed_anchor(days_ago=3, closing=100.0)
    with app_module.app.app_context():
        two_ago = app_module._nairobi_today() - timedelta(days=2)
        db.session.add(DispenseSale(
            tenant_id=1, sale_date=two_ago,
            container_size_L=20, quantity=1,
            price_per_unit_kes=50.0, total_kes=50.0,
            recorded_by="test"))
        db.session.commit()
        today = app_module._nairobi_today()
        r = app_module._auto_close_no_activity_days(1, today)
        assert r["stopped_at"] == two_ago.isoformat()
        # No rows created / closed because chain stopped immediately
        assert r["created"] == 0
        assert r["closed"] == 0
        # Confirm yesterday has no row (was not touched)
        yesterday = today - timedelta(days=1)
        row = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=yesterday).first()
        assert row is None


def test_does_not_touch_today():
    """Anchor yesterday -> today not processed."""
    _enable_dispensing(); _wipe()
    _seed_anchor(days_ago=1, closing=100.0)
    with app_module.app.app_context():
        today = app_module._nairobi_today()
        r = app_module._auto_close_no_activity_days(1, today)
        row = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=today).first()
        assert row is None
        assert r["reason"] == "up_to_date"


def test_idempotent_double_call():
    """Running twice produces no additional rows after first run."""
    _enable_dispensing(); _wipe()
    _seed_anchor(days_ago=2, closing=100.0)
    with app_module.app.app_context():
        today = app_module._nairobi_today()
        r1 = app_module._auto_close_no_activity_days(1, today)
        assert r1["created"] == 1
        r2 = app_module._auto_close_no_activity_days(1, today)
        # Second call: anchor is now yesterday -> up_to_date fast path
        assert r2["reason"] == "up_to_date"
        assert r2["created"] == 0
