"""
Nightly D1 backup — self-contained export + R2 upload + retention prune.

Delivery mechanisms (any one succeeds → backup exists for the day):
  • APScheduler at D1_BACKUP_HOUR_EAT (03:00 Nairobi default)
  • Lazy fallback: first authenticated admin request after the scheduled
    hour, if no backup exists yet, spawn a daemon thread to run it
  • Manual: POST /api/admin/backup/d1/run (super-admin only)

Design constraints:
  • Standalone module — no dependency on project-root d1_export.py
  • Never blocks an HTTP request (all heavy work is daemon-threaded)
  • Never raises: returns {ok, key, sha256, bytes, pruned, error?}
  • One backup runs at a time (in-process lock)
  • Retention: keep the newest N date folders under backups/d1/ (default 7)

Env vars (all optional):
  D1_BACKUP_ENABLED  default true
  D1_BACKUP_LAZY     default true
  D1_BACKUP_HOUR_EAT default 3
  D1_BACKUP_KEEP     default 7
"""
import os
import json
import time
import hashlib
import threading
from datetime import datetime, timedelta, timezone

import httpx

import archive


_NAIROBI_TZ = timezone(timedelta(hours=3))

_backup_lock = threading.Lock()
_lazy_lock   = threading.Lock()
_last_lazy_check_date = None


def _now_eat():
    return datetime.now(_NAIROBI_TZ)


def _env_bool(name, default):
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def _cf_creds():
    return (
        os.environ.get("CF_ACCOUNT_ID", "").strip(),
        os.environ.get("CF_D1_DATABASE_ID", "").strip(),
        os.environ.get("CF_API_TOKEN", "").strip(),
    )


def export_d1_sql(timeout=120, max_poll=30):
    """Call the Cloudflare D1 export API. Returns (ok, bytes_or_error_str)."""
    acc, db, tok = _cf_creds()
    if not (acc and db and tok):
        return False, "Missing CF_ACCOUNT_ID / CF_D1_DATABASE_ID / CF_API_TOKEN"

    url = (f"https://api.cloudflare.com/client/v4/accounts/{acc}"
           f"/d1/database/{db}/export")
    headers = {"Authorization": f"Bearer {tok}",
               "Content-Type": "application/json"}

    def _post(payload):
        try:
            r = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        except Exception as e:
            return False, f"HTTP exception: {e}"
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:300]}"
        try:
            d = r.json()
        except Exception:
            return False, "Response was not JSON"
        if not d.get("success"):
            return False, f"API error: {json.dumps(d.get('errors'))[:300]}"
        return True, d["result"]

    ok, res = _post({"output_format": "polling"})
    if not ok:
        return False, res

    bookmark = res.get("at_bookmark")
    if not bookmark:
        return False, "No at_bookmark in export response"

    signed_url = None
    for _ in range(max_poll):
        time.sleep(1)
        ok, res = _post({"output_format": "polling",
                         "current_bookmark": bookmark})
        if not ok:
            return False, res
        inner = res.get("result") or {}
        signed_url = res.get("signed_url") or inner.get("signed_url")
        if signed_url:
            break

    if not signed_url:
        return False, "Export did not produce a signed URL in time"

    try:
        with httpx.stream("GET", signed_url, timeout=timeout,
                          follow_redirects=True) as r:
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_bytes():
                if chunk:
                    buf.extend(chunk)
        return True, bytes(buf)
    except Exception as e:
        return False, f"Download failed: {e}"


def _sidecar_key(sql_key):
    return sql_key + ".sha256"


def _prune_old(keep=7):
    """Keep the newest `keep` date folders under backups/d1/. Returns count deleted."""
    if keep < 1:
        return 0
    if not archive.is_configured():
        return 0
    try:
        client = archive._client()
        bucket = archive.R2_BUCKET
        dates = set()
        token = None
        while True:
            kw = {"Bucket": bucket, "Prefix": "backups/d1/", "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            r = client.list_objects_v2(**kw)
            for obj in r.get("Contents", []):
                key = obj["Key"]
                parts = key.split("/")
                if len(parts) >= 4 and len(parts[2]) == 10:
                    dates.add(parts[2])
            token = r.get("NextContinuationToken")
            if not token:
                break

        if len(dates) <= keep:
            return 0
        to_delete = sorted(dates)[:-keep]
        deleted = 0
        for d in to_delete:
            prefix = f"backups/d1/{d}/"
            token = None
            while True:
                kw = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
                if token:
                    kw["ContinuationToken"] = token
                r = client.list_objects_v2(**kw)
                for obj in r.get("Contents", []):
                    try:
                        client.delete_object(Bucket=bucket, Key=obj["Key"])
                        deleted += 1
                    except Exception:
                        pass
                token = r.get("NextContinuationToken")
                if not token:
                    break
        return deleted
    except Exception as e:
        print(f"[d1_backup] prune failed: {e}")
        return 0


def run_backup(reason="scheduled"):
    """Full backup cycle. Returns {ok, key, sha256, bytes, pruned, reason, at, error?}."""
    if not archive.is_configured():
        return {"ok": False, "error": "R2 not configured",
                "reason": reason, "at": _now_eat().isoformat()}

    if not _backup_lock.acquire(blocking=False):
        return {"ok": False, "error": "Another backup is already running",
                "reason": reason, "at": _now_eat().isoformat()}

    try:
        ok, res = export_d1_sql()
        if not ok:
            print(f"[d1_backup] export failed: {res}")
            return {"ok": False, "error": res,
                    "reason": reason, "at": _now_eat().isoformat()}

        sql_bytes = res
        if not sql_bytes:
            return {"ok": False, "error": "Export produced empty bytes",
                    "reason": reason, "at": _now_eat().isoformat()}

        sha = hashlib.sha256(sql_bytes).hexdigest()
        now = _now_eat()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H%M%S")
        sql_key  = f"backups/d1/{date_str}/{time_str}.sql"
        sha_key  = _sidecar_key(sql_key)

        if not archive.upload_object(sql_key, sql_bytes, "application/sql"):
            return {"ok": False, "error": "R2 upload of .sql failed",
                    "reason": reason, "at": now.isoformat()}

        sidecar = f"{sha}  {os.path.basename(sql_key)}\n".encode()
        if not archive.upload_object(sha_key, sidecar, "text/plain"):
            return {"ok": False, "error": "R2 upload of .sha256 failed",
                    "key": sql_key, "reason": reason, "at": now.isoformat()}

        keep   = _env_int("D1_BACKUP_KEEP", 7)
        pruned = _prune_old(keep=keep)

        return {"ok": True, "key": sql_key, "sha256": sha,
                "bytes": len(sql_bytes), "pruned": pruned,
                "reason": reason, "at": now.isoformat()}
    finally:
        _backup_lock.release()


def backup_exists_today():
    """Return True if backups/d1/YYYY-MM-DD/ has at least one .sql object."""
    if not archive.is_configured():
        return False
    today  = _now_eat().strftime("%Y-%m-%d")
    prefix = f"backups/d1/{today}/"
    try:
        client = archive._client()
        r = client.list_objects_v2(Bucket=archive.R2_BUCKET,
                                   Prefix=prefix, MaxKeys=10)
        for obj in r.get("Contents", []):
            if obj["Key"].endswith(".sql"):
                return True
        return False
    except Exception:
        return False


def maybe_run_lazy_backup():
    """Called from _current_admin on successful auth. Fires at most once per
    process per day, only after the scheduled hour, only if today's backup
    doesn't already exist. Runs the whole backup in a daemon thread so the
    triggering HTTP request is never slowed.
    """
    global _last_lazy_check_date

    if not _env_bool("D1_BACKUP_ENABLED", True):
        return
    if not _env_bool("D1_BACKUP_LAZY", True):
        return

    now   = _now_eat()
    today = now.strftime("%Y-%m-%d")

    # Cheap in-memory guard, protected so concurrent requests don't both spawn
    with _lazy_lock:
        if _last_lazy_check_date == today:
            return
        _last_lazy_check_date = today

    target_hour = _env_int("D1_BACKUP_HOUR_EAT", 3)
    if now.hour < target_hour:
        return

    def _worker():
        try:
            if backup_exists_today():
                return
            result = run_backup(reason="lazy")
            print(f"[d1_backup] lazy result: {result}")
        except Exception as e:
            print(f"[d1_backup] lazy failed: {e}")

    threading.Thread(target=_worker, daemon=True,
                     name="d1-lazy-backup").start()
