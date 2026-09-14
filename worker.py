import json
import logging
import os
from datetime import datetime, timedelta, timezone

from pywebpush import WebPushException, webpush

from app import get_db_connection, init_db, utc_now_iso

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("routine-worker")


def deliver_due_reminders():
    vapid_private = os.environ.get("VAPID_PRIVATE_KEY")
    vapid_subject = os.environ.get("VAPID_SUBJECT")
    if not vapid_private or not vapid_subject:
        logger.error("VAPID_PRIVATE_KEY and VAPID_SUBJECT are required")
        return 0
    delivered = 0
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with get_db_connection() as conn:
        reminders = [dict(row) for row in conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending' AND delivered_at IS NULL AND COALESCE(snoozed_until, due_at) <= ? AND delivery_attempts < 5 ORDER BY due_at LIMIT 100",
            (now,),
        ).fetchall()]
        subscriptions_by_user = {
            reminder["user_id"]: [dict(row) for row in conn.execute(
                "SELECT * FROM push_subscriptions WHERE user_id = ?", (reminder["user_id"],)
            ).fetchall()]
            for reminder in reminders
        }
    updates = []
    expired_subscription_ids = []
    for reminder in reminders:
        subscriptions = subscriptions_by_user[reminder["user_id"]]
        sent = False
        for subscription in subscriptions:
            try:
                webpush(
                    subscription_info=json.loads(subscription["subscription_json"]),
                    data=json.dumps({"title": reminder["title"], "body": "Routine Tracker reminder", "url": "/"}),
                    vapid_private_key=vapid_private,
                    vapid_claims={"sub": vapid_subject},
                )
                sent = True
            except WebPushException as error:
                status = getattr(error.response, "status_code", None)
                logger.warning("Push delivery failed for subscription %s: %s", subscription["id"], error)
                if status in {404, 410}:
                    expired_subscription_ids.append(subscription["id"])
        if sent and reminder["recurrence"] in {"daily", "weekly"}:
            interval = timedelta(days=7 if reminder["recurrence"] == "weekly" else 1)
            next_due = datetime.fromisoformat(reminder["due_at"]) + interval
            updates.append(("recurring", next_due.replace(microsecond=0).isoformat(), reminder["id"]))
        else:
            updates.append(("delivered" if sent else "failed", utc_now_iso() if sent else None, reminder["id"]))
        if sent:
            delivered += 1
    with get_db_connection() as conn:
        for subscription_id in expired_subscription_ids:
            conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (subscription_id,))
        for update_type, value, reminder_id in updates:
            if update_type == "recurring":
                conn.execute("UPDATE reminders SET due_at = ?, snoozed_until = NULL, delivery_attempts = 0, delivered_at = NULL WHERE id = ?", (value, reminder_id))
            else:
                conn.execute("UPDATE reminders SET delivery_attempts = delivery_attempts + 1, delivered_at = ? WHERE id = ?", (value, reminder_id))
    return delivered


if __name__ == "__main__":
    init_db()
    logger.info("Delivered %s reminders", deliver_due_reminders())
