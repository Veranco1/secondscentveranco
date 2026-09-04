"""
Order status state machine — application-level enforcement.

db/schema.sql enforces this same whitelist as a Postgres trigger
(enforce_order_status_transition(), see docs/ARCHITECTURE.md § C). SQLite
can't easily express that same cross-row "is (from,to) in this other
table" check inside a BEFORE UPDATE trigger without recursion pitfalls
(see the comment on is_valid_transition() in app/db.py), so in THIS dev
layer the invariant is enforced here instead — in the one function every
order status change in the whole codebase must go through. Never write
`UPDATE orders SET status = ...` anywhere else.

Every transition is recorded in order_status_history (who, when, from,
to, why) — this is the audit trail the admin environment reads.
"""
import time

from app.db import is_valid_transition, new_id, now_ts


class OrderStateError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# Columns that get a timestamp stamped automatically when an order
# transitions TO that status — keeps every call site from having to
# remember which timestamp column matches which status.
_STATUS_TIMESTAMP_COLUMN = {
    "paid": "paid_at",
    "shipped": "shipped_at",
    "delivered": "delivered_at",
    "completed": "completed_at",
    "cancelled": "cancelled_at",
    "refunded": "cancelled_at",  # terminal, reuse cancelled_at as "closed_at"
}


def transition_order_status(conn, order_id, to_status, changed_by, reason=None):
    """
    Validates and applies a single order status transition inside the
    caller's transaction (caller commits). Returns the updated order row.

    changed_by: a user id, or one of the literal strings "system" /
    "admin:<user_id>" / "webhook:stripe" — order_status_history.changed_by
    is TEXT specifically so the audit trail can distinguish a human buyer/
    seller action from an automated sweep or an incoming webhook.
    """
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise OrderStateError("not_found", "Order bestaat niet.")

    from_status = order["status"]
    if from_status == to_status:
        raise OrderStateError("invalid_transition", f"Order is al '{to_status}'.")
    if not is_valid_transition(conn, from_status, to_status):
        raise OrderStateError(
            "invalid_transition",
            f"Overgang van '{from_status}' naar '{to_status}' is niet toegestaan.",
        )

    ts = now_ts()
    ts_column = _STATUS_TIMESTAMP_COLUMN.get(to_status)
    if ts_column:
        conn.execute(
            f"UPDATE orders SET status = ?, updated_at = ?, {ts_column} = ? WHERE id = ?",
            (to_status, ts, ts, order_id),
        )
    else:
        conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (to_status, ts, order_id),
        )

    conn.execute(
        """INSERT INTO order_status_history
           (id, order_id, from_status, to_status, changed_by, reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (new_id(), order_id, from_status, to_status, changed_by, reason, ts),
    )

    return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
