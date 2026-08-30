-- 0011_rate — the Rate surface's server state. Spec v2.1 §6.1, §13 stream (b), decision 35.

-- §6.1: "Mix (default — alternates sweep and battle); blocks of 15." The session lives on the
-- server because the block counter is what Undo's depth is measured in (decision 35), and a
-- counter the client owns is a counter the client can lie about.
CREATE TABLE rate_session (
    id           bigserial PRIMARY KEY,
    user_id      bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    kinds        text[] NOT NULL,
    mode         text NOT NULL DEFAULT 'mix' CHECK (mode IN ('mix', 'sweep', 'battle')),
    -- §6.1: "a persistent decisive toggle sets the margin weight (~1.6 vs 1.0)". Persistent
    -- across cards, so it belongs to the session rather than to a request.
    decisive     boolean NOT NULL DEFAULT false,
    block_index  integer NOT NULL DEFAULT 0,
    slot         smallint NOT NULL DEFAULT 1 CHECK (slot BETWEEN 1 AND 15),
    seq          integer NOT NULL DEFAULT 0,
    -- The exact card in front of the person, held here rather than sent by the client: a GET
    -- is then idempotent, a battle pair does not reshuffle under the user's thumb, and §13's
    -- re-ask marker has a server-side home no serialiser can reach.
    current_card jsonb,
    card_token   uuid,
    started_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    ended_at     timestamptz,
    CONSTRAINT rate_session_kinds_nonempty CHECK (cardinality(kinds) BETWEEN 1 AND 2),
    CONSTRAINT rate_session_kinds_valid    CHECK (kinds <@ ARRAY['movie', 'series']),
    CONSTRAINT rate_session_card_has_token CHECK ((current_card IS NULL) = (card_token IS NULL))
);
-- One live session per person: two would each hold a block counter and Undo would have to
-- guess which one the tap belonged to.
CREATE UNIQUE INDEX rate_session_one_live ON rate_session (user_id) WHERE ended_at IS NULL;

-- Decision 35 chose "an observation journal with compensating writes rather than a lastAction
-- variable". Append-only: an undone row is tombstoned, never deleted, so the §6.7 rail and any
-- later audit can still see what was retracted and when.
CREATE TABLE rate_observation (
    id          bigserial PRIMARY KEY,
    session_id  bigint NOT NULL REFERENCES rate_session(id) ON DELETE CASCADE,
    user_id     bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    seq         integer NOT NULL,
    block_index integer NOT NULL,
    slot        smallint NOT NULL CHECK (slot BETWEEN 1 AND 15),
    kind_of     text NOT NULL
                CHECK (kind_of IN ('verdict', 'not_seen', 'skip', 'duel', 'tie', 'correction')),
    -- §6.1: every observation advances the block and flips the served card type, so a run of
    -- duels still returns sweep cards. A corrections-row tap redraws the pair instead — it is
    -- a repair, not an observation. Stored rather than re-derived at three call sites, and
    -- pinned to `kind_of` by a CHECK so the two cannot drift apart.
    advances    boolean NOT NULL,
    CONSTRAINT rate_observation_advances_rule CHECK (advances = (kind_of <> 'correction')),
    -- The exact card or pair that produced it, restored verbatim by Undo — decision 35's
    -- "restores the card that produced it, not the neighbouring queue position".
    card        jsonb NOT NULL,
    -- The titles this observation touched. One predicate then covers skip suppression, "not
    -- redrawn in the same sitting", and never serving one title twice in a session.
    title_ids   integer[] NOT NULL,
    verdict_id  bigint REFERENCES verdict(id) ON DELETE SET NULL,
    duel_id     bigint REFERENCES duel(id) ON DELETE SET NULL,
    -- §4.2: a re-rating supersedes rather than mutates. Undoing it must un-supersede exactly
    -- the row it stamped, so the row it stamped is recorded.
    superseded_verdict_id bigint REFERENCES verdict(id) ON DELETE SET NULL,
    -- [{title_id, state, jf_synced_at, pushed}] — what user_title held before, and whether we
    -- actually reached Jellyfin. Undo compensates exactly what it did, not what it intended.
    prior_state jsonb NOT NULL DEFAULT '[]'::jsonb,
    latency_ms  integer,
    created_at  timestamptz NOT NULL DEFAULT now(),
    undone_at   timestamptz
);
CREATE UNIQUE INDEX rate_observation_seq ON rate_observation (session_id, seq);
CREATE INDEX rate_observation_live ON rate_observation (session_id, block_index, seq DESC)
    WHERE undone_at IS NULL;
CREATE INDEX rate_observation_titles ON rate_observation USING gin (title_ids);
