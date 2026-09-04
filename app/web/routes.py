"""
Web blueprint — server-rendered Jinja2 pages for the real, working
SecondScent website.

This blueprint never writes to the database itself for anything that
changes state (an order, a listing, a dispute, an account). Every
state-changing action on these pages is a browser-side fetch() call
(app/static/js/app.js) straight to the already-built and tested JSON
API blueprints (app.auth, app.listings, app.orders, app.disputes,
app.admin), using the exact same X-CSRF-Token convention those
blueprints already require. This file only ever *reads* the database,
to render a page — so nothing here can drift from the behaviour those
JSON endpoints already have 166 passing tests for.

Scope decision, stated plainly: the mockup (secondscent.html) this
reuses the visual identity from had marketing copy in five languages
via a client-side i18n script. The actual product is described
throughout this project as a *Dutch-language* marketplace, and every
user-facing string already written on the backend (STATUS_EXPLANATIONS,
error messages, form labels) is Dutch-only. Porting five-language
support to a server-rendered site backed by a real database (translated
listings? translated dispute threads?) is a materially different and
much larger feature than "reuse the mockup's look" — so this website is
built Dutch-only, matching the backend. This is a scope decision made
during this build, not a limitation discovered afterward; see README.md.
"""
import os

from flask import Blueprint, render_template, redirect, url_for, request, session, flash, abort

from app.db import get_db
from app.auth.routes import _public_user
from app.listings.routes import (
    _public_listing, _listing_or_404, _is_admin as _listing_is_admin,
    CATEGORY_META, CONDITION_LABELS, UPLOAD_INSTRUCTIONS,
)
from app.authenticity.verification import STATUS_EXPLANATIONS

web_bp = Blueprint("web", __name__, template_folder="../templates")


# ---------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------
def _current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if not row:
        return None
    user = _public_user(row)
    user["is_admin"] = bool(row["is_admin"])
    return user


@web_bp.app_context_processor
def inject_current_user():
    return {"current_user": _current_user()}


def _require_login():
    user = _current_user()
    if not user:
        flash("Log in om verder te gaan.", "info")
        return None
    return user


def _require_admin():
    user = _require_login()
    if user and not user["is_admin"]:
        abort(403)
    return user


PAYMENTS_ARE_FAKE = os.environ.get("PAYMENTS_MODE", "fake") != "live"


# ---------------------------------------------------------------------
# Home / browse
# ---------------------------------------------------------------------
@web_bp.route("/taal/<lang>")
def set_language(lang):
    from app.i18n import SUPPORTED_LANGUAGES
    if lang in SUPPORTED_LANGUAGES:
        session["lang"] = lang
    dest = request.referrer
    if not dest or request.host not in dest:
        dest = url_for("web.index")
    return redirect(dest)


@web_bp.route("/")
def index():
    conn = get_db()
    clauses = ["status = 'active'"]
    params = []
    q = (request.args.get("q") or "").strip()
    if q:
        clauses.append("(brand LIKE ? OR perfume_name LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    condition = (request.args.get("condition") or "").strip()
    if condition:
        clauses.append("condition = ?")
        params.append(condition)
    if request.args.get("tradeable") == "1":
        clauses.append("tradeable = 1")
    sql = f"SELECT * FROM listings WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT 60"
    rows = conn.execute(sql, params).fetchall()
    listings = [_public_listing(r, False) for r in rows]
    for listing, row in zip(listings, rows):
        photo = conn.execute(
            "SELECT id FROM listing_photos WHERE listing_id = ? AND category = 'bottle_front' "
            "ORDER BY created_at DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        listing["cover_photo_id"] = photo["id"] if photo else None
    conn.close()
    return render_template(
        "index.html", listings=listings, q=q, condition=condition,
        condition_labels=CONDITION_LABELS,
    )


@web_bp.route("/parfum/<listing_id>")
def listing_detail(listing_id):
    conn = get_db()
    listing = _listing_or_404(conn, listing_id)
    if not listing:
        conn.close()
        abort(404)
    user_id = session.get("user_id")
    is_owner_or_admin = bool(user_id) and (
        listing["seller_id"] == user_id or _listing_is_admin(conn, user_id)
    )
    if listing["status"] != "active" and not is_owner_or_admin:
        conn.close()
        abort(404)
    photos = conn.execute(
        "SELECT * FROM listing_photos WHERE listing_id = ? ORDER BY category", (listing_id,)
    ).fetchall()
    seller = conn.execute("SELECT * FROM users WHERE id = ?", (listing["seller_id"],)).fetchone()
    conn.close()

    visible_categories = [c for c in CATEGORY_META if c != "proof_of_purchase"]
    photos_by_cat = {p["category"]: p for p in photos if p["category"] in visible_categories}

    return render_template(
        "listing_detail.html",
        listing=_public_listing(listing, is_owner_or_admin),
        raw_listing=listing,
        photos_by_cat=photos_by_cat,
        category_meta=CATEGORY_META,
        condition_labels=CONDITION_LABELS,
        seller=seller,
        is_owner=bool(user_id) and listing["seller_id"] == user_id,
        can_buy=bool(user_id) and listing["seller_id"] != user_id and listing["status"] == "active",
    )


# ---------------------------------------------------------------------
# Auth pages
# ---------------------------------------------------------------------
@web_bp.route("/registreren")
def register():
    if _current_user():
        return redirect(url_for("web.index"))
    return render_template("register.html", next=request.args.get("next", ""))


@web_bp.route("/inloggen")
def login():
    if _current_user():
        return redirect(url_for("web.index"))
    return render_template("login.html", next=request.args.get("next", ""))


# ---------------------------------------------------------------------
# Sell flow
# ---------------------------------------------------------------------
@web_bp.route("/verkopen")
def sell_new():
    user = _current_user()
    if not user:
        return render_template("sell_new.html", need_login=True, need_seller=False)
    if not user["is_seller"]:
        return render_template("sell_new.html", need_login=False, need_seller=True)
    return render_template(
        "sell_new.html", need_login=False, need_seller=False,
        condition_labels=CONDITION_LABELS,
    )


@web_bp.route("/verkopen/<listing_id>")
def sell_manage(listing_id):
    user = _require_login()
    if not user:
        return redirect(url_for("web.login", next=request.path))
    conn = get_db()
    listing = _listing_or_404(conn, listing_id)
    if not listing or listing["seller_id"] != user["id"]:
        conn.close()
        abort(404)
    photos = conn.execute(
        "SELECT * FROM listing_photos WHERE listing_id = ? ORDER BY category", (listing_id,)
    ).fetchall()
    conn.close()
    photos_by_cat = {p["category"]: dict(p) for p in photos}
    can_upload = listing["status"] == "draft" or listing["verification_status"] == "additional_verification_required"
    return render_template(
        "sell_manage.html",
        listing=_public_listing(listing, True),
        category_meta=CATEGORY_META,
        upload_instructions=UPLOAD_INSTRUCTIONS,
        photos_by_cat=photos_by_cat,
        can_upload=can_upload,
        status_explanation=STATUS_EXPLANATIONS.get(listing["verification_status"], ""),
    )


# ---------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------
@web_bp.route("/afrekenen/<listing_id>")
def checkout(listing_id):
    user = _require_login()
    if not user:
        return redirect(url_for("web.login", next=request.path))
    conn = get_db()
    listing = _listing_or_404(conn, listing_id)
    if not listing or listing["status"] != "active":
        conn.close()
        abort(404)
    if listing["seller_id"] == user["id"]:
        conn.close()
        flash("Je kunt je eigen advertentie niet kopen.", "error")
        return redirect(url_for("web.listing_detail", listing_id=listing_id))

    from app.orders.pricing import compute_order_totals
    totals = compute_order_totals(conn, listing)
    conn.close()
    return render_template(
        "checkout.html", listing=_public_listing(listing, False), totals=totals,
        payments_are_fake=PAYMENTS_ARE_FAKE,
    )


@web_bp.route("/bestelling/<order_id>/afgerond")
def order_placed(order_id):
    user = _require_login()
    if not user:
        return redirect(url_for("web.login"))
    return redirect(url_for("web.account_order_detail", order_id=order_id))


# ---------------------------------------------------------------------
# Buyer account
# ---------------------------------------------------------------------
@web_bp.route("/account/aankopen")
def account_orders():
    user = _require_login()
    if not user:
        return redirect(url_for("web.login", next=request.path))
    conn = get_db()
    rows = conn.execute(
        "SELECT o.*, l.brand, l.perfume_name FROM orders o JOIN listings l ON l.id = o.listing_id "
        "WHERE o.buyer_id = ? ORDER BY o.created_at DESC", (user["id"],)
    ).fetchall()
    conn.close()
    return render_template("account_orders.html", orders=[dict(r) for r in rows])


def _order_detail_context(order_id, user):
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return None
    is_buyer = order["buyer_id"] == user["id"]
    is_seller = order["seller_id"] == user["id"]
    if not (is_buyer or is_seller or user["is_admin"]):
        conn.close()
        return None
    listing = conn.execute("SELECT * FROM listings WHERE id = ?", (order["listing_id"],)).fetchone()
    history = conn.execute(
        "SELECT * FROM order_status_history WHERE order_id = ? ORDER BY created_at ASC", (order_id,)
    ).fetchall()
    dispute = conn.execute(
        "SELECT * FROM disputes WHERE order_id = ? ORDER BY created_at DESC LIMIT 1", (order_id,)
    ).fetchone()
    other_party_id = order["seller_id"] if is_buyer else order["buyer_id"]
    other_party = conn.execute("SELECT display_name, email FROM users WHERE id = ?", (other_party_id,)).fetchone()
    conn.close()
    return {
        "order": dict(order), "listing": dict(listing) if listing else None,
        "history": [dict(h) for h in history],
        "dispute": dict(dispute) if dispute else None,
        "is_buyer": is_buyer, "is_seller": is_seller, "is_admin": user["is_admin"],
        "other_party": dict(other_party) if other_party else None,
        "payments_are_fake": PAYMENTS_ARE_FAKE,
    }


@web_bp.route("/account/aankopen/<order_id>")
def account_order_detail(order_id):
    user = _require_login()
    if not user:
        return redirect(url_for("web.login", next=request.path))
    ctx = _order_detail_context(order_id, user)
    if not ctx:
        abort(404)
    return render_template("order_detail.html", **ctx)


# ---------------------------------------------------------------------
# Disputes (shared buyer / seller / admin view)
# ---------------------------------------------------------------------
@web_bp.route("/geschillen/<dispute_id>")
def dispute_detail(dispute_id):
    user = _require_login()
    if not user:
        return redirect(url_for("web.login", next=request.path))
    conn = get_db()
    dispute = conn.execute("SELECT * FROM disputes WHERE id = ?", (dispute_id,)).fetchone()
    if not dispute:
        conn.close()
        abort(404)
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (dispute["order_id"],)).fetchone()
    is_buyer = order["buyer_id"] == user["id"]
    is_seller = order["seller_id"] == user["id"]
    if not (is_buyer or is_seller or user["is_admin"]):
        conn.close()
        abort(404)
    listing = conn.execute("SELECT * FROM listings WHERE id = ?", (order["listing_id"],)).fetchone()
    evidence = conn.execute(
        "SELECT * FROM dispute_evidence WHERE dispute_id = ? ORDER BY created_at ASC", (dispute_id,)
    ).fetchall()
    messages = conn.execute(
        "SELECT de.*, u.display_name FROM dispute_messages de JOIN users u ON u.id = de.sender_id "
        "WHERE dispute_id = ? ORDER BY de.created_at ASC", (dispute_id,)
    ).fetchall()
    admin_notes = []
    report = None
    if user["is_admin"]:
        admin_notes = conn.execute(
            "SELECT * FROM dispute_admin_notes WHERE dispute_id = ? ORDER BY created_at ASC", (dispute_id,)
        ).fetchall()
        report = conn.execute(
            "SELECT * FROM authenticity_reports WHERE dispute_id = ?", (dispute_id,)
        ).fetchone()
    conn.close()
    return render_template(
        "dispute_detail.html",
        dispute=dict(dispute), order=dict(order), listing=dict(listing) if listing else None,
        evidence=[dict(e) for e in evidence], messages=[dict(m) for m in messages],
        admin_notes=[dict(n) for n in admin_notes],
        report=dict(report) if report else None,
        is_buyer=is_buyer, is_seller=is_seller, is_admin=user["is_admin"],
    )


# ---------------------------------------------------------------------
# Seller dashboard
# ---------------------------------------------------------------------
@web_bp.route("/verkopers/dashboard")
def seller_dashboard():
    user = _require_login()
    if not user:
        return redirect(url_for("web.login", next=request.path))
    conn = get_db()
    listings = conn.execute(
        "SELECT * FROM listings WHERE seller_id = ? ORDER BY created_at DESC", (user["id"],)
    ).fetchall()
    orders = conn.execute(
        "SELECT o.*, l.brand, l.perfume_name FROM orders o JOIN listings l ON l.id = o.listing_id "
        "WHERE o.seller_id = ? ORDER BY o.created_at DESC", (user["id"],)
    ).fetchall()
    connect = conn.execute(
        "SELECT * FROM stripe_connect_accounts WHERE user_id = ?", (user["id"],)
    ).fetchone()
    conn.close()
    return render_template(
        "seller_dashboard.html",
        listings=[_public_listing(r, True) for r in listings],
        orders=[dict(r) for r in orders],
        is_seller=user["is_seller"],
        payouts_enabled=bool(connect and connect["payouts_enabled"]),
        payments_are_fake=PAYMENTS_ARE_FAKE,
    )


# ---------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------
@web_bp.route("/beheer")
def admin_queue():
    user = _require_admin()
    if not user:
        return redirect(url_for("web.login", next=request.path))
    conn = get_db()
    rows = conn.execute(
        """SELECT mr.*, l.brand, l.perfume_name, l.asking_price_cents, l.seller_id, l.risk_band, l.risk_score
           FROM manual_reviews mr JOIN listings l ON l.id = mr.listing_id
           WHERE mr.status IN ('pending', 'in_review')
           ORDER BY mr.created_at ASC"""
    ).fetchall()
    open_disputes = conn.execute(
        "SELECT COUNT(*) c FROM disputes WHERE status = 'open'"
    ).fetchone()["c"]
    conn.close()
    return render_template("admin_queue.html", queue=[dict(r) for r in rows], open_disputes=open_disputes)


@web_bp.route("/beheer/advertenties/<listing_id>")
def admin_review_case(listing_id):
    user = _require_admin()
    if not user:
        return redirect(url_for("web.login", next=request.path))
    from app.admin.routes import _seller_history, _readable_risk_summary
    from app.authenticity import verification
    import json as _json

    conn = get_db()
    listing = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not listing:
        conn.close()
        abort(404)
    photos = conn.execute(
        "SELECT * FROM listing_photos WHERE listing_id = ? ORDER BY category", (listing_id,)
    ).fetchall()
    latest_assessment = conn.execute(
        "SELECT * FROM risk_assessments WHERE listing_id = ? ORDER BY computed_at DESC LIMIT 1", (listing_id,)
    ).fetchone()
    signal_rows = []
    if latest_assessment:
        signal_ids = _json.loads(latest_assessment["signal_ids"])
        if signal_ids:
            placeholders = ",".join("?" for _ in signal_ids)
            signal_rows = conn.execute(
                f"SELECT * FROM risk_signals WHERE id IN ({placeholders})", signal_ids
            ).fetchall()
    similar = conn.execute(
        """SELECT id, seller_id, asking_price_cents, status, verification_status FROM listings
           WHERE brand = ? AND perfume_name = ? AND id != ? LIMIT 20""",
        (listing["brand"], listing["perfume_name"], listing_id),
    ).fetchall()
    review = conn.execute(
        "SELECT * FROM manual_reviews WHERE listing_id = ? ORDER BY created_at DESC LIMIT 1", (listing_id,)
    ).fetchone()
    history = conn.execute(
        "SELECT * FROM listing_verifications WHERE listing_id = ? ORDER BY created_at ASC", (listing_id,)
    ).fetchall()
    standard = verification.meets_verified_standard(conn, listing_id)
    seller_history = _seller_history(conn, listing["seller_id"])
    seller = conn.execute("SELECT * FROM users WHERE id = ?", (listing["seller_id"],)).fetchone()
    conn.close()

    return render_template(
        "admin_review_case.html",
        listing=dict(listing), photos=[dict(p) for p in photos],
        category_meta=CATEGORY_META,
        risk_assessment=dict(latest_assessment) if latest_assessment else None,
        risk_signals=[dict(s) for s in signal_rows],
        risk_summary=_readable_risk_summary(signal_rows),
        seller_history=seller_history, seller=dict(seller) if seller else None,
        similar_listings=[dict(r) for r in similar],
        review=dict(review) if review else None,
        verification_history=[dict(h) for h in history],
        meets_verified_standard=standard,
        has_open_review=bool(review and review["status"] in ("pending", "in_review")),
    )


@web_bp.route("/beheer/geschillen")
def admin_disputes():
    user = _require_admin()
    if not user:
        return redirect(url_for("web.login", next=request.path))
    conn = get_db()
    status = request.args.get("status", "open")
    rows = conn.execute(
        "SELECT d.*, o.buyer_id, o.seller_id, o.total_price_cents FROM disputes d "
        "JOIN orders o ON o.id = d.order_id WHERE d.status = ? ORDER BY d.created_at ASC", (status,)
    ).fetchall()
    conn.close()
    return render_template("admin_disputes.html", disputes=[dict(r) for r in rows], status=status)


@web_bp.route("/beheer/meldingen/<report_id>")
def admin_report_detail(report_id):
    user = _require_admin()
    if not user:
        return redirect(url_for("web.login", next=request.path))
    from app.admin.routes import _COUNTERFEIT_TO_LISTING_CATEGORIES, _seller_history

    conn = get_db()
    report = conn.execute("SELECT * FROM authenticity_reports WHERE id = ?", (report_id,)).fetchone()
    if not report:
        conn.close()
        abort(404)
    dispute = conn.execute("SELECT * FROM disputes WHERE id = ?", (report["dispute_id"],)).fetchone()
    buyer_evidence = conn.execute(
        "SELECT * FROM dispute_evidence WHERE dispute_id = ? ORDER BY created_at ASC", (report["dispute_id"],)
    ).fetchall()
    listing_photos = conn.execute(
        "SELECT * FROM listing_photos WHERE listing_id = ? ORDER BY category", (report["listing_id"],)
    ).fetchall()
    listing_photos_by_category = {}
    for p in listing_photos:
        listing_photos_by_category.setdefault(p["category"], []).append(dict(p))
    comparison = []
    for buyer_photo in buyer_evidence:
        cats = _COUNTERFEIT_TO_LISTING_CATEGORIES.get(buyer_photo["evidence_type"], [])
        originals = []
        for cat in cats:
            originals.extend(listing_photos_by_category.get(cat, []))
        comparison.append({"buyer_evidence": dict(buyer_photo), "original_listing_photos": originals})
    listing = conn.execute("SELECT * FROM listings WHERE id = ?", (report["listing_id"],)).fetchone()
    seller_history = _seller_history(conn, listing["seller_id"]) if listing else None
    conn.close()
    return render_template(
        "admin_report_detail.html",
        report=dict(report), dispute=dict(dispute) if dispute else None,
        listing=dict(listing) if listing else None, comparison=comparison,
        seller_history=seller_history,
    )


@web_bp.route("/beheer/intelligence")
def admin_intelligence():
    user = _require_admin()
    if not user:
        return redirect(url_for("web.login", next=request.path))
    conn = get_db()
    from app.db import now_ts
    since = now_ts() - 30 * 86400
    signals = conn.execute(
        """SELECT signal_type, COUNT(*) c, SUM(weight) total_weight FROM risk_signals
           WHERE detected_at >= ? GROUP BY signal_type ORDER BY c DESC""", (since,)
    ).fetchall()
    sellers = conn.execute(
        """SELECT u.id AS seller_id, u.email, SUM(rs.weight) AS total_weight, COUNT(DISTINCT rs.listing_id) AS listing_count
           FROM risk_signals rs JOIN listings l ON l.id = rs.listing_id JOIN users u ON u.id = l.seller_id
           GROUP BY u.id, u.email ORDER BY total_weight DESC LIMIT 50"""
    ).fetchall()
    clusters = conn.execute(
        """SELECT lp.sha256_hash, COUNT(DISTINCT l.seller_id) AS distinct_sellers,
                  COUNT(DISTINCT lp.listing_id) AS distinct_listings,
                  GROUP_CONCAT(DISTINCT u.email) AS seller_emails
           FROM listing_photos lp JOIN listings l ON l.id = lp.listing_id JOIN users u ON u.id = l.seller_id
           GROUP BY lp.sha256_hash HAVING COUNT(DISTINCT l.seller_id) > 1
           ORDER BY distinct_sellers DESC LIMIT 50"""
    ).fetchall()
    conn.close()
    return render_template(
        "admin_intelligence.html",
        signals=[dict(r) for r in signals],
        sellers=[dict(r) for r in sellers],
        clusters=[{**dict(r), "seller_emails": (r["seller_emails"] or "").split(",")} for r in clusters],
    )


@web_bp.route("/beheer/instellingen")
def admin_config():
    user = _require_admin()
    if not user:
        return redirect(url_for("web.login", next=request.path))
    conn = get_db()
    fee_rules = conn.execute("SELECT * FROM fee_rules").fetchall()
    config_rows = conn.execute("SELECT * FROM platform_config").fetchall()
    conn.close()
    return render_template(
        "admin_config.html",
        fee_rules=[dict(r) for r in fee_rules],
        config={r["config_key"]: r["config_value"] for r in config_rows},
    )
