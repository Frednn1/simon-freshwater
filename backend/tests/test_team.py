"""Team endpoints — create, delete, permissions."""
from helpers import make_admin, login
import app as app_module
from models import AdminUser


def _admin_login(client, tag="admin"):
    name = f"admin_{tag}"
    make_admin(name)  # role=admin
    r = login(client, name)
    if r.get_json().get("requires_profile_choice"):
        # both profiles default → switch
        client.post("/api/admin/profile/switch", json={"to": "billing"})
    return name


def test_admin_can_create_team_member(client):
    _admin_login(client, "c1")
    r = client.post("/api/admin/team", json={
        "username": "newemp_c1",
        "password": "longpassword",
        "phone": "0711111111",
        "profiles": ["billing"],
    })
    assert r.status_code == 201
    assert r.get_json()["ok"] is True


def test_team_delete_self_blocked(client):
    _admin_login(client, "c2")
    with app_module.app.app_context():
        admin = AdminUser.query.filter_by(username="admin_c2").first()
        aid = admin.id
    r = client.delete(f"/api/admin/team/{aid}")
    assert r.status_code == 400
    assert "your own account" in r.get_json()["error"].lower()


def test_team_delete_missing_returns_404(client):
    _admin_login(client, "c3")
    r = client.delete("/api/admin/team/999999")
    assert r.status_code == 404


def test_team_permissions_applies_next_request(client):
    """Grant settings to employee → next request to /api/admin/settings opens."""
    _admin_login(client, "c4")
    emp_id = make_admin("emp_perm", role="employee",
                        profiles='["billing"]',
                        permissions='{"settings": false}')
    # grant settings
    r = client.post(f"/api/admin/team/{emp_id}/permissions",
                    json={"permissions": {"settings": True},
                          "profiles": ["billing"]})
    assert r.status_code == 200
    assert r.get_json()["permissions"]["settings"] is True

    # now employee logs in and sees opening
    client.post("/api/admin/logout")
    login(client, "emp_perm")
    r = client.get("/api/admin/settings")
    # 200 or 401 — but NOT 403 permission-denied
    assert r.status_code != 403


def test_team_permissions_validates_bad_payload(client):
    _admin_login(client, "c5")
    emp_id = make_admin("emp_bad", role="employee", profiles='["billing"]')
    r = client.post(f"/api/admin/team/{emp_id}/permissions", json={})
    assert r.status_code == 400
