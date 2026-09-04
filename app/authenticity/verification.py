"""
Verification lifecycle — the ONLY module allowed to write
listings.verification_status, and the only place `secondscent_verified`
can ever be granted. See docs/AUTHENTICITY_ARCHITECTURE.md § F/F.1/F.2.

Every write goes through _record_status(), which always appends to
listing_verifications (full history) before touching the cached column
on `listings`. Nothing in this module (or anywhere else) sets
verification_status with a raw UPDATE.
"""
import json

from app.authenticity import risk_engine
from app.db import get_db, new_id, now_ts
from app.notifications import notify

# The fixed standard from docs/AUTHENTICITY_ARCHITECTURE.md § F.1.
# Versioned so a later, stricter standard doesn't retroactively change
# what an older `secondscent_verified` listing was actually held to.
CURRENT_VERIFICATION_STANDARD_VERSION = 1

# The literal status explanations shown to users — see § F. Deliberately
# defined as constants, not built up as strings, so a test can assert
# none of them ever contains a forbidden absolute claim.
STATUS_EXPLANATIONS = {
    "unverified": "Nog geen enkele controle uitgevoerd.",
    "automated_checks_completed": (
        "Automatische controles zijn uitgevoerd en gaven geen aanleiding tot extra actie. "
        "Dit is geen garantie van echtheid — er heeft geen menselijke beoordeling plaatsgevonden."
    ),
    "additional_verification_required": (
        "We vragen de verkoper om aanvullend bewijs. De advertentie kan zichtbaar zijn terwijl dit loopt."
    ),
    "manual_review": "Deze advertentie wordt handmatig beoordeeld voordat hij zichtbaar wordt.",
    "secondscent_verified": (
        "Beoordeeld door een SecondScent-reviewer volgens onze verificatiestandaard. "
        "Dit vermindert het risico aanzienlijk maar is geen 100% garantie van echtheid — "
        "SecondScent biedt geen juridische echtheidsgarantie."
    ),
    "rejected": "Deze advertentie voldoet niet aan onze voorwaarden en is niet gepubliceerd.",
}

_FORBIDDEN_PHRASES = ["100% echt", "100% authentiek", "gegarandeerd echt", "echtheid gegarandeerd"]


class VerificationError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _record_status(conn, listing_id, status, method, reviewer_id=None, notes=None,
                    verification_version=None, evidence_snapshot=None):
    ts = now_ts()
    conn.execute(
        """INSERT INTO listing_verifications
           (id, listing_id, status, method, reviewer_id, notes, verification_version,
            evidence_snapshot, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (new_id(), listing_id, status, method, reviewer_id, notes, verification_version,
         json.dumps(evidence_snapshot or {}), ts),
    )
    fields = ["verification_status = ?", "verification_version = ?", "updated_at = ?"]
    params = [status, verification_version, ts]
    if status == "secondscent_verified":
        fields.append("verified_at = ?")
        params.append(ts)
    params.append(listing_id)
    conn.execute(f"UPDATE listings SET {', '.join(fields)} WHERE id = ?", params)


def _photos_by_category(conn, listing_id):
    photos = conn.execute("SELECT * FROM listing_photos WHERE listing_id = ?", (listing_id,)).fetchall()
    by_cat = {}
    for photo in photos:
        existing = by_cat.get(photo["category"])
        if not existing or photo["created_at"] >= existing["created_at"]:
            by_cat[photo["category"]] = photo
    return by_cat


# ---------------------------------------------------------------------
# Initial, automated outcome — right after submit-for-review.
# ---------------------------------------------------------------------
def submit_for_review(conn, listing_id, actor_id):
    listing = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not listing:
        raise VerificationError("not_found", "Advertentie bestaat niet.")
    if listing["seller_id"] != actor_id:
        raise VerificationError("forbidden", "Alleen de verkoper kan deze advertentie indienen.")
    if listing["status"] not in ("draft",):
        raise VerificationError("invalid_state", "Alleen concept-advertenties kunnen ingediend worden.")

    assessment = risk_engine.assess_listing(conn, listing_id)
    signal_ids = assessment["signal_ids"]
    if signal_ids:
        placeholders = ",".join("?" for _ in signal_ids)
        rows = conn.execute(
            f"SELECT signal_type FROM risk_signals WHERE id IN ({placeholders})", signal_ids,
        ).fetchall()
    else:
        rows = []
    signal_types = {row["signal_type"] for row in rows}
    missing_required = "missing_required_photo" in signal_types
    band = assessment["band"]

    if band == "high":
        status = "manual_review"
        publish = False
        conn.execute(
            """INSERT INTO manual_reviews (id, listing_id, status, opened_reason, created_at, updated_at)
               VALUES (?, ?, 'pending', ?, ?, ?)""",
            (new_id(), listing_id, f"automated risk score {assessment['score']} (high)", now_ts(), now_ts()),
        )
    elif missing_required:
        status = "additional_verification_required"
        publish = False
    elif band == "medium":
        status = "additional_verification_required"
        publish = True
    else:
        status = "automated_checks_completed"
        publish = True

    _record_status(conn, listing_id, status, method="automated",
                    notes=f"risk score {assessment['score']} ({band})")
    if publish:
        conn.execute("UPDATE listings SET status = 'active' WHERE id = ?", (listing_id,))
    notify(conn, actor_id, "listing_verification_status", listing_id=listing_id, status=status)

    return {"verification_status": status, "published": publish, "assessment": assessment}


# ---------------------------------------------------------------------
# The secondscent_verified standard (§ F.1) — a predicate, not a side effect.
# ---------------------------------------------------------------------
def meets_verified_standard(conn, listing_id):
    listing = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    reasons = []

    photos = _photos_by_category(conn, listing_id)
    missing = risk_engine.required_categories(listing) - set(photos)
    if missing:
        reasons.append(f"ontbrekende foto's: {', '.join(sorted(missing))}")
    for cat, photo in photos.items():
        if cat in risk_engine.required_categories(listing):
            flags = json.loads(photo["integrity_flags"] or "{}")
            if flags.get("is_blurry"):
                reasons.append(f"foto '{cat}' is te wazig")

    bottle_photo = photos.get("batch_code_bottle")
    bottle_code = bottle_photo["seller_entered_code"] if bottle_photo else None
    if not bottle_code or not str(bottle_code).strip():
        reasons.append("batchcode fles ontbreekt")
    if listing["box_included"]:
        pkg_photo = photos.get("batch_code_packaging")
        pkg_code = pkg_photo["seller_entered_code"] if pkg_photo else None
        if not pkg_code or not str(pkg_code).strip():
            reasons.append("batchcode verpakking ontbreekt")
        elif bottle_code and str(bottle_code).strip().upper() != str(pkg_code).strip().upper():
            reasons.append("batchcode fles en verpakking komen niet overeen")

    latest_assessment = conn.execute(
        "SELECT * FROM risk_assessments WHERE listing_id = ? ORDER BY computed_at DESC LIMIT 1",
        (listing_id,),
    ).fetchone()
    unresolved_high_weight = []
    if latest_assessment:
        signal_ids = json.loads(latest_assessment["signal_ids"])
        if signal_ids:
            rows = conn.execute(
                "SELECT signal_type, weight FROM risk_signals WHERE id IN ({})".format(
                    ",".join("?" for _ in signal_ids)
                ),
                signal_ids,
            ).fetchall()
            unresolved_high_weight = [r["signal_type"] for r in rows if r["weight"] >= 20]

    return {
        "meets_hard_requirements": len(reasons) == 0,
        "reasons": reasons,
        "high_weight_signals_needing_explanation": unresolved_high_weight,
    }


# ---------------------------------------------------------------------
# Manual review actions — the four from the dashboard (§ G).
# ---------------------------------------------------------------------
def _load_review(conn, review_id):
    review = conn.execute("SELECT * FROM manual_reviews WHERE id = ?", (review_id,)).fetchone()
    if not review:
        raise VerificationError("not_found", "Review bestaat niet.")
    return review


def _log_action(conn, review_id, admin_id, action, notes):
    conn.execute(
        "INSERT INTO listing_review_actions (id, review_id, admin_id, action, notes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (new_id(), review_id, admin_id, action, notes, now_ts()),
    )
    conn.execute(
        """INSERT INTO audit_logs (id, actor_type, actor_id, action, entity_type, entity_id, metadata, created_at)
           VALUES (?, 'admin', ?, ?, 'listing_review', ?, ?, ?)""",
        (new_id(), admin_id, f"listing_review:{action}", review_id, json.dumps({"notes": notes}), now_ts()),
    )


def approve(conn, review_id, admin_id, notes=None):
    review = _load_review(conn, review_id)
    if review["status"] == "decided":
        raise VerificationError("already_decided", "Deze zaak is al afgehandeld.")
    listing_id = review["listing_id"]

    standard = meets_verified_standard(conn, listing_id)
    if not standard["meets_hard_requirements"]:
        raise VerificationError(
            "standard_not_met",
            "Kan niet goedkeuren als SecondScent Verified: " + "; ".join(standard["reasons"]) +
            ". Gebruik 'request_more_evidence' in plaats daarvan.",
        )
    if standard["high_weight_signals_needing_explanation"] and not (notes and notes.strip()):
        raise VerificationError(
            "notes_required",
            "Er zijn onopgeloste signalen met een hoog gewicht "
            f"({', '.join(standard['high_weight_signals_needing_explanation'])}); "
            "voeg een notitie toe die uitlegt waarom goedkeuring hier toch verantwoord is.",
        )

    photos = _photos_by_category(conn, listing_id)
    evidence_snapshot = {cat: photo["id"] for cat, photo in photos.items()}
    _record_status(conn, listing_id, "secondscent_verified", method="manual", reviewer_id=admin_id,
                    notes=notes, verification_version=CURRENT_VERIFICATION_STANDARD_VERSION,
                    evidence_snapshot=evidence_snapshot)
    conn.execute("UPDATE listings SET status = 'active' WHERE id = ?", (listing_id,))
    conn.execute("UPDATE manual_reviews SET status = 'decided', updated_at = ? WHERE id = ?",
                 (now_ts(), review_id))
    _log_action(conn, review_id, admin_id, "approve", notes)

    listing = conn.execute("SELECT seller_id FROM listings WHERE id = ?", (listing_id,)).fetchone()
    notify(conn, listing["seller_id"], "listing_verification_status", listing_id=listing_id,
           status="secondscent_verified")
    return {"verification_status": "secondscent_verified"}


def request_more_evidence(conn, review_id, admin_id, notes):
    if not notes or not notes.strip():
        raise VerificationError("notes_required", "Leg uit welk bewijs ontbreekt.")
    review = _load_review(conn, review_id)
    if review["status"] == "decided":
        raise VerificationError("already_decided", "Deze zaak is al afgehandeld.")
    listing_id = review["listing_id"]

    _record_status(conn, listing_id, "additional_verification_required", method="manual",
                    reviewer_id=admin_id, notes=notes)
    conn.execute("UPDATE manual_reviews SET status = 'pending', updated_at = ? WHERE id = ?",
                 (now_ts(), review_id))
    _log_action(conn, review_id, admin_id, "request_more_evidence", notes)

    listing = conn.execute("SELECT seller_id FROM listings WHERE id = ?", (listing_id,)).fetchone()
    notify(conn, listing["seller_id"], "listing_more_evidence_requested", listing_id=listing_id, notes=notes)
    return {"verification_status": "additional_verification_required"}


def reject(conn, review_id, admin_id, notes):
    if not notes or not notes.strip():
        raise VerificationError("notes_required", "Leg uit waarom deze advertentie wordt afgewezen.")
    review = _load_review(conn, review_id)
    if review["status"] == "decided":
        raise VerificationError("already_decided", "Deze zaak is al afgehandeld.")
    listing_id = review["listing_id"]

    _record_status(conn, listing_id, "rejected", method="manual", reviewer_id=admin_id, notes=notes)
    conn.execute("UPDATE listings SET status = 'removed_by_admin' WHERE id = ?", (listing_id,))
    conn.execute("UPDATE manual_reviews SET status = 'decided', updated_at = ? WHERE id = ?",
                 (now_ts(), review_id))
    _log_action(conn, review_id, admin_id, "reject", notes)

    listing = conn.execute("SELECT seller_id FROM listings WHERE id = ?", (listing_id,)).fetchone()
    notify(conn, listing["seller_id"], "listing_rejected", listing_id=listing_id, notes=notes)
    return {"verification_status": "rejected"}


def escalate(conn, review_id, admin_id, notes=None):
    review = _load_review(conn, review_id)
    if review["status"] == "decided":
        raise VerificationError("already_decided", "Deze zaak is al afgehandeld.")
    conn.execute(
        "UPDATE manual_reviews SET status = 'pending', assigned_admin_id = NULL, updated_at = ? WHERE id = ?",
        (now_ts(), review_id),
    )
    _log_action(conn, review_id, admin_id, "escalate", notes)
    return {"escalated": True}


# ---------------------------------------------------------------------
# § F.2 — automatic expiry when a verified listing changes.
# ---------------------------------------------------------------------
CORE_FIELDS = ("brand", "perfume_name", "size_ml", "batch_code", "box_included")


def on_listing_changed(conn, listing_id, changed_fields, actor="system"):
    """
    Call this whenever a core field changes or a required photo is
    replaced/removed. If the listing currently holds the verified badge,
    this is the ONLY code path that revokes it — it always drops back to
    manual_review (never silently to unverified) and always logs a
    post_verification_change signal, so the reviewer sees exactly why a
    previously-verified listing is back in the queue.
    """
    listing = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if not listing or listing["verification_status"] != "secondscent_verified":
        return None

    conn.execute(
        """INSERT INTO risk_signals (id, listing_id, signal_type, weight, detector, details, detected_at)
           VALUES (?, ?, 'post_verification_change', 25, 'system:post_verification_change:v1', ?, ?)""",
        (new_id(), listing_id, json.dumps({"changed_fields": list(changed_fields), "actor": actor}), now_ts()),
    )
    _record_status(conn, listing_id, "manual_review", method="system",
                    notes=f"kernvelden gewijzigd na verificatie: {', '.join(changed_fields)}")
    conn.execute(
        """INSERT INTO manual_reviews (id, listing_id, status, opened_reason, created_at, updated_at)
           VALUES (?, ?, 'pending', 'wijziging na SecondScent Verified', ?, ?)""",
        (new_id(), listing_id, now_ts(), now_ts()),
    )
    listing_seller = listing["seller_id"]
    notify(conn, listing_seller, "listing_verification_revoked", listing_id=listing_id,
           changed_fields=list(changed_fields))
    return {"verification_status": "manual_review"}
