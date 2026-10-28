"""Phase 3 — sale-recorded hardening on auto-closed days.

Rules verified:
  R6: sale on auto_closed day -> auto-reopen
  user_closed days still block (unchanged)
  dry-run does not mutate
  reopened day + sale stays open on next /today (auto-close skips days with sales)
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


def _admin_login(client, tag):
    name = f"admin_na3_{tag}_{uuid.uuid4().hex[:6]}"
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
        db.session.commit()


def _seed_day(tenant_id=1, days_ago=2, opening=100.0, closing=100.0,
              is_closed=True, auto_closed=False):
    with app_module.app.app_context():
        d = app_module._nairobi_today() - timedelta(days=days_ago)
        db.session.add(DispenseMeterReading(
            tenant_id=tenant_id, reading_date=d,
            opening_reading_m3=opening, closing_reading_m3=closing,
            consumption_m3=(closing - opening),
            is_closed=is_closed, auto_closed=auto_closed,
            recorded_by="test"))
        db.session.commit()
        return d


def test_bulk_import_reopens_auto_closed_day(client):
    """Bulk import into auto_closed day -> 200, day reopens, sale written."""
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "tc1")
    past = _seed_day(days_ago=2, opening=100.0, closing=100.0,
                     is_closed=True, auto_closed=True)
    csv_text = f"20,2,{past.isoformat()},reopen-test"

    r = client.post("/api/admin/dispense/sales/bulk_import",
                    json={"csv": csv_text, "dry_run": False})
    assert r.status_code == 200, f"body={r.get_json()}"
    body = r.get_json()
    assert body["ok"] is True
    assert body["count"] == 1

    with app_module.app.app_context():
        row = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=past).first()
        assert row.is_closed is False, "auto_closed day not reopened"
        assert row.auto_closed is False, "auto_closed flag not cleared"
        # Sale was written
        sales = DispenseSale.query.filter_by(
            tenant_id=1, sale_date=past).all()
        assert len(sales) == 1
        assert float(sales[0].total_kes) == 100.0  # 20L × 2qty × 50 = 100


def test_bulk_import_still_blocks_user_closed_day(client):
    """Regression: user-closed days remain blocked (unchanged behavior)."""
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "tc2")
    past = _seed_day(days_ago=2, opening=100.0, closing=150.0,
                     is_closed=True, auto_closed=False)
    csv_text = f"20,2,{past.isoformat()},user-closed"

    r = client.post("/api/admin/dispense/sales/bulk_import",
                    json={"csv": csv_text, "dry_run": False})
    assert r.status_code == 400, f"body={r.get_json()}"
    body = r.get_json()
    assert body["ok"] is False
    assert any("closed" in (e.get("error") or "").lower()
               for e in body["errors"])

    # No sale written, day still closed
    with app_module.app.app_context():
        row = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=past).first()
        assert row.is_closed is True
        sales = DispenseSale.query.filter_by(
            tenant_id=1, sale_date=past).all()
        assert len(sales) == 0


def test_bulk_import_multi_row_same_auto_day(client):
    """2 rows for same auto_closed day -> both written, day reopened once."""
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "tc3")
    past = _seed_day(days_ago=2, opening=100.0, closing=100.0,
                     is_closed=True, auto_closed=True)
    csv_text = (f"20,1,{past.isoformat()},r1\n"
                f"20,2,{past.isoformat()},r2")

    r = client.post("/api/admin/dispense/sales/bulk_import",
                    json={"csv": csv_text, "dry_run": False})
    assert r.status_code == 200, f"body={r.get_json()}"
    assert r.get_json()["count"] == 2

    with app_module.app.app_context():
        row = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=past).first()
        assert row.is_closed is False
        assert row.auto_closed is False
        sales = DispenseSale.query.filter_by(
            tenant_id=1, sale_date=past).all()
        assert len(sales) == 2


def test_bulk_import_failfast_on_user_closed_ignores_auto(client):
    """Mixed CSV (1 user-closed + 1 auto-closed) -> fail-fast 400; nothing written."""
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "tc4")
    user_day = _seed_day(days_ago=3, opening=100.0, closing=150.0,
                         is_closed=True, auto_closed=False)
    auto_day = _seed_day(days_ago=2, opening=150.0, closing=150.0,
                         is_closed=True, auto_closed=True)
    csv_text = (f"20,1,{auto_day.isoformat()},would-reopen\n"
                f"20,2,{user_day.isoformat()},blocked")

    r = client.post("/api/admin/dispense/sales/bulk_import",
                    json={"csv": csv_text, "dry_run": False})
    assert r.status_code == 400
    body = r.get_json()
    assert body["ok"] is False

    # Fail-fast: nothing written, auto day still auto_closed
    with app_module.app.app_context():
        user_row = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=user_day).first()
        auto_row = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=auto_day).first()
        assert user_row.is_closed is True
        assert auto_row.is_closed is True
        assert auto_row.auto_closed is True
        assert DispenseSale.query.filter_by(tenant_id=1).count() == 0


def test_auto_close_skips_reopened_day_with_sale(client):
    """After reopen + sale, next /today does not re-close the day."""
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "tc5")
    past = _seed_day(days_ago=2, opening=100.0, closing=100.0,
                     is_closed=True, auto_closed=True)
    # Reopen via bulk import + sale
    csv_text = f"20,2,{past.isoformat()},keep-open"
    r = client.post("/api/admin/dispense/sales/bulk_import",
                    json={"csv": csv_text, "dry_run": False})
    assert r.status_code == 200

    # Trigger auto-close via /today
    r2 = client.get("/api/admin/dispense/today")
    assert r2.status_code == 200

    # Day must remain OPEN (has sale -> auto-close skips it)
    with app_module.app.app_context():
        row = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=past).first()
        assert row.is_closed is False
        assert row.auto_closed is False


def test_bulk_import_dry_run_does_not_reopen(client):
    """dry_run=True on auto_closed day -> 200 dry-run; day stays auto_closed."""
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "tc6")
    past = _seed_day(days_ago=2, opening=100.0, closing=100.0,
                     is_closed=True, auto_closed=True)
    csv_text = f"20,2,{past.isoformat()},dry-run"

    r = client.post("/api/admin/dispense/sales/bulk_import",
                    json={"csv": csv_text, "dry_run": True})
    assert r.status_code == 200, f"body={r.get_json()}"
    assert r.get_json()["dry_run"] is True

    # Day unchanged (dry_run must not mutate)
    with app_module.app.app_context():
        row = DispenseMeterReading.query.filter_by(
            tenant_id=1, reading_date=past).first()
        assert row.is_closed is True
        assert row.auto_closed is True
        assert DispenseSale.query.filter_by(
            tenant_id=1, sale_date=past).count() == 0
