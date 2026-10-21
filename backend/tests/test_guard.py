"""Guard + permission gate behavior for employees."""
from helpers import make_admin, login

# Explicit all-off permissions — avoids relying on any default
_ALL_OFF = '{"settings": false, "analytics": false, "reports": false, ' \
           '"bulk_sms": false, "mpesa": false, ' \
           '"new_customer": false, "terminate": false}'


def test_employee_denied_settings_without_perm(client):
    make_admin("e_noset", role="employee", profiles='["billing"]',
               permissions=_ALL_OFF)
    login(client, "e_noset")
    assert client.get("/api/admin/settings").status_code == 403


def test_employee_denied_reports_without_perm(client):
    make_admin("e_norep", role="employee", profiles='["billing"]',
               permissions=_ALL_OFF)
    login(client, "e_norep")
    assert client.get("/api/admin/report.pdf").status_code == 403


def test_employee_denied_mpesa_without_perm(client):
    make_admin("e_nomp", role="employee", profiles='["billing"]',
               permissions=_ALL_OFF)
    login(client, "e_nomp")
    assert client.get("/api/admin/mpesa/config").status_code == 403


def test_employee_denied_new_customer_without_perm(client):
    make_admin("e_nocust", role="employee", profiles='["billing"]',
               permissions=_ALL_OFF)
    login(client, "e_nocust")
    assert client.post("/api/admin/consumer", json={}).status_code == 403


def test_employee_denied_terminate_without_perm(client):
    make_admin("e_noterm", role="employee", profiles='["billing"]',
               permissions=_ALL_OFF)
    login(client, "e_noterm")
    assert client.post("/api/admin/consumer/999/terminate", json={}).status_code == 403


def test_dispensing_only_employee_denied_consumer_even_with_perm(client):
    """Phase C4 defense-in-depth: new_customer requires billing profile too."""
    make_admin("e_disp_perm", role="employee", profiles='["dispensing"]',
               permissions='{"new_customer": true, "terminate": true}')
    login(client, "e_disp_perm")
    assert client.post("/api/admin/consumer", json={}).status_code == 403
    assert client.post("/api/admin/consumer/999/terminate", json={}).status_code == 403


def test_employee_allowed_dispense_core(client):
    """Core dispensing endpoints stay open regardless of tab permissions."""
    make_admin("e_core", role="employee", profiles='["dispensing"]',
               permissions=_ALL_OFF)
    login(client, "e_core")
    assert client.get("/api/admin/dispense/today").status_code == 200


def test_employee_html_nav_redirects(client):
    """HTML fallback: employee browsing to /admin/settings gets a redirect."""
    make_admin("e_html", role="employee", profiles='["dispensing"]',
               permissions=_ALL_OFF)
    login(client, "e_html")
    r = client.get("/admin/settings", headers={"Accept": "text/html"})
    assert r.status_code == 302
    assert r.headers["Location"] in ("/dispense", "/")
