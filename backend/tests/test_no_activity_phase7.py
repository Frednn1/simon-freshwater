"""Phase 7 — semantic no-activity labeling.

Fix: closed days with 0 sales AND 0 consumption display "No Activity"
regardless of the auto_closed flag (covers user-closed days whose flag
was cleared by Phase 5b promotion, or were closed manually with no sales).

Regression coverage: days with sales or consumption keep their real status.
"""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login
from models import DispenseMeterReading, DispenseSale, TenantProfile, db
from datetime import timedelta


def _admin_login(client, tag):
    name = f"admin_na7_{tag}_{uuid.uuid4().hex[:6]}"
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
        DispenseSale.query.filter_by(tenant_id=tenant_id).delete()
        db.session.commit()


# ══════════════════════════════════════════════════════════════════════
# Unit tests — _dispense_status branch logic
# ══════════════════════════════════════════════════════════════════════

def test_semantic_no_activity_user_closed_zero():
    """Closed + 0 sales + 0 consumption + auto_closed=False -> No Activity."""
    stats = {
        "has_reading": True, "is_closed": True, "auto_closed": False,
        "sales_kes": 0, "consumption_l": 0,
        "variance_l": 0, "outstanding_kes": 0,
    }
    assert app_module._dispense_status(stats) == "No Activity"


def test_auto_closed_still_returns_no_activity():
    """Regression: auto_closed=True still returns No Activity (Phase 6 preserved)."""
    stats = {
        "has_reading": True, "auto_closed": True,
        "variance_l": 0, "outstanding_kes": 0,
    }
    assert app_module._dispense_status(stats) == "No Activity"


def test_closed_with_sales_stays_cleared():
    """Regression: closed + sales > 0 + variance=0 + outstanding=0 -> Cleared."""
    stats = {
        "has_reading": True, "is_closed": True, "auto_closed": False,
        "sales_kes": 3950, "consumption_l": 23000,
        "variance_l": 0, "outstanding_kes": 0,
    }
    assert app_module._dispense_status(stats) == "Cleared"


def test_closed_with_consumption_no_sales_is_due():
    """Regression: closed + 0 sales + consumption > 0 -> Due (variance from water)."""
    stats = {
        "has_reading": True, "is_closed": True, "auto_closed": False,
        "sales_kes": 0, "consumption_l": 2000,
        "variance_l": 2000, "outstanding_kes": 0,
    }
    assert app_module._dispense_status(stats) == "Due"


# ══════════════════════════════════════════════════════════════════════
# Integration test — /window shows No Activity for user-closed zero day
# ══════════════════════════════════════════════════════════════════════

def test_window_shows_no_activity_for_user_closed_zero_day(client):
    """End-to-end: seed user-closed zero-activity day, /window reports No Activity."""
    _enable_dispensing(); _wipe()
    _admin_login(client, "int")
    past = app_module._nairobi_today() - timedelta(days=2)
    with app_module.app.app_context():
        db.session.add(DispenseMeterReading(
            tenant_id=1, reading_date=past,
            opening_reading_m3=90.0, closing_reading_m3=90.0,
            consumption_m3=0.0, is_closed=True, auto_closed=False,
            recorded_by="test"))
        db.session.commit()
    r = client.get("/api/admin/dispense/window?days=7")
    assert r.status_code == 200
    d = r.get_json()
    past_iso = past.isoformat()
    past_row = next((x for x in d["days"] if x["date_iso"] == past_iso), None)
    assert past_row is not None
    assert past_row["auto_closed"] is False
    assert past_row["status"] == "No Activity", (
        f"expected 'No Activity', got '{past_row['status']}'")
