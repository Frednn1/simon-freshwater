"""Tier B: session_version invalidation on logout.

Closes the Case 2 race — delayed /dispense response refreshing the browser
cookie after /api/admin/logout cleared it. The bump+check pattern forces
re-login on ANY request carrying a stale session_version, regardless of
browser race conditions or multi-device logout.
"""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login
from models import AdminUser, db


def _admin_login(client, tag):
    name = f"admin_sv_{tag}_{uuid.uuid4().hex[:6]}"
    make_admin(name)
    r = login(client, name)
    if r.get_json().get("requires_profile_choice"):
        client.post("/api/admin/profile/switch", json={"to": "billing"})
    return name


def test_login_sets_session_sv(client):
    _admin_login(client, "set")
    with client.session_transaction() as sess:
        assert "sv" in sess, "sv not set in session after login"
        assert sess["sv"] == 0, f"expected sv=0 for fresh user, got {sess['sv']}"


def test_logout_bumps_session_version(client):
    name = _admin_login(client, "bump")
    with app_module.app.app_context():
        u = AdminUser.query.filter_by(username=name).first()
        initial = u.session_version
    client.post("/api/admin/logout")
    with app_module.app.app_context():
        u = AdminUser.query.filter_by(username=name).first()
        assert u.session_version == initial + 1, \
            f"session_version not bumped: {initial} -> {u.session_version}"


def test_current_admin_rejects_stale_sv(client):
    _admin_login(client, "stale")
    with client.session_transaction() as sess:
        sess["sv"] = 999
    r = client.get("/api/admin/whoami")
    d = r.get_json()
    assert d.get("authenticated") is False, \
        f"stale sv should have been rejected: {d}"


def test_current_admin_rejects_missing_sv(client):
    _admin_login(client, "missing")
    with client.session_transaction() as sess:
        sess.pop("sv", None)
    r = client.get("/api/admin/whoami")
    d = r.get_json()
    assert d.get("authenticated") is False, \
        f"missing sv should have been rejected (legacy cookie): {d}"


def test_revived_cookie_after_logout_is_rejected(client):
    """Simulate the Case 2 race: after logout, an old cookie comes back.
    Because logout bumped user.session_version, the revived cookie's sv
    no longer matches — request is rejected -> forces re-login."""
    _admin_login(client, "revive")
    # Capture authenticated session state
    with client.session_transaction() as sess:
        saved = dict(sess)
    # Logout normally (bumps session_version)
    client.post("/api/admin/logout")
    # Simulate the revived-cookie race: restore old session wholesale
    with client.session_transaction() as sess:
        sess.clear()
        sess.update(saved)
    # Any protected request must now be rejected
    r = client.get("/api/admin/whoami")
    d = r.get_json()
    assert d.get("authenticated") is False, \
        f"revived cookie should be invalidated by version bump: {d}"
    r2 = client.get("/dispense")
    assert r2.status_code == 302, \
        f"/dispense should redirect after revived-cookie rejection, got {r2.status_code}"


def test_logout_records_audit_with_bumped_state(client):
    """Logout audit row must exist and reference the user (proves audit
    still works when version was bumped)."""
    name = _admin_login(client, "audit")
    client.post("/api/admin/logout")
    with app_module.app.app_context():
        from models import AdminAuditLog
        row = (AdminAuditLog.query
               .filter_by(admin_username=name, action="logout")
               .order_by(AdminAuditLog.id.desc())
               .first())
        assert row is not None, "logout audit row not written"
