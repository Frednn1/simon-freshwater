"""Cross-tenant isolation tests — verify tenant scoping on team endpoints.

Purpose: prove that an admin authenticated to tenant N can NEVER see,
modify, delete, or deactivate employees belonging to any other tenant.
Today these protections rely entirely on `.filter_by(tenant_id=...)' —
a single missed filter in a future refactor would silently leak data.
These tests fail loudly if that happens.
"""
import json as _json
import uuid

import app as app_module
from helpers import make_admin, login, DEFAULT_PASS
from models import AdminUser, Tenant, db


# ══════════════════════════════════════════════════════════════════════
# Helpers (kept local — do not touch tests/helpers.py)
# ══════════════════════════════════════════════════════════════════════

def _u8():
    return uuid.uuid4().hex[:8]


def _make_tenant(tid, slug):
    """Ensure a Tenant row exists. Idempotent."""
    with app_module.app.app_context():
        if Tenant.query.filter_by(id=tid).first():
            return tid
        t = Tenant(id=tid, slug=slug, org_name=f"Tenant {tid}")
        db.session.add(t)
        db.session.commit()
    return tid


def _admin_login(client, tag):
    """Create a tenant-1 admin, log in, clear any pending chooser."""
    name = f"admin_x_{tag}_{_u8()}"
    make_admin(name)  # role=admin, tenant_id=1
    r = login(client, name)
    assert r.status_code == 200, f"admin login failed: {r.get_json()}"
    d = r.get_json()
    if d.get("requires_profile_choice"):
        s = client.post("/api/admin/profile/switch", json={"to": "billing"})
        assert s.status_code == 200, f"switch failed: {s.get_json()}"
    return name


# ══════════════════════════════════════════════════════════════════════
# 1. Team list — tenant 1 admin must not see tenant 2 employees
# ══════════════════════════════════════════════════════════════════════

def test_team_get_returns_only_own_tenant(client):
    _admin_login(client, "get")
    other_user = f"t2_emp_{_u8()}"
    make_admin(other_user, role="employee", tenant_id=2,
               profiles='["billing"]')

    r = client.get("/api/admin/team")
    assert r.status_code == 200
    usernames = [m["username"] for m in r.get_json()["team"]]
    assert other_user not in usernames, (
        f"SECURITY: tenant-2 employee {other_user!r} leaked into "
        f"tenant-1 team list"
    )


# ══════════════════════════════════════════════════════════════════════
# 2. Delete — tenant 1 admin cannot delete tenant 2 employee
# ══════════════════════════════════════════════════════════════════════

def test_team_delete_tenant_scoped(client):
    _admin_login(client, "del")
    other_user = f"t2_del_{_u8()}"
    other_id = make_admin(other_user, role="employee", tenant_id=2,
                          profiles='["billing"]')

    r = client.delete(f"/api/admin/team/{other_id}")
    assert r.status_code == 404, (
        f"SECURITY: cross-tenant delete returned {r.status_code} "
        f"(expected 404). Body: {r.get_json()}"
    )
    with app_module.app.app_context():
        u = AdminUser.query.filter_by(id=other_id).first()
        assert u is not None, "SECURITY: tenant-2 employee was deleted!"
        assert u.is_active, "SECURITY: tenant-2 employee was deactivated!"


# ══════════════════════════════════════════════════════════════════════
# 3. Permissions — tenant 1 admin cannot modify tenant 2 employee
# ══════════════════════════════════════════════════════════════════════

def test_team_permissions_tenant_scoped(client):
    _admin_login(client, "perm")
    other_user = f"t2_perm_{_u8()}"
    other_id = make_admin(other_user, role="employee", tenant_id=2,
                          profiles='["billing"]',
                          permissions='{"settings": true}')

    r = client.post(f"/api/admin/team/{other_id}/permissions",
                    json={"permissions": {"settings": False},
                          "profiles": ["billing"]})
    assert r.status_code == 404, (
        f"SECURITY: cross-tenant permission update returned "
        f"{r.status_code}. Body: {r.get_json()}"
    )
    with app_module.app.app_context():
        u = AdminUser.query.filter_by(id=other_id).first()
        perms = _json.loads(u.permissions_json or "{}")
        assert perms.get("settings") is True, (
            "SECURITY: cross-tenant permission write succeeded "
            f"(settings became {perms.get('settings')!r})"
        )


# ══════════════════════════════════════════════════════════════════════
# 4. Deactivate — tenant 1 admin cannot deactivate tenant 2 employee
# ══════════════════════════════════════════════════════════════════════

def test_team_deactivate_tenant_scoped(client):
    _admin_login(client, "deact")
    other_user = f"t2_deact_{_u8()}"
    other_id = make_admin(other_user, role="employee", tenant_id=2,
                          profiles='["billing"]')

    r = client.post(f"/api/admin/team/{other_id}/deactivate")
    assert r.status_code == 404, (
        f"SECURITY: cross-tenant deactivate returned {r.status_code}. "
        f"Body: {r.get_json()}"
    )
    with app_module.app.app_context():
        u = AdminUser.query.filter_by(id=other_id).first()
        assert u.is_active, "SECURITY: tenant-2 employee was deactivated!"


# ══════════════════════════════════════════════════════════════════════
# 5. Login mismatch — tenant-2 user via tenant-1 URL must be rejected
# ══════════════════════════════════════════════════════════════════════

def test_login_tenant_mismatch_rejected(client):
    # Ensure Tenant 2 row exists (needed for _admin_slug derivation)
    _make_tenant(2, "demo-tenant-2")
    t2_user = f"t2_login_{_u8()}"
    make_admin(t2_user, role="employee", tenant_id=2,
               profiles='["billing"]')

    # (a) Wrong tenant in URL → 401 with GENERIC error
    r = client.post("/api/admin/login?__tenant=simon-freshwater",
                    json={"username": t2_user, "password": DEFAULT_PASS})
    assert r.status_code == 401, (
        f"SECURITY: cross-tenant login accepted! status={r.status_code} "
        f"body={r.get_json()}"
    )
    body = r.get_json()
    assert body.get("ok") is False
    assert "Invalid credentials" in (body.get("error") or ""), (
        f"error message may leak tenant info: {body.get('error')!r}"
    )

    # (b) Correct tenant in URL → 200 (proves credentials are valid)
    r = client.post("/api/admin/login?__tenant=demo-tenant-2",
                    json={"username": t2_user, "password": DEFAULT_PASS})
    assert r.status_code == 200, (
        f"valid tenant-2 login rejected: {r.get_json()}"
    )

    # Clear session between sub-tests — (c) must run anonymous. Without
    # this, the authenticated employee guard fires on the login POST
    # (correct behavior, wrong test precondition).
    with client.session_transaction() as sess:
        sess.clear()

    # (c) No tenant URL → 200 (login is not tenant-scoped by default)
    r = client.post("/api/admin/login",
                    json={"username": t2_user, "password": DEFAULT_PASS})
    assert r.status_code == 200, (
        f"tenant-agnostic login rejected: {r.get_json()}"
    )
