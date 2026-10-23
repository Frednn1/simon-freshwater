"""Q1 — M-Pesa default=today + payment reallocate endpoint."""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login
from models import (
    DispenseMeterReading, DispenseSale, DispensePayment,
    DispenseRate, TenantProfile, Tenant, db,
)
from datetime import timedelta


def _admin_login(client, tag):
    name = f"admin_rl_{tag}_{uuid.uuid4().hex[:6]}"
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


def _seed_past_sale(days_ago=2, price=50.0, qty=2, tenant_id=1):
    with app_module.app.app_context():
        d = app_module._nairobi_today() - timedelta(days=days_ago)
        db.session.add(DispenseSale(
            tenant_id=tenant_id, sale_date=d,
            container_size_L=20, quantity=qty,
            price_per_unit_kes=price, total_kes=price * qty,
            recorded_by="test"))
        db.session.commit()
        return d


def test_mpesa_callback_sets_sale_date_to_today(client, monkeypatch):
    """Q1: auto-recorded M-Pesa payment must default sale_date=today."""
    _enable_dispensing(); _ensure_rate(); _wipe()
    monkeypatch.setattr(app_module, "_mpesa_ip_ok", lambda: True)
    with app_module.app.app_context():
        t = Tenant.query.get(1)
        if not t:
            t = Tenant(id=1, slug="test-t1", org_name="Test T1", is_active=True)
            db.session.add(t)
        t.dispensing_paybill = "999999"
        db.session.commit()
    trans_id = "TEST" + uuid.uuid4().hex[:10].upper()
    r = client.post("/api/pay/c2b/confirmation", json={
        "TransID": trans_id,
        "TransAmount": "100",
        "BusinessShortCode": "999999",
        "BillRefNumber": "",
        "MSISDN": "254700000000",
    })
    assert r.status_code == 200, f"body={r.get_json()}"
    with app_module.app.app_context():
        p = DispensePayment.query.filter_by(reference=trans_id).first()
        assert p is not None
        assert p.sale_date == app_module._nairobi_today(), \
            f"expected today, got {p.sale_date}"


def test_reallocate_to_past_day_succeeds(client):
    """Q1: admin can move an M-Pesa payment to a past day with sales."""
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "rtp")
    past = _seed_past_sale(days_ago=2, price=50.0, qty=2)
    # Create a payment allocated to today
    with app_module.app.app_context():
        today = app_module._nairobi_today()
        p = DispensePayment(
            tenant_id=1, payment_date=today, sale_date=today,
            amount_kes=100.0, method="mpesa_c2b",
            reference=f"REC-{uuid.uuid4().hex[:8]}", recorded_by="test")
        db.session.add(p)
        db.session.commit()
        pid = p.id
    r = client.post(f"/api/admin/dispense/payments/{pid}/reallocate",
                    json={"sale_date": past.isoformat()})
    assert r.status_code == 200, f"body={r.get_json()}"
    d = r.get_json()
    assert d["new_sale_date"] == past.isoformat()
    with app_module.app.app_context():
        p2 = DispensePayment.query.get(pid)
        assert p2.sale_date == past


def test_reallocate_to_day_without_sales_rejected(client):
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "rns")
    with app_module.app.app_context():
        today = app_module._nairobi_today()
        p = DispensePayment(
            tenant_id=1, payment_date=today, sale_date=today,
            amount_kes=100.0, method="mpesa_c2b",
            reference=f"REC-{uuid.uuid4().hex[:8]}", recorded_by="test")
        db.session.add(p)
        db.session.commit()
        pid = p.id
    empty_day = (app_module._nairobi_today() - timedelta(days=5))
    r = client.post(f"/api/admin/dispense/payments/{pid}/reallocate",
                    json={"sale_date": empty_day.isoformat()})
    assert r.status_code == 400
    assert "no sales" in (r.get_json().get("error") or "").lower()


def test_reallocate_voided_payment_rejected(client):
    _enable_dispensing(); _ensure_rate(); _wipe()
    _admin_login(client, "rvd")
    past = _seed_past_sale(days_ago=2, price=50.0, qty=2)
    with app_module.app.app_context():
        today = app_module._nairobi_today()
        p = DispensePayment(
            tenant_id=1, payment_date=today, sale_date=today,
            amount_kes=100.0, method="mpesa_c2b",
            reference=f"REC-{uuid.uuid4().hex[:8]}", recorded_by="test",
            voided=True)
        db.session.add(p)
        db.session.commit()
        pid = p.id
    r = client.post(f"/api/admin/dispense/payments/{pid}/reallocate",
                    json={"sale_date": past.isoformat()})
    assert r.status_code == 400
    assert "voided" in (r.get_json().get("error") or "").lower()
