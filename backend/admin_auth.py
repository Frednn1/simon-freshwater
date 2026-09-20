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

from models import db, AdminUser, PasswordResetToken
from notifications import send_sms


LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES   = 15
RESET_TOKEN_TTL   = 15          # minutes
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

    sms_body = (
        f"SIMON FRESH WATER — Admin\n"
        f"Your password reset code is: {otp}\n"
        f"Valid {RESET_TOKEN_TTL} min. If you didn't request this, ignore."
    )
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
