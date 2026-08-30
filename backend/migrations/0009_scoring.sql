-- 0009_scoring — the §5.1 serving stack, materialised. Spec v2.1 §5.1, §4.1 rule 5, §10.
--
-- score_u(t) = b(t) + μ_u + w_cf·⟨v_u, e(t)⟩, blended with the crowd prior at β = 0.8.
-- Two tables and seven columns; each exists because a number the ranked list sorts on, or the
-- title card prints, cannot be reconstructed without it.

-- §5.1's crowd half, computed once per title so the model line on the card and the number the
-- ranked list sorts on are the same arithmetic rather than two implementations of it.
-- User-independent by construction: b, gate and item_n are crowd quantities.
CREATE TABLE title_prior (
    title_id       integer PRIMARY KEY REFERENCES title(id) ON DELETE CASCADE,
    bundle_version text NOT NULL REFERENCES artifact_bundle(version) ON DELETE CASCADE,
    b              real,                        -- NULL only when e_source = 'none'
    b_i            real,                        -- raw crowd bias; NULL with no Backbone row
    item_n         integer NOT NULL DEFAULT 0,  -- §5.1's n_t, the gate input
    gate           real    NOT NULL,            -- n_t/(n_t+k), k = 10; exactly 0 with no row
    e_source       text    NOT NULL
                   CHECK (e_source IN ('backbone', 'blended', 'cold_tower', 'none')),
    computed_at    timestamptz NOT NULL DEFAULT now(),
    -- 'none' is the only state with no prior, and it is the state §12's M2 exit criterion
    -- forbids for an owned title. Keeping the row rather than omitting it is what lets the
    -- reconciliation report name the offenders instead of reporting a difference of counts.
    CONSTRAINT title_prior_b_present CHECK ((e_source = 'none') = (b IS NULL))
);
CREATE INDEX title_prior_uncoordinated ON title_prior (title_id) WHERE e_source = 'none';

-- §5.1's per-user half. `kind` is IN THE ROW rather than inferred through a join, so no ranked
-- query can sort across the partition by omission: §4.1 rule 5 lives in the data, not in a
-- convention someone has to remember.
CREATE TABLE user_score (
    user_id        bigint  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    title_id       integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    kind           text    NOT NULL CHECK (kind IN ('movie', 'series')),
    bundle_version text    NOT NULL REFERENCES artifact_bundle(version) ON DELETE CASCADE,
    score          real    NOT NULL,            -- §5.1's score_u(t)
    cf             real    NOT NULL,            -- ⟨v_u, e(t)⟩, kept so §6.7 can show both halves
    computed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, title_id)
);
CREATE INDEX user_score_rank ON user_score (user_id, kind, score DESC);

-- §4.2's user_vector already carries `vec`, `blend_beta` and `label_count`. The nightly fit
-- produces four more scalars without which score_u(t) cannot be reproduced, and three that
-- make the fit auditable in §6.7's rail.
ALTER TABLE user_vector
    ADD COLUMN mu             real NOT NULL DEFAULT 0,  -- μ_u: shifts every score, reorders none
    ADD COLUMN prior_mean     real NOT NULL DEFAULT 0,  -- z-scoring of b(t) over the population
    ADD COLUMN prior_sd       real NOT NULL DEFAULT 1,
    ADD COLUMN cf_sd          real NOT NULL DEFAULT 1,  -- sd of ⟨v,e⟩; 0 means "no signal yet"
    ADD COLUMN foldin_lambda  real,                     -- the cross-validated ridge
    ADD COLUMN cv_rho         real,                     -- held-out ρ at the chosen (λ, β)
    ADD COLUMN bundle_version text REFERENCES artifact_bundle(version) ON DELETE CASCADE;

-- §5.1 calls β = 0.8 "the measured optimum; also exactly where per-user top-10s stop being the
-- global chart". A fit that exceeds it is unsupported by anything measured, so the ceiling is
-- a constraint rather than a comment somebody may edit past.
ALTER TABLE user_vector
    ADD CONSTRAINT user_vector_beta_range
    CHECK (blend_beta IS NULL OR (blend_beta >= 0.0 AND blend_beta <= 0.8));
