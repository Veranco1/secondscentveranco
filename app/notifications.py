"""
Notification creation — a thin, shared helper so every place in the
codebase that should notify a user does it the same way (one row in
`notifications`, JSON payload, unread until the (future) notifications
UI marks it read). No email/push delivery is wired up yet — that is a
later phase; this module is the hook point for it (see NOTE below).

Events covered here map to the user's spec list of 11 notification
triggers where they occur at the buyer-protection layer: payment
received (seller), shipped (buyer), delivered / inspection period
started (buyer), buyer confirmed / order completed (seller), issue
reported (seller + admin-visible via the dispute itself), dispute
resolved (buyer + seller), refund issued (buyer), payout sent (seller),
order auto-cancelled — no shipment (buyer + seller), inspection period
auto-completed (seller).
"""
import json

from app.db import new_id, now_ts


def notify(conn, user_id, notif_type, **payload):
    """
    NOTE: this only writes the row; it does not commit. Call sites
    already hold an open write transaction for the order/dispute change
    that triggered the notification, so committing once at the end
    (after both the state change and the notification insert) keeps
    them atomic — a notification is never created for a state change
    that itself failed to commit, and vice versa.
    """
    conn.execute(
        """INSERT INTO notifications (id, user_id, type, payload, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (new_id(), user_id, notif_type, json.dumps(payload), now_ts()),
    )
