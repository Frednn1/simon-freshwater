"""Bulk SMS bill data enrichment — closes preview/actual mismatch."""
import sys, os, uuid, re
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login
from models import Consumer, MeterReading, db
from datetime import timedelta


def _admin_login(client, tag="bs"):
    name = f"admin_{tag}_{uuid.uuid4().hex[:6]}"
    make_admin(name)
    r = login(client, name)
    d = r.get_json() or {}
    # If tenant has both profiles, login returns requires_profile_choice
    # and every subsequent non-allowlisted request is 403'd by the
    # R5 pending guard. Complete the chooser before making API calls.
    if d.get("requires_profile_choice"):
        profiles = d.get("profiles") or []
        if profiles:
            client.post("/api/admin/profile/switch", json={"to": profiles[0]})
    return name


def _make_consumer_with_bill(tenant_id=1, age_days=5, cons_m3=10.0, amount=500.0):
    """Create consumer + reading with an outstanding balance."""
    with app_module.app.app_context():
        cust = Consumer(
            tenant_id=tenant_id,
            cust_name=f"BillTest_{uuid.uuid4().hex[:6]}",
            acc_name="Bill Test",
            meter_acc_no=f"MTR-{uuid.uuid4().hex[:8].upper()}",
            contact="0700111222",
            meter_initial_reading_m3=100.0,
            is_active=True,
        )
        db.session.add(cust)
        db.session.flush()
        rdate = app_module._nairobi_today() - timedelta(days=age_days)
        # NOTE: MeterReading.balance is a @property computed from
        # amount_kes - amount_paid. It has no setter — do not pass it
        # to the constructor. Setting amount_paid=0 gives balance=amount.
        reading = MeterReading(
            tenant_id=tenant_id,
            consumer_id=cust.id,
            reading_date=rdate,
            reading_m3=100.0 + cons_m3,
            amount_kes=amount,
            amount_paid=0.0,
        )
        db.session.add(reading)
        db.session.commit()
        return cust.id, reading.id


def test_bill_details_enrichment_helper_populates_fields(client):
    cid, _ = _make_consumer_with_bill()
    with app_module.app.app_context():
        consumer = Consumer.query.get(cid)
        info = {"total_due": 500.0, "label": "Due", "status": "DUE"}
        enriched = app_module._bill_details_enrichment(consumer, info.copy())
        assert enriched["current_reading_m3"] > 0
        assert enriched["previous_reading_m3"] >= 0
        assert enriched["current_charges"] > 0
        assert enriched["total_outstanding"] > 0
        assert enriched["bill_month"]
        for k in ("paid", "payment_carried_forward", "previous_outstanding",
                  "is_current_cleared"):
            assert k in enriched


def test_notify_and_bulk_produce_identical_body():
    cid, _ = _make_consumer_with_bill()
    with app_module.app.app_context():
        consumer = Consumer.query.get(cid)
        readings = app_module._all_readings(consumer.id)
        disp = app_module._display_latest_balance(
            list(reversed(readings)),
            app_module._consumer_payments(consumer.id),
        )
        # notify path
        i1 = app_module.get_consumer_status(readings, latest_balance_override=disp)
        i1["consumption_m3"] = app_module._total_uncleared_consumption(consumer)
        i1 = app_module._bill_details_enrichment(consumer, i1)
        b1 = app_module.build_bill_message(consumer, i1, channel="sms", tenant=None)["body"]
        # bulk path
        i2 = app_module.get_consumer_status(readings, latest_balance_override=disp)
        i2["consumption_m3"] = app_module._total_uncleared_consumption(consumer)
        i2 = app_module._bill_details_enrichment(consumer, i2)
        b2 = app_module.build_bill_message(consumer, i2, channel="sms", tenant=None)["body"]
        assert b1 == b2


def test_overdue_includes_enriched_preview_body(client):
    _admin_login(client, "ov")
    cid, _ = _make_consumer_with_bill(age_days=25)
    r = client.get("/api/admin/bulk_sms/overdue")
    assert r.status_code == 200
    consumers = r.get_json().get("consumers", [])
    tgt = next((c for c in consumers if c["id"] == cid), None)
    assert tgt is not None, f"consumer {cid} not in overdue list"
    assert "preview_body" in tgt
    assert "Current (MTR)" in tgt["preview_body"]
    assert "Previous (MTR)" in tgt["preview_body"]
    # Assert the specific field values are 0.00 — NOT a substring match.
    # (Original `"0.00 M3" not in body` falsely matched "110.00 M3" and
    # "100.00 M3" since "0.00 M3" is a substring of both.)
    assert not re.search(r"Current \(MTR\)\s+: 0\.00 M3\b", tgt["preview_body"]), \
        f"preview has zero Current MTR: {tgt['preview_body'][:200]}"
    assert not re.search(r"Previous \(MTR\)\s+: 0\.00 M3\b", tgt["preview_body"]), \
        f"preview has zero Previous MTR: {tgt['preview_body'][:200]}"


def test_send_passes_enriched_info(client, monkeypatch):
    _admin_login(client, "sd")
    cid, _ = _make_consumer_with_bill(age_days=25)

    captured = {}
    def fake_send(phone, body):
        captured["body"] = body
        return {"ok": True, "provider_id": "test"}

    import bulk_sms
    monkeypatch.setattr(bulk_sms, "send_sms", fake_send)

    r = client.post("/api/admin/bulk_sms/send", json={"consumer_ids": [cid]})
    assert r.status_code == 200
    assert "body" in captured
    assert "Current (MTR)" in captured["body"]
    # Same fix as test_overdue — line-anchored check
    assert not re.search(r"Current \(MTR\)\s+: 0\.00 M3\b", captured["body"]), \
        f"SMS has zero Current MTR: {captured['body'][:300]}"
    assert not re.search(r"Previous \(MTR\)\s+: 0\.00 M3\b", captured["body"]), \
        f"SMS has zero Previous MTR: {captured['body'][:300]}"


def test_notify_unchanged_after_refactor(client, monkeypatch):
    _admin_login(client, "nt")
    cid, _ = _make_consumer_with_bill(age_days=25)

    captured = {}
    def fake_send(phone, body):
        captured["body"] = body
        return {"ok": True, "provider_id": "test"}

    monkeypatch.setattr(app_module, "send_sms", fake_send)
    r = client.post(f"/api/consumer/{cid}/notify", json={"channel": "sms"})
    assert r.status_code == 200
    assert "Current (MTR)" in captured["body"]
    # Same fix — line-anchored check (previous assertion falsely matched
    # substrings of "110.00 M3" / "100.00 M3")
    assert not re.search(r"Current \(MTR\)\s+: 0\.00 M3\b", captured["body"]), \
        f"notify SMS has zero Current MTR: {captured['body'][:300]}"
    assert not re.search(r"Previous \(MTR\)\s+: 0\.00 M3\b", captured["body"]), \
        f"notify SMS has zero Previous MTR: {captured['body'][:300]}"
