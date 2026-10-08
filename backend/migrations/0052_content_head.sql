-- 0052_content_head — §5.1's third term and the era measure behind it (decisions 568-570).
--
-- score_u(t) = μ_u + (1−w_u)·[(1−β_u)·z(b(t)) + β_u·cf(t)] + w_u·con_u(t)
--
-- The fold-in's two halves are untouched. `con` is new: a ridge over facts about the film — the
-- imported DNA vocabulary, canonical genre, decade, the era measure, a few meta columns, the crowd
-- prior, the platform score, the member's own affinity for the people who made it, and cf itself.
-- Until this head existed the personal score read a 64-d crowd coordinate and nothing about the
-- film, so the DNA could say "this is like that" and never "you will like this".
--
-- At w_u = 0 the score is the one 0009's `user_score` already held, to the bit, so a member this
-- head cannot help keeps exactly today's shelves.

-- One row per (user, kind), as `user_vector` has, but the vector is as wide as the feature layout
-- rather than 64, so it cannot borrow user_vector's fixed-width bytea convention.
CREATE TABLE user_content_fit (
    user_id          bigint  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    kind             text    NOT NULL CHECK (kind IN ('movie', 'series')),
    bundle_version   text    NOT NULL REFERENCES artifact_bundle(version) ON DELETE CASCADE,
    -- float32 little-endian, `width` of them, in the layout's own column order. Two members'
    -- weights are comparable only because that order is one shared thing (decision 570), and
    -- `digest` is what refuses a vector built under a different one.
    w                bytea   NOT NULL,
    width            integer NOT NULL CHECK (width > 0),
    digest           text    NOT NULL,
    features_version text    NOT NULL,
    -- w_u above. No ceiling, unlike §5.1's β: there is no crowd floor to defend here, because the
    -- crowd prior is itself a column of this design.
    weight           real    NOT NULL CHECK (weight >= 0 AND weight <= 1),
    content_lambda   real    NOT NULL,
    con_sd           real    NOT NULL,   -- pre-normalisation sd over the reference; 0 = no signal
    con_mean         real    NOT NULL,
    cv_rho           real,               -- held-out ρ of the blended score at the chosen (λ, w)
    base_rho         real,               -- the same at w = 0: the fold-in alone, the thing to beat
    -- The era measure (decision 569), in years. Kept because serving has to reproduce the hinge the
    -- fit was chosen under, and because §6.7's model line prints it.
    era_centre       real    NOT NULL,
    era_spread       real    NOT NULL CHECK (era_spread > 0),
    era_used         integer NOT NULL DEFAULT 0,
    label_count      integer NOT NULL DEFAULT 0,
    used             integer NOT NULL DEFAULT 0,
    dropped          integer NOT NULL DEFAULT 0,
    folds            integer NOT NULL DEFAULT 0,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, kind),
    -- The one state this table must not reach: a weight the cross-validation did not earn. §0's
    -- noise floor says an improvement inside pipeline variance is a tie, and a tie must not buy
    -- personalisation — so a served weight above zero has to come with a ρ that beat the baseline.
    CONSTRAINT user_content_fit_earned
        CHECK (weight = 0::real
               OR (cv_rho IS NOT NULL AND base_rho IS NOT NULL AND cv_rho >= base_rho))
);

-- §5.1's third half per title, beside `cf`, so §6.7's rail can show all three and so a shelf that
-- ranks by the personal half alone (decision 567) does not have to recompute anything.
ALTER TABLE user_score ADD COLUMN con real NOT NULL DEFAULT 0;

-- 0023's trigger lists by hand every table whose rows make an artifact_bundle row provenance rather
-- than garbage (decision 249), and `user_content_fit` is now one of them: a head is fitted in a
-- basis, so deleting the basis under it would leave a fit nothing can reproduce. Replacing the
-- function rather than editing 0023, which is applied and checksummed.
CREATE OR REPLACE FUNCTION artifact_bundle_is_provenance() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state IN ('staged', 'failed') AND NOT EXISTS (
        SELECT 1 FROM title            WHERE placement_bundle = OLD.version
        UNION ALL
        SELECT 1 FROM title_placement  WHERE bundle_version = OLD.version
        UNION ALL
        SELECT 1 FROM user_score       WHERE bundle_version = OLD.version
        UNION ALL
        SELECT 1 FROM title_prior      WHERE bundle_version = OLD.version
        UNION ALL
        SELECT 1 FROM ledger_fit       WHERE bundle_version = OLD.version
        UNION ALL
        SELECT 1 FROM session          WHERE bundle_version = OLD.version
        UNION ALL
        SELECT 1 FROM user_content_fit WHERE bundle_version = OLD.version
    ) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION
        'artifact_bundle row % is state % and is provenance, not garbage: it is the basis a '
        'placement, a score, a prior, a fit or a session was computed in (decision 249)',
        OLD.version, OLD.state
        USING HINT = 'Only a staged or failed row that nothing still cites may be deleted. '
                     'Supersede it instead.';
END $$;
