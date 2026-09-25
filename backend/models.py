from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class Consumer(db.Model):
    __tablename__ = 'consumers'
    id = db.Column(db.Integer, primary_key=True)
    cust_name = db.Column(db.String(100), nullable=False, index=True)
    acc_name = db.Column(db.String(100), nullable=False)
    meter_acc_no = db.Column(db.String(50), unique=True, nullable=False, index=True)
    contact = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120))
    address = db.Column(db.String(255))
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    meter_initial_reading_m3 = db.Column(db.Float, nullable=False, default=0.0)
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


class AdminUser(db.Model):
    __tablename__ = 'admin_users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(60), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120))
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    failed_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime)
    last_login_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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
