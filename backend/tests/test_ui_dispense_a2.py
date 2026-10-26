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
        # Body reserves space for banner + header via CSS vars (calc sum)
        assert 'padding-top: calc(var(--dispense-banner-h) + var(--dispense-header-h))' in html, \
            f'{fn}: body padding-top calc missing'
        assert '--dispense-banner-h: 32px;' in html, f'{fn}: banner-h CSS var missing'
        assert '--dispense-header-h: 92px;' in html, f'{fn}: header-h CSS var missing'
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

def test_dispense_step1_opening_immutable():
    """Step 1: opening is derived and shown in card; hidden from form."""
    html = _read('dispense.html')
    # Card note + fallback
    assert 'id="todayDerivationNote"' in html
    assert 'Opening auto-derived from ' in html
    # Form: opening hidden when derivable
    assert 'Step 1: opening immutable — hidden from form' in html
    # Old "Edit if needed" removed
    assert 'Edit if needed' not in html



def test_dispense_a1_prefill_opening_for_normal_derivation():
    html = _read('dispense.html')
    # Both banner + form-mode branches present
    assert html.count("d.suggested_opening_m3 != null && d.derivation_reason == null") == 2

# ══════════════════════════════════════════════════════════════════════
# Step 1 — todayDerivationNote in card + no "Edit if needed"
# ══════════════════════════════════════════════════════════════════════

def test_today_derivation_note_in_card():
    html = _read('dispense.html')
    assert 'id="todayDerivationNote"' in html
    assert 'Opening auto-derived from ' in html
    # "Edit if needed" removed
    assert 'Edit if needed' not in html


def test_no_client_side_opening_override_comment():
    html = _read('dispense.html')
    # After Step 1, opening is immutable — client doesn't send it in derivable cases
    assert 'Step 1: opening immutable' in html

# ══════════════════════════════════════════════════════════════════════
# Step 2 gap fixes — banner removal, card gating, eventful-only, locked
# ══════════════════════════════════════════════════════════════════════

def test_dispense_banner_no_prev_missing_text():
    html = _read('dispense.html')
    assert "enter the opening reading manually" not in html


def test_dispense_prev_missing_card_gated_on_manual():
    html = _read('dispense.html')
    assert "d.prev_day_missing && d.manual_opening_required" in html


def test_dispense_create_past_day_eventful_gate():
    html = _read('dispense.html')
    assert "D-CL-2=A: only eventful days can be created" in html


def test_dispense_past_day_locked_state():
    html = _read('dispense.html')
    # Locked state: card visible, form hidden, day-closed title, re-sync button
    assert "title.textContent = 'Day Closed';" in html
    assert "form.style.display = 'none';" in html
    assert 'id="dResyncBtn"' in html
    assert 'This day is closed' in html

def test_dispense_create_past_day_alert_note():
    html = _read('dispense.html')
    assert 'Sales recorded on this day but no closing reading' in html

def test_load_context_open_gap_brace_balanced():
    """Bug guard: the if(open_gap) block must have a closing brace before else-if."""
    html = _read('dispense.html')
    # Correct pattern (with `}` between wMsg and the else-if comment)
    correct = "wMsg = 'Open reading on ' + d.open_gap_date + '. Enter opening manually.';\n              }\n              // else:"
    broken = "wMsg = 'Open reading on ' + d.open_gap_date + '. Enter opening manually.';\n              // else:"
    assert correct in html, "correct brace pattern missing"
    assert broken not in html, "broken pattern still present — syntax error"

# ══════════════════════════════════════════════════════════════════════
# Past-day submit handler — must fetch ctx with ?date=
# ══════════════════════════════════════════════════════════════════════

def test_submit_handler_respects_date_param():
    html = _read('dispense.html')
    # Both loadContext and submit handler must fetch /today with ?date=
    # when the URL has one. Currently expected: 2+ occurrences of the
    # pattern `'/api/admin/dispense/today' + _dateQS`.
    occurrences = html.count("'/api/admin/dispense/today' + _dateQS")
    assert occurrences >= 2, (
        f"expected at least 2 date-aware /today fetches (loadContext + submit handler), "
        f"found {occurrences}"
    )

# ══════════════════════════════════════════════════════════════════════
# Step 1 — Activity History: Opening column + Consumption rename
# ══════════════════════════════════════════════════════════════════════

def test_dispense_window_has_opening_column():
    html = _read('dispense.html')
    assert 'Opening (m&sup3;) Reading' in html
    assert 'Meter (L)' not in html
    assert 'Consumption (L)' in html


def test_dispense_window_colspan_is_10():
    html = _read('dispense.html')
    assert 'colspan="10"' in html
    assert 'colspan="8"' not in html
    assert 'colspan="9"' not in html

# ══════════════════════════════════════════════════════════════════════
# Re-sync chain button (closed past days)
# ══════════════════════════════════════════════════════════════════════

def test_dispense_has_resync_button():
    html = _read('dispense.html')
    assert 'id="dResyncBtn"' in html
    assert 'window.reSyncChain' in html
    assert 'Re-sync chain' in html
    # Confirm it targets /reading with force+reason
    assert 'reason: \'Re-sync downstream chain\'' in html

# ══════════════════════════════════════════════════════════════════════
# Phase B — today card alert when yesterday's closing is missing
# ══════════════════════════════════════════════════════════════════════

def test_dispense_today_missing_close_alert():
    html = _read('dispense.html')
    assert 'id="todayMissingCloseAlert"' in html
    assert 'id="todayMissingCloseText"' in html
    assert 'id="todayMissingCloseLink"' in html
    assert 'class="today-alert"' in html
    assert '.today-alert {' in html
    assert 'today-alert-link' in html


def test_dispense_today_alert_link_targets_yesterday():
    html = _read('dispense.html')
    # Both trigger branches must be scoped to today view only
    assert 'd.is_today && d.yesterday_open' in html
    assert 'd.is_today && d.prev_day_missing' in html
    # Link target uses yesterday_iso
    assert "'/dispense?date=' + d.yesterday_iso" in html

# ══════════════════════════════════════════════════════════════════════
# Header overlap fix — dynamic header height → body padding-top
# ══════════════════════════════════════════════════════════════════════



def test_dispense_send_button_all_statuses():
    html = _read('dispense.html')
    # New gate: show whenever a reading exists
    assert 'const showReport = row.has_reading;' in html
    # Old variance/outstanding gate must be gone
    assert '_vr !== 0 || _or !== 0' not in html
    # Send handler still present
    assert 'sendReconReport' in html

# ══════════════════════════════════════════════════════════════════════
# Download Report button in analytics page
# ══════════════════════════════════════════════════════════════════════

def test_dispense_analytics_has_download_report_btn():
    html = _read('dispense_analytics.html')
    assert 'id="downloadReportBtn"' in html
    assert '/api/admin/dispense/report.pdf' in html
    # Button must pass through current query string (preset / from-to)
    assert "location.search" in html

# ══════════════════════════════════════════════════════════════════════
# Step 1 upgrade — hide reading card on fresh, eventless day
# ══════════════════════════════════════════════════════════════════════

def test_dispense_hide_fresh_empty_card():
    html = _read('dispense.html')
    assert '_isFreshEmpty' in html
    # All 7 conditions must be present (else we'd over-hide legitimate cases)
    assert 'd.is_today === true' in html
    assert '!d.has_reading' in html
    assert '(d.sales_kes || 0) === 0' in html
    assert '(d.paid_kes || 0) === 0' in html
    assert '!d.yesterday_open' in html
    assert '!d.prev_day_missing' in html
    assert '!d.is_first_day' in html
    # Hide action + early return
    assert "card.style.display = 'none';\n            return;" in html or \
           "card.style.display = 'none';" in html


def test_dispense_hide_does_not_break_legitimate_cases():
    """The hide logic must not affect yesterday_open, prev_day_missing, is_first_day."""
    html = _read('dispense.html')
    # yesterday_open branch: still has its lock-and-return
    assert "if (d.yesterday_open && d.is_today) {" in html
    # past-day branch: still present
    assert "if (!d.is_today) {" in html
    # first_day / prev_day_missing branch: still present
    assert "d.is_first_day" in html
    assert "d.prev_day_missing" in html

# ══════════════════════════════════════════════════════════════════════
# bfcache auth guard — re-verify auth when page restored from back/forward cache
# ══════════════════════════════════════════════════════════════════════

def test_dispense_has_bfcache_guard():
    for fn in ('dispense.html', 'dispense_analytics.html'):
        html = _read(fn)
        assert 'bfcache-auth-guard' in html, f'{fn}: marker missing'
        assert "addEventListener('pageshow'" in html, f'{fn}: pageshow listener missing'
        assert 'e.persisted' in html, f'{fn}: persisted check missing'
        # Fail-closed redirect
        assert "location.replace('/admin/login')" in html, f'{fn}: redirect missing'
        # Defense-in-depth on visibilitychange
        assert "addEventListener('visibilitychange'" in html, f'{fn}: visibilitychange missing'

# ══════════════════════════════════════════════════════════════════════
# Logout confirm — mirror billing pattern, disqualify bfcache
# ══════════════════════════════════════════════════════════════════════

def test_dispense_logout_has_confirm():
    for fn in ('dispense.html', 'dispense_analytics.html'):
        html = _read(fn)
        assert "if (!confirm('Log out of the dispensing panel?')) return;" in html, (
            f'{fn}: missing confirm() before logout'
        )


def test_dispense_logout_uses_server_redirect():
    """Both handlers must use server-provided tenant-aware redirect URL."""
    for fn in ('dispense.html', 'dispense_analytics.html'):
        html = _read(fn)
        assert "(d && d.redirect) || fallback" in html, (
            f'{fn}: missing server-redirect usage'
        )
        assert "'/admin/login?__tenant={{tenant_slug}}'" in html, (
            f'{fn}: missing tenant-aware fallback'
        )


def test_dispense_drawer_logout_still_delegates():
    """Drawer logout must still delegate to header button (no duplicate handler)."""
    for fn in ('dispense.html', 'dispense_analytics.html'):
        html = _read(fn)
        # Drawer click -> closeDrawer + click #logoutBtn
        assert "var hdr = document.getElementById('logoutBtn');" in html, (
            f'{fn}: drawer delegation to header logout broken'
        )
        assert "if (hdr) hdr.click();" in html, (
            f'{fn}: drawer must click header button (no duplicate confirm)'
        )

# ══════════════════════════════════════════════════════════════════════
# Steps 1+2 — billing-style banner replaces switch button
# ══════════════════════════════════════════════════════════════════════

def test_dispense_has_profile_banner():
    for fn in ('dispense.html', 'dispense_analytics.html'):
        html = _read(fn)
        assert 'id="profileBanner"' in html, f'{fn}: missing profileBanner'
        assert 'id="profileBannerText"' in html, f'{fn}: missing banner text'
        assert 'id="profileSwitchLink"' in html, f'{fn}: missing switch link span'
        assert 'id="switchProfileLink"' in html, f'{fn}: missing switch anchor'


def test_dispense_switch_button_removed():
    for fn in ('dispense.html', 'dispense_analytics.html'):
        html = _read(fn)
        assert 'id="switchBtn"' not in html, f'{fn}: header switchBtn still present'
        assert 'id="drawerSwitchBtn"' not in html, f'{fn}: drawer switchBtn still present'


def test_dispense_banner_uses_whoami_and_can_switch():
    for fn in ('dispense.html', 'dispense_analytics.html'):
        html = _read(fn)
        assert 'd.can_switch_to_billing' in html, f'{fn}: banner must check can_switch_to_billing'
        assert "Signed in as " in html, f'{fn}: banner must show signed-in text'
        assert "Dispensing portal" in html, f'{fn}: banner must show Dispensing portal label'

# ══════════════════════════════════════════════════════════════════════
# Banner top fix — banner is topmost visual block, above sticky header
# ══════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════
# billing-matching-header — syncHeaderPad removed, banner flush at top
# ══════════════════════════════════════════════════════════════════════

def test_dispense_matches_billing_header_pattern():
    for fn in ('dispense.html', 'dispense_analytics.html'):
        html = _read(fn)
        # syncHeaderPad and its IIFE are fully removed
        assert 'syncHeaderPad' not in html, f'{fn}: syncHeaderPad must be removed'
        # Banner-inside-header architecture
        assert 'body.settings-body .site-header #profileBanner {' in html, f'{fn}: banner-in-header CSS missing'
        assert '--dispense-banner-h' in html, f'{fn}: banner height CSS var missing'
        assert '--dispense-header-h' in html, f'{fn}: header height CSS var missing'
        assert 'padding-top: calc(var(--dispense-banner-h) + var(--dispense-header-h))' in html, f'{fn}: calc padding-top missing'
        # Banner markup intact
        assert 'id="profileBanner"' in html, f'{fn}: profileBanner missing'

# ══════════════════════════════════════════════════════════════════════
# Menu icon flush with brand top edge (Option B)
# ══════════════════════════════════════════════════════════════════════

def test_dispense_menu_flush_with_brand_top():
    for fn in ('dispense.html', 'dispense_analytics.html'):
        html = _read(fn)
        assert '/* Top-align brand + menu icon' in html, \
            f'{fn}: header-inner flex-start comment missing'
        assert '/* Top-align logo + text block within brand */' in html, \
            f'{fn}: brand flex-start comment missing'
        # Count align-items: flex-start occurrences — at least 2 in the mobile media block
        assert html.count('align-items: flex-start;') >= 2, \
            f'{fn}: expected >= 2 align-items:flex-start, got {html.count("align-items: flex-start;")}'

