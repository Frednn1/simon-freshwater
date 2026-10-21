"""Auth + profile-routing + R5 pending-flag behavior."""
from helpers import make_admin, login, DEFAULT_PASS


def test_login_admin_ok(client):
    make_admin("a_ok")
    r = login(client, "a_ok")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True
    assert d["user_role"] == "admin"


def test_login_bad_password(client):
    make_admin("a_bad")
    r = login(client, "a_bad", "wrong-password")
    assert r.status_code == 401
    assert r.get_json()["ok"] is False


def test_login_single_profile_employee_no_choice(client):
    make_admin("e_single", role="employee", profiles='["dispensing"]')
    r = login(client, "e_single")
    d = r.get_json()
    assert d["ok"] is True
    assert d["requires_profile_choice"] is False
    assert d["redirect_to"] == "/dispense"


def test_login_billing_only_employee_routes_to_root(client):
    make_admin("e_bill", role="employee", profiles='["billing"]')
    r = login(client, "e_bill")
    d = r.get_json()
    assert d["requires_profile_choice"] is False
    assert d["redirect_to"] == "/"


def test_login_both_profile_employee_requires_choice(client):
    make_admin("e_both", role="employee", profiles='["billing","dispensing"]')
    r = login(client, "e_both")
    d = r.get_json()
    assert d["requires_profile_choice"] is True
    assert d["redirect_to"] is None
    assert sorted(d["profiles"]) == ["billing", "dispensing"]


def test_login_both_profile_admin_requires_choice(client):
    make_admin("a_both")
    r = login(client, "a_both")
    d = r.get_json()
    assert d["requires_profile_choice"] is True
    assert d["redirect_to"] is None


def test_r5_pending_blocks_then_switch_clears(client):
    make_admin("e_r5", role="employee", profiles='["billing","dispensing"]')
    assert login(client, "e_r5").get_json()["requires_profile_choice"] is True

    # blocked by pending
    r = client.get("/api/admin/team")
    assert r.status_code == 403
    assert "Please choose a profile" in r.get_json()["error"]

    # chooser works
    r = client.post("/api/admin/profile/switch", json={"to": "billing"})
    assert r.status_code == 200

    # pending cleared — different error message now
    r = client.get("/api/admin/team")
    assert r.status_code == 403
    assert "Please choose a profile" not in r.get_json()["error"]


def test_r5_single_profile_no_pending_flag(client):
    """Single-profile employee never sets pending — direct access works."""
    make_admin("e_direct", role="employee", profiles='["dispensing"]')
    login(client, "e_direct")
    r = client.get("/api/admin/whoami")
    assert r.status_code == 200
    d = r.get_json()
    assert d["user_role"] == "employee"
