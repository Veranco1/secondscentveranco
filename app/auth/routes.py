"""
Accounts & auth blueprint — Fase 1.

Endpoints (JSON API):
    GET  /auth/csrf-token     -> {csrf_token}                (call once per session)
    POST /auth/register       -> {email, password, display_name}
    POST /auth/login          -> {email, password}
    POST /auth/logout
    GET  /auth/me             -> current user (requires session)
    POST /auth/become-seller  -> flips is_seller=true for the current user
                                  (Fase 4 will require real Stripe Connect
                                  onboarding before is_seller unlocks payouts
                                  — this flag alone never authorizes a payout)

Security choices, and why:
  - Passwords hashed with werkzeug's generate_password_hash (scrypt by
    default in current Werkzeug) — never stored or logged in plain text.
  - Session cookies: httponly, SameSite=Lax, secure when FORCE_HTTPS=1.
  - CSRF: this is a same-origin JSON API served with cookie-based
    sessions, so state-changing requests (register/login/logout/
    become-seller) require BOTH the session cookie AND a per-session
    CSRF token sent back in the `X-CSRF-Token` header. A page fetches
    the token once via GET /auth/csrf-token before calling anything else.
  - Login rate limiting: keyed on (email, ip) pair, 5 failed attempts /
    15 minutes locks that pair out — slows credential stuffing without
    letting one abusive IP lock out every other account.
  - Server never trusts a client-submitted user id for "who am I" — the
    session is the only source of identity.
"""
import os
import re
import secrets
import time
import uuid
from functools import wraps

from flask import Blueprint, jsonify, request, session
from werkzeug.security import generate_password_hash, check_password_hash

from app.db import get_db

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60


# ---------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------
def _csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


def csrf_protected(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        sent = request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf_token")
        if not expected or sent != expected:
            return jsonify(error="invalid_csrf_token",
                            message="Missing or invalid X-CSRF-Token header."), 400
        return view(*args, **kwargs)
    return wrapped


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify(error="not_authenticated"), 401
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """
    Stacks on top of login_required (put login_required closer to the
    function). Re-checks is_admin against the database on every request
    rather than trusting anything cached in the session, since admin
    status is exactly the kind of privilege escalation an attacker would
    want to persist past a legitimate revocation.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        conn = get_db()
        row = conn.execute(
            "SELECT is_admin FROM users WHERE id = ?", (session.get("user_id"),)
        ).fetchone()
        conn.close()
        if not row or not row["is_admin"]:
            return jsonify(error="forbidden", message="Alleen voor beheerders."), 403
        return view(*args, **kwargs)
    return wrapped


@auth_bp.route("/csrf-token", methods=["GET"])
def csrf_token():
    return jsonify(csrf_token=_csrf_token())


# ---------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------
def _too_many_attempts(email, ip):
    conn = get_db()
    since = int(time.time()) - LOGIN_WINDOW_SECONDS
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM login_attempts "
        "WHERE email = ? AND ip = ? AND attempted_at > ? AND success = 0",
        (email, ip, since),
    ).fetchone()
    conn.close()
    return row["c"] >= MAX_LOGIN_ATTEMPTS


def _record_attempt(email, ip, success):
    conn = get_db()
    conn.execute(
        "INSERT INTO login_attempts (email, ip, attempted_at, success) VALUES (?, ?, ?, ?)",
        (email, ip, int(time.time()), 1 if success else 0),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Serialization — never leak password_hash or internal risk fields to
# the client the user themselves is looking at (risk_score is
# admin-only; exposed separately via the admin blueprint in Fase 7).
# ---------------------------------------------------------------------
def _public_user(row):
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "country": row["country"],
        "locale": row["locale"],
        "is_buyer": bool(row["is_buyer"]),
        "is_seller": bool(row["is_seller"]),
        "account_status": row["account_status"],
    }


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@auth_bp.route("/register", methods=["POST"])
@csrf_protected
def register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    display_name = (data.get("display_name") or "").strip()

    if not EMAIL_RE.match(email):
        return jsonify(error="invalid_email"), 400
    if len(password) < 8:
        return jsonify(error="weak_password", message="Minimaal 8 tekens."), 400
    if not display_name:
        return jsonify(error="missing_display_name"), 400

    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.close()
        # Deliberately vague — do not reveal whether an email is registered
        # to an unauthenticated caller beyond what's needed.
        return jsonify(error="registration_failed",
                        message="Kon geen account aanmaken met deze gegevens."), 409

    user_id = str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        """INSERT INTO users
           (id, email, password_hash, display_name, is_buyer, is_seller,
            account_status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 1, 0, 'active', ?, ?)""",
        (user_id, email, generate_password_hash(password), display_name, now, now),
    )
    conn.execute(
        """INSERT INTO user_verifications (id, user_id, verification_type, status, created_at)
           VALUES (?, ?, 'email', 'pending', ?)""",
        (str(uuid.uuid4()), user_id, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()

    session.clear()
    session.permanent = True
    session["user_id"] = user_id
    return jsonify(user=_public_user(row)), 201


@auth_bp.route("/login", methods=["POST"])
@csrf_protected
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    ip = request.remote_addr or "unknown"

    if _too_many_attempts(email, ip):
        return jsonify(error="too_many_attempts",
                        message="Te veel mislukte pogingen. Probeer het over 15 minuten opnieuw."), 429

    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()

    if not row or not check_password_hash(row["password_hash"], password):
        _record_attempt(email, ip, success=False)
        return jsonify(error="invalid_credentials"), 401

    if row["account_status"] != "active":
        _record_attempt(email, ip, success=False)
        return jsonify(error="account_not_active", status=row["account_status"]), 403

    _record_attempt(email, ip, success=True)

    conn = get_db()
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (int(time.time()), row["id"]))
    conn.commit()
    conn.close()

    session.clear()
    session.permanent = True
    session["user_id"] = row["id"]
    return jsonify(user=_public_user(row))


@auth_bp.route("/logout", methods=["POST"])
@csrf_protected
def logout():
    session.clear()
    return jsonify(ok=True)


@auth_bp.route("/me", methods=["GET"])
@login_required
def me():
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()
    if not row:
        session.clear()
        return jsonify(error="not_authenticated"), 401
    return jsonify(user=_public_user(row))


@auth_bp.route("/become-seller", methods=["POST"])
@csrf_protected
@login_required
def become_seller():
    conn = get_db()
    conn.execute(
        "UPDATE users SET is_seller = 1, updated_at = ? WHERE id = ?",
        (int(time.time()), session["user_id"]),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()
    return jsonify(
        user=_public_user(row),
        note="is_seller staat nu aan. Uitbetalingen vereisen daarnaast een "
             "afgeronde Stripe Connect-onboarding (Fase 4) — dat is hier nog niet geïmplementeerd.",
    )


# ---------------------------------------------------------------------
# Dev-only: simulate a completed Stripe Connect onboarding.
#
# Real Stripe Connect onboarding (Fase 4) is not built — there is no
# live Stripe account in this sandbox to onboard against (see
# app/payments/stripe_client.py). Without SOME stand-in, a seller could
# never actually receive a payout in the demo, and a buyer's "confirm
# receipt" (app/orders/routes.py::confirm_order -> _release_payout)
# would fail every single time with seller_not_onboarded — meaning the
# core buyer-protection promise of this whole platform could never be
# demonstrated end-to-end in a browser. This endpoint exists only to
# unblock that, is explicitly labelled as a simulation everywhere it's
# surfaced in the UI, writes a fake stripe_account_id that is obviously
# not a real one, and refuses outright whenever PAYMENTS_MODE=live.
# ---------------------------------------------------------------------
@auth_bp.route("/dev-enable-payouts", methods=["POST"])
@csrf_protected
@login_required
def dev_enable_payouts():
    if os.environ.get("PAYMENTS_MODE", "fake") == "live":
        return jsonify(error="not_available",
                        message="Alleen beschikbaar met PAYMENTS_MODE=fake."), 403

    conn = get_db()
    user_id = session["user_id"]
    fake_account_id = "acct_fake_" + secrets.token_hex(8)
    conn.execute(
        """INSERT INTO stripe_connect_accounts (user_id, stripe_account_id, charges_enabled, payouts_enabled, updated_at)
           VALUES (?, ?, 1, 1, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             charges_enabled = 1, payouts_enabled = 1, updated_at = excluded.updated_at""",
        (user_id, fake_account_id, int(time.time())),
    )
    conn.commit()
    conn.close()
    return jsonify(ok=True, stripe_account_id=fake_account_id, payouts_enabled=True)
