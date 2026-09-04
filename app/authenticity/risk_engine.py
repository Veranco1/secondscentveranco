"""
Risk engine — rule-based, explainable, and never exposed to sellers or
buyers. See docs/AUTHENTICITY_ARCHITECTURE.md § E for the full table of
signals and the reasoning behind each one (and, just as importantly, for
what was deliberately NOT built and why).

Every rule below is a plain function: (conn, listing, photos_by_category)
-> list of signal dicts. assess_listing() runs all of them, writes the
resulting risk_signals + one risk_assessments row, and returns the
assessment — it does NOT touch listings.verification_status itself; that
decision belongs to app/authenticity/verification.py alone (keeps "what
raises risk" and "what a status means" independently testable and
independently changeable).

Weights and thresholds are constants here, in one place — same pattern as
fee_rules being the single source for the buyer-protection fee, just not
yet admin-editable (a natural next step, noted in the architecture doc).
"""
import json

from app.authenticity.photo_integrity import hamming_distance
from app.db import new_id, now_ts

ENGINE_VERSION = 1

REQUIRED_ALWAYS = {"bottle_front", "bottle_back", "bottle_bottom", "nozzle", "cap", "batch_code_bottle"}
REQUIRED_IF_BOX = {"box_front", "box_back", "box_bottom", "batch_code_packaging"}
OPTIONAL_CATEGORIES = {"barcode", "proof_of_purchase"}
ALL_CATEGORIES = REQUIRED_ALWAYS | REQUIRED_IF_BOX | OPTIONAL_CATEGORIES

# A near-duplicate re-encode/crop of the SAME photo lands at hamming
# distance 0 in practice; genuinely different photos of a similar-looking
# product land well above 10 (empirically verified in
# tests/test_authenticity.py against real generated images — see that
# file for the actual numbers). 10 leaves comfortable margin on both sides.
REUSE_HAMMING_THRESHOLD = 10

WEIGHTS = {
    "missing_required_photo": 15,
    "batch_code_mismatch": 20,
    "box_claim_inconsistent": 15,
    "photo_reuse_cross_account": 40,
    "photo_reuse_internal_excessive": 20,
    "price_far_below_reference": 25,
    "new_account_high_value": 15,
    "unusual_listing_velocity": 20,
    "prior_counterfeit_report": 35,
    "low_quality_evidence_photo": 10,
    "possible_manipulation_signal": 10,
}

BAND_THRESHOLDS = (20, 50)  # score < 20 -> low, < 50 -> medium, else high

NEW_ACCOUNT_DAYS = 14
NEW_ACCOUNT_PRICE_CENTS = 20000
VELOCITY_WINDOW_HOURS = 48
VELOCITY_COUNT_THRESHOLD = 5
INTERNAL_REUSE_LISTING_THRESHOLD = 3
PRICE_FLOOR_RATIO = 0.4


def required_categories(listing):
    cats = set(REQUIRED_ALWAYS)
    if listing["box_included"]:
        cats |= REQUIRED_IF_BOX
    return cats


def _signal(signal_type, details=None):
    return {
        "signal_type": signal_type,
        "weight": WEIGHTS[signal_type],
        "detector": f"rule:{signal_type}:v{ENGINE_VERSION}",
        "details": details or {},
    }


# ---------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------
def rule_missing_required_photo(conn, listing, photos_by_category):
    missing = required_categories(listing) - set(photos_by_category)
    if not missing:
        return []
    return [_signal("missing_required_photo", {"missing_categories": sorted(missing)})]


def rule_batch_code_mismatch(conn, listing, photos_by_category):
    bottle = photos_by_category.get("batch_code_bottle")
    packaging = photos_by_category.get("batch_code_packaging")
    if not bottle or not packaging:
        return []
    code_a = (bottle["seller_entered_code"] or "").strip().upper()
    code_b = (packaging["seller_entered_code"] or "").strip().upper()
    if not code_a or not code_b:
        return []
    if code_a != code_b:
        return [_signal("batch_code_mismatch", {"bottle_code": code_a, "packaging_code": code_b})]
    return []


def rule_box_claim_inconsistent(conn, listing, photos_by_category):
    has_box_photos = any(c in photos_by_category for c in ("box_front", "box_back", "box_bottom"))
    if listing["box_included"] and not has_box_photos:
        return [_signal("box_claim_inconsistent", {"reason": "box_included maar geen doosfoto's"})]
    if not listing["box_included"] and has_box_photos:
        return [_signal("box_claim_inconsistent", {"reason": "doosfoto's aanwezig maar box_included is false"})]
    return []


def rule_photo_reuse_cross_account(conn, listing, photos_by_category):
    signals = []
    seen_other_listings = set()
    for category, photo in photos_by_category.items():
        candidates = conn.execute(
            """SELECT lp.id, lp.listing_id, lp.sha256_hash, lp.phash, lp.created_at, l.seller_id
               FROM listing_photos lp JOIN listings l ON l.id = lp.listing_id
               WHERE lp.listing_id != ? AND (lp.sha256_hash = ? OR lp.phash IS NOT NULL)""",
            (listing["id"], photo["sha256_hash"]),
        ).fetchall()
        for cand in candidates:
            if cand["seller_id"] == listing["seller_id"]:
                continue
            if cand["created_at"] > photo["created_at"]:
                continue  # the other photo came later — this one is the original
            is_exact = cand["sha256_hash"] == photo["sha256_hash"]
            dist = None if is_exact else hamming_distance(cand["phash"], photo["phash"])
            if is_exact or (dist is not None and dist <= REUSE_HAMMING_THRESHOLD):
                key = (cand["listing_id"], category)
                if key in seen_other_listings:
                    continue
                seen_other_listings.add(key)
                signals.append(_signal("photo_reuse_cross_account", {
                    "category": category, "matched_listing_id": cand["listing_id"],
                    "exact_match": is_exact, "hamming_distance": dist,
                }))
    return signals


def rule_photo_reuse_internal_excessive(conn, listing, photos_by_category):
    signals = []
    for category, photo in photos_by_category.items():
        rows = conn.execute(
            """SELECT DISTINCT lp.listing_id FROM listing_photos lp
               JOIN listings l ON l.id = lp.listing_id
               WHERE l.seller_id = ? AND lp.sha256_hash = ?""",
            (listing["seller_id"], photo["sha256_hash"]),
        ).fetchall()
        distinct_listings = {r["listing_id"] for r in rows} | {listing["id"]}
        if len(distinct_listings) >= INTERNAL_REUSE_LISTING_THRESHOLD:
            signals.append(_signal("photo_reuse_internal_excessive", {
                "category": category, "distinct_listing_count": len(distinct_listings),
            }))
    return signals


def rule_price_far_below_reference(conn, listing, photos_by_category):
    ref = conn.execute(
        "SELECT typical_price_cents FROM reference_prices WHERE brand = ? AND perfume_name = ? AND size_ml = ?",
        (listing["brand"], listing["perfume_name"], listing["size_ml"]),
    ).fetchone()
    if not ref:
        return []
    floor = ref["typical_price_cents"] * PRICE_FLOOR_RATIO
    if listing["asking_price_cents"] < floor:
        return [_signal("price_far_below_reference", {
            "asking_price_cents": listing["asking_price_cents"],
            "typical_price_cents": ref["typical_price_cents"],
        })]
    return []


def rule_new_account_high_value(conn, listing, photos_by_category):
    seller = conn.execute("SELECT created_at FROM users WHERE id = ?", (listing["seller_id"],)).fetchone()
    if not seller:
        return []
    account_age_days = (now_ts() - seller["created_at"]) / 86400
    if account_age_days <= NEW_ACCOUNT_DAYS and listing["asking_price_cents"] > NEW_ACCOUNT_PRICE_CENTS:
        return [_signal("new_account_high_value", {
            "account_age_days": round(account_age_days, 1), "asking_price_cents": listing["asking_price_cents"],
        })]
    return []


def rule_unusual_listing_velocity(conn, listing, photos_by_category):
    since = now_ts() - VELOCITY_WINDOW_HOURS * 3600
    count = conn.execute(
        """SELECT COUNT(*) c FROM listings
           WHERE seller_id = ? AND brand = ? AND perfume_name = ? AND created_at >= ?""",
        (listing["seller_id"], listing["brand"], listing["perfume_name"], since),
    ).fetchone()["c"]
    if count >= VELOCITY_COUNT_THRESHOLD:
        return [_signal("unusual_listing_velocity", {"count": count, "window_hours": VELOCITY_WINDOW_HOURS})]
    return []


def rule_prior_counterfeit_report(conn, listing, photos_by_category):
    row = conn.execute(
        """SELECT COUNT(*) c FROM authenticity_reports ar
           JOIN listings l ON l.id = ar.listing_id
           WHERE l.seller_id = ? AND ar.status = 'confirmed_counterfeit'""",
        (listing["seller_id"],),
    ).fetchone()
    if row["c"] > 0:
        return [_signal("prior_counterfeit_report", {"confirmed_report_count": row["c"]})]
    return []


def rule_low_quality_evidence_photo(conn, listing, photos_by_category):
    signals = []
    for category in required_categories(listing):
        photo = photos_by_category.get(category)
        if not photo:
            continue
        flags = json.loads(photo["integrity_flags"] or "{}")
        if flags.get("is_blurry"):
            signals.append(_signal("low_quality_evidence_photo", {"category": category}))
    return signals


def rule_possible_manipulation_signal(conn, listing, photos_by_category):
    signals = []
    for category, photo in photos_by_category.items():
        flags = json.loads(photo["integrity_flags"] or "{}")
        if flags.get("possible_manipulation"):
            signals.append(_signal("possible_manipulation_signal", {"category": category}))
    return signals


RULES = [
    rule_missing_required_photo,
    rule_batch_code_mismatch,
    rule_box_claim_inconsistent,
    rule_photo_reuse_cross_account,
    rule_photo_reuse_internal_excessive,
    rule_price_far_below_reference,
    rule_new_account_high_value,
    rule_unusual_listing_velocity,
    rule_prior_counterfeit_report,
    rule_low_quality_evidence_photo,
    rule_possible_manipulation_signal,
]


def band_for_score(score):
    low_max, medium_max = BAND_THRESHOLDS
    if score < low_max:
        return "low"
    if score < medium_max:
        return "medium"
    return "high"


def assess_listing(conn, listing_id):
    """
    Runs every rule, writes one risk_signals row per finding plus one
    risk_assessments row, and returns {score, band, signal_ids}. Does
    NOT commit — caller controls the transaction (same convention as
    app.orders.state_machine.transition_order_status). Does NOT change
    listings.verification_status — see app/authenticity/verification.py.
    """
    listing = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not listing:
        raise ValueError(f"no such listing: {listing_id}")

    photos = conn.execute("SELECT * FROM listing_photos WHERE listing_id = ?", (listing_id,)).fetchall()
    # Keep only the most recent photo per category for the rules above —
    # a re-upload supersedes the earlier one for risk purposes.
    photos_by_category = {}
    for photo in photos:
        existing = photos_by_category.get(photo["category"])
        if not existing or photo["created_at"] >= existing["created_at"]:
            photos_by_category[photo["category"]] = photo

    ts = now_ts()
    signal_ids = []
    total_score = 0
    for rule in RULES:
        for signal in rule(conn, listing, photos_by_category):
            signal_id = new_id()
            conn.execute(
                """INSERT INTO risk_signals (id, listing_id, signal_type, weight, detector, details, detected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (signal_id, listing_id, signal["signal_type"], signal["weight"], signal["detector"],
                 json.dumps(signal["details"]), ts),
            )
            signal_ids.append(signal_id)
            total_score += signal["weight"]

    band = band_for_score(total_score)
    assessment_id = new_id()
    conn.execute(
        """INSERT INTO risk_assessments (id, listing_id, score, band, engine_version, signal_ids, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (assessment_id, listing_id, total_score, band, ENGINE_VERSION, json.dumps(signal_ids), ts),
    )
    conn.execute(
        "UPDATE listings SET risk_score = ?, risk_band = ? WHERE id = ?",
        (total_score, band, listing_id),
    )

    return {"assessment_id": assessment_id, "score": total_score, "band": band, "signal_ids": signal_ids}
