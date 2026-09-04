"""
Time-based sweeps — the two things that must happen even when nobody
clicks anything: a seller who never ships gets the buyer refunded, and a
buyer who never responds during the inspection period has their order
auto-complete (with payout) instead of hanging forever.

THIS SANDBOX has no scheduler (see README.md) — these functions are
plain Python, callable directly from tests, and exposed for manual
triggering via POST /admin/sweep. In production these are the two jobs
that get wired to a real scheduler (cron / Celery beat / similar) in a
later phase; nothing about their logic depends on how they're invoked.
"""
from app.db import get_db, new_id, now_ts
from app.notifications import notify
from app.orders.pricing import payout_amount_cents
from app.orders.state_machine import transition_order_status, OrderStateError
from app.payments.stripe_client import get_client, PaymentError


def sweep_shipping_deadline_expirations(conn=None):
    """
    awaiting_shipment orders whose shipping_deadline_at has passed: the
    seller never shipped, so cancel and refund the buyer in full. No
    transfer has happened yet at this point in the state machine (a
    transfer only ever happens on order completion), so a plain refund
    is correct here — no reversal needed.
    """
    owns_conn = conn is None
    conn = conn or get_db()
    now = now_ts()
    rows = conn.execute(
        "SELECT * FROM orders WHERE status = 'awaiting_shipment' AND shipping_deadline_at IS NOT NULL "
        "AND shipping_deadline_at < ?",
        (now,),
    ).fetchall()

    affected = []
    client = get_client()
    for order in rows:
        try:
            transition_order_status(conn, order["id"], "cancelled", changed_by="system:sweep",
                                      reason="shipping_deadline_expired")
        except OrderStateError:
            continue
        try:
            client.create_refund(
                payment_intent_id=order["stripe_payment_intent_id"],
                amount_cents=order["total_price_cents"],
                idempotency_key=f"refund-noship-{order['id']}",
            )
        except PaymentError:
            continue
        conn.execute(
            """INSERT INTO refunds
               (id, order_id, stripe_refund_id, amount_cents, reason, status, initiated_by,
                idempotency_key, created_at)
               VALUES (?, ?, ?, ?, 'seller_did_not_ship', 'succeeded', 'system:sweep', ?, ?)""",
            (new_id(), order["id"], f"refund-noship-{order['id']}", order["total_price_cents"],
             f"refund-noship-{order['id']}", now),
        )
        notify(conn, order["buyer_id"], "order_cancelled_refunded", order_id=order["id"],
               reason="seller_did_not_ship")
        notify(conn, order["seller_id"], "order_cancelled_no_shipment", order_id=order["id"])
        affected.append(order["id"])

    if owns_conn:
        conn.commit()
        conn.close()
    return affected


def sweep_inspection_period_expirations(conn=None):
    """
    inspection_period orders whose inspection_deadline_at has passed with
    no active dispute: auto-complete and release payout. "No active
    dispute" is structurally guaranteed here — reporting an issue moves
    an order OUT of inspection_period (into issue_reported/under_review)
    immediately, so any order still found in inspection_period by
    definition never had one, or its dispute path already diverted it.
    """
    owns_conn = conn is None
    conn = conn or get_db()
    now = now_ts()
    rows = conn.execute(
        "SELECT * FROM orders WHERE status = 'inspection_period' AND inspection_deadline_at IS NOT NULL "
        "AND inspection_deadline_at < ?",
        (now,),
    ).fetchall()

    affected = []
    for order in rows:
        try:
            transition_order_status(conn, order["id"], "completed", changed_by="system:sweep",
                                      reason="inspection_period_expired")
        except OrderStateError:
            continue
        connect = conn.execute(
            "SELECT * FROM stripe_connect_accounts WHERE user_id = ?", (order["seller_id"],)
        ).fetchone()
        if connect and connect["payouts_enabled"]:
            client = get_client()
            try:
                transfer = client.create_transfer(
                    amount_cents=payout_amount_cents(order),
                    destination_account_id=connect["stripe_account_id"],
                    source_transaction_id=order["stripe_payment_intent_id"],
                    transfer_group=order["transfer_group"],
                    idempotency_key=f"payout-{order['id']}",
                )
                conn.execute("UPDATE orders SET stripe_transfer_id = ? WHERE id = ?",
                              (transfer["id"], order["id"]))
                conn.execute(
                    """INSERT INTO payouts (id, seller_id, stripe_payout_id, amount_cents, status, initiated_at)
                       VALUES (?, ?, ?, ?, 'released_to_connect_balance', ?)""",
                    (new_id(), order["seller_id"], transfer["id"], payout_amount_cents(order), now),
                )
            except PaymentError:
                pass
        notify(conn, order["seller_id"], "order_auto_completed", order_id=order["id"])
        notify(conn, order["buyer_id"], "order_auto_completed", order_id=order["id"])
        affected.append(order["id"])

    if owns_conn:
        conn.commit()
        conn.close()
    return affected


def run_all_sweeps():
    conn = get_db()
    try:
        shipping = sweep_shipping_deadline_expirations(conn)
        inspection = sweep_inspection_period_expirations(conn)
        conn.commit()
    finally:
        conn.close()
    return {"cancelled_no_shipment": shipping, "auto_completed": inspection}
