import os
import re
import json
import secrets
import hmac
import math
import ipaddress
from datetime import datetime, date, timedelta, timezone
from collections import deque
from threading import Lock
from time import time as _time_now

from flask import (
    Flask, request, jsonify, send_from_directory, session, send_file,
    redirect, render_template_string, g,
)
# CORS-SECURITY-FIX-PHASE6: CORS middleware removed — see Phase 6 audit
# finding. The app has no cross-origin browser traffic; leaving CORS enabled
# with credentials reflected any Origin header, which is a CWE-942 misconfig.
# If future cross-origin use is required, restore with an explicit origins list.
from sqlalchemy import text, inspect
from sqlalchemy.exc import IntegrityError, OperationalError

# ── Pillow decompression-bomb guard (C5c) ──
# Cap the maximum pixels Pillow will decode in a single image. Without
# this, a 500 KB crafted PNG can decompress to ~400 MB in memory during
# favicon generation (triggered anonymously via /tenant/favicon/<slug>),
# causing OOM kills on free-tier instances. 16.7 MP = 4096×4096 × 4 bytes
# ≈ 67 MB per decode — safe ceiling for a logo, still allows high-res
# uploads. When this ceiling is exceeded, Pillow raises
# DecompressionBombError at open() time BEFORE allocating pixel memory.
try:
    import PIL.Image as _PILImage
    _PILImage.MAX_IMAGE_PIXELS = 16_777_216
    _PIL_AVAILABLE = True
except Exception:
    _PIL_AVAILABLE = False

from models import (
    db, Consumer, MeterReading, NotificationLog,
    AdminUser, PaymentLog, MpesaLog, SentBillArchive, AdminAuditLog,
    AdminOTP, PasswordResetToken,
    Tenant, TenantProfile, DispenseRate,
    DispenseMeterReading, DispenseSale, DispensePayment,
)
from billing import (
    compute_amount, compute_consumption,
    get_consumer_status, get_reading_bill_status,
)
from sheets import sync_consumer_to_sheet
from reminders import start_scheduler
from notifications import (
    send_sms, send_sms_with_retry, send_whatsapp, send_whatsapp_document,
    build_bill_message, get_available_channels, _payment_info,
)
from admin_auth import (
    create_first_admin, login as admin_login_fn,
    request_password_reset, confirm_password_reset,
    generate_login_otp, verify_login_otp,
)
from werkzeug.security import check_password_hash, generate_password_hash
from receipts import generate_receipt_pdf
from water_bill import generate_water_bill_pdf
from report import generate_report_pdf
from bulk_sms import send_bulk_sms, BULK_SMS_MAX_BATCH
from mpesa import (
    get_mpesa_config, register_c2b_urls, simulate_c2b_payment,
    credentials_for_tenant, register_c2b_urls_for,
)
import archive
import backup
from statement import generate_statement_pdf


BACKEND_DIR   = os.path.dirname(os.path.abspath(__file__))
REPO_DIR      = os.path.dirname(BACKEND_DIR)
FRONTEND_DIR  = os.path.join(REPO_DIR, "frontend")
TEMPLATES_DIR = os.path.join(REPO_DIR, "templates")

NOTIFY_COOLDOWN_SECONDS = int(os.environ.get("NOTIFY_COOLDOWN_SECONDS", "300"))


app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
# (CORS middleware removed — see note above)


# ═══════════════════════════════════════════════
#  TENANTS — request-scoped tenant resolution (Phase 2)
# ═══════════════════════════════════════════════
# Phase 2 installs the plumbing: every request resolves a Tenant and
# stores it in `g.tenant`. Nothing reads g.tenant yet, so behaviour is
# unchanged. The routing strategy (subdomain vs path prefix) is a
# separate decision, deferred to a later phase.
#
# Resolution order:
#   1. `?__tenant=<slug>` query param   — dev/testing
#   2. `X-Tenant-Slug` request header   — API clients
#   3. Fallback to the default tenant   — Simon Fresh Water

_DEFAULT_TENANT_SLUG = "simon-freshwater"

# Path prefixes that never need tenant context (pure static assets)
_STATIC_PATH_EXACT = {
    "/style.css", "/app.js", "/favicon.ico", "/manifest.webmanifest",
    "/favicon-32x32.png", "/favicon-16x16.png",
    "/apple-touch-icon.png", "/icon-192.png", "/icon-512.png",
}
_STATIC_PATH_PREFIXES = ("/images/",)


# ═══════════════════════════════════════════════
#  STEP 2 — Employee tab permissions
# ═══════════════════════════════════════════════
# Effective defaults when permissions_json is NULL: matches the
# pre-Step-2 guard exactly — employees could reach /admin/settings
# and /dispense/analytics, nothing else. Every existing employee
# therefore continues to behave identically until the admin
# explicitly toggles a checkbox.
_EMPLOYEE_PERMISSION_DEFAULTS = {
    "settings":  True,
    "analytics": True,
    "reports":   False,
    "bulk_sms":  False,
    "mpesa":     False,
}


def _employee_permissions(admin_id):
    """Return the effective permission dict for an employee.

    Safe fallback: NULL, parse error, DB error, or missing user ->
    legacy defaults (settings=True, analytics=True, others=False) —
    exactly the pre-Step-2 behaviour. Never raises.
    """
    defaults = dict(_EMPLOYEE_PERMISSION_DEFAULTS)
    if not admin_id:
        return defaults
    try:
        u = AdminUser.query.filter_by(id=admin_id).first()
    except Exception:
        return defaults
    if not u:
        return defaults
    raw = getattr(u, "permissions_json", None)
    if not raw:
        return defaults
    try:
        data = json.loads(raw)
    except Exception:
        return defaults
    if not isinstance(data, dict):
        return defaults
    out = dict(defaults)
    for k in defaults:
        if k in data:
            try:
                out[k] = bool(data[k])
            except Exception:
                pass
    return out


# Permission-controlled paths, mapped to the tab that governs them.
# Ordered; first match wins. Exact vs prefix is explicit so no accidental
# path capture (e.g. /api/admin/settings-v2 will NOT match).
_EMPLOYEE_GATED_PATHS = (
    ("settings",  "exact",  "/admin/settings"),
    ("settings",  "exact",  "/api/admin/settings"),
    ("settings",  "prefix", "/api/admin/settings/"),
    ("analytics", "exact",  "/dispense/analytics"),
    ("analytics", "exact",  "/api/admin/dispense/analytics"),
    ("reports",   "exact",  "/admin/reports"),
    ("reports",   "exact",  "/api/admin/report.pdf"),
    ("reports",   "exact",  "/api/admin/report/preview"),
    ("bulk_sms",  "exact",  "/admin/bulk_sms"),
    ("bulk_sms",  "prefix", "/api/admin/bulk_sms/"),
    ("mpesa",     "exact",  "/admin/mpesa"),
    ("mpesa",     "prefix", "/api/admin/mpesa/"),
)


def _employee_permission_for_path(path):
    """Return the permission key governing `path`, or None if not gated.
    Never raises."""
    if not path:
        return None
    for key, kind, value in _EMPLOYEE_GATED_PATHS:
        if kind == "exact":
            if path == value:
                return key
        elif kind == "prefix":
            if path.startswith(value):
                return key
    return None



def _safe_username_str(raw) -> str:
    """Coerce an arbitrary value to a lowercase username string for
    audit logging. Never raises.

    Phase 8 robustness fix: the login route calls .strip().lower() on
    the raw username when building audit entries. If an API client sends
    a non-string username (int, null, dict), login() rejects it via its
    own type guard, but the audit-logging code downstream would still
    crash (AttributeError or TypeError) → 500. This returns "" for any
    non-string input so the audit block runs safely and the intended
    401 is what the client sees.
    """
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()


def _sanitize_slug(raw: str) -> str:
    """Strip control characters and cap length on a user-supplied tenant
    slug before it goes into audit logs, JSON responses, or a DB query.

    Closes Phase 8 finding 8.6b: CRLF in a reflected slug creates forged
    audit-log entries and violates the API contract of returning only
    safe strings from public endpoints.
    """
    if not raw:
        return ""
    # Remove ASCII control characters (0x00-0x1F, 0x7F) and cap to 60
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", str(raw))
    return cleaned[:60]


def _resolve_tenant_from_request():
    """Return the Tenant for the current request, or None if not found.
    Never raises — a lookup failure falls back to None.

    Rules (this is the security boundary — do not weaken):

      • Authenticated admin → session["__tenant_slug"] is authoritative.
        The URL param is IGNORED so a logged-in admin cannot be moved to
        another tenant by a shared link.

      • Anonymous + ?__tenant=<slug> → use the URL slug for THIS request
        only. Never write to session. Lets /admin/login?__tenant=x render
        tenant x's login page without pinning the visitor to x.

      • Anonymous, no param → default tenant (must be active).
    """
    aid = session.get("admin_id")

    # ── Authenticated admin: session is authoritative ──
    if aid:
        slug = session.get("__tenant_slug") or _DEFAULT_TENANT_SLUG
        try:
            return Tenant.query.filter_by(slug=slug).first()
        except Exception:
            return None

    # ── Anonymous visitor: preview override (super-admin only) ──
    # `?__preview=<slug>` bypasses the session for one request so a
    # super-admin can preview any tenant without leaving their own.
    preview_slug = _sanitize_slug(request.args.get("__preview") or "").strip()
    if preview_slug:
        try:
            return Tenant.query.filter_by(slug=preview_slug).first()
        except Exception:
            return None

    # ── Anonymous visitor — no persistence ──
    url_slug = _sanitize_slug(request.args.get("__tenant") or "").strip()
    if not url_slug:
        url_slug = _sanitize_slug(request.headers.get("X-Tenant-Slug") or "").strip()
    if url_slug:
        try:
            return Tenant.query.filter_by(slug=url_slug).first()
        except Exception:
            return None

    # ── Anonymous visitor, no param — default tenant ──
    # Note: NOT filtered by is_active. The default tenant must always
    # resolve, otherwise g.tenant becomes None and every tenant-scoped
    # query loses context — including the dev portal itself. When the
    # default tenant is inactive, public consumer pages return a 503
    # (see _before_request_inactive_default_503), and its admin login
    # page shows the disabled-UI we already ship.
    try:
        return (Tenant.query
                .filter_by(slug=_DEFAULT_TENANT_SLUG)
                .first())
    except Exception:
        return None


@app.before_request
def _before_request_resolve_tenant():
    p = request.path or ""
    if p in _STATIC_PATH_EXACT or p.startswith(_STATIC_PATH_PREFIXES):
        return
    g.tenant = _resolve_tenant_from_request()


# ── Inactive default tenant: return 503 on consumer-facing paths ──
# Whitelist everything the operator needs to still work (dev portal,
# admin, tenant login, APIs, static assets, logos).
_CONSUMER_GATE_EXEMPT_PREFIXES = (
    "/admin", "/api/admin", "/dev", "/api/tenant", "/api/public",
    "/tenant/logo", "/api/health", "/api/ping",
    "/style.css", "/dev.css", "/tenant-watchdog.js",
    "/favicon", "/manifest", "/apple-touch", "/icon-", "/images",
)


def _render_tenant_unavailable(tenant):
    """Friendly 503 page shown to anonymous visitors when the default
    tenant is inactive. Rendered inline — no template file needed."""
    name = (tenant.org_name if tenant and tenant.org_name else "This service")
    logo = _brand_context().get("brand_logo_url", "/images/SimonW_Logo.png")
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} — Temporarily unavailable</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,-apple-system,Roboto,sans-serif;
        background:#f8f9fa;color:#1c2b36;min-height:100vh;
        display:grid;place-items:center;padding:1.25rem}}
  .card{{max-width:520px;width:100%;background:#fff;border-radius:14px;
        padding:2rem 1.75rem;box-shadow:0 4px 24px rgba(13,71,161,.10);
        text-align:center}}
  .logo{{max-width:120px;max-height:120px;margin:0 auto 1rem;display:block}}
  h1{{font-size:1.25rem;color:#0d47a1;margin-bottom:.6rem}}
  p{{color:#455a64;line-height:1.6;font-size:.95rem;margin-bottom:.5rem}}
  .pill{{display:inline-block;background:#fff3e0;color:#6d4c00;
        border:1px solid #ffb74d;border-radius:999px;
        padding:.25rem .8rem;font-size:.8rem;font-weight:600;
        letter-spacing:.3px;text-transform:uppercase;margin-bottom:1rem}}
  a{{color:#0d47a1;text-decoration:none;font-weight:600}}
</style></head><body>
  <div class="card">
    <img class="logo" src="{logo}" alt="{name}">
    <div class="pill">Temporarily unavailable</div>
    <h1>{name} is offline</h1>
    <p>This service is temporarily unavailable. Please try again later.</p>
    <p>Please contact your administrator for support.</p>
  </div>
</body></html>"""


@app.before_request
def _before_request_employee_path_guard():
    """Restrict employees (user_role="employee") to the dispensing surface only."""
    if not session.get("admin_id"):
        return
    if session.get("user_role", "admin") != "employee":
        return

    p = request.path or ""
    if p in _STATIC_PATH_EXACT or p.startswith(_STATIC_PATH_PREFIXES):
        return

    # Step 2: permission gate — must run BEFORE ALLOWED_EXACT / ALLOWED_PREFIXES
    # below (those whitelists are broad enough to bypass a permission denial).
    _perm_key = _employee_permission_for_path(p)
    if _perm_key:
        if not _employee_permissions(session.get("admin_id")).get(_perm_key, False):
            _is_api = p.startswith("/api/")
            _wants_html = "text/html" in (request.headers.get("Accept") or "")
            if _is_api or not _wants_html:
                return jsonify({
                    "error": "Forbidden. This section is not enabled for your account."
                }), 403
            return redirect("/dispense")
        # Permission granted — allow, bypassing the whitelist below.
        return

    ALLOWED_EXACT = {
        "/dispense",
        "/admin/settings",
        "/api/admin/whoami",
        "/api/admin/logout",
        "/api/admin/settings",
        "/api/admin/settings/username",
        "/api/admin/settings/phone",
        "/api/admin/settings/password",
        "/api/tenant/is-active",
        "/manifest.webmanifest",
    }
    ALLOWED_PREFIXES = (
        "/dispense/",
        "/api/admin/dispense/",
        "/api/public/",
        "/tenant/favicon/",
        "/tenant/logo/",
        "/dev.css",
        "/style.css",
        "/tenant-watchdog.js",
        "/images/",
    )
    if p in ALLOWED_EXACT:
        return
    for pre in ALLOWED_PREFIXES:
        if p.startswith(pre):
            return

    # Differentiate browser navigation from API clients:
    #   - Browser hits / → 302 → /dispense (no JSON-on-screen)
    #   - fetch() to /api/* → 403 JSON (so clients see the real reason)
    is_api = p.startswith("/api/")
    wants_html = "text/html" in (request.headers.get("Accept") or "")
    if is_api or not wants_html:
        return jsonify({
            "error": "Forbidden. Employees can only access the dispensing portal."
        }), 403
    return redirect("/dispense")


@app.before_request
def _before_request_inactive_default_503():
    p = request.path or ""
    # Static + assets: never gate
    if p in _STATIC_PATH_EXACT or p.startswith(_STATIC_PATH_PREFIXES):
        return
    # Whitelisted prefixes: never gate
    for prefix in _CONSUMER_GATE_EXEMPT_PREFIXES:
        if p == prefix or p.startswith(prefix):
            return
    # Only the default tenant triggers the gate
    try:
        t = getattr(g, "tenant", None)
    except Exception:
        return
    if t is None or t.slug != _DEFAULT_TENANT_SLUG:
        return
    if t.is_active:
        return
    # Logged-in admins bypass the gate (dev / debug)
    if session.get("admin_id"):
        return
    return _render_tenant_unavailable(t), 503


# ═══════════════════════════════════════════════
#  TENANTS — explicit query scoping (Phase 3b)
# ═══════════════════════════════════════════════
# The sqlalchemy-cloudflare-d1 dialect does not honour the ORM
# `with_loader_criteria` event, so automatic scoping is impossible.
# Every route that reads tenant data calls this helper at its entry
# point instead. `first_or_404()` respects the filter (unlike
# `get_or_404()`, which bypasses it via session.get).

def _tenant_query(model):
    """Base query on `model` scoped to the current request's tenant.

    - Resolved tenant  -> WHERE tenant_id = <tid>
    - Unknown tenant   -> WHERE tenant_id = -1 (returns nothing)
    - No request ctx   -> unfiltered (background tasks, boot)
    """
    try:
        t = g.tenant
    except (RuntimeError, AttributeError):
        return model.query
    tid = t.id if t is not None else -1
    return model.query.filter_by(tenant_id=tid)


def _write_tenant_id():
    """Tenant id to stamp on a new row.
    -1 means no tenant resolved — the caller should reject the request
    rather than accidentally writing into tenant 1."""
    t = getattr(g, "tenant", None)
    return t.id if t is not None else -1


def _audit_tenant_id():
    """Tenant id to stamp on an audit row.
    - 1 when there is no request context (boot / background) — the default
    - <tid> when the request resolved a tenant
    - -1 when the request context exists but the tenant did not resolve
      (audit writes should then be flagged rather than attributed to 1)
    """
    try:
        t = g.tenant
    except (RuntimeError, AttributeError):
        return 1
    return t.id if t is not None else -1


def _tenant_sheet_name():
    """Google Sheet name to use for the current tenant.
    - None → use the module default (Simon Fresh Water's existing sheet)
    - "<slug> - Consumer Records" → separate sheet for other tenants
    """
    try:
        t = g.tenant
    except (RuntimeError, AttributeError):
        return None
    if t is None or t.slug == "simon-freshwater":
        return None
    return f"{t.slug} - Consumer Records"


def _tenant_slug():
    """Current tenant slug for R2 key prefixing.
    Returns None when there is no request context (background / boot)."""
    try:
        t = g.tenant
    except (RuntimeError, AttributeError):
        return None
    return t.slug if t is not None else None


def _brand_context():
    """Brand fields for PDF snapshots and template substitution.
    Uses the current request's tenant, or falls back to Simon Fresh Water
    when there is no request context (background tasks, boot).
    """
    t = getattr(g, "tenant", None)
    if t is None:
        try:
            t = Tenant.query.filter_by(slug="simon-freshwater").first()
        except Exception:
            t = None

    name = (t.org_name if t and t.org_name else "Simon Fresh Water")
    addr = (t.address if t and t.address else "Kikuyu, Kenya")
    _lk = (t.logo_key if t and t.logo_key else "").strip()
    if _lk.startswith("logos/"):
        logo = "/tenant/logo/" + (t.slug if t else "")
    else:
        logo = _lk or "images/SimonW_Logo.png"
    if not logo.startswith("/"):
        logo = "/" + logo

    _city = (addr.split(",")[0].strip() if addr else "Kikuyu")

    # PHASE10-BASE-URL: absolute base URL for Open Graph / social preview
    # tags. Falls back to production URL when called from a background task
    # or boot sequence where there is no Flask request context.
    try:
        _base_url = request.host_url.rstrip("/")
    except Exception:
        _base_url = "https://freshwatersolutions.onrender.com"

    return {
        "brand_name":       name,
        "brand_name_upper": name.upper(),
        "brand_tagline":    (t.tagline if t and t.tagline else "Pure, Reliable & Sustainable Water Supply"),
        "brand_address":    addr,
        "brand_city":       _city,
        "brand_city_upper": _city.upper(),
        "brand_phone":      (t.phone if t and t.phone else "+254 700 719311"),
        "brand_email":      (t.email if t and t.email else "simonchege84@gmail.com"),
        "brand_logo_url":   logo,
        "brand_favicon_url": ("/tenant/favicon/" + t.slug
                              if t and t.logo_key and t.logo_key.startswith("logos/")
                              else "/tenant/favicon/" + (t.slug if t and t.slug else "simon-freshwater")),
        "brand_logo_r2_key": _lk if _lk.startswith("logos/") else "",
        "brand_base_url":   _base_url,
        # Tenant slug — used by admin pages to stamp <body data-tenant-slug>,
        # which the tenant watchdog reads to know which tenant to check.
        "tenant_slug":      (t.slug if t and t.slug else ""),
    }


def _serve_template(filename):
    """Serve a template HTML file (from TEMPLATES_DIR) RAW — tokens intact.
    Used by the manual Chrome -> Save as PDF workflow. The {{placeholder}}
    text must survive into the printed PDF so PyMuPDF can fill it at send
    time from the snapshot. Do NOT substitute here.
    """
    path = os.path.join(TEMPLATES_DIR, filename)
    if not os.path.exists(path):
        return f"Template not found: {filename}", 404

    with open(path, "r", encoding="utf-8") as f:
        html = f.read()

    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


def _serve_frontend(filename):
    """Serve a frontend HTML file with tenant-brand substitution.

    Rewritten to use _brand_context() as the single source of truth, so
    any token added to that helper is automatically substituted on every
    frontend page. For tenant 1 (Simon Fresh Water), the substituted
    values match the original hardcoded strings, so the rendered output
    is unchanged.
    """
    path = os.path.join(FRONTEND_DIR, filename)
    if not os.path.exists(path):
        return f"Template not found: {filename}", 404
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()

    for token, value in _brand_context().items():
        html = html.replace("{{" + token + "}}", value)

    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///simon_freshwater.db")

# ─── Cloudflare D1 credentials (build the URL from parts if not already set) ───
CF_ACCOUNT_ID    = os.environ.get("CF_ACCOUNT_ID", "")
CF_D1_DATABASE_ID = os.environ.get("CF_D1_DATABASE_ID", "")
CF_API_TOKEN     = os.environ.get("CF_API_TOKEN", "")

# If a cloudflare_d1:// URL is provided, use it as-is.
# Otherwise, if the three CF_* values are set, build the D1 URL.
if not DATABASE_URL.startswith("cloudflare_d1://"):
    if CF_ACCOUNT_ID and CF_D1_DATABASE_ID and CF_API_TOKEN:
        DATABASE_URL = f"cloudflare_d1://{CF_ACCOUNT_ID}:{CF_API_TOKEN}@{CF_D1_DATABASE_ID}"
        app.logger.info("[DB] Using Cloudflare D1 database")
    elif DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        app.logger.info("[DB] Using PostgreSQL database")

# SQLAlchemy 2.x needs the dialect imported before use for D1
if DATABASE_URL.startswith("cloudflare_d1://"):
    import sqlalchemy_cloudflare_d1  # noqa: F401

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# ── SECRET_KEY hardening (C5a) ──
# On Render, refuse to boot unless SECRET_KEY is set to a strong value.
# Without this guard a missing/weak env var silently falls back to a
# public string (visible on GitHub) — which would let anyone forge any
# session cookie. Loud boot failure beats silent auth bypass.
_secret_key = os.environ.get("SECRET_KEY", "").strip()
_on_render  = bool(os.environ.get("RENDER"))
if _on_render and (not _secret_key or _secret_key == "dev-change-me" or len(_secret_key) < 32):
    raise RuntimeError(
        "[BOOT] SECRET_KEY env var must be set to a strong random value "
        "(minimum 32 characters) on Render. Refusing to start with an "
        "insecure fallback — this protects against silent auth bypass."
    )
app.config["SECRET_KEY"] = _secret_key or "dev-change-me"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
SESSION_IDLE_MINUTES = 30
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))

# Connection-pool options only apply to real socket-based DBs (PostgreSQL).
# D1 uses stateless HTTP requests — no pool to manage.
if DATABASE_URL.startswith("postgres"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 180,
        "pool_size": 5,
        "max_overflow": 5,
        "pool_timeout": 30,
    }

db.init_app(app)


# ═══════════════════════════════════════════════
#  SECURITY HEADERS
# ═══════════════════════════════════════════════

@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(self)")

    # HSTS-PHASE8-HARDENING: force browsers to use HTTPS for 1 year.
    # Sent on every response — browsers ignore it over plain HTTP per spec.
    resp.headers.setdefault(
        "Strict-Transport-Security",
        "max-age=31536000; includeSubDomains",
    )

    # CSP-PHASE8-HARDENING: defense-in-depth against XSS/injection.
    # 'unsafe-inline' is required because pages use inline <script>,
    # inline <style> (admin_login only), and 36 inline event handlers
    # (onclick= etc). All actual <script src>/<link href> are same-origin.
    # connect-src / img-src allow Cloudflare R2 (presigned direct uploads
    # and tenant logos served via /tenant/logo/<slug>).
    #
    # CSP-IMAGES-PHASE9: per W3C spec, CSP applies to documents and workers
    # — NOT to images, CSS, JS, PDFs, or fonts. Chrome's bookmark-favicon
    # fetch path can silently fail when it sees CSP on an image response
    # (falls back to the origin letter "O"). Sending CSP only on HTML
    # responses closes this without weakening protection: the CSP on the
    # parent HTML document still governs which resources may be loaded.
    _is_doc = (resp.mimetype or "").startswith("text/html")
    if _is_doc:
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https://*.r2.cloudflarestorage.com; "
            "connect-src 'self' https://*.r2.cloudflarestorage.com; "
            "font-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "object-src 'none'",
        )

    return resp


# ═══════════════════════════════════════════════
#  RATE LIMITER
# ═══════════════════════════════════════════════

_rate_buckets = {}
_rate_lock = Lock()
_rate_db_lock = Lock()


def _client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _ip_in_allowlist(ip_str: str, allowlist_csv: str) -> bool:
    """
    Return True if `ip_str` is in the comma-separated CIDR list.
    Fail-open when the allowlist is empty (safe for initial rollout).
    """
    if not allowlist_csv or not allowlist_csv.strip():
        return True
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for cidr in allowlist_csv.split(","):
        cidr = cidr.strip()
        if not cidr:
            continue
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def _mpesa_ip_ok() -> bool:
    """True if the current request's client IP is allowed to call M-Pesa endpoints."""
    allowed = os.environ.get("MPESA_ALLOWED_IPS", "").strip()
    return _ip_in_allowlist(_client_ip(), allowed)


def _rate_limit_memory(scope: str, limit: int, window: int) -> bool:
    """Fast, per-process check. Early exit before hitting the DB."""
    key = f"{scope}:{_client_ip()}"
    now = _time_now()
    with _rate_lock:
        dq = _rate_buckets.setdefault(key, deque())
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
    return True


def _rate_limit_db(scope: str, limit: int, window: int) -> bool:
    """
    Authoritative, cross-worker rate limit backed by Cloudflare D1.
    Returns True if the request is allowed.

    Fail-open on DB error (never locks users out due to a DB hiccup).
    """
    from datetime import datetime, timedelta
    from models import RateLimitBucket

    key = f"{scope}:{_client_ip()}"
    now = datetime.utcnow()
    window_start_cutoff = now - timedelta(seconds=window)

    with _rate_db_lock:
        try:
            bucket = RateLimitBucket.query.filter_by(bucket_key=key).first()

            # No bucket → start fresh
            if bucket is None:
                bucket = RateLimitBucket(
                    bucket_key=key,
                    count=1,
                    window_start=now,
                    updated_at=now,
                )
                db.session.add(bucket)
                db.session.commit()
                return True

            # Window expired → reset
            if bucket.window_start < window_start_cutoff:
                bucket.count = 1
                bucket.window_start = now
                bucket.updated_at = now
                db.session.commit()
                return True

            # Within window — enforce limit
            if bucket.count >= limit:
                return False

            bucket.count += 1
            bucket.updated_at = now
            db.session.commit()
            return True

        except Exception as e:
            app.logger.warning(f"[RateLimit] DB error (fail-open): {e}")
            db.session.rollback()
            return True


def _rate_limit(scope: str, limit: int, window: int) -> bool:
    """
    Hybrid rate limit:
      1. Fast in-memory check — rejects immediately if this worker
         already knows the limit is exceeded.
      2. DB-backed check — authoritative across all workers.
    """
    if not _rate_limit_memory(scope, limit, window):
        return False
    return _rate_limit_db(scope, limit, window)


def _too_many(retry_after: int = 60):
    r = jsonify({"error": "Too many requests. Please slow down and try again shortly."})
    r.status_code = 429
    r.headers["Retry-After"] = str(retry_after)
    return r


# ═══════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════

def _previous_reading(consumer_id, exclude_id=None):
    q = MeterReading.query.filter_by(consumer_id=consumer_id)
    if exclude_id is not None:
        q = q.filter(MeterReading.id != exclude_id)
    return q.order_by(MeterReading.reading_date.desc(),
                      MeterReading.id.desc()).first()


def _all_readings(consumer_id):
    return (MeterReading.query
            .filter_by(consumer_id=consumer_id)
            .order_by(MeterReading.reading_date.desc(),
                      MeterReading.id.desc()).all())


def _total_uncleared_consumption(consumer) -> float:
    """
    Sum of consumption (M³) across readings whose bill is NOT fully paid.
    Cleared bills are excluded.

    Consumption for each reading = reading − previous reading
    (for the very first reading: reading − initial_meter_reading).
    """
    readings_asc = list(reversed(_all_readings(consumer.id)))  # oldest → newest
    initial = float(consumer.meter_initial_reading_m3 or 0.0)
    total = 0.0
    prev_m3 = None
    for r in readings_asc:
        cons = compute_consumption(r.reading_m3, prev_m3, initial)
        balance = (r.amount_kes or 0.0) - (r.amount_paid or 0.0)
        if balance > 0:
            total += cons
        prev_m3 = r.reading_m3
    return round(total, 2)


def _clear_session_preserve_tenant():
    """Clear the session but keep __tenant_slug so the next request
    still resolves the same tenant. Used by any code path that needs
    to log out the admin (or fail-safe) without losing tenant context.
    Without this, the browser would fall back to Simon Fresh Water's
    login page after every logout/timeout/failed-admin check.
    """
    _slug = session.get("__tenant_slug")
    session.clear()
    if _slug:
        session["__tenant_slug"] = _slug


def _current_admin():
    """Return the authenticated AdminUser or None. Enforces idle timeout."""
    aid = session.get("admin_id")
    if not aid:
        return None

    # Idle-timeout enforcement (server-side — cannot be bypassed by client)
    last = session.get("last_activity")
    now = datetime.utcnow()
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
        except (ValueError, TypeError):
            _clear_session_preserve_tenant()
            return None
        if now - last_dt > timedelta(minutes=SESSION_IDLE_MINUTES):
            _uname = session.get("admin_username")
            _aid = session.get("admin_id")
            _clear_session_preserve_tenant()
            if _uname:
                _audit("session_timeout",
                       _admin_id=_aid, _admin_username=_uname)
            return None

    # Lookup by ID alone — the ID is a primary key and does not depend
    # on the session's tenant slug. The tenant-scoped query was the
    # second half of the earlier login bug: a stale slug made the
    # admin row appear "not found", forcing a false logout.
    a = AdminUser.query.filter_by(id=aid).first()
    if not a or not a.is_active:
        _clear_session_preserve_tenant()
        return None

    # Tenant-active check: if the admin's own tenant has been deactivated,
    # treat the admin as unauthenticated. This is the server-side half of
    # the auto-logout — the client poller is 15s, but this fires on the
    # very first API call, so no stale action can slip through.
    if a.tenant_id:
        _admin_tenant_for_check = Tenant.query.get(a.tenant_id)
        if _admin_tenant_for_check is not None and not _admin_tenant_for_check.is_active:
            _uname = session.get("admin_username")
            _aid   = session.get("admin_id")
            _clear_session_preserve_tenant()
            if _uname:
                _audit("tenant_deactivated_logout",
                       _admin_id=_aid, _admin_username=_uname)
            return None

    # Password-change invalidator: a session created before the admin's
    # most recent password change is no longer valid. This closes the
    # "stolen session survives a password reset" gap. Sessions that
    # predate this feature (no login_at in the session) remain valid
    # until their normal idle timeout — deliberate, so we don't force-
    # logout existing sessions during rollout.
    if getattr(a, "password_changed_at", None) and session.get("login_at"):
        try:
            _login_at = datetime.fromisoformat(session["login_at"])
            if _login_at < a.password_changed_at:
                _uname = session.get("admin_username")
                _aid   = session.get("admin_id")
                _clear_session_preserve_tenant()
                if _uname:
                    _audit("session_invalidated_by_password_change",
                           _admin_id=_aid, _admin_username=_uname)
                return None
        except (ValueError, TypeError):
            pass

    # Self-heal: session slug must equal the admin's own tenant slug.
    # If they disagree (stale cookie, tampering, tenant slug changed),
    # silently correct the session rather than locking the admin out.
    if a.tenant_id:
        _admin_tenant = Tenant.query.get(a.tenant_id)
        if _admin_tenant:
            _sess_slug = session.get("__tenant_slug")
            if _sess_slug != _admin_tenant.slug:
                session["__tenant_slug"] = _admin_tenant.slug

    # Roll the activity timestamp forward on each authenticated request
    session["last_activity"] = now.isoformat()

    # Lazy D1 backup trigger — first successful auth after the scheduled
    # hour each day. Runs in a daemon thread; internally guarded so it
    # fires at most once per process per day. Never blocks the request.
    try:
        from d1_backup import maybe_run_lazy_backup
        maybe_run_lazy_backup()
    except Exception:
        pass

    return a


def _require_admin():
    if _current_admin():
        return None
    secret = request.headers.get("X-Admin-Secret", "")
    expected = os.environ.get("ADMIN_SEED_SECRET", "")
    if expected and secret and secret == expected:
        return None
    return jsonify({"error": "Unauthorized. Please log in."}), 401


DEV_SESSION_IDLE_MINUTES = 60


def _dev_session_ok():
    """True if the current browser session is an authenticated dev-portal
    session. Enforces its own idle timeout. Never touches admin_id /
    last_activity (which belong to the tenant-admin session)."""
    if not session.get("dev_authenticated"):
        return False
    last = session.get("dev_last_activity")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
        except (ValueError, TypeError):
            session.pop("dev_authenticated", None)
            session.pop("dev_last_activity", None)
            return False
        if datetime.utcnow() - last_dt > timedelta(minutes=DEV_SESSION_IDLE_MINUTES):
            session.pop("dev_authenticated", None)
            session.pop("dev_last_activity", None)
            return False
    session["dev_last_activity"] = datetime.utcnow().isoformat()
    return True


def _require_dev_portal():
    """Return an error response unless the caller has a dev-portal session."""
    if _dev_session_ok():
        return None
    return jsonify({"error": "Unauthorized. Dev portal login required."}), 401


def _require_super_admin():
    """Return an error response unless the caller is either:
      - a tenant admin with is_super=True (today's behaviour), or
      - an authenticated dev-portal session (Phase C addition).

    Non-super tenant admins receive 403; unauthenticated callers 401.
    Phase E will narrow this to dev-portal-only after tenant admins are
    demoted in Phase D."""
    if _dev_session_ok():
        return None
    admin = _current_admin()
    if admin is None:
        return jsonify({"error": "Unauthorized. Please log in."}), 401
    if not getattr(admin, "is_super", False):
        return jsonify({"error": "Forbidden. Super-admin only."}), 403
    return None


def _safe_filename(name: str) -> str:
    """Sanitize a consumer name for a download filename.
    'Sheila Wangu' -> 'SheilaWangu'. ASCII letters + digits only.
    Returns 'Consumer' if the input is empty or has no usable characters."""
    if not name:
        return "Consumer"
    cleaned = "".join(c for c in name if c.isascii() and c.isalnum())
    return cleaned or "Consumer"


def _fmt_money(n):
    try:
        return f"{float(n or 0):,.2f}"
    except (ValueError, TypeError):
        return "0.00"


def _fmt_date(d):
    if isinstance(d, str):
        return d
    return d.strftime("%d %b %Y") if d else "—"


# ─── Nairobi timezone helpers ───
# Africa/Nairobi is UTC+3 year-round (no DST). Fixed offset avoids the
# tzdata dependency that ZoneInfo needs on minimal Linux images.
_NAIROBI_TZ = timezone(timedelta(hours=3), name="Africa/Nairobi")


def _nairobi_now() -> datetime:
    """Current time in Africa/Nairobi (tz-aware)."""
    return datetime.now(_NAIROBI_TZ)


def _nairobi_today() -> date:
    """Today's date in Africa/Nairobi."""
    return _nairobi_now().date()


def _as_nairobi(dt):
    """Convert a naive (assumed UTC) or aware datetime to Africa/Nairobi.
    None → current Nairobi time."""
    if dt is None:
        return _nairobi_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_NAIROBI_TZ)


# ═══════════════════════════════════════════════
#  ADMIN AUDIT TRAIL helper
# ═══════════════════════════════════════════════

AUDIT_RETENTION_DAYS     = 7
AUDIT_PURGE_INTERVAL_SEC = 12 * 3600
_audit_last_purge = None
_audit_lock = Lock()


def _audit(action, target_type=None, target_id=None, target_label=None,
           detail=None, _admin_id=None, _admin_username=None):
    """Write one audit row. NEVER raises — any failure is logged and swallowed.
    Call at the END of a handler, after its own db.session.commit(), so that
    our commit does not flush someone else's pending work.
    """
    global _audit_last_purge
    try:
        if _admin_username is not None or _admin_id is not None:
            aid = _admin_id
            uname = _admin_username
        else:
            aid = session.get("admin_id")
            uname = session.get("admin_username")

        def _clean(v, n):
            if v is None:
                return None
            v = str(v).replace("\x00", "")
            return v[:n]

        _tid = _audit_tenant_id()
        row = AdminAuditLog(
            tenant_id=_tid,
            admin_id=aid,
            admin_username=_clean(uname, 60) or "unknown",
            action=_clean(action, 40) or "unknown",
            target_type=_clean(target_type, 30),
            target_id=target_id,
            target_label=_clean(target_label, 120),
            detail=_clean(detail, 500),
            ip_address=_clean(_client_ip(), 45),
            user_agent=_clean(request.headers.get("User-Agent", ""), 200),
        )
        db.session.add(row)
        db.session.commit()

        # Opportunistic purge — at most once per AUDIT_PURGE_INTERVAL_SEC
        with _audit_lock:
            now = datetime.utcnow()
            if (_audit_last_purge is None or
                (now - _audit_last_purge).total_seconds() > AUDIT_PURGE_INTERVAL_SEC):
                _audit_last_purge = now
                _audit_purge_run(tenant_id=_tid)
    except Exception:
        try:
            app.logger.exception("[Audit] insert failed")
            db.session.rollback()
        except Exception:
            pass


def _audit_purge_run(tenant_id=None):
    """Delete audit rows older than AUDIT_RETENTION_DAYS for one tenant.
    Never raises. tenant_id must be a positive int — otherwise returns 0
    (fails closed; refuses to purge across tenants)."""
    if tenant_id is None or tenant_id < 1:
        return 0
    try:
        cutoff = datetime.utcnow() - timedelta(days=AUDIT_RETENTION_DAYS)
        deleted = (AdminAuditLog.query
                   .filter(AdminAuditLog.tenant_id == tenant_id)
                   .filter(AdminAuditLog.created_at < cutoff)
                   .delete(synchronize_session=False))
        db.session.commit()
        if deleted:
            app.logger.info(
                f"[Audit] purge removed {deleted} rows for tenant "
                f"{tenant_id} older than {AUDIT_RETENTION_DAYS} days"
            )
        return deleted
    except Exception:
        try:
            app.logger.exception("[Audit] purge failed")
            db.session.rollback()
        except Exception:
            pass
        return 0


def _ensure_all_columns():
    """Idempotent migration for consumers + payment_log.
    Uses dialect-aware SQL types so it works on both SQLite (D1) and PostgreSQL."""
    insp = inspect(db.engine)
    dialect = db.engine.dialect.name  # 'sqlite' or 'postgresql'
    tables = set(insp.get_table_names())

    if dialect == "sqlite":
        REAL_T = "REAL"
        TIME_T = "TEXT"
    else:
        REAL_T = "DOUBLE PRECISION"
        TIME_T = "TIMESTAMP"

    if "consumers" in tables:
        existing = {c["name"] for c in insp.get_columns("consumers")}
        alters = []
        if "latitude" not in existing:
            alters.append(f"ADD COLUMN latitude {REAL_T} NULL")
        if "longitude" not in existing:
            alters.append(f"ADD COLUMN longitude {REAL_T} NULL")
        if "is_active" not in existing:
            if dialect == "sqlite":
                alters.append("ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
            else:
                alters.append("ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE")
        if "terminated_at" not in existing:
            alters.append(f"ADD COLUMN terminated_at {TIME_T} NULL")
        if "meter_initial_reading_m3" not in existing:
            alters.append(f"ADD COLUMN meter_initial_reading_m3 {REAL_T} NOT NULL DEFAULT 0")
        if "whatsapp_opt_in" not in existing:
            if dialect == "sqlite":
                alters.append("ADD COLUMN whatsapp_opt_in INTEGER NOT NULL DEFAULT 0")
            else:
                alters.append("ADD COLUMN whatsapp_opt_in BOOLEAN NOT NULL DEFAULT FALSE")
        if "alt_contact" not in existing:
            alters.append("ADD COLUMN alt_contact VARCHAR(20) NULL")
        if "alt_whatsapp_opt_in" not in existing:
            if dialect == "sqlite":
                alters.append("ADD COLUMN alt_whatsapp_opt_in INTEGER NOT NULL DEFAULT 0")
            else:
                alters.append("ADD COLUMN alt_whatsapp_opt_in BOOLEAN NOT NULL DEFAULT FALSE")
        for alt in alters:
            db.session.execute(text(f"ALTER TABLE consumers {alt}"))
        if alters:
            db.session.commit()
            app.logger.info(f"[Migration] Applied {len(alters)} column(s) to consumers")

    if "tenants" in tables:
        existing = {c["name"] for c in insp.get_columns("tenants")}
        t_alters = []
        if "mpesa_shortcode" not in existing:
            t_alters.append("ADD COLUMN mpesa_shortcode VARCHAR(30) NULL")
        if "mpesa_env" not in existing:
            t_alters.append("ADD COLUMN mpesa_env VARCHAR(20) DEFAULT 'sandbox'")
        # Dispensing profile payment identifiers (Phase A)
        if "dispensing_paybill" not in existing:
            t_alters.append("ADD COLUMN dispensing_paybill VARCHAR(30) NULL")
        if "dispensing_account" not in existing:
            t_alters.append("ADD COLUMN dispensing_account VARCHAR(60) NULL")
        for alt in t_alters:
            db.session.execute(text(f"ALTER TABLE tenants {alt}"))
        if t_alters:
            db.session.commit()
            app.logger.info(f"[Migration] Applied {len(t_alters)} column(s) to tenants")

    if "admin_users" in tables:
        existing = {c["name"] for c in insp.get_columns("admin_users")}
        a_alters = []
        if "password_changed_at" not in existing:
            a_alters.append(f"ADD COLUMN password_changed_at {TIME_T} NULL")
        if "user_role" not in existing:
            a_alters.append("ADD COLUMN user_role VARCHAR(20) NOT NULL DEFAULT 'admin'")
        if "permissions_json" not in existing:
            a_alters.append("ADD COLUMN permissions_json TEXT NULL")
        for alt in a_alters:
            db.session.execute(text(f"ALTER TABLE admin_users {alt}"))
        if a_alters:
            db.session.commit()
            app.logger.info(f"[Migration] Applied {len(a_alters)} column(s) to admin_users")

    if "payment_log" in tables:
        existing = {c["name"] for c in insp.get_columns("payment_log")}
        if "receipt_json" not in existing:
            db.session.execute(text("ALTER TABLE payment_log ADD COLUMN receipt_json TEXT NULL"))
            db.session.commit()
            app.logger.info("[Migration] Added receipt_json to payment_log")

    # Phase F.5: add void columns to dispense_sales.
    if "dispense_sales" in tables:
        existing = {c["name"] for c in insp.get_columns("dispense_sales")}
        ds_alters = []
        if "voided" not in existing:
            if dialect == "sqlite":
                ds_alters.append("ADD COLUMN voided INTEGER NOT NULL DEFAULT 0")
            else:
                ds_alters.append("ADD COLUMN voided BOOLEAN NOT NULL DEFAULT FALSE")
        if "voided_at" not in existing:
            ds_alters.append(f"ADD COLUMN voided_at {TIME_T} NULL")
        if "voided_by" not in existing:
            ds_alters.append("ADD COLUMN voided_by VARCHAR(60) NULL")
        for alt in ds_alters:
            db.session.execute(text(f"ALTER TABLE dispense_sales {alt}"))
        if ds_alters:
            db.session.commit()
            app.logger.info(f"[Migration] Applied {len(ds_alters)} column(s) to dispense_sales")

    if "tenants" in tables:
        existing = {c["name"] for c in insp.get_columns("tenants")}
        if "location" not in existing:
            db.session.execute(text("ALTER TABLE tenants ADD COLUMN location VARCHAR(60) NULL"))
            db.session.commit()
            app.logger.warning("[Migration] Added location to tenants")

    if "admin_users" in tables:
        existing = {c["name"] for c in insp.get_columns("admin_users")}
        if "is_super" not in existing:
            if dialect == "sqlite":
                db.session.execute(text("ALTER TABLE admin_users ADD COLUMN is_super INTEGER NOT NULL DEFAULT 0"))
            else:
                db.session.execute(text("ALTER TABLE admin_users ADD COLUMN is_super BOOLEAN NOT NULL DEFAULT FALSE"))
            db.session.commit()
            # Promote the first admin (id=1) to super so the current owner
            # retains access to tenant management.
            if dialect == "sqlite":
                db.session.execute(text("UPDATE admin_users SET is_super = 1 WHERE id = 1"))
            else:
                db.session.execute(text("UPDATE admin_users SET is_super = TRUE WHERE id = 1"))
            db.session.commit()
            app.logger.warning("[Migration] Added is_super to admin_users; promoted admin id=1")

    # ─── Phase 3a: tenant_id on all data tables ───
    # Adds the column with a default of 1 so every existing row is
    # scoped to Simon Fresh Water (tenant id 1). Idempotent — skips
    # any table where the column already exists.
    _TENANT_SCOPED_TABLES = (
        "consumers",
        "meter_readings",
        "payment_log",
        "notification_log",
        "mpesa_log",
        "sent_bill_archive",
        "admin_audit_log",
        "admin_users",
    )
    for tbl in _TENANT_SCOPED_TABLES:
        if tbl not in tables:
            continue
        cols = {c["name"] for c in insp.get_columns(tbl)}
        if "tenant_id" in cols:
            continue
        try:
            db.session.execute(text(
                f"ALTER TABLE {tbl} ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1"
            ))
            db.session.commit()
            app.logger.warning(f"[Migration] Added tenant_id to {tbl}")
        except Exception as e:
            try:
                app.logger.warning(f"[Migration] tenant_id {tbl} skipped: {e}")
                db.session.rollback()
            except Exception:
                pass


# ═══════════════════════════════════════════════
#  FRONTEND ROUTES
# ═══════════════════════════════════════════════

# ═══════════════════════════════════════════════
#  DISPENSING — ANALYTICS (B4)
# ═══════════════════════════════════════════════
# Server-rendered SVG charts. No JS chart library, no CDN dependency.
# Data is numeric; no user-supplied strings enter the SVG.

def _fmt_axis(v):
    try:
        v = float(v)
    except (ValueError, TypeError):
        return "0"
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"{v/1_000:.0f}k"
    return f"{v:.0f}"


def _svg_empty_state(title=""):
    safe = (title or "").replace("<", "&lt;").replace(">", "&gt;")[:80]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 240" '
        'style="width:100%;height:auto;font-family:system-ui,sans-serif;">'
        '<rect width="900" height="240" fill="#fafafa" rx="6"/>'
        f'<text x="450" y="120" text-anchor="middle" font-size="13" fill="#999">'
        f'{safe} — no data for this range</text></svg>'
    )


def _svg_render_bars(labels, series, title="", width=900, height=240):
    """Server-rendered grouped bar chart.
    labels: list of x-axis strings
    series: list of dicts {name, color, values}
    Never raises — falls back to empty state on error."""
    try:
        import math
        n = len(labels)
        if n == 0 or not series:
            return _svg_empty_state(title)

        m = len(series)

        # Widen the viewBox in proportion to the number of bars, and set
        # the same width as the SVG's CSS min-width. This keeps scale = 1
        # (so SVG-internal font sizes render at native pixel sizes) while
        # the parent container scrolls horizontally when the SVG is wider
        # than the viewport.
        min_w = max(900, n * 50)
        if min_w > width:
            width = min_w

        pad_l, pad_r, pad_t, pad_b = 64, 20, 46, 60
        cw = width - pad_l - pad_r
        ch = height - pad_t - pad_b

        all_vals = [float(v or 0) for s in series for v in s["values"]]
        y_max_raw = max(all_vals) if all_vals else 0
        if y_max_raw <= 0:
            return _svg_empty_state(title)

        magnitude = 10 ** int(math.floor(math.log10(y_max_raw)))
        y_max = math.ceil(y_max_raw / magnitude) * magnitude
        if y_max <= 0:
            y_max = 1

        group_w = cw / n
        inner_pad = group_w * 0.15
        bar_area = group_w - inner_pad
        bar_w = bar_area / m

        def _y(v):
            return pad_t + ch - (float(v or 0) / y_max) * ch

        parts = []
        parts.append(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'style="width:100%;min-width:{width}px;height:auto;display:block;'
            'font-family:system-ui,-apple-system,sans-serif;">'
        )
        parts.append(f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fafafa" rx="6"/>')
        if title:
            safe_title = str(title).replace("<", "&lt;").replace(">", "&gt;")[:80]
            parts.append(f'<text x="{pad_l}" y="24" font-size="14" font-weight="600" fill="#333">{safe_title}</text>')

        for i in range(6):
            y_val = (y_max / 5) * i
            y = _y(y_val)
            parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="#e0e0e0" stroke-width="1"/>')
            parts.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" font-size="11" fill="#666" text-anchor="end">{_fmt_axis(y_val)}</text>')

        for i, lbl in enumerate(labels):
            gx = pad_l + (i * group_w) + (inner_pad / 2)
            for j, sr in enumerate(series):
                v = float(sr["values"][i] or 0)
                if v <= 0:
                    continue
                x = gx + j * bar_w
                y = _y(v)
                h = pad_t + ch - y
                parts.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(bar_w - 1, 1):.1f}" '
                    f'height="{h:.1f}" fill="{sr["color"]}" rx="1"/>'
                )
            lx = pad_l + (i + 0.5) * group_w
            safe_lbl = str(lbl).replace("<", "&lt;").replace(">", "&gt;")[:10]
            parts.append(f'<text x="{lx:.1f}" y="{pad_t + ch + 16}" font-size="11" fill="#666" text-anchor="middle">{safe_lbl}</text>')

        lx = pad_l
        ly = pad_t + ch + 36
        for sr in series:
            parts.append(f'<rect x="{lx}" y="{ly}" width="10" height="10" fill="{sr["color"]}" rx="1"/>')
            parts.append(f'<text x="{lx + 14}" y="{ly + 10}" font-size="12" fill="#444">{sr["name"]}</text>')
            lx += 14 + len(sr["name"]) * 7 + 20

        parts.append('</svg>')
        return "".join(parts)
    except Exception:
        return _svg_empty_state(title)


@app.route("/dispense/analytics")
def dispense_analytics_page():
    """Dispensing analytics dashboard (B4). Same guards as /dispense."""
    admin = _current_admin()
    if not admin:
        return redirect("/admin/login")
    if _current_active_profile() != "dispensing":
        return redirect("/")
    enabled = _tenant_profiles_dict(admin.tenant_id) if admin.tenant_id else {}
    if not enabled.get("dispensing"):
        return redirect("/")
    return _serve_frontend("dispense_analytics.html")


@app.route("/api/admin/dispense/analytics", methods=["GET"])
def admin_dispense_analytics():
    """Aggregate dispensing metrics over a date range + server-rendered SVG
    charts. Returns JSON; the client inserts charts[i] into the DOM."""
    u = _require_admin()
    if u: return u
    if not _rate_limit("dispense_analytics", 30, 60):
        return _too_many(60)
    tenant_id, err = _dispense_tenant_or_error()
    if err: return err

    preset   = (request.args.get("preset") or "").strip()
    from_str = (request.args.get("from") or "").strip()
    to_str   = (request.args.get("to") or "").strip()

    today = _nairobi_today()

    if from_str and to_str:
        try:
            d_from = datetime.strptime(from_str, "%Y-%m-%d").date()
            d_to   = datetime.strptime(to_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid date format (YYYY-MM-DD)."}), 400
        if d_from > d_to:
            return jsonify({"error": "From date must be <= To date."}), 400
        if (d_to - d_from).days > 365:
            return jsonify({"error": "Range cannot exceed 365 days."}), 400
        if d_to > today:
            d_to = today
    elif preset in ("7", "30", "90"):
        days = int(preset)
        d_to = today
        d_from = today - timedelta(days=days - 1)
    else:
        d_to = today
        d_from = today - timedelta(days=29)

    dates = []
    cur = d_from
    while cur <= d_to:
        dates.append(cur)
        cur += timedelta(days=1)

    stats_map = _dispense_window_stats(tenant_id, dates)

    daily = []
    for d in dates:
        st = stats_map[d.isoformat()]
        daily.append({
            "date": d.strftime("%d %b"),
            "date_iso": d.isoformat(),
            "consumption_l": st["consumption_l"],
            "sold_l": st["sold_l"],
            "sales_kes": st["sales_kes"],
            "paid_kes": st["paid_kes"],
            "outstanding_kes": st["outstanding_kes"],
            "variance_l": st["variance_l"],
            "has_reading": st["has_reading"],
        })

    totals = {
        "consumption_l": sum(x["consumption_l"] for x in daily),
        "sold_l":        sum(x["sold_l"] for x in daily),
        "variance_l":    sum(x["variance_l"] for x in daily),
        "sales_kes":     round(sum(x["sales_kes"] for x in daily), 2),
        "paid_kes":      round(sum(x["paid_kes"] for x in daily), 2),
        "outstanding_kes": round(sum(x["outstanding_kes"] for x in daily), 2),
    }

    sales_rows = (DispenseSale.query
                  .filter(DispenseSale.tenant_id == tenant_id,
                          DispenseSale.sale_date >= d_from,
                          DispenseSale.sale_date <= d_to,
                          DispenseSale.voided == False)  # noqa: E712
                  .all())
    by_container = {}
    for sale in sales_rows:
        k = int(sale.container_size_L or 0)
        by_container.setdefault(k, {"container_size_L": k, "qty": 0, "total_kes": 0.0})
        by_container[k]["qty"] += int(sale.quantity or 0)
        by_container[k]["total_kes"] += float(sale.total_kes or 0)
    containers = sorted(by_container.values(), key=lambda x: x["container_size_L"])
    for c in containers:
        c["total_kes"] = round(c["total_kes"], 2)

    pay_rows = (DispensePayment.query
                .filter(DispensePayment.tenant_id == tenant_id,
                        DispensePayment.payment_date >= d_from,
                        DispensePayment.payment_date <= d_to,
                        DispensePayment.voided == False)  # noqa: E712
                .all())
    by_method = {}
    for p in pay_rows:
        m = (p.method or "unknown")
        by_method.setdefault(m, {"method": m, "count": 0, "total_kes": 0.0})
        by_method[m]["count"] += 1
        by_method[m]["total_kes"] += float(p.amount_kes or 0)
    methods = sorted(by_method.values(), key=lambda x: x["method"])
    for m in methods:
        m["total_kes"] = round(m["total_kes"], 2)

    labels = [x["date"] for x in daily]

    chart_consumption = _svg_render_bars(
        labels,
        [
            {"name": "Meter (L)", "color": "#2e7d32",
             "values": [x["consumption_l"] for x in daily]},
            {"name": "Sold (L)", "color": "#1976d2",
             "values": [x["sold_l"] for x in daily]},
        ],
        title="Consumption vs Sold (litres per day)",
    )
    chart_money = _svg_render_bars(
        labels,
        [
            {"name": "Sales (KES)", "color": "#f57c00",
             "values": [x["sales_kes"] for x in daily]},
            {"name": "Paid (KES)", "color": "#2e7d32",
             "values": [x["paid_kes"] for x in daily]},
        ],
        title="Sales vs Paid (KES per day)",
    )

    return jsonify({
        "ok": True,
        "from_iso": d_from.isoformat(),
        "to_iso": d_to.isoformat(),
        "days": len(dates),
        "daily": daily,
        "totals": totals,
        "containers": containers,
        "payment_methods": methods,
        "charts": {
            "consumption_vs_sold": chart_consumption,
            "sales_vs_paid": chart_money,
        },
    })


@app.route("/dispense")
def dispense_home():
    """Water Dispensing portal (Phase C placeholder — real UI arrives Phase D)."""
    admin = _current_admin()
    if not admin:
        return redirect("/admin/login")
    if _current_active_profile() != "dispensing":
        return redirect("/")
    enabled = _tenant_profiles_dict(admin.tenant_id) if admin.tenant_id else {}
    if not enabled.get("dispensing"):
        return redirect("/")
    return _serve_frontend("dispense.html")


@app.route("/")
def home():
    if session.get("admin_id"):
        try:
            if _current_active_profile() == "dispensing":
                return redirect("/dispense")
        except Exception:
            pass
    return _serve_frontend("index.html")

@app.route("/consumer")
def consumer_page():
    return _serve_frontend("consumer.html")

@app.route("/bill")
def bill_page():
    return _serve_frontend("bill.html")

@app.route("/privacy")
def privacy_page():
    return _serve_frontend("privacy.html")


@app.route("/terms")
def terms_page():
    return _serve_frontend("terms.html")


@app.route("/admin/login")
def admin_login_page():
    """Serve the tenant admin login page.

    Session-vs-URL precedence at the login page:

      • Logged in + URL names a DIFFERENT tenant → clear the session
        and render the URL tenant's login page. Prevents the cross-
        tenant confusion where a demo admin sees SFW branding and
        then submits SFW credentials — which would silently log them
        into the SFW tenant without an explicit logout.

      • Logged in + URL names the SAME tenant (or no tenant) →
        redirect to the dashboard. No reason to see the login form
        again.

      • Anonymous → existing behaviour (URL tenant wins; inactive
        tenants get the disabled UI). Both ?__tenant= and ?__preview=
        are honoured, exactly as before.
    """
    _aid       = session.get("admin_id")
    _sess_slug = session.get("__tenant_slug") or _DEFAULT_TENANT_SLUG
    _url_slug  = (request.args.get("__tenant")
                  or request.args.get("__preview")
                  or "").strip()

    if _aid:
        if _url_slug and _url_slug != _sess_slug:
            # Silent logout + override g.tenant so the render below uses
            # the URL tenant, not the stale session tenant.
            session.clear()
            try:
                g.tenant = Tenant.query.filter_by(slug=_url_slug).first()
            except Exception:
                pass
        else:
            return redirect("/")

    slug = _url_slug
    tenant_inactive = False
    if slug:
        try:
            t = Tenant.query.filter_by(slug=slug).first()
            if t is not None and not t.is_active:
                tenant_inactive = True
        except Exception:
            pass

    path = os.path.join(FRONTEND_DIR, "admin_login.html")
    if not os.path.exists(path):
        return f"Template not found: admin_login.html", 404
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()

    # Same brand substitution as _serve_frontend, plus our new token
    for token, value in _brand_context().items():
        html = html.replace("{{" + token + "}}", value)
    html = html.replace("{{tenant_inactive}}",
                        "\"1\"" if tenant_inactive else "\"\"")

    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/admin/setup")
def admin_setup_page():
    return _serve_frontend("admin_setup.html")

@app.route("/admin/forgot")
def admin_forgot_page():
    return _serve_frontend("admin_forgot.html")

@app.route("/admin/settings")
def admin_settings_page():
    return _serve_frontend("admin_settings.html")

@app.route("/admin/mpesa")
def admin_mpesa_page():
    return _serve_frontend("admin_mpesa.html")

@app.route("/admin/reports")
def admin_reports_page():
    return _serve_frontend("admin_reports.html")

@app.route("/admin/bulk_sms")
def admin_bulk_sms_page():
    return _serve_frontend("admin_bulk_sms.html")


# ═══════════════════════════════════════════════
#  ADMIN — Tenant management (super-admin only)
# ═══════════════════════════════════════════════

def _valid_slug(slug: str) -> bool:
    """Lowercase, 3-60 chars, alphanumeric + hyphen, ends alphanumeric."""
    if not slug or not (3 <= len(slug) <= 60):
        return False
    if not slug[0].isalnum() or not slug[-1].isalnum():
        return False
    return all(c.isalnum() or c == "-" for c in slug)


def _tenant_profiles_dict(tenant_id):
    """Return {billing: bool, dispensing: bool}.

    Missing rows default to billing=True, dispensing=False — this is the
    exact behaviour every existing tenant has today, so nothing changes
    visually until the dev operator toggles a profile."""
    default = {"billing": True, "dispensing": False}
    try:
        rows = TenantProfile.query.filter_by(tenant_id=tenant_id).all()
    except Exception:
        return default
    if not rows:
        return default
    out = {"billing": False, "dispensing": False}
    for r in rows:
        if r.profile_name in out:
            out[r.profile_name] = bool(r.enabled)
    return out


def _initial_profile_for(tenant_id):
    """Default profile on login. Billing wins if both are enabled
    (matches design §1.3). Falls back to dispensing for P2-only tenants."""
    if not tenant_id:
        return "billing"
    enabled = _tenant_profiles_dict(tenant_id)
    if enabled.get("billing"):
        return "billing"
    if enabled.get("dispensing"):
        return "dispensing"
    return "billing"


def _current_active_profile():
    """Return 'billing' or 'dispensing' for the session's admin.
    Self-heals if the tenant's enabled profiles changed since login."""
    aid = session.get("admin_id")
    if not aid:
        return None
    try:
        a = AdminUser.query.get(aid)
        tenant_id = a.tenant_id if a else None
    except Exception:
        tenant_id = None
    if not tenant_id:
        return None
    enabled = _tenant_profiles_dict(tenant_id)
    cur = session.get("active_profile")
    if cur == "dispensing" and enabled.get("dispensing"):
        return "dispensing"
    if cur == "billing" and enabled.get("billing"):
        return "billing"
    # Self-heal
    if enabled.get("billing"):
        session["active_profile"] = "billing"
        return "billing"
    if enabled.get("dispensing"):
        session["active_profile"] = "dispensing"
        return "dispensing"
    return "billing"


def _tenant_to_dict(t):
    return {
        "id": t.id,
        "slug": t.slug,
        "org_name": t.org_name,
        "short_name": t.short_name,
        "tagline": t.tagline,
        "email": t.email,
        "phone": t.phone,
        "address": t.address,
        "location": t.location,
        "paybill": t.paybill,
        "payment_account": t.payment_account,
        "dispensing_paybill": t.dispensing_paybill,
        "dispensing_account": t.dispensing_account,
        "mpesa_shortcode": t.mpesa_shortcode,
        "mpesa_env": t.mpesa_env or "sandbox",
        "logo_key": t.logo_key,
        "theme_primary": t.theme_primary,
        "is_active": bool(t.is_active),
        "enabled_profiles": _tenant_profiles_dict(t.id),
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


@app.route("/api/tenant/is-active")
def tenant_is_active_api():
    """Return whether the current tenant is active. Public, cache-busting
    endpoint used by the tenant watchdog poller. Never returns tenant data
    beyond the active boolean and the slug."""
    slug = _sanitize_slug(request.args.get("__tenant") or "").strip()
    if not slug:
        slug = (session.get("__tenant_slug") or _DEFAULT_TENANT_SLUG)
    try:
        t = Tenant.query.filter_by(slug=slug).first()
        active = bool(t and t.is_active)
    except Exception:
        active = False
    resp = jsonify({"active": active, "slug": slug})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/api/tenant/reset", methods=["POST"])
def tenant_reset():
    session.pop("__tenant_slug", None)
    return jsonify({"ok": True, "tenant": _DEFAULT_TENANT_SLUG})


# ═══════════════════════════════════════════════
#  DEVELOPER PORTAL (unlinked, secret-authenticated)
# ═══════════════════════════════════════════════

@app.route("/dev/login", methods=["GET"])
def dev_login_page():
    if _dev_session_ok():
        return redirect("/dev")
    return _serve_frontend("dev_login.html")


@app.route("/dev/login", methods=["POST"])
def dev_login_submit():
    if not _rate_limit("dev_login", 5, 300):
        return _too_many(300)

    data = request.get_json(silent=True) or {}
    submitted = (data.get("secret") or "").strip()
    expected  = os.environ.get("DEV_PORTAL_SECRET", "").strip()

    if not expected:
        return jsonify({"error": "Developer portal is not configured on this server."}), 503
    if not submitted or not hmac.compare_digest(submitted, expected):
        _audit("dev_login_failed")
        return jsonify({"error": "Invalid access key."}), 401

    session.permanent = True
    session["dev_authenticated"] = True
    session["dev_last_activity"] = datetime.utcnow().isoformat()
    _audit("dev_login_ok")
    return jsonify({"ok": True})


@app.route("/dev/logout", methods=["POST"])
def dev_logout():
    session.pop("dev_authenticated", None)
    session.pop("dev_last_activity", None)
    return jsonify({"ok": True})


@app.route("/dev")
def dev_portal_page():
    if not _dev_session_ok():
        return redirect("/dev/login?next=/dev")
    return _serve_frontend("dev_tenants.html")


@app.route("/dev/api/env", methods=["GET"])
def dev_api_env():
    if not _dev_session_ok():
        return jsonify({"error": "Unauthorized"}), 401
    env = (os.environ.get("BACKUP_ENV", "") or "").strip().lower() or "unknown"
    db_id = (os.environ.get("CF_D1_DATABASE_ID", "") or "").strip()
    db_tail = db_id[-6:] if len(db_id) >= 6 else ""
    return jsonify({"env": env, "db_tail": db_tail})




@app.route("/admin/tenants")
def admin_tenants_page():
    admin = _current_admin()
    if not admin:
        return redirect("/admin/login?next=/admin/tenants")
    if not getattr(admin, "is_super", False):
        return "Forbidden. Super-admin only.", 403
    return _serve_frontend("admin_tenants.html")


@app.route("/tenant/logo/<slug>")
def tenant_logo_serve(slug):
    """Stream a tenant's custom logo. Logos are public branding, not
    tenant data, so we serve them regardless of is_active. This keeps the
    correct logo visible on the login page of an inactive tenant."""
    t = Tenant.query.filter_by(slug=slug).first()
    if not t or not t.logo_key or not t.logo_key.startswith("logos/"):
        return redirect("/images/SimonW_Logo.png")
    data, ctype = archive.download_object(t.logo_key)
    if data is None:
        return redirect("/images/SimonW_Logo.png")
    from io import BytesIO
    return send_file(BytesIO(data), mimetype=ctype, as_attachment=False,
                     download_name=f"{slug}-logo")


def _validate_image_magic(data: bytes, declared_mime: str):
    """Verify a file's actual magic bytes match its declared image MIME type.

    Closes Phase 7 finding 7.3a: without this, a text file uploaded with
    Content-Type: image/png is accepted. Impact was low (no XSS since SVG
    is excluded; no code execution since it's served as a static asset)
    but it is a MIME-confusion vulnerability worth fixing.

    Returns (ok: bool, error_message: str).
    """
    if not data:
        return False, "Empty file."
    if declared_mime == "image/png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return False, "File content is not a valid PNG image."
    elif declared_mime == "image/jpeg":
        if not data.startswith(b"\xff\xd8\xff"):
            return False, "File content is not a valid JPEG image."
    elif declared_mime == "image/gif":
        if not (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
            return False, "File content is not a valid GIF image."
    elif declared_mime == "image/webp":
        if len(data) < 12 or not data.startswith(b"RIFF") or data[8:12] != b"WEBP":
            return False, "File content is not a valid WEBP image."
    else:
        return False, f"Unsupported image type: {declared_mime}"
    return True, ""


@app.route("/api/admin/tenants/<int:tenant_id>/upload_logo", methods=["POST"])
def admin_tenants_upload_logo(tenant_id):
    """Upload/replace a tenant's logo. Super-admin only.
    Multipart form-data, field name 'logo'."""
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenant_logo_upload", 10, 60):
        return _too_many(60)
    t = Tenant.query.get(tenant_id)
    if not t:
        return jsonify({"error": "Tenant not found."}), 404
    f = request.files.get("logo")
    if not f or not f.filename:
        return jsonify({"error": "No file uploaded (field name must be 'logo')."}), 400
    ALLOWED = {"image/png":".png","image/jpeg":".jpg","image/webp":".webp","image/gif":".gif"}
    ctype = (f.mimetype or "").lower().strip()
    if ctype not in ALLOWED:
        return jsonify({"error": f"Unsupported content type {ctype!r}. Allowed: PNG, JPEG, WEBP, GIF."}), 400
    data = f.read()
    if len(data) > 500_000:
        return jsonify({"error": "Logo exceeds 500 KB."}), 400
    if len(data) == 0:
        return jsonify({"error": "Empty file."}), 400
    # MAGIC-BYTE-VALIDATION-PHASE7: verify actual bytes match declared MIME.
    _ok, _err = _validate_image_magic(data, ctype)
    if not _ok:
        return jsonify({"error": _err}), 400

    # C5c: decode-probe guard. Verifies the image actually opens and stays
    # under MAX_IMAGE_PIXELS BEFORE storing. Catches (a) files with valid
    # magic bytes but a broken/corrupted body, and (b) crafted decompression
    # bombs that would OOM the favicon generator on any anonymous
    # /tenant/favicon/<slug> request after being stored.
    if _PIL_AVAILABLE:
        try:
            from io import BytesIO as _ProbeBytesIO
            _probe = _PILImage.open(_ProbeBytesIO(data))
            _probe.load()
            _probe.close()
        except _PILImage.DecompressionBombError:
            return jsonify({"error": "Image dimensions are too large (decompression bomb)."}), 400
        except Exception as e:
            return jsonify({"error": f"Image is corrupted or unreadable: {e}"}), 400

    key = f"logos/{t.slug}/logo{ALLOWED[ctype]}"
    if not archive.upload_object(key, data, ctype):
        return jsonify({"error": "Storage upload failed."}), 502
    t.logo_key = key
    db.session.commit()
    # Invalidate the favicon cache for this tenant so the new logo is
    # reflected in bookmark / PWA icons immediately.
    try:
        stale = [k for k in _FAVICON_CACHE if k[0] == t.slug]
        for k in stale:
            _FAVICON_CACHE.pop(k, None)
    except Exception:
        pass
    _audit("tenant_logo_uploaded",
           target_type="tenant", target_id=t.id,
           target_label=f"{t.org_name} · {t.slug}",
           detail=f"key={key}, size={len(data)}, ctype={ctype}")
    return jsonify({"ok": True, "logo_key": key, "logo_url": f"/tenant/logo/{t.slug}"})


@app.route("/api/admin/tenants/<int:tenant_id>/upload_template", methods=["POST"])
def admin_tenants_upload_template(tenant_id):
    """Upload/replace a tenant's custom PDF template. Super-admin only.
    Multipart form-data: field 'template' (PDF), plus form field 'kind'
    ('water_bill' or 'receipt')."""
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenant_template_upload", 10, 60):
        return _too_many(60)

    kind = (request.form.get("kind") or "").strip()
    filename = _TEMPLATE_KINDS.get(kind)
    if not filename:
        return jsonify({"error": "kind must be 'water_bill' or 'receipt'."}), 400

    t = Tenant.query.get(tenant_id)
    if not t:
        return jsonify({"error": "Tenant not found."}), 404

    f = request.files.get("template")
    if not f or not f.filename:
        return jsonify({"error": "No file uploaded (field name must be 'template')."}), 400

    data = f.read()
    if not data:
        return jsonify({"error": "Empty file."}), 400
    if len(data) > 5_000_000:
        return jsonify({"error": "Template exceeds 5 MB."}), 400
    if not data.startswith(b"%PDF-"):
        return jsonify({"error": "Template must be a PDF file."}), 400

    # Sanity: it must open as a PDF and contain at least one {{placeholder}}.
    try:
        import fitz
        _doc = fitz.open(stream=data, filetype="pdf")
        _text = _doc[0].get_text()
        _doc.close()
    except Exception as e:
        return jsonify({"error": f"Invalid PDF: {e}"}), 400
    if "{{" not in _text or "}}" not in _text:
        return jsonify({
            "error": "Template contains no {{placeholder}} tokens. "
                     "Upload the exact PDF produced from the template HTML."
        }), 400

    key = f"templates/{t.slug}/{filename}"
    if not archive.upload_object(key, data, "application/pdf"):
        return jsonify({"error": "Storage upload failed."}), 502

    _audit("tenant_template_uploaded",
           target_type="tenant", target_id=t.id,
           target_label=f"{t.org_name} · {t.slug}",
           detail=f"kind={kind}, key={key}, bytes={len(data)}")
    return jsonify({"ok": True, "kind": kind, "key": key, "bytes": len(data)})


@app.route("/api/admin/tenants/<int:tenant_id>/sign_upload", methods=["POST"])
def admin_tenants_sign_upload(tenant_id):
    """Return a presigned R2 PUT URL for a browser → R2 direct upload
    of a tenant PDF template. The browser then PUTs the file straight
    to R2, so bytes never traverse this server.

    The signature is scoped to (bucket, key, content-type) and expires
    in 5 minutes. The server does NOT trust the upload — after the PUT
    the browser calls verify_upload, which re-downloads the object and
    runs the same validations the multipart endpoint runs."""
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenant_template_sign", 20, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    kind = (data.get("kind") or "").strip()
    filename = _TEMPLATE_KINDS.get(kind)
    if not filename:
        return jsonify({"error": "kind must be 'water_bill' or 'receipt'."}), 400

    t = Tenant.query.get(tenant_id)
    if not t:
        return jsonify({"error": "Tenant not found."}), 404

    key = f"templates/{t.slug}/{filename}"
    url = archive.presign_put(key, "application/pdf", expires=300)
    if not url:
        return jsonify({"error": "Storage is not configured or signing failed."}), 502

    _audit("tenant_template_signed",
           target_type="tenant", target_id=t.id,
           target_label=f"{t.org_name} · {t.slug}",
           detail=f"kind={kind}, key={key}")
    return jsonify({"ok": True, "kind": kind, "key": key, "upload_url": url})


@app.route("/api/admin/tenants/<int:tenant_id>/verify_upload", methods=["POST"])
def admin_tenants_verify_upload(tenant_id):
    """Validate that a browser → R2 direct upload produced a valid PDF.
    Re-downloads the object, runs the same checks as the multipart
    endpoint (magic bytes, size, {{placeholder}} presence). On any
    failure, deletes the just-uploaded object so the tenant is left
    with the previous template (or the bundled default)."""
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenant_template_verify", 20, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    kind = (data.get("kind") or "").strip()
    filename = _TEMPLATE_KINDS.get(kind)
    if not filename:
        return jsonify({"error": "kind must be 'water_bill' or 'receipt'."}), 400

    t = Tenant.query.get(tenant_id)
    if not t:
        return jsonify({"error": "Tenant not found."}), 404

    key = f"templates/{t.slug}/{filename}"
    blob, _ctype = archive.download_object(key)
    if not blob:
        return jsonify({"error": "Uploaded object not found in storage."}), 404
    if len(blob) > 5_000_000:
        archive.delete_pdf(key)
        return jsonify({"error": "Template exceeds 5 MB."}), 400
    if not blob.startswith(b"%PDF-"):
        archive.delete_pdf(key)
        return jsonify({"error": "Uploaded file is not a PDF."}), 400
    try:
        import fitz
        _doc = fitz.open(stream=blob, filetype="pdf")
        _text = _doc[0].get_text()
        _doc.close()
    except Exception as e:
        archive.delete_pdf(key)
        return jsonify({"error": f"Invalid PDF: {e}"}), 400
    if "{{" not in _text or "}}" not in _text:
        archive.delete_pdf(key)
        return jsonify({
            "error": "Template contains no {{placeholder}} tokens. "
                     "Upload the exact PDF produced from the template HTML."
        }), 400

    _audit("tenant_template_verified",
           target_type="tenant", target_id=t.id,
           target_label=f"{t.org_name} · {t.slug}",
           detail=f"kind={kind}, key={key}, bytes={len(blob)}")
    return jsonify({"ok": True, "kind": kind, "key": key, "bytes": len(blob)})


@app.route("/api/admin/tenants/<int:tenant_id>/delete_template", methods=["POST"])
def admin_tenants_delete_template(tenant_id):
    """Remove a tenant's custom PDF template, falling back to the
    bundled template on the next generation. Super-admin only."""
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenant_template_delete", 10, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    kind = (data.get("kind") or "").strip()
    filename = _TEMPLATE_KINDS.get(kind)
    if not filename:
        return jsonify({"error": "kind must be 'water_bill' or 'receipt'."}), 400

    t = Tenant.query.get(tenant_id)
    if not t:
        return jsonify({"error": "Tenant not found."}), 404

    key = f"templates/{t.slug}/{filename}"
    removed = archive.delete_pdf(key)

    _audit("tenant_template_deleted",
           target_type="tenant", target_id=t.id,
           target_label=f"{t.org_name} · {t.slug}",
           detail=f"kind={kind}, key={key}, removed={removed}")
    # Removing a non-existent key is not an error — the tenant simply
    # has no override, which is the desired end state.
    return jsonify({"ok": True, "kind": kind, "removed": bool(removed)})


# ═══════════════════════════════════════════════
#  DISPENSING PROFILE — CONTROLS (Phase B)
# ═══════════════════════════════════════════════

@app.route("/api/admin/tenants/<int:tenant_id>/profiles", methods=["GET"])
def admin_tenant_profiles_get(tenant_id):
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenant_profiles_get", 30, 60):
        return _too_many(60)
    t = Tenant.query.get(tenant_id)
    if not t:
        return jsonify({"error": "Tenant not found."}), 404
    return jsonify({"ok": True, "tenant_id": t.id,
                    "profiles": _tenant_profiles_dict(t.id)})


@app.route("/api/admin/tenants/<int:tenant_id>/profiles", methods=["POST"])
def admin_tenant_profiles_post(tenant_id):
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenant_profiles_post", 20, 60):
        return _too_many(60)
    t = Tenant.query.get(tenant_id)
    if not t:
        return jsonify({"error": "Tenant not found."}), 404

    data = request.get_json(silent=True) or {}
    billing = bool(data.get("billing"))
    dispensing = bool(data.get("dispensing"))
    if not billing and not dispensing:
        return jsonify({"error": "At least one profile must be enabled."}), 400

    now = datetime.utcnow()
    actor = session.get("admin_username") or "?"
    for name, enabled in (("billing", billing), ("dispensing", dispensing)):
        row = (TenantProfile.query
               .filter_by(tenant_id=t.id, profile_name=name)
               .first())
        if row is None:
            row = TenantProfile(tenant_id=t.id, profile_name=name,
                                enabled=enabled, enabled_at=now,
                                enabled_by=actor)
            db.session.add(row)
        elif bool(row.enabled) != enabled:
            row.enabled = enabled
            row.enabled_at = now
            row.enabled_by = actor
    db.session.commit()

    _audit("tenant_profiles_updated",
           target_type="tenant", target_id=t.id,
           target_label=f"{t.org_name} · {t.slug}",
           detail=f"billing={billing}, dispensing={dispensing}")
    return jsonify({"ok": True, "profiles": _tenant_profiles_dict(t.id)})


@app.route("/api/admin/tenants/<int:tenant_id>/dispense_rates", methods=["GET"])
def admin_tenant_dispense_rates_get(tenant_id):
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenant_dispense_rates_get", 30, 60):
        return _too_many(60)
    t = Tenant.query.get(tenant_id)
    if not t:
        return jsonify({"error": "Tenant not found."}), 404
    rows = (DispenseRate.query
            .filter_by(tenant_id=t.id)
            .order_by(DispenseRate.container_size_L.asc())
            .all())
    return jsonify({"ok": True, "rates": [
        {"id": r.id, "container_size_L": r.container_size_L,
         "price_kes": r.price_kes, "is_active": bool(r.is_active)}
        for r in rows
    ]})


@app.route("/api/admin/tenants/<int:tenant_id>/dispense_rates", methods=["POST"])
def admin_tenant_dispense_rates_post(tenant_id):
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenant_dispense_rates_post", 30, 60):
        return _too_many(60)
    t = Tenant.query.get(tenant_id)
    if not t:
        return jsonify({"error": "Tenant not found."}), 404

    data = request.get_json(silent=True) or {}
    try:
        size = int(data.get("container_size_L"))
        price = float(data.get("price_kes"))
    except (ValueError, TypeError):
        return jsonify({"error": "container_size_L and price_kes must be numbers."}), 400
    if size < 1 or size > 1_000_000:
        return jsonify({"error": "container_size_L out of range."}), 400
    if price < 0 or price > 1_000_000:
        return jsonify({"error": "price_kes out of range."}), 400

    row = (DispenseRate.query
           .filter_by(tenant_id=t.id, container_size_L=size)
           .first())
    if row is None:
        row = DispenseRate(tenant_id=t.id, container_size_L=size,
                           price_kes=price, is_active=True)
        db.session.add(row)
        action = "created"
    else:
        row.price_kes = price
        row.is_active = True
        action = "updated"
    db.session.commit()

    _audit("tenant_dispense_rate_saved",
           target_type="tenant", target_id=t.id,
           target_label=f"{t.org_name} · {t.slug}",
           detail=f"{action} size={size}L price={price}")
    return jsonify({"ok": True, "action": action, "rate": {
        "id": row.id,
        "container_size_L": row.container_size_L,
        "price_kes": row.price_kes,
        "is_active": bool(row.is_active),
    }})


@app.route("/api/admin/tenants/<int:tenant_id>/dispense_rates/<int:rate_id>",
           methods=["DELETE"])
def admin_tenant_dispense_rates_delete(tenant_id, rate_id):
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenant_dispense_rates_delete", 30, 60):
        return _too_many(60)
    t = Tenant.query.get(tenant_id)
    if not t:
        return jsonify({"error": "Tenant not found."}), 404
    row = DispenseRate.query.filter_by(id=rate_id, tenant_id=t.id).first()
    if not row:
        return jsonify({"error": "Rate not found."}), 404
    size = row.container_size_L
    db.session.delete(row)
    db.session.commit()

    _audit("tenant_dispense_rate_deleted",
           target_type="tenant", target_id=t.id,
           target_label=f"{t.org_name} · {t.slug}",
           detail=f"size={size}L")
    return jsonify({"ok": True, "deleted_id": rate_id})


@app.route("/api/admin/tenants", methods=["GET"])
def admin_tenants_list():
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenants_list", 30, 60):
        return _too_many(60)
    rows = (Tenant.query
            .order_by(Tenant.org_name.asc())
            .all())
    # Tenants admin hint — has_admin per row so the UI can hide the
    # "Create Admin" button once the tenant's first admin exists.
    out = []
    for t in rows:
        d = _tenant_to_dict(t)
        d["has_admin"] = (AdminUser.query
                          .filter_by(tenant_id=t.id)
                          .count() > 0)
        out.append(d)
    return jsonify({"tenants": out})


@app.route("/api/admin/tenants", methods=["POST"])
def admin_tenants_create():
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenants_create", 10, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}

    slug = (data.get("slug") or "").strip().lower()
    org_name = (data.get("org_name") or "").strip()
    if not _valid_slug(slug):
        return jsonify({"error": "Slug must be 3-60 chars, lowercase, "
                                 "letters/digits/hyphens, and start and end alphanumeric."}), 400
    if not org_name:
        return jsonify({"error": "org_name is required."}), 400
    if Tenant.query.filter_by(slug=slug).first():
        return jsonify({"error": f"Slug '{slug}' is already in use."}), 409

    paybill = (data.get("paybill") or "").strip() or None
    if paybill:
        clash = Tenant.query.filter_by(paybill=paybill, is_active=True).first()
        if clash:
            return jsonify({"error": f"Paybill {paybill} is already used by "
                                     f"active tenant '{clash.slug}'."}), 409
    mpesa_shortcode = (data.get("mpesa_shortcode") or "").strip() or None
    mpesa_env = (data.get("mpesa_env") or "sandbox").strip().lower()
    if mpesa_env not in ("sandbox", "production"):
        mpesa_env = "sandbox"

    t = Tenant(
        slug=slug,
        org_name=org_name,
        short_name=(data.get("short_name") or "").strip() or None,
        tagline=(data.get("tagline") or "").strip() or None,
        email=(data.get("email") or "").strip() or None,
        phone=(data.get("phone") or "").strip() or None,
        address=(data.get("address") or "").strip() or None,
        location=(data.get("location") or "").strip() or None,
        paybill=paybill,
        payment_account=(data.get("payment_account") or "").strip() or None,
        mpesa_shortcode=mpesa_shortcode,
        mpesa_env=mpesa_env,
        logo_key=(data.get("logo_key") or "").strip() or None,
        theme_primary=(data.get("theme_primary") or "").strip() or "#0d47a1",
        is_active=False,
    )
    db.session.add(t)
    db.session.commit()

    _audit("tenant_created",
           target_type="tenant", target_id=t.id,
           target_label=f"{t.org_name} · {t.slug}",
           detail=f"paybill={t.paybill or '—'}, is_active=False")

    return jsonify({"ok": True, "tenant": _tenant_to_dict(t)}), 201


@app.route("/api/admin/tenants/<int:tenant_id>/update", methods=["POST"])
def admin_tenants_update(tenant_id):
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenants_update", 30, 60):
        return _too_many(60)

    t = Tenant.query.get(tenant_id)
    if not t:
        return jsonify({"error": "Tenant not found."}), 404

    data = request.get_json(silent=True) or {}
    allowed = ("org_name", "short_name", "tagline", "email", "phone",
               "address", "location", "paybill", "payment_account",
               "dispensing_paybill", "dispensing_account",
               "mpesa_shortcode", "mpesa_env",
               "logo_key", "theme_primary")
    updated = []
    for f in allowed:
        if f not in data:
            continue
        v = (str(data[f]) or "").strip() if data[f] is not None else ""
        if f == "paybill" and v:
            clash = (Tenant.query
                     .filter(Tenant.paybill == v, Tenant.id != t.id,
                             Tenant.is_active == True)
                     .first())
            if clash:
                return jsonify({"error": f"Paybill {v} is already used by "
                                         f"active tenant '{clash.slug}'."}), 409
        if f == "dispensing_paybill" and v:
            # Phase G: dispensing paybill must not collide with this tenant's
            # billing paybill (would cause routing ambiguity), nor with any
            # other active tenant's paybill or dispensing_paybill.
            if v == (t.paybill or "").strip():
                return jsonify({"error": "Dispensing paybill must differ from "
                                         "the billing paybill."}), 409
            clash = (Tenant.query
                     .filter(Tenant.id != t.id, Tenant.is_active == True)
                     .filter(db.or_(Tenant.paybill == v,
                                    Tenant.dispensing_paybill == v))
                     .first())
            if clash:
                return jsonify({"error": f"Shortcode {v} is already used by "
                                         f"active tenant '{clash.slug}'."}), 409
        if f == "mpesa_env":
            if v not in ("sandbox", "production"):
                v = "sandbox"
            setattr(t, f, v)
            updated.append(f)
            continue
        setattr(t, f, v or None)
        updated.append(f)

    if not updated:
        return jsonify({"error": "No valid fields to update."}), 400

    db.session.commit()
    _audit("tenant_updated",
           target_type="tenant", target_id=t.id,
           target_label=f"{t.org_name} · {t.slug}",
           detail=f"fields={','.join(updated)}")
    return jsonify({"ok": True, "tenant": _tenant_to_dict(t)})


@app.route("/api/admin/tenants/<int:tenant_id>/create_admin", methods=["POST"])
def admin_tenants_create_admin(tenant_id):
    """Create the first admin for a tenant (super-admin only).
    Bypasses ADMIN_SETUP_CODE — the super-admin is the authority."""
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenant_admin_create", 10, 60):
        return _too_many(60)

    t = Tenant.query.get(tenant_id)
    if not t:
        return jsonify({"error": "Tenant not found."}), 404
    if not t.is_active:
        return jsonify({"error": "Activate the tenant first."}), 400
    if AdminUser.query.filter_by(tenant_id=t.id).count() > 0:
        return jsonify({"error": "This tenant already has an admin."}), 409

    data = request.get_json(silent=True) or {}
    # SAFE-CREATE-ADMIN-PHASE8: type-guard against non-string payloads
    # (parallel fix to the login route). Without this, an int username
    # would crash .strip().lower() with AttributeError → 500.
    _raw_un = data.get("username")
    _raw_pw = data.get("password")
    _raw_ph = data.get("phone")
    _raw_em = data.get("email")
    if not isinstance(_raw_un, str) or not isinstance(_raw_pw, str):
        return jsonify({"error": "username and password must be strings."}), 400
    username = _raw_un.strip().lower()
    password = _raw_pw
    phone    = (_raw_ph if isinstance(_raw_ph, str) else "").strip()
    email    = (_raw_em if isinstance(_raw_em, str) else "").strip() or None

    if len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    if not phone:
        return jsonify({"error": "Phone is required for password reset."}), 400
    if AdminUser.query.filter_by(username=username).first():
        return jsonify({"error": "Username already taken."}), 409

    admin = AdminUser(
        tenant_id=t.id,
        username=username,
        password_hash=generate_password_hash(password),
        phone=phone,
        email=email,
        is_active=True,
        is_super=False,
    )
    db.session.add(admin)
    db.session.commit()

    _audit("tenant_admin_created",
           target_type="tenant", target_id=t.id,
           target_label=f"{t.org_name} · {t.slug}",
           detail=f"admin_username={username}")

    return jsonify({"ok": True,
                    "admin": {"id": admin.id, "username": admin.username}}), 201


@app.route("/api/admin/tenants/<int:tenant_id>/switch", methods=["POST"])
def admin_tenants_switch(tenant_id):
    """Super-admin only. Clears the current session and returns the
    target tenant's login URL. The super-admin then signs in as that
    tenant's admin. This is the ONLY safe way to change tenants while
    logged in — the resolver ignores URL params for authenticated users
    on purpose, so cross-tenant redirection cannot happen via a link."""
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenant_switch", 10, 60):
        return _too_many(60)

    t = Tenant.query.get(tenant_id)
    if not t or not t.is_active:
        return jsonify({"error": "Tenant not found or inactive."}), 404

    _audit("tenant_switch",
           target_type="tenant", target_id=t.id,
           target_label=f"{t.org_name} · {t.slug}")

    session.clear()
    return jsonify({
        "ok": True,
        "redirect": f"/admin/login?__tenant={t.slug}",
    }), 200


@app.route("/api/admin/tenants/<int:tenant_id>/toggle_active", methods=["POST"])
def admin_tenants_toggle_active(tenant_id):
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenants_toggle", 30, 60):
        return _too_many(60)

    t = Tenant.query.get(tenant_id)
    if not t:
        return jsonify({"error": "Tenant not found."}), 404
    # The default tenant CAN be toggled: when inactive, its admin login
    # shows the disabled UI and public consumer pages return 503.
    # It cannot be deleted (see admin_tenants_delete).

    data = request.get_json(silent=True) or {}
    want_active = bool(data.get("active"))
    if want_active == bool(t.is_active):
        return jsonify({"ok": True, "tenant": _tenant_to_dict(t)})

    if want_active and t.paybill:
        clash = (Tenant.query
                 .filter(Tenant.paybill == t.paybill, Tenant.id != t.id,
                         Tenant.is_active == True)
                 .first())
        if clash:
            return jsonify({"error": f"Cannot activate: paybill {t.paybill} "
                                     f"is already used by active tenant '{clash.slug}'."}), 409

    t.is_active = want_active
    db.session.commit()
    _audit("tenant_toggled",
           target_type="tenant", target_id=t.id,
           target_label=f"{t.org_name} · {t.slug}",
           detail=f"is_active={t.is_active}")
    return jsonify({"ok": True, "tenant": _tenant_to_dict(t)})


@app.route("/api/admin/tenants/<int:tenant_id>/delete", methods=["POST"])
def admin_tenants_delete(tenant_id):
    """Hard-delete a tenant. Only allowed when the tenant has no consumers.
    The default tenant (simon-freshwater) can never be deleted."""
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("tenants_delete", 5, 60):
        return _too_many(60)

    # ── Confirmation secret required for delete ──
    # Deleting a tenant is irreversible. The caller must re-supply the
    # developer portal secret in the POST body, and it must match the
    # server's DEV_PORTAL_SECRET. Missing/incorrect → 401 (not 400) so
    # the frontend can distinguish "wrong secret" from "validation".
    _expected = os.environ.get("DEV_PORTAL_SECRET", "").strip()
    if not _expected:
        return jsonify({"error": "Delete confirmation is not configured on this server."}), 503
    _data = request.get_json(silent=True) or {}
    _submitted = (_data.get("secret") or "").strip()
    if not _submitted or not hmac.compare_digest(_submitted, _expected):
        _audit("tenant_delete_denied",
               target_type="tenant", target_id=tenant_id,
               detail="bad or missing confirmation secret")
        return jsonify({"error": "Invalid confirmation secret."}), 401

    t = Tenant.query.get(tenant_id)
    if not t:
        return jsonify({"error": "Tenant not found."}), 404
    if t.slug == "simon-freshwater":
        return jsonify({"error": "The default tenant cannot be deleted."}), 400

    # Safety check: refuse if any consumers, readings, payments, or admins
    # are associated with this tenant. Operator must deactivate instead.
    counts = {
        "consumers":       Consumer.query.filter_by(tenant_id=t.id).count(),
        "meter_readings":  MeterReading.query.filter_by(tenant_id=t.id).count(),
        "payment_log":     PaymentLog.query.filter_by(tenant_id=t.id).count(),
        "notification_log": NotificationLog.query.filter_by(tenant_id=t.id).count(),
        "mpesa_log":       MpesaLog.query.filter_by(tenant_id=t.id).count(),
        "sent_bill_archive": SentBillArchive.query.filter_by(tenant_id=t.id).count(),
        "admin_users":     AdminUser.query.filter_by(tenant_id=t.id).count(),
        "admin_audit_log": AdminAuditLog.query.filter_by(tenant_id=t.id).count(),
    }
    blocking = {k: v for k, v in counts.items() if v > 0}
    if blocking:
        summary = ", ".join(f"{k}={v}" for k, v in blocking.items())
        return jsonify({
            "error": f"Cannot delete tenant with data: {summary}. "
                     "Deactivate it instead."
        }), 409

    label = f"{t.org_name} · {t.slug}"
    db.session.delete(t)
    db.session.commit()

    _audit("tenant_deleted",
           target_type="tenant", target_id=tenant_id,
           target_label=label,
           detail="hard-delete; no associated rows")

    return jsonify({"ok": True, "deleted": label})


# ═══════════════════════════════════════════════
#  ADMIN — MONTHLY BILLING REPORT
# ═══════════════════════════════════════════════

def _parse_ym_from_request():
    """Extract (year, month) from query string; default = current month."""
    today = _nairobi_today()
    try:
        year = int(request.args.get("year", today.year))
    except (ValueError, TypeError):
        year = today.year
    try:
        month = int(request.args.get("month", today.month))
    except (ValueError, TypeError):
        month = today.month
    if not (1 <= month <= 12):
        month = today.month
    if not (2000 <= year <= 2100):
        year = today.year
    return year, month


def _build_report_snapshot(year: int, month: int) -> dict:
    """Collect the report data for the given month from D1."""
    month_names = ["January","February","March","April","May","June",
                   "July","August","September","October","November","December"]

    consumers = _tenant_query(Consumer).order_by(Consumer.cust_name.asc()).all()

    total_consumers = len(consumers)
    active_consumers = sum(1 for c in consumers if c.is_active)
    total_consumption = 0.0
    total_billed = 0.0
    total_paid = 0.0
    total_outstanding = 0.0

    # per-consumer data (active + terminated)
    rows = []
    terminated_rows = []
    for c in consumers:
        all_readings = (MeterReading.query
                        .filter_by(consumer_id=c.id)
                        .order_by(MeterReading.reading_date.asc(),
                                  MeterReading.id.asc())
                        .all())

        initial = float(c.meter_initial_reading_m3 or 0.0)
        cons_by_id = {}
        prev_m3 = None
        for r in all_readings:
            cons_by_id[r.id] = compute_consumption(r.reading_m3, prev_m3, initial)
            prev_m3 = r.reading_m3

        # Readings in the given month
        month_readings = [r for r in all_readings
                          if r.reading_date.year == year
                          and r.reading_date.month == month]

        cnt = len(month_readings)
        cm3 = round(sum(cons_by_id[r.id] for r in month_readings), 2)
        amt = round(sum(float(r.amount_kes or 0.0) for r in month_readings), 2)

        # Payments in the given month
        all_payments = PaymentLog.query.filter_by(consumer_id=c.id).all()
        month_payments = []
        for p in all_payments:
            dt = p.created_at
            if dt and dt.year == year and dt.month == month:
                month_payments.append(p)
        paid = round(sum(float(p.amount_kes or 0.0) for p in month_payments), 2)

        # Compute display balances (carry-forward) — same as consumer page.
        _disp_all = _carry_forward_display(all_readings, all_payments)
        _newest   = all_readings[-1] if all_readings else None
        _newest_display = (_disp_all.get(_newest.id, {}).get("balance")
                           if _newest else None)

        # Build the display-balance map BEFORE the status call, so the
        # report KPI card (via info["total_due"]) and the row both see the
        # same source of truth.
        _display_balances = {rid: v.get("balance", 0.0)
                             for rid, v in _disp_all.items()}

        # Overall status + outstanding balance (all-time).
        # Pass display_balances so KPI Outstanding uses display_balances
        # (sum of positive balances when Due/Overdue, 0 otherwise).
        # The override feeds the carry-forward display balance of the newest
        # reading into the status decision, so the report label is always
        # identical to the consumer page badge.
        info = get_consumer_status(list(reversed(all_readings)),
                                   latest_balance_override=_newest_display,
                                   display_balances=_display_balances)
        outstanding = float(info.get("total_due", 0.0))

        # Row's Outstanding — same rules as the consumer page summary block:
        #   Due / Overdue  → sum of all positive display balances
        #   Prepayment     → the negative credit (newest display balance)
        #   Cleared        → 0.00
        _status = info["status"]
        if _status in ("DUE", "OVERDUE"):
            _row_amount = round(
                sum(max(b, 0.0) for b in _display_balances.values()), 2)
        elif _status == "PREPAYMENT":
            _row_amount = round(_newest_display or 0.0, 2)   # negative
        else:  # CLEARED
            _row_amount = 0.0

        row = {
            "id": c.id,
            "name": c.cust_name,
            "meter": c.meter_acc_no,
            "cnt": cnt,
            "cm3": f"{cm3:.2f}",
            "amt": _fmt_money(amt),
            "paid": _fmt_money(paid),
            "bal": f"{_fmt_money(_row_amount)} · {info['label']}",
        }

        if c.is_active:
            total_consumption += cm3
            total_billed += amt
            total_paid += paid
            total_outstanding += outstanding
            rows.append(row)
        else:
            # Terminated — include only if there was activity this month
            if cnt > 0 or paid > 0:
                terminated_rows.append(row)

    # Safety cap — protects against pathological data; not a user-facing limit.
    # Override with env REPORT_MAX_ROWS (default 500).
    try:
        _report_max = int(os.environ.get("REPORT_MAX_ROWS", "500"))
    except (ValueError, TypeError):
        _report_max = 500
    if _report_max < 20:
        _report_max = 20

    # Full active + terminated lists — used by the HTML template and preview.
    _all_active_rows = list(rows[:_report_max])
    _all_terminated_rows = list(terminated_rows[:_report_max])

    truncated = (len(rows) > _report_max) or (len(terminated_rows) > _report_max)

    # The r1_*..r20_ placeholders below are LEGACY (used only by the PDF
    # endpoint /api/admin/report.pdf). The preview reads the full lists.
    rows = rows[:20]

    snapshot = {
        "report_no": f"RPT-{year}{month:02d}",
        "report_month": month_names[month - 1],
        "report_year": str(year),
        "generated_date": _nairobi_today().strftime("%d %b %Y"),
        "total_consumers": str(total_consumers),
        "active_consumers": str(active_consumers),
        "total_consumption": f"{total_consumption:.2f}",
        "total_billed": _fmt_money(total_billed),
        "total_paid": _fmt_money(total_paid),
        "total_outstanding": _fmt_money(total_outstanding),
    }

    # Row placeholders r1_*..r20_*
    for idx in range(1, 21):
        row = rows[idx - 1] if idx - 1 < len(rows) else None
        snapshot[f"r{idx}_id"]    = str(row["id"]) if row else ""
        snapshot[f"r{idx}_name"]  = row["name"]  if row else ""
        snapshot[f"r{idx}_meter"] = row["meter"] if row else ""
        snapshot[f"r{idx}_cnt"]   = str(row["cnt"]) if row else ""
        snapshot[f"r{idx}_cm3"]   = row["cm3"]   if row else ""
        snapshot[f"r{idx}_amt"]   = row["amt"]   if row else ""
        snapshot[f"r{idx}_paid"]  = row["paid"]  if row else ""
        snapshot[f"r{idx}_bal"]   = row["bal"]   if row else ""

    # Full active rows list — used by the HTML report template and the preview
    snapshot["rows"] = _all_active_rows
    # Full terminated rows list — used only by the preview JSON, not the PDF
    snapshot["_terminated_rows"] = _all_terminated_rows

    return snapshot, truncated


@app.route("/api/admin/report.pdf")
def download_billing_report():
    """Deprecated — the report is now generated from the HTML preview
    at /template/report and saved as PDF from the browser. This route
    preserves old bookmarks by redirecting with the same period."""
    u = _require_admin()
    if u: return u
    if not _rate_limit("report", 10, 60):
        return _too_many(60)

    year, month = _parse_ym_from_request()
    return redirect(f"/template/report?year={year}&month={month:02d}")


@app.route("/api/admin/report/preview")
def billing_report_preview():
    """Return the report data as JSON for the admin preview page."""
    u = _require_admin()
    if u: return u
    if not _rate_limit("report_preview", 20, 60):
        return _too_many(60)

    year, month = _parse_ym_from_request()

    # ─── Future-period guard ───
    today = _nairobi_today()
    is_future = (year > today.year) or (year == today.year and month > today.month)

    if is_future:
        month_name = _month_name(month)
        return jsonify({
            "year": year,
            "month": month,
            "report_no": f"RPT-{year}{month:02d}",
            "status": "future_period",
            "message": (
                f"No data available for the selected period. "
                f"{month_name} {year} is in the future — a report can "
                f"only cover months that have already passed."
            ),
            "summary": None,
            "rows": [],
            "terminated_rows": [],
            "truncated": False,
        })

    try:
        snapshot, truncated = _build_report_snapshot(year, month)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # ─── No-data guard ───
    month_has_readings = (_tenant_query(MeterReading)
                          .filter(db.extract('year', MeterReading.reading_date) == year)
                          .filter(db.extract('month', MeterReading.reading_date) == month)
                          .first())
    month_has_payments = None
    for p in _tenant_query(PaymentLog).all():
        if p.created_at and p.created_at.year == year and p.created_at.month == month:
            month_has_payments = p
            break

    if not month_has_readings and not month_has_payments:
        month_name = _month_name(month)
        return jsonify({
            "year": year,
            "month": month,
            "report_no": snapshot["report_no"],
            "status": "no_data",
            "message": (
                f"No data available for the selected period "
                f"({month_name} {year}). No meter readings or "
                f"payments were recorded in this month."
            ),
            "summary": None,
            "rows": [],
            "terminated_rows": [],
            "truncated": False,
        })

    # ─── Normal response — full lists, no 20-row cap ───
    rows = list(snapshot.pop("rows", []) or [])
    terminated_rows = list(snapshot.pop("_terminated_rows", []) or [])

    return jsonify({
        "year": year,
        "month": month,
        "report_no": snapshot["report_no"],
        "status": "ok",
        "summary": {
            "total_consumers": snapshot["total_consumers"],
            "active_consumers": snapshot["active_consumers"],
            "total_consumption": snapshot["total_consumption"],
            "total_billed": snapshot["total_billed"],
            "total_paid": snapshot["total_paid"],
            "total_outstanding": snapshot["total_outstanding"],
        },
        "rows": rows,
        "terminated_rows": terminated_rows,
        "truncated": truncated,
    })

@app.route("/template/waterbill")
def waterbill_template_view():
    """Serve the water bill template HTML for manual Chrome -> Save as PDF."""
    if not _current_admin():
        return redirect("/admin/login?next=/template/waterbill")
    return _serve_template("water_bill_template.html")


@app.route("/template/report")
def report_template_view():
    """Live-preview the monthly report HTML with real data.

    Query params (optional):
        ?year=YYYY&month=MM   — defaults to the current Nairobi month.
    The HTML template is filled server-side via Jinja2 using the same
    snapshot the PDF generator uses. Save as PDF from the browser.
    """
    if not _current_admin():
        return redirect("/admin/login?next=/template/report")

    year, month = _parse_ym_from_request()

    try:
        snapshot, _ = _build_report_snapshot(year, month)
    except Exception as e:
        app.logger.exception("[Report template] snapshot failed")
        return f"Report data failed: {e}", 500

    # The HTML template only needs the active rows list and scalars.
    snapshot.pop("_terminated_rows", None)

    html_path = os.path.join(TEMPLATES_DIR, "SimonWater_ReportTemplate.html")
    if not os.path.exists(html_path):
        return f"Report template not found: {html_path}", 500

    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    snapshot.update(_brand_context())
    rendered = render_template_string(html, **snapshot)
    return rendered, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/dev.css")
def _dev_css():
    return send_from_directory(FRONTEND_DIR, "dev.css")


@app.route("/tenant-watchdog.js")
def _tenant_watchdog_js():
    return send_from_directory(FRONTEND_DIR, "tenant-watchdog.js")


@app.route("/style.css")
def styles():
    return send_from_directory(FRONTEND_DIR, "style.css")


# ─── Favicons & PWA assets ───
def _serve_favicon_tenant_aware(default_filename, mimetype):
    """Serve a favicon.

    Resolution order:
      1. ?__tenant=<slug> in query → serve that tenant's logo
      2. Referer header contains ?__tenant=<slug> → same
      3. Otherwise → serve the static default file (SFW branding)

    When the tenant has an uploaded logo (logo_key starts with 'logos/'),
    we 302 redirect to /tenant/logo/<slug> which streams the R2 object
    with the correct content type. Otherwise we serve the static file.
    """
    slug = (request.args.get("__tenant") or "").strip()
    if not slug:
        ref = request.headers.get("Referer", "") or ""
        try:
            from urllib.parse import urlparse, parse_qs
            ref_q = parse_qs(urlparse(ref).query)
            slug = (ref_q.get("__tenant", [""])[0] or "").strip()
        except Exception:
            slug = ""

    if slug:
        try:
            t = Tenant.query.filter_by(slug=slug).first()
        except Exception:
            t = None
        if t and t.logo_key and t.logo_key.startswith("logos/"):
            # Serve bytes directly — no redirect. Chrome's bookmark-favicon
            # path handles 302s poorly and falls back to the "letter" icon.
            # Direct delivery also preserves the real Content-Type from R2
            # (image/jpeg, image/webp, etc.), which the previous 302 target
            # already sent correctly — but Chrome didn't get to see it.
            data, ctype = archive.download_object(t.logo_key)
            if data:
                from io import BytesIO
                return send_file(BytesIO(data),
                                 mimetype=ctype or mimetype,
                                 as_attachment=False)

    return send_from_directory(FRONTEND_DIR, default_filename, mimetype=mimetype)


# In-memory favicon cache. Keyed on (slug, size). Cleared on logo upload.
_FAVICON_CACHE = {}


def _tenant_favicon_png(slug: str, size: int = 192):
    """Return a square PNG favicon for the tenant. Downloads their R2
    logo (or the bundled SFW logo as fallback), center-crops to square,
    and resizes to `size` x `size`. Returns bytes, or None on any failure.

    This is the fix for Chrome's bookmark-shortcut path, which requires a
    PNG that actually matches the declared type and dimensions. The raw
    tenant logo may be JPEG, WEBP, or non-square — this normalises it.
    """
    cache_key = (slug, size)
    if cache_key in _FAVICON_CACHE:
        return _FAVICON_CACHE[cache_key]

    data = None
    try:
        t = Tenant.query.filter_by(slug=slug).first()
    except Exception:
        t = None
    if t and t.logo_key and t.logo_key.startswith("logos/"):
        data, _ctype = archive.download_object(t.logo_key)

    if not data:
        try:
            bundled = os.path.join(FRONTEND_DIR, "images", "SimonW_Logo.png")
            with open(bundled, "rb") as f:
                data = f.read()
        except Exception:
            return None

    try:
        from PIL import Image
        from io import BytesIO

        try:
            im = Image.open(BytesIO(data))
            im.load()
        except Image.DecompressionBombError:
            try:
                app.logger.warning(f"[Favicon] decompression bomb rejected for {slug}@{size}")
            except Exception:
                pass
            return None

        # Flatten transparency to white so the favicon looks consistent on
        # every browser theme.
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg.convert("RGB")
        elif im.mode != "RGB":
            im = im.convert("RGB")

        # Center-crop to square (handles wide or tall logos)
        w, h = im.size
        side = min(w, h)
        left = (w - side) // 2
        top  = (h - side) // 2
        im = im.crop((left, top, left + side, top + side))

        # High-quality resize
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.LANCZOS
        im = im.resize((size, size), resample)

        out = BytesIO()
        im.save(out, format="PNG", optimize=True)
        png = out.getvalue()
    except Exception as e:
        try:
            app.logger.warning(f"[Favicon] conversion failed for {slug}@{size}: {e}")
        except Exception:
            pass
        return None

    _FAVICON_CACHE[cache_key] = png
    return png


@app.route("/tenant/favicon/<slug>")
def tenant_favicon_serve(slug):
    """Serve a square PNG favicon for the tenant. This is what the
    bookmark-shortcut path fetches — a proper PNG at exactly the declared
    size, so Chrome accepts it."""
    try:
        size = int(request.args.get("size", "192"))
    except (ValueError, TypeError):
        size = 192
    if size not in (32, 64, 96, 128, 192, 256, 512):
        size = 192

    png = _tenant_favicon_png(slug, size)
    if png is None:
        return redirect("/images/SimonW_Logo.png")

    from io import BytesIO
    resp = send_file(BytesIO(png), mimetype="image/png",
                     as_attachment=False, download_name=f"{slug}-favicon.png")
    resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
    return resp


@app.route("/favicon.ico")
def favicon_ico():
    return _serve_favicon_tenant_aware("favicon.ico", "image/x-icon")


@app.route("/favicon-32x32.png")
def favicon_32():
    return _serve_favicon_tenant_aware("favicon-32x32.png", "image/png")


@app.route("/favicon-16x16.png")
def favicon_16():
    return _serve_favicon_tenant_aware("favicon-16x16.png", "image/png")


@app.route("/apple-touch-icon.png")
def apple_touch_icon():
    return _serve_favicon_tenant_aware("apple-touch-icon.png", "image/png")


@app.route("/icon-192.png")
def icon_192():
    return send_from_directory(FRONTEND_DIR, "icon-192.png",
                                mimetype="image/png")


@app.route("/icon-512.png")
def icon_512():
    return send_from_directory(FRONTEND_DIR, "icon-512.png",
                                mimetype="image/png")


@app.route("/manifest.webmanifest")
def manifest():
    """Tenant-aware PWA manifest.

    This route is in the static-path whitelist so the tenant-resolution
    middleware does NOT set g.tenant for it. We therefore resolve the
    tenant explicitly from ?__tenant= here. Falls back to the default
    tenant (SFW) when no slug is provided, matching the old static
    manifest's behaviour.

    Icons point at /tenant/logo/<slug> for tenants with an uploaded
    logo, or /images/SimonW_Logo.png for the default tenant.
    """
    slug = (request.args.get("__tenant") or "").strip() or _DEFAULT_TENANT_SLUG
    t = None
    try:
        t = Tenant.query.filter_by(slug=slug).first()
    except Exception:
        t = None
    if t is None:
        try:
            t = Tenant.query.filter_by(slug=_DEFAULT_TENANT_SLUG).first()
        except Exception:
            t = None

    name  = (t.org_name if t and t.org_name else "Simon Fresh Water")
    short = ((t.short_name if t and t.short_name else name)[:12]).strip() or name[:12]
    _lk   = ((t.logo_key if t and t.logo_key else "") or "").strip()
    if _lk.startswith("logos/"):
        logo = "/tenant/logo/" + (t.slug if t else "")
    else:
        logo = "/" + (_lk or "images/SimonW_Logo.png")
    if not logo.startswith("/"):
        logo = "/" + logo
    theme = (t.theme_primary if t and t.theme_primary else "#0d47a1")
    slug_out = (t.slug if t and t.slug else _DEFAULT_TENANT_SLUG)

    return jsonify({
        "name":             name,
        "short_name":       short,
        "description":      f"{name} utility billing portal",
        "start_url":        f"/?__tenant={slug_out}",
        "scope":            "/",
        "display":          "standalone",
        "background_color": "#0a2540",
        "theme_color":      theme,
        "icons": [
            {"src": f"/tenant/favicon/{slug_out}?size=192", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": f"/tenant/favicon/{slug_out}?size=512", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
        ],
    }), 200, {"Content-Type": "application/manifest+json"}


@app.route("/images/<path:filename>")
def images(filename):
    """Serve files from frontend/images/ (logo, etc.)."""
    return send_from_directory(os.path.join(FRONTEND_DIR, "images"), filename)

@app.route("/app.js")
def appjs():
    return send_from_directory(FRONTEND_DIR, "app.js")


# ─── Public documentation (guides, onboarding PDFs, etc.) ───
DOCS_DIR = os.path.join(REPO_DIR, "docs")

@app.route("/docs/<path:filename>")
def serve_docs_file(filename):
    """Serve a file from the repository's docs/ folder."""
    return send_from_directory(DOCS_DIR, filename)


@app.route("/template/statement")
def statement_template_view():
    """
    Serve the statement template HTML for manual Chrome -> Save as PDF.
    Admin-only.
    """
    if not _current_admin():
        return redirect("/admin/login?next=/template/statement")
    return _serve_template("SimonWater_StatementTemplate.html")


@app.route("/template/receipt")
def receipt_template_view():
    """
    Serve the receipt template HTML for manual Chrome -> Save as PDF.
    Admin-only; unauthenticated visitors are redirected to the login page.
    """
    if not _current_admin():
        return redirect("/admin/login?next=/template/receipt")
    return _serve_template("SimonWater_ReceiptTemplate.html")


# ═══════════════════════════════════════════════
#  API — HEALTH
# ═══════════════════════════════════════════════

@app.route("/api/public/config")
def public_config():
    """Non-secret config values safe for the frontend to read."""
    return jsonify({
        "paybill": os.environ.get("PAYBILL", ""),
        "company": "Simon Fresh Water",
    })


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})


@app.route("/api/ping")
def ping():
    """Ultra-light keep-alive. Returns exactly 2 bytes: OK."""
    return "OK", 200, {"Content-Type": "text/plain"}


# ═══════════════════════════════════════════════
#  API — SEARCH (admin-only)
# ═══════════════════════════════════════════════

@app.route("/api/search")
def search_consumer():
    if not _rate_limit("search", 30, 60):
        return _too_many(60)

    u = _require_admin()
    if u:
        return u

    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []}), 400

    pattern = f"%{q}%"
    consumers = (_tenant_query(Consumer).filter(db.or_(
        Consumer.cust_name.ilike(pattern),
        Consumer.meter_acc_no.ilike(pattern),
    )).limit(20).all())

    out = []
    for c in consumers:
        # Carry-forward display balance — same source as the consumer page
        # badge, so search results and the destination page agree.
        _r_desc = _all_readings(c.id)
        _pays   = _consumer_payments(c.id)
        _disp   = _carry_forward_display(_r_desc, _pays)
        _newest_id = _r_desc[0].id if _r_desc else None
        _newest_disp = _disp.get(_newest_id, {}).get("balance") if _newest_id else None
        info = get_consumer_status(_r_desc, latest_balance_override=_newest_disp)
        out.append({
            "id": c.id, "cust_name": c.cust_name, "acc_name": c.acc_name,
            "meter_acc_no": c.meter_acc_no,
            "status": info["status"], "status_label": info["label"],
            "is_active": bool(c.is_active),
        })
    return jsonify({"results": out})


# ═══════════════════════════════════════════════
#  API — CONSUMER
# ═══════════════════════════════════════════════

def _consumer_payments(consumer_id):
    """All payments for a consumer, oldest first — used by the
    carry-forward display reconstruction."""
    return (PaymentLog.query
            .filter_by(consumer_id=consumer_id)
            .order_by(PaymentLog.created_at.asc(),
                      PaymentLog.id.asc())
            .all())


def _display_latest_balance(all_r, all_p=None):
    """Return the carry-forward display balance of the newest reading,
    or None if there are no readings. Used to feed status decisions
    that must agree with the readings table."""
    if not all_r:
        return None
    disp = _carry_forward_display(all_r, all_p)
    newest_id = all_r[-1].id
    return disp.get(newest_id, {}).get("balance")


def _carry_forward_display(all_r, all_p=None):
    """Per-reading display values: paid, prepaid_credit, balance, status.

    Reconstructs "effective paid" per reading from the actual payment
    events (PaymentLog) — NOT the FIFO-allocated MeterReading.amount_paid
    column, which silently redistributes payments across readings.

    Chronological walk of readings + payments:
      - A new reading opens a bill; carry-forward credit is applied
        immediately, any surplus flowing to the next reading.
      - A payment is allocated FIFO across readings that exist at that
        moment; a surplus beyond all dues stays on the newest reading
        and becomes the carry-forward credit for the next reading.

    Each row shows:
        paid            — money actually paid against this bill at its time
        prepaid_credit  — credit carried forward from the previous reading
        balance         — amount_kes − paid − prepaid_credit
        status          — Prepayment | Cleared | Due | Overdue

    Raw DB values are not modified. Returns
    {reading_id: {"paid", "prepaid_credit", "balance", "status"}}.
    """
    from datetime import datetime as _dt, time as _time

    if not all_r:
        return {}

    # ── Chronological event stream ──
    # Readings are timestamped by their real created_at (when they were
    # saved into the system), NOT by reading_date — several readings can
    # share one reading_date, so reading_date alone destroys the true
    # sequence against payments. Falls back to reading_date only for
    # legacy rows whose created_at is missing.
    events = []
    for r in all_r:
        ct = getattr(r, "created_at", None)
        if ct is not None:
            rdt = ct
        else:
            rd = r.reading_date
            rdt = rd if isinstance(rd, _dt) else _dt.combine(rd, _time.min)
        events.append((rdt, 0, r))            # 0 = reading

    for p in (all_p or []):
        if getattr(p, "created_at", None) is None:
            continue
        events.append((p.created_at, 1, p))   # 1 = payment

    # Reading sorts before payment at identical timestamps; id is a stable
    # tiebreaker for events that share a timestamp to the microsecond.
    events.sort(key=lambda e: (e[0], e[1], getattr(e[2], "id", 0)))

    eff_paid        = {}
    credit_in       = {}
    readings_so_far = []
    running_credit  = 0.0

    for _, etype, obj in events:
        if etype == 0:
            r = obj
            charge = round(float(r.amount_kes or 0.0), 2)
            credit_in[r.id] = round(running_credit, 2)
            eff_paid[r.id]  = 0.0
            remaining = round(charge - running_credit, 2)
            running_credit = round(-remaining, 2) if remaining < 0 else 0.0
            readings_so_far.append(r)
        else:
            p = obj
            amount = round(float(p.amount_kes or 0.0), 2)
            if amount <= 0 or not readings_so_far:
                continue

            # FIFO across existing readings
            for r in readings_so_far:
                if amount <= 0:
                    break
                paid   = eff_paid.get(r.id, 0.0)
                cred   = credit_in.get(r.id, 0.0)
                charge = round(float(r.amount_kes or 0.0), 2)
                due = round(charge - paid - cred, 2)
                if due <= 0:
                    continue
                apply = min(amount, due)
                eff_paid[r.id] = round(paid + apply, 2)
                amount = round(amount - apply, 2)

            # Surplus stays on the newest reading
            if amount > 0:
                newest = readings_so_far[-1]
                eff_paid[newest.id] = round(
                    eff_paid.get(newest.id, 0.0) + amount, 2)

            # Recompute carry-forward credit
            newest = readings_so_far[-1]
            charge = round(float(newest.amount_kes or 0.0), 2)
            bal = round(charge - eff_paid.get(newest.id, 0.0)
                               - credit_in.get(newest.id, 0.0), 2)
            running_credit = round(-bal, 2) if bal < 0 else 0.0

    # ── Compose output ──
    result = {}
    for r in all_r:
        charge = round(float(r.amount_kes or 0.0), 2)
        paid_val   = round(eff_paid.get(r.id, 0.0), 2)
        credit_val = round(credit_in.get(r.id, 0.0), 2)
        balance = round(charge - paid_val - credit_val, 2)

        if balance < 0:
            status = "Prepayment"
        elif balance == 0:
            status = "Cleared"
        else:
            age_days = (_nairobi_today() - r.reading_date).days
            status = "Due" if age_days <= 20 else "Overdue"

        result[r.id] = {
            "paid":           paid_val,
            "prepaid_credit": credit_val,
            "balance":        balance,
            "status":         status,
        }
    return result


@app.route("/api/consumer/<int:consumer_id>")
def get_consumer_details(consumer_id):
    consumer = _tenant_query(Consumer).filter_by(id=consumer_id).first_or_404()
    all_r = (MeterReading.query.filter_by(consumer_id=consumer.id)
             .order_by(MeterReading.reading_date.asc(),
                       MeterReading.id.asc()).all())

    initial = float(consumer.meter_initial_reading_m3 or 0.0)
    cons_by_id, prev_m3 = {}, None
    for r in all_r:
        cons_by_id[r.id] = compute_consumption(r.reading_m3, prev_m3, initial)
        prev_m3 = r.reading_m3

    # ─── Carry-forward display (paid, balance, status) ───
    # Waterfall over ALL readings, oldest → newest. Surplus on an older
    # reading flows to the next under its Paid column. Older readings
    # show 0.00; only the newest can be negative (Prepayment).
    _payments_all = _consumer_payments(consumer.id)
    display_by_id = _carry_forward_display(all_r, _payments_all)

    latest5 = list(reversed(all_r))[:5]
    latest12 = list(reversed(all_r))[:12]   # for the trend chart
    # Feed the newest reading's carry-forward display balance into the
    # status decision — so the badge matches the readings table below it.
    _newest_id = latest5[0].id if latest5 else None
    _newest_display = display_by_id.get(_newest_id, {}).get("balance") if _newest_id else None
    # Pass the full display-balance map so the summary totals reflect every
    # bill (Outstanding = sum of positive balances when the badge is Due/Overdue).
    _display_balances = {rid: v.get("balance", 0.0)
                         for rid, v in display_by_id.items()}
    info = get_consumer_status(latest5,
                               latest_balance_override=_newest_display,
                               display_balances=_display_balances)

    last_sms = (NotificationLog.query
                .filter_by(consumer_id=consumer.id, channel="sms", status="sent")
                .order_by(NotificationLog.sent_at.desc()).first())
    last_email = (NotificationLog.query
                  .filter_by(consumer_id=consumer.id, channel="email", status="sent")
                  .order_by(NotificationLog.sent_at.desc()).first())

    payments_out = []
    if _current_admin():
        payments = (PaymentLog.query
                    .filter_by(consumer_id=consumer.id)
                    .order_by(PaymentLog.created_at.desc())
                    .limit(10)
                    .all())
        payments_out = [{
            "id": p.id,
            "amount_kes": p.amount_kes,
            "method": p.method,
            "reference": p.reference,
            "recorded_by": p.recorded_by,
            "created_at": p.created_at.isoformat(),
            "has_receipt": bool(p.receipt_json),
        } for p in payments]

    return jsonify({
        "consumer": {
            "id": consumer.id, "cust_name": consumer.cust_name,
            "acc_name": consumer.acc_name, "meter_acc_no": consumer.meter_acc_no,
            "contact": consumer.contact, "email": consumer.email,
            "address": consumer.address,
            "latitude": consumer.latitude, "longitude": consumer.longitude,
            "meter_initial_reading_m3": initial,
            "is_active": bool(consumer.is_active),
            "terminated_at": consumer.terminated_at.isoformat() if consumer.terminated_at else None,
            "whatsapp_opt_in": bool(consumer.whatsapp_opt_in),
            "alt_contact": consumer.alt_contact,
            "alt_whatsapp_opt_in": bool(consumer.alt_whatsapp_opt_in),
        },
        "readings": [{
            "id": r.id, "reading_m3": r.reading_m3,
            "consumption_m3": cons_by_id.get(r.id, 0.0),
            "reading_date": r.reading_date.isoformat(),
            "amount_kes": r.amount_kes,
            "amount_paid":    display_by_id.get(r.id, {}).get("paid", r.amount_paid),
            "prepaid_credit": display_by_id.get(r.id, {}).get("prepaid_credit", 0.0),
            "balance":        display_by_id.get(r.id, {}).get("balance", r.balance),
            "bill_status":    display_by_id.get(r.id, {}).get(
                "status", get_reading_bill_status(r)),
        } for r in latest5],
        "chart_readings": [{
            "reading_m3": r.reading_m3,
            "consumption_m3": cons_by_id.get(r.id, 0.0),
            "reading_date": r.reading_date.isoformat(),
        } for r in latest12],
        "payments": payments_out,
        "overall_status": info,
        "available_channels": get_available_channels(),
        "last_notifications": {
            "sms": last_sms.sent_at.isoformat() if last_sms else None,
            "email": last_email.sent_at.isoformat() if last_email else None,
        },
    })


@app.route("/api/reading/<int:reading_id>")
def get_reading_bill(reading_id):
    reading = _tenant_query(MeterReading).filter_by(id=reading_id).first_or_404()
    consumer = reading.consumer
    # Compute carry-forward display values for the selected reading
    _all_asc = (MeterReading.query
                .filter_by(consumer_id=consumer.id)
                .order_by(MeterReading.reading_date.asc(),
                          MeterReading.id.asc()).all())
    _disp = _carry_forward_display(_all_asc, _consumer_payments(consumer.id)).get(reading.id, {})
    # Find the reading that came immediately BEFORE this one
    # (largest (reading_date, id) strictly smaller than the current one).
    prev_query = (MeterReading.query
                  .filter_by(consumer_id=consumer.id)
                  .filter(db.or_(
                      MeterReading.reading_date < reading.reading_date,
                      db.and_(
                          MeterReading.reading_date == reading.reading_date,
                          MeterReading.id < reading.id,
                      ),
                  ))
                  .order_by(MeterReading.reading_date.desc(),
                            MeterReading.id.desc()))
    prev = prev_query.first()

    # Previous (MTR) Reading:
    #   - if a previous reading exists → its cumulative reading_m3
    #   - otherwise (first-ever reading) → the meter's initial value
    if prev is not None:
        prev_reading_m3 = float(prev.reading_m3)
        prev_m3_for_calc = prev_reading_m3
    else:
        prev_reading_m3 = float(consumer.meter_initial_reading_m3 or 0.0)
        prev_m3_for_calc = None   # compute_consumption treats None as first-reading

    consumption = compute_consumption(
        reading.reading_m3, prev_m3_for_calc,
        float(consumer.meter_initial_reading_m3 or 0.0),
    )

    return jsonify({
        "consumer": {
            "cust_name": consumer.cust_name, "acc_name": consumer.acc_name,
            "meter_acc_no": consumer.meter_acc_no, "contact": consumer.contact,
            "address": consumer.address,
        },
        "reading": {
            "id": reading.id,
            "reading_m3": reading.reading_m3,
            "previous_reading_m3": prev_reading_m3,
            "consumption_m3": consumption,
            "reading_date": reading.reading_date.isoformat(),
            "bill_month": reading.reading_date.strftime("%B %Y"),
            "amount_kes": reading.amount_kes,
            "amount_paid":    _disp.get("paid", reading.amount_paid),
            "prepaid_credit": _disp.get("prepaid_credit", 0.0),
            "balance":        _disp.get("balance", reading.balance),
            "bill_status":    _disp.get("status", get_reading_bill_status(reading)),
        },
    })


@app.route("/api/reading/<int:reading_id>/bill.pdf")
def download_water_bill(reading_id):
    """Generate and stream the water-bill PDF for a specific reading.
    Admin-only."""
    if not _rate_limit("water_bill", 20, 60):
        return _too_many(60)

    u = _require_admin()
    if u:
        return u

    reading = _tenant_query(MeterReading).filter_by(id=reading_id).first_or_404()
    consumer = reading.consumer

    # Consumption for this specific reading
    prev = _previous_reading(consumer.id, exclude_id=reading.id)
    prev_m3 = None
    if prev and (prev.reading_date < reading.reading_date
                 or (prev.reading_date == reading.reading_date
                     and prev.id < reading.id)):
        prev_m3 = prev.reading_m3
    consumption = compute_consumption(
        reading.reading_m3, prev_m3,
        float(consumer.meter_initial_reading_m3 or 0.0),
    )

    # Total outstanding balance across all readings
    all_r = _all_readings(consumer.id)
    info = get_consumer_status(all_r)

    readings_desc = _all_readings(consumer.id)
    idx = next((i for i, r in enumerate(readings_desc) if r.id == reading.id), None)
    older_readings = readings_desc[idx + 1:] if idx is not None else []
    if older_readings:
        previous_m3 = older_readings[0].reading_m3
    else:
        previous_m3 = float(consumer.meter_initial_reading_m3 or 0.0)
    current_charges = float(reading.amount_kes or 0.0)

    # Carry-forward display values for the billed reading
    _asc = list(reversed(readings_desc))
    _pays = _consumer_payments(consumer.id)
    _disp_map = _carry_forward_display(_asc, _pays)
    _d = _disp_map.get(reading.id, {})

    # Total Outstanding = signed net position (Option B)
    total_outstanding = round(sum(r.balance for r in readings_desc), 2)
    current_display_balance = _d.get("balance", reading.balance)
    # Previous Outstanding = Total − current bill's display balance
    prev_outstanding = round(total_outstanding - current_display_balance, 2)

    _pay = _payment_info(consumer, tenant=g.tenant)
    snapshot = {
        "bill_no":              f"{reading.id:06d}",
        "date":                 reading.reading_date.strftime("%d %b %Y"),
        "bill_month":           reading.reading_date.strftime("%B %Y"),
        "cust_name":            consumer.cust_name,
        "meter_acc_no":         consumer.meter_acc_no,
        "reading_m3":           f"{reading.reading_m3:.2f}",
        "previous_reading":     f"{previous_m3:.2f}",
        "amount_kes":           _fmt_money(current_charges),
        "payment_carried_forward": f"KES {_fmt_money(_d.get('prepaid_credit', 0.0))}",
        "paid":                 f"KES {_fmt_money(_d.get('paid', 0.0))}",
        "previous_outstanding": _fmt_money(prev_outstanding),
        "total_outstanding":    _fmt_money(total_outstanding),
        "status_label":         _d.get("status", get_reading_bill_status(reading)).upper(),
        "paybill":              _pay["paybill"],
        "payment_account":      _pay["account"],
    }

    try:
        snapshot.update(_brand_context())
        snapshot["template_r2_key"] = _resolve_template_key(g.tenant, "water_bill")
        pdf_buf = generate_water_bill_pdf(snapshot)
    except Exception as e:
        app.logger.exception("[WaterBill] PDF generation failed")
        return jsonify({"error": f"PDF generation failed: {e}"}), 500

    filename = f"{_safe_filename(consumer.cust_name)}_bill_{reading.id:06d}.pdf"
    return send_file(
        pdf_buf,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=filename,
    )


@app.route("/api/consumer/<int:consumer_id>/statement.pdf")
def download_statement(consumer_id):
    """Generate the customer accounting statement PDF (admin-only).
    Built on-the-fly from current DB state — reflects live readings + payments."""
    if not _rate_limit("statement", 20, 60):
        return _too_many(60)

    u = _require_admin()
    if u:
        return u

    consumer = _tenant_query(Consumer).filter_by(id=consumer_id).first_or_404()

    # ── Readings, oldest first ──
    readings_asc = (MeterReading.query
                    .filter_by(consumer_id=consumer.id)
                    .order_by(MeterReading.reading_date.asc(),
                              MeterReading.id.asc())
                    .all())

    initial = float(consumer.meter_initial_reading_m3 or 0.0)
    cons_by_id = {}
    prev_m3 = None
    for r in readings_asc:
        cons_by_id[r.id] = compute_consumption(r.reading_m3, prev_m3, initial)
        prev_m3 = r.reading_m3

    # ── Payments, oldest first ──
    payments_asc = (PaymentLog.query
                    .filter_by(consumer_id=consumer.id)
                    .order_by(PaymentLog.created_at.asc(),
                              PaymentLog.id.asc())
                    .all())

    # ── Carry-forward display map (for prepaid credit rows + status) ──
    _disp = _carry_forward_display(readings_asc, payments_asc)

    # ── Chronological ledger — created_at ordering ──
    from datetime import datetime as _dt, time as _time
    events = []
    for r in readings_asc:
        _ts = r.created_at if r.created_at else _dt.combine(r.reading_date, _time.min)
        events.append({
            "ts":        _ts,
            "date":      r.reading_date,
            "kind":      "reading",
            "type":      "Water Bill",
            "ref":       f"Reading {r.reading_m3:.2f} M³",
            "dr":        float(r.amount_kes or 0.0),
            "cr":        0.0,
            "sort_id":   r.id,
            "affects":   True,
            "is_credit": False,
        })
        _cr_used = float(_disp.get(r.id, {}).get("prepaid_credit", 0.0) or 0.0)
        if _cr_used > 0:
            events.append({
                "ts":        _ts,
                "date":      r.reading_date,
                "kind":      "prepaid",
                "type":      "Prepaid Credit",
                "ref":       f"Applied to Reading {r.reading_m3:.2f} M³",
                "dr":        0.0,
                "cr":        _cr_used,
                "sort_id":   r.id,
                "affects":   False,
                "is_credit": True,
            })

    for p in payments_asc:
        _ts = p.created_at if p.created_at else _dt.combine(_nairobi_today(), _time.min)
        _pdate = _ts.date() if hasattr(_ts, "date") else _nairobi_today()
        _ref = (p.method or "Payment").title()
        if p.reference:
            _ref += f" · {p.reference}"
        events.append({
            "ts":        _ts,
            "date":      _pdate,
            "kind":      "payment",
            "type":      "Payment",
            "ref":       _ref,
            "dr":        0.0,
            "cr":        float(p.amount_kes or 0.0),
            "sort_id":   p.id,
            "affects":   True,
            "is_credit": False,
        })

    _KIND_ORDER = {"prepaid": 0, "reading": 1, "payment": 2}
    events.sort(key=lambda e: (e["ts"], _KIND_ORDER.get(e["kind"], 99), e["sort_id"]))

    bal = 0.0
    for e in events:
        if e["affects"]:
            bal += e["dr"] - e["cr"]
        e["balance"] = round(bal, 2)

    events_desc = list(reversed(events))
    rows = events_desc[:30]

    # ── Totals ──
    total_billed = round(sum(float(r.amount_kes or 0.0) for r in readings_asc), 2)
    total_paid   = round(sum(float(p.amount_kes or 0.0) for p in payments_asc), 2)
    outstanding  = round(total_billed - total_paid, 2)   # signed — carries credit
    total_consumption_m3 = round(sum(cons_by_id.values()), 2)

    latest_r = readings_asc[-1] if readings_asc else None

    # Overall status — same source as the consumer page badge
    _newest_r = readings_asc[-1] if readings_asc else None
    _newest_disp = _disp.get(_newest_r.id, {}).get("balance") if _newest_r else None
    _disp_balances = {rid: v.get("balance", 0.0) for rid, v in _disp.items()}
    info = (get_consumer_status(list(reversed(readings_asc)),
                                latest_balance_override=_newest_disp,
                                display_balances=_disp_balances)
            if readings_asc else get_consumer_status([]))

    # Period
    if events:
        period_from = events[0]["date"].strftime("%d %b %Y")
        period_to   = events[-1]["date"].strftime("%d %b %Y")
    else:
        period_from = period_to = "—"

    snapshot = {
        "statement_no":   f"STMT-{consumer.id:06d}",
        "statement_date": _nairobi_today().strftime("%d %b %Y"),
        "period_from":    period_from,
        "period_to":      period_to,
        "cust_name":      consumer.cust_name,
        "acc_name":       consumer.acc_name,
        "meter_acc_no":   consumer.meter_acc_no,
        "contact":        consumer.contact or "—",
        "email":          consumer.email or "—",
        "address":        consumer.address or "—",
        "total_consumption_m3": f"{total_consumption_m3:.2f}",
        "latest_reading_m3":    f"{latest_r.reading_m3:.2f}" if latest_r else "—",
        "latest_reading_date":  latest_r.reading_date.strftime("%d %b %Y") if latest_r else "—",
        "status_label":         info["label"],
        "total_billed":         _fmt_money(total_billed),
        "total_paid":           _fmt_money(total_paid),
        "outstanding_balance":  _fmt_money(outstanding),
    }

    for idx in range(1, 31):
        if idx - 1 < len(rows):
            e = rows[idx - 1]
            snapshot[f"t{idx}_date"] = e["date"].strftime("%d %b %Y")
            snapshot[f"t{idx}_type"] = e["type"]
            snapshot[f"t{idx}_ref"]  = e["ref"]
            snapshot[f"t{idx}_dr"]   = _fmt_money(e["dr"]) if e["dr"] > 0 else ""
            snapshot[f"t{idx}_cr"]   = _fmt_money(e["cr"]) if e["cr"] > 0 else ""
            snapshot[f"t{idx}_bal"]  = _fmt_money(e["balance"])
            snapshot[f"t{idx}_is_credit"] = "1" if e.get("is_credit") else ""
        else:
            snapshot[f"t{idx}_date"] = ""
            snapshot[f"t{idx}_type"] = ""
            snapshot[f"t{idx}_ref"]  = ""
            snapshot[f"t{idx}_dr"]   = ""
            snapshot[f"t{idx}_cr"]   = ""
            snapshot[f"t{idx}_bal"]  = ""
            snapshot[f"t{idx}_is_credit"] = ""

    try:
        snapshot.update(_brand_context())
        pdf_buf = generate_statement_pdf(snapshot)
    except Exception as e:
        app.logger.exception("[Statement] PDF generation failed")
        return jsonify({"error": f"PDF generation failed: {e}"}), 500

    filename = f"{_safe_filename(consumer.cust_name)}_statement_{consumer.id:06d}.pdf"
    return send_file(
        pdf_buf,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=filename,
    )


@app.route("/api/consumer/<int:consumer_id>/whatsapp_bill", methods=["POST"])
def send_whatsapp_bill(consumer_id):
    """
    Build the water bill PDF for the consumer's most recent reading,
    upload it to a public URL, and deliver it via WhatsApp template.
    Admin-only.
    """
    if not _rate_limit("whatsapp_bill", 20, 60):
        return _too_many(60)

    u = _require_admin()
    if u:
        return u

    # Honor the WA_ENABLED toggle — refuse if disabled, even if the
    # request bypasses the UI.
    wa_toggle = os.environ.get("WA_ENABLED", "false").strip().lower()
    if wa_toggle not in ("1", "true", "yes", "on"):
        return jsonify({"error": "WhatsApp sending is currently disabled."}), 403

    data = request.get_json(silent=True) or {}
    send_to_alt = bool(data.get("send_to_alt"))

    consumer = _tenant_query(Consumer).filter_by(id=consumer_id).first_or_404()
    if not consumer.is_active:
        return jsonify({"error": "Consumer is terminated. No reminders sent."}), 403
    if not consumer.contact:
        return jsonify({"error": "Consumer has no contact number on file."}), 400

    # Latest reading
    latest = _all_readings(consumer.id)
    if not latest:
        return jsonify({"error": "No readings on file — cannot generate bill."}), 400
    reading = latest[0]

    # Consumption for that reading
    prev = _previous_reading(consumer.id, exclude_id=reading.id)
    prev_m3 = None
    if prev and (prev.reading_date < reading.reading_date
                 or (prev.reading_date == reading.reading_date
                     and prev.id < reading.id)):
        prev_m3 = prev.reading_m3
    consumption = compute_consumption(
        reading.reading_m3, prev_m3,
        float(consumer.meter_initial_reading_m3 or 0.0),
    )

    info = get_consumer_status(latest)

    # Build water bill snapshot (same shape as /bill.pdf endpoint)
    readings_desc = _all_readings(consumer.id)
    older_readings = readings_desc[1:]   # latest is readings_desc[0]
    previous_m3 = (
        older_readings[0].reading_m3 if older_readings
        else float(consumer.meter_initial_reading_m3 or 0.0)
    )
    current_charges = float(reading.amount_kes or 0.0)

    # Carry-forward display values for the billed reading
    _asc = list(reversed(readings_desc))
    _pays = _consumer_payments(consumer.id)
    _disp_map = _carry_forward_display(_asc, _pays)
    _d = _disp_map.get(reading.id, {})

    # Total Outstanding = signed net position (Option B)
    total_outstanding = round(sum(r.balance for r in readings_desc), 2)
    current_display_balance = _d.get("balance", reading.balance)
    # Previous Outstanding = Total − current bill's display balance
    prev_outstanding = round(total_outstanding - current_display_balance, 2)

    _pay = _payment_info(consumer, tenant=g.tenant)
    snapshot = {
        "bill_no":              f"{reading.id:06d}",
        "date":                 reading.reading_date.strftime("%d %b %Y"),
        "bill_month":           reading.reading_date.strftime("%B %Y"),
        "cust_name":            consumer.cust_name,
        "meter_acc_no":         consumer.meter_acc_no,
        "reading_m3":           f"{reading.reading_m3:.2f}",
        "previous_reading":     f"{previous_m3:.2f}",
        "amount_kes":           _fmt_money(current_charges),
        "payment_carried_forward": f"KES {_fmt_money(_d.get('prepaid_credit', 0.0))}",
        "paid":                 f"KES {_fmt_money(_d.get('paid', 0.0))}",
        "previous_outstanding": _fmt_money(prev_outstanding),
        "total_outstanding":    _fmt_money(total_outstanding),
        "status_label":         _d.get("status", get_reading_bill_status(reading)).upper(),
        "paybill":              _pay["paybill"],
        "payment_account":      _pay["account"],
    }

    try:
        snapshot.update(_brand_context())
        snapshot["template_r2_key"] = _resolve_template_key(g.tenant, "water_bill")
        pdf_buf = generate_water_bill_pdf(snapshot)
    except Exception as e:
        app.logger.exception("[WhatsApp Bill] PDF generation failed")
        return jsonify({"error": f"PDF generation failed: {e}"}), 500

    # Save PDF into a token-addressed public slot.
    # Reuse the receipt token machinery but with a dedicated store.
    token = secrets.token_urlsafe(32)
    pdf_bytes = bytes(pdf_buf.getvalue())
    _PUBLIC_BILL_TOKENS[token] = pdf_bytes

    public_url = request.host_url.rstrip("/") + f"/public/bill/{token}.pdf"
    filename = f"{_safe_filename(consumer.cust_name)}_bill_{reading.id:06d}.pdf"

    # ── Build recipients list — main + alt (if requested & available) ──
    recipients = [consumer.contact]
    if send_to_alt and consumer.alt_contact:
        recipients.append(consumer.alt_contact)

    # ── Send to each recipient ──
    results = []
    for phone in recipients:
        r = send_whatsapp_document(
            to_phone=phone,
            pdf_public_url=public_url,
            filename=filename,
            body_params=[consumer.cust_name, consumer.meter_acc_no],
        )
        results.append({"phone": phone, "ok": r.get("ok"),
                        "error": r.get("error") or "",
                        "provider_id": r.get("provider_id", "")})

    sent_phones = [r["phone"] for r in results if r["ok"]]
    failed      = [r for r in results if not r["ok"]]
    overall_ok  = len(failed) == 0
    first_error = failed[0]["error"] if failed else ""
    to_value    = ", ".join(sent_phones) if sent_phones \
                  else ", ".join(r["phone"] for r in results)

    # ── Best-effort archive: PDF to R2, metadata to D1 ──
    # Wrapped in try/except — an archive failure must NEVER break the send.
    try:
        sent_at = datetime.utcnow()
        for r in results:
            key = None
            if r["ok"] and archive.is_configured():
                key = archive.make_key(consumer.id, reading.id, sent_at,
                                            tenant_slug=_tenant_slug())
                if not archive.upload_pdf(key, pdf_bytes):
                    key = None
            row = SentBillArchive(
                consumer_id=consumer.id,
                tenant_id=consumer.tenant_id,
                reading_id=reading.id,
                channel="whatsapp",
                kind="bill_pdf",
                recipient_phone=r["phone"],
                template_name=os.environ.get("WA_TEMPLATE_NAME", ""),
                provider_message_id=r.get("provider_id", ""),
                sent_at=sent_at,
                status="sent" if r["ok"] else "failed",
                error=(r["error"] or None) if not r["ok"] else None,
                body_text=None,
                storage_key=key,
                file_size=len(pdf_bytes),
                snapshot_json=json.dumps(snapshot),
            )
            db.session.add(row)
        db.session.commit()
        archive.purge_expired()
    except Exception:
        app.logger.exception("[Archive] bill_pdf insert failed")
        db.session.rollback()

    _audit("send_whatsapp_bill",
           target_type="consumer", target_id=consumer.id,
           target_label=f"{consumer.cust_name} · {consumer.meter_acc_no}",
           detail=f"to={to_value}, ok={overall_ok}, recipients={len(results)}, "
                  f"err={(first_error or '')[:80]}")

    if overall_ok:
        return jsonify({
            "ok": True,
            "channel": "whatsapp",
            "message": "WhatsApp bill sent successfully.",
            "to": to_value,
            "recipients_sent": len(sent_phones),
        }), 200

    return jsonify({
        "ok": False,
        "error": first_error or "Send failed",
    }), 502


@app.route("/public/bill/<token>.pdf")
def public_bill_pdf(token):
    """Serve a token-addressed water bill PDF (no auth)."""
    data = _PUBLIC_BILL_TOKENS.get(token)
    if not data:
        return "Not found.", 404
    from io import BytesIO
    return send_file(BytesIO(data), mimetype="application/pdf",
                     as_attachment=False,
                     download_name="SimonWater_Bill.pdf")


@app.route("/api/reading", methods=["POST"])
def submit_reading():
    if not _rate_limit("reading_post", 60, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    consumer_id = data.get("consumer_id")
    reading_m3 = data.get("reading_m3")
    if not consumer_id or reading_m3 is None:
        return jsonify({"error": "consumer_id and reading_m3 are required"}), 400
    try:
        reading_m3 = float(reading_m3)
    except (ValueError, TypeError):
        return jsonify({"error": "reading_m3 must be a number"}), 400
    if reading_m3 < 0 or reading_m3 > 1_000_000:
        return jsonify({"error": "reading_m3 out of range"}), 400

    consumer = _tenant_query(Consumer).filter_by(id=consumer_id).first()
    if not consumer:
        return jsonify({"error": "Consumer not found"}), 404
    if not consumer.is_active:
        return jsonify({"error": "Consumer is terminated. Reactivate to submit readings."}), 403

    initial = float(consumer.meter_initial_reading_m3 or 0.0)
    prev = _previous_reading(consumer.id)
    prev_m3 = prev.reading_m3 if prev else None
    baseline = prev_m3 if prev_m3 is not None else initial

    if reading_m3 <= baseline:
        if prev_m3 is not None:
            err = (f"New reading must be greater than the previous reading "
                   f"({prev_m3} M³). You entered {reading_m3} M³.")
        else:
            err = (f"New reading must be greater than the initial meter reading "
                   f"({initial} M³). You entered {reading_m3} M³.")
        return jsonify({"error": err}), 400

    consumption = compute_consumption(reading_m3, prev_m3, initial)
    amount = compute_amount(reading_m3, prev_m3, initial)

    new_reading = MeterReading(
        consumer_id=consumer.id, reading_m3=reading_m3,
        reading_date=_nairobi_today(), amount_kes=amount, amount_paid=0.0,
        tenant_id=consumer.tenant_id,
    )
    db.session.add(new_reading)
    db.session.commit()

    try:
        latest = (MeterReading.query.filter_by(consumer_id=consumer.id)
                  .order_by(MeterReading.reading_date.desc(),
                            MeterReading.id.desc()).limit(5).all())
        sync_consumer_to_sheet(consumer, latest,
                               spreadsheet_name=_tenant_sheet_name())
    except Exception as e:
        app.logger.warning(f"[Sheets sync] {e}")

    _audit("add_reading",
           target_type="consumer", target_id=consumer.id,
           target_label=f"{consumer.cust_name} · {consumer.meter_acc_no}",
           detail=f"reading={new_reading.reading_m3:.2f} M³, "
                  f"consumption={consumption:.2f} M³, "
                  f"amount=KES {new_reading.amount_kes:.2f}")

    return jsonify({"message": "Reading recorded", "reading": {
        "id": new_reading.id, "reading_m3": new_reading.reading_m3,
        "consumption_m3": consumption,
        "reading_date": new_reading.reading_date.isoformat(),
        "amount_kes": new_reading.amount_kes, "balance": new_reading.balance,
    }}), 201


@app.route("/api/consumer/<int:consumer_id>/notify", methods=["POST"])
def notify_consumer(consumer_id):
    if not _rate_limit("notify", 20, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    channel = (data.get("channel") or "").lower().strip()
    if channel not in ("sms", "whatsapp"):
        return jsonify({"error": "channel must be 'sms' or 'whatsapp'"}), 400
    send_to_alt = bool(data.get("send_to_alt"))

    consumer = _tenant_query(Consumer).filter_by(id=consumer_id).first_or_404()
    if not consumer.is_active:
        return jsonify({"error": "Consumer is terminated. No reminders sent."}), 403

    _all_readings_list = _all_readings(consumer.id)
    _disp_latest = _display_latest_balance(
        list(reversed(_all_readings_list)),
        _consumer_payments(consumer.id),
    )
    info = get_consumer_status(_all_readings_list,
                               latest_balance_override=_disp_latest)
    if info["status"] not in ("DUE", "OVERDUE"):
        return jsonify({"error": "No outstanding balance — nothing to notify.",
                        "status": info["status"]}), 400

    # Total M3 consumed across all readings whose bill is not yet fully paid
    info["consumption_m3"] = _total_uncleared_consumption(consumer)

    # ─── Current bill breakdown (for the SMS) ───
    readings_desc = _all_readings(consumer.id)   # DESC
    if readings_desc:
        latest = readings_desc[0]
        previous = readings_desc[1] if len(readings_desc) > 1 else None
        previous_m3 = (
            previous.reading_m3 if previous
            else float(consumer.meter_initial_reading_m3 or 0.0)
        )
        current_charges = float(latest.amount_kes or 0.0)

        # Carry-forward display values for the billed (newest) reading
        _asc = list(reversed(readings_desc))
        _pays = _consumer_payments(consumer.id)
        _disp_map = _carry_forward_display(_asc, _pays)
        _d = _disp_map.get(latest.id, {})

        # Total Outstanding = signed net position (Option B)
        total_out = round(sum(r.balance for r in readings_desc), 2)
        current_display_balance = _d.get("balance", latest.balance)
        # Previous Outstanding = Total − current bill's display balance
        prev_out = round(total_out - current_display_balance, 2)

        info["current_reading_m3"]    = latest.reading_m3
        info["previous_reading_m3"]   = previous_m3
        info["current_charges"]       = current_charges
        info["paid"]                  = _d.get("paid", latest.amount_paid)
        info["payment_carried_forward"] = _d.get("prepaid_credit", 0.0)
        info["previous_outstanding"]  = prev_out
        info["total_outstanding"]     = total_out
        info["bill_month"]            = latest.reading_date.strftime("%B %Y")
        info["is_current_cleared"]    = current_display_balance <= 0

    cutoff = datetime.utcnow() - timedelta(seconds=NOTIFY_COOLDOWN_SECONDS)
    recent = (NotificationLog.query
              .filter_by(consumer_id=consumer.id, channel=channel, status="sent")
              .filter(NotificationLog.sent_at >= cutoff)
              .order_by(NotificationLog.sent_at.desc()).first())
    if recent:
        elapsed = int((datetime.utcnow() - recent.sent_at).total_seconds())
        wait = max(NOTIFY_COOLDOWN_SECONDS - elapsed, 1)
        return jsonify({"error": f"Please wait {wait}s before resending via {channel}."}), 429

    payload = build_bill_message(consumer, info, channel=channel, tenant=g.tenant)

    # ── Build recipients list — main + alt (if requested & available) ──
    recipients = []
    if consumer.contact:
        recipients.append(consumer.contact)
    if send_to_alt and consumer.alt_contact:
        recipients.append(consumer.alt_contact)
    if not recipients:
        return jsonify({"error": "Consumer has no contact number on file."}), 400

    # ── Send to each recipient ──
    results = []
    for phone in recipients:
        if channel == "sms":
            r = send_sms(phone, payload["body"])
        else:
            r = send_whatsapp(phone, payload["body"])
        results.append({"phone": phone, "ok": r.get("ok"),
                        "error": r.get("error") or "",
                        "provider_id": r.get("provider_id", "")})

    sent_phones = [r["phone"] for r in results if r["ok"]]
    failed      = [r for r in results if not r["ok"]]
    overall_ok  = len(failed) == 0
    first_error = failed[0]["error"] if failed else ""
    to_value    = ", ".join(sent_phones) if sent_phones \
                  else ", ".join(r["phone"] for r in results)

    # ── One NotificationLog per recipient ──
    for r in results:
        log = NotificationLog(
            consumer_id=consumer.id, channel=channel,
            status="sent" if r["ok"] else "failed",
            detail=((r["error"] or r["provider_id"]) or "")[:255],
        )
        db.session.add(log)
    db.session.commit()

    # ── One archive row per recipient ──
    try:
        for r in results:
            arch_row = SentBillArchive(
                consumer_id=consumer.id,
                tenant_id=consumer.tenant_id,
                reading_id=None,
                channel=channel,
                kind="reminder_text",
                recipient_phone=r["phone"],
                template_name=None,
                provider_message_id=r.get("provider_id", ""),
                sent_at=datetime.utcnow(),
                status="sent" if r["ok"] else "failed",
                error=(r["error"] or None) if not r["ok"] else None,
                body_text=payload.get("body") or "",
                storage_key=None,
                file_size=None,
                snapshot_json=None,
            )
            db.session.add(arch_row)
        db.session.commit()
        archive.purge_expired()
    except Exception:
        app.logger.exception("[Archive] reminder_text insert failed")
        db.session.rollback()

    _audit(f"send_{channel}",
           target_type="consumer", target_id=consumer.id,
           target_label=f"{consumer.cust_name} · {consumer.meter_acc_no}",
           detail=f"to={to_value}, ok={overall_ok}, recipients={len(results)}, "
                  f"err={(first_error or '')[:80]}")

    if overall_ok:
        return jsonify({"ok": True, "channel": channel,
                        "message": f"{channel.upper()} reminder sent.",
                        "to": to_value,
                        "recipients_sent": len(sent_phones)}), 200
    return jsonify({"ok": False, "error": first_error or "Send failed"}), 502


# ═══════════════════════════════════════════════
#  ADMIN AUTH
# ═══════════════════════════════════════════════

def _dispense_recorded_by():
    """Username to stamp on a dispensing write. If session has a valid
    recording_as target (an active employee of the current admin's tenant),
    return that; otherwise return the current admin's own username.
    Self-heals stale toggles by popping session["recording_as"]."""
    base = session.get("admin_username") or "?"
    target = session.get("recording_as")
    if not target or target == base:
        return base
    try:
        cur = _current_admin()
        if not cur or not cur.tenant_id:
            session.pop("recording_as", None)
            return base
        t = (AdminUser.query
             .filter_by(username=target, is_active=True,
                        user_role="employee", tenant_id=cur.tenant_id)
             .first())
        if not t:
            session.pop("recording_as", None)
            return base
        return t.username
    except Exception:
        session.pop("recording_as", None)
        return base


@app.route("/api/admin/whoami")
def admin_whoami():
    setup_required = _tenant_query(AdminUser).count() == 0
    admin = _current_admin()
    if admin:
        _prof = _current_active_profile()
        _enabled = _tenant_profiles_dict(admin.tenant_id) if admin.tenant_id else {"billing": True, "dispensing": False}
        return jsonify({
            "authenticated": True,
            "username": admin.username,
            "user_role": session.get("user_role", "admin"),
            "setup_required": False,
            "active_profile": _prof,
            "enabled_profiles": _enabled,
            "recording_as": _dispense_recorded_by(),
            "permissions": (_employee_permissions(admin.id)
                            if session.get("user_role", "admin") == "employee"
                            else None),
        })
    return jsonify({"authenticated": False, "setup_required": setup_required})


@app.route("/api/admin/setup", methods=["POST"])
def admin_setup_route():
    if not _rate_limit("setup", 10, 60):
        return _too_many(60)

    _tid_setup = _write_tenant_id()
    if _tid_setup < 0:
        return jsonify({"ok": False, "error": "No tenant context for this request."}), 400
    data = request.get_json(silent=True) or {}
    result = create_first_admin(
        username=data.get("username", ""),
        password=data.get("password", ""),
        phone=data.get("phone", ""),
        email=data.get("email"),
        setup_code=data.get("setup_code", ""),
        tenant_id=_tid_setup,
    )
    if result.get("ok"):
        # Preserve the tenant slug so the admin lands on their own portal
        # after the setup flow completes. Fall back to the tenant id used
        # to create the admin when the session has no slug yet.
        _slug = session.get("__tenant_slug")
        if not _slug:
            try:
                _t = Tenant.query.get(_tid_setup)
                if _t:
                    _slug = _t.slug
            except Exception:
                pass
        session.clear()
        session.permanent = True
        session["admin_id"] = result["admin_id"]
        session["admin_username"] = result["username"]
        session["last_activity"] = datetime.utcnow().isoformat()
        if _slug:
            session["__tenant_slug"] = _slug
        return jsonify({"ok": True, "username": result["username"]}), 200
    return jsonify(result), 400


@app.route("/api/admin/login", methods=["POST"])
def admin_login_route():
    if not _rate_limit("login", 20, 60):
        return _too_many(60)

    # Login is not tenant-scoped — the username is globally unique, so a
    # super-admin can sign in from any URL without being rejected by a
    # stale session or a mismatched ?__tenant=. The session slug is set
    # below from the authenticated admin's own tenant_id.
    data = request.get_json(silent=True) or {}
    result = admin_login_fn(data.get("username", ""), data.get("password", ""))
    if not result.get("ok"):
        # Structured audit: capture the resolved tenant, the URL tenant,
        # and (if the username exists) which tenant the admin actually
        # belongs to. This makes a repeat of the earlier cross-tenant
        # bug diagnosable from one audit query instead of ad-hoc debug
        # endpoints.
        _attempted = _safe_username_str(data.get("username"))
        _resolved = getattr(g, "tenant", None)
        _resolved_slug = _resolved.slug if _resolved else "—"
        _url_slug = _sanitize_slug(request.args.get("__tenant") or "") or "—"
        _reason = (result.get("error") or "")[:60]
        _true_tenant = "—"
        try:
            _u = AdminUser.query.filter_by(username=_attempted).first()
            if _u and _u.tenant_id:
                _t = Tenant.query.get(_u.tenant_id)
                if _t:
                    _true_tenant = _t.slug
        except Exception:
            pass
        _audit("login_failed",
               detail=(f"attempted_username={_attempted[:40]} "
                       f"resolved_tenant={_resolved_slug} "
                       f"url_tenant={_url_slug} "
                       f"admin_tenant={_true_tenant} "
                       f"reason={_reason}"))
        return jsonify({"ok": False, "error": result["error"]}), 401

    # ── Tenant enforcement ──
    # Usernames are globally unique in the model, so login() authenticates
    # by username alone. That is deliberate (it fixed the earlier cross-
    # tenant login bug) — but it also means a valid demo admin could sign
    # in from Simon Fresh Water's URL and vice versa. This block closes
    # that: if the POST carried ?__tenant=<slug>, the authenticated
    # admin must belong to that tenant.
    #
    # The refusal message matches a wrong password exactly, so an attacker
    # cannot learn which tenant a username belongs to by probing URLs.
    _requested_slug = _sanitize_slug(request.args.get("__tenant") or "").strip()

    # Compute the tenant slug the session must carry after login.
    # The admin's own tenant_id is the definitive source — this makes the
    # flow robust even if the login POST arrives without ?__tenant=.
    _admin_slug = _DEFAULT_TENANT_SLUG
    try:
        _admin_row = AdminUser.query.get(result["admin_id"])
        if _admin_row and _admin_row.tenant_id:
            _t_row = Tenant.query.get(_admin_row.tenant_id)
            if _t_row:
                _admin_slug = _t_row.slug
    except Exception:
        pass

    # Determine the initial profile (billing or dispensing) from the
    # admin's OWN tenant_id (not the URL tenant). Defaults to billing
    # if anything goes wrong — matches every existing tenant's state.
    _initial_profile = "billing"
    _init_role = "admin"
    try:
        _init_ar = AdminUser.query.get(result["admin_id"])
        if _init_ar and _init_ar.tenant_id:
            _initial_profile = _initial_profile_for(_init_ar.tenant_id)
            _init_role = getattr(_init_ar, "user_role", "admin") or "admin"
            # Employees are restricted to the dispensing profile
            if _init_role == "employee":
                _initial_profile = "dispensing"
    except Exception:
        pass

    if _requested_slug and _admin_slug != _requested_slug:
        _audit("login_failed_tenant_mismatch",
               detail=(f"attempted_username={_safe_username_str(data.get('username'))[:40]} "
                       f"requested_tenant={_requested_slug} "
                       f"admin_tenant={_admin_slug}"))
        return jsonify({"ok": False, "error": "Invalid credentials."}), 401

    # ── 2FA branch ──
    two_fa_on = os.environ.get("ADMIN_2FA_ENABLED", "false").strip().lower() in ("1","true","yes","on")
    if two_fa_on:
        admin = _tenant_query(AdminUser).filter_by(id=result["admin_id"]).first()
        if not admin or not admin.phone:
            return jsonify({"ok": False,
                            "error": "2FA is enabled but no phone is configured for this admin."}), 500

        send = generate_login_otp(admin)
        if not send.get("ok"):
            return jsonify({"ok": False, "error": send.get("error")}), 502

        # Do NOT establish a full session yet — mark as "pending 2FA"
        session.clear()
        session.permanent = True
        session["pending_2fa_admin_id"] = admin.id
        session["last_activity"] = datetime.utcnow().isoformat()
        session["__tenant_slug"] = _admin_slug
        session["active_profile"] = _initial_profile
        session["user_role"] = _init_role

        return jsonify({
            "ok": True,
            "requires_2fa": True,
            "message": send.get("message"),
            "user_role": _init_role,
        }), 200

    # No 2FA — full session immediately (existing behaviour)
    session.clear()
    session.permanent = True
    session["admin_id"] = result["admin_id"]
    session["admin_username"] = result["username"]
    session["last_activity"] = datetime.utcnow().isoformat()
    session["login_at"] = datetime.utcnow().isoformat()
    session["__tenant_slug"] = _admin_slug
    session["active_profile"] = _initial_profile
    session["user_role"] = _init_role
    _audit("login")
    return jsonify({
        "ok": True,
        "username": result["username"],
        "user_role": _init_role,
        "redirect_to": "/dispense" if _init_role == "employee" else "/",
    }), 200


@app.route("/api/admin/login/verify-otp", methods=["POST"])
def admin_login_verify_otp():
    if not _rate_limit("login_verify", 20, 60):
        return _too_many(60)

    pending_id = session.get("pending_2fa_admin_id")
    if not pending_id:
        return jsonify({"ok": False, "error": "No pending login. Please sign in again."}), 401

    admin = _tenant_query(AdminUser).filter_by(id=pending_id).first()
    if not admin or not admin.is_active:
        session.clear()
        return jsonify({"ok": False, "error": "Invalid session."}), 401

    data = request.get_json(silent=True) or {}
    result = verify_login_otp(admin, data.get("otp", ""))
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error")}), 401

    # Promote the pending session to a full authenticated session
    session.pop("pending_2fa_admin_id", None)
    session["admin_id"] = admin.id
    session["admin_username"] = admin.username
    session["last_activity"] = datetime.utcnow().isoformat()
    session["login_at"] = datetime.utcnow().isoformat()
    _audit("login_2fa_ok")
    _verify_role = getattr(admin, "user_role", "admin") or "admin"
    return jsonify({
        "ok": True,
        "username": admin.username,
        "user_role": _verify_role,
        "redirect_to": "/dispense" if _verify_role == "employee" else "/",
    }), 200


@app.route("/api/admin/login/resend-otp", methods=["POST"])
def admin_login_resend_otp():
    if not _rate_limit("login_resend", 5, 60):
        return _too_many(60)

    pending_id = session.get("pending_2fa_admin_id")
    if not pending_id:
        return jsonify({"ok": False, "error": "No pending login. Please sign in again."}), 401

    admin = _tenant_query(AdminUser).filter_by(id=pending_id).first()
    if not admin or not admin.is_active:
        return jsonify({"ok": False, "error": "Invalid session."}), 401

    send = generate_login_otp(admin)
    if not send.get("ok"):
        return jsonify({"ok": False, "error": send.get("error")}), 429

    return jsonify({"ok": True, "message": send.get("message")}), 200


@app.route("/api/admin/profile/switch", methods=["POST"])
def admin_profile_switch():
    u = _require_admin()
    if u: return u
    if not _rate_limit("profile_switch", 30, 60):
        return _too_many(60)
    admin = _current_admin()
    if not admin or not admin.tenant_id:
        return jsonify({"error": "Unauthorized."}), 401
    enabled = _tenant_profiles_dict(admin.tenant_id)
    data = request.get_json(silent=True) or {}
    target = (data.get("to") or "").strip().lower()
    if target not in ("billing", "dispensing"):
        return jsonify({"error": "Invalid target."}), 400
    if not enabled.get(target):
        # Neutral 403 — never reveal what profiles exist for this tenant.
        return jsonify({"error": "Profile not available for this tenant."}), 403
    _from = session.get("active_profile") or "?"
    session["active_profile"] = target
    _audit("profile_switched",
           target_type="tenant", target_id=admin.tenant_id,
           detail=f"from={_from}, to={target}")
    return jsonify({
        "ok": True,
        "active_profile": target,
        "redirect": "/dispense" if target == "dispensing" else "/",
    })


# ═══════════════════════════════════════════════
#  DISPENSING — READ-ONLY VIEWS (Phase D)
# ═══════════════════════════════════════════════

def _dispense_tenant_or_error():
    """Return (tenant_id, None) or (None, error_response).
    Guards: authenticated admin + dispensing profile active + dispensing enabled."""
    admin = _current_admin()
    if not admin or not admin.tenant_id:
        return None, (jsonify({"error": "Unauthorized."}), 401)
    if _current_active_profile() != "dispensing":
        return None, (jsonify({"error": "Not on dispensing profile."}), 403)
    enabled = _tenant_profiles_dict(admin.tenant_id)
    if not enabled.get("dispensing"):
        return None, (jsonify({"error": "Dispensing not enabled for this tenant."}), 403)
    return admin.tenant_id, None


def _dispense_window_stats(tenant_id, dates):
    """Bulk-compute dispensing metrics for a list of date objects.
    Returns {iso_date: stats_dict}. Three queries total regardless of window size."""
    if not dates:
        return {}
    readings = {r.reading_date.isoformat(): r for r in
                DispenseMeterReading.query
                .filter(DispenseMeterReading.tenant_id == tenant_id,
                        DispenseMeterReading.reading_date.in_(dates))
                .all()}
    sales_by_date = {}
    for row in (DispenseSale.query
                .filter(DispenseSale.tenant_id == tenant_id,
                        DispenseSale.sale_date.in_(dates),
                        DispenseSale.voided == False)  # noqa: E712
                .all()):
        sales_by_date.setdefault(row.sale_date.isoformat(), []).append(row)
    payments_by_date = {}
    for row in (DispensePayment.query
                .filter(DispensePayment.tenant_id == tenant_id,
                        DispensePayment.payment_date.in_(dates),
                        DispensePayment.voided == False)  # noqa: E712
                .all()):
        payments_by_date.setdefault(row.payment_date.isoformat(), []).append(row)

    out = {}
    for d in dates:
        iso = d.isoformat()
        reading = readings.get(iso)
        sales = sales_by_date.get(iso, [])
        payments = payments_by_date.get(iso, [])
        sales_kes = round(sum(float(r.total_kes or 0) for r in sales), 2)
        sold_l = sum(int(r.container_size_L or 0) * int(r.quantity or 0) for r in sales)
        paid_kes = round(sum(float(p.amount_kes or 0) for p in payments), 2)
        consumption_m3 = round(float(reading.consumption_m3 or 0), 4) if reading else 0.0
        consumption_l = int(round(consumption_m3 * 1000))
        out[iso] = {
            "reading": reading,
            "consumption_m3": consumption_m3,
            "consumption_l": consumption_l,
            "sold_l": sold_l,
            "sales_kes": sales_kes,
            "paid_kes": paid_kes,
            "outstanding_kes": round(sales_kes - paid_kes, 2),
            "variance_l": consumption_l - sold_l,
            "has_reading": reading is not None,
        }
    return out


def _dispense_status(stats):
    if not stats["has_reading"]:
        return "No reading"
    if stats["outstanding_kes"] > 0.01:
        return "Due"
    if stats["outstanding_kes"] < -0.01:
        return "Overpaid"
    return "Cleared"


# ═══════════════════════════════════════════════
#  DISPENSING — DAILY RECONCILIATION SMS (A2)
# ═══════════════════════════════════════════════
# Manual, on-demand. Fired by the "Send" button on each qualifying row
# of the 7-day table. No scheduler — the operator decides when to notify.

def _fmt_kes_sms(v):
    """Compact KES for SMS: whole numbers lose the decimals, else 2 dp."""
    try:
        n = float(v or 0)
    except (ValueError, TypeError):
        n = 0.0
    if abs(n - round(n)) < 0.005:
        return f"{int(round(n)):,}"
    return f"{n:,.2f}"


def _fmt_l_sms(v):
    """Compact litres for SMS: integer, comma thousands."""
    try:
        return f"{int(round(float(v or 0))):,}"
    except (ValueError, TypeError):
        return "0"


def _dispense_recon_sms_text(tenant, st, target_date):
    """Build one SMS for a day's dispensing reconciliation.
    Target ~130 chars so it usually fits one SMS segment. SMSGate
    handles multipart if a value ever pushes past 160."""
    label = ""
    if tenant:
        label = ((tenant.short_name or "").strip() or
                 (tenant.org_name or "").strip())[:14]
    dd_mm = target_date.strftime("%d/%m")
    cons = _fmt_l_sms(st["consumption_l"])
    sold = _fmt_l_sms(st["sold_l"])
    var  = int(round(float(st["variance_l"] or 0)))
    var_s = f"+{var:,}" if var > 0 else (f"{var:,}" if var < 0 else "0")
    sales = _fmt_kes_sms(st["sales_kes"])
    paid  = _fmt_kes_sms(st["paid_kes"])
    out   = _fmt_kes_sms(st["outstanding_kes"])
    return (
        f"[{label}] Recon {dd_mm}:\n"
        f"Meter {cons}L | Sold {sold}L | Var {var_s}L\n"
        f"Sales {sales} | Paid {paid} | Due {out}\n"
        f"Detail: dispensing portal"
    )


@app.route("/api/admin/dispense/reconciliation/send", methods=["POST"])
def admin_dispense_reconciliation_send():
    """Send a reconciliation SMS to all active admins of the current tenant
    for a given day (must be within the last 7 days, must have a reading).
    Manual only — no scheduler fires this."""
    u = _require_admin()
    if u: return u
    if not _rate_limit("dispense_recon_send", 10, 60):
        return _too_many(60)
    tenant_id, err = _dispense_tenant_or_error()
    if err: return err

    date_str = (request.args.get("date") or "").strip()
    if not date_str:
        return jsonify({"error": "date query parameter required (YYYY-MM-DD)."}), 400
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid date format (YYYY-MM-DD expected)."}), 400

    today = _nairobi_today()
    earliest = today - timedelta(days=6)
    if target < earliest or target > today:
        return jsonify({"error": "Date must be within the last 7 days."}), 400

    stats_map = _dispense_window_stats(tenant_id, [target])
    st = stats_map[target.isoformat()]
    if not st["has_reading"]:
        return jsonify({"error": "This day has no closing reading recorded."}), 400

    tenant = Tenant.query.get(tenant_id)
    sms_text = _dispense_recon_sms_text(tenant, st, target)
    date_display = target.strftime("%d %b %Y")

    admins = (AdminUser.query
              .filter_by(tenant_id=tenant_id, is_active=True)
              .order_by(AdminUser.id.asc())
              .all())
    if not admins:
        return jsonify({"error": "No active admins for this tenant."}), 400

    results = []
    for a in admins:
        if not a.phone:
            results.append({"username": a.username, "ok": False,
                            "error": "no phone on file"})
            continue
        r = send_sms_with_retry(a.phone, sms_text, max_attempts=2)
        tail = a.phone[-4:] if len(a.phone) >= 4 else "****"
        results.append({
            "username": a.username,
            "phone_tail": tail,
            "ok": bool(r.get("ok")),
            "provider_id": r.get("provider_id"),
            "error": r.get("error"),
        })

    sent   = sum(1 for r in results if r["ok"])
    failed = len(results) - sent

    try:
        _audit("dispense_reconciliation_sent",
               target_type="tenant", target_id=tenant_id,
               detail=(f"date={date_str} sent={sent} failed={failed} "
                       f"variance_l={st['variance_l']} "
                       f"outstanding_kes={st['outstanding_kes']}"))
    except Exception:
        pass

    status = 200 if sent > 0 else 502
    return jsonify({
        "ok": sent > 0,
        "date_iso": date_str,
        "date_display": date_display,
        "sms_text": sms_text,
        "recipients": results,
        "sent_count": sent,
        "failed_count": failed,
    }), status


@app.route("/api/admin/dispense/today", methods=["GET"])
def admin_dispense_today():
    u = _require_admin()
    if u: return u
    if not _rate_limit("dispense_today", 60, 60):
        return _too_many(60)
    tenant_id, err = _dispense_tenant_or_error()
    if err: return err

    today = _nairobi_today()
    yesterday = today - timedelta(days=1)
    stats_map = _dispense_window_stats(tenant_id, [today, yesterday])
    tstats = stats_map[today.isoformat()]
    ystats = stats_map[yesterday.isoformat()]

    any_reading_ever = (DispenseMeterReading.query
                        .filter_by(tenant_id=tenant_id)
                        .first())
    any_before_yesterday = (DispenseMeterReading.query
                            .filter(DispenseMeterReading.tenant_id == tenant_id,
                                    DispenseMeterReading.reading_date < yesterday)
                            .first())

    is_first_day = (any_reading_ever is None) and (not tstats["has_reading"])
    prev_day_missing = (not ystats["has_reading"]
                        and any_before_yesterday is not None)

    # Determine whether the admin must supply the opening reading manually.
    # Both "first day of dispensing" and "yesterday's reading missing" require it.
    manual_opening_required = is_first_day or prev_day_missing

    # For the normal case, expose the auto-computed opening (previous day's
    # closing, or the most recent prior reading's closing).
    suggested_opening = None
    if not manual_opening_required and not tstats["has_reading"]:
        prev_reading = (DispenseMeterReading.query
                        .filter(DispenseMeterReading.tenant_id == tenant_id,
                                DispenseMeterReading.reading_date < today)
                        .order_by(DispenseMeterReading.reading_date.desc())
                        .first())
        if prev_reading:
            suggested_opening = float(prev_reading.closing_reading_m3)

    reading_id = tstats["reading"].id if tstats["reading"] else None

    return jsonify({
        "ok": True,
        "date": today.strftime("%d %b %Y"),
        "date_iso": today.isoformat(),
        "reading_id": reading_id,
        "opening_reading_m3": (float(tstats["reading"].opening_reading_m3)
                               if tstats["reading"] else None),
        "closing_reading_m3": (float(tstats["reading"].closing_reading_m3)
                               if tstats["reading"] else None),
        "consumption_m3": tstats["consumption_m3"],
        "consumption_l": tstats["consumption_l"],
        "sold_l": tstats["sold_l"],
        "sales_kes": tstats["sales_kes"],
        "paid_kes": tstats["paid_kes"],
        "outstanding_kes": tstats["outstanding_kes"],
        "variance_l": tstats["variance_l"],
        "has_reading": tstats["has_reading"],
        "is_first_day": is_first_day,
        "prev_day_missing": prev_day_missing,
        "manual_opening_required": manual_opening_required,
        "suggested_opening_m3": suggested_opening,
    })


@app.route("/api/admin/dispense/reading", methods=["POST"])
def admin_dispense_reading_post():
    """Record or correct today's dispensing meter reading.

    Phase E accepts TODAY only. Past-day corrections (with reason field
    fully wired) arrive in a follow-up. The opening reading is auto-computed
    from the previous reading's closing, EXCEPT on the first day or when
    yesterday's reading is missing — in those cases the admin must supply it.

    Corrections of an existing reading require force=true AND a reason."""
    u = _require_admin()
    if u: return u
    if not _rate_limit("dispense_reading_post", 20, 60):
        return _too_many(60)
    tenant_id, err = _dispense_tenant_or_error()
    if err: return err

    data = request.get_json(silent=True) or {}

    # ── 1. Parse reading_date (default = today) ──
    date_str = (data.get("reading_date") or "").strip()
    if date_str:
        try:
            requested = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid reading_date."}), 400
    else:
        requested = _nairobi_today()

    today = _nairobi_today()
    if requested > today:
        return jsonify({"error": "Reading date cannot be in the future."}), 400
    if requested != today:
        return jsonify({
            "error": "Only today's reading can be recorded in Phase E. "
                     "Past-day corrections arrive in a follow-up."
        }), 400

    # ── 2. Parse closing_reading_m3 ──
    try:
        closing = float(data.get("closing_reading_m3"))
    except (ValueError, TypeError):
        return jsonify({"error": "closing_reading_m3 must be a number."}), 400
    if closing < 0 or closing > 10_000_000:
        return jsonify({"error": "Closing reading is out of range."}), 400

    # ── 3. Check for existing reading today ──
    existing = (DispenseMeterReading.query
                .filter_by(tenant_id=tenant_id, reading_date=requested)
                .first())
    force  = bool(data.get("force"))
    reason = (data.get("reason") or "").strip()

    if existing is not None:
        if not force:
            return jsonify({
                "error": "Reading already exists for today. "
                         "Set force=true with a reason to correct it.",
                "existing_reading_id": existing.id,
            }), 409
        if not reason:
            return jsonify({
                "error": "A reason is required when correcting an existing reading."
            }), 400

    # ── 4. Determine whether opening must be supplied manually ──
    previous = (DispenseMeterReading.query
                .filter(DispenseMeterReading.tenant_id == tenant_id,
                        DispenseMeterReading.reading_date < requested)
                .order_by(DispenseMeterReading.reading_date.desc())
                .first())
    any_reading_ever = (DispenseMeterReading.query
                        .filter_by(tenant_id=tenant_id)
                        .first())

    bootstrap_required = (any_reading_ever is None) and (existing is None)

    yesterday = requested - timedelta(days=1)
    has_yesterday = (DispenseMeterReading.query
                     .filter_by(tenant_id=tenant_id, reading_date=yesterday)
                     .first())
    previous_day_missing = (
        (not bootstrap_required)
        and existing is None
        and has_yesterday is None
        and any_reading_ever is not None
    )
    manual_opening_required = bootstrap_required or previous_day_missing

    # ── 5. Resolve opening ──
    if manual_opening_required:
        try:
            opening = float(data.get("opening_reading_m3"))
        except (ValueError, TypeError):
            return jsonify({
                "error": "opening_reading_m3 is required for the first day "
                         "or when yesterday's reading is missing."
            }), 400
        if opening < 0 or opening > 10_000_000:
            return jsonify({"error": "Opening reading is out of range."}), 400
    elif existing is not None:
        # Correction: the STORED opening is the source of truth. Never
        # recompute — that would risk drifting from the historical capture
        # (design §5.5) and would also break the closing >= opening check.
        opening = float(existing.opening_reading_m3)
    else:
        # New reading for a day where yesterday's reading exists: compute
        # opening from previous day's closing.
        opening = float(previous.closing_reading_m3) if previous else 0.0

    if closing < opening:
        return jsonify({
            "error": f"Closing reading ({closing:g}) must be >= opening reading ({opening:g})."
        }), 400

    consumption = round(closing - opening, 4)

    # ── 6. Write or update ──
    if existing is None:
        row = DispenseMeterReading(
            tenant_id=tenant_id,
            reading_date=requested,
            opening_reading_m3=opening,
            closing_reading_m3=closing,
            consumption_m3=consumption,
            recorded_by=_dispense_recorded_by(),
        )
        db.session.add(row)
        db.session.commit()
        _audit("dispense_reading_recorded",
               target_type="tenant", target_id=tenant_id,
               detail=(f"date={requested.isoformat()} "
                       f"opening={opening:g} closing={closing:g} consumption={consumption:g}"))
        action = "created"
        rid = row.id
    else:
        # Opening is NEVER modified on update (design §5.5).
        old_closing = float(existing.closing_reading_m3)
        existing.closing_reading_m3 = closing
        existing.consumption_m3 = consumption
        existing.recorded_by = _dispense_recorded_by()
        db.session.commit()
        _audit("dispense_reading_corrected",
               target_type="tenant", target_id=tenant_id,
               detail=(f"date={requested.isoformat()} "
                       f"old_closing={old_closing:g} new_closing={closing:g} "
                       f"reason={reason[:100]}"))
        action = "corrected"
        rid = existing.id

    # ── 7. Reload fresh stats for the response ──
    stats_map = _dispense_window_stats(tenant_id, [requested])
    st = stats_map[requested.isoformat()]

    return jsonify({
        "ok": True,
        "action": action,
        "reading_id": rid,
        "date_iso": requested.isoformat(),
        "opening_reading_m3": opening,
        "closing_reading_m3": closing,
        "consumption_m3": consumption,
        "consumption_l": st["consumption_l"],
        "sold_l": st["sold_l"],
        "sales_kes": st["sales_kes"],
        "paid_kes": st["paid_kes"],
        "outstanding_kes": st["outstanding_kes"],
        "variance_l": st["variance_l"],
        "has_reading": True,
    })


# ═══════════════════════════════════════════════
#  DISPENSING — PAYMENT RECORDING (Phase F)
# ═══════════════════════════════════════════════

@app.route("/api/admin/dispense/payments", methods=["GET"])
def admin_dispense_payments_get():
    u = _require_admin()
    if u: return u
    if not _rate_limit("dispense_payments_get", 60, 60):
        return _too_many(60)
    tenant_id, err = _dispense_tenant_or_error()
    if err: return err

    date_str = (request.args.get("date") or "").strip()
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid date (expected YYYY-MM-DD)."}), 400
    else:
        target_date = _nairobi_today()

    rows = (DispensePayment.query
            .filter_by(tenant_id=tenant_id, payment_date=target_date)
            .order_by(DispensePayment.created_at.desc(),
                      DispensePayment.id.desc())
            .all())

    total_paid = round(sum(float(r.amount_kes or 0) for r in rows if not r.voided), 2)

    return jsonify({
        "ok": True,
        "date": target_date.strftime("%d %b %Y"),
        "date_iso": target_date.isoformat(),
        "total_paid_kes": total_paid,
        "payments": [{
            "id": r.id,
            "amount_kes": float(r.amount_kes),
            "method": r.method,
            "reference": r.reference,
            "notes": r.notes,
            "voided": bool(r.voided),
            "voided_at": r.voided_at.isoformat() if r.voided_at else None,
            "voided_by": r.voided_by,
            "recorded_by": r.recorded_by,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
    })


@app.route("/api/admin/dispense/payments", methods=["POST"])
def admin_dispense_payments_post():
    u = _require_admin()
    if u: return u
    if not _rate_limit("dispense_payments_post", 30, 60):
        return _too_many(60)
    tenant_id, err = _dispense_tenant_or_error()
    if err: return err

    data = request.get_json(silent=True) or {}

    try:
        amount = float(data.get("amount_kes"))
    except (ValueError, TypeError):
        return jsonify({"error": "amount_kes must be a number."}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be greater than zero."}), 400
    if amount > 1_000_000:
        return jsonify({"error": "Amount exceeds the 1,000,000 KES limit."}), 400

    method = (data.get("method") or "").strip().lower()
    if method not in ("cash", "mpesa_c2b", "manual"):
        return jsonify({"error": "method must be cash, mpesa_c2b, or manual."}), 400

    reference = (data.get("reference") or "").strip()[:80]
    notes = (data.get("notes") or "").strip()[:500]
    admin_username = _dispense_recorded_by()

    if method in ("mpesa_c2b", "manual") and not reference:
        return jsonify({"error": "Reference is required for M-Pesa payments."}), 400

    if not reference:
        ts = int(datetime.utcnow().timestamp())
        reference = f"cash-{ts}-{admin_username}"[:80]

    if method in ("mpesa_c2b", "manual"):
        dup = (DispensePayment.query
               .filter_by(tenant_id=tenant_id, reference=reference)
               .first())
        if dup:
            return jsonify({
                "error": f"A payment with reference '{reference}' already exists.",
                "existing_payment_id": dup.id,
            }), 409

    today = _nairobi_today()
    row = DispensePayment(
        tenant_id=tenant_id,
        payment_date=today,
        amount_kes=amount,
        method=method,
        reference=reference,
        recorded_by=admin_username,
        notes=notes or None,
    )
    db.session.add(row)
    db.session.commit()

    _audit("dispense_payment_recorded",
           target_type="tenant", target_id=tenant_id,
           detail=f"amount={amount:.2f} method={method} ref={reference[:40]}")

    stats = _dispense_window_stats(tenant_id, [today])
    st = stats[today.isoformat()]

    return jsonify({
        "ok": True,
        "payment": {
            "id": row.id,
            "amount_kes": float(row.amount_kes),
            "method": row.method,
            "reference": row.reference,
            "voided": False,
            "recorded_by": row.recorded_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        },
        "today_paid_kes": st["paid_kes"],
        "today_outstanding_kes": st["outstanding_kes"],
    })


@app.route("/api/admin/dispense/payments/<int:payment_id>/void", methods=["POST"])
def admin_dispense_payment_void(payment_id):
    u = _require_admin()
    if u: return u
    if not _rate_limit("dispense_payment_void", 30, 60):
        return _too_many(60)
    tenant_id, err = _dispense_tenant_or_error()
    if err: return err

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()[:200]
    if not reason:
        return jsonify({"error": "A reason is required to void a payment."}), 400

    row = DispensePayment.query.filter_by(
        id=payment_id, tenant_id=tenant_id).first()
    if not row:
        return jsonify({"error": "Payment not found."}), 404
    if row.voided:
        return jsonify({"error": "Payment is already voided."}), 400

    row.voided = True
    row.voided_at = datetime.utcnow()
    row.voided_by = session.get("admin_username") or "?"
    db.session.commit()

    _audit("dispense_payment_voided",
           target_type="tenant", target_id=tenant_id,
           detail=f"payment_id={row.id} amount={row.amount_kes:.2f} reason={reason[:100]}")

    stats = _dispense_window_stats(tenant_id, [row.payment_date])
    st = stats[row.payment_date.isoformat()]

    return jsonify({
        "ok": True,
        "payment_id": row.id,
        "voided": True,
        "voided_at": row.voided_at.isoformat(),
        "voided_by": row.voided_by,
        "today_paid_kes": st["paid_kes"],
        "today_outstanding_kes": st["outstanding_kes"],
    })


# ═══════════════════════════════════════════════
#  DISPENSING — SALES RECORDING (Phase F.5)
# ═══════════════════════════════════════════════

def _dispense_active_rates(tenant_id):
    """Return active rate card for the tenant, ordered by size asc."""
    return (DispenseRate.query
            .filter_by(tenant_id=tenant_id, is_active=True)
            .order_by(DispenseRate.container_size_L.asc())
            .all())


@app.route("/api/admin/dispense/sales", methods=["GET"])
def admin_dispense_sales_get():
    u = _require_admin()
    if u: return u
    if not _rate_limit("dispense_sales_get", 60, 60):
        return _too_many(60)
    tenant_id, err = _dispense_tenant_or_error()
    if err: return err

    date_str = (request.args.get("date") or "").strip()
    if date_str:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid date (expected YYYY-MM-DD)."}), 400
    else:
        target_date = _nairobi_today()

    rows = (DispenseSale.query
            .filter_by(tenant_id=tenant_id, sale_date=target_date)
            .order_by(DispenseSale.created_at.desc(),
                      DispenseSale.id.desc())
            .all())

    active_sales = [r for r in rows if not r.voided]
    total_sales_kes = round(sum(float(r.total_kes or 0) for r in active_sales), 2)
    total_sold_l = sum(int(r.container_size_L or 0) * int(r.quantity or 0) for r in active_sales)

    rates = [{"container_size_L": r.container_size_L,
              "price_kes": float(r.price_kes)} for r in _dispense_active_rates(tenant_id)]

    return jsonify({
        "ok": True,
        "date": target_date.strftime("%d %b %Y"),
        "date_iso": target_date.isoformat(),
        "total_sales_kes": total_sales_kes,
        "total_sold_l": total_sold_l,
        "rates": rates,
        "sales": [{
            "id": r.id,
            "container_size_L": r.container_size_L,
            "quantity": r.quantity,
            "price_per_unit_kes": float(r.price_per_unit_kes),
            "total_kes": float(r.total_kes),
            "notes": r.notes,
            "voided": bool(r.voided),
            "voided_at": r.voided_at.isoformat() if r.voided_at else None,
            "voided_by": r.voided_by,
            "recorded_by": r.recorded_by,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
    })


@app.route("/api/admin/dispense/sales", methods=["POST"])
def admin_dispense_sales_post():
    u = _require_admin()
    if u: return u
    if not _rate_limit("dispense_sales_post", 30, 60):
        return _too_many(60)
    tenant_id, err = _dispense_tenant_or_error()
    if err: return err

    data = request.get_json(silent=True) or {}

    try:
        size = int(data.get("container_size_L"))
        qty  = int(data.get("quantity"))
    except (ValueError, TypeError):
        return jsonify({"error": "container_size_L and quantity must be integers."}), 400
    if size < 1 or size > 1_000_000:
        return jsonify({"error": "container_size_L out of range."}), 400
    if qty < 1 or qty > 10_000:
        return jsonify({"error": "quantity out of range (1..10000)."}), 400

    rate = (DispenseRate.query
            .filter_by(tenant_id=tenant_id, container_size_L=size, is_active=True)
            .first())
    if not rate:
        return jsonify({
            "error": f"No active dispensing rate for {size} L. "
                     f"Configure it in the dev portal before recording sales."
        }), 400

    price = float(rate.price_kes)
    total = round(price * qty, 2)
    notes = (data.get("notes") or "").strip()[:500]
    admin_username = _dispense_recorded_by()

    today = _nairobi_today()
    row = DispenseSale(
        tenant_id=tenant_id,
        sale_date=today,
        container_size_L=size,
        quantity=qty,
        price_per_unit_kes=price,
        total_kes=total,
        recorded_by=admin_username,
        notes=notes or None,
    )
    db.session.add(row)
    db.session.commit()

    _audit("dispense_sale_recorded",
           target_type="tenant", target_id=tenant_id,
           detail=f"size={size}L qty={qty} price={price:.2f} total={total:.2f}")

    stats = _dispense_window_stats(tenant_id, [today])
    st = stats[today.isoformat()]

    return jsonify({
        "ok": True,
        "sale": {
            "id": row.id,
            "container_size_L": size,
            "quantity": qty,
            "price_per_unit_kes": price,
            "total_kes": total,
            "voided": False,
        },
        "today_sales_kes": st["sales_kes"],
        "today_sold_l": st["sold_l"],
        "today_variance_l": st["variance_l"],
    })


@app.route("/api/admin/dispense/sales/<int:sale_id>/void", methods=["POST"])
def admin_dispense_sale_void(sale_id):
    u = _require_admin()
    if u: return u
    if not _rate_limit("dispense_sale_void", 30, 60):
        return _too_many(60)
    tenant_id, err = _dispense_tenant_or_error()
    if err: return err

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()[:200]
    if not reason:
        return jsonify({"error": "A reason is required to void a sale."}), 400

    row = DispenseSale.query.filter_by(id=sale_id, tenant_id=tenant_id).first()
    if not row:
        return jsonify({"error": "Sale not found."}), 404
    if row.voided:
        return jsonify({"error": "Sale is already voided."}), 400

    row.voided = True
    row.voided_at = datetime.utcnow()
    row.voided_by = session.get("admin_username") or "?"
    db.session.commit()

    _audit("dispense_sale_voided",
           target_type="tenant", target_id=tenant_id,
           detail=f"sale_id={row.id} size={row.container_size_L}L qty={row.quantity} "
                  f"total={row.total_kes:.2f} reason={reason[:100]}")

    stats = _dispense_window_stats(tenant_id, [row.sale_date])
    st = stats[row.sale_date.isoformat()]

    return jsonify({
        "ok": True,
        "sale_id": row.id,
        "voided": True,
        "voided_at": row.voided_at.isoformat(),
        "voided_by": row.voided_by,
        "today_sales_kes": st["sales_kes"],
        "today_sold_l": st["sold_l"],
        "today_variance_l": st["variance_l"],
    })


@app.route("/api/admin/dispense/sales/bulk_import", methods=["POST"])
def admin_dispense_sales_bulk_import():
    """Bulk-import sales from a CSV paste.

    Body (JSON):
      {
        "csv": "<multiline CSV text>",
        "dry_run": true|false
      }

    CSV format (header optional, blank lines ignored):
      size_l,quantity[,date][,notes]
      size_l,quantity[,notes]
      size_l,quantity

    - size_l  — must match an ACTIVE dispensing rate for this tenant
    - quantity — 1..10000
    - date    — YYYY-MM-DD, must be within the last 7 days (default today)
    - notes   — optional free text, up to 500 chars

    All-or-nothing: if ANY row is invalid, no sales are written and 400 is
    returned with row-by-row errors. With dry_run=true, valid data is
    returned in the response but never written.
    """
    u = _require_admin()
    if u: return u
    if not _rate_limit("dispense_sales_bulk", 5, 60):
        return _too_many(60)
    tenant_id, err = _dispense_tenant_or_error()
    if err: return err

    data = request.get_json(silent=True) or {}
    csv_text = (data.get("csv") or "").lstrip("\ufeff")
    dry_run  = bool(data.get("dry_run"))

    if not csv_text or not csv_text.strip():
        return jsonify({
            "error": "Empty CSV. Expected lines like: size_l,quantity[,date][,notes]"
        }), 400

    import csv as _csv
    import io as _io

    rates = {r.container_size_L: float(r.price_kes)
             for r in _dispense_active_rates(tenant_id)}
    if not rates:
        return jsonify({
            "error": "No active dispensing rates configured. "
                     "Set them up in the dev portal before importing sales."
        }), 400

    today    = _nairobi_today()
    earliest = today - timedelta(days=6)

    reader = _csv.reader(_io.StringIO(csv_text))
    parsed = []
    errors = []

    for lineno, row in enumerate(reader, start=1):
        if not row or not any(str(c).strip() for c in row):
            continue
        # Header detection (only on the first non-empty row)
        if lineno == 1 and str(row[0]).strip().lower() in ("size_l", "size", "container"):
            continue

        cells = [str(c).strip() for c in row]
        while len(cells) < 2:
            cells.append("")

        try:
            size = int(cells[0])
        except (ValueError, TypeError):
            errors.append({"line": lineno, "error": f"invalid size_l {cells[0]!r}"})
            continue
        if size not in rates:
            errors.append({"line": lineno, "error": f"{size} L has no active rate"})
            continue

        try:
            qty = int(cells[1])
        except (ValueError, TypeError):
            errors.append({"line": lineno, "error": f"invalid quantity {cells[1]!r}"})
            continue
        if qty < 1 or qty > 10000:
            errors.append({"line": lineno, "error": f"quantity {qty} out of range (1..10000)"})
            continue

        # Optional date column at position 3
        sale_date   = today
        notes_parts = []
        rest        = cells[2:]
        if rest:
            first = rest[0]
            if first:
                try:
                    sale_date = datetime.strptime(first, "%Y-%m-%d").date()
                    notes_parts.extend([c for c in rest[1:] if c])
                except (ValueError, TypeError):
                    notes_parts.extend([c for c in rest if c])
            else:
                notes_parts.extend([c for c in rest if c])

        if sale_date < earliest or sale_date > today:
            errors.append({
                "line": lineno,
                "error": f"date {sale_date.isoformat()} outside last 7 days"
            })
            continue

        price = rates[size]
        total = round(price * qty, 2)
        notes = (" ".join(notes_parts)).strip()[:500] or None

        parsed.append({
            "line": lineno,
            "size_l": size,
            "quantity": qty,
            "date_iso": sale_date.isoformat(),
            "price_per_unit_kes": price,
            "total_kes": total,
            "notes": notes,
        })

    if not parsed and not errors:
        return jsonify({"error": "No data rows found in CSV."}), 400

    total_kes = round(sum(p["total_kes"] for p in parsed), 2)
    total_l   = sum(p["size_l"] * p["quantity"] for p in parsed)

    if errors:
        return jsonify({
            "ok": False,
            "dry_run": dry_run,
            "parsed": parsed,
            "errors": errors,
            "count_ok": len(parsed),
            "count_err": len(errors),
            "total_kes": total_kes,
            "total_liters": total_l,
        }), 400

    if dry_run:
        return jsonify({
            "ok": True,
            "dry_run": True,
            "parsed": parsed,
            "errors": [],
            "count_ok": len(parsed),
            "count_err": 0,
            "total_kes": total_kes,
            "total_liters": total_l,
        })

    admin_username = _dispense_recorded_by()
    try:
        created_ids = []
        for p in parsed:
            row = DispenseSale(
                tenant_id=tenant_id,
                sale_date=datetime.strptime(p["date_iso"], "%Y-%m-%d").date(),
                container_size_L=p["size_l"],
                quantity=p["quantity"],
                price_per_unit_kes=p["price_per_unit_kes"],
                total_kes=p["total_kes"],
                recorded_by=admin_username,
                notes=p["notes"],
            )
            db.session.add(row)
            db.session.flush()
            created_ids.append(row.id)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.exception("[BulkSales] commit failed")
        return jsonify({"ok": False, "error": f"Database write failed: {e}"}), 500

    _audit("dispense_sales_bulk_imported",
           target_type="tenant", target_id=tenant_id,
           detail=(f"count={len(parsed)} total_kes={total_kes:.2f} "
                   f"liters={total_l}"))

    stats = _dispense_window_stats(tenant_id, [today])
    st = stats[today.isoformat()]

    return jsonify({
        "ok": True,
        "dry_run": False,
        "count": len(parsed),
        "total_kes": total_kes,
        "total_liters": total_l,
        "created_ids": created_ids,
        "today_sales_kes": st["sales_kes"],
        "today_sold_l": st["sold_l"],
        "today_variance_l": st["variance_l"],
    })


@app.route("/api/admin/dispense/window", methods=["GET"])
def admin_dispense_window():
    u = _require_admin()
    if u: return u
    if not _rate_limit("dispense_window", 30, 60):
        return _too_many(60)
    tenant_id, err = _dispense_tenant_or_error()
    if err: return err

    try:
        n = int(request.args.get("days", "7"))
    except (ValueError, TypeError):
        n = 7
    if n < 1: n = 1
    if n > 30: n = 30

    today = _nairobi_today()
    dates = [today - timedelta(days=i) for i in range(n)]
    stats_map = _dispense_window_stats(tenant_id, dates)

    days = []
    for d in dates:
        iso = d.isoformat()
        st = stats_map[iso]
        days.append({
            "date": d.strftime("%d %b %Y"),
            "date_iso": iso,
            "consumption_l": st["consumption_l"],
            "consumption_m3": st["consumption_m3"],
            "sold_l": st["sold_l"],
            "sales_kes": st["sales_kes"],
            "paid_kes": st["paid_kes"],
            "outstanding_kes": st["outstanding_kes"],
            "variance_l": st["variance_l"],
            "has_reading": st["has_reading"],
            "status": _dispense_status(st),
        })

    return jsonify({"ok": True, "days": days})


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout_route():
    # Capture the tenant slug BEFORE clearing the session, then return
    # a tenant-scoped login URL so the post-logout redirect stays on the
    # correct tenant's portal. Default tenant → plain /admin/login.
    _slug = session.get("__tenant_slug") or _DEFAULT_TENANT_SLUG
    _audit("logout")
    session.clear()
    redirect_url = "/admin/login"
    if _slug and _slug != _DEFAULT_TENANT_SLUG:
        redirect_url = f"/admin/login?__tenant={_slug}"
    return jsonify({"ok": True, "redirect": redirect_url}), 200


@app.route("/api/admin/forgot", methods=["POST"])
def admin_forgot_route():
    if not _rate_limit("forgot", 10, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    result = request_password_reset(data.get("identifier", ""))
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/admin/reset", methods=["POST"])
def admin_reset_route():
    if not _rate_limit("reset", 10, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    result = confirm_password_reset(
        data.get("token", ""), data.get("otp", ""), data.get("new_password", ""),
    )
    return jsonify(result), (200 if result.get("ok") else 400)


# ═══════════════════════════════════════════════
#  ADMIN — SETTINGS
# ═══════════════════════════════════════════════

# ═══════════════════════════════════════════════
#  ADMIN — DIAGNOSTICS (super-admin only)
# ═══════════════════════════════════════════════
# Permanent, replaces the ad-hoc debug endpoints. Returns no secrets,
# no password hashes, no tokens. Super-admin only. Used to diagnose
# login/session/tenant issues in one query.

@app.route("/api/admin/diagnostics/session", methods=["GET"])
def admin_diagnostics_session():
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("diag_session", 30, 60):
        return _too_many(60)

    admin = _current_admin()

    sess_view = {
        "admin_id": session.get("admin_id"),
        "admin_username": session.get("admin_username"),
        "__tenant_slug": session.get("__tenant_slug"),
        "last_activity": session.get("last_activity"),
        "pending_2fa_admin_id": session.get("pending_2fa_admin_id"),
    }

    resolved = None
    try:
        t = g.tenant
        if t is not None:
            resolved = {"id": t.id, "slug": t.slug,
                        "org_name": t.org_name, "is_active": bool(t.is_active)}
    except Exception:
        pass

    admin_view = None
    admin_tenant_view = None
    if admin:
        admin_view = {
            "id": admin.id, "username": admin.username,
            "tenant_id": admin.tenant_id,
            "is_super": bool(getattr(admin, "is_super", False)),
            "is_active": bool(admin.is_active),
        }
        if admin.tenant_id:
            at = Tenant.query.get(admin.tenant_id)
            if at:
                admin_tenant_view = {
                    "id": at.id, "slug": at.slug,
                    "org_name": at.org_name, "is_active": bool(at.is_active),
                }

    match = None
    if admin_tenant_view and resolved:
        match = (admin_tenant_view["slug"] == resolved["slug"])

    recent = []
    try:
        cutoff = datetime.utcnow() - timedelta(hours=24)
        rows = (AdminAuditLog.query
                .filter(AdminAuditLog.created_at >= cutoff)
                .filter(AdminAuditLog.action.in_(
                    ("login", "logout", "login_failed",
                     "session_timeout", "login_2fa_ok")))
                .order_by(AdminAuditLog.created_at.desc())
                .limit(30)
                .all())
        for r in rows:
            dt_nbo = _as_nairobi(r.created_at)
            recent.append({
                "time": dt_nbo.strftime("%d %b %Y, %H:%M:%S"),
                "admin": r.admin_username,
                "action": r.action,
                "detail": r.detail,
                "ip": r.ip_address,
            })
    except Exception:
        pass

    tenants_all = []
    admins_all = []
    try:
        for t in Tenant.query.order_by(Tenant.id.asc()).all():
            tenants_all.append({
                "id": t.id, "slug": t.slug, "org_name": t.org_name,
                "is_active": bool(t.is_active),
            })
        for a in AdminUser.query.order_by(AdminUser.id.asc()).all():
            admins_all.append({
                "id": a.id, "username": a.username,
                "tenant_id": a.tenant_id,
                "is_super": bool(getattr(a, "is_super", False)),
                "is_active": bool(a.is_active),
                "failed_attempts": a.failed_attempts,
                "locked": bool(a.locked_until and a.locked_until > datetime.utcnow()),
            })
    except Exception:
        pass

    return jsonify({
        "now_utc": datetime.utcnow().isoformat(),
        "authenticated": bool(admin),
        "admin": admin_view,
        "admin_tenant": admin_tenant_view,
        "resolved_tenant": resolved,
        "session": sess_view,
        "match": match,
        "url_tenant": request.args.get("__tenant") or None,
        "url_preview": request.args.get("__preview") or None,
        "recent_auth_events": recent,
        "all_tenants": tenants_all,
        "all_admins": admins_all,
    })


@app.route("/api/admin/settings", methods=["GET"])
def admin_get_settings():
    if not _rate_limit("settings_get", 30, 60):
        return _too_many(60)
    u = _require_admin()
    if u:
        return u
    admin = _current_admin()
    return jsonify({
        "username": admin.username,
        "phone": admin.phone,
        "is_super": bool(getattr(admin, "is_super", False)),
    })


@app.route("/api/admin/settings/username", methods=["POST"])
def admin_update_username():
    if not _rate_limit("settings_username", 5, 60):
        return _too_many(60)
    u = _require_admin()
    if u:
        return u
    admin = _current_admin()

    data = request.get_json(silent=True) or {}
    new_username = (data.get("new_username") or "").strip().lower()

    if len(new_username) < 3:
        return jsonify({"error": "Username must be at least 3 characters."}), 400
    if len(new_username) > 60:
        return jsonify({"error": "Username too long (max 60)."}), 400
    if not all(c.isalnum() or c in "@._-" for c in new_username):
        return jsonify({"error": "Only letters, digits, and @ . _ - are allowed."}), 400
    if new_username == (admin.username or "").lower():
        return jsonify({"error": "That is already your username."}), 400

    # Global uniqueness — the model declares username with unique=True
    # across ALL tenants, so the pre-check must also be global. A
    # tenant-scoped check would allow a rename to collide with another
    # tenant's admin and raise IntegrityError (HTTP 500) instead of a
    # clean, friendly 409.
    dup = AdminUser.query.filter_by(username=new_username).first()
    if dup and dup.id != admin.id:
        return jsonify({
            "error": (f"Username '{new_username}' is already in use. "
                      "Please choose a different username."),
            "code": "username_taken",
        }), 409

    admin.username = new_username
    try:
        db.session.commit()
    except IntegrityError:
        # Safety net: if the pre-check missed (race, or a legacy row outside
        # the current tenant scope), still return a friendly 409 rather than
        # a 500. Roll back so the ORM session remains usable.
        db.session.rollback()
        return jsonify({
            "error": (f"Username '{new_username}' is already in use. "
                      "Please choose a different username."),
            "code": "username_taken",
        }), 409

    session["admin_username"] = new_username

    _audit("update_settings_username",
           detail=f"new_username={new_username}")

    return jsonify({"ok": True, "username": new_username})


@app.route("/api/admin/settings/phone", methods=["POST"])
def admin_update_phone():
    if not _rate_limit("settings_phone", 5, 60):
        return _too_many(60)
    u = _require_admin()
    if u:
        return u
    admin = _current_admin()

    data = request.get_json(silent=True) or {}
    new_phone = (data.get("new_phone") or "").strip()

    digits = "".join(c for c in new_phone if c.isdigit())
    if not digits:
        return jsonify({"error": "Phone number is required."}), 400

    # Accept: 07XXXXXXXX, 254XXXXXXXXX, 7XXXXXXXX, +254XXXXXXXXX
    if digits.startswith("254") and len(digits) == 12:
        pass
    elif digits.startswith("0") and len(digits) == 10:
        pass
    elif len(digits) == 9:
        pass
    else:
        return jsonify({"error": "Invalid Kenyan phone number."}), 400

    admin.phone = new_phone
    db.session.commit()

    _audit("update_settings_phone",
           detail=f"new_phone={new_phone}")

    return jsonify({"ok": True, "phone": new_phone})


@app.route("/api/admin/settings/password", methods=["POST"])
def admin_update_password():
    if not _rate_limit("settings_password", 5, 60):
        return _too_many(60)
    u = _require_admin()
    if u:
        return u
    admin = _current_admin()

    data = request.get_json(silent=True) or {}
    current_pw = data.get("current_password") or ""
    new_pw = data.get("new_password") or ""

    if not check_password_hash(admin.password_hash, current_pw):
        return jsonify({"error": "Current password is incorrect."}), 401
    if len(new_pw) < 8:
        return jsonify({"error": "New password must be at least 8 characters."}), 400
    if new_pw == current_pw:
        return jsonify({"error": "New password must be different from current."}), 400

    admin.password_hash = generate_password_hash(new_pw)
    admin.failed_attempts = 0
    admin.locked_until = None
    admin.password_changed_at = datetime.utcnow()
    db.session.commit()

    _audit("update_settings_password")

    _clear_session_preserve_tenant()
    return jsonify({"ok": True, "message": "Password updated. Please log in again."})


# ═══════════════════════════════════════════════
#  ADMIN — consumers
# ═══════════════════════════════════════════════

@app.route("/api/admin/consumer", methods=["POST"])
def admin_create_consumer():
    u = _require_admin()
    if u: return u
    if not _rate_limit("consumer_create", 20, 60):
        return _too_many(60)

    _tid = _write_tenant_id()
    if _tid < 0:
        return jsonify({"error": "No tenant context for this request."}), 400

    data = request.get_json(silent=True) or {}
    cust_name = (data.get("cust_name") or "").strip()
    acc_name = (data.get("acc_name") or "").strip()
    meter_acc_no = (data.get("meter_acc_no") or "").strip()
    contact = (data.get("contact") or "").strip()
    email = (data.get("email") or "").strip()
    address = (data.get("address") or "").strip()
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    initial_raw = data.get("meter_initial_reading_m3")
    whatsapp_opt_in = bool(data.get("whatsapp_opt_in"))
    alt_contact = (data.get("alt_contact") or "").strip()
    alt_whatsapp_opt_in = bool(data.get("alt_whatsapp_opt_in")) and bool(alt_contact)

    if not (cust_name and acc_name and meter_acc_no and contact):
        return jsonify({"error": "cust_name, acc_name, meter_acc_no, and contact are required."}), 400
    if _tenant_query(Consumer).filter_by(meter_acc_no=meter_acc_no).first():
        return jsonify({"error": f"Meter account {meter_acc_no} already exists."}), 409

    try:
        lat = float(latitude) if latitude not in (None, "", "null") else None
        lng = float(longitude) if longitude not in (None, "", "null") else None
    except (ValueError, TypeError):
        return jsonify({"error": "latitude/longitude must be numbers."}), 400
    # C5e1 Finding #5: reject NaN/inf — they defeat range checks below.
    if lat is not None and not math.isfinite(lat):
        return jsonify({"error": "latitude must be a finite number."}), 400
    if lng is not None and not math.isfinite(lng):
        return jsonify({"error": "longitude must be a finite number."}), 400
    if lat is not None and not (-90 <= lat <= 90):
        return jsonify({"error": "latitude must be between -90 and 90."}), 400
    if lng is not None and not (-180 <= lng <= 180):
        return jsonify({"error": "longitude must be between -180 and 180."}), 400

    try:
        initial = float(initial_raw) if initial_raw not in (None, "", "null") else 0.0
    except (ValueError, TypeError):
        return jsonify({"error": "meter_initial_reading_m3 must be a number."}), 400
    # C5e1 Finding #5: math.isfinite guards against NaN/inf, which pass
    # both comparison checks above and corrupt consumption math.
    if not math.isfinite(initial) or initial < 0 or initial > 1_000_000:
        return jsonify({"error": "meter_initial_reading_m3 out of range."}), 400

    c = Consumer(cust_name=cust_name, acc_name=acc_name,
                 meter_acc_no=meter_acc_no, contact=contact,
                 email=email or None, address=address or None,
                 latitude=lat, longitude=lng,
                 meter_initial_reading_m3=initial, is_active=True,
                 whatsapp_opt_in=whatsapp_opt_in,
                 alt_contact=alt_contact or None,
                 alt_whatsapp_opt_in=alt_whatsapp_opt_in,
                 tenant_id=_tid)
    db.session.add(c)
    # C5e1 Finding #4: consumer.meter_acc_no has a GLOBAL unique constraint,
    # but the pre-check above is tenant-scoped. A cross-tenant collision
    # reaches this commit and would 500 without the except. Return a clean
    # 409 instead. Capture the value before commit — after a rollback the
    # pending object is expunged and its attributes may be inaccessible.
    _meter = meter_acc_no
    try:
        db.session.commit()
    except (IntegrityError, OperationalError) as _e:
        # C5e1-FIX2: D1 dialect raises OperationalError (not IntegrityError)
        # for unique-constraint violations, wrapping the D1 HTTP 400
        # response. Only convert to 409 when the message proves a UNIQUE
        # constraint failed; other operational errors (connection down,
        # D1 unreachable) must stay visible as 500s.
        db.session.rollback()
        _msg = str(_e).lower()
        if "unique constraint" in _msg or "sqlite_constraint_unique" in _msg:
            return jsonify({"error": f"Meter account {_meter} is already in use."}), 409
        raise

    _audit("create_consumer",
           target_type="consumer", target_id=c.id,
           target_label=f"{c.cust_name} · {c.meter_acc_no}",
           detail=f"contact={c.contact}, alt={c.alt_contact or '—'}, "
                  f"email={c.email or '—'}, opt_in={bool(c.whatsapp_opt_in)}, "
                  f"alt_opt_in={bool(c.alt_whatsapp_opt_in)}")

    return jsonify({"ok": True, "consumer": {
        "id": c.id, "cust_name": c.cust_name, "acc_name": c.acc_name,
        "meter_acc_no": c.meter_acc_no, "contact": c.contact,
        "email": c.email, "address": c.address,
        "latitude": c.latitude, "longitude": c.longitude,
        "meter_initial_reading_m3": float(c.meter_initial_reading_m3 or 0.0),
        "is_active": True,
        "whatsapp_opt_in": bool(c.whatsapp_opt_in),
        "alt_contact": c.alt_contact,
        "alt_whatsapp_opt_in": bool(c.alt_whatsapp_opt_in),
    }}), 201


@app.route("/api/admin/consumer/<int:consumer_id>/update", methods=["POST"])
def admin_update_consumer(consumer_id):
    u = _require_admin()
    if u: return u
    if not _rate_limit("consumer_update", 30, 60):
        return _too_many(60)

    consumer = _tenant_query(Consumer).filter_by(id=consumer_id).first()
    if not consumer:
        return jsonify({"error": "Consumer not found"}), 404

    data = request.get_json(silent=True) or {}
    allowed = ("cust_name", "acc_name", "meter_acc_no", "contact",
               "email", "address", "latitude", "longitude",
               "meter_initial_reading_m3", "whatsapp_opt_in",
               "alt_contact", "alt_whatsapp_opt_in")
    updated = {}
    for f in allowed:
        if f not in data:
            continue
        v = data[f]

        if f == "meter_acc_no":
            new_no = (str(v) or "").strip()
            if not new_no:
                return jsonify({"error": "meter_acc_no cannot be empty."}), 400
            if new_no != consumer.meter_acc_no:
                dup = _tenant_query(Consumer).filter_by(meter_acc_no=new_no).first()
                if dup and dup.id != consumer.id:
                    return jsonify({"error": f"Meter account {new_no} already exists."}), 409
            setattr(consumer, f, new_no)
        elif f in ("latitude", "longitude"):
            if v in (None, "", "null"):
                setattr(consumer, f, None)
            else:
                try: _llv = float(v)
                except (ValueError, TypeError):
                    return jsonify({"error": f"{f} must be a number."}), 400
                # C5e1 Finding #5: reject NaN/inf — they pass every
                # comparison (`nan < -90` is False) and would corrupt
                # any downstream geolocation math.
                if not math.isfinite(_llv):
                    return jsonify({"error": f"{f} must be a finite number."}), 400
                if f == "latitude" and not (-90 <= _llv <= 90):
                    return jsonify({"error": "latitude must be between -90 and 90."}), 400
                if f == "longitude" and not (-180 <= _llv <= 180):
                    return jsonify({"error": "longitude must be between -180 and 180."}), 400
                setattr(consumer, f, _llv)
        elif f == "meter_initial_reading_m3":
            try:
                _init = float(v) if v not in (None, "", "null") else 0.0
            except (ValueError, TypeError):
                return jsonify({"error": "meter_initial_reading_m3 must be a number."}), 400
            # C5e1 Finding #2: reject negative / out-of-range / NaN initial
            # readings. A negative value inflates every future bill by the
            # full magnitude; a huge value produces enormous false credit.
            if not math.isfinite(_init) or _init < 0 or _init > 1_000_000:
                return jsonify({"error": "meter_initial_reading_m3 out of range."}), 400
            setattr(consumer, f, _init)
        elif f == "whatsapp_opt_in":
            # Accept bool, int (0/1), or strings "true"/"false"/"1"/"0"
            if isinstance(v, bool):
                setattr(consumer, f, v)
            elif isinstance(v, (int, float)):
                setattr(consumer, f, bool(v))
            elif isinstance(v, str):
                setattr(consumer, f, v.strip().lower() in ("1", "true", "yes", "on"))
            else:
                setattr(consumer, f, False)
        elif f == "alt_contact":
            setattr(consumer, f, (str(v) or "").strip() or None)
        elif f == "alt_whatsapp_opt_in":
            if isinstance(v, bool):
                setattr(consumer, f, v)
            elif isinstance(v, (int, float)):
                setattr(consumer, f, bool(v))
            elif isinstance(v, str):
                setattr(consumer, f, v.strip().lower() in ("1", "true", "yes", "on"))
            else:
                setattr(consumer, f, False)
        else:
            setattr(consumer, f, v)
        updated[f] = v

    if not updated:
        return jsonify({"error": "No valid fields to update"}), 400

    # Alt opt-in is meaningless without an alt contact.
    if not consumer.alt_contact and consumer.alt_whatsapp_opt_in:
        consumer.alt_whatsapp_opt_in = False

    # C5e1 Finding #4: same cross-tenant collision concern as create — the
    # DB unique constraint is global, the pre-check is tenant-scoped.
    _meter = consumer.meter_acc_no
    try:
        db.session.commit()
    except (IntegrityError, OperationalError) as _e:
        # C5e1-FIX2: same treatment as create-path. See comment there.
        db.session.rollback()
        _msg = str(_e).lower()
        if "unique constraint" in _msg or "sqlite_constraint_unique" in _msg:
            return jsonify({"error": f"Meter account {_meter} is already in use."}), 409
        raise

    _audit("update_consumer",
           target_type="consumer", target_id=consumer.id,
           target_label=f"{consumer.cust_name} · {consumer.meter_acc_no}",
           detail=f"fields={','.join(sorted(updated.keys()))}")

    return jsonify({"ok": True, "updated": updated, "consumer": {
        "id": consumer.id, "cust_name": consumer.cust_name,
        "acc_name": consumer.acc_name, "meter_acc_no": consumer.meter_acc_no,
        "contact": consumer.contact, "email": consumer.email,
        "address": consumer.address,
        "latitude": consumer.latitude, "longitude": consumer.longitude,
        "meter_initial_reading_m3": float(consumer.meter_initial_reading_m3 or 0.0),
        "is_active": bool(consumer.is_active),
        "whatsapp_opt_in": bool(consumer.whatsapp_opt_in),
        "alt_contact": consumer.alt_contact,
        "alt_whatsapp_opt_in": bool(consumer.alt_whatsapp_opt_in),
    }})


# ═══════════════════════════════════════════════
#  PUBLIC — token-based bill PDF (used by WhatsApp)
# ═══════════════════════════════════════════════

# In-memory store: {token: payment_id}
# Tokens live for the lifetime of the process — enough for WhatsApp to fetch
# the PDF within seconds of the send request.
_PUBLIC_PDF_TOKENS = {}
_PUBLIC_BILL_TOKENS = {}


def _register_public_pdf(payment_id: int) -> str:
    """Create a one-time-style token for a payment PDF."""
    token = secrets.token_urlsafe(32)
    _PUBLIC_PDF_TOKENS[token] = payment_id
    return token


@app.route("/public/receipt/<token>.pdf")
def public_receipt_pdf(token):
    """
    Public PDF endpoint — no auth. Token is unguessable (32-byte URL-safe).
    Meta's servers call this URL to fetch the PDF for WhatsApp delivery.
    """
    payment_id = _PUBLIC_PDF_TOKENS.get(token)
    if not payment_id:
        return "Not found.", 404

    log = PaymentLog.query.get(payment_id)
    if not log or not log.receipt_json:
        return "Receipt not available.", 404

    try:
        snapshot = json.loads(log.receipt_json)
        snapshot.update(_brand_context())
        snapshot["template_r2_key"] = _resolve_template_key(g.tenant, "receipt")
        pdf_buf = generate_receipt_pdf(snapshot)
    except Exception:
        app.logger.exception("[Public Receipt] generation failed")
        return "PDF generation failed.", 500

    filename = f"{_safe_filename(snapshot.get('cust_name',''))}_receipt_{snapshot.get('receipt_no','payment')}.pdf"
    return send_file(pdf_buf, mimetype="application/pdf",
                     as_attachment=False, download_name=filename)


# ═══════════════════════════════════════════════
#  HELPERS — FIFO payment allocation (shared by admin + M-Pesa)
# ═══════════════════════════════════════════════

def _apply_fifo_payment(consumer, amount: float, method: str,
                        reference: str, recorded_by: str):
    """
    Apply a payment to a consumer's bills using FIFO (oldest unpaid first).
    Excess becomes prepayment credit on the newest reading.
    Creates a PaymentLog and receipt snapshot. Returns the PaymentLog.
    """
    readings = (MeterReading.query
                .filter_by(consumer_id=consumer.id)
                .order_by(MeterReading.reading_date.asc(),
                          MeterReading.id.asc())
                .all())
    if not readings:
        raise ValueError("Consumer has no readings on file yet.")

    previous_balance = round(sum(max(r.balance, 0.0) for r in readings), 2)

    remaining = amount
    allocations = []
    for r in readings:
        owed = round((r.amount_kes or 0.0) - (r.amount_paid or 0.0), 2)
        if owed <= 0:
            continue
        apply = round(min(remaining, owed), 2)
        if apply <= 0:
            break
        r.amount_paid = round((r.amount_paid or 0.0) + apply, 2)
        allocations.append({
            "reading_id": r.id,
            "reading_date": _fmt_date(r.reading_date),
            "reading_m3": f"{r.reading_m3:.2f}",
            "amount_kes": _fmt_money(r.amount_kes),
            "applied": _fmt_money(apply),
            "new_balance": _fmt_money(r.amount_kes - r.amount_paid),
            "note": "",
        })
        remaining = round(remaining - apply, 2)
        if remaining <= 0:
            break

    if remaining > 0:
        newest = readings[-1]
        newest.amount_paid = round((newest.amount_paid or 0.0) + remaining, 2)
        allocations.append({
            "reading_id": newest.id,
            "reading_date": _fmt_date(newest.reading_date),
            "reading_m3": f"{newest.reading_m3:.2f}",
            "amount_kes": _fmt_money(newest.amount_kes),
            "applied": _fmt_money(remaining),
            "new_balance": _fmt_money(newest.amount_kes - newest.amount_paid),
            "note": "(Prepayment credit)",
        })
        remaining = 0

    remaining_balance = round(sum(max(r.balance, 0.0) for r in readings), 2)
    prepaid_credit = round(sum(max(-r.balance, 0.0) for r in readings), 2)
    info = get_consumer_status(list(reversed(readings)))
    newest = readings[-1]

    log = PaymentLog(
        consumer_id=consumer.id,
        amount_kes=amount,
        method=method,
        reference=reference or None,
        recorded_by=recorded_by[:60],
        tenant_id=consumer.tenant_id,
    )
    db.session.add(log)
    db.session.flush()

    receipt_no = f"{log.id:06d}"
    created = _as_nairobi(log.created_at)

    snapshot = {
        "receipt_no": receipt_no,
        "receipt_date": created.strftime("%d %b %Y"),
        "receipt_time": created.strftime("%H:%M"),
        "cust_name": consumer.cust_name,
        "acc_name": consumer.acc_name,
        "meter_acc_no": consumer.meter_acc_no,
        "contact": consumer.contact or "—",
        "email": consumer.email or "—",
        "address": consumer.address or "—",
        "amount_kes": _fmt_money(amount),
        "method": method.capitalize(),
        "reference": reference or "—",
        "recorded_by": recorded_by[:60],
        "previous_balance": _fmt_money(previous_balance),
        "remaining_balance": _fmt_money(remaining_balance),
        "prepaid_credit": _fmt_money(prepaid_credit),
        "status_label": info["label"],
        "last_reading_date": _fmt_date(newest.reading_date),
        "last_reading_m3": f"{newest.reading_m3:.2f}",
        "allocations": allocations,
    }
    log.receipt_json = json.dumps(snapshot)

    # Statement snapshot (last 5 readings, newest first)
    touched_ids = {a["reading_id"] for a in allocations}
    applied_by_id = {a["reading_id"]: a["applied"] for a in allocations}
    statement = []
    for r in reversed(readings[-5:]):
        statement.append({
            "reading_id": r.id,
            "reading_date": _fmt_date(r.reading_date),
            "reading_m3": f"{r.reading_m3:.2f}",
            "amount_kes": _fmt_money(r.amount_kes),
            "paid": _fmt_money(r.amount_paid),
            "applied": applied_by_id.get(r.id, "0.00"),
            "new_balance": _fmt_money(r.amount_kes - r.amount_paid),
            "touched": r.id in touched_ids,
        })
    snapshot["statement"] = statement
    log.receipt_json = json.dumps(snapshot)

    return log


# ═══════════════════════════════════════════════
#  ADMIN — PAYMENTS
# ═══════════════════════════════════════════════

@app.route("/api/admin/consumer/<int:consumer_id>/payment", methods=["POST"])
def admin_record_payment(consumer_id):
    """FIFO: oldest unpaid bill first. Excess becomes prepayment credit.
    Stores a full JSON snapshot for later receipt generation."""
    u = _require_admin()
    if u: return u
    if not _rate_limit("consumer_payment", 20, 60):
        return _too_many(60)

    consumer = _tenant_query(Consumer).filter_by(id=consumer_id).first()
    if not consumer:
        return jsonify({"error": "Consumer not found"}), 404

    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount_kes", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "amount_kes must be a number"}), 400
    if amount <= 0:
        return jsonify({"error": "Payment amount must be positive"}), 400
    if amount > 10_000_000:
        return jsonify({"error": "Payment amount out of range"}), 400
    amount = round(amount, 2)

    method = (str(data.get("method") or "cash")).strip().lower()[:30] or "cash"
    reference = (str(data.get("reference") or "")).strip()[:80]

    readings = (MeterReading.query
                .filter_by(consumer_id=consumer.id)
                .order_by(MeterReading.reading_date.asc(),
                          MeterReading.id.asc())
                .all())
    if not readings:
        return jsonify({
            "error": "Cannot record payment: no meter readings on file yet. "
                     "Add the first reading before accepting payment."
        }), 400

    # Snapshot of outstanding BEFORE applying
    previous_balance = round(sum(max(r.balance, 0.0) for r in readings), 2)

    remaining = amount
    allocations = []
    for r in readings:
        owed = round((r.amount_kes or 0.0) - (r.amount_paid or 0.0), 2)
        if owed <= 0:
            continue
        apply = round(min(remaining, owed), 2)
        if apply <= 0:
            break
        r.amount_paid = round((r.amount_paid or 0.0) + apply, 2)
        allocations.append({
            "reading_id": r.id,
            "reading_date": _fmt_date(r.reading_date),
            "reading_m3": f"{r.reading_m3:.2f}",
            "amount_kes": _fmt_money(r.amount_kes),
            "applied": _fmt_money(apply),
            "new_balance": _fmt_money(r.amount_kes - r.amount_paid),
            "note": "",
        })
        remaining = round(remaining - apply, 2)
        if remaining <= 0:
            break

    if remaining > 0:
        newest = readings[-1]
        newest.amount_paid = round((newest.amount_paid or 0.0) + remaining, 2)
        allocations.append({
            "reading_id": newest.id,
            "reading_date": _fmt_date(newest.reading_date),
            "reading_m3": f"{newest.reading_m3:.2f}",
            "amount_kes": _fmt_money(newest.amount_kes),
            "applied": _fmt_money(remaining),
            "new_balance": _fmt_money(newest.amount_kes - newest.amount_paid),
            "note": "(Prepayment credit)",
        })
        remaining = 0

    # State AFTER applying
    remaining_balance = round(sum(max(r.balance, 0.0) for r in readings), 2)
    prepaid_credit = round(sum(max(-r.balance, 0.0) for r in readings), 2)

    # Status AFTER applying
    info = get_consumer_status(list(reversed(readings)))  # newest first
    newest = readings[-1]

    admin = _current_admin()
    recorded_by = (admin.username if admin else "legacy-secret")[:60]

    log = PaymentLog(
        consumer_id=consumer.id,
        amount_kes=amount,
        method=method,
        reference=reference or None,
        recorded_by=recorded_by,
        # C5e1 Finding #1: PaymentLog.tenant_id must match the consumer's
        # tenant. Without this line the model default (1) tags every
        # manual payment as Simon Fresh Water's, breaking receipt
        # downloads for any other tenant and misattributing reports.
        tenant_id=consumer.tenant_id,
    )
    db.session.add(log)
    db.session.flush()   # get log.id

    receipt_no = f"{log.id:06d}"
    created = _as_nairobi(log.created_at)

    snapshot = {
        "receipt_no": receipt_no,
        "receipt_date": created.strftime("%d %b %Y"),
        "receipt_time": created.strftime("%H:%M"),
        "cust_name": consumer.cust_name,
        "acc_name": consumer.acc_name,
        "meter_acc_no": consumer.meter_acc_no,
        "contact": consumer.contact or "—",
        "email": consumer.email or "—",
        "address": consumer.address or "—",
        "amount_kes": _fmt_money(amount),
        "method": method.capitalize(),
        "reference": reference or "—",
        "recorded_by": recorded_by,
        "previous_balance": _fmt_money(previous_balance),
        "remaining_balance": _fmt_money(remaining_balance),
        "prepaid_credit": _fmt_money(prepaid_credit),
        "status_label": info["label"],
        "last_reading_date": _fmt_date(newest.reading_date),
        "last_reading_m3": f"{newest.reading_m3:.2f}",
        "allocations": allocations,
    }
    # ── Statement: last 5 readings, newest first, with applied-from-this-payment
    touched_ids = {a["reading_id"] for a in allocations}
    applied_by_id = {a["reading_id"]: a["applied"] for a in allocations}

    statement = []
    for r in reversed(readings[-5:]):   # readings asc; take last 5, reverse → desc
        applied_amt = applied_by_id.get(r.id, "0.00")
        statement.append({
            "reading_id": r.id,
            "reading_date": _fmt_date(r.reading_date),
            "reading_m3": f"{r.reading_m3:.2f}",
            "amount_kes": _fmt_money(r.amount_kes),
            "paid": _fmt_money(r.amount_paid),
            "applied": applied_amt,
            "new_balance": _fmt_money(r.amount_kes - r.amount_paid),
            "touched": r.id in touched_ids,
        })
    snapshot["statement"] = statement

    log.receipt_json = json.dumps(snapshot)

    db.session.commit()

    _audit("record_payment",
           target_type="payment", target_id=log.id,
           target_label=f"{consumer.cust_name} · KES {amount:.2f}",
           detail=f"method={method}, ref={reference or '—'}")

    return jsonify({
        "ok": True,
        "payment_id": log.id,
        "receipt_no": receipt_no,
        "amount_kes": amount,
        "method": method,
        "reference": reference or None,
        "allocations": allocations,
    }), 201


@app.route("/api/admin/payment/<int:payment_id>/receipt.pdf")
def download_receipt(payment_id):
    """Generate and stream the receipt PDF for a specific payment."""
    if not _rate_limit("receipt", 20, 60):
        return _too_many(60)

    u = _require_admin()
    if u: return u
    # IDOR-FIX-PHASE4: tenant-scoped lookup. A demo admin cannot read
    # SFW's payment receipt by guessing an id — the query returns nothing
    # and the response is 404.
    log = _tenant_query(PaymentLog).filter_by(id=payment_id).first()
    if not log:
        return jsonify({"error": "Payment not found"}), 404

    if not log.receipt_json:
        return jsonify({
            "error": "Receipt not available for this payment. "
                     "Only payments recorded after the receipt feature was enabled have receipts."
        }), 400

    try:
        snapshot = json.loads(log.receipt_json)
    except Exception:
        return jsonify({"error": "Receipt data corrupted."}), 500

    try:
        snapshot.update(_brand_context())
        snapshot["template_r2_key"] = _resolve_template_key(g.tenant, "receipt")
        pdf_buf = generate_receipt_pdf(snapshot)
    except Exception as e:
        app.logger.exception("[Receipt] PDF generation failed")
        return jsonify({"error": f"PDF generation failed: {e}"}), 500

    filename = f"{_safe_filename(snapshot.get('cust_name',''))}_receipt_{snapshot.get('receipt_no','payment')}.pdf"
    return send_file(
        pdf_buf,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=filename,
    )


@app.route("/receipt/<int:payment_id>")
def receipt_browser_view(payment_id):
    """
    Convenience URL for admin to download a receipt straight from the browser.
    Admin-only — unauthenticated visitors are redirected to the login page.
    """
    if not _current_admin():
        return redirect(f"/admin/login?next=/receipt/{payment_id}")

    # IDOR-FIX-PHASE4: tenant-scoped lookup, same as download_receipt.
    log = _tenant_query(PaymentLog).filter_by(id=payment_id).first()
    if not log:
        return "Payment not found.", 404
    if not log.receipt_json:
        return "Receipt not available for this payment.", 400

    try:
        snapshot = json.loads(log.receipt_json)
    except Exception:
        return "Receipt data corrupted.", 500

    try:
        snapshot.update(_brand_context())
        snapshot["template_r2_key"] = _resolve_template_key(g.tenant, "receipt")
        pdf_buf = generate_receipt_pdf(snapshot)
    except Exception:
        app.logger.exception("[Receipt] PDF generation failed")
        return "PDF generation failed.", 500

    filename = f"{_safe_filename(snapshot.get('cust_name',''))}_receipt_{snapshot.get('receipt_no','payment')}.pdf"
    return send_file(
        pdf_buf,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=filename,
    )


# ═══════════════════════════════════════════════
#  ADMIN — terminate / reactivate / delete
# ═══════════════════════════════════════════════

@app.route("/api/admin/consumer/<int:consumer_id>/terminate", methods=["POST"])
def admin_terminate_consumer(consumer_id):
    u = _require_admin()
    if u: return u
    if not _rate_limit("consumer_terminate", 10, 60):
        return _too_many(60)

    consumer = _tenant_query(Consumer).filter_by(id=consumer_id).first()
    if not consumer:
        return jsonify({"error": "Consumer not found"}), 404
    if not consumer.is_active:
        return jsonify({"ok": True, "message": "Already terminated.", "is_active": False}), 200

    consumer.is_active = False
    consumer.terminated_at = datetime.utcnow()
    db.session.commit()

    _audit("terminate_consumer",
           target_type="consumer", target_id=consumer.id,
           target_label=f"{consumer.cust_name} · {consumer.meter_acc_no}")

    return jsonify({"ok": True, "message": f"{consumer.cust_name} terminated.",
                    "is_active": False,
                    "terminated_at": consumer.terminated_at.isoformat()})


@app.route("/api/admin/consumer/<int:consumer_id>/reactivate", methods=["POST"])
def admin_reactivate_consumer(consumer_id):
    u = _require_admin()
    if u: return u
    if not _rate_limit("consumer_reactivate", 10, 60):
        return _too_many(60)

    consumer = _tenant_query(Consumer).filter_by(id=consumer_id).first()
    if not consumer:
        return jsonify({"error": "Consumer not found"}), 404

    consumer.is_active = True
    consumer.terminated_at = None
    db.session.commit()

    _audit("reactivate_consumer",
           target_type="consumer", target_id=consumer.id,
           target_label=f"{consumer.cust_name} · {consumer.meter_acc_no}")

    return jsonify({"ok": True, "message": f"{consumer.cust_name} reactivated.",
                    "is_active": True})


@app.route("/api/admin/consumer/<int:consumer_id>", methods=["DELETE"])
def admin_delete_consumer(consumer_id):
    u = _require_admin()
    if u: return u
    if not _rate_limit("consumer_delete", 3, 300):
        return _too_many(300)

    consumer = _tenant_query(Consumer).filter_by(id=consumer_id).first()
    if not consumer:
        return jsonify({"error": "Consumer not found"}), 404

    name = consumer.cust_name

    # 1. Sent-bill archive — delete R2 objects first, then DB rows.
    #    This satisfies the FK constraint on sent_bill_archive.consumer_id
    #    and prevents orphaned files in Cloudflare R2.
    #    R2 failures are logged but never block the delete — a stale
    #    object in R2 costs ~nothing; blocking user intent does not.
    archive_rows = SentBillArchive.query.filter_by(consumer_id=consumer.id).all()
    r2_failed = 0
    for row in archive_rows:
        if row.storage_key:
            try:
                ok = archive.delete_pdf(row.storage_key)
                if not ok:
                    r2_failed += 1
                    app.logger.warning(
                        f"[Delete] R2 delete returned False for "
                        f"key={row.storage_key!r} "
                        f"(archive_id={row.id}, consumer={consumer.id})"
                    )
            except Exception:
                r2_failed += 1
                app.logger.exception(
                    f"[Delete] R2 delete raised for key={row.storage_key!r} "
                    f"(archive_id={row.id}, consumer={consumer.id})"
                )
        db.session.delete(row)

    # 2. M-Pesa logs — MUST be deleted BEFORE PaymentLog
    #    (FK: mpesa_log.payment_log_id → payment_log.id)
    mpesa_rows = MpesaLog.query.filter_by(consumer_id=consumer.id).count()
    MpesaLog.query.filter_by(consumer_id=consumer.id).delete(synchronize_session=False)

    # Belt-and-braces: any MpesaLog whose consumer_id is NULL but whose
    # payment_log_id still points at one of this consumer's payments.
    _pay_ids = [p.id for p in PaymentLog.query.filter_by(consumer_id=consumer.id).all()]
    orphan_mpesa = 0
    if _pay_ids:
        orphan_mpesa = (MpesaLog.query
                        .filter(MpesaLog.payment_log_id.in_(_pay_ids))
                        .delete(synchronize_session=False))

    # 3. Payments, notifications, readings
    PaymentLog.query.filter_by(consumer_id=consumer.id).delete()
    NotificationLog.query.filter_by(consumer_id=consumer.id).delete()
    MeterReading.query.filter_by(consumer_id=consumer.id).delete()

    # 3. Consumer itself
    db.session.delete(consumer)
    db.session.commit()

    # 4. Audit log — records exactly what was deleted
    app.logger.info(
        f"[Delete] consumer={consumer_id} name={name!r} "
        f"archives_removed={len(archive_rows)} r2_orphaned={r2_failed} "
        f"mpesa_removed={mpesa_rows} orphan_mpesa_removed={orphan_mpesa}"
    )

    _audit("delete_consumer",
           target_type="consumer", target_id=consumer_id,
           target_label=name,
           detail=f"archives_removed={len(archive_rows)}, "
                  f"r2_orphaned={r2_failed}, mpesa_removed={mpesa_rows}, "
                  f"orphan_mpesa_removed={orphan_mpesa}")

    return jsonify({
        "ok": True,
        "message": f"{name} permanently deleted.",
        "archives_removed": len(archive_rows),
        "r2_orphaned": r2_failed,
        "mpesa_removed": mpesa_rows,
        "orphan_mpesa_removed": orphan_mpesa,
    })


# ═══════════════════════════════════════════════
#  ADMIN — seed + recompute
# ═══════════════════════════════════════════════

SEED_DATA = []


@app.route("/api/admin/seed", methods=["POST"])
def admin_seed():
    # IDOR-FIX-PHASE4: previously _require_admin — any tenant admin could
    # wipe all tenants' data. Now super-only AND deletes are scoped to
    # the current tenant, so even a super admin cannot inadvertently wipe
    # another tenant's rows by accident.
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("seed", 3, 300):
        return _too_many(300)

    replace = request.args.get("replace") == "1"
    if replace:
        tid = _write_tenant_id()
        if tid < 1:
            return jsonify({"error": "No tenant context for this request."}), 400
        PaymentLog.query.filter_by(tenant_id=tid).delete()
        NotificationLog.query.filter_by(tenant_id=tid).delete()
        MeterReading.query.filter_by(tenant_id=tid).delete()
        Consumer.query.filter_by(tenant_id=tid).delete()
        db.session.commit()
        return jsonify({"ok": True, "wiped": True,
                        "tenant_id": tid, "created": 0}), 200

    return jsonify({"created": 0, "skipped": 0, "replaced": False}), 200


@app.route("/api/admin/recompute-amounts", methods=["POST"])
def admin_recompute_amounts():
    u = _require_admin()
    if u: return u
    # C5e2 Fix 3: rate limit (was unlimited)
    if not _rate_limit("recompute_amounts", 3, 300):
        return _too_many(300)

    # C5e2 Fix 1: tenant-scoped. Before: Consumer.query.all() rewrote the
    # amount_kes of EVERY reading of EVERY tenant on a single call.
    tid = _write_tenant_id()
    if tid < 1:
        return jsonify({"error": "No tenant context for this request."}), 400

    updated = scanned = skipped_paid = 0
    for c in Consumer.query.filter_by(tenant_id=tid).all():
        initial = float(c.meter_initial_reading_m3 or 0.0)
        readings = (MeterReading.query
                    .filter_by(consumer_id=c.id)
                    .order_by(MeterReading.reading_date.asc(),
                              MeterReading.id.asc()).all())
        prev_m3 = None
        for r in readings:
            scanned += 1
            new_amt = compute_amount(r.reading_m3, prev_m3, initial)
            # C5e2 Fix 2: never modify a bill that has any money applied.
            # Silent recompute of a paid bill would make it appear unpaid
            # and create false debt (amount_kes increases) or wipe credit
            # (amount_kes decreases) without the admin's intent.
            if (r.amount_paid or 0.0) > 0:
                if abs(new_amt - (r.amount_kes or 0.0)) > 0.001:
                    skipped_paid += 1
                prev_m3 = r.reading_m3
                continue
            if abs(new_amt - (r.amount_kes or 0.0)) > 0.001:
                r.amount_kes = new_amt
                updated += 1
            prev_m3 = r.reading_m3

    db.session.commit()
    # C5e2 Fix 4: audit trail
    _audit("recompute_amounts",
           target_type="tenant", target_id=tid,
           detail=f"scanned={scanned} updated={updated} skipped_paid={skipped_paid}")
    return jsonify({
        "ok": True,
        "tenant_id": tid,
        "readings_scanned": scanned,
        "readings_updated": updated,
        "readings_skipped_paid": skipped_paid,
    }), 200


# ═══════════════════════════════════════════════
#  ADMIN — BULK SMS
# ═══════════════════════════════════════════════

@app.route("/api/admin/bulk_sms/overdue", methods=["GET"])
def admin_bulk_sms_overdue():
    """
    List consumers with outstanding balances.
    Query params:
      include_due=1  → also include DUE (default: only OVERDUE)
    """
    u = _require_admin()
    if u: return u
    if not _rate_limit("bulk_sms_list", 20, 60):
        return _too_many(60)

    include_due = request.args.get("include_due") == "1"

    allowed = {"OVERDUE"}
    if include_due:
        allowed.add("DUE")

    consumers = (_tenant_query(Consumer)
                 .filter_by(is_active=True)
                 .order_by(Consumer.cust_name.asc())
                 .all())

    out = []
    for c in consumers:
        readings = _all_readings(c.id)
        _disp = _display_latest_balance(
            list(reversed(readings)),
            _consumer_payments(c.id),
        )
        info = get_consumer_status(readings, latest_balance_override=_disp)
        if info["status"] not in allowed:
            continue
        out.append({
            "id": c.id,
            "cust_name": c.cust_name,
            "meter_acc_no": c.meter_acc_no,
            "contact": c.contact or "",
            "status": info["status"],
            "status_label": info["label"],
            "total_due": round(float(info.get("total_due", 0.0)), 2),
        })

    return jsonify({"consumers": out, "count": len(out)})


@app.route("/api/admin/bulk_sms/send", methods=["POST"])
def admin_bulk_sms_send():
    """
    Send the bill-reminder SMS to a selected list of consumer IDs.
    Body:
      {"consumer_ids": [1, 2, 3]}
    Hard cap: BULK_SMS_MAX_BATCH (default 30) per call.
    """
    u = _require_admin()
    if u: return u
    if not _rate_limit("bulk_sms_send", 3, 300):
        return _too_many(300)

    data = request.get_json(silent=True) or {}
    ids = data.get("consumer_ids") or []

    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "consumer_ids must be a non-empty list"}), 400
    if len(ids) > BULK_SMS_MAX_BATCH:
        return jsonify({
            "error": f"Too many recipients. Max {BULK_SMS_MAX_BATCH} per batch."
        }), 400

    # Fetch consumers + compute info
    items = []
    missing = []
    for cid in ids:
        c = _tenant_query(Consumer).filter_by(id=cid).first()
        if not c or not c.is_active:
            missing.append(cid)
            continue
        readings = _all_readings(c.id)
        _disp = _display_latest_balance(
            list(reversed(readings)),
            _consumer_payments(c.id),
        )
        info = get_consumer_status(readings, latest_balance_override=_disp)
        if info["status"] not in ("DUE", "OVERDUE"):
            missing.append(cid)
            continue
        items.append({"consumer": c, "info": info})

    if not items:
        return jsonify({"error": "No eligible consumers in the selection."}), 400

    admin = _current_admin()
    operator = (admin.username if admin else "admin")[:60]

    try:
        result = send_bulk_sms(items, operator=operator)
    except Exception as e:
        app.logger.exception("[BulkSMS] failed")
        return jsonify({"error": f"Send failed: {e}"}), 500

    if missing:
        result["skipped_ids"] = missing

    _audit("bulk_sms_send",
           detail=f"recipients={len(items)}, "
                  f"ok={result.get('sent', result.get('ok_count', 0))}, "
                  f"failed={result.get('failed', result.get('fail_count', 0))}, "
                  f"skipped={len(missing)}")

    return jsonify(result), 200


# ═══════════════════════════════════════════════
#  M-PESA C2B — PUBLIC CALLBACKS
# ═══════════════════════════════════════════════

def _normalize_msisdn(msisdn: str) -> list:
    """Return possible local formats for a Kenyan MSISDN."""
    digits = "".join(c for c in (msisdn or "") if c.isdigit())
    if not digits:
        return []
    variants = {digits}
    if digits.startswith("254") and len(digits) == 12:
        local = "0" + digits[3:]
        variants.add(local)
        variants.add("+" + digits)
    elif digits.startswith("0") and len(digits) == 10:
        variants.add("254" + digits[1:])
        variants.add("+254" + digits[1:])
    return list(variants)


# ── Safe-path aliases for Safaricom callback registration ──
# Safaricom rejects URLs containing "mpesa", "m-pesa", "safaricom", "exe", "cmd", "sql".
# These aliases use a clean path so the URLs pass Safaricom's validation.

@app.route("/api/pay/c2b/validation", methods=["POST"])
def pay_c2b_validation():
    """Safe-path alias for mpesa_c2b_validation."""
    return mpesa_c2b_validation()


@app.route("/api/pay/c2b/confirmation", methods=["POST"])
def pay_c2b_confirmation():
    """Safe-path alias for mpesa_c2b_confirmation."""
    return mpesa_c2b_confirmation()


@app.route("/api/mpesa/c2b/validation", methods=["POST"])
def mpesa_c2b_validation():
    """
    Safaricom calls this BEFORE processing a payment.
    We always accept (ResponseCode=0) — bill matching happens post-payment.
    """
    if not _mpesa_ip_ok():
        app.logger.warning(f"[M-Pesa] Rejected validation from {_client_ip()}")
        return jsonify({"ResultCode": 1, "ResultDesc": "Forbidden"}), 403
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200


# (Note: deliberately not decorated with @app.route — this helper requires
# an argument, so Flask cannot dispatch to it. The public route is
# /api/pay/c2b/confirmation.)
def _resolve_callback_tenant(business_shortcode: str):
    """Resolve the tenant a C2B callback belongs to. Matches the incoming
    BusinessShortCode against either the tenant's display paybill or its
    dedicated mpesa_shortcode — whichever is configured.
    Returns the Tenant or None."""
    code = (business_shortcode or "").strip()
    if not code:
        return None
    try:
        return (Tenant.query
                .filter(db.or_(Tenant.paybill == code,
                               Tenant.mpesa_shortcode == code),
                        Tenant.is_active == True)
                .first())
    except Exception:
        return None


def _resolve_dispensing_tenant(business_shortcode: str):
    """Return the tenant whose dispensing_paybill matches this shortcode,
    or None. Distinct from _resolve_callback_tenant so the C2B handler
    can route a dispensing payment to DispensePayment BEFORE the consumer
    match path runs. A dispensing payment must never credit a consumer."""
    code = (business_shortcode or "").strip()
    if not code:
        return None
    try:
        return (Tenant.query
                .filter(Tenant.dispensing_paybill == code,
                        Tenant.is_active == True)
                .first())
    except Exception:
        return None


def mpesa_c2b_confirmation():
    """
    Safaricom calls this AFTER a payment is processed.
    Public endpoint (Safaricom cannot authenticate). Idempotent on TransID.
    """
    if not _mpesa_ip_ok():
        app.logger.warning(f"[M-Pesa] Rejected confirmation from {_client_ip()}")
        return jsonify({"ResultCode": 1, "ResultDesc": "Forbidden"}), 403

    data = request.get_json(silent=True) or {}
    trans_id = (data.get("TransID") or "").strip()
    if not trans_id:
        return jsonify({"ResultCode": 1, "ResultDesc": "Missing TransID"}), 400

    # Resolve tenant from the paybill in this callback. Two independent
    # lookups: one for dispensing (priority) and one for consumer billing.
    _shortcode = str(data.get("BusinessShortCode") or "").strip()
    _cb_tenant         = _resolve_callback_tenant(_shortcode)
    _dispensing_tenant = _resolve_dispensing_tenant(_shortcode)

    # Idempotency: if we've seen this TransID (for this tenant), silently accept.
    _dup_q = MpesaLog.query.filter_by(trans_id=trans_id)
    if _cb_tenant is not None:
        _dup_q = _dup_q.filter_by(tenant_id=_cb_tenant.id)
    if _dup_q.first():
        return jsonify({"ResultCode": 0, "ResultDesc": "Already processed"}), 200

    try:
        amount = float(data.get("TransAmount") or 0)
    except (ValueError, TypeError):
        amount = 0.0

    bill_ref = (data.get("BillRefNumber") or "").strip()
    msisdn = (data.get("MSISDN") or "").strip()

    _effective_tenant = _dispensing_tenant if _dispensing_tenant else _cb_tenant
    log = MpesaLog(
        tenant_id=_effective_tenant.id if _effective_tenant else -1,
        trans_id=trans_id,
        trans_time=str(data.get("TransTime") or ""),
        trans_amount=amount,
        business_shortcode=_shortcode,
        bill_ref_number=bill_ref,
        msisdn=msisdn,
        first_name=data.get("FirstName"),
        middle_name=data.get("MiddleName"),
        last_name=data.get("LastName"),
        raw_payload=json.dumps(data),
        status="received",
    )
    db.session.add(log)
    db.session.flush()

    # ── Dispensing branch (Phase G) ──
    # Fires only when the callback shortcode equals a tenant's
    # dispensing_paybill. Runs BEFORE the consumer match so a walk-in
    # water payment can never credit a consumer account.
    if _dispensing_tenant is not None:
        # Guard: profile must be enabled for this tenant.
        _enabled = _tenant_profiles_dict(_dispensing_tenant.id)
        if not _enabled.get("dispensing"):
            log.status = "dispensing_disabled"
            db.session.commit()
            return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200

        # Idempotency: TransID already recorded for this tenant.
        _existing_dp = (DispensePayment.query
                        .filter_by(tenant_id=_dispensing_tenant.id,
                                   reference=trans_id)
                        .first())
        if _existing_dp is not None:
            log.status = "duplicate"
            db.session.commit()
            return jsonify({"ResultCode": 0, "ResultDesc": "Already processed"}), 200

        if amount <= 0:
            log.status = "unmatched"
            db.session.commit()
            return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200

        try:
            _pay = DispensePayment(
                tenant_id=_dispensing_tenant.id,
                payment_date=_nairobi_today(),
                amount_kes=amount,
                method="mpesa_c2b",
                reference=trans_id,
                recorded_by="M-Pesa-C2B",
                mpesa_log_id=log.id,
            )
            db.session.add(_pay)
            log.status = "matched_dispensing"
            db.session.commit()
        except Exception as e:
            app.logger.exception("[M-Pesa] dispense payment create failed")
            log.status = "unmatched"
            db.session.commit()
            return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200

        _audit("dispense_payment_auto_recorded",
               target_type="tenant", target_id=_dispensing_tenant.id,
               detail=(f"trans_id={trans_id} amount={amount:.2f} "
                       f"shortcode={_shortcode}"))
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200

    # ── Matching: bill_ref → meter_acc_no, then msisdn → contact ──
    # Scoped to the tenant resolved from the paybill. If no tenant matched
    # the paybill, no consumer lookup happens (orphan payment).
    consumer = None
    if _cb_tenant is not None:
        if bill_ref:
            consumer = (Consumer.query
                        .filter_by(tenant_id=_cb_tenant.id, meter_acc_no=bill_ref)
                        .first())
        if not consumer and msisdn:
            variants = _normalize_msisdn(msisdn)
            if variants:
                consumer = (Consumer.query
                            .filter(Consumer.tenant_id == _cb_tenant.id)
                            .filter(Consumer.contact.in_(variants))
                            .first())

    if not consumer:
        log.status = "unmatched"
        db.session.commit()
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200

    if not consumer.is_active:
        log.status = "unmatched"
        log.consumer_id = consumer.id
        db.session.commit()
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200

    try:
        payment = _apply_fifo_payment(
            consumer, amount, method="mpesa",
            reference=trans_id, recorded_by="M-Pesa C2B",
        )
        log.status = "matched"
        log.consumer_id = consumer.id
        log.payment_log_id = payment.id
        db.session.commit()
    except Exception as e:
        app.logger.exception("[M-Pesa] FIFO failed")
        log.status = "unmatched"
        log.consumer_id = consumer.id
        db.session.commit()
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200

    # SMS confirmation (with retry — does not block the flow)
    try:
        msg = (f"Payment received: KES {amount:,.2f} for {consumer.meter_acc_no}. "
               f"Ref {trans_id}. Thank you.")
        sms_result = send_sms_with_retry(consumer.contact, msg, max_attempts=3)
        if not sms_result.get("ok"):
            app.logger.warning(f"[M-Pesa SMS] final failure: {sms_result.get('error')}")
    except Exception as e:
        app.logger.warning(f"[M-Pesa SMS] exception: {e}")

    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200


# ═══════════════════════════════════════════════
#  M-PESA C2B — ADMIN (register URLs, simulate, list)
# ═══════════════════════════════════════════════

@app.route("/api/admin/mpesa/config", methods=["GET"])
def admin_mpesa_config():
    u = _require_admin()
    if u: return u
    return jsonify(get_mpesa_config())


@app.route("/api/admin/mpesa/register", methods=["POST"])
def admin_mpesa_register():
    u = _require_admin()
    if u: return u
    if not _rate_limit("mpesa_register", 5, 60):
        return _too_many(60)

    base = request.host_url.rstrip("/")
    # Use safe paths (no forbidden words) for Safaricom registration
    conf = f"{base}/api/pay/c2b/confirmation"
    val  = f"{base}/api/pay/c2b/validation"

    # Resolve the current tenant's credentials. Falls back to Simon's
    # globals when the tenant has no per-tenant env vars configured.
    _t = getattr(g, "tenant", None)
    _slug = _t.slug if _t else None
    creds = credentials_for_tenant(_slug)
    if _t and _t.mpesa_shortcode:
        creds["shortcode"] = _t.mpesa_shortcode
    if _t and _t.mpesa_env:
        creds["env"] = _t.mpesa_env
        creds["base_url"] = ("https://api.safaricom.co.ke"
                             if creds["env"] == "production"
                             else "https://sandbox.safaricom.co.ke")

    result = register_c2b_urls_for(creds, conf, val)
    if not result.get("ok"):
        _audit("register_mpesa",
               detail=f"FAILED (tenant={_slug or 'default'}): {result.get('error')}")
        return jsonify({"ok": False, "error": result.get("error")}), 502
    _audit("register_mpesa",
           detail=f"tenant={_slug or 'default'}, shortcode={creds.get('shortcode')}, "
                  f"env={creds.get('env')}")
    return jsonify({
        "ok": True,
        "confirmation_url": conf,
        "validation_url": val,
        "shortcode": creds.get("shortcode"),
        "env": creds.get("env"),
        "response": result.get("response"),
    })


@app.route("/api/admin/mpesa/simulate", methods=["POST"])
def admin_mpesa_simulate():
    u = _require_admin()
    if u: return u
    if not _rate_limit("mpesa_simulate", 10, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    try:
        amount = int(data.get("amount", 10))
    except (ValueError, TypeError):
        return jsonify({"error": "amount must be an integer"}), 400

    msisdn = str(data.get("msisdn") or "254708374149")
    bill_ref = str(data.get("bill_ref_number") or "")

    result = simulate_c2b_payment(amount, msisdn, bill_ref)
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error")}), 502
    return jsonify({"ok": True, "response": result.get("response")})


@app.route("/api/admin/mpesa/payments", methods=["GET"])
def admin_mpesa_payments():
    u = _require_admin()
    if u: return u
    logs = (_tenant_query(MpesaLog)
            .order_by(MpesaLog.received_at.desc())
            .limit(50)
            .all())
    return jsonify({
        "payments": [{
            "id": l.id,
            "trans_id": l.trans_id,
            "trans_time": l.trans_time,
            "amount": l.trans_amount,
            "bill_ref": l.bill_ref_number,
            "msisdn": l.msisdn,
            "first_name": l.first_name,
            "status": l.status,
            "consumer_id": l.consumer_id,
            "payment_log_id": l.payment_log_id,
            "received_at": l.received_at.isoformat() if l.received_at else None,
        } for l in logs],
    })


# ═══════════════════════════════════════════════
#  ADMIN — Sent-bills archive
# ═══════════════════════════════════════════════

@app.route("/api/admin/consumer/<int:consumer_id>/bills_archive", methods=["GET"])
def admin_consumer_bills_archive(consumer_id):
    """List archived sends for one consumer (admin-only)."""
    u = _require_admin()
    if u: return u
    if not _rate_limit("bills_archive", 30, 60):
        return _too_many(60)

    consumer = _tenant_query(Consumer).filter_by(id=consumer_id).first_or_404()
    rows = (_tenant_query(SentBillArchive)
            .filter_by(consumer_id=consumer.id)
            .order_by(SentBillArchive.sent_at.desc())
            .limit(200)
            .all())

    out = []
    for r in rows:
        sent_nbo = _as_nairobi(r.sent_at)
        out.append({
            "id": r.id,
            "channel": r.channel,
            "kind": r.kind,
            "recipient_phone": r.recipient_phone,
            "sent_at": sent_nbo.isoformat(),
            "sent_at_display": sent_nbo.strftime("%d %b %Y, %H:%M"),
            "status": r.status,
            "error": r.error,
            "has_pdf": bool(r.storage_key),
            "body_text": r.body_text,
            "file_size": r.file_size,
        })

    return jsonify({"archives": out})


@app.route("/api/admin/archive/<int:archive_id>/download", methods=["GET"])
def admin_archive_download(archive_id):
    """Stream an archived PDF (admin-only)."""
    u = _require_admin()
    if u: return u
    if not _rate_limit("archive_download", 30, 60):
        return _too_many(60)

    # IDOR-FIX-PHASE4: tenant-scoped lookup. Demo admin cannot read
    # SFW's archived PDFs by guessing an id.
    row = _tenant_query(SentBillArchive).filter_by(id=archive_id).first()
    if not row:
        return jsonify({"error": "Archive not found."}), 404
    if not row.storage_key:
        return jsonify({"error": "No PDF attached to this archive row."}), 404

    data = archive.download_pdf(row.storage_key)
    if data is None:
        return jsonify({"error": "PDF not available in storage."}), 502

    from io import BytesIO
    filename = f"SimonWater_Archive_{row.id:06d}.pdf"
    return send_file(
        BytesIO(data),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=filename,
    )


@app.route("/api/admin/archive/purge", methods=["POST"])
def admin_archive_purge():
    """Manually trigger the 3-year retention purge (admin-only)."""
    u = _require_admin()
    if u: return u
    if not _rate_limit("archive_purge", 3, 300):
        return _too_many(300)

    result = archive.purge_expired(force=True)
    _audit("purge_archive",
           detail=f"purged={result.get('purged', 0)}, "
                  f"failed={result.get('failed', 0)}")
    return jsonify({"ok": True, **result})


# ═══════════════════════════════════════════════
#  ADMIN — Audit trail (read-only + manual purge)
# ═══════════════════════════════════════════════

@app.route("/api/admin/audit", methods=["GET"])
def admin_audit_list():
    u = _require_admin()
    if u: return u
    if not _rate_limit("audit_list", 30, 60):
        return _too_many(60)

    days_raw = request.args.get("days", "7")
    try:
        days = int(days_raw)
    except (ValueError, TypeError):
        days = 7
    days = max(1, min(days, 7))

    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (_tenant_query(AdminAuditLog)
            .filter(AdminAuditLog.created_at >= cutoff)
            .order_by(AdminAuditLog.created_at.desc())
            .limit(500)
            .all())

    out = []
    for r in rows:
        dt_nbo = _as_nairobi(r.created_at)
        out.append({
            "id": r.id,
            "admin_username": r.admin_username,
            "action": r.action,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "target_label": r.target_label,
            "detail": r.detail,
            "ip_address": r.ip_address,
            "created_at": dt_nbo.isoformat(),
            "created_at_display": dt_nbo.strftime("%d %b %Y, %H:%M:%S"),
        })

    return jsonify({"audit": out, "days": days, "count": len(out)})


@app.route("/api/admin/audit/purge", methods=["POST"])
def admin_audit_purge():
    global _audit_last_purge
    u = _require_admin()
    if u: return u
    if not _rate_limit("audit_purge", 3, 300):
        return _too_many(300)

    _audit_last_purge = datetime.utcnow()   # suppress opportunistic purge
    deleted = _audit_purge_run(tenant_id=_audit_tenant_id())
    _audit("purge_audit", detail=f"deleted={deleted}")
    return jsonify({"ok": True, "deleted": deleted})


# ═══════════════════════════════════════════════
#  ADMIN — Backup to Google Drive
# ═══════════════════════════════════════════════

@app.route("/api/admin/backup/run", methods=["POST"])
def admin_backup_run():
    """Build a full .xlsx backup and upload it to the configured R2 bucket."""
    # IDOR-FIX-PHASE4: previously _require_admin — allowed any tenant
    # admin to trigger a full backup. Now super-only.
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("backup_run", 3, 300):
        return _too_many(300)

    if not archive.is_configured():
        return jsonify({"ok": False,
                        "error": "R2 is not configured on this service."}), 500

    try:
        result = backup.build_and_upload(tenant_slug=_tenant_slug())
    except Exception as e:
        app.logger.exception("[Backup] run failed")
        _audit("backup_run_failed", detail=str(e)[:200])
        return jsonify({"ok": False, "error": str(e)[:200]}), 500

    if result.get("ok"):
        _audit("backup_run",
               detail=f"file={result.get('filename')}, "
                      f"size={result.get('size_bytes')}, "
                      f"key={result.get('key')}")
        return jsonify(result), 200

    _audit("backup_run_failed",
           detail=(result.get("error") or "")[:200])
    return jsonify(result), 502


@app.route("/api/admin/backup/d1/run", methods=["POST"])
def admin_d1_backup_run():
    """Manually trigger a D1 SQL backup to R2. Super-admin only.
    Rate limit: 2/hour. Useful for testing and for external cron
    (e.g. GitHub Actions) to fire the backup during low-traffic hours."""
    u = _require_super_admin()
    if u: return u
    if not _rate_limit("d1_backup_run", 2, 3600):
        return _too_many(3600)

    try:
        from d1_backup import run_backup
    except Exception as e:
        return jsonify({"ok": False,
                        "error": f"d1_backup module unavailable: {e}"}), 500

    result = run_backup(reason="manual")

    try:
        _audit("d1_backup_manual",
               target_type="tenant", target_id=1,
               detail=(f"ok={result.get('ok')} "
                       f"key={result.get('key') or '—'} "
                       f"bytes={result.get('bytes') or 0} "
                       f"error={(result.get('error') or '—')[:100]}"))
    except Exception:
        pass

    status = 200 if result.get("ok") else 500
    return jsonify(result), status


# ═══════════════════════════════════════════════
#  TEAM MANAGEMENT + RECORDING-AS (Step 2)
# ═══════════════════════════════════════════════

def _require_team_admin():
    """Return error if the caller is not an admin-role user of their tenant."""
    u = _require_admin()
    if u: return u
    if session.get("user_role", "admin") != "admin":
        return jsonify({"error": "Only admins can manage the team."}), 403
    return None


@app.route("/api/admin/team", methods=["GET"])
def admin_team_get():
    u = _require_team_admin()
    if u: return u
    if not _rate_limit("team_get", 30, 60):
        return _too_many(60)
    admin = _current_admin()
    if not admin or not admin.tenant_id:
        return jsonify({"error": "Unauthorized."}), 401
    rows = (AdminUser.query
            .filter_by(tenant_id=admin.tenant_id)
            .order_by(AdminUser.id.asc())
            .all())
    out = []
    for a in rows:
        role = getattr(a, "user_role", "admin") or "admin"
        out.append({
            "id": a.id,
            "username": a.username,
            "user_role": role,
            "is_super": bool(getattr(a, "is_super", False)),
            "is_active": bool(a.is_active),
            "phone_tail": (a.phone[-4:] if a.phone and len(a.phone) >= 4 else None),
            "email": a.email,
            "last_login_at": a.last_login_at.isoformat() if a.last_login_at else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "is_self": (a.id == admin.id),
            "permissions": _employee_permissions(a.id) if role == "employee" else None,
        })
    return jsonify({"ok": True, "team": out, "current_user_id": admin.id})


@app.route("/api/admin/team", methods=["POST"])
def admin_team_create():
    u = _require_team_admin()
    if u: return u
    if not _rate_limit("team_create", 10, 60):
        return _too_many(60)
    admin = _current_admin()
    if not admin or not admin.tenant_id:
        return jsonify({"error": "Unauthorized."}), 401

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    phone    = (data.get("phone") or "").strip()
    email    = (data.get("email") or "").strip() or None
    _raw_perms = data.get("permissions")

    if len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters."}), 400
    if len(username) > 60:
        return jsonify({"error": "Username too long (max 60)."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    if not phone:
        return jsonify({"error": "Phone is required for password reset."}), 400
    if not all(c.isalnum() or c in "@._-" for c in username):
        return jsonify({"error": "Username can only contain letters, digits, @ . _ -"}), 400
    if AdminUser.query.filter_by(username=username).first():
        return jsonify({"error": "Username already taken."}), 409

    emp = AdminUser(
        tenant_id=admin.tenant_id,
        username=username,
        password_hash=generate_password_hash(password),
        phone=phone,
        email=email,
        is_active=True,
        is_super=False,
        user_role="employee",
    )
    if isinstance(_raw_perms, dict):
        _clean = {}
        for k in _EMPLOYEE_PERMISSION_DEFAULTS:
            if k in _raw_perms:
                try:
                    _clean[k] = bool(_raw_perms[k])
                except Exception:
                    pass
        if _clean:
            emp.permissions_json = json.dumps(_clean)
    db.session.add(emp)
    db.session.commit()

    _audit("team_member_created",
           target_type="tenant", target_id=admin.tenant_id,
           detail=f"username={username} role=employee")
    return jsonify({
        "ok": True,
        "member": {"id": emp.id, "username": emp.username, "user_role": "employee"},
    }), 201


@app.route("/api/admin/team/<int:user_id>/deactivate", methods=["POST"])
def admin_team_deactivate(user_id):
    u = _require_team_admin()
    if u: return u
    if not _rate_limit("team_deactivate", 20, 60):
        return _too_many(60)
    admin = _current_admin()
    if not admin or not admin.tenant_id:
        return jsonify({"error": "Unauthorized."}), 401

    target = AdminUser.query.filter_by(id=user_id, tenant_id=admin.tenant_id).first()
    if not target:
        return jsonify({"error": "Team member not found."}), 404
    if target.id == admin.id:
        return jsonify({"error": "You cannot deactivate your own account."}), 400
    if getattr(target, "is_super", False):
        return jsonify({"error": "Super-admin accounts cannot be deactivated here."}), 400
    if not target.is_active:
        return jsonify({"error": "Already deactivated."}), 400

    # Must keep at least one active admin in the tenant
    if (getattr(target, "user_role", "admin") or "admin") == "admin":
        active_admins = (AdminUser.query
                         .filter_by(tenant_id=admin.tenant_id, is_active=True,
                                    user_role="admin")
                         .filter(AdminUser.id != target.id)
                         .count())
        if active_admins < 1:
            return jsonify({"error": "Cannot deactivate the last active admin."}), 400

    target.is_active = False
    db.session.commit()
    _audit("team_member_deactivated",
           target_type="tenant", target_id=admin.tenant_id,
           detail=f"username={target.username}")
    return jsonify({"ok": True, "deactivated_id": target.id})


@app.route("/api/admin/team/<int:user_id>/reactivate", methods=["POST"])
def admin_team_reactivate(user_id):
    u = _require_team_admin()
    if u: return u
    if not _rate_limit("team_reactivate", 20, 60):
        return _too_many(60)
    admin = _current_admin()
    if not admin or not admin.tenant_id:
        return jsonify({"error": "Unauthorized."}), 401

    target = AdminUser.query.filter_by(id=user_id, tenant_id=admin.tenant_id).first()
    if not target:
        return jsonify({"error": "Team member not found."}), 404
    if target.is_active:
        return jsonify({"error": "Already active."}), 400

    target.is_active = True
    target.failed_attempts = 0
    target.locked_until = None
    db.session.commit()
    _audit("team_member_reactivated",
           target_type="tenant", target_id=admin.tenant_id,
           detail=f"username={target.username}")
    return jsonify({"ok": True, "reactivated_id": target.id})


@app.route("/api/admin/team/<int:user_id>", methods=["DELETE"])
def admin_team_delete(user_id):
    u = _require_team_admin()
    if u: return u
    if not _rate_limit("team_delete", 3, 300):
        return _too_many(300)
    admin = _current_admin()
    if not admin or not admin.tenant_id:
        return jsonify({"error": "Unauthorized."}), 401

    target = AdminUser.query.filter_by(id=user_id, tenant_id=admin.tenant_id).first()
    if not target:
        return jsonify({"error": "Team member not found."}), 404
    if target.id == admin.id:
        return jsonify({"error": "You cannot delete your own account."}), 400
    if getattr(target, "is_super", False):
        return jsonify({"error": "Super-admin accounts cannot be deleted."}), 400

    # Must keep at least one active admin in the tenant
    if (getattr(target, "user_role", "admin") or "admin") == "admin":
        active_admins = (AdminUser.query
                         .filter_by(tenant_id=admin.tenant_id, is_active=True,
                                    user_role="admin")
                         .filter(AdminUser.id != target.id)
                         .count())
        if active_admins < 1:
            return jsonify({"error": "Cannot delete the last active admin."}), 400

    uname = target.username

    # FK cleanup — mirrors models.py design intent:
    #   admin_otp.admin_id              (NOT NULL) → delete rows
    #   password_reset_tokens.admin_id  (NOT NULL) → delete rows
    #   admin_audit_log.admin_id        (nullable) → NULL it; denormalized
    #                                     admin_username survives (by design)
    AdminOTP.query.filter_by(admin_id=target.id).delete(synchronize_session=False)
    PasswordResetToken.query.filter_by(admin_id=target.id).delete(synchronize_session=False)
    AdminAuditLog.query.filter_by(admin_id=target.id).update(
        {"admin_id": None}, synchronize_session=False)

    db.session.delete(target)
    db.session.commit()

    _audit("team_member_deleted",
           target_type="tenant", target_id=admin.tenant_id,
           detail=f"username={uname}")
    return jsonify({"ok": True, "deleted_id": user_id})


@app.route("/api/admin/team/<int:user_id>/permissions", methods=["POST"])
def admin_team_permissions(user_id):
    u = _require_team_admin()
    if u: return u
    if not _rate_limit("team_permissions", 30, 60):
        return _too_many(60)
    admin = _current_admin()
    if not admin or not admin.tenant_id:
        return jsonify({"error": "Unauthorized."}), 401

    target = AdminUser.query.filter_by(id=user_id, tenant_id=admin.tenant_id).first()
    if not target:
        return jsonify({"error": "Team member not found."}), 404
    if target.id == admin.id:
        return jsonify({"error": "Cannot change your own permissions."}), 400
    if getattr(target, "is_super", False):
        return jsonify({"error": "Super-admin permissions cannot be modified here."}), 400
    if (getattr(target, "user_role", "admin") or "admin") != "employee":
        return jsonify({"error": "Permissions apply to employees only."}), 400

    data = request.get_json(silent=True) or {}
    raw = data.get("permissions")
    if not isinstance(raw, dict):
        return jsonify({"error": "Body must contain a 'permissions' object."}), 400

    clean = {}
    for k in _EMPLOYEE_PERMISSION_DEFAULTS:
        if k in raw:
            try:
                clean[k] = bool(raw[k])
            except Exception:
                return jsonify({"error": f"Invalid value for permission '{k}'."}), 400

    target.permissions_json = json.dumps(clean) if clean else None
    db.session.commit()

    _audit("team_member_permissions_changed",
           target_type="tenant", target_id=admin.tenant_id,
           detail=f"username={target.username} perms={clean}")
    return jsonify({"ok": True, "permissions": _employee_permissions(target.id)})


@app.route("/api/admin/dispense/recording_as", methods=["POST"])
def admin_dispense_recording_as():
    """Admin-only: set which user's name gets stamped on dispensing writes.
    Target must be self OR an active employee of the same tenant."""
    u = _require_team_admin()
    if u: return u
    if not _rate_limit("dispense_recording_as", 30, 60):
        return _too_many(60)

    admin = _current_admin()
    if not admin or not admin.tenant_id:
        return jsonify({"error": "Unauthorized."}), 401

    enabled = _tenant_profiles_dict(admin.tenant_id)
    if not enabled.get("dispensing"):
        return jsonify({"error": "Dispensing is not enabled for this tenant."}), 403

    data = request.get_json(silent=True) or {}
    target_username = (data.get("as") or "").strip().lower()
    if not target_username:
        return jsonify({"error": "Missing 'as'."}), 400

    # self → clear toggle
    if target_username == (admin.username or "").lower():
        _prev = session.pop("recording_as", None)
        _audit("dispense_recording_as_changed",
               target_type="tenant", target_id=admin.tenant_id,
               detail=f"from={_prev or admin.username} to={admin.username} (self)")
        return jsonify({"ok": True, "recording_as": admin.username})

    # employee of same tenant
    emp = (AdminUser.query
           .filter_by(tenant_id=admin.tenant_id, username=target_username,
                      user_role="employee", is_active=True)
           .first())
    if not emp:
        return jsonify({"error": "Target is not an active employee of this tenant."}), 400

    _prev = session.get("recording_as") or admin.username
    session["recording_as"] = emp.username
    _audit("dispense_recording_as_changed",
           target_type="tenant", target_id=admin.tenant_id,
           detail=f"from={_prev} to={emp.username}")
    return jsonify({"ok": True, "recording_as": emp.username})


@app.route("/api/admin/consumers/all", methods=["GET"])
def admin_consumers_all():
    """Return all consumers (active + terminated), alphabetically.
    Used by the Reports page — Consumers Database block.
    Admin-only."""
    u = _require_admin()
    if u: return u
    if not _rate_limit("consumers_all", 30, 60):
        return _too_many(60)

    consumers = (_tenant_query(Consumer)
                 .order_by(db.func.lower(Consumer.cust_name).asc())
                 .all())

    return jsonify({
        "consumers": [{
            "id": c.id,
            "cust_name": c.cust_name,
            "meter_acc_no": c.meter_acc_no,
            "contact": c.contact or "",
            "is_active": bool(c.is_active),
        } for c in consumers],
    })


@app.route("/api/admin/consumers/search", methods=["GET"])
def admin_consumers_search():
    """
    Admin-only search for the M-Pesa assign form.
    Accepts q=<any substring of name, meter, or phone>.
    """
    u = _require_admin()
    if u: return u
    if not _rate_limit("consumer_search", 60, 60):
        return _too_many(60)

    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []})

    pattern = f"%{q}%"
    consumers = (_tenant_query(Consumer).filter(db.or_(
        Consumer.cust_name.ilike(pattern),
        Consumer.meter_acc_no.ilike(pattern),
        Consumer.contact.ilike(pattern),
    )).limit(15).all())

    return jsonify({
        "results": [{
            "id": c.id,
            "cust_name": c.cust_name,
            "meter_acc_no": c.meter_acc_no,
            "contact": c.contact,
        } for c in consumers],
    })


@app.route("/api/admin/mpesa/assign", methods=["POST"])
def admin_mpesa_assign():
    """Manually assign an unmatched M-Pesa payment to a consumer."""
    u = _require_admin()
    if u: return u
    if not _rate_limit("mpesa_assign", 20, 60):
        return _too_many(60)

    data = request.get_json(silent=True) or {}
    log_id = data.get("log_id")
    consumer_id = data.get("consumer_id")
    if not log_id or not consumer_id:
        return jsonify({"error": "log_id and consumer_id required"}), 400

    log = _tenant_query(MpesaLog).filter_by(id=log_id).first()
    if not log:
        return jsonify({"error": "M-Pesa log not found"}), 404
    if log.status == "matched":
        return jsonify({"error": "Already assigned"}), 409

    consumer = _tenant_query(Consumer).filter_by(id=consumer_id).first()
    if not consumer:
        return jsonify({"error": "Consumer not found"}), 404

    admin = _current_admin()
    recorded_by = (admin.username if admin else "admin")[:60]

    try:
        payment = _apply_fifo_payment(
            consumer, log.trans_amount, method="mpesa",
            reference=log.trans_id,
            recorded_by=f"{recorded_by} (manual)",
        )
        log.status = "matched"
        log.consumer_id = consumer.id
        log.payment_log_id = payment.id
        db.session.commit()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True, "payment_id": payment.id})


# ═══════════════════════════════════════════════
#  TENANTS — seed helper
# ═══════════════════════════════════════════════

# ── Per-tenant PDF template overrides ──
# R2 layout mirrors the logo convention:
#   templates/<slug>/water_bill_template.pdf
#   templates/<slug>/SimonWater_ReceiptTemplate.pdf
# If present, the override is used; otherwise the bundled PDF in
# templates/ is used. No caching — R2 HEAD per generation request.
_TEMPLATE_KINDS = {
    "water_bill": "water_bill_template.pdf",
    "receipt":    "SimonWater_ReceiptTemplate.pdf",
}


def _resolve_template_key(tenant, kind: str) -> str:
    """Return the R2 key of the tenant's custom PDF template, or "" to
    fall back to the bundled template. Never raises — any R2 lookup
    failure returns "" so PDF generation is unaffected."""
    filename = _TEMPLATE_KINDS.get(kind)
    if not filename or tenant is None:
        return ""
    slug = getattr(tenant, "slug", "") or ""
    if not slug:
        return ""
    key = f"templates/{slug}/{filename}"
    try:
        if archive.object_exists(key):
            return key
    except Exception:
        return ""
    return ""


def _seed_default_tenant():
    """Create the Simon Fresh Water tenant row on first boot.

    Idempotent — does nothing if any tenant already exists. This gives
    every existing consumer/reading/payment an implicit owner so that
    Phase 3 (tenant_id columns) can default to 1 without touching data.
    """
    try:
        if Tenant.query.count() > 0:
            return
        t = Tenant(
            slug="simon-freshwater",
            org_name="Simon Fresh Water",
            short_name="SFW",
            tagline="Pure, Reliable & Sustainable Water Supply",
            email="simonchege84@gmail.com",
            phone="+254 700 719311",
            address="Kikuyu, Kenya",
            paybill=os.environ.get("PAYBBILL", "") or os.environ.get("PAYBILL", ""),
            payment_account=None,
            logo_key="images/SimonW_Logo.png",
            theme_primary="#0d47a1",
            is_active=True,
        )
        db.session.add(t)
        db.session.commit()
        app.logger.info("[Tenants] Seeded default tenant: simon-freshwater")
    except Exception:
        try:
            app.logger.exception("[Tenants] seed failed")
            db.session.rollback()
        except Exception:
            pass


# ═══════════════════════════════════════════════
#  INIT
# ═══════════════════════════════════════════════

with app.app_context():
    db.create_all()
    _ensure_all_columns()
    _seed_default_tenant()
    try:
        start_scheduler(app, Consumer, MeterReading)
    except Exception as e:
        app.logger.warning(f"[Scheduler] failed to start: {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
