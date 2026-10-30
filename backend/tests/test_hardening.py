"""Hardening — CF-Connecting-IP precedence + audit login-failure filter."""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from helpers import make_admin, login, DEFAULT_PASS


def test_client_ip_trusts_cf_after_cf_hop():
    """Legit CF path: XFF ends in CF IP → CF-Connecting-IP is used."""
    with app_module.app.test_request_context(
        headers={
            "CF-Connecting-IP": "1.2.3.4",
            # 162.158.0.0/15 is a Cloudflare range
            "X-Forwarded-For": "1.2.3.4, 162.158.110.152, 10.27.253.146",
        }
    ):
        assert app_module._client_ip() == "1.2.3.4"


def test_client_ip_uses_rightmost_public_when_no_cf():
    """No CF hop → rightmost public XFF entry (closest trusted hop)."""
    with app_module.app.test_request_context(
        headers={"X-Forwarded-For": "6.6.6.6, 2.2.2.2, 10.27.253.146"}
    ):
        assert app_module._client_ip() == "2.2.2.2"


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

def test_client_ip_ignores_cf_header_on_direct_origin_attempt():
    """Attacker bypasses CF: spoofed CF-Connecting-IP + spoofed XFF ending
    in a non-CF IP. Render appends real socket peer → rightmost public
    hop is the real attacker IP → rate-limited on that, not the spoof."""
    with app_module.app.test_request_context(
        headers={
            "CF-Connecting-IP": "99.99.99.99",  # spoof
            "X-Forwarded-For": "88.88.88.88, 5.5.5.5, 10.27.253.146",
        }
    ):
        # rightmost public = 5.5.5.5 (not CF) → return 5.5.5.5, not 99.99.99.99
        assert app_module._client_ip() == "5.5.5.5"


def test_client_ip_handles_fake_cf_ip_in_xff():
    """Attacker tries to fake a CF IP in XFF; Render appends real socket
    peer AFTER it → rightmost public hop is still the real client IP."""
    with app_module.app.test_request_context(
        headers={
            "CF-Connecting-IP": "99.99.99.99",  # spoof
            # 104.16.1.1 is CF range but attacker-supplied (will be left of real IP)
            "X-Forwarded-For": "104.16.1.1, 5.5.5.5, 10.27.253.146",
        }
    ):
        assert app_module._client_ip() == "5.5.5.5"


def test_client_ip_handles_malformed_xff():
    """Malformed XFF entries must not crash — falls through to peer."""
    with app_module.app.test_request_context(
        environ_base={"REMOTE_ADDR": "9.9.9.9"},
        headers={"X-Forwarded-For": "not-an-ip, garbage, 10.27.253.146"},
    ):
        # all filtered as private/unparseable → public = [] → fallback
        assert app_module._client_ip() == "9.9.9.9"

