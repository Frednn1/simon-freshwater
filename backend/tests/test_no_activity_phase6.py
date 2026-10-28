"""Phase 6 — status label rename: auto_closed days -> 'No Activity'.

Verifies the visible label change end-to-end (unit + integration + CSS).
"""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login
from models import DispenseMeterReading, TenantProfile, db
from datetime import timedelta


def _admin_login(client, tag):
    name = f"admin_na6_{tag}_{uuid.uuid4().hex[:6]}"
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
        db.session.commit()


def test_status_returns_no_activity_for_auto_closed():
    """Unit: _dispense_status returns 'No Activity' for auto_closed days."""
    stats = {"has_reading": True, "auto_closed": True,
             "variance_l": 0, "outstanding_kes": 0}
    assert app_module._dispense_status(stats) == "No Activity"


def test_window_returns_no_activity_for_auto_closed(client):
    """Integration: /window emits 'No Activity' for auto_closed rows."""
    _enable_dispensing(); _wipe()
    _admin_login(client, "wi")
    past = app_module._nairobi_today() - timedelta(days=2)
    with app_module.app.app_context():
        db.session.add(DispenseMeterReading(
            tenant_id=1, reading_date=past,
            opening_reading_m3=100.0, closing_reading_m3=100.0,
            consumption_m3=0.0, is_closed=True, auto_closed=True,
            recorded_by="test"))
        db.session.commit()
    r = client.get("/api/admin/dispense/window?days=7")
    assert r.status_code == 200
    d = r.get_json()
    past_iso = past.isoformat()
    past_row = next((x for x in d["days"] if x["date_iso"] == past_iso), None)
    assert past_row is not None, f"past day {past_iso} missing from window"
    assert past_row["auto_closed"] is True
    assert past_row["status"] == "No Activity", (
        f"expected 'No Activity', got '{past_row['status']}'")


def test_frontend_has_status_no_activity_css():
    """CSS: .status-no-activity class must exist for the pill to render."""
    _FRONTEND = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "frontend"))
    html = open(os.path.join(_FRONTEND, "dispense.html"), encoding="utf-8").read()
    assert ".status-no-activity" in html
    # Must coexist with the existing no-reading class (both are grey pills)
    assert ".status-no-reading" in html
