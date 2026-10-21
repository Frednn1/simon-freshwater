"""Pytest fixtures for simon-freshwater.

SAFETY: sets env vars BEFORE importing app so import-time db.create_all()
and start_scheduler() operate on an isolated temp DB with scheduler
disabled. Never touches production.
"""
import os, sys, tempfile, pytest

# ── MUST run before importing app ──
_tmp_db = tempfile.NamedTemporaryFile(prefix="sfw_test_", suffix=".db", delete=False)
_tmp_db.close()
os.environ["DATABASE_URL"]      = f"sqlite:///{_tmp_db.name}"
os.environ["D1_BACKUP_ENABLED"] = "false"
os.environ["SECRET_KEY"]        = "pytest-only-not-for-prod"
os.environ["ADMIN_2FA_ENABLED"] = "false"
os.environ.pop("RENDER", None)

_TESTS   = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_TESTS, ".."))
for _p in (_TESTS, _BACKEND):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import app as app_module  # noqa: E402


@pytest.fixture(scope="session")
def flask_app():
    return app_module.app


@pytest.fixture(scope="session", autouse=True)
def _setup_tenant_profiles(flask_app):
    """Enable BOTH profiles on tenant 1 so tests exercise the real
    both-profile state (matches demo tenant). Idempotent."""
    from models import TenantProfile, db
    with flask_app.app_context():
        for pname in ("billing", "dispensing"):
            existing = TenantProfile.query.filter_by(tenant_id=1, profile_name=pname).first()
            if existing:
                existing.enabled = True
            else:
                db.session.add(TenantProfile(
                    tenant_id=1, profile_name=pname, enabled=True))
        db.session.commit()
    yield


@pytest.fixture(autouse=True)
def _disable_rate_limits(monkeypatch):
    """Bypass rate limiter so tests can exercise login paths freely."""
    monkeypatch.setattr(app_module, "_rate_limit", lambda *a, **k: True)


@pytest.fixture
def client(flask_app):
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def pytest_sessionfinish(session, exitstatus):
    try:
        os.unlink(_tmp_db.name)
    except Exception:
        pass
