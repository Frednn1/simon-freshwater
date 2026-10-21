"""Test helpers — create users, login, manipulate state."""
import sys, os, uuid
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app as app_module
from werkzeug.security import generate_password_hash
from models import AdminUser, db

DEFAULT_PASS = "testpass123"


def make_admin(username=None, password=DEFAULT_PASS, role="admin",
               profiles=None, permissions=None, is_super=False, tenant_id=1):
    """Create (or reset) an admin/employee with the given state. Returns id."""
    username = username or f"u_{uuid.uuid4().hex[:8]}"
    with app_module.app.app_context():
        u = AdminUser.query.filter_by(username=username).first()
        if u:
            u.is_active = True
            u.password_hash = generate_password_hash(password)
            if profiles is not None:
                u.profiles_json = profiles
            if permissions is not None:
                u.permissions_json = permissions
            db.session.commit()
            return u.id
        u = AdminUser(
            tenant_id=tenant_id,
            username=username,
            password_hash=generate_password_hash(password),
            phone="0700000000",
            user_role=role,
            is_active=True,
            is_super=is_super,
            profiles_json=profiles,
            permissions_json=permissions,
        )
        db.session.add(u)
        db.session.commit()
        return u.id


def login(client, username, password=DEFAULT_PASS):
    return client.post("/api/admin/login",
                       json={"username": username, "password": password})


def logout(client):
    return client.post("/api/admin/logout")


def set_permissions(username, permissions):
    with app_module.app.app_context():
        u = AdminUser.query.filter_by(username=username).first()
        assert u, f"user {username} not found"
        u.permissions_json = permissions
        db.session.commit()
