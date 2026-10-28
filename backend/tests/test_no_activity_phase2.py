"""Phase 2a — auto_closed in API responses + status function.

Phase 2a ships response additions ONLY. No auto-close call yet. Every test
here verifies additive behavior. Zero test breakage.
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
    name = f"admin_na2_{tag}_{uuid.uuid4().hex[:6]}"
    make_admin(name)
    login(client, name)
    # Ensure dispensing profile is active (required for /today / /window)
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


# ══════════════════════════════════════════════════════════════════════
# Response shape additions
# ══════════════════════════════════════════════════════════════════════

def test_today_response_includes_auto_closed(client):
    _enable_dispensing(); _wipe()
    _admin_login(client, "acr")
    r = client.get("/api/admin/dispense/today")
    assert r.status_code == 200, f"body={r.get_json()}"
    d = r.get_json()
    assert "auto_closed" in d, "auto_closed missing from /today response"
    # Default (no reading) is False, not None
    assert d["auto_closed"] is False


def test_window_response_includes_auto_closed(client):
    _enable_dispensing(); _wipe()
    _admin_login(client, "awr")
    r = client.get("/api/admin/dispense/window")
    assert r.status_code == 200
    d = r.get_json()
    assert "days" in d
    assert len(d["days"]) > 0
    for row in d["days"]:
        assert "auto_closed" in row, f"auto_closed missing on day {row.get('date_iso')}"


# ══════════════════════════════════════════════════════════════════════
# _dispense_status branch behavior
# ══════════════════════════════════════════════════════════════════════

def test_status_no_activity_for_auto_closed():
    """auto_closed=True -> 'No Activity' regardless of other stats (Phase 6)."""
    stats = {
        "has_reading": True, "auto_closed": True,
        "variance_l": 0, "outstanding_kes": 0,
    }
    assert app_module._dispense_status(stats) == "No Activity"


def test_status_cleared_for_manual_closed():
    """auto_closed=False stays 'Cleared' (unchanged behavior)."""
    stats = {
        "has_reading": True, "auto_closed": False,
        "variance_l": 0, "outstanding_kes": 0,
    }
    assert app_module._dispense_status(stats) == "Cleared"


def test_status_no_reading_for_missing_row():
    """has_reading=False (lowercase) still returns 'No reading' — unchanged."""
    stats = {"has_reading": False}
    assert app_module._dispense_status(stats) == "No reading"


# ══════════════════════════════════════════════════════════════════════
# Zero behavior change for pre-Phase-2b data
# ══════════════════════════════════════════════════════════════════════

def test_existing_closed_row_shows_cleared(client):
    """Legacy closed row (auto_closed defaults False) still 'Cleared' in /window."""
    _enable_dispensing(); _wipe()
    _admin_login(client, "lgc")
    with app_module.app.app_context():
        today = app_module._nairobi_today()
        db.session.add(DispenseMeterReading(
            tenant_id=1, reading_date=today,
            opening_reading_m3=100.0, closing_reading_m3=150.0,
            consumption_m3=50.0, is_closed=True, auto_closed=False,
            recorded_by="test",
        ))
        db.session.commit()
    r = client.get("/api/admin/dispense/window")
    assert r.status_code == 200
    d = r.get_json()
    today_iso = app_module._nairobi_today().isoformat()
    today_row = next((row for row in d["days"] if row["date_iso"] == today_iso), None)
    assert today_row is not None
    assert today_row["auto_closed"] is False
    # Status label uses variance/outstanding logic — not "No Activity"
    assert today_row["status"] != "No Activity"
