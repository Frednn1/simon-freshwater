"""
Admin authentication.
  • Session-based login (Flask signed cookie)
  • Passwords hashed with werkzeug (scrypt)
  • Lockout after 5 failed attempts (15 min)
  • Password reset via SMS OTP (SMSGate — already configured)
"""
import os
import secrets
import hashlib
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

from models import db, AdminUser, PasswordResetToken, AdminOTP
from notifications import send_sms


LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES   = 15
RESET_TOKEN_TTL   = 5           # minutes
OTP_LENGTH        = 6

ADMIN_SETUP_CODE = os.environ.get("ADMIN_SETUP_CODE", "")


# ─── Hashing helpers ───
def _hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()

def _hash_otp(o: str) -> str:
    return hashlib.sha256(o.encode()).hexdigest()


# ─── Bootstrap ───
def create_first_admin(username, password, phone, email, setup_code):
    if not ADMIN_SETUP_CODE:
        return {"ok": False, "error": "Setup disabled (ADMIN_SETUP_CODE not configured on server)."}
    if not secrets.compare_digest(setup_code or "", ADMIN_SETUP_CODE):
        return {"ok": False, "error": "Invalid setup code."}
    if AdminUser.query.count() > 0:
        return {"ok": False, "error": "An admin already exists. Please log in."}

    username = (username or "").strip().lower()
    if len(username) < 3:
        return {"ok": False, "error": "Username must be at least 3 characters."}
    if len(password or "") < 8:
        return {"ok": False, "error": "Password must be at least 8 characters."}
    if not (phone or "").strip():
        return {"ok": False, "error": "Phone number is required for password recovery."}

    admin = AdminUser(
        username=username,
        password_hash=generate_password_hash(password),
        phone=phone.strip(),
        email=(email or "").strip() or None,
        is_active=True,
    )
    db.session.add(admin)
    db.session.commit()
    return {"ok": True, "admin_id": admin.id, "username": admin.username}


# ─── Login ───
def login(username, password):
    username = (username or "").strip().lower()
    admin = AdminUser.query.filter_by(username=username).first()
    if not admin or not admin.is_active:
        return {"ok": False, "error": "Invalid credentials."}

    now = datetime.utcnow()
    if admin.locked_until and admin.locked_until > now:
        mins = int((admin.locked_until - now).total_seconds() // 60) + 1
        return {"ok": False, "error": f"Account locked. Try again in {mins} min."}

    if not check_password_hash(admin.password_hash, password or ""):
        admin.failed_attempts = (admin.failed_attempts or 0) + 1
        if admin.failed_attempts >= LOCKOUT_THRESHOLD:
            admin.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            admin.failed_attempts = 0
            db.session.commit()
            return {"ok": False, "error": f"Too many attempts. Locked for {LOCKOUT_MINUTES} min."}
        db.session.commit()
        left = LOCKOUT_THRESHOLD - admin.failed_attempts
        return {"ok": False, "error": f"Invalid credentials. {left} attempt(s) left."}

    admin.failed_attempts = 0
    admin.locked_until = None
    admin.last_login_at = now
    db.session.commit()
    return {"ok": True, "admin_id": admin.id, "username": admin.username}


# ─── Password reset ───
def request_password_reset(identifier):
    q = (identifier or "").strip()
    if not q:
        return {"ok": False, "error": "Provide your username or phone number."}

    admin = (AdminUser.query.filter_by(username=q.lower()).first()
             or AdminUser.query.filter_by(phone=q).first())
    if not admin:
        # Don't reveal existence
        return {"ok": True, "message": "If the account exists, a code was sent.",
                "token": None, "sms_ok": False}

    # Invalidate any prior unused tokens for this admin
    PasswordResetToken.query.filter_by(admin_id=admin.id, used=False).update({"used": True})
    db.session.commit()

    reset_token = secrets.token_urlsafe(32)
    otp = f"{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}"

    tok = PasswordResetToken(
        admin_id=admin.id,
        token_hash=_hash_token(reset_token),
        otp_hash=_hash_otp(otp),
        expires_at=datetime.utcnow() + timedelta(minutes=RESET_TOKEN_TTL),
        used=False,
    )
    db.session.add(tok)
    db.session.commit()

    sms_body = f"Your Simon Fresh Water reset code: {otp}  (valid {RESET_TOKEN_TTL} min)"
    sms_result = send_sms(admin.phone, sms_body)

    tail = admin.phone[-4:] if len(admin.phone) >= 4 else "****"
    if sms_result.get("ok"):
        msg = f"Reset code sent to phone ending ...{tail}."
    else:
        msg = f"SMS could not be sent: {sms_result.get('error','unknown')}"

    return {"ok": True, "message": msg, "token": reset_token,
            "sms_ok": bool(sms_result.get("ok"))}


def confirm_password_reset(reset_token, otp, new_password):
    if not reset_token or not otp or not new_password:
        return {"ok": False, "error": "All fields are required."}
    if len(new_password) < 8:
        return {"ok": False, "error": "Password must be at least 8 characters."}

    now = datetime.utcnow()
    tok = PasswordResetToken.query.filter_by(
        token_hash=_hash_token(reset_token), used=False
    ).first()

    if not tok:
        return {"ok": False, "error": "Invalid or already-used reset token."}
    if tok.expires_at < now:
        return {"ok": False, "error": "Reset token has expired."}
    if tok.otp_hash != _hash_otp(otp):
        return {"ok": False, "error": "Incorrect OTP code."}

    admin = AdminUser.query.get(tok.admin_id)
    if not admin:
        return {"ok": False, "error": "Admin no longer exists."}

    admin.password_hash = generate_password_hash(new_password)
    admin.failed_attempts = 0
    admin.locked_until = None
    tok.used = True
    db.session.commit()
    return {"ok": True, "message": "Password updated. You can now log in."}


# ═══════════════════════════════════════════════
#  2FA — LOGIN OTP
# ═══════════════════════════════════════════════

LOGIN_OTP_TTL_MIN    = 5       # minutes
LOGIN_OTP_MAX_TRIES  = 5       # verification attempts per OTP
LOGIN_OTP_MAX_SENDS  = 3       # OTP sends per phone per 10 min
LOGIN_OTP_RESEND_GAP = 30      # seconds between resends


def _hash_otp(o: str) -> str:
    return hashlib.sha256(o.encode()).hexdigest()


def generate_login_otp(admin) -> dict:
    """
    Create and send a 6-digit OTP to the admin's phone.
    Enforces: max 3 sends per phone per 10 minutes, 30-second cooldown.
    Returns {ok, message?, error?}.
    """
    from datetime import datetime, timedelta
    from notifications import send_sms

    now = datetime.utcnow()

    # Cooldown check — most recent non-expired OTP
    recent = (AdminOTP.query
              .filter_by(admin_id=admin.id, used=False)
              .filter(AdminOTP.expires_at > now)
              .order_by(AdminOTP.created_at.desc())
              .first())
    if recent:
        elapsed = (now - recent.created_at).total_seconds()
        if elapsed < LOGIN_OTP_RESEND_GAP:
            wait = int(LOGIN_OTP_RESEND_GAP - elapsed) + 1
            return {"ok": False, "error": f"Please wait {wait}s before requesting a new code."}
        recent.used = True   # invalidate the old OTP
        db.session.commit()

    # Rate limit: max N sends per phone per 10 min
    cutoff = now - timedelta(minutes=10)
    sends_last_10min = (AdminOTP.query
                        .filter_by(admin_id=admin.id)
                        .filter(AdminOTP.created_at >= cutoff)
                        .count())
    if sends_last_10min >= LOGIN_OTP_MAX_SENDS:
        return {"ok": False,
                "error": "Too many codes requested. Please try again in a few minutes."}

    # Generate and store
    otp = f"{secrets.randbelow(10 ** 6):06d}"
    record = AdminOTP(
        admin_id=admin.id,
        otp_hash=_hash_otp(otp),
        phone=admin.phone,
        expires_at=now + timedelta(minutes=LOGIN_OTP_TTL_MIN),
        attempts=0,
        used=False,
    )
    db.session.add(record)
    db.session.commit()

    # Send SMS (best-effort)
    body = f"Your Simon Fresh Water login code: {otp}  (valid {LOGIN_OTP_TTL_MIN} min)"
    sms_result = send_sms(admin.phone, body)
    if not sms_result.get("ok"):
        return {"ok": False,
                "error": f"Could not send SMS: {sms_result.get('error', 'unknown')}"}

    return {"ok": True,
            "message": f"Verification code sent to phone ending ...{admin.phone[-4:]}"}


def verify_login_otp(admin, otp: str) -> dict:
    """
    Verify an OTP for the admin. Increments attempt counter on failure.
    On success, marks the OTP used and returns {ok: True}.
    """
    from datetime import datetime

    otp = (otp or "").strip()
    if not otp:
        return {"ok": False, "error": "Please enter the 6-digit code."}

    now = datetime.utcnow()
    record = (AdminOTP.query
              .filter_by(admin_id=admin.id, used=False)
              .filter(AdminOTP.expires_at > now)
              .order_by(AdminOTP.created_at.desc())
              .first())

    if not record:
        return {"ok": False, "error": "No active code. Please request a new one."}

    if record.attempts >= LOGIN_OTP_MAX_TRIES:
        record.used = True
        db.session.commit()
        return {"ok": False, "error": "Too many incorrect attempts. Please request a new code."}

    record.attempts += 1

    # Constant-time comparison
    if not secrets.compare_digest(record.otp_hash, _hash_otp(otp)):
        db.session.commit()
        left = LOGIN_OTP_MAX_TRIES - record.attempts
        return {"ok": False,
                "error": f"Incorrect code. {left} attempt(s) left."}

    record.used = True
    db.session.commit()
    return {"ok": True}
