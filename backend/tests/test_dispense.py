"""Step 2+3: dispensing reading — opening-only + day-close flow."""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login
from models import DispenseMeterReading, TenantProfile, db
from datetime import timedelta


def _admin_login(client, tag):
    name = f"admin_d_{tag}_{uuid.uuid4().hex[:6]}"
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


def _wipe_readings(tenant_id=1):
    with app_module.app.app_context():
        DispenseMeterReading.query.filter_by(tenant_id=tenant_id).delete()
        db.session.commit()


def test_bootstrap_opening_only_creates_open_row(client):
    _enable_dispensing()
    _wipe_readings()
    _admin_login(client, "boot")
    r = client.post("/api/admin/dispense/reading",
                    json={"opening_reading_m3": 100.0})
    assert r.status_code == 200, f"body={r.get_json()}"
    d = r.get_json()
    assert d["action"] == "opened"
    assert d["is_closed"] is False
    assert d["closing_reading_m3"] is None
    assert d["opening_reading_m3"] == 100.0


def test_close_open_row_succeeds(client):
    _enable_dispensing()
    _wipe_readings()
    _admin_login(client, "close")
    client.post("/api/admin/dispense/reading",
                json={"opening_reading_m3": 100.0})
    r = client.post("/api/admin/dispense/reading",
                    json={"closing_reading_m3": 200.0})
    assert r.status_code == 200, f"body={r.get_json()}"
    d = r.get_json()
    assert d["action"] == "closed"
    assert d["is_closed"] is True
    assert d["closing_reading_m3"] == 200.0


def test_close_open_row_requires_closing(client):
    _enable_dispensing()
    _wipe_readings()
    _admin_login(client, "needc")
    client.post("/api/admin/dispense/reading",
                json={"opening_reading_m3": 100.0})
    r = client.post("/api/admin/dispense/reading", json={})
    assert r.status_code == 400
    assert "closing" in (r.get_json().get("error") or "").lower()


def test_correction_of_closed_row_requires_force_and_reason(client):
    _enable_dispensing()
    _wipe_readings()
    _admin_login(client, "corr")
    client.post("/api/admin/dispense/reading",
                json={"opening_reading_m3": 100.0})
    client.post("/api/admin/dispense/reading",
                json={"closing_reading_m3": 200.0})
    r = client.post("/api/admin/dispense/reading",
                    json={"closing_reading_m3": 201.0})
    assert r.status_code == 409
    r = client.post("/api/admin/dispense/reading",
                    json={"closing_reading_m3": 201.0, "force": True})
    assert r.status_code == 400
    r = client.post("/api/admin/dispense/reading",
                    json={"closing_reading_m3": 201.0, "force": True,
                          "reason": "typo"})
    assert r.status_code == 200
    assert r.get_json()["action"] == "corrected"


def test_get_today_exposes_is_closed(client):
    _enable_dispensing()
    _wipe_readings()
    _admin_login(client, "gett")
    client.post("/api/admin/dispense/reading",
                json={"opening_reading_m3": 100.0})
    r = client.get("/api/admin/dispense/today")
    assert r.status_code == 200
    d = r.get_json()
    assert d["has_reading"] is True
    assert d["is_closed"] is False
    assert d["closing_reading_m3"] is None
    assert d["opening_reading_m3"] == 100.0


def test_next_day_rejected_when_yesterday_open(client):
    _enable_dispensing()
    _wipe_readings()
    _admin_login(client, "guard")
    with app_module.app.app_context():
        yesterday = app_module._nairobi_today() - timedelta(days=1)
        db.session.add(DispenseMeterReading(
            tenant_id=1,
            reading_date=yesterday,
            opening_reading_m3=50.0,
            closing_reading_m3=50.0,
            consumption_m3=0.0,
            is_closed=False,
            recorded_by="test",
        ))
        db.session.commit()
    r = client.post("/api/admin/dispense/reading",
                    json={"opening_reading_m3": 100.0, "closing_reading_m3": 150.0})
    assert r.status_code == 400
    err = (r.get_json().get("error") or "").lower()
    assert "yesterday" in err and "open" in err, f"got: {err}"
