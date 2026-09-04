-- SecondScent platform — database schema (PostgreSQL 14+)
--
-- Design notes:
--  * Money is always stored as integer cents (never floating point).
--  * Foreign keys use ON DELETE RESTRICT by default for anything touching
--    money or audit history — we never want a cascade delete to silently
--    wipe a financial record.
--  * The order status machine is enforced at the DATABASE level (not just
--    in application code) via order_status_transitions + a trigger, so a
--    bug in the app cannot write an impossible status change.

CREATE EXTENSION IF NOT EXISTS pgcrypto; -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS citext;   -- case-insensitive email column

-- ---------------------------------------------------------------------
-- Users & verification
-- ---------------------------------------------------------------------

CREATE TABLE users (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email               CITEXT UNIQUE NOT NULL,
    password_hash       TEXT NOT NULL,
    display_name        TEXT NOT NULL,
    country             CHAR(2),
    locale              TEXT NOT NULL DEFAULT 'nl',
    is_buyer            BOOLEAN NOT NULL DEFAULT TRUE,
    is_seller           BOOLEAN NOT NULL DEFAULT FALSE,
    account_status      TEXT NOT NULL DEFAULT 'active'
                            CHECK (account_status IN ('active','restricted','suspended','banned')),
    risk_score          SMALLINT NOT NULL DEFAULT 0,
    last_login_at       TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_users_account_status ON users(account_status);

-- Explicitly separate from `users`: a verification claim only counts as
-- "geverifieerd" if a row exists here with status = 'verified'. The
-- frontend must never render a verified badge based on user-supplied data
-- alone (e.g. a self-reported phone number is NOT verified until this
-- table says so).
CREATE TABLE user_verifications (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    verification_type   TEXT NOT NULL
                            CHECK (verification_type IN ('email','phone','id_document','stripe_kyc')),
    status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','verified','rejected')),
    evidence_ref        TEXT,               -- pointer to stored evidence, never raw PII inline
    verified_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, verification_type)
);

CREATE TABLE stripe_connect_accounts (
    user_id             UUID PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
    stripe_account_id   TEXT UNIQUE NOT NULL,
    charges_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    payouts_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    onboarding_status   TEXT NOT NULL DEFAULT 'started'
                            CHECK (onboarding_status IN ('started','requirements_due','complete','disabled')),
    requirements_due    JSONB NOT NULL DEFAULT '[]',
    payout_hold         BOOLEAN NOT NULL DEFAULT FALSE, -- our own kill switch, independent of Stripe
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Listings
-- ---------------------------------------------------------------------

CREATE TABLE listings (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id                   UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    brand                       TEXT NOT NULL,
    perfume_name                TEXT NOT NULL,
    variant_concentration       TEXT,               -- eau de parfum / extrait / ...
    size_ml                     INTEGER NOT NULL CHECK (size_ml > 0),
    original_size_ml            INTEGER NOT NULL CHECK (original_size_ml > 0),
    estimated_remaining_percent SMALLINT NOT NULL CHECK (estimated_remaining_percent BETWEEN 0 AND 100),
    condition                   TEXT NOT NULL
                                    CHECK (condition IN ('new_sealed','new_decanted','used_like_new','used_good','used_fair')),
    batch_code                  TEXT,
    barcode                     TEXT,
    purchase_source              TEXT,
    purchase_date                DATE,
    asking_price_cents          INTEGER NOT NULL CHECK (asking_price_cents > 0),
    currency                    CHAR(3) NOT NULL DEFAULT 'EUR',
    box_included                BOOLEAN NOT NULL DEFAULT FALSE,
    proof_of_purchase_available BOOLEAN NOT NULL DEFAULT FALSE,
    description                 TEXT NOT NULL DEFAULT '',
    tradeable                   BOOLEAN NOT NULL DEFAULT FALSE,
    status                       TEXT NOT NULL DEFAULT 'draft'
                                    CHECK (status IN ('draft','active','paused','sold','removed_by_admin')),
    -- Authenticity & Anti-Counterfeit (see docs/AUTHENTICITY_ARCHITECTURE.md).
    -- These four columns are a CACHE of the latest listing_verifications /
    -- risk_assessments rows, purely for fast reads — never the source of
    -- truth, and never the place `secondscent_verified` gets decided.
    verification_status         TEXT NOT NULL DEFAULT 'unverified'
                                    CHECK (verification_status IN (
                                        'unverified', 'automated_checks_completed',
                                        'additional_verification_required', 'manual_review',
                                        'secondscent_verified', 'rejected'
                                    )),
    risk_band                   TEXT CHECK (risk_band IN ('low','medium','high')),
    risk_score                  INTEGER,
    verification_version        INTEGER,
    verified_at                  TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_listings_seller ON listings(seller_id);
CREATE INDEX idx_listings_status ON listings(status);
CREATE INDEX idx_listings_verification_status ON listings(verification_status);

-- One row per required-or-optional evidence photo (see § D in
-- docs/AUTHENTICITY_ARCHITECTURE.md for the 12 categories). `file_ref` is
-- a path into the private evidence store, NEVER a public URL — photos are
-- only ever served through an authenticated, authorized endpoint.
CREATE TABLE listing_photos (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id          UUID NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    category            TEXT NOT NULL CHECK (category IN (
                            'bottle_front','bottle_back','bottle_bottom','nozzle','cap',
                            'box_front','box_back','box_bottom',
                            'batch_code_bottle','batch_code_packaging','barcode',
                            'proof_of_purchase'
                        )),
    file_ref            TEXT NOT NULL,
    sha256_hash         TEXT NOT NULL,
    phash               TEXT,            -- perceptual (difference) hash, hex
    width                INTEGER,
    height               INTEGER,
    exif_json            TEXT,            -- JSON; internal use only, never re-served to other users
    integrity_flags      TEXT NOT NULL DEFAULT '{}', -- JSON result of photo_integrity.analyze()
    seller_entered_code  TEXT,            -- what the seller typed for batch_code_*/barcode categories
    position             SMALLINT NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_listing_photos_listing ON listing_photos(listing_id);
CREATE INDEX idx_listing_photos_sha256 ON listing_photos(sha256_hash);
CREATE INDEX idx_listing_photos_phash ON listing_photos(phash);

-- ---------------------------------------------------------------------
-- Orders — the state machine
-- ---------------------------------------------------------------------

CREATE TABLE orders (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    buyer_id                 UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    seller_id                UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    listing_id               UUID NOT NULL REFERENCES listings(id) ON DELETE RESTRICT,
    status                   TEXT NOT NULL DEFAULT 'payment_pending',
    item_price_cents         INTEGER NOT NULL CHECK (item_price_cents > 0),
    shipping_price_cents     INTEGER NOT NULL DEFAULT 0 CHECK (shipping_price_cents >= 0),
    buyer_protection_fee_cents INTEGER NOT NULL DEFAULT 0 CHECK (buyer_protection_fee_cents >= 0),
    tax_cents                INTEGER NOT NULL DEFAULT 0 CHECK (tax_cents >= 0),
    total_price_cents        INTEGER NOT NULL CHECK (total_price_cents > 0),
    currency                 CHAR(3) NOT NULL DEFAULT 'EUR',
    stripe_payment_intent_id TEXT UNIQUE,
    transfer_group            TEXT UNIQUE,
    stripe_transfer_id        TEXT UNIQUE,
    shipping_deadline_at      TIMESTAMPTZ,
    inspection_deadline_at    TIMESTAMPTZ,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at                   TIMESTAMPTZ,
    shipped_at                TIMESTAMPTZ,
    delivered_at              TIMESTAMPTZ,
    completed_at              TIMESTAMPTZ,
    cancelled_at               TIMESTAMPTZ,

    CONSTRAINT chk_order_status CHECK (status IN (
        'payment_pending','paid','awaiting_shipment','shipped','delivered',
        'inspection_period','completed','issue_reported','under_review',
        'return_required','return_shipped','refunded','cancelled'
    )),
    CONSTRAINT chk_total_matches CHECK (
        total_price_cents = item_price_cents + shipping_price_cents
                             + buyer_protection_fee_cents + tax_cents
    ),
    CONSTRAINT chk_buyer_not_seller CHECK (buyer_id <> seller_id)
);
CREATE INDEX idx_orders_buyer ON orders(buyer_id);
CREATE INDEX idx_orders_seller ON orders(seller_id);
CREATE INDEX idx_orders_status ON orders(status);

-- Whitelist of allowed status transitions. This is the source of truth
-- the trigger below checks against — editing the state machine means
-- editing this table's contents, not application code scattered across
-- the codebase.
CREATE TABLE order_status_transitions (
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    PRIMARY KEY (from_status, to_status)
);

INSERT INTO order_status_transitions (from_status, to_status) VALUES
    ('payment_pending', 'paid'),
    ('payment_pending', 'cancelled'),
    ('paid',            'awaiting_shipment'),
    ('paid',            'cancelled'),
    ('awaiting_shipment','shipped'),
    ('awaiting_shipment','cancelled'),
    ('shipped',         'delivered'),
    ('shipped',         'issue_reported'),
    ('delivered',       'inspection_period'),
    ('inspection_period','completed'),
    ('inspection_period','issue_reported'),
    ('issue_reported',  'under_review'),
    ('under_review',    'return_required'),
    ('under_review',    'completed'),
    ('under_review',    'refunded'),
    ('return_required', 'return_shipped'),
    ('return_shipped',  'refunded'),
    -- A Stripe chargeback can force an in-flight (not-yet-completed) order
    -- into review at any point after payment. A chargeback on an already
    -- COMPLETED order is deliberately NOT modeled as an order-status
    -- transition (see `chargebacks` table below) — we don't retroactively
    -- reopen a finished marketplace transaction's own state machine days
    -- or months later; the financial dispute is tracked separately.
    ('paid',              'under_review'),
    ('awaiting_shipment',  'under_review'),
    ('shipped',            'under_review'),
    ('inspection_period',  'under_review');

CREATE OR REPLACE FUNCTION enforce_order_status_transition()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT EXISTS (
            SELECT 1 FROM order_status_transitions
            WHERE from_status = OLD.status AND to_status = NEW.status
        ) THEN
            RAISE EXCEPTION
                'Invalid order status transition: % -> % (order %)',
                OLD.status, NEW.status, OLD.id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_enforce_order_status_transition
    BEFORE UPDATE ON orders
    FOR EACH ROW
    EXECUTE FUNCTION enforce_order_status_transition();

-- Append-only audit trail of every status change (written by the
-- application, in the same transaction as the UPDATE, so it always
-- matches — the trigger above only guards validity, it doesn't log).
CREATE TABLE order_status_history (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id    UUID NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    changed_by  TEXT NOT NULL, -- user id, 'system', or 'admin:<admin_id>'
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_order_status_history_order ON order_status_history(order_id);

CREATE TABLE shipments (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id             UUID NOT NULL UNIQUE REFERENCES orders(id) ON DELETE RESTRICT,
    carrier              TEXT NOT NULL,
    tracking_number      TEXT NOT NULL,
    tracking_url         TEXT,
    shipped_at           TIMESTAMPTZ,
    estimated_delivery_at TIMESTAMPTZ,
    delivered_at         TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Disputes, refunds, transfer reversals, payouts
-- ---------------------------------------------------------------------

CREATE TABLE disputes (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id         UUID NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    opened_by        UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    reason           TEXT NOT NULL CHECK (reason IN (
                        'not_received', 'damaged', 'wrong_item',
                        'significantly_not_as_described',
                        'counterfeit_suspected', 'missing_parts_or_packaging'
                     )),
    status           TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','evidence_requested','under_review','resolved_buyer','resolved_seller','resolved_split')),
    description      TEXT NOT NULL DEFAULT '',
    response_due_at  TIMESTAMPTZ,   -- seller/other party must respond by this time
    resolution_notes TEXT,
    resolved_by      UUID REFERENCES users(id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ
);
CREATE INDEX idx_disputes_order ON disputes(order_id);
CREATE INDEX idx_disputes_status ON disputes(status);

-- Evidence is APPEND-ONLY: neither party may edit or remove evidence,
-- including their own (prevents tampering with the record after the
-- fact) or the other side's. Enforced twice: no UPDATE/DELETE route is
-- ever exposed by the app, AND a trigger blocks it at the database
-- level regardless of how the row is reached.
CREATE TABLE dispute_evidence (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispute_id     UUID NOT NULL REFERENCES disputes(id) ON DELETE RESTRICT,
    submitted_by   UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    evidence_type  TEXT NOT NULL CHECK (evidence_type IN (
                        'photo_item','photo_box','photo_packaging','photo_shipping_label',
                        'video','description','batch_code','proof_of_purchase','other'
                     )),
    file_ref       TEXT,           -- pointer to object storage; NULL for text-only evidence
    text_value     TEXT,           -- e.g. the batch code itself, or a written description
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_dispute_evidence_dispute ON dispute_evidence(dispute_id);

CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'This record is append-only and cannot be changed or deleted (%: %)',
        TG_TABLE_NAME, COALESCE(OLD.id, NEW.id) USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_dispute_evidence_immutable
    BEFORE UPDATE OR DELETE ON dispute_evidence
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Buyer <-> seller messages tied to a case. Also append-only, and always
-- visible to admin — this is the paper trail a resolution is based on.
CREATE TABLE dispute_messages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispute_id  UUID NOT NULL REFERENCES disputes(id) ON DELETE RESTRICT,
    sender_id   UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    body        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_dispute_messages_dispute ON dispute_messages(dispute_id);

CREATE TRIGGER trg_dispute_messages_immutable
    BEFORE UPDATE OR DELETE ON dispute_messages
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Internal admin notes — NEVER exposed on any buyer/seller-facing route.
CREATE TABLE dispute_admin_notes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispute_id  UUID NOT NULL REFERENCES disputes(id) ON DELETE RESTRICT,
    admin_id    UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    note        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_dispute_admin_notes_dispute ON dispute_admin_notes(dispute_id);

CREATE TRIGGER trg_dispute_admin_notes_immutable
    BEFORE UPDATE OR DELETE ON dispute_admin_notes
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- The full timeline of a case (opened, evidence added, status changed,
-- resolved, ...), independent of the three tables above so the UI can
-- render one chronological feed without a UNION across them.
CREATE TABLE dispute_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispute_id  UUID NOT NULL REFERENCES disputes(id) ON DELETE RESTRICT,
    event_type  TEXT NOT NULL,
    actor_type  TEXT NOT NULL CHECK (actor_type IN ('buyer','seller','admin','system')),
    actor_id    UUID,
    payload     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_dispute_events_dispute ON dispute_events(dispute_id);

CREATE TRIGGER trg_dispute_events_immutable
    BEFORE UPDATE OR DELETE ON dispute_events
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Real Stripe chargebacks (card-network disputes), which are distinct
-- from our own buyer-initiated `disputes` case system above: they can
-- arrive from Stripe up to ~120 days after a charge, even against an
-- order our own state machine already marked `completed`. Tracked
-- separately rather than forced through the order state machine.
CREATE TABLE chargebacks (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id           UUID NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    stripe_dispute_id  TEXT UNIQUE NOT NULL,
    amount_cents       INTEGER NOT NULL CHECK (amount_cents > 0),
    reason             TEXT,
    status             TEXT NOT NULL CHECK (status IN (
                            'needs_response','under_review','won','lost'
                        )),
    transfer_reversed  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at        TIMESTAMPTZ
);
CREATE INDEX idx_chargebacks_order ON chargebacks(order_id);

CREATE TABLE refunds (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id         UUID NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    stripe_refund_id TEXT UNIQUE NOT NULL,
    amount_cents     INTEGER NOT NULL CHECK (amount_cents > 0),
    reason           TEXT,
    status           TEXT NOT NULL CHECK (status IN ('pending','succeeded','failed')),
    initiated_by     TEXT NOT NULL, -- user id, 'system', or 'admin:<admin_id>'
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE transfer_reversals (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id          UUID NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    stripe_reversal_id TEXT UNIQUE NOT NULL,
    amount_cents      INTEGER NOT NULL CHECK (amount_cents > 0),
    reason            TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE payouts (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id        UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    stripe_payout_id TEXT UNIQUE NOT NULL,
    amount_cents     INTEGER NOT NULL CHECK (amount_cents > 0),
    status           TEXT NOT NULL CHECK (status IN ('pending','in_transit','paid','failed','cancelled')),
    initiated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    arrived_at       TIMESTAMPTZ
);
CREATE INDEX idx_payouts_seller ON payouts(seller_id);

-- ---------------------------------------------------------------------
-- Notifications, webhooks, audit, risk, reviews
-- ---------------------------------------------------------------------

CREATE TABLE notifications (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type       TEXT NOT NULL CHECK (type IN (
                    'sale','payment_received','shipping_deadline','shipped','delivered',
                    'inspection_period_ending','issue_reported','evidence_requested',
                    'return','refund','payout'
                )),
    payload    JSONB NOT NULL DEFAULT '{}',
    read_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_notifications_user_unread ON notifications(user_id) WHERE read_at IS NULL;

-- Idempotency + audit layer for incoming Stripe webhooks. A row is
-- inserted BEFORE processing; the unique constraint on stripe_event_id
-- is what makes double-delivery safe.
CREATE TABLE webhook_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stripe_event_id TEXT UNIQUE NOT NULL,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL,
    processed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_logs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_type  TEXT NOT NULL CHECK (actor_type IN ('user','admin','system')),
    actor_id    UUID,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   UUID,
    metadata    JSONB NOT NULL DEFAULT '{}',
    ip_address  INET,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_logs_entity ON audit_logs(entity_type, entity_id);

CREATE TABLE risk_flags (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    listing_id  UUID REFERENCES listings(id) ON DELETE CASCADE,
    order_id    UUID REFERENCES orders(id) ON DELETE CASCADE,
    flag_type   TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('low','medium','high')),
    details     JSONB NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','reviewed','dismissed')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_risk_flag_target CHECK (
        user_id IS NOT NULL OR listing_id IS NOT NULL OR order_id IS NOT NULL
    )
);
CREATE INDEX idx_risk_flags_status ON risk_flags(status);

-- A review can only ever exist for a completed order, and only one per
-- order — this is the structural guard against fake reviews.
CREATE TABLE reviews (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id    UUID NOT NULL UNIQUE REFERENCES orders(id) ON DELETE RESTRICT,
    reviewer_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    reviewee_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    rating      SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_review_not_self CHECK (reviewer_id <> reviewee_id)
);
CREATE INDEX idx_reviews_reviewee ON reviews(reviewee_id);

-- ---------------------------------------------------------------------
-- Admin-configurable fee structure & platform settings
--
-- Buyer-protection fee (and any future fee) is NEVER hardcoded in
-- application code: `fee_rules` holds the current rule per fee_key,
-- `fee_rule_history` is an append-only audit trail of every change
-- (who changed what, when, from what to what). Amounts are integer
-- basis points / cents throughout — never floats.
-- ---------------------------------------------------------------------

CREATE TABLE fee_rules (
    fee_key           TEXT PRIMARY KEY,     -- e.g. 'buyer_protection'
    percentage_bps    INTEGER NOT NULL DEFAULT 0 CHECK (percentage_bps >= 0), -- 1% = 100 bps
    fixed_cents        INTEGER NOT NULL DEFAULT 0 CHECK (fixed_cents >= 0),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by         UUID REFERENCES users(id)
);

CREATE TABLE fee_rule_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fee_key         TEXT NOT NULL,
    percentage_bps  INTEGER NOT NULL,
    fixed_cents     INTEGER NOT NULL,
    changed_by      UUID REFERENCES users(id),
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_fee_rule_history_immutable
    BEFORE UPDATE OR DELETE ON fee_rule_history
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Simple typed key/value settings: shipping_flat_rate_cents,
-- tax_rate_bps, inspection_period_hours, shipping_deadline_hours,
-- dispute_response_hours, ... — all admin-editable without a deploy.
CREATE TABLE platform_config (
    config_key   TEXT PRIMARY KEY,
    config_value TEXT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by   UUID REFERENCES users(id)
);

INSERT INTO fee_rules (fee_key, percentage_bps, fixed_cents) VALUES
    ('buyer_protection', 500, 95);   -- default: 5% + €0.95, admin-editable

INSERT INTO platform_config (config_key, config_value) VALUES
    ('shipping_flat_rate_cents', '495'),
    ('tax_rate_bps', '0'),
    ('inspection_period_hours', '72'),
    ('shipping_deadline_hours', '120'),
    ('dispute_response_hours', '72');

-- =======================================================================
-- Authenticity & Anti-Counterfeit
-- See docs/AUTHENTICITY_ARCHITECTURE.md for the full design. Every
-- append-only table here uses the same forbid_mutation() trigger defined
-- above for dispute_evidence etc.
-- =======================================================================

-- One row per detected fraud/inconsistency signal. Admin-only — NEVER
-- serialized on any buyer/seller-facing route. Weight and rule internals
-- live in app/authenticity/risk_engine.py, not in the database, so they
-- can evolve without a migration; `detector` records which rule version
-- produced the row for later audits.
CREATE TABLE risk_signals (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id   UUID NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    signal_type  TEXT NOT NULL,
    weight       INTEGER NOT NULL CHECK (weight >= 0),
    detector     TEXT NOT NULL,      -- e.g. 'rule:batch_code_mismatch:v1'
    details      JSONB NOT NULL DEFAULT '{}',
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_risk_signals_listing ON risk_signals(listing_id);
CREATE TRIGGER trg_risk_signals_immutable
    BEFORE UPDATE OR DELETE ON risk_signals
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- One row per (re)computation of a listing's risk score — history, not
-- just current state, so "why was this medium risk on Tuesday" stays
-- answerable. listings.risk_band/risk_score cache the latest row.
CREATE TABLE risk_assessments (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id     UUID NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    score          INTEGER NOT NULL CHECK (score >= 0),
    band           TEXT NOT NULL CHECK (band IN ('low','medium','high')),
    engine_version INTEGER NOT NULL,
    signal_ids     JSONB NOT NULL DEFAULT '[]',
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_risk_assessments_listing ON risk_assessments(listing_id);
CREATE TRIGGER trg_risk_assessments_immutable
    BEFORE UPDATE OR DELETE ON risk_assessments
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Full history of a listing's verification_status. The ONLY status that
-- may ever appear here with method='manual' is the one an admin actually
-- chose — app/authenticity/verification.py is the single write path.
CREATE TABLE listing_verifications (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id            UUID NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    status                TEXT NOT NULL CHECK (status IN (
                                'unverified', 'automated_checks_completed',
                                'additional_verification_required', 'manual_review',
                                'secondscent_verified', 'rejected'
                            )),
    method                TEXT NOT NULL CHECK (method IN ('automated','manual','system')),
    reviewer_id           UUID REFERENCES users(id),
    notes                 TEXT,
    verification_version  INTEGER,
    evidence_snapshot     JSONB NOT NULL DEFAULT '{}',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_listing_verifications_listing ON listing_verifications(listing_id);
CREATE TRIGGER trg_listing_verifications_immutable
    BEFORE UPDATE OR DELETE ON listing_verifications
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- The manual-review work queue. Unlike the tables above, THIS one is
-- mutable (its whole point is to change status as work happens) — the
-- append-only audit trail of *how* it changed lives in
-- listing_review_actions right below.
CREATE TABLE manual_reviews (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    listing_id     UUID NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    status         TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','in_review','decided')),
    opened_reason  TEXT NOT NULL,
    assigned_admin_id UUID REFERENCES users(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_manual_reviews_listing ON manual_reviews(listing_id);
CREATE INDEX idx_manual_reviews_status ON manual_reviews(status);

CREATE TABLE listing_review_actions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    review_id   UUID NOT NULL REFERENCES manual_reviews(id) ON DELETE CASCADE,
    admin_id    UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    action      TEXT NOT NULL CHECK (action IN (
                    'approve','request_more_evidence','reject','escalate'
                )),
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_listing_review_actions_review ON listing_review_actions(review_id);
CREATE TRIGGER trg_listing_review_actions_immutable
    BEFORE UPDATE OR DELETE ON listing_review_actions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- A buyer's post-purchase "I suspect this isn't authentic" report.
-- Reuses the existing disputes/dispute_evidence machinery for the
-- underlying case (reason='counterfeit_suspected', already a valid
-- disputes.reason value) — this table adds the structured link to the
-- original listing's evidence for reviewer comparison, without
-- duplicating payment/refund logic that already exists on disputes.
CREATE TABLE authenticity_reports (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispute_id    UUID NOT NULL UNIQUE REFERENCES disputes(id) ON DELETE CASCADE,
    order_id      UUID NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    listing_id    UUID NOT NULL REFERENCES listings(id) ON DELETE RESTRICT,
    reporter_id   UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    status        TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open','reviewed','confirmed_counterfeit','not_counterfeit')),
    reviewed_by   UUID REFERENCES users(id),
    reviewed_at   TIMESTAMPTZ,
    outcome_notes TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_authenticity_reports_listing ON authenticity_reports(listing_id);

-- Admin-maintained reference prices, used only as one input to the
-- price_far_below_reference risk signal (a listing far below the going
-- rate for that exact brand/perfume/size is one weak fraud indicator
-- among many — see docs/AUTHENTICITY_ARCHITECTURE.md § E). No entry for
-- a given brand/perfume/size simply means that signal doesn't fire —
-- this is deliberately NOT a market-data integration.
CREATE TABLE reference_prices (
    brand               TEXT NOT NULL,
    perfume_name        TEXT NOT NULL,
    size_ml             INTEGER NOT NULL,
    typical_price_cents INTEGER NOT NULL CHECK (typical_price_cents > 0),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by          UUID REFERENCES users(id),
    PRIMARY KEY (brand, perfume_name, size_ml)
);
