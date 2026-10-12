"""
Permanent archive for sent bills and reminders.

Storage split:
  • WhatsApp PDF bills  → Cloudflare R2 (PDF bytes) + D1 row (metadata)
  • SMS / WhatsApp text → D1 row only (body_text column)

Retention: 3 years (1095 days). Purge runs at most once every 24 h,
triggered opportunistically from the archive-insert path in app.py.

Graceful degradation: if R2 is not configured (any of the four env
vars missing), PDF archiving is silently skipped — the send path is
completely unaffected. This lets the code ship before the bucket is
provisioned.
"""
import os
import uuid
from datetime import datetime, timedelta


# ─── R2 configuration (all four required for PDF archiving) ───
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "").strip()
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "").strip()
R2_BUCKET     = os.environ.get("R2_BUCKET", "").strip()

RETENTION_DAYS = 1095    # 3 years

# In-memory guard so purge() runs at most once per 24 h even when
# called from every insert.
_last_purge = None


def is_configured() -> bool:
    """True if all four R2 env vars are present."""
    return bool(R2_ACCOUNT_ID and R2_ACCESS_KEY and R2_SECRET_KEY and R2_BUCKET)


def _client():
    """Build a boto3 S3 client pointed at Cloudflare R2.
    boto3 is imported lazily so a missing dependency cannot break
    the rest of the app."""
    import boto3
    endpoint = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def make_key(consumer_id: int, reading_id: int, sent_at: datetime,
             tenant_slug: str = None) -> str:
    """Namespaced R2 object key.

    Tenant 1 (slug 'simon-freshwater' or None) keeps the original layout:
        bills/2026/10/consumer-42-reading-187-a3f9c2e1.pdf

    Other tenants get a slug prefix so their objects live in a separate
    virtual folder inside the same bucket:
        <slug>/bills/2026/10/consumer-42-reading-187-a3f9c2e1.pdf

    Existing keys are never rewritten — callers fetch by the stored
    storage_key verbatim, so adding this prefix is safe.
    """
    year = sent_at.strftime("%Y")
    month = sent_at.strftime("%m")
    uniq = uuid.uuid4().hex[:8]
    prefix = ""
    if tenant_slug and tenant_slug != "simon-freshwater":
        prefix = f"{tenant_slug}/"
    return f"{prefix}bills/{year}/{month}/consumer-{consumer_id}-reading-{reading_id}-{uniq}.pdf"


def fetch_logo_bytes(r2_key: str):
    """Fetch the raw bytes of a tenant's logo from R2.
    Returns (bytes, ctype) or (None, "") if not a logos/ key or fetch fails."""
    if not r2_key or not r2_key.startswith("logos/"):
        return None, ""
    data, ctype = download_object(r2_key)
    if data is None:
        return None, ""
    return data, (ctype or "image/png")


def upload_object(key: str, data: bytes, content_type: str) -> bool:
    """Upload an arbitrary object to R2 with an explicit content type."""
    if not is_configured():
        return False
    try:
        c = _client()
        c.put_object(Bucket=R2_BUCKET, Key=key, Body=data,
                     ContentType=content_type or "application/octet-stream")
        return True
    except Exception as e:
        print(f"[Archive] R2 upload_object failed: {e}")
        return False


def download_object(key: str):
    """Fetch an arbitrary object's bytes (and content type if available).
    Returns (bytes, content_type) or (None, None)."""
    if not is_configured() or not key:
        return None, None
    try:
        c = _client()
        obj = c.get_object(Bucket=R2_BUCKET, Key=key)
        return obj["Body"].read(), obj.get("ContentType") or "application/octet-stream"
    except Exception as e:
        print(f"[Archive] R2 download_object failed: {e}")
        return None, None


def upload_pdf(key: str, data: bytes) -> bool:
    """Upload PDF bytes to R2. Returns True on success."""
    if not is_configured():
        return False
    try:
        c = _client()
        c.put_object(
            Bucket=R2_BUCKET,
            Key=key,
            Body=data,
            ContentType="application/pdf",
        )
        return True
    except Exception as e:
        print(f"[Archive] R2 upload failed: {e}")
        return False


def download_pdf(key: str):
    """Fetch PDF bytes from R2. Returns bytes or None."""
    if not is_configured() or not key:
        return None
    try:
        c = _client()
        obj = c.get_object(Bucket=R2_BUCKET, Key=key)
        return obj["Body"].read()
    except Exception as e:
        print(f"[Archive] R2 download failed: {e}")
        return None


def delete_pdf(key: str) -> bool:
    """Delete one object. Returns True on success."""
    if not is_configured() or not key:
        return False
    try:
        c = _client()
        c.delete_object(Bucket=R2_BUCKET, Key=key)
        return True
    except Exception as e:
        print(f"[Archive] R2 delete failed: {e}")
        return False


def purge_expired(force: bool = False) -> dict:
    """
    Delete archive rows (and their R2 objects) older than RETENTION_DAYS.
    Rate-limited to once every 24 h unless force=True.

    Returns {'purged': N, 'failed': M, 'ran': bool}
    """
    global _last_purge

    if not force:
        if _last_purge and (datetime.utcnow() - _last_purge) < timedelta(hours=24):
            return {"purged": 0, "failed": 0, "ran": False}

    _last_purge = datetime.utcnow()

    # Late import to keep this module import-safe even before models loads
    from models import db, SentBillArchive

    cutoff = datetime.utcnow() - timedelta(days=RETENTION_DAYS)

    try:
        expired = (SentBillArchive.query
                   .filter(SentBillArchive.sent_at < cutoff)
                   .all())
    except Exception as e:
        print(f"[Archive] purge query failed: {e}")
        return {"purged": 0, "failed": 0, "ran": True}

    purged = 0
    failed = 0
    for row in expired:
        try:
            if row.storage_key:
                delete_pdf(row.storage_key)
            db.session.delete(row)
            purged += 1
        except Exception as e:
            print(f"[Archive] purge failed for id={row.id}: {e}")
            failed += 1

    try:
        db.session.commit()
    except Exception as e:
        print(f"[Archive] purge commit failed: {e}")
        db.session.rollback()
        failed += purged
        purged = 0

    return {"purged": purged, "failed": failed, "ran": True}
