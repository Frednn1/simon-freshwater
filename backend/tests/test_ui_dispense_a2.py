"""Phase A2 — dispense.html new UI elements."""
import os

_FRONTEND = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'frontend'
))


def _read(name):
    with open(os.path.join(_FRONTEND, name), encoding='utf-8') as f:
        return f.read()


def test_dispense_has_sale_closed_banner():
    html = _read('dispense.html')
    assert 'id="dSaleClosedBanner"' in html
    assert "Day closed. Sales cannot be recorded" in html


def test_dispense_has_no_activity_button():
    html = _read('dispense.html')
    assert 'id="dNoActivityBtn"' in html
    assert 'Mark as No Activity' in html


def test_dispense_has_payment_sale_date_picker():
    html = _read('dispense.html')
    assert 'id="dPaySaleDate"' in html
    assert 'Apply to which day' in html


def test_dispense_has_reading_warning_banner():
    html = _read('dispense.html')
    assert 'id="dFormWarn"' in html
