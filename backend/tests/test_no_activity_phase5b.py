"""Phase 5b — correction promotes auto_closed day to user-closed."""
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
    name = f"admin_na5b_{tag}_{uuid.uuid4().hex[:6]}"
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


def test_correction_promotes_auto_closed(client):
    """Correct an auto_closed day with force+reason -> auto_closed=False,
    consumption reflects real closing."""
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "p1")
    today = app_module._nairobi_today()

    # Day -3: user-closed baseline at 100 (so derivation source exists)
    _seed(days_ago=3, opening=100.0, closing=100.0,
          is_closed=True, auto_closed=False)
    # Day -2: auto-closed (placeholder, opening=closing=100, cons=0)
    auto_d2 = _seed(days_ago=2, opening=100.0, closing=100.0,
                    is_closed=True, auto_closed=True)

    # Admin corrects day -2 with real closing=150
    r = client.post("/api/admin/dispense/reading",
                    json={"reading_date": auto_d2.isoformat(),
                          "closing_reading_m3": 150.0,
                          "force": True,
                          "reason": "forgot to close, real reading"})
    assert r.status_code == 200, f"body={r.get_json()}"

    d2 = _get(1, auto_d2)
    assert d2.is_closed is True
    assert d2.auto_closed is False, "auto_closed should be False after correction"
    assert float(d2.opening_reading_m3) == 100.0
    assert float(d2.closing_reading_m3) == 150.0
    assert float(d2.consumption_m3) == 50.0


def test_correction_of_auto_closed_preserves_downstream_invariant(client):
    """Correct auto_closed day N -> cascade to day N+1 (auto_closed).
    Day N+1 must remain auto_closed with invariant opening==closing."""
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "p2")
    today = app_module._nairobi_today()

    # Day -3: user-closed at 100
    _seed(days_ago=3, opening=100.0, closing=100.0,
          is_closed=True, auto_closed=False)
    # Day -2: auto_closed at 100 (placeholder)
    auto_d2 = _seed(days_ago=2, opening=100.0, closing=100.0,
                    is_closed=True, auto_closed=True)
    # Day -1: auto_closed at 100 (placeholder)
    auto_d1 = _seed(days_ago=1, opening=100.0, closing=100.0,
                    is_closed=True, auto_closed=True)

    # Correct day -2 to closing=150
    r = client.post("/api/admin/dispense/reading",
                    json={"reading_date": auto_d2.isoformat(),
                          "closing_reading_m3": 150.0,
                          "force": True,
                          "reason": "forgot to close"})
    assert r.status_code == 200, f"body={r.get_json()}"

    # Day -2 promoted, has real consumption
    d2 = _get(1, auto_d2)
    assert d2.auto_closed is False
    assert float(d2.consumption_m3) == 50.0

    # Day -1 auto_closed: cascade updated opening to 150, invariant preserved
    d1 = _get(1, auto_d1)
    assert d1.auto_closed is True, "downstream auto_closed flag lost"
    assert d1.is_closed is True
    assert float(d1.opening_reading_m3) == 150.0
    assert float(d1.closing_reading_m3) == 150.0, (
        f"INVARIANT BROKEN: closing={d1.closing_reading_m3} != opening={d1.opening_reading_m3}"
    )
    assert float(d1.consumption_m3) == 0.0
