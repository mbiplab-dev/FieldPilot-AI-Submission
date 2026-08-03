"""Notification service — the ONLY component allowed to notify.

Subscribes to rule-engine outcomes, deduplicates, and fans out to channels:
dashboard, SMS, email, WhatsApp, push. External channels are pluggable senders with
retry + backoff; the dashboard channel is always on (store + bus topic).
"""

from fieldpilot.notifications.service import NotificationService

__all__ = ["NotificationService"]
