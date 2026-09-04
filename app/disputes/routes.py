"""
Disputes blueprint — evidence, messages, admin notes, and resolution.

Every dispute is tied 1:1 to an order that is (or was) `under_review`
(app/orders/routes.py::report_issue is the only place a dispute gets
created). This blueprint never writes to orders.status directly — it
always goes through app.orders.state_machine.transition_order_status().

Immutability: dispute_evidence, dispute_messages, dispute_admin_notes and
dispute_events are append-only at the DATABASE level (see the
`trg_..._no_update` / `trg_..._no_delete` triggers in app/db.py, mirrored
by real Postgres triggers in db/schema.sql) — this blueprint's INSERT-only
usage of those tables isn't the only thing stopping a party from editing
or deleting the other side's evidence, the database itself refuses it
even if application code had a bug.
"""
import json

from flask import Blueprint, jsonify, request, session

from app.auth.routes import login_required, admin_required, csrf_protected
from app.db import get_db, new_id, now_ts
from app.notifications import notify
from app.orders.state_machine import transition_order_status, OrderStateError
from app.orders.routes import _release_payout
from app.payments.stripe_client import get_client, PaymentError

disputes_bp = Blueprint("disputes", __name__, url_prefix="/disputes")

EVIDENCE_TYPES = {
    "photo", "video", "description", "packaging_photo", "shipping_label",
    "batch_code_photo", "bottle_photo", "box_photo", "proof_of_purchase",
    # Counterfeit-specific evidence categories (§ H) — what a buyer
    # documents about the RECEIVED product, a separate set from the 12
    # listing-photo categories a seller uploads for the advertisement.
    "bottle_bottom_photo", "nozzle_photo", "discrepancy_description",
}

RESOLUTIONS = {"release_to_seller", "refund_full", "refund_partial", "require_return"}


def _dispute_or_404(conn, dispute_id):
    return conn.execute("SELECT * FROM disputes WHERE id = ?", (dispute_id,)).fetchone()


def _order_for_dispute(conn, dispute):
    return conn.execute("SELECT * FROM orders WHERE id = ?", (dispute["order_id"],)).fetchone()


def _is_party(order, user_id):
    return order["buyer_id"] == user_id or order["seller_id"] == user_id


def _is_admin(conn, user_id):
    row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["is_admin"])


def _public_dispute(row):
    return {
        "id": row["id"], "order_id": row["order_id"], "opened_by": row["opened_by"],
        "reason": row["reason"], "status": row["status"], "description": row["description"],
        "response_due_at": row["response_due_at"], "resolution_notes": row["resolution_notes"],
        "resolved_by": row["resolved_by"], "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
    }


# ---------------------------------------------------------------------
# Read the full case: timeline = evidence + messages + events, in order.
# Admin notes are only included for admins.
# ---------------------------------------------------------------------
@disputes_bp.route("/<dispute_id>", methods=["GET"])
@login_required
def get_dispute(dispute_id):
    conn = get_db()
    dispute = _dispute_or_404(conn, dispute_id)
    if not dispute:
        conn.close()
        return jsonify(error="not_found"), 404
    order = _order_for_dispute(conn, dispute)
    user_id = session["user_id"]
    is_admin = _is_admin(conn, user_id)
    if not (_is_party(order, user_id) or is_admin):
        conn.close()
        return jsonify(error="not_found"), 404

    evidence = conn.execute(
        "SELECT * FROM dispute_evidence WHERE dispute_id = ? ORDER BY created_at ASC", (dispute_id,)
    ).fetchall()
    messages = conn.execute(
        "SELECT * FROM dispute_messages WHERE dispute_id = ? ORDER BY created_at ASC", (dispute_id,)
    ).fetchall()
    events = conn.execute(
        "SELECT * FROM dispute_events WHERE dispute_id = ? ORDER BY created_at ASC", (dispute_id,)
    ).fetchall()
    admin_notes = []
    if is_admin:
        admin_notes = [
            dict(r) for r in conn.execute(
                "SELECT * FROM dispute_admin_notes WHERE dispute_id = ? ORDER BY created_at ASC",
                (dispute_id,),
            ).fetchall()
        ]
    conn.close()

    return jsonify(
        dispute=_public_dispute(dispute),
        evidence=[dict(r) for r in evidence],
        messages=[dict(r) for r in messages],
        events=[dict(r) for r in events],
        admin_notes=admin_notes,
    )


# ---------------------------------------------------------------------
# Evidence — either party, append-only.
# ---------------------------------------------------------------------
@disputes_bp.route("/<dispute_id>/evidence", methods=["POST"])
@csrf_protected
@login_required
def add_evidence(dispute_id):
    data = request.get_json(silent=True) or {}
    evidence_type = data.get("evidence_type")
    file_ref = data.get("file_ref")
    text_value = data.get("text_value")
    if evidence_type not in EVIDENCE_TYPES:
        return jsonify(error="invalid_evidence_type", allowed=sorted(EVIDENCE_TYPES)), 400
    if not file_ref and not text_value:
        return jsonify(error="missing_evidence_content"), 400

    conn = get_db()
    dispute = _dispute_or_404(conn, dispute_id)
    if not dispute:
        conn.close()
        return jsonify(error="not_found"), 404
    order = _order_for_dispute(conn, dispute)
    user_id = session["user_id"]
    if not _is_party(order, user_id):
        conn.close()
        return jsonify(error="forbidden"), 403
    if dispute["status"] != "open":
        conn.close()
        return jsonify(error="dispute_closed", message="Deze zaak is al afgesloten."), 409

    evidence_id = new_id()
    ts = now_ts()
    conn.execute(
        """INSERT INTO dispute_evidence
           (id, dispute_id, submitted_by, evidence_type, file_ref, text_value, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (evidence_id, dispute_id, user_id, evidence_type, file_ref, text_value, ts),
    )
    conn.execute(
        """INSERT INTO dispute_events (id, dispute_id, event_type, actor_type, actor_id, payload, created_at)
           VALUES (?, ?, 'evidence_submitted', 'party', ?, ?, ?)""",
        (new_id(), dispute_id, user_id, json.dumps({"evidence_type": evidence_type}), ts),
    )
    other_party = order["seller_id"] if user_id == order["buyer_id"] else order["buyer_id"]
    notify(conn, other_party, "dispute_evidence_submitted", dispute_id=dispute_id, evidence_type=evidence_type)
    conn.commit()
    conn.close()
    return jsonify(evidence_id=evidence_id), 201


# ---------------------------------------------------------------------
# Buyer/seller messages — append-only.
# ---------------------------------------------------------------------
@disputes_bp.route("/<dispute_id>/messages", methods=["POST"])
@csrf_protected
@login_required
def add_message(dispute_id):
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify(error="missing_body"), 400

    conn = get_db()
    dispute = _dispute_or_404(conn, dispute_id)
    if not dispute:
        conn.close()
        return jsonify(error="not_found"), 404
    order = _order_for_dispute(conn, dispute)
    user_id = session["user_id"]
    if not _is_party(order, user_id):
        conn.close()
        return jsonify(error="forbidden"), 403

    message_id = new_id()
    ts = now_ts()
    conn.execute(
        "INSERT INTO dispute_messages (id, dispute_id, sender_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
        (message_id, dispute_id, user_id, body, ts),
    )
    other_party = order["seller_id"] if user_id == order["buyer_id"] else order["buyer_id"]
    notify(conn, other_party, "dispute_message", dispute_id=dispute_id)
    conn.commit()
    conn.close()
    return jsonify(message_id=message_id), 201


# ---------------------------------------------------------------------
# Admin-only internal notes — append-only, never visible to buyer/seller.
# ---------------------------------------------------------------------
@disputes_bp.route("/<dispute_id>/admin-notes", methods=["POST"])
@csrf_protected
@login_required
@admin_required
def add_admin_note(dispute_id):
    data = request.get_json(silent=True) or {}
    note = (data.get("note") or "").strip()
    if not note:
        return jsonify(error="missing_note"), 400

    conn = get_db()
    dispute = _dispute_or_404(conn, dispute_id)
    if not dispute:
        conn.close()
        return jsonify(error="not_found"), 404

    note_id = new_id()
    conn.execute(
        "INSERT INTO dispute_admin_notes (id, dispute_id, admin_id, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (note_id, dispute_id, session["user_id"], note, now_ts()),
    )
    conn.commit()
    conn.close()
    return jsonify(note_id=note_id), 201


# ---------------------------------------------------------------------
# Buyer marks the required return as shipped back to the seller.
# ---------------------------------------------------------------------
@disputes_bp.route("/<dispute_id>/return-shipped", methods=["POST"])
@csrf_protected
@login_required
def return_shipped(dispute_id):
    data = request.get_json(silent=True) or {}
    tracking_number = (data.get("tracking_number") or "").strip()
    if not tracking_number:
        return jsonify(error="missing_tracking_number"), 400

    conn = get_db()
    dispute = _dispute_or_404(conn, dispute_id)
    if not dispute:
        conn.close()
        return jsonify(error="not_found"), 404
    order = _order_for_dispute(conn, dispute)
    if order["buyer_id"] != session["user_id"]:
        conn.close()
        return jsonify(error="forbidden"), 403

    try:
        transition_order_status(conn, order["id"], "return_shipped", changed_by=session["user_id"])
    except OrderStateError as exc:
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 409

    conn.execute(
        """INSERT INTO dispute_events (id, dispute_id, event_type, actor_type, actor_id, payload, created_at)
           VALUES (?, ?, 'return_shipped', 'buyer', ?, ?, ?)""",
        (new_id(), dispute_id, session["user_id"], json.dumps({"tracking_number": tracking_number}), now_ts()),
    )
    notify(conn, order["seller_id"], "return_shipped", dispute_id=dispute_id, tracking_number=tracking_number)
    conn.commit()
    conn.close()
    return jsonify(ok=True)


# ---------------------------------------------------------------------
# Admin resolves — the one place a dispute's outcome becomes money.
# ---------------------------------------------------------------------
@disputes_bp.route("/<dispute_id>/resolve", methods=["POST"])
@csrf_protected
@login_required
@admin_required
def resolve_dispute(dispute_id):
    data = request.get_json(silent=True) or {}
    resolution = data.get("resolution")
    notes = (data.get("notes") or "").strip()
    if resolution not in RESOLUTIONS:
        return jsonify(error="invalid_resolution", allowed=sorted(RESOLUTIONS)), 400

    conn = get_db()
    dispute = _dispute_or_404(conn, dispute_id)
    if not dispute:
        conn.close()
        return jsonify(error="not_found"), 404
    if dispute["status"] != "open":
        conn.close()
        return jsonify(error="dispute_already_resolved",
                        message="Deze zaak is al afgehandeld."), 409

    order = _order_for_dispute(conn, dispute)
    admin_id = session["user_id"]
    ts = now_ts()

    try:
        if resolution == "release_to_seller":
            if order["status"] not in ("under_review",):
                conn.close()
                return jsonify(error="invalid_order_status_for_resolution"), 409
            transition_order_status(conn, order["id"], "completed", changed_by=f"admin:{admin_id}",
                                      reason="dispute_resolved_release_to_seller")
            _release_payout(conn, order)

        elif resolution in ("refund_full", "refund_partial"):
            if order["status"] not in ("under_review", "return_shipped"):
                conn.close()
                return jsonify(error="invalid_order_status_for_resolution"), 409

            if resolution == "refund_full":
                refund_amount = order["total_price_cents"]
            else:
                refund_amount = data.get("refund_amount_cents")
                if not isinstance(refund_amount, int) or not (0 < refund_amount < order["total_price_cents"]):
                    conn.close()
                    return jsonify(error="invalid_refund_amount",
                                    message="refund_amount_cents moet tussen 0 en total_price_cents liggen "
                                            "(gebruik refund_full voor het volledige bedrag)."), 400

            client = get_client()
            idem_key = str(new_id())  # fresh key per admin action, deliberately NOT
            # derived from the order id, so a genuinely separate duplicate
            # refund REQUEST (as opposed to a network retry of the same
            # request) is not silently deduped by Stripe's idempotency
            # cache — it must instead be caught by the over-refund guard
            # inside create_refund() itself, which is what this endpoint's
            # order-status check plus that guard together provide.
            try:
                client.create_refund(
                    payment_intent_id=order["stripe_payment_intent_id"],
                    amount_cents=refund_amount,
                    idempotency_key=idem_key,
                )
            except PaymentError as exc:
                conn.close()
                return jsonify(error=exc.code, message=exc.message), 502
            conn.execute(
                """INSERT INTO refunds
                   (id, order_id, stripe_refund_id, amount_cents, reason, status,
                    initiated_by, idempotency_key, created_at)
                   VALUES (?, ?, ?, ?, ?, 'succeeded', ?, ?, ?)""",
                (new_id(), order["id"], idem_key, refund_amount, dispute["reason"],
                 f"admin:{admin_id}", idem_key, ts),
            )

            if resolution == "refund_full":
                # under_review -> refunded IS in the whitelist; so is
                # return_shipped -> refunded. Both land on 'refunded'.
                transition_order_status(conn, order["id"], "refunded", changed_by=f"admin:{admin_id}",
                                          reason="dispute_resolved_refund_full")
                notify(conn, order["buyer_id"], "refund_issued", order_id=order["id"], amount_cents=refund_amount)
            else:
                transition_order_status(conn, order["id"], "completed", changed_by=f"admin:{admin_id}",
                                          reason="dispute_resolved_refund_partial")
                remaining = max(0, order["item_price_cents"] + order["shipping_price_cents"] - refund_amount)
                if remaining > 0:
                    connect = conn.execute(
                        "SELECT * FROM stripe_connect_accounts WHERE user_id = ?", (order["seller_id"],)
                    ).fetchone()
                    if connect and connect["payouts_enabled"]:
                        client.create_transfer(
                            amount_cents=remaining,
                            destination_account_id=connect["stripe_account_id"],
                            source_transaction_id=order["stripe_payment_intent_id"],
                            transfer_group=order["transfer_group"],
                            idempotency_key=f"payout-partial-{order['id']}",
                        )
                        conn.execute(
                            """INSERT INTO payouts
                               (id, seller_id, stripe_payout_id, amount_cents, status, initiated_at)
                               VALUES (?, ?, ?, ?, 'released_to_connect_balance', ?)""",
                            (new_id(), order["seller_id"], f"partial-{order['id']}", remaining, ts),
                        )
                notify(conn, order["buyer_id"], "refund_issued", order_id=order["id"], amount_cents=refund_amount)
                notify(conn, order["seller_id"], "order_completed_partial_refund", order_id=order["id"])

        elif resolution == "require_return":
            if order["status"] != "under_review":
                conn.close()
                return jsonify(error="invalid_order_status_for_resolution"), 409
            transition_order_status(conn, order["id"], "return_required", changed_by=f"admin:{admin_id}")
            notify(conn, order["buyer_id"], "return_required", order_id=order["id"], notes=notes)
            conn.execute(
                "UPDATE disputes SET resolution_notes = ?, resolved_by = ? WHERE id = ?",
                (notes, admin_id, dispute_id),
            )
            conn.commit()
            conn.close()
            return jsonify(ok=True, dispute_status="open", order_status="return_required")

    except OrderStateError as exc:
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 409
    except PaymentError as exc:
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 502

    conn.execute(
        "UPDATE disputes SET status = 'resolved', resolution_notes = ?, resolved_by = ?, resolved_at = ? WHERE id = ?",
        (notes, admin_id, ts, dispute_id),
    )
    conn.execute(
        """INSERT INTO dispute_events (id, dispute_id, event_type, actor_type, actor_id, payload, created_at)
           VALUES (?, ?, 'dispute_resolved', 'admin', ?, ?, ?)""",
        (new_id(), dispute_id, admin_id, json.dumps({"resolution": resolution}), ts),
    )
    notify(conn, order["seller_id"] if resolution != "release_to_seller" else order["buyer_id"],
           "dispute_resolved", dispute_id=dispute_id, resolution=resolution)
    conn.commit()
    conn.close()
    return jsonify(ok=True, resolution=resolution)
