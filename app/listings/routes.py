"""
Listings — creation and the 12-category evidence photo upload, scoped
exactly to what the Authenticity & Anti-Counterfeit system needs (see
docs/AUTHENTICITY_ARCHITECTURE.md § A). Full listing CRUD/search/browse
is still a later, separate phase.

Endpoints:
    POST /listings                              seller -> create a draft
    GET  /listings/mine                          seller -> own listings
    GET  /listings/<id>                          public (if active) / owner / admin
    GET  /listings/photo-categories              the 12 categories + instructions
    POST /listings/<id>/photos                   seller -> upload one evidence photo
    GET  /listings/<id>/photos/<photo_id>/file    authorized -> raw image bytes
    PATCH /listings/<id>                          seller -> edit (triggers § F.2 on a verified listing)
    POST /listings/<id>/submit-for-review         seller -> runs the risk engine, sets initial status
"""
import json

from flask import Blueprint, jsonify, request, session, Response

from app.auth.routes import login_required, admin_required, csrf_protected
from app.authenticity import photo_integrity, risk_engine, verification
from app.authenticity.photo_integrity import InvalidImageError
from app.authenticity.verification import STATUS_EXPLANATIONS, VerificationError
from app.db import get_db, new_id, now_ts
from app.evidence_store import save as save_evidence, read as read_evidence

listings_bp = Blueprint("listings", __name__, url_prefix="/listings")

# category -> (NL label, required always, required only if box_included)
CATEGORY_META = {
    "bottle_front":         ("Voorkant fles", True, False),
    "bottle_back":          ("Achterkant fles", True, False),
    "bottle_bottom":        ("Onderkant fles", True, False),
    "nozzle":                ("Verstuiver / nozzle", True, False),
    "cap":                   ("Dop", True, False),
    "box_front":             ("Doos — voorkant", False, True),
    "box_back":              ("Doos — achterkant", False, True),
    "box_bottom":            ("Doos — onderkant", False, True),
    "batch_code_bottle":     ("Batchcode op de fles", True, False),
    "batch_code_packaging":  ("Batchcode op de verpakking", False, True),
    "barcode":                ("Barcode (indien aanwezig)", False, False),
    "proof_of_purchase":      ("Aankoopbewijs (optioneel)", False, False),
}

UPLOAD_INSTRUCTIONS = (
    "Zorg voor voldoende licht, een scherpe foto (niet wazig), gebruik geen "
    "filters of bewerkingen, zorg dat tekst goed leesbaar is, en zorg dat "
    "het hele object in beeld is."
)

REQUIRED_BASE_FIELDS = ("brand", "perfume_name", "size_ml", "original_size_ml",
                         "estimated_remaining_percent", "condition", "asking_price_cents")

CORE_FIELDS = ("brand", "perfume_name", "size_ml", "batch_code", "box_included")

# condition -> NL label. Matches the CHECK constraint in db/schema.sql.
CONDITION_LABELS = {
    "new_sealed": "Nieuw, verzegeld",
    "new_decanted": "Nieuw, gedecanteerd",
    "used_like_new": "Gebruikt, als nieuw",
    "used_good": "Gebruikt, goede staat",
    "used_fair": "Gebruikt, redelijke staat",
}


def _listing_or_404(conn, listing_id):
    return conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()


def _is_admin(conn, user_id):
    row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["is_admin"])


def _public_listing(listing, viewer_is_owner_or_admin):
    data = {
        "id": listing["id"], "seller_id": listing["seller_id"], "brand": listing["brand"],
        "perfume_name": listing["perfume_name"], "variant_concentration": listing["variant_concentration"],
        "size_ml": listing["size_ml"], "original_size_ml": listing["original_size_ml"],
        "estimated_remaining_percent": listing["estimated_remaining_percent"],
        "condition": listing["condition"], "asking_price_cents": listing["asking_price_cents"],
        "currency": listing["currency"], "box_included": bool(listing["box_included"]),
        "proof_of_purchase_available": bool(listing["proof_of_purchase_available"]),
        "description": listing["description"], "tradeable": bool(listing["tradeable"]),
        "status": listing["status"],
        "verification_status": listing["verification_status"],
        "verification_explanation": STATUS_EXPLANATIONS.get(listing["verification_status"], ""),
        "verified_at": listing["verified_at"],
        "created_at": listing["created_at"], "updated_at": listing["updated_at"],
    }
    # Batch code / barcode text and risk internals are never shown to a
    # buyer browsing the marketplace — only to the seller themselves or admin.
    if viewer_is_owner_or_admin:
        data["batch_code"] = listing["batch_code"]
        data["barcode"] = listing["barcode"]
        data["purchase_source"] = listing["purchase_source"]
        data["purchase_date"] = listing["purchase_date"]
    return data


# ---------------------------------------------------------------------
# Category metadata (drives the seller's upload UI)
# ---------------------------------------------------------------------
@listings_bp.route("/photo-categories", methods=["GET"])
def photo_categories():
    return jsonify(
        instructions=UPLOAD_INSTRUCTIONS,
        categories=[
            {"category": cat, "label": label, "required": required,
             "required_if_box_included": required_if_box}
            for cat, (label, required, required_if_box) in CATEGORY_META.items()
        ],
    )


# ---------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------
@listings_bp.route("", methods=["POST"])
@csrf_protected
@login_required
def create_listing():
    conn = get_db()
    user = conn.execute("SELECT is_seller FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if not user or not user["is_seller"]:
        conn.close()
        return jsonify(error="not_a_seller", message="Word eerst verkoper via /auth/become-seller."), 403

    data = request.get_json(silent=True) or {}
    missing = [f for f in REQUIRED_BASE_FIELDS if data.get(f) in (None, "")]
    if missing:
        conn.close()
        return jsonify(error="missing_fields", fields=missing), 400
    if data["asking_price_cents"] <= 0 or data["size_ml"] <= 0 or data["original_size_ml"] <= 0:
        conn.close()
        return jsonify(error="invalid_values"), 400
    if not (0 <= data["estimated_remaining_percent"] <= 100):
        conn.close()
        return jsonify(error="invalid_values", message="estimated_remaining_percent moet 0-100 zijn."), 400

    listing_id = new_id()
    ts = now_ts()
    conn.execute(
        """INSERT INTO listings
           (id, seller_id, brand, perfume_name, variant_concentration, size_ml, original_size_ml,
            estimated_remaining_percent, condition, batch_code, barcode, purchase_source, purchase_date,
            asking_price_cents, currency, box_included, proof_of_purchase_available, description,
            tradeable, status, verification_status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', 'unverified', ?, ?)""",
        (listing_id, session["user_id"], data["brand"], data["perfume_name"],
         data.get("variant_concentration"), data["size_ml"], data["original_size_ml"],
         data["estimated_remaining_percent"], data["condition"], data.get("batch_code"),
         data.get("barcode"), data.get("purchase_source"), data.get("purchase_date"),
         data["asking_price_cents"], data.get("currency", "EUR"),
         1 if data.get("box_included") else 0, 1 if data.get("proof_of_purchase_available") else 0,
         data.get("description", ""), 1 if data.get("tradeable") else 0, ts, ts),
    )
    conn.commit()
    listing = _listing_or_404(conn, listing_id)
    conn.close()
    return jsonify(listing=_public_listing(listing, True)), 201


@listings_bp.route("/mine", methods=["GET"])
@login_required
def my_listings():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM listings WHERE seller_id = ? ORDER BY created_at DESC", (session["user_id"],)
    ).fetchall()
    conn.close()
    return jsonify(listings=[_public_listing(r, True) for r in rows])


# ---------------------------------------------------------------------
# Public browse — active listings only. This is the one endpoint an
# anonymous visitor (or the homepage/shop page) can call; every other
# read above requires a session. Filtering is deliberately simple
# (exact/substring match server-side) — a real search index is a later
# phase, same as the rest of "advertenties komen later".
# ---------------------------------------------------------------------
@listings_bp.route("", methods=["GET"])
def browse_listings():
    conn = get_db()
    clauses = ["status = 'active'"]
    params = []

    q = (request.args.get("q") or "").strip()
    if q:
        clauses.append("(brand LIKE ? OR perfume_name LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])

    brand = (request.args.get("brand") or "").strip()
    if brand:
        clauses.append("brand = ?")
        params.append(brand)

    condition = (request.args.get("condition") or "").strip()
    if condition:
        clauses.append("condition = ?")
        params.append(condition)

    if request.args.get("tradeable") == "1":
        clauses.append("tradeable = 1")

    try:
        limit = min(max(int(request.args.get("limit", 60)), 1), 200)
    except ValueError:
        limit = 60

    sql = f"SELECT * FROM listings WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()

    brands = [r["brand"] for r in conn.execute(
        "SELECT DISTINCT brand FROM listings WHERE status = 'active' ORDER BY brand"
    ).fetchall()]
    conn.close()
    return jsonify(listings=[_public_listing(r, False) for r in rows], brands=brands)


@listings_bp.route("/<listing_id>", methods=["GET"])
def get_listing(listing_id):
    conn = get_db()
    listing = _listing_or_404(conn, listing_id)
    if not listing:
        conn.close()
        return jsonify(error="not_found"), 404
    user_id = session.get("user_id")
    is_owner_or_admin = bool(user_id) and (listing["seller_id"] == user_id or _is_admin(conn, user_id))
    if listing["status"] != "active" and not is_owner_or_admin:
        conn.close()
        return jsonify(error="not_found"), 404
    conn.close()
    return jsonify(listing=_public_listing(listing, is_owner_or_admin))


# ---------------------------------------------------------------------
# Photo upload
# ---------------------------------------------------------------------
@listings_bp.route("/<listing_id>/photos", methods=["POST"])
@csrf_protected
@login_required
def upload_photo(listing_id):
    conn = get_db()
    listing = _listing_or_404(conn, listing_id)
    if not listing:
        conn.close()
        return jsonify(error="not_found"), 404
    if listing["seller_id"] != session["user_id"]:
        conn.close()
        return jsonify(error="forbidden"), 403
    if listing["status"] not in ("draft",) and listing["verification_status"] not in ("additional_verification_required",):
        conn.close()
        return jsonify(error="invalid_state",
                        message="Foto's kunnen alleen worden geüpload voor een concept-advertentie "
                                "of terwijl aanvullend bewijs gevraagd is."), 409

    category = request.form.get("category")
    if category not in CATEGORY_META:
        conn.close()
        return jsonify(error="invalid_category", allowed=sorted(CATEGORY_META)), 400
    file_storage = request.files.get("file")
    if not file_storage:
        conn.close()
        return jsonify(error="missing_file"), 400
    seller_entered_code = (request.form.get("seller_entered_code") or "").strip() or None

    image_bytes = file_storage.read()
    try:
        flags = photo_integrity.analyze(image_bytes)
    except InvalidImageError as exc:
        conn.close()
        return jsonify(error="invalid_image", message=str(exc)), 400

    was_previously_verified = listing["verification_status"] == "secondscent_verified"
    had_existing_photo_in_category = conn.execute(
        "SELECT 1 FROM listing_photos WHERE listing_id = ? AND category = ?", (listing_id, category)
    ).fetchone() is not None

    ext = (file_storage.filename or "photo.jpg").rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        ext = "jpg"
    file_ref = save_evidence("listing_photos", listing_id, image_bytes, extension=ext)

    photo_id = new_id()
    conn.execute(
        """INSERT INTO listing_photos
           (id, listing_id, category, file_ref, sha256_hash, phash, width, height, exif_json,
            integrity_flags, seller_entered_code, position, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
        (photo_id, listing_id, category, file_ref, flags["sha256"], flags["phash"],
         flags["width"], flags["height"], json.dumps(flags["exif"]), json.dumps(flags),
         seller_entered_code, now_ts()),
    )

    if was_previously_verified and had_existing_photo_in_category:
        verification.on_listing_changed(conn, listing_id, [f"photo:{category}"], actor=session["user_id"])

    conn.commit()
    conn.close()

    # Only user-facing quality feedback — never the internal signals.
    warnings = []
    if flags["is_blurry"]:
        warnings.append("Deze foto lijkt wazig. Overweeg een scherpere foto te uploaden.")
    return jsonify(photo_id=photo_id, category=category, warnings=warnings), 201


@listings_bp.route("/<listing_id>/photos/<photo_id>/file", methods=["GET"])
def get_photo_file(listing_id, photo_id):
    conn = get_db()
    listing = _listing_or_404(conn, listing_id)
    photo = conn.execute(
        "SELECT * FROM listing_photos WHERE id = ? AND listing_id = ?", (photo_id, listing_id)
    ).fetchone()
    if not listing or not photo:
        conn.close()
        return jsonify(error="not_found"), 404

    user_id = session.get("user_id")
    is_owner_or_admin = bool(user_id) and (listing["seller_id"] == user_id or _is_admin(conn, user_id))
    # Proof of purchase is always restricted (§ I). Other categories are
    # part of the normal listing photos and are viewable once the
    # listing is published — same "not before, not if removed" rule as
    # the listing itself.
    if photo["category"] == "proof_of_purchase":
        allowed = is_owner_or_admin
    else:
        allowed = is_owner_or_admin or listing["status"] == "active"
    if not allowed:
        conn.close()
        return jsonify(error="not_found"), 404

    conn.close()
    data = read_evidence(photo["file_ref"])
    ext = photo["file_ref"].rsplit(".", 1)[-1].lower()
    mimetype = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "application/octet-stream")
    return Response(data, mimetype=mimetype)


# ---------------------------------------------------------------------
# Edit (only what's needed to exercise § F.2 expiry — full editing UI is
# still a later phase)
# ---------------------------------------------------------------------
@listings_bp.route("/<listing_id>", methods=["PATCH"])
@csrf_protected
@login_required
def update_listing(listing_id):
    conn = get_db()
    listing = _listing_or_404(conn, listing_id)
    if not listing:
        conn.close()
        return jsonify(error="not_found"), 404
    if listing["seller_id"] != session["user_id"]:
        conn.close()
        return jsonify(error="forbidden"), 403

    data = request.get_json(silent=True) or {}
    editable = ("brand", "perfume_name", "variant_concentration", "size_ml", "condition",
                "batch_code", "barcode", "asking_price_cents", "box_included",
                "proof_of_purchase_available", "description", "tradeable")
    changed_core_fields = []
    set_clauses = []
    params = []
    for field in editable:
        if field not in data:
            continue
        new_value = data[field]
        if field in ("box_included", "proof_of_purchase_available", "tradeable"):
            new_value = 1 if new_value else 0
        old_value = listing[field]
        if field in CORE_FIELDS and new_value != old_value:
            changed_core_fields.append(field)
        set_clauses.append(f"{field} = ?")
        params.append(new_value)

    if not set_clauses:
        conn.close()
        return jsonify(error="no_fields_to_update"), 400

    set_clauses.append("updated_at = ?")
    params.append(now_ts())
    params.append(listing_id)
    conn.execute(f"UPDATE listings SET {', '.join(set_clauses)} WHERE id = ?", params)

    if changed_core_fields:
        verification.on_listing_changed(conn, listing_id, changed_core_fields, actor=session["user_id"])

    conn.commit()
    updated = _listing_or_404(conn, listing_id)
    conn.close()
    return jsonify(listing=_public_listing(updated, True))


# ---------------------------------------------------------------------
# Submit for review — runs the risk engine, sets the initial status.
# ---------------------------------------------------------------------
@listings_bp.route("/<listing_id>/submit-for-review", methods=["POST"])
@csrf_protected
@login_required
def submit_for_review(listing_id):
    conn = get_db()
    try:
        result = verification.submit_for_review(conn, listing_id, session["user_id"])
    except VerificationError as exc:
        conn.close()
        return jsonify(error=exc.code, message=exc.message), 409
    conn.commit()
    listing = _listing_or_404(conn, listing_id)
    conn.close()
    return jsonify(
        listing=_public_listing(listing, True),
        verification_status=result["verification_status"],
        published=result["published"],
    )
