"""UI structure regression tests.

Reads HTML/CSS source files directly (no HTTP, no browser) —
deterministic, fast, auth-independent. Catches accidental deletions
of critical DOM IDs, CSS classes, and structural markers that would
silently break the UI at runtime.

Not pixel-perfect — that is Playwright territory. This is a coarse
safety net for refactor regressions across the 7-phase delivery +
4-upgrade batch.
"""
import os

_FRONTEND = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'frontend'
))


def _read(name):
    with open(os.path.join(_FRONTEND, name), encoding='utf-8') as f:
        return f.read()


# ══════════════════════════════════════════════════════════════════════
# Step 6 — Team tab + drawer mirror
# ══════════════════════════════════════════════════════════════════════

def test_billing_home_team_tab_present():
    html = _read('index.html')
    assert 'id="teamTab"' in html
    assert 'href="/admin/team"' in html
    assert 'id="drawerTeam"' in html


def test_admin_team_page_critical_ids():
    html = _read('admin_team.html')
    required = [
        'teamCard', 'teamList', 'addTeamBtn', 'teamMsg',
        'teamModal', 'teamSaveBtn', 'tmUsername', 'tmPassword',
        'permModal', 'permSaveBtn', 'permList', 'permUsername',
    ]
    missing = [i for i in required if f'id="{i}"' not in html]
    assert not missing, f"admin_team.html missing: {missing}"


def test_admin_settings_team_block_removed():
    """Step 6: team card extracted; must not creep back."""
    html = _read('admin_settings.html')
    assert 'id="teamCard"' not in html
    assert 'id="teamModal"' not in html
    assert 'window.openTeamModal' not in html


def test_admin_team_no_share_url_block():
    """Step 1: share-URL block removed; must not creep back."""
    html = _read('admin_team.html')
    assert 'Share this login URL' not in html


# ══════════════════════════════════════════════════════════════════════
# Step 3 (upgrade) — team action button CSS classes
# ══════════════════════════════════════════════════════════════════════

def test_team_action_button_classes():
    html = _read('admin_team.html')
    required = [
        'team-action-btn--permissions',
        'team-action-btn--deactivate',
        'team-action-btn--reactivate',
        'team-action-btn--delete',
    ]
    missing = [c for c in required if c not in html]
    assert not missing, f"admin_team.html missing button classes: {missing}"


# ══════════════════════════════════════════════════════════════════════
# Step 7 — dispensing mobile drawer + floating preserved
# ══════════════════════════════════════════════════════════════════════

def test_dispense_drawer_dom():
    html = _read('dispense.html')
    required = [
        'menuToggle', 'drawerBackdrop', 'mobileDrawer', 'drawerClose',
        'drawerAnalyticsDisp', 'drawerTeamDisp', 'drawerSettingsDisp',
        'drawerSwitchBtn', 'drawerLogoutBtn',
    ]
    missing = [i for i in required if f'id="{i}"' not in html]
    assert not missing, f"dispense.html missing drawer IDs: {missing}"


def test_dispense_analytics_drawer_dom():
    html = _read('dispense_analytics.html')
    required = [
        'menuToggle', 'drawerBackdrop', 'mobileDrawer', 'drawerClose',
        'drawerDispensing', 'drawerTeamDisp', 'drawerSettingsDisp',
        'drawerSwitchBtn', 'drawerLogoutBtn',
    ]
    missing = [i for i in required if f'id="{i}"' not in html]
    assert not missing, f"dispense_analytics.html missing drawer IDs: {missing}"


def test_dispense_floating_header_preserved():
    """Step 7 exception: .settings-body must remain — creates floating look."""
    assert 'class="settings-body"' in _read('dispense.html')
    assert 'class="settings-body"' in _read('dispense_analytics.html')


# ══════════════════════════════════════════════════════════════════════
# Step 4 + Upgrade 3 — profile chooser JS
# ══════════════════════════════════════════════════════════════════════

def test_login_chooser_function_defined():
    html = _read('admin_login.html')
    assert 'function showProfileChooser' in html
    assert 'class="profile-choice"' in html
    assert 'id="__profileChooser"' in html


def test_login_auto_chooser_upgrade3_present():
    html = _read('admin_login.html')
    assert 'Upgrade 3: Auto-chooser' in html
    assert 'showSignOut: true' in html


def test_login_tenant_aware_signout_present():
    """Upgrade 3 fix-2: sign-out must preserve tenant context."""
    html = _read('admin_login.html')
    assert 'Upgrade 3-fix-2: tenant-aware sign-out' in html


# ══════════════════════════════════════════════════════════════════════
# Shared CSS — drawer classes must exist
# ══════════════════════════════════════════════════════════════════════

def test_style_css_drawer_classes():
    css = _read('style.css')
    required = ['.drawer', '.drawer-backdrop', '.drawer-nav',
                '.drawer-item', '.menu-toggle']
    missing = [c for c in required if c not in css]
    assert not missing, f"style.css missing drawer classes: {missing}"
