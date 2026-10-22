"""Step 1 batch 2: day-close lock + credit offset enforcement."""
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
    name = f"admin_lk_{tag}_{uuid.uuid4().hex[:6]}"
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


def _wipe_dispense_activity(tenant_id=1):
    with app_module.app.app_context():
        DispenseMeterReading.query.filter_by(tenant_id=tenant_id).delete()
        DispenseSale.query.filter_by(tenant_id=tenant_id).delete()
        DispensePayment.query.filter_by(tenant_id=tenant_id).delete()
        db.session.commit()


def _ensure_today_closed(client):
    d = client.get("/api/admin/dispense/today").get_json()
    if d.get("has_reading") and d.get("is_closed"):
        return
    if d.get("has_reading"):
        client.post("/api/admin/dispense/reading",
                    json={"closing_reading_m3": 100.0})
    else:
        client.post("/api/admin/dispense/reading",
                    json={"opening_reading_m3": 0.0})
        client.post("/api/admin/dispense/reading",
                    json={"closing_reading_m3": 100.0})


def _seed_past_sale(tenant_id=1, days_ago=2, size=20, qty=1, price=50.0):
    with app_module.app.app_context():
        d = app_module._nairobi_today() - timedelta(days=days_ago)
        sale = DispenseSale(
            tenant_id=tenant_id, sale_date=d,
            container_size_L=size, quantity=qty,
            price_per_unit_kes=price, total_kes=price * qty,
            recorded_by="test",
        )
        db.session.add(sale)
        db.session.commit()
        return d


def test_sales_blocked_when_day_closed(client):
    _enable_dispensing(); _ensure_rate(); _wipe_dispense_activity()
    _admin_login(client, "sb")
    _ensure_today_closed(client)
    r = client.post("/api/admin/dispense/sales",
                    json={"container_size_L": 20, "quantity": 1})
    assert r.status_code == 409, f"body={r.get_json()}"
    assert "closed" in (r.get_json().get("error") or "").lower()


def test_sale_void_blocked_when_day_closed(client):
    _enable_dispensing(); _ensure_rate(); _wipe_dispense_activity()
    _admin_login(client, "svb")
    r = client.post("/api/admin/dispense/sales",
                    json={"container_size_L": 20, "quantity": 1})
    assert r.status_code == 200, f"setup sale failed: {r.get_json()}"
    sale_id = r.get_json()["sale"]["id"]
    _ensure_today_closed(client)
    r = client.post(f"/api/admin/dispense/sales/{sale_id}/void",
                    json={"reason": "test"})
    assert r.status_code == 409
    assert "closed" in (r.get_json().get("error") or "").lower()


def test_payment_default_targets_today(client):
    _enable_dispensing(); _ensure_rate(); _wipe_dispense_activity()
    _admin_login(client, "pdt")
    client.post("/api/admin/dispense/sales",
                json={"container_size_L": 20, "quantity": 1})
    r = client.post("/api/admin/dispense/payments",
                    json={"amount_kes": 50.0, "method": "cash"})
    assert r.status_code == 200, f"body={r.get_json()}"


def test_payment_targets_past_day_with_outstanding(client):
    _enable_dispensing(); _ensure_rate(); _wipe_dispense_activity()
    _admin_login(client, "ppt")
    past = _seed_past_sale(days_ago=2, price=50.0, qty=2)
    r = client.post("/api/admin/dispense/payments",
                    json={"amount_kes": 100.0, "method": "cash",
                          "sale_date": past.isoformat()})
    assert r.status_code == 200, f"body={r.get_json()}"
    win = client.get("/api/admin/dispense/window?days=7").get_json()
    past_iso = past.isoformat()
    past_stats = next((d for d in win["days"] if d["date_iso"] == past_iso), None)
    assert past_stats is not None
    assert past_stats["paid_kes"] == 100.0
    assert past_stats["outstanding_kes"] == 0.0


def test_payment_rejects_day_with_no_sales(client):
    _enable_dispensing(); _ensure_rate(); _wipe_dispense_activity()
    _admin_login(client, "pns")
    with app_module.app.app_context():
        empty_day = app_module._nairobi_today() - timedelta(days=5)
    r = client.post("/api/admin/dispense/payments",
                    json={"amount_kes": 50.0, "method": "cash",
                          "sale_date": empty_day.isoformat()})
    assert r.status_code == 400
    assert "no sales" in (r.get_json().get("error") or "").lower()


def test_payment_rejects_invalid_sale_date(client):
    _enable_dispensing(); _ensure_rate(); _wipe_dispense_activity()
    _admin_login(client, "pid")
    r = client.post("/api/admin/dispense/payments",
                    json={"amount_kes": 50.0, "method": "cash",
                          "sale_date": "not-a-date"})
    assert r.status_code == 400


def test_window_shows_paid_vs_cash_received(client):
    _enable_dispensing(); _ensure_rate(); _wipe_dispense_activity()
    _admin_login(client, "wsh")
    past = _seed_past_sale(days_ago=2, price=50.0, qty=2)
    client.post("/api/admin/dispense/payments",
                json={"amount_kes": 100.0, "method": "cash",
                      "sale_date": past.isoformat()})
    win = client.get("/api/admin/dispense/window?days=7").get_json()
    past_iso = past.isoformat()
    today_iso = app_module._nairobi_today().isoformat()

    past_day = next(d for d in win["days"] if d["date_iso"] == past_iso)
    today_day = next(d for d in win["days"] if d["date_iso"] == today_iso)

    assert past_day["paid_kes"] == 100.0
    assert past_day["outstanding_kes"] == 0.0
    assert past_day["cash_received_kes"] == 0.0

    assert today_day["cash_received_kes"] == 100.0
    assert today_day["paid_kes"] == 0.0


def test_credit_offset_on_closed_past_day_still_allowed(client):
    _enable_dispensing(); _ensure_rate(); _wipe_dispense_activity()
    _admin_login(client, "coc")
    past = _seed_past_sale(days_ago=3, price=50.0, qty=2)
    with app_module.app.app_context():
        db.session.add(DispenseMeterReading(
            tenant_id=1, reading_date=past,
            opening_reading_m3=0.0, closing_reading_m3=100.0,
            consumption_m3=100.0, is_closed=True, recorded_by="test",
        ))
        db.session.commit()
    r = client.post("/api/admin/dispense/payments",
                    json={"amount_kes": 100.0, "method": "cash",
                          "sale_date": past.isoformat()})
    assert r.status_code == 200, f"credit offset blocked: {r.get_json()}"

def test_bulk_import_blocked_when_target_day_closed(client):
    """Bulk import must respect day-close lock — the exact bug reported."""
    _enable_dispensing(); _ensure_rate(); _wipe_dispense_activity()
    _admin_login(client, "bic")
    past = _seed_past_sale(days_ago=2, price=50.0, qty=1)
    with app_module.app.app_context():
        db.session.add(DispenseMeterReading(
            tenant_id=1, reading_date=past,
            opening_reading_m3=0.0, closing_reading_m3=100.0,
            consumption_m3=100.0, is_closed=True, recorded_by="test",
        ))
        db.session.commit()
    csv_text = f"20,3,{past.isoformat()},import-test"
    r = client.post("/api/admin/dispense/sales/bulk_import",
                    json={"csv": csv_text, "dry_run": False})
    assert r.status_code == 400, f"body={r.get_json()}"
    body = r.get_json()
    assert body["ok"] is False
    assert any("closed" in (e.get("error") or "").lower() for e in body["errors"])


def test_bulk_import_dry_run_also_rejects_closed_day(client):
    """dry_run=true must validate the closed-day lock too (no bypass)."""
    _enable_dispensing(); _ensure_rate(); _wipe_dispense_activity()
    _admin_login(client, "bid")
    past = _seed_past_sale(days_ago=2, price=50.0, qty=1)
    with app_module.app.app_context():
        db.session.add(DispenseMeterReading(
            tenant_id=1, reading_date=past,
            opening_reading_m3=0.0, closing_reading_m3=100.0,
            consumption_m3=100.0, is_closed=True, recorded_by="test",
        ))
        db.session.commit()
    csv_text = f"20,3,{past.isoformat()},import-dry"
    r = client.post("/api/admin/dispense/sales/bulk_import",
                    json={"csv": csv_text, "dry_run": True})
    assert r.status_code == 400
    assert any("closed" in (e.get("error") or "").lower()
               for e in r.get_json()["errors"])

