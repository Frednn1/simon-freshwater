# Changelog

All notable changes to **simon-freshwater** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project does not yet follow strict SemVer.

## [Unreleased]

_nothing yet_

## [1.0.0] — 2026-10-22

Full 7-phase upgrade delivery plus follow-on hardening (R4/R5).
All changes staged-tested and merged to `main`. No security
infrastructure was weakened; no new attack surface was introduced.

### Step 1 — Team delete + share-URL removal · `ebb3ff3`

- Removed the "Share this login URL" block from the Team Members card
  — employees now reach the correct landing via profile assignment
  (Step 4), not a hand-copied URL.
- Added a permanent **Delete** button beside Deactivate/Reactivate on
  every team row. Mirrors the consumer-delete safety pattern:
  rate-limited, tenant-scoped, blocks self-delete, blocks super-admin,
  refuses to delete the last active admin. FK cleanup nulls
  `admin_audit_log.admin_id` and deletes `admin_otp` / `password_reset_tokens`
  rows atomically before the user row.

### Steps 2+3 — Employee tab permissions · `b915fcf`

- New `admin_users.permissions_json` column (migration is idempotent,
  backfills existing employees to pre-Step-2 behaviour).
- Extended `_before_request_employee_path_guard` with a permission gate
  that runs **before** the legacy allowlist — a denied tab returns
  `403` (API) or `302 → home` (HTML browser), never a silent bypass.
- Admin UI: Permissions button per employee row opens a modal with
  checkboxes for Settings / Analytics / Reports / Bulk SMS / M-Pesa.
- Employee UI: disallowed tabs are hidden from the header + drawer.
  Admin UI is provably unaffected (`role === 'employee'` guard).

### Step 4 — Employee profile persistence · `1ec20f5` + `59b1f92`

- New `admin_users.profiles_json` column — employee profile assignment
  stored as a JSON array. Backfill for existing employees = `["dispensing"]`
  (matches pre-Step-4 hardcoded redirect).
- Employees auto-route on login: single-profile → straight to that home
  (no chooser); both-profile → chooser modal must be used.
- Admin UI: "Profile Assignment" section added to both Add Member and
  Permissions modals.
- `59b1f92` fix: employees can now call `POST /api/admin/profile/switch`
  (was blocked by the guard, breaking the chooser). Allowlist addition —
  no privilege widening.

### Step 5 — Admin profile chooser · `8b1d8f8`

- Admin login (and verify-otp) now returns `requires_profile_choice: true`
  plus the available `profiles` array **when the tenant has both billing
  and dispensing enabled**. Reuses the same `showProfileChooser()` UI
  introduced for employees.
- Single-profile tenants — admin goes directly to their home, no chooser.

### Step 6 — Team tab extracted · `151be9d`

- Moved the entire Team Members UI out of `admin_settings.html` into
  a dedicated `admin_team.html` page served at `/admin/team`.
- Added a Team tab just before Settings on the billing home, dispensing
  header, dispensing analytics header, and mobile drawer.
- Employee visits to `/admin/team` are blocked by the guard (not in
  any employee allowlist) — admin-only by construction.
- `admin_settings.html` shrunk from ~36 KB → ~14 KB, retaining only
  Username / Phone / Password / Audit / Backup cards.

### Step 7 — Dispensing header emulation · `8d14565` + `adb3ca8`

- Dispensing pages now mirror the billing header structure:
  brand wrapped in `<a class="brand-link">`, tabs moved to a
  `desktop-only` nav, mobile hamburger + sliding drawer added —
  including Switch to Billing, Analytics, Team, Settings, Logout.
- **Floating header preserved** — `.settings-body` padding unchanged.
- `adb3ca8` fix: on mobile, hamburger top-aligned with brand content
  and header background forced to solid deep blue (removed the
  top-of-screen light gradient).

### Follow-on A — Permissions save no longer stalls · `4b1383c`

- The "Saving…" button on the Permissions modal used to stay stuck
  after a successful save (only the error branch reset it). Fixed with
  a `finally` block plus a defensive reset on every modal open.
- Bug repro'd via manual staging test; a single reload was previously
  the only workaround.

### Follow-on B — Team row buttons modernized · `79f3c23`

- Semantic color palette (blue/orange/green/red), flex alignment,
  hover lift + shadow. Scoped to `admin_team.html` inline `<style>` —
  zero changes to shared `style.css`.

### Follow-on C — new_customer + terminate permissions · `d123297`

- Two new employee permission keys: `new_customer` (POST `/api/admin/consumer`)
  and `terminate` (POST `/api/admin/consumer/<id>/terminate`).
- Defaults = `False` — preserves pre-Step-1 behaviour for existing
  employees.
- **Defense-in-depth:** `_before_request_employee_path_guard` requires
  **both** the permission **and** the billing profile for these two
  keys. A dispensing-only employee who somehow has the permission
  stored as `true` still 403s.
- UI: both checkboxes only rendered in the Permissions modal for
  employees whose `profiles` includes `billing`. Buttons (New Customer,
  Terminate) hidden on billing home for employees without permission.

### Follow-on D — R4 pytest suite · `ed28db9`

- **21 isolated tests** in `backend/tests/` covering:
  - login routing (single vs both profile, admin vs employee chooser)
  - R5 pending-flag lifecycle
  - every permission gate (7 keys, positive + negative)
  - Phase C defense-in-depth (billing-profile requirement)
  - team create / self-delete / missing-target / permission update
- **Zero production impact:** tests run against a temp SQLite DB,
  scheduler disabled via `D1_BACKUP_ENABLED=false`, `pytest` is **not**
  in `backend/requirements.txt`.

### Follow-on E — R5 strict chooser enforcement · `76516a6`

- New session field `pending_profile_choice`, set at login (and
  verify-otp) when `requires_profile_choice = true`.
- Guard blocks **every non-chooser path** until the user picks a
  profile via `POST /api/admin/profile/switch`. Only exceptions:
  static assets, `/tenant/*`, `/admin/login` (re-auth escape hatch),
  `/api/admin/login*`, `/api/admin/whoami`, `/api/admin/logout`,
  `/api/admin/profile/switch`, `/tenant-watchdog.js`.
- **Shrinks** attack surface — no new endpoints, no new session field
  trusted for privilege.

### CI · `79cd960`

- `.github/workflows/tests.yml` runs `pytest tests/ -v` on every push
  and pull request to `main` and `staging`. Python 3.11 (matches
  `render.yaml`). Fails the PR if any of the 21 tests fail.

## Security posture — unchanged

- All authentication, session handling, rate limiting, CSRF (via
  same-site cookies + `HttpOnly`), and tenant scoping preserved.
- `_before_request_employee_path_guard` **extended**, never weakened.
  Every addition is a new restriction (deny-by-default at each new
  gate).
- No new endpoints introduced by R5, R4, or CI.
- No new session fields carry privilege — `pending_profile_choice`
  is a boolean that only triggers deny behaviour.

## Deferred

- **R6** — session-level caching of `permissions_json` / `profiles_json`
  to reduce per-request DB queries. **Not shipped.** Reason: any cache
  introduces a staleness window between an admin's permission change
  and the employee's next request — a security regression. Current
  query cost is 2 indexed SELECTs per gated request; negligible at
  current scale.

## Links

- Architecture overview: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Site navigation guide: `docs/SITE_NAVIGATION_GUIDE.html`
- Test suite: [`backend/tests/README.md`](backend/tests/README.md)
