"""
Buyer Protection verification — run with: python3 tests/test_buyer_protection.py

Covers, end-to-end, against the real Flask app + a real (temp) SQLite db
+ the FakeStripeClient payment simulation (see app/payments/stripe_client.py),
every scenario the user explicitly asked to have tested:

  1.  successful order (checkout -> paid -> shipped -> delivered ->
      inspection_period -> buyer confirms -> completed + payout)
  2.  payment fails (webhook payment_intent.payment_failed -> cancelled)
  3.  seller doesn't ship (shipping-deadline sweep -> cancelled + refund)
  4.  package lost (dispute opened while status = shipped, reason not_received)
  5.  successful delivery (mark-delivered sets inspection_deadline_at correctly)
  6.  buyer confirms (-> completed, transfer created)
  7.  inspection period expires, no dispute (sweep -> completed + payout)
  8.  dispute opened before payout (blocks the sweep / normal completion)
  9.  refund (full, via admin dispute resolution)
  10. partial refund (seller still gets paid the remainder)
  11. chargeback (mid-flow order -> under_review; completed order with an
      existing transfer -> best-effort transfer reversal, status untouched)
  12. duplicate webhook delivery (same stripe_event_id twice -> no-op the 2nd time)
  13. duplicate refund request (2nd resolve rejected; the payment-layer
      over-refund guard verified independently too)
  14. manipulated frontend price (client-submitted price fields are ignored;
      server recomputes from listings + fee_rules + platform_config)

Plus supporting checks: dispute evidence/messages are append-only at the
DB layer, cross-user authorization (only the right buyer/seller/admin can
call each endpoint), and admin fee/config updates actually change what
checkout computes.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DEV_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["PAYMENTS_MODE"] = "fake"

from app import create_app  # noqa: E402
from app.db import get_db, new_id, now_ts  # noqa: E402
from app.payments.stripe_client import reset_client_for_tests  # noqa: E402
from app.jobs.sweep import (  # noqa: E402
    sweep_shipping_deadline_expirations,
    sweep_inspection_period_expirations,
)

PASSED = 0
FAILED = 0


def check(label, condition):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok   - {label}")
    else:
        FAILED += 1
        print(f"  FAIL - {label}")


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def csrf(client):
    return client.get("/auth/csrf-token").get_json()["csrf_token"]


def register(app, email, name):
    client = app.test_client()
    token = csrf(client)
    resp = client.post("/auth/register", json={
        "email": email, "password": "hunter2222", "display_name": name,
    }, headers={"X-CSRF-Token": token})
    assert resp.status_code == 201, resp.get_json()
    user_id = resp.get_json()["user"]["id"]
    return client, user_id


def make_seller(client):
    token = csrf(client)
    resp = client.post("/auth/become-seller", headers={"X-CSRF-Token": token})
    assert resp.status_code == 200


def set_admin(user_id):
    conn = get_db()
    conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def seed_listing(seller_id, price_cents=15000):
    conn = get_db()
    listing_id = new_id()
    conn.execute(
        """INSERT INTO listings (id, seller_id, brand, perfume_name, size_ml, condition,
           asking_price_cents, currency, status, created_at)
           VALUES (?, ?, 'Maison Exemple', 'Nuit Fictive', 50, 'like_new', ?, 'EUR', 'active', ?)""",
        (listing_id, seller_id, price_cents, now_ts()),
    )
    conn.commit()
    conn.close()
    return listing_id


def seed_connect_account(seller_id, payouts_enabled=True):
    conn = get_db()
    conn.execute(
        """INSERT INTO stripe_connect_accounts
           (user_id, stripe_account_id, charges_enabled, payouts_enabled, updated_at)
           VALUES (?, ?, 1, ?, ?)""",
        (seller_id, f"acct_fake_{seller_id[:8]}", 1 if payouts_enabled else 0, now_ts()),
    )
    conn.commit()
    conn.close()


def backdate(column, order_id, seconds_ago=3600):
    conn = get_db()
    conn.execute(f"UPDATE orders SET {column} = ? WHERE id = ?", (now_ts() - seconds_ago, order_id))
    conn.commit()
    conn.close()


def checkout(buyer_client, listing_id, extra=None):
    body = {"listing_id": listing_id}
    if extra:
        body.update(extra)
    token = csrf(buyer_client)
    return buyer_client.post("/orders/checkout", json=body, headers={"X-CSRF-Token": token})


def pay_order(app, order_id):
    """Simulate Stripe confirming the PaymentIntent via a real webhook call."""
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    from app.payments.stripe_client import get_client
    stripe = get_client()
    stripe.mark_payment_succeeded(order["stripe_payment_intent_id"])
    payload, event_id = stripe.build_event("payment_intent.succeeded", {"id": order["stripe_payment_intent_id"]})
    resp = app.test_client().post(
        "/webhooks/stripe", data=payload, headers={"Stripe-Signature": "valid-test-signature",
                                                     "Content-Type": "application/json"},
    )
    return resp, payload, event_id


def full_flow_to_awaiting_shipment(app, buyer_client, seller_id, price_cents=15000):
    listing_id = seed_listing(seller_id, price_cents)
    resp = checkout(buyer_client, listing_id)
    order_id = resp.get_json()["order"]["id"]
    pay_resp, _, _ = pay_order(app, order_id)
    return order_id, resp, pay_resp


def get_order(client, order_id):
    return client.get(f"/orders/{order_id}")


def post(client, path, body=None):
    token = csrf(client)
    return client.post(path, json=body or {}, headers={"X-CSRF-Token": token})


def put(client, path, body=None):
    token = csrf(client)
    return client.put(path, json=body or {}, headers={"X-CSRF-Token": token})


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def main():
    app = create_app()
    reset_client_for_tests()

    buyer, buyer_id = register(app, "koper@example.nl", "Koper")
    seller, seller_id = register(app, "verkoper@example.nl", "Verkoper")
    admin, admin_id = register(app, "admin@example.nl", "Beheerder")
    make_seller(seller)
    set_admin(admin_id)
    seed_connect_account(seller_id)

    # ===================================================================
    print("== 1. Successful order, end-to-end ==")
    order_id, checkout_resp, pay_resp = full_flow_to_awaiting_shipment(app, buyer, seller_id, 15000)
    check("checkout returns 201 with client_secret", checkout_resp.status_code == 201
          and "client_secret" in checkout_resp.get_json())
    check("webhook payment_intent.succeeded -> 200", pay_resp.status_code == 200)
    order = get_order(buyer, order_id).get_json()["order"]
    check("order status is awaiting_shipment after payment", order["status"] == "awaiting_shipment")
    check("shipping_deadline_at was set", order["shipping_deadline_at"] is not None)

    ship_resp = post(seller, f"/orders/{order_id}/ship", {"carrier": "PostNL", "tracking_number": "3SXYZ001"})
    check("seller can ship -> 200", ship_resp.status_code == 200)
    check("status shipped", ship_resp.get_json()["order"]["status"] == "shipped")

    forbidden_ship = post(buyer, f"/orders/{order_id}/ship", {"carrier": "X", "tracking_number": "Y"})
    check("buyer cannot ship someone else's sale (403)", forbidden_ship.status_code == 403)

    print("== 5. Successful delivery ==")
    deliver_resp = post(admin, f"/orders/{order_id}/mark-delivered")
    check("admin marks delivered -> 200", deliver_resp.status_code == 200)
    check("status inspection_period", deliver_resp.get_json()["order"]["status"] == "inspection_period")
    check("inspection_deadline_at set", deliver_resp.get_json()["order"]["inspection_deadline_at"] is not None)

    forbidden_deliver = post(seller, f"/orders/{order_id}/mark-delivered")
    check("seller cannot self-confirm delivery (403)", forbidden_deliver.status_code == 403)

    print("== 6. Buyer confirms -> completed + payout ==")
    confirm_resp = post(buyer, f"/orders/{order_id}/confirm")
    check("buyer confirm -> 200", confirm_resp.status_code == 200)
    check("status completed", confirm_resp.get_json()["order"]["status"] == "completed")
    conn = get_db()
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    payout_row = conn.execute("SELECT * FROM payouts WHERE seller_id = ?", (seller_id,)).fetchone()
    conn.close()
    check("stripe_transfer_id recorded", row["stripe_transfer_id"] is not None)
    check("payout amount = item + shipping (not the buyer-protection fee)",
          payout_row["amount_cents"] == row["item_price_cents"] + row["shipping_price_cents"])

    forbidden_confirm = post(seller, f"/orders/{order_id}/confirm")
    check("seller cannot confirm their own sale (403 or 409)", forbidden_confirm.status_code in (403, 404))

    # ===================================================================
    print("== 2. Payment fails ==")
    listing_id = seed_listing(seller_id, 9000)
    co = checkout(buyer, listing_id)
    failed_order_id = co.get_json()["order"]["id"]
    conn = get_db()
    o = conn.execute("SELECT * FROM orders WHERE id = ?", (failed_order_id,)).fetchone()
    conn.close()
    from app.payments.stripe_client import get_client
    stripe = get_client()
    stripe.mark_payment_failed(o["stripe_payment_intent_id"])
    payload, _ = stripe.build_event("payment_intent.payment_failed", {"id": o["stripe_payment_intent_id"]})
    wh = app.test_client().post("/webhooks/stripe", data=payload,
                                  headers={"Stripe-Signature": "valid-test-signature"})
    check("webhook payment_intent.payment_failed -> 200", wh.status_code == 200)
    order = get_order(buyer, failed_order_id).get_json()["order"]
    check("order cancelled after payment failure", order["status"] == "cancelled")

    # ===================================================================
    print("== 3. Seller doesn't ship -> sweep cancels + refunds ==")
    order_id2, _, _ = full_flow_to_awaiting_shipment(app, buyer, seller_id, 12000)
    backdate("shipping_deadline_at", order_id2, seconds_ago=10)
    affected = sweep_shipping_deadline_expirations()
    check("sweep picked up the overdue order", order_id2 in affected)
    order = get_order(buyer, order_id2).get_json()["order"]
    check("order cancelled by sweep", order["status"] == "cancelled")
    conn = get_db()
    refund_row = conn.execute("SELECT * FROM refunds WHERE order_id = ?", (order_id2,)).fetchone()
    conn.close()
    check("full refund recorded for no-shipment cancellation (entire total, not just item price)",
          refund_row is not None and refund_row["amount_cents"] == order["total_price_cents"])

    # ===================================================================
    print("== 4. Package lost (dispute while status=shipped, reason=not_received) ==")
    order_id3, _, _ = full_flow_to_awaiting_shipment(app, buyer, seller_id, 8000)
    post(seller, f"/orders/{order_id3}/ship", {"carrier": "DHL", "tracking_number": "JD0001"})
    issue_resp = post(buyer, f"/orders/{order_id3}/report-issue",
                       {"reason": "not_received", "description": "Pakket nooit aangekomen volgens tracking."})
    check("report-issue on shipped order -> 201", issue_resp.status_code == 201)
    order = get_order(buyer, order_id3).get_json()["order"]
    check("order moved to under_review", order["status"] == "under_review")
    bad_reason = post(buyer, f"/orders/{order_id3}/report-issue", {"reason": "not_a_real_reason", "description": "x"})
    check("invalid dispute reason rejected (400)", bad_reason.status_code == 400)

    # ===================================================================
    print("== 7. Inspection period expires, no dispute -> sweep completes + pays out ==")
    order_id4, _, _ = full_flow_to_awaiting_shipment(app, buyer, seller_id, 11000)
    post(seller, f"/orders/{order_id4}/ship", {"carrier": "PostNL", "tracking_number": "3SXYZ002"})
    post(admin, f"/orders/{order_id4}/mark-delivered")
    backdate("inspection_deadline_at", order_id4, seconds_ago=5)
    affected = sweep_inspection_period_expirations()
    check("sweep picked up the expired inspection order", order_id4 in affected)
    order = get_order(buyer, order_id4).get_json()["order"]
    check("order auto-completed", order["status"] == "completed")
    conn = get_db()
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id4,)).fetchone()
    conn.close()
    check("payout transfer recorded for auto-completed order", row["stripe_transfer_id"] is not None)

    # ===================================================================
    print("== 8. Dispute opened before payout blocks completion ==")
    order_id5, _, _ = full_flow_to_awaiting_shipment(app, buyer, seller_id, 20000)
    post(seller, f"/orders/{order_id5}/ship", {"carrier": "PostNL", "tracking_number": "3SXYZ003"})
    post(admin, f"/orders/{order_id5}/mark-delivered")
    dispute_resp = post(buyer, f"/orders/{order_id5}/report-issue",
                         {"reason": "damaged", "description": "Fles gebarsten aangekomen, foto's bijgevoegd."})
    check("report-issue during inspection_period -> 201", dispute_resp.status_code == 201)
    dispute_id5 = dispute_resp.get_json()["dispute_id"]
    order = get_order(buyer, order_id5).get_json()["order"]
    check("order in under_review, NOT completed", order["status"] == "under_review")
    check("no payout happened yet", order["completed_at"] is None)
    backdate("inspection_deadline_at", order_id5, seconds_ago=5)  # would be overdue if still in inspection_period
    affected = sweep_inspection_period_expirations()
    check("sweep does NOT touch a disputed order (it's no longer inspection_period)",
          order_id5 not in affected)

    # Evidence + messages + admin notes, and append-only enforcement
    ev1 = post(buyer, f"/disputes/{dispute_id5}/evidence",
               {"evidence_type": "photo", "file_ref": "uploads/broken-bottle.jpg"})
    check("buyer submits evidence -> 201", ev1.status_code == 201)
    ev2 = post(seller, f"/disputes/{dispute_id5}/evidence",
               {"evidence_type": "packaging_photo", "file_ref": "uploads/box-as-shipped.jpg"})
    check("seller submits evidence -> 201", ev2.status_code == 201)
    msg1 = post(buyer, f"/disputes/{dispute_id5}/messages", {"body": "Zie de foto's, de fles was gebarsten."})
    check("buyer message -> 201", msg1.status_code == 201)
    outsider_ev = post(admin, f"/disputes/{dispute_id5}/evidence", {"evidence_type": "photo", "file_ref": "x"})
    check("a non-party (admin, not buyer/seller) cannot submit evidence (403)", outsider_ev.status_code == 403)
    note1 = post(admin, f"/disputes/{dispute_id5}/admin-notes", {"note": "Bewijs lijkt overtuigend, verkoper reageerde niet."})
    check("admin can add an internal note -> 201", note1.status_code == 201)
    note_forbidden = post(buyer, f"/disputes/{dispute_id5}/admin-notes", {"note": "ik probeer te gluren"})
    check("buyer cannot add an admin note (403)", note_forbidden.status_code == 403)

    case = buyer.get(f"/disputes/{dispute_id5}").get_json()
    check("buyer sees evidence + messages but NOT admin_notes", len(case["evidence"]) == 2
          and len(case["messages"]) == 1 and case["admin_notes"] == [])
    admin_case = admin.get(f"/disputes/{dispute_id5}").get_json()
    check("admin sees admin_notes too", len(admin_case["admin_notes"]) == 1)

    ev_id = ev1.get_json()["evidence_id"]
    conn = get_db()
    mutation_blocked = False
    try:
        conn.execute("UPDATE dispute_evidence SET text_value = 'gehackt' WHERE id = ?", (ev_id,))
        conn.commit()
    except Exception:
        mutation_blocked = True
    check("dispute_evidence UPDATE is rejected at the database level", mutation_blocked)
    delete_blocked = False
    try:
        conn.execute("DELETE FROM dispute_evidence WHERE id = ?", (ev_id,))
        conn.commit()
    except Exception:
        delete_blocked = True
    check("dispute_evidence DELETE is rejected at the database level", delete_blocked)
    conn.close()

    # ===================================================================
    print("== 9. Refund (full) via admin dispute resolution ==")
    resolve_full = post(admin, f"/disputes/{dispute_id5}/resolve", {"resolution": "refund_full", "notes": "Bewijs klopt."})
    check("admin resolves with full refund -> 200", resolve_full.status_code == 200)
    order = get_order(buyer, order_id5).get_json()["order"]
    check("order status refunded", order["status"] == "refunded")
    conn = get_db()
    o5_row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id5,)).fetchone()
    check("no payout to seller for a fully-refunded order", o5_row["stripe_transfer_id"] is None)
    refund_row = conn.execute("SELECT * FROM refunds WHERE order_id = ?", (order_id5,)).fetchone()
    conn.close()
    check("refund amount = full total_price_cents", refund_row["amount_cents"] == order["total_price_cents"])

    # ===================================================================
    print("== 13. Duplicate refund request is rejected ==")
    resolve_again = post(admin, f"/disputes/{dispute_id5}/resolve", {"resolution": "refund_full"})
    check("resolving an already-resolved dispute again -> 409", resolve_again.status_code == 409)

    # Independently verify the payment-layer over-refund guard itself
    # (not just the app-level "already resolved" dedup above) using a
    # deliberately different idempotency key, exactly like a genuinely
    # separate duplicate request would send:
    from app.payments.stripe_client import PaymentError
    conn = get_db()
    o5 = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id5,)).fetchone()
    conn.close()
    guard_tripped = False
    try:
        stripe.create_refund(o5["stripe_payment_intent_id"], 100, idempotency_key=str(new_id()))
    except PaymentError as exc:
        guard_tripped = exc.code == "invalid_request"
    check("FakeStripeClient itself refuses a refund exceeding the original amount", guard_tripped)

    # ===================================================================
    print("== 10. Partial refund (seller still paid the remainder) ==")
    order_id6, _, _ = full_flow_to_awaiting_shipment(app, buyer, seller_id, 30000)
    post(seller, f"/orders/{order_id6}/ship", {"carrier": "PostNL", "tracking_number": "3SXYZ004"})
    post(admin, f"/orders/{order_id6}/mark-delivered")
    dispute6 = post(buyer, f"/orders/{order_id6}/report-issue",
                     {"reason": "significantly_not_as_described", "description": "Inhoud minder vol dan vermeld."})
    dispute_id6 = dispute6.get_json()["dispute_id"]
    partial_resolve = post(admin, f"/disputes/{dispute_id6}/resolve",
                            {"resolution": "refund_partial", "refund_amount_cents": 5000,
                             "notes": "Deels gecrediteerd voor het contentverschil."})
    check("partial refund resolution -> 200", partial_resolve.status_code == 200)
    order = get_order(buyer, order_id6).get_json()["order"]
    check("order completed (not refunded) after partial refund", order["status"] == "completed")
    conn = get_db()
    refund_row = conn.execute("SELECT * FROM refunds WHERE order_id = ?", (order_id6,)).fetchone()
    payout_row = conn.execute(
        "SELECT * FROM payouts WHERE stripe_payout_id LIKE 'partial-%' AND stripe_payout_id LIKE ?",
        (f"%{order_id6}%",),
    ).fetchone()
    conn.close()
    check("partial refund amount recorded", refund_row["amount_cents"] == 5000)
    check("seller payout reduced by the refunded amount",
          payout_row is not None and payout_row["amount_cents"] == (30000 + 495) - 5000)

    # ===================================================================
    print("== 11. Chargeback ==")
    # (a) mid-flow order -> pulled into under_review, no transfer to reverse yet
    order_id7, _, _ = full_flow_to_awaiting_shipment(app, buyer, seller_id, 7000)
    conn = get_db()
    o7 = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id7,)).fetchone()
    conn.close()
    cb_payload, _ = stripe.build_event("charge.dispute.created", {
        "id": f"dp_fake_{order_id7[:8]}", "payment_intent": o7["stripe_payment_intent_id"],
        "amount": o7["total_price_cents"], "reason": "fraudulent",
    })
    cb_resp = app.test_client().post("/webhooks/stripe", data=cb_payload,
                                       headers={"Stripe-Signature": "valid-test-signature"})
    check("chargeback webhook -> 200", cb_resp.status_code == 200)
    order = get_order(buyer, order_id7).get_json()["order"]
    check("mid-flow order pulled into under_review by chargeback", order["status"] == "under_review")
    conn = get_db()
    cb_row = conn.execute("SELECT * FROM chargebacks WHERE order_id = ?", (order_id7,)).fetchone()
    conn.close()
    check("chargeback recorded, no transfer to reverse", cb_row is not None and cb_row["transfer_reversed"] == 0)

    # (b) already-completed order with an existing transfer -> best-effort reversal, status untouched
    order_id8 = order_id  # the order fully completed back in scenario 1
    conn = get_db()
    o8 = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id8,)).fetchone()
    conn.close()
    cb_payload8, _ = stripe.build_event("charge.dispute.created", {
        "id": f"dp_fake_{order_id8[:8]}", "payment_intent": o8["stripe_payment_intent_id"],
        "amount": 1000, "reason": "product_not_received",
    })
    cb_resp8 = app.test_client().post("/webhooks/stripe", data=cb_payload8,
                                        headers={"Stripe-Signature": "valid-test-signature"})
    check("chargeback webhook on completed order -> 200", cb_resp8.status_code == 200)
    order8 = get_order(buyer, order_id8).get_json()["order"]
    check("completed order's status is NOT retroactively reopened", order8["status"] == "completed")
    conn = get_db()
    cb_row8 = conn.execute("SELECT * FROM chargebacks WHERE order_id = ?", (order_id8,)).fetchone()
    conn.close()
    check("best-effort transfer reversal attempted and succeeded", cb_row8["transfer_reversed"] == 1)

    # ===================================================================
    print("== 12. Duplicate webhook delivery ==")
    order_id9, checkout9, _ = None, None, None
    listing_id9 = seed_listing(seller_id, 6000)
    co9 = checkout(buyer, listing_id9)
    order_id9 = co9.get_json()["order"]["id"]
    conn = get_db()
    o9 = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id9,)).fetchone()
    conn.close()
    stripe.mark_payment_succeeded(o9["stripe_payment_intent_id"])
    payload9, event_id9 = stripe.build_event("payment_intent.succeeded", {"id": o9["stripe_payment_intent_id"]})
    r1 = app.test_client().post("/webhooks/stripe", data=payload9, headers={"Stripe-Signature": "valid-test-signature"})
    r2 = app.test_client().post("/webhooks/stripe", data=payload9, headers={"Stripe-Signature": "valid-test-signature"})
    check("first delivery -> 200, processed", r1.status_code == 200 and r1.get_json().get("duplicate") is not True)
    check("second (duplicate) delivery -> 200, marked duplicate, not reprocessed",
          r2.status_code == 200 and r2.get_json().get("duplicate") is True)
    conn = get_db()
    wh_count = conn.execute(
        "SELECT COUNT(*) c FROM webhook_events WHERE stripe_event_id = ?", (event_id9,)
    ).fetchone()["c"]
    history_count = conn.execute(
        "SELECT COUNT(*) c FROM order_status_history WHERE order_id = ? AND to_status = 'paid'", (order_id9,)
    ).fetchone()["c"]
    conn.close()
    check("webhook_events has exactly one row for this event id (UNIQUE)", wh_count == 1)
    check("order transitioned to paid exactly once, not twice", history_count == 1)

    # ===================================================================
    print("== 14. Manipulated frontend price is ignored ==")
    listing_id10 = seed_listing(seller_id, 25000)
    malicious = checkout(buyer, listing_id10, extra={
        "total_price_cents": 1, "item_price_cents": 1, "price": 1, "total": "€0.01",
    })
    check("checkout still succeeds (bogus fields silently ignored)", malicious.status_code == 201)
    order = malicious.get_json()["order"]
    conn = get_db()
    fee_rule = conn.execute("SELECT * FROM fee_rules WHERE fee_key = 'buyer_protection'").fetchone()
    shipping_default = conn.execute(
        "SELECT config_value FROM platform_config WHERE config_key = 'shipping_flat_rate_cents'"
    ).fetchone()
    conn.close()
    expected_fee = (25000 * fee_rule["percentage_bps"] // 10000) + fee_rule["fixed_cents"]
    expected_total = 25000 + int(shipping_default["config_value"]) + expected_fee
    check("item_price_cents computed server-side, matches the real listing price",
          order["item_price_cents"] == 25000)
    check("total_price_cents computed server-side, ignores the submitted 1-cent total",
          order["total_price_cents"] == expected_total)

    # ===================================================================
    print("== Admin fee/config changes actually affect checkout ==")
    new_rule = put(admin, "/admin/fee-rules/buyer_protection", {"percentage_bps": 800, "fixed_cents": 150})
    check("admin updates buyer_protection fee rule -> 200", new_rule.status_code == 200)
    conn = get_db()
    hist = conn.execute(
        "SELECT COUNT(*) c FROM fee_rule_history WHERE fee_key = 'buyer_protection'"
    ).fetchone()["c"]
    conn.close()
    check("fee rule change is recorded in fee_rule_history (audit trail)", hist >= 1)
    listing_id11 = seed_listing(seller_id, 10000)
    co11 = checkout(buyer, listing_id11)
    order11 = co11.get_json()["order"]
    check("new fee rule (8% + 150c) applied without any code change",
          order11["buyer_protection_fee_cents"] == (10000 * 800 // 10000) + 150)

    non_admin_fee = put(seller, "/admin/fee-rules/buyer_protection", {"percentage_bps": 1, "fixed_cents": 1})
    check("a non-admin cannot change fee rules (403)", non_admin_fee.status_code == 403)

    print()
    print(f"{PASSED} passed, {FAILED} failed")
    return FAILED == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
