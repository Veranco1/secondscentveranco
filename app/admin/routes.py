"""
Admin blueprint — Fase 7, buyer-protection slice only (fee/config
management, manual sweep trigger, and read access to orders/disputes).
Listing moderation, user suspension, and full risk-flag review are a
later phase (deferred with "advertenties komen later").

Every write here is audited: fee_rules changes append to
fee_rule_history (append-only, see app/db.py); nothing here bypasses
csrf_protected/login_required/admin_required.
"""
import json

from flask import Blueprint, jsonify, request, session

from app.auth.routes import login_required, admin_required, csrf_protected
from app.authenticity import verification
from app.authenticity.verification import VerificationError
from app.db import get_db, new_id, now_ts
from app.jobs.sweep import run_all_sweeps

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


# ---------------------------------------------------------------------
# Fee rules (e.g. "buyer_protection": percentage_bps + fixed_cents)
# ---------------------------------------------------------------------
@admin_bp.route("/fee-rules", methods=["GET"])
@login_required
@admin_required
def list_fee_rules():
    conn = get_db()
    rows = conn.execute("SELECT * FROM fee_rules").fetchall()
    conn.close()
    return jsonify(fee_rules=[dict(r) for r in rows])


@admin_bp.route("/fee-rules/<fee_key>", methods=["PUT"])
@csrf_protected
@login_required
@admin_required
def update_fee_rule(fee_key):
    data = request.get_json(silent=True) or {}
    percentage_bps = data.get("percentage_bps")
    fixed_cents = data.get("fixed_cents")
    if not isinstance(percentage_bps, int) or not isinstance(fixed_cents, int):
        return jsonify(error="invalid_input",
                        message="percentage_bps en fixed_cents zijn verplicht en moeten gehele getallen zijn."), 400
    if percentage_bps < 0 or percentage_bps > 10000 or fixed_cents < 0:
        return jsonify(error="invalid_input", message="Waarden buiten toegestaan bereik."), 400

    conn = get_db()
    ts = now_ts()
    admin_id = session["user_id"]
    conn.execute(
        """INSERT INTO fee_rules (fee_key, percentage_bps, fixed_cents, updated_at, updated_by)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(fee_key) DO UPDATE SET
             percentage_bps = excluded.percentage_bps,
             fixed_cents = excluded.fixed_cents,
             updated_at = excluded.updated_at,
             updated_by = excluded.updated_by""",
        (fee_key, percentage_bps, fixed_cents, ts, admin_id),
    )
    conn.execute(
        """INSERT INTO fee_rule_history (id, fee_key, percentage_bps, fixed_cents, changed_by, changed_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (new_id(), fee_key, percentage_bps, fixed_cents, admin_id, ts),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM fee_rules WHERE fee_key = ?", (fee_key,)).fetchone()
    conn.close()
    return jsonify(fee_rule=dict(row))


@admin_bp.route("/fee-rules/<fee_key>/history", methods=["GET"])
@login_required
@admin_required
def fee_rule_history(fee_key):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM fee_rule_history WHERE fee_key = ? ORDER BY changed_at DESC", (fee_key,)
    ).fetchall()
    conn.close()
    return jsonify(history=[dict(r) for r in rows])


# ---------------------------------------------------------------------
# Platform config (shipping rate, tax rate, inspection/shipping/dispute
# windows) — same "configurable, not hardcoded" requirement as fee_rules.
# ---------------------------------------------------------------------
@admin_bp.route("/config", methods=["GET"])
@login_required
@admin_required
def list_config():
    conn = get_db()
    rows = conn.execute("SELECT * FROM platform_config").fetchall()
    conn.close()
    return jsonify(config={r["config_key"]: r["config_value"] for r in rows})


@admin_bp.route("/config/<key>", methods=["PUT"])
@csrf_protected
@login_required
@admin_required
def update_config(key):
    data = request.get_json(silent=True) or {}
    value = data.get("value")
    if value is None:
        return jsonify(error="missing_value"), 400

    conn = get_db()
    conn.execute(
        """INSERT INTO platform_config (config_key, config_value, updated_at, updated_by)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(config_key) DO UPDATE SET
             config_value = excluded.config_value,
             updated_at = excluded.updated_at,
             updated_by = excluded.updated_by""",
        (key, str(value), now_ts(), session["user_id"]),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM platform_config WHERE config_key = ?", (key,)).fetchone()
    conn.close()
    return jsonify(config=dict(row))


# ---------------------------------------------------------------------
# Manual sweep trigger — dev/test substitute for a real scheduler.
# ---------------------------------------------------------------------
@admin_bp.route("/sweep", methods=["POST"])
@csrf_protected
@login_required
@admin_required
def trigger_sweep():
    result = run_all_sweeps()
    return jsonify(result)


# ---------------------------------------------------------------------
# Read access — orders/disputes needing review.
# ---------------------------------------------------------------------
@admin_bp.route("/orders", methods=["GET"])
@login_required
@admin_required
def list_orders():
    status = request.args.get("status")
    conn = get_db()
    if status:
        rows = conn.execute("SELECT * FROM orders WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 200").fetchall()
    conn.close()
    return jsonify(orders=[dict(r) for r in rows])


@admin_bp.route("/disputes", methods=["GET"])
@login_required
@admin_required
def list_disputes():
    status = request.args.get("status", "open")
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM disputes WHERE status = ? ORDER BY created_at ASC", (status,)
    ).fetchall()
    conn.close()
    return jsonify(disputes=[dict(r) for r in rows])


@admin_bp.route("/risk-flags", methods=["GET"])
@login_required
@admin_required
def list_risk_flags():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM risk_flags WHERE status = 'open' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return jsonify(risk_flags=[dict(r) for r in rows])


# =======================================================================
# Authenticity & Anti-Counterfeit — manual review dashboard (§ G)
# See docs/AUTHENTICITY_ARCHITECTURE.md. Every read here is admin-only;
# risk_signals/risk_assessments/dispute internals are never exposed on
# any buyer/seller-facing route — this is the one place they're visible.
# =======================================================================
@admin_bp.route("/listings/review-queue", methods=["GET"])
@login_required
@admin_required
def review_queue():
    conn = get_db()
    rows = conn.execute(
        """SELECT mr.*, l.brand, l.perfume_name, l.asking_price_cents, l.seller_id
           FROM manual_reviews mr JOIN listings l ON l.id = mr.listing_id
           WHERE mr.status IN ('pending', 'in_review')
           ORDER BY mr.created_at ASC"""
    ).fetchall()
    conn.close()
    return jsonify(queue=[dict(r) for r in rows])


def _seller_history(conn, seller_id):
    listing_counts = conn.execute(
        """SELECT status, COUNT(*) c FROM listings WHERE seller_id = ? GROUP BY status""",
        (seller_id,),
    ).fetchall()
    account = conn.execute("SELECT created_at FROM users WHERE id = ?", (seller_id,)).fetchone()
    prior_reports = conn.execute(
        """SELECT ar.status, COUNT(*) c FROM authenticity_reports ar
           JOIN listings l ON l.id = ar.listing_id
           WHERE l.seller_id = ? GROUP BY ar.status""",
        (seller_id,),
    ).fetchall()
    return {
        "account_created_at": account["created_at"] if account else None,
        "listing_counts_by_status": {r["status"]: r["c"] for r in listing_counts},
        "prior_authenticity_reports_by_status": {r["status"]: r["c"] for r in prior_reports},
    }


def _readable_risk_summary(signal_rows):
    if not signal_rows:
        return "Geen signalen gevonden bij de laatste beoordeling."
    labels = {
        "missing_required_photo": "verplichte foto('s) ontbreken",
        "batch_code_mismatch": "batchcode fles en verpakking komen niet overeen",
        "box_claim_inconsistent": "opgave 'doos aanwezig' klopt niet met de foto's",
        "photo_reuse_cross_account": "een foto lijkt al door een andere verkoper gebruikt",
        "photo_reuse_internal_excessive": "dezelfde foto wordt in opvallend veel eigen listings gebruikt",
        "price_far_below_reference": "vraagprijs ligt ver onder de gebruikelijke prijs",
        "new_account_high_value": "nieuw account plaatst een hoogwaardige advertentie",
        "unusual_listing_velocity": "ongebruikelijk veel identieke nieuwe advertenties kort na elkaar",
        "prior_counterfeit_report": "verkoper heeft een eerder bevestigde counterfeit-melding",
        "low_quality_evidence_photo": "een verplichte foto is te wazig",
        "possible_manipulation_signal": "mogelijke fotobewerking gedetecteerd (zwak signaal)",
        "post_verification_change": "kernvelden zijn gewijzigd na eerdere verificatie",
    }
    parts = [labels.get(r["signal_type"], r["signal_type"]) for r in signal_rows]
    return "Gevonden signalen: " + "; ".join(parts) + "."


@admin_bp.route("/listings/<listing_id>/review", methods=["GET"])
@login_required
@admin_required
def review_case(listing_id):
    conn = get_db()
    listing = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not listing:
        conn.close()
        return jsonify(error="not_found"), 404

    photos = conn.execute(
        "SELECT * FROM listing_photos WHERE listing_id = ? ORDER BY category", (listing_id,)
    ).fetchall()
    latest_assessment = conn.execute(
        "SELECT * FROM risk_assessments WHERE listing_id = ? ORDER BY computed_at DESC LIMIT 1",
        (listing_id,),
    ).fetchone()
    signal_rows = []
    if latest_assessment:
        signal_ids = json.loads(latest_assessment["signal_ids"])
        if signal_ids:
            placeholders = ",".join("?" for _ in signal_ids)
            signal_rows = conn.execute(
                f"SELECT * FROM risk_signals WHERE id IN ({placeholders})", signal_ids,
            ).fetchall()

    similar = conn.execute(
        """SELECT id, seller_id, asking_price_cents, status, verification_status FROM listings
           WHERE brand = ? AND perfume_name = ? AND id != ? LIMIT 20""",
        (listing["brand"], listing["perfume_name"], listing_id),
    ).fetchall()
    review = conn.execute(
        "SELECT * FROM manual_reviews WHERE listing_id = ? ORDER BY created_at DESC LIMIT 1",
        (listing_id,),
    ).fetchone()
    history = conn.execute(
        "SELECT * FROM listing_verifications WHERE listing_id = ? ORDER BY created_at ASC", (listing_id,)
    ).fetchall()
    standard = verification.meets_verified_standard(conn, listing_id)
    seller_history = _seller_history(conn, listing["seller_id"])
    conn.close()

    return jsonify(
        listing=dict(listing),
        photos=[dict(p) for p in photos],
        risk_assessment=dict(latest_assessment) if latest_assessment else None,
        risk_signals=[dict(s) for s in signal_rows],
        risk_summary=_readable_risk_summary(signal_rows),
        seller_history=seller_history,
        similar_listings=[dict(r) for r in similar],
        review=dict(review) if review else None,
        verification_history=[dict(h) for h in history],
        meets_verified_standard=standard,
    )


@admin_bp.route("/listings/<listing_id>/review", methods=["POST"])
@csrf_protected
@login_required
@admin_required
def act_on_review(listing_id):
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    notes = data.get("notes")
    if action not in ("approve", "request_more_evidence", "reject", "escalate"):
        return jsonify(error="invalid_action",
                        allowed=["approve", "request_more_evidence", "reject", "escalate"]), 400

    conn = get_db()
    review = conn.execute(
        "SELECT * FROM manual_reviews WHERE listing_id = ? AND status IN ('pending','in_review') "
        "ORDER BY created_at DESC LIMIT 1",
        (listing_id,),
    ).fetchone()
    if not review:
        conn.close()
        return jsonify(error="no_open_review",
                        message="Er is geen openstaande review voor deze advertentie."), 404

    admin_id = session["user_id"]
    try:
        if action == "approve":
            result = verification.approve(conn, review["id"], admin_id, notes)
        elif action == "request_more_evidence":
            result = verification.request_more_evidence(conn, review["id"], admin_id, notes)
        elif action == "reject":
            result = verification.reject(conn, review["id"], admin_id, notes)
        else:
            result = verification.escalate(conn, review["id"], admin_id, notes)
    except VerificationError as exc:
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 409

    conn.commit()
    conn.close()
    return jsonify(result)


# =======================================================================
# Admin counterfeit intelligence dashboard (§ J) — aggregated patterns,
# minimal PII (id + email only where actionable), never raw evidence
# content beyond what the dedicated, authorized evidence endpoints serve.
# =======================================================================
@admin_bp.route("/intelligence/signal-summary", methods=["GET"])
@login_required
@admin_required
def intelligence_signal_summary():
    conn = get_db()
    since = now_ts() - 30 * 86400
    rows = conn.execute(
        """SELECT signal_type, COUNT(*) c, SUM(weight) total_weight FROM risk_signals
           WHERE detected_at >= ? GROUP BY signal_type ORDER BY c DESC""",
        (since,),
    ).fetchall()
    conn.close()
    return jsonify(window_days=30, signals=[dict(r) for r in rows])


@admin_bp.route("/intelligence/flagged-sellers", methods=["GET"])
@login_required
@admin_required
def intelligence_flagged_sellers():
    conn = get_db()
    rows = conn.execute(
        """SELECT u.id AS seller_id, u.email, SUM(rs.weight) AS total_weight, COUNT(DISTINCT rs.listing_id) AS listing_count
           FROM risk_signals rs
           JOIN listings l ON l.id = rs.listing_id
           JOIN users u ON u.id = l.seller_id
           GROUP BY u.id, u.email
           ORDER BY total_weight DESC
           LIMIT 50"""
    ).fetchall()
    conn.close()
    return jsonify(sellers=[dict(r) for r in rows])


# =======================================================================
# Counterfeit report review (§ H) — side-by-side of what the buyer
# reported vs. the original listing's evidence. Purely a structured
# juxtaposition, never an automatic match/no-match verdict (see
# docs/AUTHENTICITY_ARCHITECTURE.md § H for why).
# =======================================================================
_COUNTERFEIT_TO_LISTING_CATEGORIES = {
    "bottle_photo": ["bottle_front", "bottle_back"],
    "bottle_bottom_photo": ["bottle_bottom"],
    "nozzle_photo": ["nozzle"],
    "batch_code_photo": ["batch_code_bottle", "batch_code_packaging"],
    "box_photo": ["box_front", "box_back", "box_bottom"],
    "packaging_photo": ["box_front", "box_back", "box_bottom"],
    "discrepancy_description": [],
}


@admin_bp.route("/authenticity-reports/<report_id>", methods=["GET"])
@login_required
@admin_required
def get_authenticity_report(report_id):
    conn = get_db()
    report = conn.execute("SELECT * FROM authenticity_reports WHERE id = ?", (report_id,)).fetchone()
    if not report:
        conn.close()
        return jsonify(error="not_found"), 404

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
        listing_categories = _COUNTERFEIT_TO_LISTING_CATEGORIES.get(buyer_photo["evidence_type"], [])
        original_photos = []
        for cat in listing_categories:
            original_photos.extend(listing_photos_by_category.get(cat, []))
        comparison.append({
            "buyer_evidence": dict(buyer_photo),
            "original_listing_photos": original_photos,
        })

    seller_history = _seller_history(conn, dispute["opened_by"]) if dispute else None
    listing = conn.execute("SELECT * FROM listings WHERE id = ?", (report["listing_id"],)).fetchone()
    conn.close()

    return jsonify(
        report=dict(report),
        dispute=dict(dispute) if dispute else None,
        listing=dict(listing) if listing else None,
        comparison=comparison,
    )


@admin_bp.route("/authenticity-reports/<report_id>", methods=["PATCH"])
@csrf_protected
@login_required
@admin_required
def update_authenticity_report(report_id):
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    notes = data.get("notes")
    if status not in ("reviewed", "confirmed_counterfeit", "not_counterfeit"):
        return jsonify(error="invalid_status",
                        allowed=["reviewed", "confirmed_counterfeit", "not_counterfeit"]), 400

    conn = get_db()
    report = conn.execute("SELECT * FROM authenticity_reports WHERE id = ?", (report_id,)).fetchone()
    if not report:
        conn.close()
        return jsonify(error="not_found"), 404

    conn.execute(
        "UPDATE authenticity_reports SET status = ?, reviewed_by = ?, reviewed_at = ?, outcome_notes = ? WHERE id = ?",
        (status, session["user_id"], now_ts(), notes, report_id),
    )
    conn.execute(
        """INSERT INTO audit_logs (id, actor_type, actor_id, action, entity_type, entity_id, metadata, created_at)
           VALUES (?, 'admin', ?, ?, 'authenticity_report', ?, ?, ?)""",
        (new_id(), session["user_id"], f"authenticity_report:{status}", report_id,
         json.dumps({"notes": notes}), now_ts()),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM authenticity_reports WHERE id = ?", (report_id,)).fetchone()
    conn.close()
    return jsonify(report=dict(updated))


@admin_bp.route("/intelligence/photo-reuse-clusters", methods=["GET"])
@login_required
@admin_required
def intelligence_photo_reuse_clusters():
    conn = get_db()
    # GROUP_CONCAT is SQLite-specific (this dev layer only) — the Postgres
    # port of this query uses string_agg(DISTINCT u.email, ',') instead.
    rows = conn.execute(
        """SELECT lp.sha256_hash, COUNT(DISTINCT l.seller_id) AS distinct_sellers,
                  COUNT(DISTINCT lp.listing_id) AS distinct_listings,
                  GROUP_CONCAT(DISTINCT u.email) AS seller_emails
           FROM listing_photos lp
           JOIN listings l ON l.id = lp.listing_id
           JOIN users u ON u.id = l.seller_id
           GROUP BY lp.sha256_hash
           HAVING COUNT(DISTINCT l.seller_id) > 1
           ORDER BY distinct_sellers DESC
           LIMIT 50"""
    ).fetchall()
    conn.close()
    clusters = []
    for r in rows:
        clusters.append({
            "sha256_hash": r["sha256_hash"],
            "distinct_sellers": r["distinct_sellers"],
            "distinct_listings": r["distinct_listings"],
            "seller_emails": (r["seller_emails"] or "").split(","),
        })
    return jsonify(clusters=clusters)
