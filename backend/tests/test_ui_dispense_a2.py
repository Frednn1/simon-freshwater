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

# ══════════════════════════════════════════════════════════════════════
# Upgrade Step 3 — 3-tab navigation
# ══════════════════════════════════════════════════════════════════════

def test_dispense_has_3tab_nav():
    html = _read('dispense.html')
    assert 'id="dispenseTabs"' in html
    # Count via data-tab attribute — robust to extra classes added by
    # later UX patches (e.g. dispense-tab-noicon)
    assert html.count('data-tab="sales"') == 1
    assert html.count('data-tab="payments"') == 1
    assert html.count('data-tab="history"') == 1
    assert html.count('class="dispense-tab dispense-tab-noicon"') == 2
    assert html.count('class="dispense-tab"') == 1   # only History (no icon-less variant)


def test_dispense_has_3tab_panels():
    html = _read('dispense.html')
    assert html.count('class="dispense-tab-panel"') == 3
    assert 'data-panel="sales"' in html
    assert 'data-panel="payments"' in html
    assert 'data-panel="history"' in html


def test_dispense_tab_toggle_js():
    html = _read('dispense.html')
    assert "getElementById('dispenseTabs')" in html
    assert 'dispense-tab--active' in html
    assert '.dispense-tab-panel[data-panel=' in html

# ══════════════════════════════════════════════════════════════════════
# Step 3 UX enhancement — icon cleanup + single-row fitting
# ══════════════════════════════════════════════════════════════════════

def test_dispense_tabs_no_prefix_icons_sales_payment():
    html = _read('dispense.html')
    # Sales + Payment have no prefix icon
    assert "\U0001F4B7</span> Record Sales" not in html
    assert "\U0001F4B0</span> Record Payment" not in html
    # Activity History icon preserved
    assert "\U0001F4CB</span>" in html
    # Two noicon tabs marked
    assert html.count('dispense-tab dispense-tab-noicon') == 2


def test_dispense_tabs_single_row_layout():
    html = _read('dispense.html')
    # Nowrap on container
    assert 'flex-wrap: nowrap' in html
    # Equal-width flex on tabs
    assert 'flex: 1 1 0' in html
    # Mobile media queries present
    assert 'max-width: 640px' in html
    assert 'max-width: 420px' in html

# ══════════════════════════════════════════════════════════════════════
# Header full-width background fix
# ══════════════════════════════════════════════════════════════════════

def test_header_full_width_background():
    for fn in ('dispense.html', 'dispense_analytics.html'):
        html = _read(fn)
        # New design (position: fixed): header anchored to visual viewport
        assert 'position: fixed;' in html, f'{fn}: header must be position:fixed'
        assert 'top: 0;' in html and 'left: 0;' in html and 'right: 0;' in html, \
            f'{fn}: fixed inset missing'
        assert '.settings-body .site-header {' in html, f'{fn}: selector missing'


def test_header_step7b_mobile_fix_preserved():
    for fn in ('dispense.html', 'dispense_analytics.html'):
        html = _read(fn)
        assert 'Step 7b mobile fix' in html, f'{fn}: Step 7b comment missing'
        assert 'background: var(--blue-900);' in html, f'{fn}: Step 7b override missing'

# ══════════════════════════════════════════════════════════════════════
# Header full-width — explicit width (bypass flex stretch quirk)
# ══════════════════════════════════════════════════════════════════════

def test_header_explicit_full_width_rule():
    for fn in ('dispense.html', 'dispense_analytics.html'):
        html = _read(fn)
        # New design: fixed positioning anchored to visual viewport
        assert 'position: fixed;' in html, f'{fn}: header must be position:fixed'
        assert 'top: 0;' in html and 'left: 0;' in html and 'right: 0;' in html, \
            f'{fn}: fixed inset missing'
        assert 'z-index: 100;' in html, f'{fn}: z-index missing'
        # Body reserves space so fixed header does not overlap content
        assert 'padding-top: 92px;' in html, f'{fn}: body padding-top missing'
        # html preserves existing hidden overflow + blue-900 background
        assert 'html {' in html and 'overflow-x: hidden;' in html, \
            f'{fn}: html overflow-x hidden missing'
        assert 'background: var(--blue-900);' in html, f'{fn}: html bg missing'



def test_dispense_solid_blue900_background():
    for fn in ('dispense.html', 'dispense_analytics.html'):
        html = _read(fn)
        # Body override present
        assert 'body.settings-body {' in html, f'{fn}: body override missing'
        assert 'body.settings-body .site-header {' in html, f'{fn}: header override missing'
        # Both use blue-900 (not blue-700 gradient)
        # Confirm no blue-700 gradient remains in inline styles
        assert 'linear-gradient(135deg, var(--blue-900), var(--blue-700))' not in html, \
            f'{fn}: blue-700 gradient still in inline styles'

# ══════════════════════════════════════════════════════════════════════
# Flex-item overflow fix — .settings-wrap must not expand past viewport
# ══════════════════════════════════════════════════════════════════════

def test_settings_wrap_min_width_zero():
    for fn in ('dispense.html', 'dispense_analytics.html'):
        html = _read(fn)
        # Rule must be present with exact structure
        assert '.settings-wrap {\n      min-width: 0;\n    }' in html, \
            f'{fn}: .settings-wrap min-width: 0 missing'
        # Table scroll wrappers still present — this fix must NOT clip them
        assert 'overflow-x:auto' in html, f'{fn}: overflow-x:auto wrappers missing'
        # No body-level overflow: clip (that was the fix6 failure)
        assert 'overflow-x: clip' not in html, f'{fn}: overflow-x: clip must not be present'

# ══════════════════════════════════════════════════════════════════════
# Brand left-aligned on mobile — immune to header-inner width growth
# ══════════════════════════════════════════════════════════════════════

def test_brand_left_aligned_mobile():
    for fn in ('dispense.html', 'dispense_analytics.html'):
        html = _read(fn)
        assert '.settings-body .site-header .header-inner {' in html
        assert 'justify-content: flex-start;' in html, f'{fn}: flex-start missing'
        assert '.settings-body .site-header .brand {' in html
        assert 'padding: 0 0 0 60px;' in html, f'{fn}: left padding missing'

# ══════════════════════════════════════════════════════════════════════
# Dispensing M-Pesa tab
# ══════════════════════════════════════════════════════════════════════

def test_dispense_has_mpesa_tab():
    html = _read('dispense.html')
    assert 'id="tabMpesaDisp"' in html
    assert 'href="/admin/mpesa/dispensing"' in html
    assert 'id="drawerMpesaDisp"' in html


def test_dispense_analytics_has_mpesa_tab():
    html = _read('dispense_analytics.html')
    assert 'id="tabMpesaDisp"' in html
    assert 'href="/admin/mpesa/dispensing"' in html
    assert 'id="drawerMpesaDisp"' in html

# ══════════════════════════════════════════════════════════════════════
# A1 fix — normal auto-derive path (yesterday closed, today not yet opened)
# ══════════════════════════════════════════════════════════════════════

def test_dispense_a1_auto_derive_branch():
    html = _read('dispense.html')
    # Banner condition for normal derivation
    assert "d.suggested_opening_m3 != null && d.derivation_reason == null" in html
    # Marker comments
    assert 'A1 fix: normal derivation path' in html
    assert 'A1 fix: normal derivation available' in html


def test_dispense_a1_prefill_opening_for_normal_derivation():
    html = _read('dispense.html')
    # Both banner + form-mode branches present
    assert html.count("d.suggested_opening_m3 != null && d.derivation_reason == null") == 2

