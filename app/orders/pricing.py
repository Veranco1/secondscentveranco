"""
Server-side checkout pricing.

This is the direct answer to the user's explicit requirement: "Bereken
server-side: item_price / shipping_cost / buyer_protection_fee /
tax_if_applicable / total" and "Maak de fee-structuur configureerbaar
vanuit admin in plaats van hardcoded."

Nothing here ever accepts a price, fee, or total from the client. The
checkout endpoint (app/orders/routes.py) computes everything below from
the listing row plus fee_rules/platform_config and ignores any pricing
field the client submits — that is also directly what the "gemanipuleerde
frontend-prijs" test scenario verifies.

Money is integer cents throughout, never floats — same convention as
fee_rules.percentage_bps (basis points: 500 = 5.00%).
"""
from app.db import now_ts


class PricingError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _get_config_int(conn, key, default):
    row = conn.execute(
        "SELECT config_value FROM platform_config WHERE config_key = ?", (key,)
    ).fetchone()
    return int(row["config_value"]) if row else default


def get_fee_rule(conn, fee_key):
    row = conn.execute("SELECT * FROM fee_rules WHERE fee_key = ?", (fee_key,)).fetchone()
    if not row:
        raise PricingError("fee_rule_missing", f"Geen fee_rules-rij voor '{fee_key}'.")
    return row


def compute_order_totals(conn, listing_row):
    """
    Returns a dict of {item_price_cents, shipping_price_cents,
    buyer_protection_fee_cents, tax_cents, total_price_cents} for the
    given listing, computed entirely from server-side state. The caller
    (checkout endpoint) must not accept any of these values from the
    request body.
    """
    item_price_cents = listing_row["asking_price_cents"]

    shipping_price_cents = _get_config_int(conn, "shipping_flat_rate_cents", 495)
    tax_rate_bps = _get_config_int(conn, "tax_rate_bps", 0)

    fee_rule = get_fee_rule(conn, "buyer_protection")
    buyer_protection_fee_cents = (
        item_price_cents * fee_rule["percentage_bps"] // 10000
    ) + fee_rule["fixed_cents"]

    taxable_base = item_price_cents + shipping_price_cents + buyer_protection_fee_cents
    tax_cents = taxable_base * tax_rate_bps // 10000

    total_price_cents = (
        item_price_cents + shipping_price_cents + buyer_protection_fee_cents + tax_cents
    )

    return {
        "item_price_cents": item_price_cents,
        "shipping_price_cents": shipping_price_cents,
        "buyer_protection_fee_cents": buyer_protection_fee_cents,
        "tax_cents": tax_cents,
        "total_price_cents": total_price_cents,
        "currency": listing_row["currency"],
    }


def payout_amount_cents(order_row):
    """
    What the SELLER receives: item price + shipping. The platform's
    revenue is the buyer_protection_fee plus any tax collected — those
    are never transferred out. Kept as one function so this decision is
    made in exactly one place.
    """
    return order_row["item_price_cents"] + order_row["shipping_price_cents"]


def get_config_hours(conn, key, default):
    return _get_config_int(conn, key, default)
