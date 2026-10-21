# simon-freshwater tests

Isolated pytest suite. Runs against a temp SQLite DB — never touches
production. Scheduler is disabled via D1_BACKUP_ENABLED=false, which
short-circuits start_scheduler() before any worker starts (see
backend/reminders.py::start_scheduler).

## Setup (dev only)

    pip install pytest

Do NOT add pytest to backend/requirements.txt. Production should not
carry test deps.

## Run

    cd backend
    python -m pytest tests/ -v

## Coverage

- test_auth.py    — login paths, single vs both profile routing,
                    admin vs employee chooser, R5 pending-flag lifecycle
- test_guard.py   — employee path restrictions, permission gates,
                    Phase C defense-in-depth (billing profile required
                    for new_customer/terminate), HTML redirect fallback
- test_team.py    — team create, self-delete block, missing-target
                    handling, permission updates applied next request

## Safety guarantees

1. conftest.py sets DATABASE_URL to a temp file BEFORE importing app
2. db.create_all() (import-time) therefore only touches the temp DB
3. D1_BACKUP_ENABLED=false -> scheduler returns None at first line
4. RENDER env popped, so production SECRET_KEY hardening is skipped
5. Temp DB is deleted after session (pytest_sessionfinish)

## Adding tests

Use helpers:

    from helpers import make_admin, login
    make_admin("some_user", role="employee", profiles='["billing"]')
    login(client, "some_user")

make_admin returns the user id — useful for delete/permission calls.
