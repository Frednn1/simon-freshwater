"""
Full-database backup to Cloudflare R2 as a styled Excel workbook.

  • build_backup_workbook() → bytes of the .xlsx (8 tabs)
  • upload_to_r2(filename, data) → S3-compatible PUT, returns metadata

Auth: reuses the same R2 env vars as archive.py —
R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET.

Destination: backups/YYYY/MM/ prefix inside the configured bucket.
Retention: manual (no auto-delete).
"""
import os
from datetime import datetime, timezone, timedelta
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter


# ─── Cloudflare R2 configuration (mirrors archive.py — same env vars) ───
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "").strip()
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "").strip()
R2_BUCKET     = os.environ.get("R2_BUCKET", "").strip()

_NAIROBI_TZ = timezone(timedelta(hours=3), name="Africa/Nairobi")

# Excel palette
_HEADER_FILL  = PatternFill("solid", fgColor="0D47A1")
_HEADER_FONT  = Font(bold=True, color="FFFFFF", size=11)
_HEADER_ALIGN = Alignment(horizontal="left", vertical="center")
_TITLE_FONT   = Font(bold=True, color="0D47A1", size=14)
_LABEL_FONT   = Font(bold=True, color="455A64", size=11)
_VALUE_FONT   = Font(color="0D47A1", size=12, bold=True)


# ═══════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════

def _now_nbo() -> datetime:
    return datetime.now(_NAIROBI_TZ)


def _fmt_dt(dt):
    """UTC-naive DB datetime → Nairobi-formatted string."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_NAIROBI_TZ).strftime("%d %b %Y %H:%M:%S")


def _fmt_date(d):
    if d is None:
        return ""
    if hasattr(d, "strftime"):
        return d.strftime("%d %b %Y")
    return str(d)


def _bool_str(b):
    return "Yes" if b else "No"


def _autosize_and_freeze(ws, headers):
    """Bold-blue header row, frozen top row, filter, auto-width columns."""
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _HEADER_ALIGN

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(ws.max_row, 1)}"

    for i, h in enumerate(headers, 1):
        max_len = len(str(h))
        for row in ws.iter_rows(min_row=2, min_col=i, max_col=i, values_only=True):
            v = row[0]
            if v is None:
                continue
            max_len = max(max_len, min(len(str(v)), 60))
        ws.column_dimensions[get_column_letter(i)].width = min(max_len + 2, 60)


def _sheet(wb, title, headers, rows):
    ws = wb.create_sheet(title=title)
    _autosize_and_freeze(ws, headers)
    for r in rows:
        ws.append(r)
    return ws


# ═══════════════════════════════════════════════
#  Data extraction — one function per tab
# ═══════════════════════════════════════════════

def _summary_sheet(wb):
    from models import (db, Consumer, MeterReading, PaymentLog,
                        SentBillArchive, MpesaLog, NotificationLog,
                        AdminAuditLog)

    ws = wb.create_sheet(title="Summary")

    total_c = Consumer.query.count()
    active_c = Consumer.query.filter_by(is_active=True).count()
    term_c = total_c - active_c

    total_r = MeterReading.query.count()
    total_p = PaymentLog.query.count()

    total_billed = float(db.session.query(db.func.sum(MeterReading.amount_kes)).scalar() or 0.0)
    total_paid   = float(db.session.query(db.func.sum(MeterReading.amount_paid)).scalar() or 0.0)
    outstanding  = max(total_billed - total_paid, 0.0)

    archive_count = SentBillArchive.query.count()
    mpesa_count   = MpesaLog.query.count()
    notif_count   = NotificationLog.query.count()

    cutoff_14 = datetime.utcnow() - timedelta(days=14)
    audit_count = (AdminAuditLog.query
                   .filter(AdminAuditLog.created_at >= cutoff_14).count())

    rows = [
        ("Generated", _now_nbo().strftime("%d %b %Y %H:%M:%S (Africa/Nairobi)")),
        ("", ""),
        ("Consumers", ""),
        ("  Total", total_c),
        ("  Active", active_c),
        ("  Terminated", term_c),
        ("", ""),
        ("Meter Readings", total_r),
        ("Payments Recorded", total_p),
        ("", ""),
        ("Financials (KES)", ""),
        ("  Total Billed",   f"{total_billed:,.2f}"),
        ("  Total Paid",     f"{total_paid:,.2f}"),
        ("  Outstanding",    f"{outstanding:,.2f}"),
        ("", ""),
        ("Activity", ""),
        ("  Archived Sends (3-yr window)", archive_count),
        ("  M-Pesa Transactions",          mpesa_count),
        ("  Notifications",                notif_count),
        ("  Audit Rows (14-day window)",   audit_count),
        ("", ""),
        ("Note", "All timestamps in this workbook are Africa/Nairobi (UTC+3)."),
        ("Note", "Passwords, OTP hashes, and password-reset tokens are excluded from all tabs."),
    ]

    for i, (label, value) in enumerate(rows, 1):
        c1 = ws.cell(row=i, column=1, value=label)
        c2 = ws.cell(row=i, column=2, value=value)
        if label and not label.startswith(" "):
            c1.font = _LABEL_FONT
        if value != "":
            c2.font = _VALUE_FONT

    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 50

    # Title
    ws.insert_rows(1, 2)
    ws["A1"] = "Simon Fresh Water — Database Backup"
    ws["A1"].font = _TITLE_FONT
    ws["A2"] = f"Generated {_now_nbo().strftime('%d %b %Y at %H:%M')} (Africa/Nairobi)"
    ws["A2"].font = _LABEL_FONT


def _consumers_rows():
    from models import Consumer
    rows = []
    for c in Consumer.query.order_by(Consumer.id.asc()).all():
        rows.append([
            c.id, c.cust_name, c.acc_name, c.meter_acc_no,
            c.contact, c.alt_contact or "",
            c.email or "", c.address or "",
            c.latitude if c.latitude is not None else "",
            c.longitude if c.longitude is not None else "",
            c.meter_initial_reading_m3,
            _bool_str(c.whatsapp_opt_in),
            _bool_str(c.alt_whatsapp_opt_in),
            "Active" if c.is_active else "Terminated",
            _fmt_dt(c.terminated_at),
            _fmt_dt(c.created_at),
        ])
    return rows


def _readings_rows():
    from models import Consumer, MeterReading
    from billing import compute_consumption
    rows = []
    for c in Consumer.query.order_by(Consumer.id.asc()).all():
        readings = (MeterReading.query
                    .filter_by(consumer_id=c.id)
                    .order_by(MeterReading.reading_date.asc(),
                              MeterReading.id.asc()).all())
        initial = float(c.meter_initial_reading_m3 or 0.0)
        prev_m3 = None
        for r in readings:
            consumption = compute_consumption(r.reading_m3, prev_m3, initial)
            prev_m3 = r.reading_m3
            status = "Cleared" if r.balance <= 0 else "Due"
            rows.append([
                r.id, c.id, c.cust_name, c.meter_acc_no,
                _fmt_date(r.reading_date),
                r.reading_m3, consumption,
                r.amount_kes, r.amount_paid or 0.0, r.balance,
                status,
                _fmt_dt(r.created_at),
            ])
    return rows


def _payments_rows():
    from models import db, Consumer, PaymentLog
    rows = []
    q = (db.session.query(PaymentLog, Consumer)
         .join(Consumer, PaymentLog.consumer_id == Consumer.id)
         .order_by(PaymentLog.id.asc()))
    for p, c in q.all():
        rows.append([
            p.id, c.id, c.cust_name, c.meter_acc_no,
            p.amount_kes, p.method,
            p.reference or "", p.recorded_by or "",
            _fmt_dt(p.created_at),
        ])
    return rows


def _bills_archive_rows():
    from models import db, Consumer, SentBillArchive
    rows = []
    q = (db.session.query(SentBillArchive, Consumer)
         .join(Consumer, SentBillArchive.consumer_id == Consumer.id)
         .order_by(SentBillArchive.id.asc()))
    for a, c in q.all():
        rows.append([
            a.id, c.cust_name, c.meter_acc_no,
            a.channel, a.kind, a.recipient_phone,
            a.template_name or "", a.provider_message_id or "",
            _fmt_dt(a.sent_at), a.status, a.error or "",
            a.file_size if a.file_size is not None else "",
            a.storage_key or "",
        ])
    return rows


def _mpesa_rows():
    from models import MpesaLog
    rows = []
    for m in MpesaLog.query.order_by(MpesaLog.id.asc()).all():
        name = " ".join(filter(None, [m.first_name, m.middle_name, m.last_name])).strip()
        rows.append([
            m.id, m.trans_id, m.trans_time or "", m.trans_amount,
            m.business_shortcode or "", m.bill_ref_number or "",
            m.msisdn or "", name,
            m.status,
            m.consumer_id if m.consumer_id is not None else "",
            m.payment_log_id if m.payment_log_id is not None else "",
            _fmt_dt(m.received_at),
        ])
    return rows


def _notifications_rows():
    from models import db, Consumer, NotificationLog
    rows = []
    q = (db.session.query(NotificationLog, Consumer)
         .outerjoin(Consumer, NotificationLog.consumer_id == Consumer.id)
         .order_by(NotificationLog.id.asc()))
    for n, c in q.all():
        rows.append([
            n.id,
            c.id if c else "",
            c.cust_name if c else "",
            c.meter_acc_no if c else "",
            n.channel, n.status, n.detail or "",
            _fmt_dt(n.sent_at),
        ])
    return rows


def _audit_rows():
    from models import AdminAuditLog
    rows = []
    cutoff = datetime.utcnow() - timedelta(days=14)
    q = (AdminAuditLog.query
         .filter(AdminAuditLog.created_at >= cutoff)
         .order_by(AdminAuditLog.id.asc()).all())
    for a in q:
        rows.append([
            a.id, a.admin_username, a.action,
            a.target_type or "",
            a.target_id if a.target_id is not None else "",
            a.target_label or "", a.detail or "",
            a.ip_address or "",
            _fmt_dt(a.created_at),
        ])
    return rows


# ═══════════════════════════════════════════════
#  Workbook build
# ═══════════════════════════════════════════════

def build_backup_workbook() -> bytes:
    """Build the full .xlsx in memory and return the bytes."""
    wb = Workbook()
    wb.remove(wb.active)  # drop default sheet; we add our own

    _summary_sheet(wb)

    _sheet(wb, "Consumers",
        ["ID", "Customer Name", "Account Name", "Meter No.",
         "Contact", "Alt. Contact", "Email", "Address",
         "Latitude", "Longitude", "Initial MTR (M³)",
         "Main WhatsApp Opt-In", "Alt WhatsApp Opt-In",
         "Status", "Terminated At", "Created At"],
        _consumers_rows())

    _sheet(wb, "Readings",
        ["Reading ID", "Consumer ID", "Customer Name", "Meter No.",
         "Reading Date", "Reading (M³)", "Consumption (M³)",
         "Billed (KES)", "Paid (KES)", "Balance (KES)",
         "Status", "Created At"],
        _readings_rows())

    _sheet(wb, "Payments",
        ["Payment ID", "Consumer ID", "Customer Name", "Meter No.",
         "Amount (KES)", "Method", "Reference", "Recorded By",
         "Created At"],
        _payments_rows())

    _sheet(wb, "BillsArchive",
        ["Archive ID", "Customer Name", "Meter No.",
         "Channel", "Kind", "Recipient",
         "Template", "Provider Message ID",
         "Sent At", "Status", "Error",
         "File Size (bytes)", "R2 Storage Key"],
        _bills_archive_rows())

    _sheet(wb, "MpesaLog",
        ["Log ID", "Trans ID", "Trans Time", "Amount (KES)",
         "Shortcode", "Bill Ref", "MSISDN", "Payer Name",
         "Status", "Consumer ID", "Payment Log ID", "Received At"],
        _mpesa_rows())

    _sheet(wb, "Notifications",
        ["Log ID", "Consumer ID", "Customer Name", "Meter No.",
         "Channel", "Status", "Detail", "Sent At"],
        _notifications_rows())

    _sheet(wb, "AuditLog",
        ["Log ID", "Admin", "Action", "Target Type",
         "Target ID", "Target Label", "Detail", "IP Address",
         "Created At"],
        _audit_rows())

    out = BytesIO()
    wb.save(out)
    return out.getvalue()


# ═══════════════════════════════════════════════
#  Google Drive upload
# ═══════════════════════════════════════════════


# ═══════════════════════════════════════════════
#  Cloudflare R2 upload
# ═══════════════════════════════════════════════

def _r2_client():
    """S3-compatible boto3 client pointed at Cloudflare R2.
    Lazy import so a missing dependency cannot break the module load."""
    import boto3
    endpoint = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def _r2_key(filename: str) -> str:
    """Fixed object key — every backup overwrites the previous one.

    The dated filename is only used for the success message shown in the UI.
    R2 always stores exactly one file at this path: backups/SimonFreshWater_Backup_LATEST.xlsx
    """
    return "backups/SimonFreshWater_Backup_LATEST.xlsx"


def _r2_configured() -> bool:
    return bool(R2_ACCOUNT_ID and R2_ACCESS_KEY and R2_SECRET_KEY and R2_BUCKET)


def upload_to_r2(filename: str, data: bytes) -> dict:
    """Upload the .xlsx bytes to R2 under the backups/ prefix.
    Returns {'ok', 'key', 'bucket', 'filename', 'size_bytes'} or {'ok': False, 'error'}."""
    if not _r2_configured():
        return {"ok": False,
                "error": "R2 is not configured on this service "
                         "(need R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET)."}

    key = _r2_key(filename)
    try:
        c = _r2_client()
        c.put_object(
            Bucket=R2_BUCKET,
            Key=key,
            Body=data,
            ContentType=("application/vnd.openxmlformats-"
                         "officedocument.spreadsheetml.sheet"),
        )
    except Exception as e:
        return {"ok": False, "error": f"R2 upload failed: {e}"}

    return {
        "ok": True,
        "key": key,
        "bucket": R2_BUCKET,
        "filename": filename,
        "size_bytes": len(data),
    }


def build_and_upload() -> dict:
    """Build the workbook and upload it to R2. Returns metadata dict."""
    filename = f"SimonFreshWater_Backup_{_now_nbo().strftime('%Y-%m-%d_%H%M')}.xlsx"

    try:
        data = build_backup_workbook()
    except Exception as e:
        return {"ok": False, "error": f"Workbook build failed: {e}"}

    return upload_to_r2(filename, data)
