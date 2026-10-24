"""Phase A — cascade downstream when a day's closing changes.

Root scenario from production:
  Day A: open only (no closing) — then retro-closed at 370
  Day B: created before Day A was closed -> opening=stale 330
  Cascade fixes Day B's opening to 370 (Day A closing) on retro-close.
"""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login
from models import (
    DispenseMeterReading, DispenseRate, DispenseSale, DispensePayment,
    TenantProfile, db,
)
from datetime import timedelta


def _admin_login(client, tag):
    name = f"admin_casc_{tag}_{uuid.uuid4().hex[:6]}"
    make_admin(name)
    login(client, name)
    client.post("/api/admin/profile/switch", json={"to": "dispensing"})
    return name


def _enable(tenant_id=1):
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


def _seed_row(tenant_id=1, days_ago=0, opening=0.0, closing=0.0, is_closed=True):
    with app_module.app.app_context():
        d = app_module._nairobi_today() - timedelta(days=days_ago)
        db.session.add(DispenseMeterReading(
            tenant_id=tenant_id, reading_date=d,
            opening_reading_m3=opening, closing_reading_m3=closing,
            consumption_m3=(closing - opening) if is_closed else 0.0,
            is_closed=is_closed, recorded_by="test"))
        db.session.commit()
        return d


def _get_row(tenant_id, target_date):
    with app_module.app.app_context():
        return DispenseMeterReading.query.filter_by(
            tenant_id=tenant_id, reading_date=target_date).first()


def test_cascade_updates_downstream_closed_row(client):
    """EXACT production scenario: retro-close day N, day N+1 was closed
    earlier with stale opening. Cascade must fix N+1."""
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "upd")
    today = app_module._nairobi_today()

    # Seed: Day -3 closed at 330, Day -1 closed with STALE opening=330 (should be 370)
    _seed_row(days_ago=3, opening=300, closing=330, is_closed=True)
    _seed_row(days_ago=1, opening=330, closing=380, is_closed=True)  # stale

    # Retro-close Day -2 (was open before our repair — but here we seed it open)
    d2 = _seed_row(days_ago=2, opening=330, closing=330, is_closed=False)

    # Retro-close Day -2 at 370 (via API force+reason)
    r = client.post("/api/admin/dispense/reading",
                    json={"reading_date": d2.isoformat(),
                          "closing_reading_m3": 370.0,
                          "force": True,
                          "reason": "cascade test"})
    assert r.status_code == 200, f"body={r.get_json()}"
    body = r.get_json()

    # Day -2 should be closed at 370
    row_d2 = _get_row(1, d2)
    assert row_d2.is_closed is True
    assert float(row_d2.closing_reading_m3) == 370.0

    # Day -1 (downstream closed) should have opening=370 now
    d1 = today - timedelta(days=1)
    row_d1 = _get_row(1, d1)
    assert float(row_d1.opening_reading_m3) == 370.0, (
        f"CASCADE FAILED: day-1 opening={row_d1.opening_reading_m3}, expected 370"
    )
    # Consumption recomputed: closing 380 - opening 370 = 10 m³
    assert float(row_d1.consumption_m3) == 10.0

    # Response should report the change
    assert "cascade_affected" in body
    assert len(body["cascade_affected"]) == 1
    affected = body["cascade_affected"][0]
    assert affected["date"] == d1.isoformat()
    assert affected["old_opening"] == 330.0
    assert affected["new_opening"] == 370.0
    assert affected["old_consumption_l"] == 50000
    assert affected["new_consumption_l"] == 10000


def test_cascade_noop_when_chain_already_consistent(client):
    """If downstream opening already matches, no changes."""
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "noop")
    # Seed consistent chain
    _seed_row(days_ago=3, opening=300, closing=330, is_closed=True)
    _seed_row(days_ago=2, opening=330, closing=370, is_closed=True)
    _seed_row(days_ago=1, opening=370, closing=380, is_closed=True)

    # Correction on day -3 with the SAME closing — should cascade-noop
    d3 = app_module._nairobi_today() - timedelta(days=3)
    r = client.post("/api/admin/dispense/reading",
                    json={"reading_date": d3.isoformat(),
                          "closing_reading_m3": 330.0,
                          "force": True, "reason": "no-op test"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["cascade_affected"] == []


def test_cascade_stops_at_open_row(client):
    """Cascade walks closed rows then stops at first open row (it has no
    real closing, so downstream derivation skips it anyway)."""
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "stop")
    today = app_module._nairobi_today()
    _seed_row(days_ago=4, opening=300, closing=330, is_closed=True)
    _seed_row(days_ago=3, opening=330, closing=330, is_closed=False)  # open
    _seed_row(days_ago=2, opening=330, closing=370, is_closed=True)
    _seed_row(days_ago=1, opening=370, closing=380, is_closed=True)

    # Correction on day -4 to new closing=340
    d4 = today - timedelta(days=4)
    r = client.post("/api/admin/dispense/reading",
                    json={"reading_date": d4.isoformat(),
                          "closing_reading_m3": 340.0,
                          "force": True, "reason": "cascade stop test"})
    assert r.status_code == 200

    # Day -3 (open) opening should update to 340
    d3 = _get_row(1, today - timedelta(days=3))
    assert float(d3.opening_reading_m3) == 340.0
    # Day -2 (closed) should NOT change — cascade stopped at open row -3
    d2 = _get_row(1, today - timedelta(days=2))
    assert float(d2.opening_reading_m3) == 330.0  # unchanged
