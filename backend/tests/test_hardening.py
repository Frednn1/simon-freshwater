"""Hardening — CF-Connecting-IP precedence + audit login-failure filter."""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login, DEFAULT_PASS


def test_client_ip_prefers_cf_connecting_ip():
    """CF-Connecting-IP wins over XFF (which is client-spoofable)."""
    with app_module.app.test_request_context(
        headers={
            "CF-Connecting-IP": "1.2.3.4",
            "X-Forwarded-For": "6.6.6.6, 2.2.2.2",
        }
    ):
        assert app_module._client_ip() == "1.2.3.4"


def test_client_ip_uses_first_xff_when_no_cf():
    """Falls back to first XFF entry when CF-Connecting-IP absent."""
    with app_module.app.test_request_context(
        headers={"X-Forwarded-For": "10.0.0.1, 10.0.0.2, 10.0.0.3"}
    ):
        assert app_module._client_ip() == "10.0.0.1"


def test_client_ip_falls_back_to_remote_addr():
    # Flask caches REMOTE_ADDR on first access — must pass via environ_base
    with app_module.app.test_request_context(
        environ_base={"REMOTE_ADDR": "9.9.9.9"}
    ):
        assert app_module._client_ip() == "9.9.9.9"


def test_audit_exclude_login_failed_filters_correctly(client):
    """?exclude_login_failed=1 must drop login_failed and mismatch actions."""
    from models import AdminAuditLog, db as _db
    name = f"audit_filter_{uuid.uuid4().hex[:6]}"
    make_admin(name)

    # Seed audit rows directly (bypass rate limit interference)
    with app_module.app.app_context():
        for action in ("login_failed", "login_failed_tenant_mismatch", "custom_marker"):
            _db.session.add(AdminAuditLog(
                tenant_id=1,
                admin_username="seed",
                action=action,
                detail=f"probe-{uuid.uuid4().hex[:8]}",
            ))
        _db.session.commit()

    LOGIN = login(client, name)
    # Both profiles are enabled for tenant 1 in tests → profile choice
    # is required. Complete it so /api/admin/audit isn't blocked by
    # the pending guard (returns 403 otherwise).
    if LOGIN.get_json().get("requires_profile_choice"):
        client.post("/api/admin/profile/switch", json={"to": "billing"})

    # Without filter → login_failed visible
    r1 = client.get("/api/admin/audit?days=7")
    assert r1.status_code == 200
    actions_all = [row["action"] for row in r1.get_json()["audit"]]
    assert "login_failed" in actions_all
    assert "custom_marker" in actions_all

    # With filter → login_failed hidden, custom_marker still visible
    r2 = client.get("/api/admin/audit?days=7&exclude_login_failed=1")
    assert r2.status_code == 200
    actions_f = [row["action"] for row in r2.get_json()["audit"]]
    assert "login_failed" not in actions_f, "filter did not hide login_failed"
    assert "login_failed_tenant_mismatch" not in actions_f
    assert "custom_marker" in actions_f, "filter hid a non-login action"
