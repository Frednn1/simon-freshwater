"""
Background scheduler for operational jobs.

Currently schedules one job:
  • d1_backup — nightly D1 SQL dump to R2 at D1_BACKUP_HOUR_EAT (default 03:00).

Consumer bill reminders are NOT scheduled here — they are sent on-demand
from the consumer page (see notifications.py).

Delivery design:
  • APScheduler is in-process and does NOT keep Render awake.
  • If Render sleeps through the scheduled hour, the job is skipped and the
    lazy fallback in d1_backup.maybe_run_lazy_backup() catches it on the
    next authenticated admin request after the scheduled hour.
  • The manual endpoint POST /api/admin/backup/d1/run is also available.

Env vars (all optional):
  D1_BACKUP_ENABLED   default true  — master switch for scheduled job
  D1_BACKUP_HOUR_EAT  default 3     — hour of day in Africa/Nairobi (0-23)
  D1_BACKUP_KEEP      default 7     — retention days
"""
import os
from datetime import timezone, timedelta


_NAIROBI_TZ = timezone(timedelta(hours=3))


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


def start_scheduler(app, Consumer=None, MeterReading=None):
    """Start the operational scheduler. Returns the scheduler, or None if disabled.

    Signature is kept identical to the previous no-op version so app.py's
    call site does not need to change.
    """
    if not _env_bool("D1_BACKUP_ENABLED", True):
        print("[Scheduler] D1 backups disabled via D1_BACKUP_ENABLED=false")
        return None

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from d1_backup import run_backup
    except Exception as e:
        print(f"[Scheduler] cannot import scheduler dependencies: {e}")
        return None

    hour = _env_int("D1_BACKUP_HOUR_EAT", 3)
    if hour < 0 or hour > 23:
        hour = 3

    scheduler = BackgroundScheduler(timezone=_NAIROBI_TZ)

    def _d1_job():
        try:
            with app.app_context():
                result = run_backup(reason="scheduled")
                print(f"[Scheduler] D1 backup result: {result}")
        except Exception as e:
            print(f"[Scheduler] D1 backup failed: {e}")

    scheduler.add_job(
        _d1_job,
        trigger="cron",
        hour=hour,
        minute=0,
        id="d1_backup",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    scheduler.start()
    print(f"[Scheduler] D1 backup scheduled daily at {hour:02d}:00 EAT "
          f"(retention {_env_int('D1_BACKUP_KEEP', 7)} days)")
    return scheduler
