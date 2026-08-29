-- 0005_ledger — user state and the Personal Ledger. Spec v2.1 §4.2 verbatim, §5.2.
-- Table and column names follow §4.2's block exactly wherever it names them.

-- §4.2: "state: unseen | seen  (owner decision 2026-08-29: no 'forgotten' state —
--        'seen, don't remember' is marked plain unseen; verdict/duel history is
--        append-only and survives the flip)"
CREATE TABLE user_title (
    user_id          bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    title_id         integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    state            text NOT NULL CHECK (state IN ('unseen', 'seen')),
    state_changed_at timestamptz NOT NULL DEFAULT now(),
    jf_synced_at     timestamptz,          -- §7.3: prevents sync loops
    PRIMARY KEY (user_id, title_id)
);
CREATE INDEX user_title_state ON user_title (user_id, state);

-- §5.2 arm 1: "3-class verdicts — ordered logit, free per-user cutpoints".
-- Append-only: a re-rating supersedes rather than overwrites, so the rewatch arm (arm 4)
-- and the §13 re-ask stream both have their history.
CREATE TABLE verdict (
    id            bigserial PRIMARY KEY,
    user_id       bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    title_id      integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    value         smallint NOT NULL CHECK (value IN (0, 1, 2)),  -- 0 disliked/1 ok/2 liked
    created_at    timestamptz NOT NULL DEFAULT now(),
    superseded_by bigint REFERENCES verdict(id) ON DELETE SET NULL,
    -- §13 stream (b): "a separate silent re-ask stream — ~10% of comparisons/verdicts
    -- re-asked after ≥3 days; ~200 re-asks measure the flip rate σ".
    is_reask      boolean NOT NULL DEFAULT false,
    reask_of      bigint REFERENCES verdict(id) ON DELETE SET NULL,
    source        text NOT NULL DEFAULT 'sweep'
);
CREATE INDEX verdict_user_title ON verdict (user_id, title_id);
CREATE INDEX verdict_live ON verdict (user_id, title_id) WHERE superseded_by IS NULL;

-- §5.2 arm 2: "Duels — Davidson Bradley–Terry with ties; margin-weighted".
-- §4.2: "outcome: A | B | TIE ('about the same' is first-class data: 22% of random pairs are
--        genuine ties; margin optional: decisive vs hesitant)"
CREATE TABLE duel (
    id         bigserial PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    title_a    integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    title_b    integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    outcome    text NOT NULL CHECK (outcome IN ('A', 'B', 'TIE')),
    margin     real,                       -- NULL = margin-less (e.g. a drag-drop neighbour duel)
    context    text NOT NULL CHECK (context IN ('profile_battle', 'tier_queue', 'tier_insert')),
    -- §6.3 / §13 stream (a): "the 10% uniform-random comparison stream is the ONLY data used to
    -- evaluate the tier model — adaptively-selected pairs inflate reliability … the guard is
    -- non-negotiable." Held-out rows must be identifiable at query time, hence a column.
    selection  text NOT NULL DEFAULT 'random'
               CHECK (selection IN ('random', 'boundary', 'exploration', 'uniform_holdout')),
    created_at timestamptz NOT NULL DEFAULT now(),
    is_reask   boolean NOT NULL DEFAULT false,
    reask_of   bigint REFERENCES duel(id) ON DELETE SET NULL,
    CONSTRAINT duel_distinct_titles CHECK (title_a <> title_b)
);
CREATE INDEX duel_user ON duel (user_id, created_at DESC);
CREATE INDEX duel_user_a ON duel (user_id, title_a);
CREATE INDEX duel_user_b ON duel (user_id, title_b);
CREATE INDEX duel_holdout ON duel (user_id) WHERE selection = 'uniform_holdout';

-- §5.2 arm 3: "Tier edits (drag-drop, explicit picks) — K-level ordered logit … drag-and-drop
-- = data, not override; the model re-fits around it."
CREATE TABLE tier_edit (
    id         bigserial PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    title_id   integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    tier       smallint NOT NULL,          -- index into the user's configured tier set
    via        text NOT NULL CHECK (via IN ('drag_drop', 'explicit')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tier_edit_user ON tier_edit (user_id, created_at DESC);

-- §4.2: "nightly MAP output; displayed 0..1 via posterior CDF (the 'relative 0..1 weight' the
-- owner asked for)". §5.2: the CDF is empirical over the user's own fitted s values, PER KIND.
CREATE TABLE ledger_state (
    user_id    bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    title_id   integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    s          double precision NOT NULL,
    sigma      double precision NOT NULL,
    cdf        double precision,           -- the displayed 0..1 weight, per kind
    tier       smallint,
    straddle   smallint,                   -- §6.3: the adjacent tier when the posterior straddles
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, title_id)
);
CREATE INDEX ledger_state_rank ON ledger_state (user_id, s DESC);

-- §4.2: "learned tier cutpoints; length = |tier set| − 1 (default 6, ordered ascending)".
-- Per kind, because §4.1 rule 5 partitions every ranking surface by kind.
CREATE TABLE ledger_cutpoints (
    user_id    bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    kind       text NOT NULL CHECK (kind IN ('movie', 'series')),
    boundaries double precision[] NOT NULL,
    tier_set   text[] NOT NULL DEFAULT ARRAY['F','D','C','B','A','A+','S'],
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, kind),
    CONSTRAINT cutpoints_length CHECK
        (array_length(boundaries, 1) = array_length(tier_set, 1) - 1)
);

-- §4.2: "64-d fold-in, mood tilt cache"
CREATE TABLE user_vector (
    user_id    bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    kind       text NOT NULL CHECK (kind IN ('movie', 'series')),
    purpose    text NOT NULL DEFAULT 'foldin' CHECK (purpose IN ('foldin', 'tilt')),
    vec        bytea NOT NULL,
    blend_beta real,                       -- §5.1: per-label-count blend weight, refit nightly
    label_count integer,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, kind, purpose)
);

-- §4.2 / §7.3
CREATE TABLE playback_event (
    id       bigserial PRIMARY KEY,
    source   text NOT NULL,                -- jellyfin | external (§7.3 theater path, §11 HA)
    title_id integer REFERENCES title(id) ON DELETE SET NULL,
    user_id  bigint REFERENCES app_user(id) ON DELETE SET NULL,
    finished boolean NOT NULL DEFAULT false,
    progress real,
    at       timestamptz NOT NULL DEFAULT now(),
    -- §7.3: "Jellyfin playback is a suggestion, never a silent write" — the prompt this event
    -- armed, and whether it has been answered.
    prompt_state text NOT NULL DEFAULT 'armed'
                 CHECK (prompt_state IN ('armed', 'shown', 'answered', 'dismissed'))
);
CREATE INDEX playback_event_user ON playback_event (user_id, at DESC);
CREATE INDEX playback_event_pending ON playback_event (user_id)
    WHERE finished AND prompt_state IN ('armed', 'shown');

-- §8: the acquisition pipeline board.
CREATE TABLE acquisition_job (
    title_id   integer PRIMARY KEY REFERENCES title(id) ON DELETE CASCADE,
    stage      smallint NOT NULL CHECK (stage BETWEEN 1 AND 10),
    status     text NOT NULL CHECK (status IN ('queued', 'running', 'parked', 'ready', 'failed')),
    detail     jsonb NOT NULL DEFAULT '{}'::jsonb,
    reason     text,                       -- shown verbatim on the admin board
    retry_after timestamptz,               -- §8 stage 4: 30-day review-accrual window
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX acquisition_job_status ON acquisition_job (status, updated_at DESC);
