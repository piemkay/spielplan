-- 0010_ledger_fit — the fit state §5.3's 50 ms budget needs. Spec v2.1 §5.2, §5.3, §4.3, §10.
--
-- §4.2 already carries the Ledger's OUTPUT (ledger_state, ledger_cutpoints, user_vector) and
-- its INPUT (verdict, duel, tier_edit). What it does not carry is the small amount of fit state
-- an incremental update needs in order not to redo the nightly solve. Without it "incremental"
-- means "re-read every observation and re-minimise", which is the nightly job under another
-- name, and §5.3's "<50 ms" is unreachable by construction rather than by inefficiency.

CREATE TABLE ledger_fit (
    user_id        bigint  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    kind           text    NOT NULL CHECK (kind IN ('movie', 'series')),
    -- Each blob is a numpy .npy payload, so shape and dtype travel with the bytes rather than
    -- living in a comment that can drift from the writer.
    theta          bytea   NOT NULL,   -- (mu, v[64], gamma[2], cuts[K-1], log_nu)
    title_ids      bytea   NOT NULL,   -- int32[n], ascending — the row order of everything below
    residuals      bytea   NOT NULL,   -- float64[n], r
    sigma          bytea   NOT NULL,   -- float64[n], sqrt of the Laplace marginal variance
    sigma_prior    bytea   NOT NULL,   -- float64[n], §5.2's freshness cap
    anchor_curv    bytea   NOT NULL,   -- float64[n], diag of the ridge (anchor) Hessian's r block
    duel_curv      bytea   NOT NULL,   -- float64[n], λ_bt · diag(H_BT) at the optimum
    cdf_reference  bytea   NOT NULL,   -- float64[m], sorted s over the observed titles
    z_cov          bytea   NOT NULL,   -- float64[65,65], the (μ, v) posterior block — every
                                       --   unobserved title's σ comes from this
    n_observed     integer NOT NULL,

    -- §4.3: "every constant comes from ledger_hyperparams.json". A cache built under other
    -- constants is wrong rather than stale, so the digest is a precondition and not a hint.
    hp_digest      text    NOT NULL,
    hp_source      text    NOT NULL CHECK (hp_source IN ('bundle', 'default')),
    -- §10: e_i is a Backbone row, so a fit outlives its bundle only as garbage.
    bundle_version text    REFERENCES artifact_bundle(version) ON DELETE SET NULL,
    fit_source     text    NOT NULL DEFAULT 'nightly'
                   CHECK (fit_source IN ('nightly', 'incremental')),
    objective      double precision,
    grad_inf       double precision,
    converged      boolean NOT NULL DEFAULT true,
    fitted_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, kind)
);

-- §5.2 freshness: "after 12 months untouched, a title's σ inflates Glicko-style at rate c per
-- √month, capped at the prior σ." The fitted σ and the displayed σ are different numbers and
-- both are needed — the fit must not be contaminated by a display rule, and the §6.3 queue must
-- not re-derive it on every request.
--
-- `straddle` already exists in 0005 and keeps its meaning (the adjacent tier when the posterior
-- straddles a boundary); no second column for it.
ALTER TABLE ledger_state
    ADD COLUMN kind             text,
    ADD COLUMN observed         boolean NOT NULL DEFAULT false,
    ADD COLUMN sigma_prior      double precision,
    ADD COLUMN sigma_eff        double precision,
    ADD COLUMN last_observed_at timestamptz,
    ADD COLUMN fit_source       text NOT NULL DEFAULT 'nightly'
        CHECK (fit_source IN ('nightly', 'incremental'));

-- Nothing writes ledger_state before M2, so the backfill is a formality — written out so the
-- migration is still correct on an install that somehow has rows.
UPDATE ledger_state ls SET kind = t.kind FROM title t WHERE t.id = ls.title_id;
ALTER TABLE ledger_state
    ALTER COLUMN kind SET NOT NULL,
    ADD CONSTRAINT ledger_state_kind CHECK (kind IN ('movie', 'series'));

-- §4.1 rule 5: the ranked read is per (user, kind) and never one interleaved ordering.
DROP INDEX IF EXISTS ledger_state_rank;
CREATE INDEX ledger_state_rank ON ledger_state (user_id, kind, s DESC);
-- The CDF reference set is the observed block, and this is the query that builds it nightly.
CREATE INDEX ledger_state_observed ON ledger_state (user_id, kind) WHERE observed;
