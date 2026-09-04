"""
Stripe webhook receiver.

Security/correctness properties this endpoint must have, and how:
  - Signature verification: every request must carry a valid
    Stripe-Signature header, checked via stripe_client.construct_webhook_event()
    (real HMAC verification in LiveStripeClient, a simple shared-secret
    check in FakeStripeClient) — an unsigned or forged POST to this URL
    can never move money or change an order's status.
  - Duplicate delivery is normal for Stripe (at-least-once delivery) and
    must be a no-op the second time: webhook_events.stripe_event_id is
    UNIQUE, and we check-then-skip BEFORE doing anything else — this is
    exactly the "dubbele webhook" test scenario.
  - This endpoint deliberately does not require a session/login — Stripe
    calls it directly. It authenticates the CALLER via the signature,
    not via a user session.
"""
import json

from flask import Blueprint, jsonify, request

from app.db import get_db, new_id, now_ts
from app.notifications import notify
from app.orders.state_machine import transition_order_status, OrderStateError
from app.orders.pricing import get_config_hours
from app.payments.stripe_client import get_client, PaymentError

webhooks_bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


@webhooks_bp.route("/stripe", methods=["POST"])
def stripe_webhook():
    payload_bytes = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    client = get_client()
    try:
        event = client.construct_webhook_event(payload_bytes, sig_header)
    except PaymentError as exc:
        return jsonify(error=exc.code, message=exc.message), 400

    event_id = event["id"]
    event_type = event["type"]
    data_object = event["data"]["object"]

    conn = get_db()
    existing = conn.execute(
        "SELECT id, processed_at FROM webhook_events WHERE stripe_event_id = ?", (event_id,)
    ).fetchone()
    if existing:
        # Already seen (and, if processed_at is set, already handled) —
        # acknowledge with 200 so Stripe stops retrying, but do nothing
        # else. This is the entire fix for duplicate webhook delivery.
        conn.close()
        return jsonify(received=True, duplicate=True)

    ts = now_ts()
    conn.execute(
        """INSERT INTO webhook_events (id, stripe_event_id, event_type, payload, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (new_id(), event_id, event_type, json.dumps(event), ts),
    )

    try:
        if event_type == "payment_intent.succeeded":
            _handle_payment_succeeded(conn, data_object)
        elif event_type == "payment_intent.payment_failed":
            _handle_payment_failed(conn, data_object)
        elif event_type == "charge.dispute.created":
            _handle_chargeback(conn, data_object)
        # Unknown event types are acknowledged and ignored — Stripe sends
        # many event types this platform doesn't act on yet.
    except OrderStateError:
        # The order was already past the status this event expects (e.g.
        # a payment_intent.succeeded arriving after the order was
        # separately cancelled) — log and acknowledge rather than
        # erroring, so Stripe doesn't retry forever on something that
        # will never become valid.
        pass

    conn.execute("UPDATE webhook_events SET processed_at = ? WHERE stripe_event_id = ?", (now_ts(), event_id))
    conn.commit()
    conn.close()
    return jsonify(received=True)


def _handle_payment_succeeded(conn, payment_intent):
    order = conn.execute(
        "SELECT * FROM orders WHERE stripe_payment_intent_id = ?", (payment_intent["id"],)
    ).fetchone()
    if not order or order["status"] != "payment_pending":
        return
    transition_order_status(conn, order["id"], "paid", changed_by="webhook:stripe")
    deadline_hours = get_config_hours(conn, "shipping_deadline_hours", 120)
    conn.execute(
        "UPDATE orders SET shipping_deadline_at = ? WHERE id = ?",
        (now_ts() + deadline_hours * 3600, order["id"]),
    )
    transition_order_status(conn, order["id"], "awaiting_shipment", changed_by="webhook:stripe")
    notify(conn, order["seller_id"], "payment_received", order_id=order["id"])


def _handle_payment_failed(conn, payment_intent):
    order = conn.execute(
        "SELECT * FROM orders WHERE stripe_payment_intent_id = ?", (payment_intent["id"],)
    ).fetchone()
    if not order or order["status"] != "payment_pending":
        return
    transition_order_status(conn, order["id"], "cancelled", changed_by="webhook:stripe",
                              reason="payment_failed")
    notify(conn, order["buyer_id"], "payment_failed", order_id=order["id"])


def _handle_chargeback(conn, charge_dispute):
    payment_intent_id = charge_dispute.get("payment_intent")
    order = conn.execute(
        "SELECT * FROM orders WHERE stripe_payment_intent_id = ?", (payment_intent_id,)
    ).fetchone()
    if not order:
        return

    existing = conn.execute(
        "SELECT id FROM chargebacks WHERE stripe_dispute_id = ?", (charge_dispute["id"],)
    ).fetchone()
    if existing:
        return

    transfer_reversed = False
    client = get_client()
    if order["stripe_transfer_id"]:
        # Best-effort: attempt to reverse the transfer already made to the
        # seller. In the real world this can fail (seller's Connect
        # balance already paid out to their bank) — that failure is
        # recorded as a risk flag for manual admin follow-up, it must
        # never raise and block recording the chargeback itself.
        try:
            client.create_transfer_reversal(
                transfer_id=order["stripe_transfer_id"],
                amount_cents=charge_dispute.get("amount", order["total_price_cents"]),
                idempotency_key=f"chargeback-reversal-{charge_dispute['id']}",
            )
            transfer_reversed = True
        except PaymentError:
            conn.execute(
                """INSERT INTO risk_flags
                   (id, user_id, order_id, flag_type, severity, details, created_at)
                   VALUES (?, ?, ?, 'chargeback_reversal_failed', 'high', ?, ?)""",
                (new_id(), order["seller_id"], order["id"],
                 json.dumps({"stripe_dispute_id": charge_dispute["id"]}), now_ts()),
            )

    conn.execute(
        """INSERT INTO chargebacks
           (id, order_id, stripe_dispute_id, amount_cents, reason, status, transfer_reversed, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (new_id(), order["id"], charge_dispute["id"],
         charge_dispute.get("amount", order["total_price_cents"]),
         charge_dispute.get("reason", "unknown"), "needs_response",
         1 if transfer_reversed else 0, now_ts()),
    )
    conn.execute(
        """INSERT INTO risk_flags (id, user_id, order_id, flag_type, severity, details, created_at)
           VALUES (?, ?, ?, 'chargeback', 'high', ?, ?)""",
        (new_id(), order["buyer_id"], order["id"],
         json.dumps({"stripe_dispute_id": charge_dispute["id"]}), now_ts()),
    )

    # A chargeback only pulls an order still mid-flow into under_review.
    # An already-completed order's status is deliberately left alone —
    # see docs/ARCHITECTURE.md and the module docstring in
    # app/payments/stripe_client.py for why (chargebacks can arrive up to
    # ~120 days after the charge, long after a normal order closes).
    if order["status"] in ("paid", "awaiting_shipment", "shipped", "inspection_period"):
        transition_order_status(conn, order["id"], "under_review", changed_by="webhook:stripe",
                                  reason="chargeback")

    notify(conn, order["seller_id"], "chargeback_received", order_id=order["id"])
