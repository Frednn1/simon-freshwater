"""
Auto-send reminders have been removed.

Bill reminders are now sent ON-DEMAND from the consumer page:
  • SMS       → SMSGate
  • WhatsApp  → Meta Cloud API

See notifications.py for the actual senders.
"""


def start_scheduler(app, Consumer, MeterReading):
    """No-op. Kept so existing app.py imports continue to work."""
    print("[Scheduler] Disabled — reminders are sent on-demand from the consumer page.")
    return None
