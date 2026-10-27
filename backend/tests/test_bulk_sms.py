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
    def fake_send(phone, body, tenant_slug=None, tenant_row=None):
        # Phase 3: bulk_sms.py passes tenant_slug
        # Phase 4.5: bulk_sms.py also passes tenant_row — accept both
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

# ══════════════════════════════════════════════════════════════════════
# Bulk SMS — WhatsApp channel support
# ══════════════════════════════════════════════════════════════════════

def test_bulk_send_whatsapp_channel(client, monkeypatch):
    """POST with channel=whatsapp → injected whatsapp_sender called, send_sms not called."""
    _admin_login(client, "wa")
    cid, _ = _make_consumer_with_bill(age_days=25)

    monkeypatch.setenv("WA_ENABLED", "true")

    sms_calls = []
    wa_calls = []
    def fake_sms(phone, body):
        sms_calls.append((phone, body)); return {"ok": True, "provider_id": "sms"}
    def fake_wa_sender(consumer, info, tenant):
        wa_calls.append((consumer.id, info.get("total_due")))
        return {"ok": True, "provider_id": "wa-doc"}

    import bulk_sms, app as app_module
    monkeypatch.setattr(bulk_sms, "send_sms", fake_sms)
    monkeypatch.setattr(app_module, "_bulk_whatsapp_send_bill", fake_wa_sender)

    r = client.post("/api/admin/bulk_sms/send",
                    json={"consumer_ids": [cid], "channel": "whatsapp"})
    assert r.status_code == 200, f"body={r.get_json()}"
    assert len(wa_calls) == 1, f"expected 1 whatsapp_sender call, got {len(wa_calls)}"
    assert wa_calls[0][0] == cid
    assert len(sms_calls) == 0, f"expected 0 send_sms calls, got {len(sms_calls)}"
    results = r.get_json().get("results", [])
    assert results and results[0].get("channel") == "whatsapp"


def test_bulk_send_invalid_channel_rejected(client):
    _admin_login(client, "bad")
    cid, _ = _make_consumer_with_bill(age_days=25)
    r = client.post("/api/admin/bulk_sms/send",
                    json={"consumer_ids": [cid], "channel": "telegram"})
    assert r.status_code == 400
    err = (r.get_json().get("error") or "").lower()
    assert "channel" in err


def test_bulk_send_whatsapp_disabled_rejected(client, monkeypatch):
    _admin_login(client, "wd")
    cid, _ = _make_consumer_with_bill(age_days=25)
    monkeypatch.setenv("WA_ENABLED", "false")
    r = client.post("/api/admin/bulk_sms/send",
                    json={"consumer_ids": [cid], "channel": "whatsapp"})
    assert r.status_code == 403
    assert "disabled" in (r.get_json().get("error") or "").lower()


def test_bulk_whatsapp_writes_whatsapp_notification_log(client, monkeypatch):
    _admin_login(client, "wl")
    cid, _ = _make_consumer_with_bill(age_days=25)
    monkeypatch.setenv("WA_ENABLED", "true")

    def fake_wa_sender(consumer, info, tenant):
        return {"ok": True, "provider_id": "wa-test"}

    import app as app_module
    monkeypatch.setattr(app_module, "_bulk_whatsapp_send_bill", fake_wa_sender)

    r = client.post("/api/admin/bulk_sms/send",
                    json={"consumer_ids": [cid], "channel": "whatsapp"})
    assert r.status_code == 200

    from models import NotificationLog
    with app_module.app.app_context():
        log = (NotificationLog.query
               .filter_by(consumer_id=cid)
               .order_by(NotificationLog.id.desc())
               .first())
        assert log is not None
        assert log.channel == "whatsapp"


def test_overdue_returns_whatsapp_available_flag(client, monkeypatch):
    _admin_login(client, "wav")
    # WA_PHONE_NUMBER_ID / WA_ACCESS_TOKEN are captured as module-level
    # constants at import time (not read from os.environ on each call),
    # so patching env vars alone is insufficient — patch the constants.
    # WA_ENABLED is read fresh on each call, so env monkeypatch works.
    import notifications
    monkeypatch.setenv("WA_ENABLED", "true")
    monkeypatch.setattr(notifications, "WA_PHONE_NUMBER_ID", "test_id")
    monkeypatch.setattr(notifications, "WA_ACCESS_TOKEN", "test_token")
    r = client.get("/api/admin/bulk_sms/overdue")
    assert r.status_code == 200
    d = r.get_json()
    assert "whatsapp_available" in d
    assert d["whatsapp_available"] is True

    # Also verify with WA disabled
    monkeypatch.setenv("WA_ENABLED", "false")
    r2 = client.get("/api/admin/bulk_sms/overdue")
    d2 = r2.get_json()
    assert d2["whatsapp_available"] is False


def test_overdue_returns_whatsapp_preview_body(client):
    _admin_login(client, "wpb")
    cid, _ = _make_consumer_with_bill(age_days=25)
    r = client.get("/api/admin/bulk_sms/overdue")
    assert r.status_code == 200
    d = r.get_json()
    tgt = next((c for c in d.get("consumers", []) if c["id"] == cid), None)
    assert tgt is not None
    assert "preview_body_wa" in tgt
    # WhatsApp body contains name and paybill info
    wa_body = tgt["preview_body_wa"]
    assert "Dear" in wa_body
    assert tgt["meter_acc_no"] in wa_body

# ══════════════════════════════════════════════════════════════════════
# Bulk WhatsApp — document template flow (uses _bulk_whatsapp_send_bill)
# ══════════════════════════════════════════════════════════════════════

def test_bulk_whatsapp_uses_document_template(client, monkeypatch):
    """The injected whatsapp_sender must call send_whatsapp_document with
    body_params=[cust_name, meter_acc_no] — matching the WA_TEMPLATE_NAME
    template that has 2 body params + document header."""
    _admin_login(client, "wtpl")
    cid, _ = _make_consumer_with_bill(age_days=25)
    monkeypatch.setenv("WA_ENABLED", "true")

    captured = {}
    def fake_doc_sender(to_phone, pdf_public_url, filename,
                        body_params, template_name=None, language=None,
                        tenant_slug=None, template_lang_override=None):
        # Phase 3: _bulk_whatsapp_send_bill now passes tenant_slug — accept
        captured["to_phone"] = to_phone
        captured["filename"] = filename
        captured["body_params"] = body_params
        return {"ok": True, "provider_id": "test"}

    # Stub PDF gen + archive config so this test doesn't depend on
    # template files or R2 credentials — we're testing the WhatsApp
    # document call shape, not PDF rendering or archiving.
    import io
    def fake_pdf(snapshot):
        return io.BytesIO(b"%PDF-1.4 test")

    import app as app_module
    import archive
    monkeypatch.setattr(app_module, "send_whatsapp_document", fake_doc_sender)
    monkeypatch.setattr(app_module, "generate_water_bill_pdf", fake_pdf)
    monkeypatch.setattr(archive, "is_configured", lambda: False)

    r = client.post("/api/admin/bulk_sms/send",
                    json={"consumer_ids": [cid], "channel": "whatsapp"})
    assert r.status_code == 200, f"body={r.get_json()}"

    resp = r.get_json() or {}
    assert "to_phone" in captured, (
        f"send_whatsapp_document not called. "
        f"results={resp.get('results')}, "
        f"error={resp.get('error')}"
    )
    assert len(captured["body_params"]) == 2, \
        f"expected 2 body params (name, meter), got {captured['body_params']}"
    assert captured["filename"].endswith(".pdf"), \
        f"filename should be a PDF: {captured['filename']}"


def test_bulk_whatsapp_sender_missing_fails_cleanly(client):
    """If endpoint injects None for whatsapp_sender (should not happen in prod),
    batch must fail with a clear error rather than silently falling back."""
    import bulk_sms as bs_mod
    import app as app_module
    from models import Consumer
    cid, _ = _make_consumer_with_bill()
    with app_module.app.app_context():
        consumer = Consumer.query.get(cid)
        info = {"total_due": 100, "label": "Due", "status": "DUE",
                "previous_reading_m3": 100}
        items = [{"consumer": consumer, "info": info}]
        # send_bulk_sms writes a NotificationLog row → needs app context
        result = bs_mod.send_bulk_sms(items, channel="whatsapp", whatsapp_sender=None)
    assert result["failed"] == 1
    assert result["sent"] == 0
    assert "WhatsApp sender not configured" in result["results"][0]["error"]

