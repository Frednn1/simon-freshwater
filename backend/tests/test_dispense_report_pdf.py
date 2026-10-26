"""Dispensing Activity Report PDF endpoint tests."""
import sys, os, uuid
import pytest

# Skip cleanly if PyMuPDF not installed (e.g. Termux local dev).
# Production/CI has pymupdf in requirements.txt so tests always run there.
pytest.importorskip("fitz", reason="PyMuPDF not installed locally")
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login
from models import (
    DispenseMeterReading, DispenseRate, DispenseSale,
    TenantProfile, db,
)
from datetime import timedelta
from io import BytesIO


def _admin_login(client, tag):
    name = f"admin_rpt_{tag}_{uuid.uuid4().hex[:6]}"
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
        db.session.commit()


def _seed(tenant_id=1, days_ago=1, opening=100.0, closing=150.0):
    with app_module.app.app_context():
        d = app_module._nairobi_today() - timedelta(days=days_ago)
        db.session.add(DispenseMeterReading(
            tenant_id=tenant_id, reading_date=d,
            opening_reading_m3=opening, closing_reading_m3=closing,
            consumption_m3=closing - opening, is_closed=True,
            recorded_by="test"))
        db.session.commit()


def test_report_pdf_returns_valid_pdf_for_preset_7(client):
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "p7")
    _seed(days_ago=2, opening=100, closing=150)
    _seed(days_ago=1, opening=150, closing=180)
    r = client.get("/api/admin/dispense/report.pdf?preset=7")
    assert r.status_code == 200, f"body={r.data[:200]}"
    assert r.headers.get("Content-Type", "").startswith("application/pdf")
    # PDF magic bytes
    assert r.data[:5] == b"%PDF-"
    # Valid PDF via fitz
    try:
        import fitz
        doc = fitz.open(stream=BytesIO(r.data), filetype="pdf")
        assert len(doc) >= 1
        doc.close()
    except ImportError:
        pass


def test_report_pdf_preset_30(client):
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "p30")
    r = client.get("/api/admin/dispense/report.pdf?preset=30")
    assert r.status_code == 200
    assert r.data[:5] == b"%PDF-"


def test_report_pdf_preset_90(client):
    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "p90")
    r = client.get("/api/admin/dispense/report.pdf?preset=90")
    assert r.status_code == 200
    assert r.data[:5] == b"%PDF-"

def test_report_pdf_rows_descending(client):
    """Report rows must be DESCENDING (most recent first)."""
    import pytest as _pt
    try:
        import fitz as _fitz
    except ImportError:
        _pt.skip("PyMuPDF not installed locally")

    _enable(); _ensure_rate(); _wipe()
    _admin_login(client, "desc")
    _seed(days_ago=3, opening=100, closing=150)
    _seed(days_ago=2, opening=150, closing=200)
    _seed(days_ago=1, opening=200, closing=250)
    r = client.get("/api/admin/dispense/report.pdf?preset=7")
    assert r.status_code == 200
    doc = _fitz.open(stream=BytesIO(r.data), filetype="pdf")
    txt = "\n".join(p.get_text() for p in doc)
    doc.close()
    import datetime as _dt
    today = app_module._nairobi_today()
    newest = (today - _dt.timedelta(days=1)).strftime("%d %b")
    older = (today - _dt.timedelta(days=3)).strftime("%d %b")
    assert newest in txt, f"newest date {newest} missing"
    assert older in txt, f"older date {older} missing"
    assert txt.index(newest) < txt.index(older), (
        f"rows not descending: newest at {txt.index(newest)}, "
        f"older at {txt.index(older)}"
    )

# ══════════════════════════════════════════════════════════════════════
# Regression — D1 SQL-variable limit (90-day report)
# ══════════════════════════════════════════════════════════════════════

def test_window_stats_90_days_returns_90_dates(client):
    """Regression: 90-day call must not hit D1 SQL-variable limit.

    Fixed by switching from .in_() on the dates list to range filters.
    On D1 the old approach sent 1+1+90+90=182 bind params in the payments
    query and failed with 'too many SQL variables at offset 130'."""
    import app as app_module
    from datetime import timedelta
    with app_module.app.app_context():
        today = app_module._nairobi_today()
        dates = [today - timedelta(days=i) for i in range(90)]
        result = app_module._dispense_window_stats(1, dates)
    assert len(result) == 90, f"expected 90 entries, got {len(result)}"
    for d in dates:
        assert d.isoformat() in result, f"missing {d.isoformat()} in result"


def test_window_stats_range_filter_preserves_data(client):
    """Regression: range filter must still pick up data deep in the window."""
    import app as app_module
    from models import DispenseMeterReading, db
    from datetime import timedelta
    with app_module.app.app_context():
        today = app_module._nairobi_today()
        d45 = today - timedelta(days=45)
        DispenseMeterReading.query.filter_by(tenant_id=1, reading_date=d45).delete()
        db.session.add(DispenseMeterReading(
            tenant_id=1, reading_date=d45,
            opening_reading_m3=100.0, closing_reading_m3=150.0,
            consumption_m3=50.0, is_closed=True, recorded_by="test",
        ))
        db.session.commit()

        dates = [today - timedelta(days=i) for i in range(90)]
        result = app_module._dispense_window_stats(1, dates)

        target_iso = d45.isoformat()
        assert target_iso in result
        assert result[target_iso]["consumption_l"] == 50000, (
            f"expected 50,000 L, got {result[target_iso]['consumption_l']}"
        )
        assert result[target_iso]["opening_reading_m3"] == 100.0
        assert result[target_iso]["is_closed"] is True

        DispenseMeterReading.query.filter_by(tenant_id=1, reading_date=d45).delete()
        db.session.commit()

# ══════════════════════════════════════════════════════════════════════
# Logo height = text block height (name top → tagline bottom)
# ══════════════════════════════════════════════════════════════════════

def test_dispense_report_logo_height_matches_text_block():
    """Logo is sized to span from org-name top to tagline bottom, not the
    old fixed 48pt box."""
    import inspect
    import dispense_report as dr
    src = inspect.getsource(dr._draw_header)
    assert '_tagline_bottom' in src, "tagline_bottom computation missing"
    assert '_logo_height' in src, "logo_height computation missing"
    assert 'abs(_font_sub.descender)' in src, "font descender usage missing"
    assert 'size=48' not in src, "old fixed size=48 still present"


def test_dispense_report_logo_top_preserved():
    """Logo top edge must stay at y - 13 (name ascender preserved)."""
    import inspect
    import dispense_report as dr
    src = inspect.getsource(dr._draw_header)
    assert '_logo_top = y - 13' in src, "logo top preservation removed"
    assert '_try_insert_logo(page, x, _logo_top, _logo_height, logo_bytes)' in src, \
        "logo helper call shape changed"


def test_dispense_report_header_drawing_preserved():
    """Verify name / tagline / report title / period / timestamp all still drawn."""
    import inspect
    import dispense_report as dr
    src = inspect.getsource(dr._draw_header)
    assert 'fontsize=18' in src, "name fontsize missing"
    assert 'fontsize=9' in src, "tagline fontsize missing"
    assert 'Dispensing Activity Report' in src, "report title missing"
    assert 'Period:' in src, "period line missing"
    assert 'Generated:' in src, "generated line missing"

