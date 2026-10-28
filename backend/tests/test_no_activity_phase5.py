"""Phase 5a — cascade preserves auto_closed invariant."""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login
from models import (
    DispenseMeterReading, DispenseRate,
    TenantProfile, db,
)
from datetime import timedelta


def _admin_login(client, tag):
    name = f"admin_na5a_{tag}_{uuid.uuid4().hex[:6]}"
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
        db.session.commit()


def _seed(tenant_id=1, days_ago=0, opening=0.0, closing=0.0,
          is_closed=True, auto_closed=False):
    with app_module.app.app_context():
        d = app_module._nairobi_today() - timedelta(days=days_ago)
        db.session.add(DispenseMeterReading(
            tenant_id=tenant_id, reading_date=d,
            opening_reading_m3=opening, closing_reading_m3=closing,
            consumption_m3=(closing - opening) if is_closed else 0.0,
            is_closed=is_closed, auto_closed=auto_closed,
            recorded_by="test"))
        db.session.commit()
        return d


def _get(tenant_id, d):
    with app_module.app.app_context():
        return DispenseMeterReading.query.filter_by(
            tenant_id=tenant_id, reading_date=d).first()


def test_cascade_preserves_auto_closed_invariant(client):
    """Retro-correct an earlier day -> cascade hits auto_closed downstream row.
    Auto_closed must keep opening==closing==prev_closing, consumption==0."""
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "ca1")
    today = app_module._nairobi_today()

    # Day -3: user-closed at 330
    _seed(days_ago=3, opening=300, closing=330, is_closed=True, auto_closed=False)
    # Day -2: auto-closed (sentinel opening==closing==330)
    auto_d2 = _seed(days_ago=2, opening=330, closing=330,
                    is_closed=True, auto_closed=True)
    # Day -1: user-closed at 380 with opening=330 (stale, but consistent with auto)
    _seed(days_ago=1, opening=330, closing=380, is_closed=True, auto_closed=False)

    # Retro-correct day -3: closing 330 -> 340.
    # Cascade will propagate opening=340 through day -2 (auto) and day -1 (user).
    d3 = today - timedelta(days=3)
    r = client.post("/api/admin/dispense/reading",
                    json={"reading_date": d3.isoformat(),
                          "closing_reading_m3": 340.0,
                          "force": True,
                          "reason": "phase 5a test"})
    assert r.status_code == 200, f"body={r.get_json()}"

    # Day -2 (auto_closed): must stay opening==closing==340, cons==0
    d2 = _get(1, auto_d2)
    assert d2.is_closed is True
    assert d2.auto_closed is True, "auto_closed flag lost during cascade"
    assert float(d2.opening_reading_m3) == 340.0
    assert float(d2.closing_reading_m3) == 340.0, (
        f"INVARIANT BROKEN: closing={d2.closing_reading_m3} != opening={d2.opening_reading_m3}"
    )
    assert float(d2.consumption_m3) == 0.0, (
        f"INVARIANT BROKEN: consumption={d2.consumption_m3} != 0"
    )


def test_cascade_reports_auto_closed_flag_in_affected(client):
    """cascade_affected entries must include was_auto_closed=True for auto days."""
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "ca2")
    today = app_module._nairobi_today()

    _seed(days_ago=3, opening=300, closing=330, is_closed=True, auto_closed=False)
    _seed(days_ago=2, opening=330, closing=330, is_closed=True, auto_closed=True)

    d3 = today - timedelta(days=3)
    r = client.post("/api/admin/dispense/reading",
                    json={"reading_date": d3.isoformat(),
                          "closing_reading_m3": 340.0,
                          "force": True,
                          "reason": "phase 5a affected test"})
    assert r.status_code == 200
    body = r.get_json()
    affected = body.get("cascade_affected", [])
    auto_entries = [x for x in affected if x.get("was_auto_closed") is True]
    assert len(auto_entries) == 1, f"expected 1 auto_closed entry, got {affected}"
    entry = auto_entries[0]
    assert entry["new_opening"] == 340.0
    assert entry["new_consumption_l"] == 0
