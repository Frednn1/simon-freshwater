"""Phase A3a — past-day view + retroactive close."""
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
    name = f"admin_pd_{tag}_{uuid.uuid4().hex[:6]}"
    make_admin(name)
    login(client, name)
    client.post("/api/admin/profile/switch", json={"to": "dispensing"})
    return name


def _enable_dispensing(tenant_id=1):
    with app_module.app.app_context():
        for pname in ("billing", "dispensing"):
            row = TenantProfile.query.filter_by(
                tenant_id=tenant_id, profile_name=pname
            ).first()
            if row:
                row.enabled = True
            else:
                db.session.add(TenantProfile(
                    tenant_id=tenant_id, profile_name=pname, enabled=True
                ))
        db.session.commit()


def _ensure_rate(tenant_id=1, size=20, price=50.0):
    with app_module.app.app_context():
        r = DispenseRate.query.filter_by(
            tenant_id=tenant_id, container_size_L=size
        ).first()
        if r:
            r.is_active = True
            r.price_kes = price
        else:
            db.session.add(DispenseRate(
                tenant_id=tenant_id, container_size_L=size,
                price_kes=price, is_active=True,
            ))
        db.session.commit()


def _wipe(tenant_id=1):
    with app_module.app.app_context():
        DispenseMeterReading.query.filter_by(tenant_id=tenant_id).delete()
        DispenseSale.query.filter_by(tenant_id=tenant_id).delete()
        DispensePayment.query.filter_by(tenant_id=tenant_id).delete()
        db.session.commit()


def _seed_past_open_reading(days_ago=2, opening=100.0, tenant_id=1):
    with app_module.app.app_context():
        d = app_module._nairobi_today() - timedelta(days=days_ago)
        db.session.add(DispenseMeterReading(
            tenant_id=tenant_id, reading_date=d,
            opening_reading_m3=opening, closing_reading_m3=opening,
            consumption_m3=0.0, is_closed=False, recorded_by="test",
        ))
        db.session.commit()
        return d


def _seed_past_sale(days_ago=2, price=50.0, qty=1, tenant_id=1):
    with app_module.app.app_context():
        d = app_module._nairobi_today() - timedelta(days=days_ago)
        db.session.add(DispenseSale(
            tenant_id=tenant_id, sale_date=d,
            container_size_L=20, quantity=qty,
            price_per_unit_kes=price, total_kes=price * qty,
            recorded_by="test",
        ))
        db.session.commit()
        return d


def test_get_today_with_date_param_past(client):
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "gpast")
    past = _seed_past_open_reading(days_ago=2, opening=100.0)
    r = client.get(f"/api/admin/dispense/today?date={past.isoformat()}")
    assert r.status_code == 200, f"body={r.get_json()}"
    d = r.get_json()
    assert d["is_today"] is False
    assert d["date_iso"] == past.isoformat()


def test_get_today_with_date_future_rejected(client):
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "gfut")
    future = (app_module._nairobi_today() + timedelta(days=1)).isoformat()
    r = client.get(f"/api/admin/dispense/today?date={future}")
    assert r.status_code == 400
    assert "future" in (r.get_json().get("error") or "").lower()


def test_get_today_with_date_too_old_rejected(client):
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "gold")
    old = (app_module._nairobi_today() - timedelta(days=31)).isoformat()
    r = client.get(f"/api/admin/dispense/today?date={old}")
    assert r.status_code == 400
    assert "30 days" in (r.get_json().get("error") or "")


def test_get_sales_with_date_param_past(client):
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "gspast")
    past = _seed_past_sale(days_ago=2, price=50.0, qty=2)
    r = client.get(f"/api/admin/dispense/sales?date={past.isoformat()}")
    assert r.status_code == 200
    d = r.get_json()
    assert d["is_today"] is False
    assert d["date_iso"] == past.isoformat()
    assert d["total_sales_kes"] == 100.0
    assert len(d["sales"]) >= 1


def test_payments_get_shows_credit_offset_targeting_past_day(client):
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "gcrd")
    past = _seed_past_sale(days_ago=3, price=50.0, qty=2)
    today = app_module._nairobi_today()
    with app_module.app.app_context():
        db.session.add(DispensePayment(
            tenant_id=1, payment_date=today, sale_date=past,
            amount_kes=100.0, method="cash",
            reference=f"credit-{uuid.uuid4().hex[:8]}", recorded_by="test",
        ))
        db.session.commit()
    r = client.get(f"/api/admin/dispense/payments?date={past.isoformat()}")
    assert r.status_code == 200
    d = r.get_json()
    targeting = [p for p in d["payments"] if p.get("direction") == "targeting"]
    assert len(targeting) >= 1, f"credit payment not shown: {d}"
    assert d["total_targeting_kes"] == 100.0
    # received that day = 0 (payment received today, not past day)
    assert d["total_received_kes"] == 0.0


def test_close_open_past_day_without_force_rejected(client):
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "cno")
    past = _seed_past_open_reading(days_ago=2, opening=100.0)
    r = client.post("/api/admin/dispense/reading",
                    json={"reading_date": past.isoformat(),
                          "closing_reading_m3": 150.0})
    assert r.status_code == 400
    assert "force" in (r.get_json().get("error") or "").lower()


def test_close_open_past_day_succeeds(client):
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "cyes")
    past = _seed_past_open_reading(days_ago=2, opening=100.0)
    r = client.post("/api/admin/dispense/reading",
                    json={"reading_date": past.isoformat(),
                          "closing_reading_m3": 150.0,
                          "force": True,
                          "reason": "forgot close"})
    assert r.status_code == 200, f"body={r.get_json()}"
    d = r.get_json()
    assert d.get("is_closed") is True


def test_create_new_past_day_reading_now_requires_force_and_full_data(client):
    """GAP-1 fix: retroactive creation is now ALLOWED, but only with
    force+reason+opening+closing. Without all four, still rejected."""
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "cnw")

    # (a) Full data → 200 (new behavior)
    past_a = (app_module._nairobi_today() - timedelta(days=3))
    r = client.post("/api/admin/dispense/reading",
                    json={"reading_date": past_a.isoformat(),
                          "opening_reading_m3": 50.0,
                          "closing_reading_m3": 60.0,
                          "force": True,
                          "reason": "retroactive-create"})
    assert r.status_code == 200, f"body={r.get_json()}"
    assert r.get_json()["is_closed"] is True

    # (b) Missing force → 400
    past_b = (app_module._nairobi_today() - timedelta(days=10))
    r = client.post("/api/admin/dispense/reading",
                    json={"reading_date": past_b.isoformat(),
                          "opening_reading_m3": 60.0,
                          "closing_reading_m3": 70.0,
                          "reason": "no force"})
    assert r.status_code == 400

    # (c) Missing opening or closing → 400
    past_c = (app_module._nairobi_today() - timedelta(days=15))
    r = client.post("/api/admin/dispense/reading",
                    json={"reading_date": past_c.isoformat(),
                          "opening_reading_m3": 70.0,
                          "force": True,
                          "reason": "missing closing"})
    assert r.status_code == 400

