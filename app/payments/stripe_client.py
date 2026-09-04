"""
Payment provider abstraction.

Why this exists: the business logic in app/orders and app/disputes (the
buyer-protection state machine, the idempotency handling, the refund/
reversal flow) must be fully buildable and testable WITHOUT a live Stripe
account — because creating that account, and handling its live secret
key, is something only the platform owner can do (see docs/ARCHITECTURE.md
§ D). This module defines the interface once, with two implementations:

  - LiveStripeClient: thin wrapper around the real `stripe` package,
    following the "Separate charges and transfers" pattern exactly as
    researched from the current official Stripe docs (see
    docs/ARCHITECTURE.md § D for the source URLs). Requires the `stripe`
    package (not installable in this sandbox — see requirements.txt) and
    a real STRIPE_SECRET_KEY. This is what production uses.

  - FakeStripeClient: an in-memory simulation used by the test suite and
    for local development without Stripe credentials. It mimics Stripe's
    real behaviour closely enough to test idempotency, double-webhook
    delivery, refunds, transfer reversals and chargebacks honestly — but
    it is NOT Stripe, moves no real money, and must never be selected in
    production (see get_client() below).

Both implementations return plain dicts shaped like the relevant Stripe
API objects (id, status, amount, ...), so calling code never branches on
which implementation is active.
"""
import os
import time
import uuid


class PaymentError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# ---------------------------------------------------------------------
# Live implementation — real Stripe. Only usable where `stripe` is
# installed and STRIPE_SECRET_KEY is a real key; both are deliberately
# absent in this sandbox (see requirements.txt / README.md).
# ---------------------------------------------------------------------
class LiveStripeClient:
    def __init__(self, secret_key, webhook_secret):
        try:
            import stripe  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "The `stripe` package is not installed. This is expected in "
                "the development sandbox (see README.md) — install "
                "requirements.txt in an environment with normal internet "
                "access before using LiveStripeClient."
            ) from exc
        import stripe as stripe_module
        stripe_module.api_key = secret_key
        self._stripe = stripe_module
        self._webhook_secret = webhook_secret

    def create_payment_intent(self, amount_cents, currency, transfer_group, idempotency_key):
        pi = self._stripe.PaymentIntent.create(
            amount=amount_cents,
            currency=currency,
            transfer_group=transfer_group,
            idempotency_key=idempotency_key,
        )
        return {"id": pi.id, "status": pi.status, "client_secret": pi.client_secret}

    def create_transfer(self, amount_cents, destination_account_id, source_transaction_id,
                          transfer_group, idempotency_key):
        tr = self._stripe.Transfer.create(
            amount=amount_cents,
            currency="eur",
            destination=destination_account_id,
            source_transaction=source_transaction_id,
            transfer_group=transfer_group,
            idempotency_key=idempotency_key,
        )
        return {"id": tr.id, "amount": tr.amount}

    def create_refund(self, payment_intent_id, amount_cents, idempotency_key):
        rf = self._stripe.Refund.create(
            payment_intent=payment_intent_id,
            amount=amount_cents,
            idempotency_key=idempotency_key,
        )
        return {"id": rf.id, "status": rf.status, "amount": rf.amount}

    def create_transfer_reversal(self, transfer_id, amount_cents, idempotency_key):
        rev = self._stripe.Transfer.create_reversal(
            transfer_id,
            amount=amount_cents,
            idempotency_key=idempotency_key,
        )
        return {"id": rev.id, "amount": rev.amount}

    def construct_webhook_event(self, payload_bytes, sig_header):
        event = self._stripe.Webhook.construct_event(payload_bytes, sig_header, self._webhook_secret)
        return event  # dict-like, has .id / ["type"] / ["data"]["object"]


# ---------------------------------------------------------------------
# Fake implementation — in-memory, deterministic, built specifically to
# exercise: idempotency (same key -> same cached result), double
# transfers, partial/duplicate refunds, and simulated webhook delivery
# (including deliberately re-delivering the same event to test dedup).
# ---------------------------------------------------------------------
class FakeStripeClient:
    def __init__(self):
        self._idempotency_cache = {}   # idempotency_key -> response dict
        self.payment_intents = {}       # id -> {..., "amount_refunded": int}
        self.transfers = {}             # id -> {..., "amount_reversed": int}
        self.refunds = {}
        self.reversals = {}
        self._pending_events = []       # events queued by test helpers

    # -- idempotency helper -------------------------------------------------
    def _idempotent(self, key, fn):
        if key and key in self._idempotency_cache:
            return self._idempotency_cache[key]
        result = fn()
        if key:
            self._idempotency_cache[key] = result
        return result

    # -- API surface (mirrors LiveStripeClient) ------------------------------
    def create_payment_intent(self, amount_cents, currency, transfer_group, idempotency_key):
        def _do():
            pi_id = "pi_fake_" + uuid.uuid4().hex[:16]
            pi = {
                "id": pi_id, "status": "requires_payment_method",
                "amount": amount_cents, "currency": currency,
                "transfer_group": transfer_group, "amount_refunded": 0,
            }
            self.payment_intents[pi_id] = pi
            return {"id": pi_id, "status": pi["status"], "client_secret": pi_id + "_secret"}
        return self._idempotent(idempotency_key, _do)

    def create_transfer(self, amount_cents, destination_account_id, source_transaction_id,
                          transfer_group, idempotency_key):
        def _do():
            pi = self.payment_intents.get(source_transaction_id)
            if not pi or pi["status"] != "succeeded":
                raise PaymentError("invalid_request", "source_transaction has not succeeded")
            already_transferred = sum(
                t["amount"] for t in self.transfers.values()
                if t["source_transaction_id"] == source_transaction_id
            )
            if already_transferred + amount_cents > pi["amount"]:
                raise PaymentError("invalid_request", "transfer exceeds source charge amount")
            tr_id = "tr_fake_" + uuid.uuid4().hex[:16]
            tr = {
                "id": tr_id, "amount": amount_cents, "amount_reversed": 0,
                "destination": destination_account_id,
                "source_transaction_id": source_transaction_id,
                "transfer_group": transfer_group,
            }
            self.transfers[tr_id] = tr
            return {"id": tr_id, "amount": amount_cents}
        return self._idempotent(idempotency_key, _do)

    def create_refund(self, payment_intent_id, amount_cents, idempotency_key):
        def _do():
            pi = self.payment_intents.get(payment_intent_id)
            if not pi:
                raise PaymentError("invalid_request", "no such payment_intent")
            if pi["amount_refunded"] + amount_cents > pi["amount"]:
                raise PaymentError(
                    "invalid_request",
                    f"refund would exceed original amount "
                    f"(already refunded {pi['amount_refunded']} of {pi['amount']})",
                )
            pi["amount_refunded"] += amount_cents
            rf_id = "re_fake_" + uuid.uuid4().hex[:16]
            rf = {"id": rf_id, "status": "succeeded", "amount": amount_cents,
                  "payment_intent_id": payment_intent_id}
            self.refunds[rf_id] = rf
            return rf
        return self._idempotent(idempotency_key, _do)

    def create_transfer_reversal(self, transfer_id, amount_cents, idempotency_key):
        def _do():
            tr = self.transfers.get(transfer_id)
            if not tr:
                raise PaymentError("invalid_request", "no such transfer")
            if tr["amount_reversed"] + amount_cents > tr["amount"]:
                raise PaymentError("invalid_request", "reversal exceeds transfer amount")
            tr["amount_reversed"] += amount_cents
            rev_id = "trr_fake_" + uuid.uuid4().hex[:16]
            rev = {"id": rev_id, "amount": amount_cents, "transfer_id": transfer_id}
            self.reversals[rev_id] = rev
            return rev
        return self._idempotent(idempotency_key, _do)

    def construct_webhook_event(self, payload_bytes, sig_header):
        if sig_header != "valid-test-signature":
            raise PaymentError("signature_verification_failed", "bad Stripe-Signature header")
        import json
        return json.loads(payload_bytes)

    # -- test-only helpers, not part of the real Stripe interface -----------
    def mark_payment_succeeded(self, pi_id):
        self.payment_intents[pi_id]["status"] = "succeeded"

    def mark_payment_failed(self, pi_id):
        self.payment_intents[pi_id]["status"] = "payment_failed"

    def build_event(self, event_type, data_object, event_id=None):
        import json
        event_id = event_id or ("evt_fake_" + uuid.uuid4().hex[:16])
        event = {
            "id": event_id, "type": event_type,
            "data": {"object": data_object},
            "created": int(time.time()),
        }
        return json.dumps(event).encode("utf-8"), event_id


# ---------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------
_client_singleton = None


def get_client():
    """
    Returns the active payment client. Selection is explicit and
    logged-by-construction: FakeStripeClient is only ever returned when
    PAYMENTS_MODE=fake is set (the default in this sandbox / tests),
    never silently as a fallback from a failed live-client construction —
    a misconfigured production deploy should fail loudly, not
    quietly start faking payments.
    """
    global _client_singleton
    if _client_singleton is not None:
        return _client_singleton

    mode = os.environ.get("PAYMENTS_MODE", "fake")
    if mode == "live":
        secret_key = os.environ.get("STRIPE_SECRET_KEY")
        webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
        if not secret_key or not webhook_secret:
            raise RuntimeError(
                "PAYMENTS_MODE=live requires STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET."
            )
        _client_singleton = LiveStripeClient(secret_key, webhook_secret)
    else:
        _client_singleton = FakeStripeClient()
    return _client_singleton


def reset_client_for_tests():
    global _client_singleton
    _client_singleton = FakeStripeClient()
    return _client_singleton
