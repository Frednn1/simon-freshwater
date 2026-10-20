from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class Consumer(db.Model):
    __tablename__ = 'consumers'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, default=1, index=True)
    cust_name = db.Column(db.String(100), nullable=False, index=True)
    acc_name = db.Column(db.String(100), nullable=False)
    meter_acc_no = db.Column(db.String(50), unique=True, nullable=False, index=True)
    contact = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120))
    address = db.Column(db.String(255))
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    meter_initial_reading_m3 = db.Column(db.Float, nullable=False, default=0.0)
    whatsapp_opt_in = db.Column(db.Boolean, nullable=False, default=False)
    alt_contact = db.Column(db.String(20))
    alt_whatsapp_opt_in = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    terminated_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    readings = db.relationship(
        'MeterReading', backref='consumer', lazy=True,
        order_by='MeterReading.reading_date.desc()'
    )


class MeterReading(db.Model):
    __tablename__ = 'meter_readings'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, default=1, index=True)
    consumer_id = db.Column(db.Integer, db.ForeignKey('consumers.id'), nullable=False)
    reading_m3 = db.Column(db.Float, nullable=False)
    reading_date = db.Column(db.Date, nullable=False)
    amount_kes = db.Column(db.Float, nullable=False)
    amount_paid = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def balance(self):
        return round(self.amount_kes - self.amount_paid, 2)


class PaymentLog(db.Model):
    __tablename__ = 'payment_log'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, default=1, index=True)
    consumer_id = db.Column(db.Integer, db.ForeignKey('consumers.id'),
                            nullable=False, index=True)
    amount_kes = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(30), nullable=False, default="cash")
    reference = db.Column(db.String(80))
    recorded_by = db.Column(db.String(60))
    # JSON snapshot for receipt generation (allocations, balances, status, etc.)
    receipt_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False, index=True)


class NotificationLog(db.Model):
    __tablename__ = 'notification_log'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, default=1, index=True)
    consumer_id = db.Column(db.Integer, db.ForeignKey('consumers.id'),
                            nullable=False, index=True)
    channel = db.Column(db.String(10), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    detail = db.Column(db.String(255))
    sent_at = db.Column(db.DateTime, default=datetime.utcnow,
                        nullable=False, index=True)


class MpesaLog(db.Model):
    """Audit trail for every M-Pesa C2B callback received."""
    __tablename__ = 'mpesa_log'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, default=1, index=True)
    trans_id = db.Column(db.String(40), unique=True, nullable=False, index=True)
    trans_time = db.Column(db.String(20))
    trans_amount = db.Column(db.Float, nullable=False, default=0.0)
    business_shortcode = db.Column(db.String(20))
    bill_ref_number = db.Column(db.String(60), index=True)
    msisdn = db.Column(db.String(20), index=True)
    first_name = db.Column(db.String(60))
    middle_name = db.Column(db.String(60))
    last_name = db.Column(db.String(60))
    raw_payload = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default="received",
                       index=True)  # received | matched | unmatched | duplicate
    consumer_id = db.Column(db.Integer, db.ForeignKey('consumers.id'),
                            nullable=True, index=True)
    payment_log_id = db.Column(db.Integer, db.ForeignKey('payment_log.id'),
                               nullable=True)
    received_at = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False, index=True)


class RateLimitBucket(db.Model):
    """Shared, DB-backed rate-limit counters — one row per (key, window)."""
    __tablename__ = 'rate_limit_bucket'
    id = db.Column(db.Integer, primary_key=True)
    bucket_key = db.Column(db.String(120), unique=True, nullable=False, index=True)
    count = db.Column(db.Integer, nullable=False, default=1)
    window_start = db.Column(db.DateTime, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class AdminUser(db.Model):
    __tablename__ = 'admin_users'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, default=1, index=True)
    username = db.Column(db.String(60), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120))
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_super = db.Column(db.Boolean, nullable=False, default=False)
    failed_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime)
    last_login_at = db.Column(db.DateTime)
    # Set whenever the password is changed (via settings or forgot flow).
    # Sessions created before this timestamp are invalidated on next request.
    password_changed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AdminOTP(db.Model):
    """One-time passwords for admin 2FA login."""
    __tablename__ = 'admin_otp'
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admin_users.id'),
                         nullable=False, index=True)
    otp_hash = db.Column(db.String(64), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)
    used = db.Column(db.Boolean, default=False, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False, index=True)


class PasswordResetToken(db.Model):
    __tablename__ = 'password_reset_tokens'
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('admin_users.id'),
                         nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, index=True)
    otp_hash = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ═══════════════════════════════════════════════
#  PERMANENT ARCHIVE — sent bills and reminders
# ═══════════════════════════════════════════════
# Rows are tiny (~500 bytes). For WhatsApp PDF bills, the PDF bytes
# live in Cloudflare R2 and only the storage_key is kept here.
# For SMS / WhatsApp text reminders, the message body is stored
# in body_text — no external storage needed.
#
# Retention: 3 years (1095 days). Purged automatically from the
# archive.py module (rate-limited to once per 24 h).


class SentBillArchive(db.Model):
    __tablename__ = 'sent_bill_archive'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, default=1, index=True)
    consumer_id = db.Column(db.Integer, db.ForeignKey('consumers.id'),
                            nullable=False, index=True)
    reading_id = db.Column(db.Integer, db.ForeignKey('meter_readings.id'),
                           nullable=True, index=True)
    channel = db.Column(db.String(20), nullable=False)     # 'whatsapp' | 'sms'
    kind = db.Column(db.String(30), nullable=False)        # 'bill_pdf' | 'reminder_text'
    recipient_phone = db.Column(db.String(20), nullable=False)
    template_name = db.Column(db.String(80))
    provider_message_id = db.Column(db.String(120))
    sent_at = db.Column(db.DateTime, default=datetime.utcnow,
                        nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False)      # 'sent' | 'failed'
    error = db.Column(db.String(255))
    body_text = db.Column(db.Text)
    storage_key = db.Column(db.String(255))
    file_size = db.Column(db.Integer)
    snapshot_json = db.Column(db.Text)


# ═══════════════════════════════════════════════
#  ADMIN AUDIT TRAIL — 14-day rolling activity log
# ═══════════════════════════════════════════════
# Write-only from a single helper (_audit in app.py). No endpoint
# accepts writes to this table. Rows older than AUDIT_RETENTION_DAYS
# are purged opportunistically.
#
# admin_username is denormalized so log rows survive later deletion
# of the AdminUser record. Passwords, OTP codes and message bodies
# are never written to this table.


class AdminAuditLog(db.Model):
    __tablename__ = 'admin_audit_log'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, default=1, index=True)
    admin_id = db.Column(db.Integer,
                         db.ForeignKey('admin_users.id'),
                         nullable=True, index=True)
    admin_username = db.Column(db.String(60), nullable=False, index=True)
    action = db.Column(db.String(40), nullable=False, index=True)
    target_type = db.Column(db.String(30))
    target_id = db.Column(db.Integer)
    target_label = db.Column(db.String(120))
    detail = db.Column(db.String(500))
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False, index=True)


# ═══════════════════════════════════════════════
#  TENANTS — one row per billing organization
# ═══════════════════════════════════════════════
# Phase 1 of the multi-tenant upgrade. The table is created and
# seeded on first boot (see app.py init). Nothing reads it yet.


class Tenant(db.Model):
    __tablename__ = 'tenants'
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(60), unique=True, nullable=False, index=True)
    org_name = db.Column(db.String(120), nullable=False)
    short_name = db.Column(db.String(60))
    tagline = db.Column(db.String(200))
    email = db.Column(db.String(120))
    phone = db.Column(db.String(30))
    address = db.Column(db.String(255))
    location = db.Column(db.String(60))
    paybill = db.Column(db.String(30))
    payment_account = db.Column(db.String(60))
    # Dispensing profile: separate paybill + fixed account shown to
    # walk-in customers. Independent of the billing paybill above.
    dispensing_paybill = db.Column(db.String(30))
    dispensing_account = db.Column(db.String(60))
    # Per-tenant M-Pesa configuration (public identifiers only — the
    # consumer key and secret live in Render env vars, one set per tenant).
    mpesa_shortcode = db.Column(db.String(30))
    mpesa_env = db.Column(db.String(20), default="sandbox")
    logo_key = db.Column(db.String(255))
    theme_primary = db.Column(db.String(10), default="#0d47a1")
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ═══════════════════════════════════════════════
#  WATER DISPENSING PROFILE (Phase A)
# ═══════════════════════════════════════════════

class TenantProfile(db.Model):
    """One row per (tenant, profile). Enabled/disabled from the dev portal."""
    __tablename__ = 'tenant_profiles'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    profile_name = db.Column(db.String(30), nullable=False)   # 'billing' | 'dispensing'
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    enabled_at = db.Column(db.DateTime, default=datetime.utcnow)
    enabled_by = db.Column(db.String(60))
    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'profile_name', name='uq_tenant_profile'),
    )


class DispenseMeterReading(db.Model):
    """Daily closing meter reading per tenant. One row per (tenant, date)."""
    __tablename__ = 'dispense_meter_readings'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    reading_date = db.Column(db.Date, nullable=False, index=True)
    opening_reading_m3 = db.Column(db.Float, nullable=False)
    closing_reading_m3 = db.Column(db.Float, nullable=False)
    consumption_m3 = db.Column(db.Float, nullable=False)
    recorded_by = db.Column(db.String(60))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'reading_date',
                            name='uq_dispense_reading_day'),
    )


class DispenseSale(db.Model):
    """One transaction per customer purchase (walk-in)."""
    __tablename__ = 'dispense_sales'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    sale_date = db.Column(db.Date, nullable=False, index=True)
    container_size_L = db.Column(db.Integer, nullable=False)   # 20 | 200 | 5000 | 10000
    quantity = db.Column(db.Integer, nullable=False, default=1)
    price_per_unit_kes = db.Column(db.Float, nullable=False)
    total_kes = db.Column(db.Float, nullable=False)
    recorded_by = db.Column(db.String(60))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False, index=True)


class DispensePayment(db.Model):
    """Payment received against dispensing sales. Cash or M-Pesa."""
    __tablename__ = 'dispense_payments'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    payment_date = db.Column(db.Date, nullable=False, index=True)
    amount_kes = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(20), nullable=False)  # cash | mpesa_c2b | manual
    reference = db.Column(db.String(80), index=True)
    mpesa_log_id = db.Column(db.Integer, db.ForeignKey('mpesa_log.id'),
                             nullable=True)
    recorded_by = db.Column(db.String(60))
    # Soft-void — see Phase 4 §4.4 of the design doc
    voided = db.Column(db.Boolean, nullable=False, default=False)
    voided_at = db.Column(db.DateTime)
    voided_by = db.Column(db.String(60))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False, index=True)


class DispenseRate(db.Model):
    """Configurable rate card per tenant (container_size → price)."""
    __tablename__ = 'dispense_rates'
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False, index=True)
    container_size_L = db.Column(db.Integer, nullable=False)
    price_kes = db.Column(db.Float, nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'container_size_L',
                            name='uq_dispense_rate_size'),
    )
