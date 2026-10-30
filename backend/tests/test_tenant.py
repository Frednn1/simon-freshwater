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

# ══════════════════════════════════════════════════════════════════════
# Step 2 — Consumer CRUD cross-tenant isolation
#
# Every consumer endpoint filters via _tenant_query(Consumer).filter_by(id=).
# These tests prove a tenant-1 admin cannot reach a tenant-2 consumer via
# any of these endpoints — the 404 (not 403-leak, not 200) is the proof.
# ══════════════════════════════════════════════════════════════════════

def _make_t2_consumer(tag):
    """Create a minimal consumer row on tenant 2. Returns the id."""
    import app as _a
    from models import Consumer
    from datetime import date
    with _a.app.app_context():
        c = Consumer(
            tenant_id=2,
            cust_name=f"T2 {tag} {_u8()}",
            acc_name=f"T2 Account {_u8()}",           # required — no default
            meter_acc_no=f"MTR-T2-{_u8()}",
            contact="0700000000",                       # field is "contact", not "phone"
            is_active=True,
        )
        db.session.add(c)
        db.session.commit()
        return c.id


def test_consumer_update_tenant_scoped(client):
    _admin_login(client, "cu")
    t2_cid = _make_t2_consumer("upd")
    r = client.post(f"/api/admin/consumer/{t2_cid}/update",
                    json={"cust_name": "HACKED"})
    assert r.status_code == 404, (
        f"SECURITY: cross-tenant consumer update returned "
        f"{r.status_code}. Body: {r.get_json()}"
    )
    with app_module.app.app_context():
        from models import Consumer
        c = Consumer.query.get(t2_cid)
        assert c.cust_name != "HACKED", (
            "SECURITY: cross-tenant consumer UPDATE succeeded!"
        )


def test_consumer_payment_tenant_scoped(client):
    _admin_login(client, "cp")
    t2_cid = _make_t2_consumer("pay")
    r = client.post(f"/api/admin/consumer/{t2_cid}/payment",
                    json={"amount": 999, "method": "cash"})
    assert r.status_code == 404, (
        f"SECURITY: cross-tenant consumer payment returned "
        f"{r.status_code}. Body: {r.get_json()}"
    )


def test_consumer_terminate_tenant_scoped(client):
    _admin_login(client, "ct")
    t2_cid = _make_t2_consumer("term")
    r = client.post(f"/api/admin/consumer/{t2_cid}/terminate")
    assert r.status_code == 404, (
        f"SECURITY: cross-tenant terminate returned {r.status_code}. "
        f"Body: {r.get_json()}"
    )
    with app_module.app.app_context():
        from models import Consumer
        assert Consumer.query.get(t2_cid).is_active, (
            "SECURITY: cross-tenant consumer was terminated!"
        )


def test_consumer_delete_tenant_scoped(client):
    _admin_login(client, "cd")
    t2_cid = _make_t2_consumer("del")
    r = client.delete(f"/api/admin/consumer/{t2_cid}")
    assert r.status_code == 404, (
        f"SECURITY: cross-tenant consumer delete returned "
        f"{r.status_code}. Body: {r.get_json()}"
    )
    with app_module.app.app_context():
        from models import Consumer
        assert Consumer.query.get(t2_cid) is not None, (
            "SECURITY: cross-tenant consumer was DELETED!"
        )


def test_consumer_reactivate_tenant_scoped(client):
    """Tenant-2 consumer is terminated; tenant-1 admin must not reactivate."""
    _admin_login(client, "cr")
    t2_cid = _make_t2_consumer("react")
    with app_module.app.app_context():
        from models import Consumer
        c = Consumer.query.get(t2_cid)
        c.is_active = False
        db.session.commit()
    r = client.post(f"/api/admin/consumer/{t2_cid}/reactivate")
    assert r.status_code == 404, (
        f"SECURITY: cross-tenant reactivate returned {r.status_code}. "
        f"Body: {r.get_json()}"
    )
    with app_module.app.app_context():
        from models import Consumer
        assert Consumer.query.get(t2_cid).is_active is False, (
            "SECURITY: cross-tenant consumer was reactivated!"
        )

# ══════════════════════════════════════════════════════════════════════
# Step 4 upgrade — cross-profile switch enforcement
#
# Backend enforcement of employee profile assignment is in
# admin_profile_switch (target not in _employee_profiles -> 403).
# These tests prove it holds AND that whoami exposes the right flags
# so the frontend never shows an unusable switch button.
# ══════════════════════════════════════════════════════════════════════

def test_whoami_both_profile_employee_can_switch_both(client):
    """Both-profile employee: effective = both, can_switch_to_* both true."""
    make_admin("emp_both_sw", role="employee", profiles='["billing","dispensing"]')
    login(client, "emp_both_sw")  # pending chooser fires
    d = client.get("/api/admin/whoami").get_json()
    assert d["can_switch_to_billing"] is True
    assert d["can_switch_to_dispensing"] is True
    assert sorted(d["effective_profiles"]) == ["billing", "dispensing"]


def test_whoami_dispensing_only_employee_cannot_switch_to_billing(client):
    """Dispensing-only employee: effective = [dispensing], can_switch_to_billing false."""
    make_admin("emp_disp_sw", role="employee", profiles='["dispensing"]')
    login(client, "emp_disp_sw")
    d = client.get("/api/admin/whoami").get_json()
    assert d["can_switch_to_billing"] is False
    assert d["can_switch_to_dispensing"] is True
    assert d["effective_profiles"] == ["dispensing"]


def test_switch_api_employee_unassigned_profile_403(client):
    """Dispensing-only employee POST switch to billing -> 403 (backend enforcement)."""
    make_admin("emp_disp_403", role="employee", profiles='["dispensing"]')
    login(client, "emp_disp_403")
    r = client.post("/api/admin/profile/switch", json={"to": "billing"})
    assert r.status_code == 403, (
        f"SECURITY: dispensing-only employee switched to billing! "
        f"status={r.status_code} body={r.get_json()}"
    )
    assert "not available for your account" in (r.get_json().get("error") or "").lower()


def test_switch_api_both_profile_employee_to_billing_200(client):
    """Both-profile employee POST switch to billing -> 200 (allowed)."""
    make_admin("emp_both_200", role="employee", profiles='["billing","dispensing"]')
    login(client, "emp_both_200")  # pending fires
    r = client.post("/api/admin/profile/switch", json={"to": "billing"})
    assert r.status_code == 200
    assert r.get_json().get("ok") is True
    assert r.get_json().get("active_profile") == "billing"

# ══════════════════════════════════════════════════════════════════════
# Step 1 — Audit trail tenant persistence
# ══════════════════════════════════════════════════════════════════════

def test_login_failed_unknown_username_attributed_to_url_tenant(client):
    """Failed login with a nonexistent username lands at the URL tenant
    (2 = demo-tenant-2). Matches policy: endpoint owner sees all attempts
    against their endpoint, regardless of where the username belongs."""
    from models import AdminAuditLog
    _make_tenant(2, "demo-tenant-2")
    fake = f"nonexistent_{_u8()}"
    r = client.post("/api/admin/login?__tenant=demo-tenant-2",
                    json={"username": fake, "password": "wrongpw"})
    assert r.status_code == 401
    with app_module.app.app_context():
        row = (AdminAuditLog.query
               .filter_by(action="login_failed")
               .filter(AdminAuditLog.detail.contains(fake))
               .order_by(AdminAuditLog.id.desc())
               .first())
        assert row is not None, "audit row should exist"
        assert row.tenant_id == 2, (
            f"expected tenant_id=2 (URL tenant), got {row.tenant_id}"
        )


def test_login_failed_other_tenant_user_attributed_to_url_tenant(client):
    """Attempt with tenant-2 user at tenant-1 URL logs to tenant-1's trail.
    The endpoint owner (SFW) sees the attempt. Cross-tenant username is
    intentionally visible per policy — the URL owner needs to know who
    tried to log in against their endpoint."""
    from models import AdminAuditLog
    _make_tenant(2, "demo-tenant-2")
    t2_user = f"t2_u_{_u8()}"
    make_admin(t2_user, role="employee", tenant_id=2, profiles='["billing"]')
    r = client.post("/api/admin/login?__tenant=simon-freshwater",
                    json={"username": t2_user, "password": "wrongpw"})
    assert r.status_code == 401
    with app_module.app.app_context():
        row = (AdminAuditLog.query
               .filter_by(action="login_failed")
               .filter(AdminAuditLog.detail.contains(t2_user))
               .order_by(AdminAuditLog.id.desc())
               .first())
        assert row is not None
        assert row.tenant_id == 1, (
            f"expected tenant_id=1 (URL tenant = simon-freshwater), "
            f"got {row.tenant_id}"
        )
        # Forensic context preserved
        assert "admin_tenant=demo-tenant-2" in (row.detail or "")


def test_login_failed_mismatch_attributed_to_url_tenant(client):
    """Mismatch (correct password, wrong URL tenant) logs to URL tenant.
    Detail preserves both requested_tenant and admin_tenant for forensics."""
    from models import AdminAuditLog
    _make_tenant(2, "demo-tenant-2")
    t2_user = f"t2_mm_{_u8()}"
    make_admin(t2_user, role="employee", tenant_id=2, profiles='["billing"]')
    r = client.post("/api/admin/login?__tenant=simon-freshwater",
                    json={"username": t2_user, "password": DEFAULT_PASS})
    assert r.status_code == 401, "mismatch should be rejected"
    with app_module.app.app_context():
        row = (AdminAuditLog.query
               .filter_by(action="login_failed_tenant_mismatch")
               .filter(AdminAuditLog.detail.contains(t2_user))
               .order_by(AdminAuditLog.id.desc())
               .first())
        assert row is not None
        assert row.tenant_id == 1, (
            f"expected tenant_id=1 (URL tenant), got {row.tenant_id}"
        )
        assert "requested_tenant=simon-freshwater" in (row.detail or "")
        assert "admin_tenant=demo-tenant-2" in (row.detail or "")


def test_audit_read_is_tenant_scoped(client):
    """Tenant-1 admin's /api/admin/audit must NOT show tenant-2 rows."""
    from models import AdminAuditLog, db as _db
    _make_tenant(2, "demo-tenant-2")
    with app_module.app.app_context():
        _db.session.add(AdminAuditLog(
            tenant_id=2,
            admin_username="t2_marker",
            action="step1_marker_t2",
            detail="tenant-2-only-marker",
        ))
        _db.session.commit()
    _admin_login(client, "aread")
    r = client.get("/api/admin/audit?days=7")
    assert r.status_code == 200
    rows = r.get_json().get("audit", [])
    assert not any(row.get("action") == "step1_marker_t2" for row in rows), (
        "SECURITY: tenant-2 audit row leaked into tenant-1's list"
    )


def test_audit_purge_is_tenant_scoped(client):
    """Tenant-1 admin's purge must NOT delete tenant-2's rows."""
    from models import AdminAuditLog, db as _db
    from datetime import datetime as _dt, timedelta as _td
    _make_tenant(2, "demo-tenant-2")
    with app_module.app.app_context():
        _db.session.add(AdminAuditLog(
            tenant_id=2,
            admin_username="t2_purge",
            action="step1_purge_t2",
            detail="tenant-2-old-row",
            created_at=_dt.utcnow() - _td(days=365),
        ))
        _db.session.commit()
    _admin_login(client, "apurge")
    r = client.post("/api/admin/audit/purge")
    assert r.status_code == 200
    with app_module.app.app_context():
        row = (AdminAuditLog.query
               .filter_by(action="step1_purge_t2")
               .first())
        assert row is not None, (
            "SECURITY: tenant-1's purge deleted tenant-2's row"
        )
        assert row.tenant_id == 2

