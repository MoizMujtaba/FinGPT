-- Finogrid Agent Ledger — Schema Migration 004
-- Audit fixes: correct timestamp column types, add mandate_id to micro_transactions.
-- Additive only: no columns removed; existing String columns altered to TIMESTAMPTZ.

-- ── Fix timestamp columns on agent_kya ───────────────────────────────────────

ALTER TABLE agent_kya
    ALTER COLUMN validator_expires_at TYPE TIMESTAMPTZ USING
        CASE WHEN validator_expires_at IS NULL OR validator_expires_at = '' THEN NULL
             ELSE validator_expires_at::TIMESTAMPTZ END,
    ALTER COLUMN validated_at TYPE TIMESTAMPTZ USING
        CASE WHEN validated_at IS NULL OR validated_at = '' THEN NULL
             ELSE validated_at::TIMESTAMPTZ END,
    ALTER COLUMN last_reviewed_at TYPE TIMESTAMPTZ USING
        CASE WHEN last_reviewed_at IS NULL OR last_reviewed_at = '' THEN NULL
             ELSE last_reviewed_at::TIMESTAMPTZ END,
    ALTER COLUMN submitted_at TYPE TIMESTAMPTZ USING
        CASE WHEN submitted_at IS NULL OR submitted_at = '' THEN NULL
             ELSE submitted_at::TIMESTAMPTZ END;

-- ── Fix timestamp columns on agent_wallets ────────────────────────────────────

ALTER TABLE agent_wallets
    ALTER COLUMN daily_reset_at TYPE TIMESTAMPTZ USING
        CASE WHEN daily_reset_at IS NULL OR daily_reset_at = '' THEN NULL
             ELSE daily_reset_at::TIMESTAMPTZ END,
    ALTER COLUMN expires_at TYPE TIMESTAMPTZ USING
        CASE WHEN expires_at IS NULL OR expires_at = '' THEN NULL
             ELSE expires_at::TIMESTAMPTZ END;

-- ── Fix timestamp columns on payment_intents ──────────────────────────────────

ALTER TABLE payment_intents
    ALTER COLUMN expires_at TYPE TIMESTAMPTZ USING expires_at::TIMESTAMPTZ;

-- ── Fix timestamp columns on micro_transactions ───────────────────────────────

ALTER TABLE micro_transactions
    ALTER COLUMN on_chain_confirmed_at TYPE TIMESTAMPTZ USING
        CASE WHEN on_chain_confirmed_at IS NULL OR on_chain_confirmed_at = '' THEN NULL
             ELSE on_chain_confirmed_at::TIMESTAMPTZ END,
    ALTER COLUMN settled_at TYPE TIMESTAMPTZ USING
        CASE WHEN settled_at IS NULL OR settled_at = '' THEN NULL
             ELSE settled_at::TIMESTAMPTZ END;

-- ── Add mandate_id to micro_transactions ─────────────────────────────────────
-- Links each micropayment to the authorising Mandate for compliance tracing.

ALTER TABLE micro_transactions
    ADD COLUMN IF NOT EXISTS mandate_id UUID REFERENCES mandates(id);

CREATE INDEX IF NOT EXISTS idx_micro_transactions_mandate_id
    ON micro_transactions (mandate_id);

COMMENT ON COLUMN micro_transactions.mandate_id IS
    'The active Mandate that authorised this payment. Required for zero-trust audit trail.';
