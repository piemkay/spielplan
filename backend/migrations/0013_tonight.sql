-- 0013_tonight — §4.2's session block, which no migration has claimed until now.
-- Spec v2.1 §4.2, §6.2 (rewritten: 54a-54g), §4.1 rule 5, §10, §13, §14 risk 6; decision 154.
--
-- 0002 held the name for this file: "§4.2 reserves the bare name `session` for a *Tonight*
-- session, so the auth session table is `auth_session`."
--
-- WHAT §4.2 GIVES AND WHAT 54g ADDS. §4.2 sketches session, session_participant,
-- session_answer, session_result and session_outcome. The owner's 2026-08-29 redesign of §6.2
-- adds three columns and one table, and its own cost note says why they land here rather than
-- in a later migration: "the schema is decided now so the M4 write-path is built against it
-- rather than migrated afterwards." The three are `session_participant.converged_at`,
-- `session_participant.ended_by` and `session_answer.selection`; the table is `session_ballot`,
-- without which §13's headline metric — winner approval share — has no data path at all.
--
-- THE ONE PLACE THIS DEVIATES FROM §4.2's COLUMN LIST, and why. §4.2 gives `session` a
-- `context jsonb` and no controls. §6.2 step 1's three controls — kind, runtime budget,
-- rewatch — are not free-form context: §4.1 rule 5 says `kind` is "non-null, indexed, and
-- every ranking surface partitions by it", and a kind buried in jsonb is neither non-null nor
-- indexed nor checkable. §6.2 step 2 additionally requires the open-rooms row to *render*
-- all three ("Film · 60 min · skips seen"), which is a query, not a decoration. So they are
-- columns with constraints, and `context` survives for what it was for — §11's Home Assistant
-- launch metadata, and anything a later milestone hangs off a session.

CREATE TABLE session (
    id              bigserial PRIMARY KEY,
    -- §6.2 step 2's join channel that works when push and the WebSocket do not, and the one
    -- §11 hands back to a Home Assistant dashboard. Shaped like the spec's own example,
    -- `MX-2210`.
    room_code       text NOT NULL,
    host_user_id    bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    state           text NOT NULL DEFAULT 'open'
                    CHECK (state IN ('open', 'voting', 'ballot', 'resolved', 'abandoned')),
    -- §6.2 step 1's controls. Single-valued `kind`: an evening resolves to ONE title, so
    -- decision 18's both-on case (two headed sections) has no meaning here — §4.1 rule 5's
    -- partition is satisfied by choosing a side, not by rendering both.
    kind            text NOT NULL CHECK (kind IN ('movie', 'series')),
    -- Soft (§6.2 step 1): the pool admits up to budget + 40 and labels what runs over. The
    -- bounds are the slider's, not the pool's — 60..200 in steps of 5, default 130.
    runtime_budget_min integer NOT NULL DEFAULT 130
                    CHECK (runtime_budget_min BETWEEN 60 AND 200),
    -- §6.2 step 1: "default: exclude titles *every* participant has seen; 'include rewatches'
    -- flips it". The quantifier lives in the query; this is only which way the toggle sits.
    include_rewatches boolean NOT NULL DEFAULT false,
    -- §10's invariant: "no process may score or refit with a loaded bundle version different
    -- from the active row." The pool is built from §5.1 scores, so the session records the
    -- basis it was built in. NOT CASCADE, unlike title_prior/user_score: those numbers are
    -- garbage against a new Backbone, but the votes and the outcome are vocabulary-independent
    -- facts and survive a re-import exactly as §10 says Ledger observations do.
    bundle_version  text NOT NULL REFERENCES artifact_bundle(version),
    context         jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at      timestamptz NOT NULL DEFAULT now(),
    ended_at        timestamptz,
    CONSTRAINT session_ended_states CHECK (
        (ended_at IS NOT NULL) = (state IN ('resolved', 'abandoned'))
    )
);
-- Two live rooms sharing a code walks a household member into the wrong evening. Partial,
-- because a code is only a handle while the room is live and may be reused afterwards.
CREATE UNIQUE INDEX session_room_code_live ON session (room_code) WHERE ended_at IS NULL;
-- §6.2 step 2's open-rooms list: "active sessions are visible to every household device".
CREATE INDEX session_open_rooms ON session (started_at DESC) WHERE ended_at IS NULL;
CREATE INDEX session_host ON session (host_user_id, started_at DESC);

-- §4.2: "session_participant(session_id, user_id NULL, role, tilt jsonb, answered_count,
--        joined_at) -- NULL = guest slot on the host phone"
-- plus 54g's converged_at and ended_by.
--
-- THE KEY IS NOT (session_id, user_id). §6.2 step 2 hands the initiator's phone round, so two
-- guests in one session is the designed case; on a (session_id, user_id) key the second guest
-- collides with the first on NULL — or, worse, is silently absorbed by it. The seat carries
-- its own id, and "one member holds one seat" (§6.2 step 2's "join channels, all equivalent")
-- is a PARTIAL unique index that NULLs do not participate in.
CREATE TABLE session_participant (
    id            bigserial PRIMARY KEY,
    session_id    bigint NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    user_id       bigint REFERENCES app_user(id) ON DELETE CASCADE,
    role          text NOT NULL CHECK (role IN ('host', 'member', 'guest')),
    -- The seat's position in the room, which is also the hand-the-phone order: §6.2 step 2's
    -- "Guests use the initiator's phone **after the initiator finishes**".
    seat          smallint NOT NULL,
    -- §6.2 steps 4-5: chosen-minus-rejected DNA centred on the candidate-pool mean, per
    -- participant. jsonb because it is a sparse map over vocabulary terms, not a dense vector.
    tilt          jsonb NOT NULL DEFAULT '{}'::jsonb,
    answered_count integer NOT NULL DEFAULT 0 CHECK (answered_count >= 0),
    -- 54c/54g: "ended_by: converged | cap | escape — §14 risk 6 wants the rate of each".
    ended_by      text CHECK (ended_by IN ('converged', 'cap', 'escape')),
    converged_at  timestamptz,
    joined_at     timestamptz NOT NULL DEFAULT now(),
    -- §4.2's own NULL is the guest slot, so the two facts are one fact and must not drift:
    -- a guest never carries a user_id, and a seated member always does.
    CONSTRAINT session_participant_guest_has_no_user
        CHECK ((role = 'guest') = (user_id IS NULL)),
    -- 54c: converged_at is stamped only in the converged case. COALESCE rather than a bare
    -- comparison so a still-running seat (ended_by NULL) is covered rather than passing on a
    -- NULL result.
    CONSTRAINT session_participant_converged_at_when_converged
        CHECK ((converged_at IS NOT NULL) = (COALESCE(ended_by, '') = 'converged'))
);
CREATE UNIQUE INDEX session_participant_seat ON session_participant (session_id, seat);
CREATE UNIQUE INDEX session_participant_one_seat_per_member
    ON session_participant (session_id, user_id) WHERE user_id IS NOT NULL;
CREATE INDEX session_participant_user ON session_participant (user_id, joined_at DESC);

-- §4.2: "session_answer(session_id, participant, seq, title_a, title_b, answer, latency_ms)
--        -- one of the participant's ~10 candidate votes (§6.2 step 4); answer: A | B | EITHER"
-- plus decision 154's NEITHER and 54g's `selection`.
--
-- §14 risk 6: "the round itself must be instrumented at M4 (log every vote …) before anyone
-- tunes it." This table is the instrument, so it is append-only in the same sense `verdict` is:
-- §6's "undo everywhere" retracts by tombstone, never by DELETE, so a retraction is still a
-- fact the rail and any later audit can read.
CREATE TABLE session_answer (
    id             bigserial PRIMARY KEY,
    session_id     bigint NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    participant_id bigint NOT NULL REFERENCES session_participant(id) ON DELETE CASCADE,
    seq            integer NOT NULL CHECK (seq >= 0),
    title_a        integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    title_b        integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    -- Decision 154: "`either` lifts both candidates, `neither` lowers both … Under the
    -- adaptive round `neither` is the most informative answer available — it eliminates two
    -- candidates at once — and the escape from a badly-built pool." The prototype collected
    -- NO_PULL, logged it, and discarded it in tilt(); this CHECK is what keeps that value from
    -- ever being stored again under any name.
    answer         text NOT NULL CHECK (answer IN ('A', 'B', 'EITHER', 'NEITHER')),
    -- 54b, §13's non-negotiable guard: "One pair in ten is drawn uniformly at random from the
    -- candidate pool and is used for neither selection nor stopping … `session_answer` carries
    -- the same `selection` discriminator as `duel`." Spelled exactly as 0005 spells it — a
    -- second spelling is how an exclusion silently stops matching.
    selection      text NOT NULL DEFAULT 'adaptive'
                   CHECK (selection IN ('adaptive', 'uniform_holdout')),
    latency_ms     integer,
    created_at     timestamptz NOT NULL DEFAULT now(),
    -- §6 preamble's "undo everywhere", reaching the one surface where a mis-tap is otherwise
    -- permanent: a hard cap of 20 pairs, a blind reveal, no second pass. Tombstone, not
    -- DELETE — see the table comment.
    retracted_at   timestamptz,
    CONSTRAINT session_answer_distinct_titles CHECK (title_a <> title_b)
);
CREATE UNIQUE INDEX session_answer_seq ON session_answer (participant_id, seq);
CREATE INDEX session_answer_live ON session_answer (participant_id, seq DESC)
    WHERE retracted_at IS NULL;
-- §13 stream (a) via 54b: the held-out arm is the only data admissible for evaluating the
-- round, so it is selectable without scanning every answer — the same shape `duel_holdout`
-- gives the tier queue in 0005.
CREATE INDEX session_answer_holdout ON session_answer (session_id)
    WHERE selection = 'uniform_holdout' AND retracted_at IS NULL;

-- 54e: "Each participant taps everything they would be happy with among the three finalists
-- and the wildcard — an approval ballot, not a ranking. Approvals stay hidden until every
-- participant has submitted; then they are revealed together."
--
-- `submitted_at` is what makes the blind rule readable in one query: a participant has
-- submitted when their rows carry it, and the reveal is "no seat is missing one".
CREATE TABLE session_ballot (
    id             bigserial PRIMARY KEY,
    session_id     bigint NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    participant_id bigint NOT NULL REFERENCES session_participant(id) ON DELETE CASCADE,
    title_id       integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    approved       boolean NOT NULL,
    submitted_at   timestamptz NOT NULL DEFAULT now()
);
-- One participant approving one title twice would inflate the approval share §13 evaluates on.
CREATE UNIQUE INDEX session_ballot_one_per_title
    ON session_ballot (participant_id, title_id);
CREATE INDEX session_ballot_session ON session_ballot (session_id);

-- §4.2: "session_result(session_id, title_id, rank, group_score, per_user_match jsonb,
--        conflict jsonb)" — the slate, per candidate. Distinct from session_outcome's single
-- chosen row, and persisted rather than recomputed: a slate re-derived on read cannot be
-- compared against the votes that produced it, which is what §14 risk 6 exists to require.
CREATE TABLE session_result (
    session_id     bigint NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    title_id       integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    rank           smallint NOT NULL CHECK (rank >= 1),
    -- 54d: "three finalists and a wildcard … plus one exploratory pick honestly labelled".
    -- The wildcard is "never counted among the three", so which slot a row occupies is a fact
    -- about the slate and not an inference from its rank.
    slot           text NOT NULL CHECK (slot IN ('finalist', 'wildcard', 'runner_up')),
    group_score    real NOT NULL,
    -- §6.2 step 7: "per-person match lines in DNA terms including the honest negative".
    per_user_match jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- §6.2 step 5: "~14.5% of nights; **below that, decide silently**". NULL is the silent
    -- night, and it is the common one — a conflict object written on every session turns the
    -- repair-explanation register into background noise.
    conflict       jsonb,
    PRIMARY KEY (session_id, title_id)
);
CREATE UNIQUE INDEX session_result_rank ON session_result (session_id, rank);

-- §4.2: "session_outcome(session_id, chosen_title_id, approval_share, participants)
--        -- feeds §13". One row per resolved evening; approval share is §13's headline metric
-- for the whole feature and exists nowhere else.
CREATE TABLE session_outcome (
    session_id      bigint PRIMARY KEY REFERENCES session(id) ON DELETE CASCADE,
    chosen_title_id integer NOT NULL REFERENCES title(id) ON DELETE CASCADE,
    -- Approving participants over participants. Out of [0, 1] is a counting bug that would
    -- otherwise be found in a chart months later.
    approval_share  real NOT NULL CHECK (approval_share >= 0.0 AND approval_share <= 1.0),
    participants    integer NOT NULL CHECK (participants > 0),
    resolved_at     timestamptz NOT NULL DEFAULT now()
);
