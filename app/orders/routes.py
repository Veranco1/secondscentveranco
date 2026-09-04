"""
Orders blueprint — checkout, shipping, delivery, buyer confirmation, and
issue reporting. This is the core of the buyer-protection request.

Every route that changes money or order status goes through:
  - pricing.compute_order_totals() for anything involving cents (never a
    client-submitted price — see checkout() below for exactly where a
    submitted price would be ignored),
  - state_machine.transition_order_status() for anything involving
    orders.status (never a raw UPDATE),
  - app.payments.stripe_client.get_client() for anything involving
    Stripe, always with an idempotency key.

Endpoints:
    POST /orders/checkout              buyer  -> create order + PaymentIntent
    GET  /orders                       buyer/seller -> list my orders
    GET  /orders/<id>                  buyer/seller/admin -> order detail
    POST /orders/<id>/ship             seller -> awaiting_shipment -> shipped
    POST /orders/<id>/mark-delivered   admin  -> shipped -> delivered -> inspection_period
                                        (stand-in for a real carrier-tracking
                                        webhook, which is a later phase —
                                        deliberately NOT buyer/seller-callable,
                                        so neither side can fake a delivery)
    POST /orders/<id>/confirm          buyer  -> inspection_period -> completed (+ payout)
    POST /orders/<id>/report-issue     buyer  -> shipped|inspection_period -> issue_reported -> under_review
"""
import json
import os

from flask import Blueprint, jsonify, request, session

from app.auth.routes import login_required, admin_required, csrf_protected
from app.db import get_db, new_id, now_ts
from app.notifications import notify
from app.orders.pricing import compute_order_totals, payout_amount_cents, get_config_hours, PricingError
from app.orders.state_machine import transition_order_status, OrderStateError
from app.payments.stripe_client import get_client, PaymentError

orders_bp = Blueprint("orders", __name__, url_prefix="/orders")

DISPUTE_REASONS = {
    "not_received",
    "damaged",
    "wrong_item",
    "significantly_not_as_described",
    "counterfeit_suspected",
    "missing_parts_or_packaging",
}


# ---------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------
def _public_order(row):
    return {
        "id": row["id"],
        "buyer_id": row["buyer_id"],
        "seller_id": row["seller_id"],
        "listing_id": row["listing_id"],
        "status": row["status"],
        "item_price_cents": row["item_price_cents"],
        "shipping_price_cents": row["shipping_price_cents"],
        "buyer_protection_fee_cents": row["buyer_protection_fee_cents"],
        "tax_cents": row["tax_cents"],
        "total_price_cents": row["total_price_cents"],
        "currency": row["currency"],
        "shipping_deadline_at": row["shipping_deadline_at"],
        "inspection_deadline_at": row["inspection_deadline_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "paid_at": row["paid_at"],
        "shipped_at": row["shipped_at"],
        "delivered_at": row["delivered_at"],
        "completed_at": row["completed_at"],
        "cancelled_at": row["cancelled_at"],
    }


def _order_or_404(conn, order_id):
    return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def _is_party_or_admin(conn, order, user_id):
    if order["buyer_id"] == user_id or order["seller_id"] == user_id:
        return True
    row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["is_admin"])


# ---------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------
@orders_bp.route("/checkout", methods=["POST"])
@csrf_protected
@login_required
def checkout():
    data = request.get_json(silent=True) or {}
    listing_id = data.get("listing_id")
    if not listing_id:
        return jsonify(error="missing_listing_id"), 400

    buyer_id = session["user_id"]
    conn = get_db()
    listing = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not listing:
        conn.close()
        return jsonify(error="listing_not_found"), 404
    if listing["status"] != "active":
        conn.close()
        return jsonify(error="listing_not_available"), 409
    if listing["seller_id"] == buyer_id:
        conn.close()
        return jsonify(error="cannot_buy_own_listing"), 400

    # NOTE, explicitly: any "price", "total", "fee" or similar field the
    # client sent in `data` is never read here. The full price is always
    # recomputed from the listing + fee_rules + platform_config below —
    # this is the direct fix for the "gemanipuleerde frontend-prijs"
    # scenario in the test suite.
    try:
        totals = compute_order_totals(conn, listing)
    except PricingError as exc:
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 500

    order_id = new_id()
    ts = now_ts()
    conn.execute(
        """INSERT INTO orders
           (id, buyer_id, seller_id, listing_id, status,
            item_price_cents, shipping_price_cents, buyer_protection_fee_cents,
            tax_cents, total_price_cents, currency, transfer_group,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, 'payment_pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            order_id, buyer_id, listing["seller_id"], listing_id,
            totals["item_price_cents"], totals["shipping_price_cents"],
            totals["buyer_protection_fee_cents"], totals["tax_cents"],
            totals["total_price_cents"], totals["currency"], order_id,
            ts, ts,
        ),
    )

    client = get_client()
    try:
        pi = client.create_payment_intent(
            amount_cents=totals["total_price_cents"],
            currency=totals["currency"].lower(),
            transfer_group=order_id,
            idempotency_key=f"checkout-pi-{order_id}",
        )
    except PaymentError as exc:
        conn.rollback()
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 502

    conn.execute(
        "UPDATE orders SET stripe_payment_intent_id = ? WHERE id = ?",
        (pi["id"], order_id),
    )
    conn.commit()
    row = _order_or_404(conn, order_id)
    conn.close()

    return jsonify(order=_public_order(row), client_secret=pi["client_secret"]), 201


# ---------------------------------------------------------------------
# Dev-only: simulate the Stripe payment succeeding.
#
# In production, payment_pending -> paid happens exclusively via the
# signed Stripe webhook (see app/payments/webhooks.py::_handle_payment_succeeded).
# There is no real Stripe account wired into this sandbox (see
# app/payments/stripe_client.py), so there is nothing that would ever
# call that webhook for a checkout started from the browser demo. This
# endpoint exists ONLY so the demo website's checkout flow is genuinely
# clickable end-to-end: it marks the buyer's own FakeStripeClient
# PaymentIntent as succeeded and then runs the exact same handler the
# real webhook would run — it does not bypass or duplicate that logic,
# and it refuses outright whenever PAYMENTS_MODE=live.
# ---------------------------------------------------------------------
@orders_bp.route("/<order_id>/dev-pay", methods=["POST"])
@csrf_protected
@login_required
def dev_simulate_payment(order_id):
    if os.environ.get("PAYMENTS_MODE", "fake") == "live":
        return jsonify(error="not_available",
                        message="Alleen beschikbaar met PAYMENTS_MODE=fake (geen echte Stripe-koppeling)."), 403

    conn = get_db()
    order = _order_or_404(conn, order_id)
    if not order:
        conn.close()
        return jsonify(error="not_found"), 404
    if order["buyer_id"] != session["user_id"]:
        conn.close()
        return jsonify(error="forbidden"), 403
    if order["status"] != "payment_pending":
        conn.close()
        return jsonify(error="invalid_state",
                        message="Deze order wacht niet (meer) op betaling."), 409

    from app.payments.webhooks import _handle_payment_succeeded

    client = get_client()
    pi_id = order["stripe_payment_intent_id"]
    client.mark_payment_succeeded(pi_id)
    try:
        _handle_payment_succeeded(conn, {"id": pi_id})
    except OrderStateError as exc:
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 409
    conn.commit()
    row = _order_or_404(conn, order_id)
    conn.close()
    return jsonify(order=_public_order(row))


# ---------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------
@orders_bp.route("", methods=["GET"])
@login_required
def list_orders():
    user_id = session["user_id"]
    role = request.args.get("role")  # "buyer" | "seller" | None (both)
    conn = get_db()
    if role == "buyer":
        rows = conn.execute("SELECT * FROM orders WHERE buyer_id = ? ORDER BY created_at DESC", (user_id,)).fetchall()
    elif role == "seller":
        rows = conn.execute("SELECT * FROM orders WHERE seller_id = ? ORDER BY created_at DESC", (user_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM orders WHERE buyer_id = ? OR seller_id = ? ORDER BY created_at DESC",
            (user_id, user_id),
        ).fetchall()
    conn.close()
    return jsonify(orders=[_public_order(r) for r in rows])


@orders_bp.route("/<order_id>", methods=["GET"])
@login_required
def get_order(order_id):
    conn = get_db()
    order = _order_or_404(conn, order_id)
    if not order or not _is_party_or_admin(conn, order, session["user_id"]):
        conn.close()
        return jsonify(error="not_found"), 404
    history = conn.execute(
        "SELECT from_status, to_status, changed_by, reason, created_at "
        "FROM order_status_history WHERE order_id = ? ORDER BY created_at ASC",
        (order_id,),
    ).fetchall()
    conn.close()
    return jsonify(
        order=_public_order(order),
        history=[dict(h) for h in history],
    )


# ---------------------------------------------------------------------
# Seller ships
# ---------------------------------------------------------------------
@orders_bp.route("/<order_id>/ship", methods=["POST"])
@csrf_protected
@login_required
def ship_order(order_id):
    data = request.get_json(silent=True) or {}
    carrier = (data.get("carrier") or "").strip()
    tracking_number = (data.get("tracking_number") or "").strip()
    if not carrier or not tracking_number:
        return jsonify(error="missing_shipment_details"), 400

    conn = get_db()
    order = _order_or_404(conn, order_id)
    if not order:
        conn.close()
        return jsonify(error="not_found"), 404
    if order["seller_id"] != session["user_id"]:
        conn.close()
        return jsonify(error="forbidden"), 403

    try:
        updated = transition_order_status(conn, order_id, "shipped", changed_by=session["user_id"])
    except OrderStateError as exc:
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 409

    conn.execute(
        """INSERT INTO shipments (id, order_id, carrier, tracking_number, shipped_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (new_id(), order_id, carrier, tracking_number, now_ts(), now_ts()),
    )
    notify(conn, order["buyer_id"], "order_shipped", order_id=order_id, carrier=carrier, tracking_number=tracking_number)
    conn.commit()
    row = _order_or_404(conn, order_id)
    conn.close()
    return jsonify(order=_public_order(row))


# ---------------------------------------------------------------------
# Carrier-confirmed delivery (admin-triggered stand-in — see docstring)
# ---------------------------------------------------------------------
@orders_bp.route("/<order_id>/mark-delivered", methods=["POST"])
@csrf_protected
@login_required
@admin_required
def mark_delivered(order_id):
    conn = get_db()
    order = _order_or_404(conn, order_id)
    if not order:
        conn.close()
        return jsonify(error="not_found"), 404

    try:
        transition_order_status(conn, order_id, "delivered", changed_by=f"admin:{session['user_id']}",
                                  reason="carrier_confirmed_delivery")
        inspection_hours = get_config_hours(conn, "inspection_period_hours", 72)
        ts = now_ts()
        inspection_deadline_at = ts + inspection_hours * 3600
        conn.execute(
            "UPDATE orders SET inspection_deadline_at = ? WHERE id = ?",
            (inspection_deadline_at, order_id),
        )
        updated = transition_order_status(conn, order_id, "inspection_period", changed_by=f"admin:{session['user_id']}")
    except OrderStateError as exc:
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 409

    conn.execute(
        "UPDATE shipments SET delivered_at = ? WHERE order_id = ?", (now_ts(), order_id)
    )
    notify(conn, order["buyer_id"], "order_delivered", order_id=order_id,
           inspection_deadline_at=inspection_deadline_at)
    conn.commit()
    row = _order_or_404(conn, order_id)
    conn.close()
    return jsonify(order=_public_order(row))


# ---------------------------------------------------------------------
# Buyer confirms — releases payout
# ---------------------------------------------------------------------
def _release_payout(conn, order):
    """
    Shared by buyer-confirm, the inspection-period sweep, and a
    dispute resolution of "release_to_seller" — every path that ends
    with the seller actually getting paid goes through this one
    function so the idempotency key and payout bookkeeping can't drift
    between call sites.
    """
    connect = conn.execute(
        "SELECT * FROM stripe_connect_accounts WHERE user_id = ?", (order["seller_id"],)
    ).fetchone()
    if not connect or not connect["payouts_enabled"]:
        raise PaymentError("seller_not_onboarded", "Verkoper heeft geen actieve Stripe Connect-koppeling.")

    amount = payout_amount_cents(order)
    client = get_client()
    transfer = client.create_transfer(
        amount_cents=amount,
        destination_account_id=connect["stripe_account_id"],
        source_transaction_id=order["stripe_payment_intent_id"],
        transfer_group=order["transfer_group"],
        idempotency_key=f"payout-{order['id']}",
    )
    conn.execute(
        "UPDATE orders SET stripe_transfer_id = ? WHERE id = ?", (transfer["id"], order["id"])
    )
    conn.execute(
        """INSERT INTO payouts (id, seller_id, stripe_payout_id, amount_cents, status, initiated_at)
           VALUES (?, ?, ?, ?, 'released_to_connect_balance', ?)""",
        (new_id(), order["seller_id"], transfer["id"], amount, now_ts()),
    )
    notify(conn, order["seller_id"], "payout_released", order_id=order["id"], amount_cents=amount)
    return transfer


@orders_bp.route("/<order_id>/confirm", methods=["POST"])
@csrf_protected
@login_required
def confirm_order(order_id):
    conn = get_db()
    order = _order_or_404(conn, order_id)
    if not order:
        conn.close()
        return jsonify(error="not_found"), 404
    if order["buyer_id"] != session["user_id"]:
        conn.close()
        return jsonify(error="forbidden"), 403

    try:
        transition_order_status(conn, order_id, "completed", changed_by=session["user_id"], reason="buyer_confirmed")
    except OrderStateError as exc:
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 409

    try:
        _release_payout(conn, order)
    except PaymentError as exc:
        conn.rollback()
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 502

    notify(conn, order["seller_id"], "order_completed", order_id=order_id)
    conn.commit()
    row = _order_or_404(conn, order_id)
    conn.close()
    return jsonify(order=_public_order(row))


# ---------------------------------------------------------------------
# Buyer reports an issue
# ---------------------------------------------------------------------
@orders_bp.route("/<order_id>/report-issue", methods=["POST"])
@csrf_protected
@login_required
def report_issue(order_id):
    data = request.get_json(silent=True) or {}
    reason = data.get("reason")
    description = (data.get("description") or "").strip()
    if reason not in DISPUTE_REASONS:
        return jsonify(error="invalid_reason", allowed=sorted(DISPUTE_REASONS)), 400
    if not description:
        return jsonify(error="missing_description"), 400

    conn = get_db()
    order = _order_or_404(conn, order_id)
    if not order:
        conn.close()
        return jsonify(error="not_found"), 404
    if order["buyer_id"] != session["user_id"]:
        conn.close()
        return jsonify(error="forbidden"), 403

    try:
        transition_order_status(conn, order_id, "issue_reported", changed_by=session["user_id"], reason=reason)
    except OrderStateError as exc:
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 409

    response_hours = get_config_hours(conn, "dispute_response_hours", 72)
    ts = now_ts()
    dispute_id = new_id()
    conn.execute(
        """INSERT INTO disputes
           (id, order_id, opened_by, reason, status, description, response_due_at, created_at)
           VALUES (?, ?, ?, ?, 'open', ?, ?, ?)""",
        (dispute_id, order_id, session["user_id"], reason, description, ts + response_hours * 3600, ts),
    )
    conn.execute(
        """INSERT INTO dispute_events (id, dispute_id, event_type, actor_type, actor_id, payload, created_at)
           VALUES (?, ?, 'dispute_opened', 'buyer', ?, ?, ?)""",
        (new_id(), dispute_id, session["user_id"], json.dumps({"reason": reason}), ts),
    )

    try:
        transition_order_status(conn, order_id, "under_review", changed_by=session["user_id"], reason="dispute_opened")
    except OrderStateError as exc:
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 409

    notify(conn, order["seller_id"], "dispute_opened", order_id=order_id, dispute_id=dispute_id, reason=reason)
    conn.commit()
    row = _order_or_404(conn, order_id)
    conn.close()
    return jsonify(order=_public_order(row), dispute_id=dispute_id), 201


# ---------------------------------------------------------------------
# Counterfeit-melding na aankoop (§ H in docs/AUTHENTICITY_ARCHITECTURE.md)
#
# Deliberately reuses the same dispute machinery as report_issue() above
# (reason is always 'counterfeit_suspected', already a valid disputes
# reason) rather than building separate refund/payment handling — the
# only thing genuinely specific to a counterfeit claim is the
# authenticity_reports row this adds, which links the case to the
# original listing's evidence for a reviewer's side-by-side comparison
# (see GET /admin/authenticity-reports/<id> in app/disputes/routes.py).
# ---------------------------------------------------------------------
COUNTERFEIT_EVIDENCE_CATEGORIES = [
    "bottle_photo", "bottle_bottom_photo", "nozzle_photo",
    "batch_code_photo", "box_photo", "packaging_photo", "discrepancy_description",
]


@orders_bp.route("/<order_id>/report-counterfeit", methods=["POST"])
@csrf_protected
@login_required
def report_counterfeit(order_id):
    data = request.get_json(silent=True) or {}
    description = (data.get("description") or "").strip()
    if not description:
        return jsonify(error="missing_description",
                        message="Beschrijf waarom je vermoedt dat dit product niet authentiek is."), 400

    conn = get_db()
    order = _order_or_404(conn, order_id)
    if not order:
        conn.close()
        return jsonify(error="not_found"), 404
    if order["buyer_id"] != session["user_id"]:
        conn.close()
        return jsonify(error="forbidden"), 403

    try:
        transition_order_status(conn, order_id, "issue_reported", changed_by=session["user_id"],
                                  reason="counterfeit_suspected")
    except OrderStateError as exc:
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 409

    response_hours = get_config_hours(conn, "dispute_response_hours", 72)
    ts = now_ts()
    dispute_id = new_id()
    conn.execute(
        """INSERT INTO disputes
           (id, order_id, opened_by, reason, status, description, response_due_at, created_at)
           VALUES (?, ?, ?, 'counterfeit_suspected', 'open', ?, ?, ?)""",
        (dispute_id, order_id, session["user_id"], description, ts + response_hours * 3600, ts),
    )
    conn.execute(
        """INSERT INTO dispute_events (id, dispute_id, event_type, actor_type, actor_id, payload, created_at)
           VALUES (?, ?, 'dispute_opened', 'buyer', ?, ?, ?)""",
        (new_id(), dispute_id, session["user_id"], json.dumps({"reason": "counterfeit_suspected"}), ts),
    )

    try:
        transition_order_status(conn, order_id, "under_review", changed_by=session["user_id"],
                                  reason="counterfeit_suspected")
    except OrderStateError as exc:
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 409

    report_id = new_id()
    conn.execute(
        """INSERT INTO authenticity_reports
           (id, dispute_id, order_id, listing_id, reporter_id, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'open', ?)""",
        (report_id, dispute_id, order_id, order["listing_id"], session["user_id"], ts),
    )

    notify(conn, order["seller_id"], "counterfeit_report_opened", order_id=order_id, dispute_id=dispute_id)
    conn.commit()
    row = _order_or_404(conn, order_id)
    conn.close()
    return jsonify(
        order=_public_order(row), dispute_id=dispute_id, authenticity_report_id=report_id,
        requested_evidence=COUNTERFEIT_EVIDENCE_CATEGORIES,
        instructions=(
            "Upload duidelijke foto's van de fles, de bodem, de batchcode, de doos, de "
            "verstuiver/nozzle en de verpakking, en beschrijf specifiek wat er afwijkt. "
            "Gebruik voldoende licht, maak scherpe foto's zonder filters, en zorg dat "
            "tekst leesbaar en het hele object zichtbaar is."
        ),
    ), 201
