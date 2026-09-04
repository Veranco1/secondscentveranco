"""
Data-access layer.

PRODUCTION: the canonical schema is db/schema.sql, written for PostgreSQL,
and verified directly against a real Postgres 16 instance (see
docs/ARCHITECTURE.md § F) — including the order-status state machine
trigger, the append-only evidence/messages/notes/timeline triggers, the
total-price check constraint, and the fake-review guards. Run it with a
real Postgres driver (psycopg[binary]) in any environment with normal
internet access.

THIS SANDBOX: package installation from PyPI is blocked here, so there is
no Postgres driver available to run a Python app against Postgres in this
environment. To still deliver a real, runnable, end-to-end tested system,
this module provides a SQLite stand-in for local development ONLY. It
mirrors db/schema.sql's shape and — importantly — re-implements the SAME
two safety mechanisms as native SQLite triggers, not just Python checks:
  1. order status transitions are validated against order_status_transitions
     (a bad transition raises, exactly like the Postgres version);
  2. dispute_evidence / dispute_messages / dispute_admin_notes /
     dispute_events are append-only — UPDATE/DELETE is rejected by the
     database itself.
Moving to Postgres later is a connection swap, not a rewrite: keep the
same table/column names (already aligned with db/schema.sql).
"""
import os
import sqlite3
import time
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DEV_DB_PATH", os.path.join(BASE_DIR, "data", "dev.db"))

ORDER_STATUS_TRANSITIONS = [
    ("payment_pending", "paid"),
    ("payment_pending", "cancelled"),
    ("paid", "awaiting_shipment"),
    ("paid", "cancelled"),
    ("awaiting_shipment", "shipped"),
    ("awaiting_shipment", "cancelled"),
    ("shipped", "delivered"),
    ("shipped", "issue_reported"),
    ("delivered", "inspection_period"),
    ("inspection_period", "completed"),
    ("inspection_period", "issue_reported"),
    ("issue_reported", "under_review"),
    ("under_review", "return_required"),
    ("under_review", "completed"),
    ("under_review", "refunded"),
    ("return_required", "return_shipped"),
    ("return_shipped", "refunded"),
    ("paid", "under_review"),
    ("awaiting_shipment", "under_review"),
    ("shipped", "under_review"),
    ("inspection_period", "under_review"),
]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def new_id():
    return str(uuid.uuid4())


def now_ts():
    return int(time.time())


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id              TEXT PRIMARY KEY,
            email           TEXT UNIQUE NOT NULL,
            password_hash   TEXT NOT NULL,
            display_name    TEXT NOT NULL,
            country         TEXT,
            locale          TEXT NOT NULL DEFAULT 'nl',
            is_buyer        INTEGER NOT NULL DEFAULT 1,
            is_seller       INTEGER NOT NULL DEFAULT 0,
            is_admin        INTEGER NOT NULL DEFAULT 0,
            account_status  TEXT NOT NULL DEFAULT 'active',
            risk_score      INTEGER NOT NULL DEFAULT 0,
            last_login_at   INTEGER,
            created_at      INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_verifications (
            id                TEXT PRIMARY KEY,
            user_id           TEXT NOT NULL REFERENCES users(id),
            verification_type TEXT NOT NULL,
            status            TEXT NOT NULL DEFAULT 'pending',
            evidence_ref      TEXT,
            verified_at       INTEGER,
            created_at        INTEGER NOT NULL,
            UNIQUE(user_id, verification_type)
        );

        CREATE TABLE IF NOT EXISTS login_attempts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            email        TEXT NOT NULL,
            ip           TEXT NOT NULL,
            attempted_at INTEGER NOT NULL,
            success      INTEGER NOT NULL
        );

        -- Listings. Full CRUD/search/moderation UI is still a later,
        -- separate phase ("advertenties komen later") — what's here is
        -- exactly what checkout and the Authenticity & Anti-Counterfeit
        -- system need (see docs/AUTHENTICITY_ARCHITECTURE.md). New
        -- columns beyond the original minimal set are nullable here even
        -- where db/schema.sql (the canonical Postgres schema) makes them
        -- NOT NULL — required-ness for a real submission is enforced in
        -- app/listings/routes.py, matching how this dev layer already
        -- treats CHECK constraints elsewhere as a looser mirror.
        CREATE TABLE IF NOT EXISTS listings (
            id                          TEXT PRIMARY KEY,
            seller_id                   TEXT NOT NULL REFERENCES users(id),
            brand                       TEXT NOT NULL,
            perfume_name                TEXT NOT NULL,
            variant_concentration       TEXT,
            size_ml                     INTEGER NOT NULL,
            original_size_ml            INTEGER,
            estimated_remaining_percent INTEGER,
            condition                   TEXT NOT NULL,
            batch_code                  TEXT,
            barcode                     TEXT,
            purchase_source             TEXT,
            purchase_date               TEXT,
            asking_price_cents          INTEGER NOT NULL,
            currency                    TEXT NOT NULL DEFAULT 'EUR',
            box_included                INTEGER NOT NULL DEFAULT 0,
            proof_of_purchase_available INTEGER NOT NULL DEFAULT 0,
            description                 TEXT NOT NULL DEFAULT '',
            tradeable                   INTEGER NOT NULL DEFAULT 0,
            status                      TEXT NOT NULL DEFAULT 'active',
            -- Authenticity & Anti-Counterfeit cache columns — source of
            -- truth is listing_verifications / risk_assessments below.
            verification_status         TEXT NOT NULL DEFAULT 'unverified',
            risk_band                   TEXT,
            risk_score                  INTEGER,
            verification_version        INTEGER,
            verified_at                 INTEGER,
            created_at                  INTEGER NOT NULL,
            updated_at                  INTEGER
        );

        -- One row per evidence photo. See § D in
        -- docs/AUTHENTICITY_ARCHITECTURE.md for the 12 categories.
        CREATE TABLE IF NOT EXISTS listing_photos (
            id                   TEXT PRIMARY KEY,
            listing_id           TEXT NOT NULL REFERENCES listings(id),
            category             TEXT NOT NULL,
            file_ref             TEXT NOT NULL,
            sha256_hash          TEXT NOT NULL,
            phash                TEXT,
            width                INTEGER,
            height               INTEGER,
            exif_json            TEXT,
            integrity_flags      TEXT NOT NULL DEFAULT '{}',
            seller_entered_code  TEXT,
            position             INTEGER NOT NULL DEFAULT 0,
            created_at           INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_listing_photos_listing ON listing_photos(listing_id);
        CREATE INDEX IF NOT EXISTS idx_listing_photos_sha256 ON listing_photos(sha256_hash);
        CREATE INDEX IF NOT EXISTS idx_listing_photos_phash ON listing_photos(phash);

        -- Admin-only, append-only. Never serialized on a buyer/seller route.
        CREATE TABLE IF NOT EXISTS risk_signals (
            id           TEXT PRIMARY KEY,
            listing_id   TEXT NOT NULL REFERENCES listings(id),
            signal_type  TEXT NOT NULL,
            weight       INTEGER NOT NULL,
            detector     TEXT NOT NULL,
            details      TEXT NOT NULL DEFAULT '{}',
            detected_at  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_risk_signals_listing ON risk_signals(listing_id);
        CREATE TRIGGER IF NOT EXISTS trg_risk_signals_no_update
            BEFORE UPDATE ON risk_signals
        BEGIN SELECT RAISE(ABORT, 'risk_signals is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_risk_signals_no_delete
            BEFORE DELETE ON risk_signals
        BEGIN SELECT RAISE(ABORT, 'risk_signals is append-only'); END;

        CREATE TABLE IF NOT EXISTS risk_assessments (
            id             TEXT PRIMARY KEY,
            listing_id     TEXT NOT NULL REFERENCES listings(id),
            score          INTEGER NOT NULL,
            band           TEXT NOT NULL,
            engine_version INTEGER NOT NULL,
            signal_ids     TEXT NOT NULL DEFAULT '[]',
            computed_at    INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_risk_assessments_listing ON risk_assessments(listing_id);
        CREATE TRIGGER IF NOT EXISTS trg_risk_assessments_no_update
            BEFORE UPDATE ON risk_assessments
        BEGIN SELECT RAISE(ABORT, 'risk_assessments is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_risk_assessments_no_delete
            BEFORE DELETE ON risk_assessments
        BEGIN SELECT RAISE(ABORT, 'risk_assessments is append-only'); END;

        CREATE TABLE IF NOT EXISTS listing_verifications (
            id                    TEXT PRIMARY KEY,
            listing_id            TEXT NOT NULL REFERENCES listings(id),
            status                TEXT NOT NULL,
            method                TEXT NOT NULL,
            reviewer_id           TEXT,
            notes                 TEXT,
            verification_version  INTEGER,
            evidence_snapshot     TEXT NOT NULL DEFAULT '{}',
            created_at            INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_listing_verifications_listing ON listing_verifications(listing_id);
        CREATE TRIGGER IF NOT EXISTS trg_listing_verifications_no_update
            BEFORE UPDATE ON listing_verifications
        BEGIN SELECT RAISE(ABORT, 'listing_verifications is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_listing_verifications_no_delete
            BEFORE DELETE ON listing_verifications
        BEGIN SELECT RAISE(ABORT, 'listing_verifications is append-only'); END;

        CREATE TABLE IF NOT EXISTS manual_reviews (
            id                 TEXT PRIMARY KEY,
            listing_id         TEXT NOT NULL REFERENCES listings(id),
            status             TEXT NOT NULL DEFAULT 'pending',
            opened_reason      TEXT NOT NULL,
            assigned_admin_id  TEXT,
            created_at         INTEGER NOT NULL,
            updated_at         INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_manual_reviews_listing ON manual_reviews(listing_id);
        CREATE INDEX IF NOT EXISTS idx_manual_reviews_status ON manual_reviews(status);

        CREATE TABLE IF NOT EXISTS listing_review_actions (
            id          TEXT PRIMARY KEY,
            review_id   TEXT NOT NULL REFERENCES manual_reviews(id),
            admin_id    TEXT NOT NULL REFERENCES users(id),
            action      TEXT NOT NULL,
            notes       TEXT,
            created_at  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_listing_review_actions_review ON listing_review_actions(review_id);
        CREATE TRIGGER IF NOT EXISTS trg_listing_review_actions_no_update
            BEFORE UPDATE ON listing_review_actions
        BEGIN SELECT RAISE(ABORT, 'listing_review_actions is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_listing_review_actions_no_delete
            BEFORE DELETE ON listing_review_actions
        BEGIN SELECT RAISE(ABORT, 'listing_review_actions is append-only'); END;

        -- Post-purchase "I suspect this isn't authentic" reports. Links
        -- to the existing disputes system (reason='counterfeit_suspected')
        -- rather than duplicating refund/payment logic.
        CREATE TABLE IF NOT EXISTS authenticity_reports (
            id             TEXT PRIMARY KEY,
            dispute_id     TEXT NOT NULL UNIQUE REFERENCES disputes(id),
            order_id       TEXT NOT NULL REFERENCES orders(id),
            listing_id     TEXT NOT NULL REFERENCES listings(id),
            reporter_id    TEXT NOT NULL REFERENCES users(id),
            status         TEXT NOT NULL DEFAULT 'open',
            reviewed_by    TEXT,
            reviewed_at    INTEGER,
            outcome_notes  TEXT,
            created_at     INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_authenticity_reports_listing ON authenticity_reports(listing_id);

        -- Admin-maintained reference prices — see the comment above the
        -- Postgres version of this table in db/schema.sql.
        CREATE TABLE IF NOT EXISTS reference_prices (
            brand               TEXT NOT NULL,
            perfume_name        TEXT NOT NULL,
            size_ml             INTEGER NOT NULL,
            typical_price_cents INTEGER NOT NULL,
            updated_at          INTEGER NOT NULL,
            updated_by          TEXT,
            PRIMARY KEY (brand, perfume_name, size_ml)
        );

        CREATE TABLE IF NOT EXISTS stripe_connect_accounts (
            user_id           TEXT PRIMARY KEY REFERENCES users(id),
            stripe_account_id TEXT UNIQUE NOT NULL,
            charges_enabled   INTEGER NOT NULL DEFAULT 0,
            payouts_enabled   INTEGER NOT NULL DEFAULT 0,
            payout_hold       INTEGER NOT NULL DEFAULT 0,
            updated_at        INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orders (
            id                        TEXT PRIMARY KEY,
            buyer_id                  TEXT NOT NULL REFERENCES users(id),
            seller_id                 TEXT NOT NULL REFERENCES users(id),
            listing_id                TEXT NOT NULL REFERENCES listings(id),
            status                    TEXT NOT NULL DEFAULT 'payment_pending',
            item_price_cents          INTEGER NOT NULL,
            shipping_price_cents      INTEGER NOT NULL DEFAULT 0,
            buyer_protection_fee_cents INTEGER NOT NULL DEFAULT 0,
            tax_cents                 INTEGER NOT NULL DEFAULT 0,
            total_price_cents         INTEGER NOT NULL,
            currency                  TEXT NOT NULL DEFAULT 'EUR',
            stripe_payment_intent_id  TEXT UNIQUE,
            transfer_group            TEXT UNIQUE,
            stripe_transfer_id        TEXT UNIQUE,
            shipping_deadline_at      INTEGER,
            inspection_deadline_at    INTEGER,
            created_at                INTEGER NOT NULL,
            updated_at                INTEGER NOT NULL,
            paid_at                   INTEGER,
            shipped_at                INTEGER,
            delivered_at              INTEGER,
            completed_at              INTEGER,
            cancelled_at              INTEGER,
            CHECK (total_price_cents = item_price_cents + shipping_price_cents
                                        + buyer_protection_fee_cents + tax_cents),
            CHECK (buyer_id <> seller_id)
        );

        CREATE TABLE IF NOT EXISTS order_status_history (
            id          TEXT PRIMARY KEY,
            order_id    TEXT NOT NULL REFERENCES orders(id),
            from_status TEXT NOT NULL,
            to_status   TEXT NOT NULL,
            changed_by  TEXT NOT NULL,
            reason      TEXT,
            created_at  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shipments (
            id               TEXT PRIMARY KEY,
            order_id         TEXT NOT NULL UNIQUE REFERENCES orders(id),
            carrier          TEXT NOT NULL,
            tracking_number  TEXT NOT NULL,
            tracking_url     TEXT,
            shipped_at       INTEGER,
            delivered_at     INTEGER,
            created_at       INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS disputes (
            id               TEXT PRIMARY KEY,
            order_id         TEXT NOT NULL REFERENCES orders(id),
            opened_by        TEXT NOT NULL REFERENCES users(id),
            reason           TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'open',
            description      TEXT NOT NULL DEFAULT '',
            response_due_at  INTEGER,
            resolution_notes TEXT,
            resolved_by      TEXT,
            created_at       INTEGER NOT NULL,
            resolved_at      INTEGER
        );

        CREATE TABLE IF NOT EXISTS dispute_evidence (
            id            TEXT PRIMARY KEY,
            dispute_id    TEXT NOT NULL REFERENCES disputes(id),
            submitted_by  TEXT NOT NULL REFERENCES users(id),
            evidence_type TEXT NOT NULL,
            file_ref      TEXT,
            text_value    TEXT,
            created_at    INTEGER NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS trg_dispute_evidence_no_update
            BEFORE UPDATE ON dispute_evidence
        BEGIN SELECT RAISE(ABORT, 'dispute_evidence is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_dispute_evidence_no_delete
            BEFORE DELETE ON dispute_evidence
        BEGIN SELECT RAISE(ABORT, 'dispute_evidence is append-only'); END;

        CREATE TABLE IF NOT EXISTS dispute_messages (
            id          TEXT PRIMARY KEY,
            dispute_id  TEXT NOT NULL REFERENCES disputes(id),
            sender_id   TEXT NOT NULL REFERENCES users(id),
            body        TEXT NOT NULL,
            created_at  INTEGER NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS trg_dispute_messages_no_update
            BEFORE UPDATE ON dispute_messages
        BEGIN SELECT RAISE(ABORT, 'dispute_messages is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_dispute_messages_no_delete
            BEFORE DELETE ON dispute_messages
        BEGIN SELECT RAISE(ABORT, 'dispute_messages is append-only'); END;

        CREATE TABLE IF NOT EXISTS dispute_admin_notes (
            id          TEXT PRIMARY KEY,
            dispute_id  TEXT NOT NULL REFERENCES disputes(id),
            admin_id    TEXT NOT NULL REFERENCES users(id),
            note        TEXT NOT NULL,
            created_at  INTEGER NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS trg_dispute_admin_notes_no_update
            BEFORE UPDATE ON dispute_admin_notes
        BEGIN SELECT RAISE(ABORT, 'dispute_admin_notes is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_dispute_admin_notes_no_delete
            BEFORE DELETE ON dispute_admin_notes
        BEGIN SELECT RAISE(ABORT, 'dispute_admin_notes is append-only'); END;

        CREATE TABLE IF NOT EXISTS dispute_events (
            id          TEXT PRIMARY KEY,
            dispute_id  TEXT NOT NULL REFERENCES disputes(id),
            event_type  TEXT NOT NULL,
            actor_type  TEXT NOT NULL,
            actor_id    TEXT,
            payload     TEXT NOT NULL DEFAULT '{}',
            created_at  INTEGER NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS trg_dispute_events_no_update
            BEFORE UPDATE ON dispute_events
        BEGIN SELECT RAISE(ABORT, 'dispute_events is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_dispute_events_no_delete
            BEFORE DELETE ON dispute_events
        BEGIN SELECT RAISE(ABORT, 'dispute_events is append-only'); END;

        CREATE TABLE IF NOT EXISTS refunds (
            id               TEXT PRIMARY KEY,
            order_id         TEXT NOT NULL REFERENCES orders(id),
            stripe_refund_id TEXT UNIQUE NOT NULL,
            amount_cents     INTEGER NOT NULL,
            reason           TEXT,
            status           TEXT NOT NULL,
            initiated_by     TEXT NOT NULL,
            idempotency_key  TEXT UNIQUE,
            created_at       INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS transfer_reversals (
            id                  TEXT PRIMARY KEY,
            order_id            TEXT NOT NULL REFERENCES orders(id),
            stripe_reversal_id  TEXT UNIQUE NOT NULL,
            amount_cents        INTEGER NOT NULL,
            reason              TEXT,
            created_at          INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chargebacks (
            id                 TEXT PRIMARY KEY,
            order_id           TEXT NOT NULL REFERENCES orders(id),
            stripe_dispute_id  TEXT UNIQUE NOT NULL,
            amount_cents       INTEGER NOT NULL,
            reason             TEXT,
            status             TEXT NOT NULL,
            transfer_reversed  INTEGER NOT NULL DEFAULT 0,
            created_at         INTEGER NOT NULL,
            resolved_at        INTEGER
        );

        CREATE TABLE IF NOT EXISTS payouts (
            id               TEXT PRIMARY KEY,
            seller_id        TEXT NOT NULL REFERENCES users(id),
            stripe_payout_id TEXT UNIQUE NOT NULL,
            amount_cents     INTEGER NOT NULL,
            status           TEXT NOT NULL,
            initiated_at     INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notifications (
            id         TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL REFERENCES users(id),
            type       TEXT NOT NULL,
            payload    TEXT NOT NULL DEFAULT '{}',
            read_at    INTEGER,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS webhook_events (
            id              TEXT PRIMARY KEY,
            stripe_event_id TEXT UNIQUE NOT NULL,
            event_type      TEXT NOT NULL,
            payload         TEXT NOT NULL,
            processed_at    INTEGER,
            created_at      INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id          TEXT PRIMARY KEY,
            actor_type  TEXT NOT NULL,
            actor_id    TEXT,
            action      TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id   TEXT,
            metadata    TEXT NOT NULL DEFAULT '{}',
            created_at  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS risk_flags (
            id          TEXT PRIMARY KEY,
            user_id     TEXT,
            listing_id  TEXT,
            order_id    TEXT,
            flag_type   TEXT NOT NULL,
            severity    TEXT NOT NULL,
            details     TEXT NOT NULL DEFAULT '{}',
            status      TEXT NOT NULL DEFAULT 'open',
            created_at  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id          TEXT PRIMARY KEY,
            order_id    TEXT NOT NULL UNIQUE REFERENCES orders(id),
            reviewer_id TEXT NOT NULL REFERENCES users(id),
            reviewee_id TEXT NOT NULL REFERENCES users(id),
            rating      INTEGER NOT NULL,
            comment     TEXT,
            created_at  INTEGER NOT NULL,
            CHECK (reviewer_id <> reviewee_id)
        );

        CREATE TABLE IF NOT EXISTS fee_rules (
            fee_key         TEXT PRIMARY KEY,
            percentage_bps  INTEGER NOT NULL DEFAULT 0,
            fixed_cents     INTEGER NOT NULL DEFAULT 0,
            updated_at      INTEGER NOT NULL,
            updated_by      TEXT
        );

        CREATE TABLE IF NOT EXISTS fee_rule_history (
            id             TEXT PRIMARY KEY,
            fee_key        TEXT NOT NULL,
            percentage_bps INTEGER NOT NULL,
            fixed_cents    INTEGER NOT NULL,
            changed_by     TEXT,
            changed_at     INTEGER NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS trg_fee_rule_history_no_update
            BEFORE UPDATE ON fee_rule_history
        BEGIN SELECT RAISE(ABORT, 'fee_rule_history is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS trg_fee_rule_history_no_delete
            BEFORE DELETE ON fee_rule_history
        BEGIN SELECT RAISE(ABORT, 'fee_rule_history is append-only'); END;

        CREATE TABLE IF NOT EXISTS platform_config (
            config_key   TEXT PRIMARY KEY,
            config_value TEXT NOT NULL,
            updated_at   INTEGER NOT NULL,
            updated_by   TEXT
        );

        CREATE TABLE IF NOT EXISTS order_status_transitions (
            from_status TEXT NOT NULL,
            to_status   TEXT NOT NULL,
            PRIMARY KEY (from_status, to_status)
        );
        """
    )

    # Seed the transition whitelist + default fee/config rows, idempotently.
    for frm, to in ORDER_STATUS_TRANSITIONS:
        conn.execute(
            "INSERT OR IGNORE INTO order_status_transitions (from_status, to_status) VALUES (?, ?)",
            (frm, to),
        )

    if conn.execute("SELECT COUNT(*) c FROM fee_rules").fetchone()["c"] == 0:
        conn.execute(
            "INSERT INTO fee_rules (fee_key, percentage_bps, fixed_cents, updated_at) "
            "VALUES ('buyer_protection', 500, 95, ?)",
            (now_ts(),),
        )

    defaults = {
        "shipping_flat_rate_cents": "495",
        "tax_rate_bps": "0",
        "inspection_period_hours": "72",
        "shipping_deadline_hours": "120",
        "dispute_response_hours": "72",
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO platform_config (config_key, config_value, updated_at) VALUES (?, ?, ?)",
            (key, value, now_ts()),
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Order status transitions — application-level guard (a SECOND layer;
# the same whitelist also lives in order_status_transitions, but SQLite
# triggers can't easily do the same cross-row lookup as Postgres inside
# a BEFORE UPDATE trigger without recursion pitfalls, so here the
# invariant is enforced in the one function every status change must
# go through: transition_order_status() in app/orders/state_machine.py)
def is_valid_transition(conn, from_status, to_status):
    row = conn.execute(
        "SELECT 1 FROM order_status_transitions WHERE from_status = ? AND to_status = ?",
        (from_status, to_status),
    ).fetchone()
    return row is not None
