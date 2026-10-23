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

# ══════════════════════════════════════════════════════════════════════
# Phase A3b — past-day view + read-only mode
# ══════════════════════════════════════════════════════════════════════

def test_dispense_has_view_date_banner():
    html = _read('dispense.html')
    assert 'id="viewDateBanner"' in html
    assert 'id="viewDateText"' in html
    assert 'Back to Today' in html


def test_dispense_uses_url_date_param():
    html = _read('dispense.html')
    assert "URLSearchParams(location.search).get('date')" in html


def test_dispense_rows_have_data_date():
    html = _read('dispense.html')
    assert 'data-date="' in html


def test_dispense_has_close_past_day_mode():
    html = _read('dispense.html')
    assert 'Close Past Day' in html

# ══════════════════════════════════════════════════════════════════════
# Upgrade Step 1 — modern professional reminder banner
# ══════════════════════════════════════════════════════════════════════

def test_dispense_has_modern_reminder_banner():
    html = _read('dispense.html')
    assert 'id="dFormWarn"' in html                       # ID preserved
    assert 'class="dispense-warn hidden"' in html
    assert 'id="dFormWarnText"' in html
    assert 'dispense-warn-close' in html                  # dismiss button
    assert '.dispense-warn {' in html                     # CSS block


def test_dispense_reminder_copy_reworded():
    html = _read('dispense.html')
    assert 'Remember to enter the closing reading' in html
    assert 'Close the day to lock activity' not in html

# ══════════════════════════════════════════════════════════════════════
# Upgrade Step 2 — modern Void/Allocate buttons
# ══════════════════════════════════════════════════════════════════════

def test_dispense_has_modern_pay_buttons():
    html = _read('dispense.html')
    assert 'dispense-pay-action' in html
    assert 'dispense-pay-action--alloc' in html
    assert 'dispense-pay-action--void' in html
    assert '.dispense-pay-action {' in html
    assert 'dispense-pay-actions' in html          # wrapper


def test_dispense_old_pay_buttons_removed():
    html = _read('dispense.html')
    # Old generic dev-btn-based buttons must be gone
    assert 'dev-btn dev-btn--ghost" type="button" style="margin-right:.35rem;" onclick="reallocatePay' not in html
    assert 'dev-btn dev-btn--danger" type="button" onclick="voidPay' not in html

