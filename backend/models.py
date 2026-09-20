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
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    terminated_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    readings = db.relationship(
        'MeterReading',
        backref='consumer',
        lazy=True,
        order_by='MeterReading.reading_date.desc()'
    )


class MeterReading(db.Model):
    __tablename__ = 'meter_readings'
    id = db.Column(db.Integer, primary_key=True)
    consumer_id = db.Column(
        db.Integer, db.ForeignKey('consumers.id'), nullable=False
    )
    reading_m3 = db.Column(db.Float, nullable=False)
    reading_date = db.Column(db.Date, nullable=False)
    amount_kes = db.Column(db.Float, nullable=False)
    amount_paid = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def balance(self):
        return round(self.amount_kes - self.amount_paid, 2)


class NotificationLog(db.Model):
    __tablename__ = 'notification_log'
    id = db.Column(db.Integer, primary_key=True)
    consumer_id = db.Column(
        db.Integer, db.ForeignKey('consumers.id'), nullable=False, index=True
    )
    channel = db.Column(db.String(10), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    detail = db.Column(db.String(255))
    sent_at = db.Column(db.DateTime, default=datetime.utcnow,
                        nullable=False, index=True)
