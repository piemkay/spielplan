"""The constraints, asserted by trying to violate them. Spec v2.1 §4.1, §4.2, §10.

`test_migrations.py` checks the schema's *shape* against PGlite. These check its *behaviour*
against a real server: a CHECK constraint that is never tried is a comment with punctuation.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import asyncpg
import pytest

from spielplan.db import migrate
from tests.test_backup import _drop, _recreate, _sibling
from tests.test_upgrade_drill import _complete, _stage


async def _title(db, title_id=1, kind="movie") -> int:
    await db.execute(
        "INSERT INTO title (id, kind, name) VALUES ($1, $2, 'x') ON CONFLICT DO NOTHING",
        title_id, kind,
    )
    return title_id


async def _user(db, name="patrick", role="member") -> int:
    return await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ($1, $2) RETURNING id", name, role
    )


# --- §4.1 rule 5: kind ----------------------------------------------------------------


async def test_kind_is_not_null_and_constrained_to_two_values(db):
    with pytest.raises(asyncpg.NotNullViolationError):
        await db.execute("INSERT INTO title (id, kind, name) VALUES (900, NULL, 'x')")
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute("INSERT INTO title (id, kind, name) VALUES (901, 'episode', 'x')")


async def test_kind_is_indexed(db):
    """Every ranking surface partitions by it, so it is on the hot path of every list."""
    indexes = await db.fetch(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'title' AND schemaname = 'public'"
    )
    defs = " ".join(r["indexdef"] for r in indexes)
    assert "(kind)" in defs or "(kind, " in defs


# --- §4.1 rule 4: the frozen rating_source ids ----------------------------------------


async def test_the_database_refuses_a_renumbered_rating_source(db):
    """The validator catches this at import; the CHECK is the second line, for anything that
    reaches the table another way. These ids key fitted_cuts and equating_map."""
    await db.execute("INSERT INTO rating_source (id, name) VALUES (1, 'ok')")
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute("INSERT INTO rating_source (id, name) VALUES (99, 'renumbered')")


# --- §4.1 rule 6: no UNIQUE on the external ids ---------------------------------------


async def test_duplicate_tmdb_ids_are_accepted(db):
    """315 duplicates exist and are legitimate, mostly movie/series pairs."""
    await db.execute("INSERT INTO title (id, kind, name, tmdb_id) VALUES (910, 'movie', 'a', 42)")
    await db.execute("INSERT INTO title (id, kind, name, tmdb_id) VALUES (911, 'series', 'b', 42)")
    assert await db.fetchval("SELECT count(*) FROM title WHERE tmdb_id = 42") == 2


# --- §4.1 rule 1 + §6.6: the extracted tier's arbiter ---------------------------------


async def test_dna_tag_provider_is_not_null_so_its_unique_index_fires(db):
    """`0004_dna.sql:83` declares `UNIQUE (title_id, version, term, provider)` and 0018 is what
    makes it mean anything.

    NULLs are distinct in a unique index, and the importer never wrote `provider`, so every row
    carried NULL and the arbiter matched no pair of rows at all — a constraint that reads as
    enforced in the DDL and enforces nothing. §6.6's parallel extraction mode is the case it
    exists for: two providers naming the same term for one title must be one row per provider,
    and until 0018 they were unbounded rows per provider.

    The two halves are separate assertions because neither implies the other. A default of `''`
    with the column still nullable would leave every row the importer writes explicitly as NULL
    outside the index; NOT NULL without the default would make the importer's current INSERT
    fail rather than key correctly. Written as the violation rather than as a catalogue lookup
    for this file's reason (module docstring): a constraint that is never tried is a comment
    with punctuation. Before 0018 the second INSERT below landed cleanly and the row count was
    2. [M4.9, decision 162's install is repaired by the same migration]
    """
    column = await db.fetchrow(
        "SELECT is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'dna_tag' AND column_name = 'provider'"
    )
    assert column["is_nullable"] == "NO", "a NULL provider is invisible to the arbiter index"
    assert column["column_default"] is not None and "''" in column["column_default"], (
        "the importer writes no provider today, so 'no provider recorded' needs one spelling"
    )

    await _title(db, 1)
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 11, 3)"
    )
    insert = (
        "INSERT INTO dna_tag (title_id, version, term, facet, salience) "
        "VALUES (1, 'v1', 'mood.dread', 'mood', 3)"
    )
    await db.execute(insert)
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(insert)
    assert await db.fetchval("SELECT count(*) FROM dna_tag") == 1


# --- §4.2: seen state ------------------------------------------------------------------


async def test_seen_state_has_exactly_two_values(db):
    """Owner decision 2026-08-29: there is no 'forgotten'. A title you cannot remember is
    plain `unseen` — one control, one sync rule."""
    user_id = await _user(db)
    await _title(db)
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 1, 'seen')", user_id
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 1, 'forgotten')",
            user_id,
        )


async def test_flipping_seen_to_unseen_keeps_the_history(db):
    """§4.2: "verdict/duel history is append-only and survives the flip"."""
    user_id = await _user(db)
    await _title(db)
    await _title(db, 2)
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 1, 'seen')", user_id
    )
    await db.execute("INSERT INTO verdict (user_id, title_id, value) VALUES ($1, 1, 2)", user_id)
    await db.execute(
        "INSERT INTO duel (user_id, title_a, title_b, outcome, context) "
        "VALUES ($1, 1, 2, 'A', 'profile_battle')",
        user_id,
    )

    await db.execute(
        "UPDATE user_title SET state = 'unseen' WHERE user_id = $1 AND title_id = 1", user_id
    )
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE user_id = $1", user_id) == 1
    assert await db.fetchval("SELECT count(*) FROM duel WHERE user_id = $1", user_id) == 1


# --- §4.2: the ledger arms -------------------------------------------------------------


async def test_a_verdict_is_one_of_three_classes(db):
    user_id = await _user(db)
    await _title(db)
    for value in (0, 1, 2):
        await db.execute(
            "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, 1, $2)", user_id, value
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute("INSERT INTO verdict (user_id, title_id, value) VALUES ($1, 1, 5)", user_id)


async def test_a_duel_records_ties_and_refuses_a_self_pairing(db):
    """§4.2: "about the same" is first-class data — 22% of random pairs are genuine ties."""
    user_id = await _user(db)
    await _title(db)
    await _title(db, 2)
    await db.execute(
        "INSERT INTO duel (user_id, title_a, title_b, outcome, context) "
        "VALUES ($1, 1, 2, 'TIE', 'profile_battle')",
        user_id,
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO duel (user_id, title_a, title_b, outcome, context) "
            "VALUES ($1, 1, 1, 'A', 'profile_battle')",
            user_id,
        )


async def test_the_uniform_holdout_stream_is_addressable(db):
    """§13: the 10% uniform-random stream is the ONLY data admissible for evaluating the tier
    model. It has to be separable by query, or the guard is unenforceable."""
    user_id = await _user(db)
    await _title(db)
    await _title(db, 2)
    for selection in ("random", "boundary", "uniform_holdout"):
        await db.execute(
            "INSERT INTO duel (user_id, title_a, title_b, outcome, context, selection) "
            "VALUES ($1, 1, 2, 'A', 'tier_queue', $2)",
            user_id, selection,
        )
    held = await db.fetchval(
        "SELECT count(*) FROM duel WHERE selection = 'uniform_holdout' AND user_id = $1", user_id
    )
    assert held == 1


async def test_cutpoints_must_match_the_tier_set(db):
    """§4.2: "length = |tier set| − 1". Decision 11 makes the set per-user, so the invariant
    has to hold per row rather than globally."""
    user_id = await _user(db)
    await db.execute(
        "INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set) "
        "VALUES ($1, 'movie', ARRAY[0.1,0.2,0.3,0.4,0.5,0.6], "
        "ARRAY['F','D','C','B','A','A+','S'])",
        user_id,
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set) "
            "VALUES ($1, 'series', ARRAY[0.1,0.2], ARRAY['F','D','C','B'])",
            user_id,
        )


async def test_one_user_changing_their_tier_set_leaves_another_alone(db):
    """Decision 11: the tier set is a per-user preference."""
    a = await _user(db, "patrick")
    b = await _user(db, "jenny")
    for user_id in (a, b):
        await db.execute(
            "INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set) "
            "VALUES ($1, 'movie', ARRAY[0.1,0.2,0.3,0.4,0.5,0.6], "
            "ARRAY['F','D','C','B','A','A+','S'])",
            user_id,
        )
    await db.execute(
        "UPDATE ledger_cutpoints SET boundaries = ARRAY[0.2,0.4,0.6,0.8], "
        "tier_set = ARRAY['bad','ok','good','great','best'] WHERE user_id = $1",
        a,
    )
    other = await db.fetchval(
        "SELECT array_length(tier_set, 1) FROM ledger_cutpoints WHERE user_id = $1", b
    )
    assert other == 7


# --- §10: exactly one active bundle ----------------------------------------------------


async def test_only_one_bundle_can_be_active(db):
    """§10's invariant, enforced by a partial unique index rather than by discipline."""
    # One seed, then a model bundle: decision 162's two kinds, because 0015's
    # `artifact_bundle_one_seed` index makes a second `kind = 'seed'` row impossible and the
    # rule under test here is the ACTIVE one.
    for version, kind in (("v1", "seed"), ("v2", "model")):
        await db.execute(
            "INSERT INTO artifact_bundle (version, manifest, state, kind)"
            " VALUES ($1, '{}', 'staged', $2)",
            version, kind,
        )
    await db.execute("UPDATE artifact_bundle SET state = 'active' WHERE version = 'v1'")
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute("UPDATE artifact_bundle SET state = 'active' WHERE version = 'v2'")


# --- §2: connector secrets --------------------------------------------------------------


async def test_a_connector_secret_cannot_be_stored_without_naming_its_key(db):
    """Every ciphertext carries its key_id, or rotation cannot find what to re-wrap."""
    await db.execute(
        "INSERT INTO data_encryption_key (key_id, wrapped_dek) VALUES ('k1', '\\x00')"
    )
    await db.execute(
        "INSERT INTO connector_config (name, config, secrets_encrypted, secrets_key_id) "
        "VALUES ('jellyfin', '{}', '\\xdeadbeef', 'k1')"
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO connector_config (name, config, secrets_encrypted) "
            "VALUES ('tmdb', '{}', '\\xdeadbeef')"
        )


# --- §4.2 + 54g: the Tonight session block ----------------------------------------------


async def _session(db, host: int, *, kind: str = "movie", budget: int = 130) -> int:
    """§10: a session records the basis its pool was built in, so it needs a bundle to exist.
    That is the point of the NOT NULL — a Tonight session has no pool without §5.1 scores, and
    §5.1 scores have no meaning without an active bundle."""
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) VALUES ('test-v1', '{}', 'active') "
        "ON CONFLICT DO NOTHING"
    )
    # A distinct code per call: `session_room_code_live` is unique among live rooms, which is
    # the constraint under test elsewhere and merely scaffolding here.
    return await db.fetchval(
        "INSERT INTO session (room_code, host_user_id, kind, runtime_budget_min, bundle_version) "
        "VALUES ('MX-' || nextval('session_id_seq')::text, $1, $2, $3, 'test-v1') RETURNING id",
        host, kind, budget,
    )


async def _seat(db, session_id: int, *, user_id=None, role="guest", seat=1) -> int:
    return await db.fetchval(
        "INSERT INTO session_participant (session_id, user_id, role, seat) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        session_id, user_id, role, seat,
    )


async def test_a_session_answer_is_one_of_four_values(db):
    """Decision 154: `A | B | EITHER | NEITHER`. `EITHER` lifts both, `NEITHER` lowers both —
    opposite signals, not two names for a shrug. The prototype collected `NO_PULL` and threw
    it away in `tilt()`; the CHECK is what stops that value ever being stored again."""
    user = await _user(db)
    a, b = await _title(db, 1), await _title(db, 2)
    sid = await _session(db, user)
    pid = await _seat(db, sid, user_id=user, role="host")
    for i, answer in enumerate(("A", "B", "EITHER", "NEITHER")):
        await db.execute(
            "INSERT INTO session_answer (session_id, participant_id, seq, title_a, title_b, answer) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            sid, pid, i, a, b, answer,
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO session_answer (session_id, participant_id, seq, title_a, title_b, answer) "
            "VALUES ($1, $2, 99, $3, $4, 'NO_PULL')",
            sid, pid, a, b,
        )


async def test_a_session_answer_names_the_stream_it_belongs_to(db):
    """54b, §13's non-negotiable guard: the hold-out arm must be identifiable end to end, and
    a client must never be able to file an adaptive pair as held-out or the reverse. The
    spelling is `uniform_holdout`, the same string `duel.selection` already uses — a second
    spelling is how an exclusion silently stops matching."""
    user = await _user(db)
    a, b = await _title(db, 1), await _title(db, 2)
    sid = await _session(db, user)
    pid = await _seat(db, sid, user_id=user, role="host")
    for i, selection in enumerate(("adaptive", "uniform_holdout")):
        await db.execute(
            "INSERT INTO session_answer "
            "(session_id, participant_id, seq, title_a, title_b, answer, selection) "
            "VALUES ($1, $2, $3, $4, $5, 'A', $6)",
            sid, pid, i, a, b, selection,
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO session_answer "
            "(session_id, participant_id, seq, title_a, title_b, answer, selection) "
            "VALUES ($1, $2, 9, $3, $4, 'A', 'boundary')",
            sid, pid, a, b,
        )


async def test_a_pair_never_names_the_same_title_twice(db):
    """A "which one tonight?" between a title and itself is not a question."""
    user = await _user(db)
    a = await _title(db, 1)
    sid = await _session(db, user)
    pid = await _seat(db, sid, user_id=user, role="host")
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO session_answer (session_id, participant_id, seq, title_a, title_b, answer) "
            "VALUES ($1, $2, 0, $3, $3, 'A')",
            sid, pid, a,
        )


async def test_a_participant_round_ends_with_exactly_one_named_reason(db):
    """54c/54g: `ended_by: converged | cap | escape` — §14 risk 6 wants the rate of each, and
    a fourth value nobody defined would make that rate unreadable."""
    user = await _user(db)
    sid = await _session(db, user)
    await db.execute(
        "UPDATE session_participant SET ended_by = 'converged', converged_at = now() WHERE id = $1",
        await _seat(db, sid, seat=1),
    )
    for i, reason in enumerate(("cap", "escape"), start=2):
        await db.execute(
            "UPDATE session_participant SET ended_by = $1 WHERE id = $2",
            reason, await _seat(db, sid, seat=i),
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "UPDATE session_participant SET ended_by = 'timeout' WHERE id = $1",
            await _seat(db, sid, seat=9),
        )


async def test_converged_at_is_stamped_only_when_the_round_converged(db):
    """54c: the round ends for a person "when the shortlist boundary is resolved … subject to a
    hard cap of 20", and from the sixth pair an escape ends it early. Neither of those is a
    convergence, so neither may carry a convergence timestamp — otherwise §14 risk 6's "how
    often does the cap fire?" is answered by a column that quietly says "never".

    Both directions, because either alone is satisfiable by an implementation that never
    stamps the column at all.
    """
    user = await _user(db)
    sid = await _session(db, user)
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "UPDATE session_participant SET ended_by = 'cap', converged_at = now() WHERE id = $1",
            await _seat(db, sid, seat=1),
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "UPDATE session_participant SET ended_by = 'converged' WHERE id = $1",
            await _seat(db, sid, seat=2),
        )
    # And a seat still answering carries neither.
    running = await _seat(db, sid, seat=3)
    row = await db.fetchrow(
        "SELECT ended_by, converged_at FROM session_participant WHERE id = $1", running
    )
    assert row["ended_by"] is None and row["converged_at"] is None


async def test_two_guest_seats_coexist_in_one_session(db):
    """§4.2: "user_id NULL — NULL = guest slot on the host phone", and §6.2 step 2 hands that
    phone round, so *two* guests is the designed case rather than the edge one. A
    (session_id, user_id) key would seat the first and drop the second."""
    user = await _user(db)
    sid = await _session(db, user)
    first = await _seat(db, sid, seat=1)
    second = await _seat(db, sid, seat=2)
    assert first != second
    rows = await db.fetch(
        "SELECT id, user_id FROM session_participant WHERE session_id = $1 AND user_id IS NULL",
        sid,
    )
    assert len(rows) == 2, "both guest seats must survive"


async def test_one_member_cannot_hold_two_seats_in_one_session(db):
    """§6.2 step 2's "join channels, all equivalent" — a member who arrives twice, by code and
    then from the open-rooms list, must re-attach rather than seat twice. Two seats would
    change the participant count every average and §13's approval share are computed over."""
    user = await _user(db)
    other = await _user(db, name="jenny")
    sid = await _session(db, user)
    await _seat(db, sid, user_id=user, role="host", seat=1)
    await _seat(db, sid, user_id=other, role="member", seat=2)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _seat(db, sid, user_id=user, role="member", seat=3)


async def test_a_ballot_is_one_row_per_participant_and_title(db):
    """54e: the approval ballot is a multi-select over the finalists and the wildcard. One
    participant approving one title twice would inflate the approval share §13 evaluates on."""
    user = await _user(db)
    sid = await _session(db, user)
    pid = await _seat(db, sid, user_id=user, role="host")
    title = await _title(db, 1)
    await db.execute(
        "INSERT INTO session_ballot (session_id, participant_id, title_id, approved) "
        "VALUES ($1, $2, $3, true)",
        sid, pid, title,
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute(
            "INSERT INTO session_ballot (session_id, participant_id, title_id, approved) "
            "VALUES ($1, $2, $3, false)",
            sid, pid, title,
        )


async def test_an_approval_share_outside_zero_to_one_is_refused(db):
    """§13's headline metric is a fraction of participants; a value outside [0, 1] is a
    counting bug that would otherwise be discovered in a chart months later."""
    user = await _user(db)
    sid = await _session(db, user)
    title = await _title(db, 1)
    await db.execute(
        "INSERT INTO session_outcome (session_id, chosen_title_id, approval_share, participants) "
        "VALUES ($1, $2, 0.5, 2)",
        sid, title,
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO session_outcome "
            "(session_id, chosen_title_id, approval_share, participants) "
            "VALUES ($1, $2, 1.5, 2)",
            await _session(db, user), title,
        )


# --- M4.13 / 0022_model_basis: the schema sweep -------------------------------------------
#
# Every test below attempts the write the OLD constraint accepted. That is the only honest shape
# for this section: each of these rules was already in the DDL under a name that promised it, and
# what made them decorative was that nothing had ever tried them. Reproduced against a database
# migrated to 0020 -- `DELETE FROM title WHERE id = 1` succeeded and left every observation table
# at 0 rows; `ARRAY['movie','movie']` inserted; `seat = -3` inserted; a verdict superseded itself;
# a strictly descending six-element boundary array inserted; an empty tier set inserted, because
# `array_length('{}', 1)` is NULL and `NULL = NULL - 1` is NULL, which a CHECK accepts.
# [M4.13 plan §5 items 1-6; findings 33 and 34; decision 239]

# The migration these tests are about. Named once, because the backfill test has to stage the
# release that comes before it and hardcoding the neighbour would go stale at the next number.
UNDER_TEST = "0022_model_basis"

# The tables §4.2 and §13 call observations -- a person asserted every row -- keyed by what counts
# them for one title. This is the list 0022 moved from CASCADE to RESTRICT, and it is a constant
# rather than eight assertions because the two tests below need the same list for opposite
# purposes: one deletes against it, the other counts it afterwards.
_OBSERVATIONS = {
    "verdict": "SELECT count(*) FROM verdict WHERE title_id = $1",
    "duel": "SELECT count(*) FROM duel WHERE title_a = $1 OR title_b = $1",
    "tier_edit": "SELECT count(*) FROM tier_edit WHERE title_id = $1",
    "user_title": "SELECT count(*) FROM user_title WHERE title_id = $1",
    "session_answer": "SELECT count(*) FROM session_answer WHERE title_a = $1 OR title_b = $1",
    "session_ballot": "SELECT count(*) FROM session_ballot WHERE title_id = $1",
    "session_result": "SELECT count(*) FROM session_result WHERE title_id = $1",
    "session_outcome": "SELECT count(*) FROM session_outcome WHERE chosen_title_id = $1",
}


async def _observed(db, target: int = 1) -> int:
    """One row in every table `_OBSERVATIONS` names, all of them naming `target`.

    `duel` and `session_answer` get two rows each, because both carry a title on either side and a
    constraint re-added on only one of them would pass a single-sided fixture.
    """
    user = await _user(db)
    await _title(db, target)
    other = await _title(db, target + 1, kind="series")
    sid = await _session(db, user)
    pid = await _seat(db, sid, user_id=user, role="host")

    await db.execute(
        "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, $2, 2)", user, target
    )
    for a, b in ((target, other), (other, target)):
        await db.execute(
            "INSERT INTO duel (user_id, title_a, title_b, outcome, context) "
            "VALUES ($1, $2, $3, 'A', 'tier_queue')",
            user, a, b,
        )
    await db.execute(
        "INSERT INTO tier_edit (user_id, title_id, tier, via) VALUES ($1, $2, 5, 'drag_drop')",
        user, target,
    )
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, $2, 'seen')", user, target
    )
    for seq, (a, b) in enumerate(((target, other), (other, target))):
        await db.execute(
            "INSERT INTO session_answer "
            "(session_id, participant_id, seq, title_a, title_b, answer) "
            "VALUES ($1, $2, $3, $4, $5, 'A')",
            sid, pid, seq, a, b,
        )
    await db.execute(
        "INSERT INTO session_ballot (session_id, participant_id, title_id, approved) "
        "VALUES ($1, $2, $3, true)",
        sid, pid, target,
    )
    await db.execute(
        "INSERT INTO session_result (session_id, title_id, rank, slot, group_score) "
        "VALUES ($1, $2, 1, 'finalist', 0.9)",
        sid, target,
    )
    await db.execute(
        "INSERT INTO session_outcome (session_id, chosen_title_id, approval_share, participants) "
        "VALUES ($1, $2, 1.0, 1)",
        sid, target,
    )
    return target


async def test_deleting_a_title_that_carries_observations_is_refused(db):
    """§10 line 387: "Ledger observations always survive re-import".

    Until 0022 that sentence was kept by the convention that no code deletes a title, not by the
    schema: every one of these tables declared ON DELETE CASCADE, and `DELETE FROM title WHERE
    id = 1` left all of them at zero rows without an error or a log line. M5's acquisition pipeline
    and any "remove a mis-acquired title" admin action are the first writers that will not know the
    convention, and taste data is the one thing this app cannot re-derive.
    """
    target = await _observed(db)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.execute("DELETE FROM title WHERE id = $1", target)


async def test_every_observation_row_survives_the_refused_delete(db):
    """The caller's half of the refusal: the rows are where they were, not half-cleared.

    The docstring that stood here argued that this test told a partial refusal from a whole one.
    It cannot, and no test that goes through a single DELETE can: the statement is atomic, so
    Postgres rolls back the cascades its RI triggers had already performed along with the raise,
    and these counts read identically whether ten of the ten columns are RESTRICT or one is.
    Reproduced with nine reverted to CASCADE -- both this and the test above pass. What the pair
    holds is the runtime refusal and the caller's view of it, which is worth having and is not the
    extent of it; the extent is asserted off the catalogue, column by column, in
    `test_every_observation_foreign_key_is_declared_restrict_by_name` below.
    [M4.13 cycle 2, M413-C2-D6-01]
    """
    target = await _observed(db)
    before = {name: await db.fetchval(q, target) for name, q in _OBSERVATIONS.items()}
    missing = [name for name, count in before.items() if not count]
    assert not missing, f"the fixture wrote no row for {missing}"

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.execute("DELETE FROM title WHERE id = $1", target)

    after = {name: await db.fetchval(q, target) for name, q in _OBSERVATIONS.items()}
    assert after == before, f"the refused delete still took rows: {after}, was {before}"
    assert await db.fetchval("SELECT count(*) FROM title WHERE id = $1", target) == 1


# The ten foreign-key columns 0022 section 2 re-declares, by the names the migration gives them.
# Ten over eight tables, because `duel` and `session_answer` carry a title on either side -- and
# spelled out here rather than derived from `_OBSERVATIONS`, since a derivation would be the same
# guess the schema is being asked about.
_RESTRICTED = (
    ("verdict", "title_id", "verdict_title_id_fkey"),
    ("duel", "title_a", "duel_title_a_fkey"),
    ("duel", "title_b", "duel_title_b_fkey"),
    ("tier_edit", "title_id", "tier_edit_title_id_fkey"),
    ("user_title", "title_id", "user_title_title_id_fkey"),
    ("session_answer", "title_a", "session_answer_title_a_fkey"),
    ("session_answer", "title_b", "session_answer_title_b_fkey"),
    ("session_ballot", "title_id", "session_ballot_title_id_fkey"),
    ("session_result", "title_id", "session_result_title_id_fkey"),
    ("session_outcome", "chosen_title_id", "session_outcome_chosen_title_id_fkey"),
)


async def test_every_observation_foreign_key_is_declared_restrict_by_name(db):
    """Which of the ten refuses, which the pair above cannot say.

    A DELETE that raises tells the caller that SOMETHING refused and nothing else: the rollback
    hides how much had already been cascaded away, so nine of these ten can be on CASCADE with
    both tests above green and `ops/m413_exit_criterion.py` still printing 6/6 -- its check 6
    wraps the same DELETE in a transaction and counts rows after it. That is the whole of §10's
    "observations always survive" resting on whichever RI trigger Postgres happens to reach first.

    So the action is read off the catalogue instead of inferred from a raise, which is also the
    argument `test_migrations.py::test_every_observation_table_can_be_searched_by_the_title_it_names`
    makes for the sibling indexes: an ALTER that named the wrong column would still apply. The
    column is asserted beside the action, so a constraint that kept its name over a different
    column cannot pass, and the eight tables are swept afterwards so a new observation column
    cannot arrive on CASCADE without this test being read. The derived tables are deliberately
    absent: `test_a_title_carrying_only_derived_rows_still_deletes` is the other half of that
    choice, and a title that could never be removed is not the guarantee 0022 wanted.
    [M4.13 cycle 2, M413-C2-D6-01]
    """
    rows = await db.fetch(
        """
        -- ::text because `confdeltype` is a `"char"`, which asyncpg hands back as b'r' -- and a
        -- comparison against the wrong type is how a catalogue assertion fails open.
        SELECT rel.relname AS table_name, con.conname, con.confdeltype::text AS delete_action,
               pg_get_constraintdef(con.oid) AS definition
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_class ref ON ref.oid = con.confrelid
        WHERE con.contype = 'f' AND ref.relname = 'title'
        """
    )
    held = {(r["table_name"], r["conname"]): r for r in rows}

    wrong = []
    for table, column, name in _RESTRICTED:
        row = held.get((table, name))
        if row is None:
            wrong.append(f"{table}.{column}: no foreign key named {name} references title")
        elif f"({column})" not in row["definition"]:
            wrong.append(f"{name} no longer covers {table}.{column}: {row['definition']}")
        elif row["delete_action"] != "r":
            wrong.append(f"{name} is declared {row['definition']}, not ON DELETE RESTRICT")
    assert not wrong, (
        "0022 section 2 declares ten observation foreign keys ON DELETE RESTRICT and the "
        "catalogue disagrees:" + "".join(f"\n  {line}" for line in wrong)
    )

    named = {(table, name) for table, _column, name in _RESTRICTED}
    unlisted = sorted(
        f"{table}.{conname}" for (table, conname) in held
        if table in _OBSERVATIONS and (table, conname) not in named
    )
    assert not unlisted, (
        f"these observation foreign keys reference title and are in no RESTRICT list: {unlisted}"
    )


async def test_a_title_carrying_only_derived_rows_still_deletes(db):
    """RESTRICT on the observations must not become an obstruction everywhere else.

    `ledger_state`, `user_score`, `title_prior`, `title_placement` and `acquisition_job` are
    recomputed outputs -- the next refit and the next sweep rewrite every row of them -- so a
    delete that had to be hand-cleared of them first would make the guarantee above read as "a
    title can never be removed". They stay on CASCADE, and this is the half of that choice a
    constraint cannot state.

    `display.platform_rating` is the exception, and the exception is deliberate: `0003:177-184`
    refuses it a cross-schema FK so that a display row can never put rule 3's schema in the feature
    builder's query plan, which leaves it behind as an orphan. Asserted rather than assumed,
    because it is what the reaping in `importer/load.py` exists to clear.
    """
    user = await _user(db)
    target = await _title(db, 7)
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) "
        "VALUES ('derived-v1', '{}', 'active')"
    )
    await db.execute(
        "INSERT INTO ledger_state (user_id, title_id, s, sigma, kind) "
        "VALUES ($1, $2, 0.5, 0.2, 'movie')",
        user, target,
    )
    await db.execute(
        "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
        "VALUES ($1, $2, 'movie', 'derived-v1', 0.5, 0)",
        user, target,
    )
    await db.execute(
        "INSERT INTO title_prior (title_id, bundle_version, b, item_n, gate, e_source) "
        "VALUES ($1, 'derived-v1', 0.1, 120, 0.92, 'backbone')",
        target,
    )
    await db.execute(
        """
        INSERT INTO title_placement (title_id, bundle_version, e_hat, b_hat, contract_sha256,
                                     tower_sha256, input_dim, blocks_present, blocks_dropped,
                                     blocks_imputed, nnz)
        VALUES ($1, 'derived-v1', $2, 0.25, 'contract-sha', 'tower-sha', 128,
                '{meta}', '{}', '{}', 64)
        """,
        target, b"\x00" * 256,
    )
    await db.execute(
        "INSERT INTO acquisition_job (title_id, stage, status) VALUES ($1, 2, 'parked')", target
    )
    await db.execute(
        "INSERT INTO display.platform_rating (title_id, platform, score) VALUES ($1, 'imdb', 7.4)",
        target,
    )

    await db.execute("DELETE FROM title WHERE id = $1", target)

    assert await db.fetchval("SELECT count(*) FROM title WHERE id = $1", target) == 0
    for table in ("ledger_state", "user_score", "title_prior", "title_placement",
                  "acquisition_job"):
        left = await db.fetchval(f"SELECT count(*) FROM {table} WHERE title_id = $1", target)
        assert left == 0, f"{table} kept a row for a title that is gone"
    orphan = await db.fetchval(
        "SELECT count(*) FROM display.platform_rating WHERE title_id = $1", target
    )
    assert orphan == 1, (
        "rule 3's schema has no FK by design, so the orphan is expected here -- and it is what "
        "importer/load.py's reload path reaps"
    )


async def test_the_reload_path_reaps_a_display_row_whose_title_is_gone(db):
    """Decision 239: the reaping lives in the importer, not in a cross-schema FK.

    `0003:177-184` keeps `display.platform_rating` un-referenced on purpose, so nothing in the
    database can clear the orphan the test above leaves. The reload path is the one place that can:
    it is the only code that sees the catalogue's id set change, and §10 calls a re-import "a
    planned admin event with a diff report", which is exactly when a stale display row should go.
    """
    from spielplan.importer import load

    kept = await _title(db, 3)
    await db.execute(
        "INSERT INTO display.platform_rating (title_id, platform, score) VALUES "
        "($1, 'imdb', 7.4), (4242, 'imdb', 6.1), (4242, 'tmdb', 6.4)",
        kept,
    )

    await load._reap_display_orphans(db)

    assert await db.fetchval("SELECT count(*) FROM display.platform_rating") == 1
    assert await db.fetchval("SELECT title_id FROM display.platform_rating") == kept


# --- §4.2: "length = |tier set| - 1, ordered ascending", as a constraint -------------------


async def test_cutpoints_refuse_an_empty_or_one_level_tier_set(db):
    """`array_length('{}', 1)` is NULL, so the shipped `cutpoints_length` passed for exactly the
    two rows it existed to refuse. A board with no levels has no tiers to render and a board with
    one has no boundary to fit; both are refused on the write side by `rank/tiers.py`'s MIN_TIERS,
    and 0022 is the second line under it."""
    user = await _user(db)
    for tier_set in ("ARRAY[]::text[]", "ARRAY['only']"):
        with pytest.raises(asyncpg.CheckViolationError, match="cutpoints_length"):
            await db.execute(
                "INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set) "
                f"VALUES ($1, 'movie', ARRAY[]::double precision[], {tier_set})",
                user,
            )
    assert await db.fetchval("SELECT count(*) FROM ledger_cutpoints") == 0


async def test_cutpoints_refuse_descending_boundaries_and_accept_coincident_ones(db):
    """Both halves, because the easy constraint here is the wrong one.

    §4.2 says "ordered ascending" and nothing enforced it: a strictly descending six-element array
    inserted. But the fitter's cone is CLOSED -- `ledger/model.py:151-155` admits coincident
    cutpoints and `rank/tiers.py:105-108` refuses to nudge them apart, because a person whose whole
    board sits on one value has levels of genuinely zero width. A CHECK that demanded a strict
    increase would be a nightly refit failing its INSERT inside a background transaction, which is
    the `0009_scoring.sql:56-65` incident in a different column.
    """
    user = await _user(db)
    seven = "ARRAY['F','D','C','B','A','A+','S']"
    with pytest.raises(asyncpg.CheckViolationError, match="cutpoints_ascend"):
        await db.execute(
            "INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set) "
            f"VALUES ($1, 'movie', ARRAY[0.6,0.5,0.4,0.3,0.2,0.1], {seven})",
            user,
        )
    await db.execute(
        "INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set) "
        f"VALUES ($1, 'movie', ARRAY[0.1,0.1,0.3,0.3,0.5,0.5], {seven})",
        user,
    )
    kept = await db.fetchval(
        "SELECT boundaries FROM ledger_cutpoints WHERE user_id = $1 AND kind = 'movie'", user
    )
    assert kept == [0.1, 0.1, 0.3, 0.3, 0.5, 0.5]


# --- Three CHECKs that mean their names ----------------------------------------------------


async def test_a_rate_session_cannot_name_one_kind_twice(db):
    """`kinds <@ ARRAY['movie','series']` is containment, and containment ignores duplicates, so
    `['movie','movie']` satisfied it and the cardinality bound together -- a session with two of
    one kind and none of the other, which §4.1 rule 5 has no reading for.

    The canonical order is pinned in the same breath: `db/library.py`'s `normalise_kinds` builds
    its answer by filtering KINDS, so ('series', 'movie') is not a preference a caller expressed
    but a writer that went around the helper.
    """
    user = await _user(db)
    for kinds in ("ARRAY['movie','movie']", "ARRAY['series','movie']"):
        with pytest.raises(asyncpg.CheckViolationError, match="rate_session_kinds_distinct"):
            await db.execute(
                f"INSERT INTO rate_session (user_id, kinds) VALUES ($1, {kinds})", user
            )
    # Ended, because `rate_session_one_live` allows one live session per user and the subject here
    # is the kinds array, not that index.
    for kinds in ("ARRAY['movie']", "ARRAY['series']", "ARRAY['movie','series']"):
        await db.execute(
            f"INSERT INTO rate_session (user_id, kinds, ended_at) VALUES ($1, {kinds}, now())",
            user,
        )
    assert await db.fetchval("SELECT count(*) FROM rate_session WHERE user_id = $1", user) == 3


async def test_a_session_seat_below_one_is_refused(db):
    """§6.2's seat is the 1-based hand-the-phone order. `session_participant_seat` is unique on
    (session_id, seat), which made -3 as acceptable as 2 while reading like a validity check."""
    user = await _user(db)
    sid = await _session(db, user)
    for seat in (-3, 0):
        with pytest.raises(asyncpg.CheckViolationError, match="seat_positive"):
            await _seat(db, sid, user_id=user, role="host", seat=seat)
    assert await _seat(db, sid, user_id=user, role="host", seat=1)


async def test_a_verdict_can_neither_supersede_nor_re_ask_itself(db):
    """§4.2 makes the verdict chain append-only and `observations.py` walks it -- a row pointing at
    itself is a cycle of length one with no terminating case. Both columns are nullable, so the
    constraint is IS DISTINCT FROM rather than <>.

    `duel.reask_of` carries the same defect and the same one-line fix, and it is asserted here
    rather than in a test of its own because it is the same constraint under a second name.
    """
    user = await _user(db)
    await _title(db)
    await _title(db, 2, kind="series")
    verdict = await db.fetchval(
        "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, 1, 2) RETURNING id", user
    )
    for column in ("superseded_by", "reask_of"):
        with pytest.raises(asyncpg.CheckViolationError, match="verdict_not_self"):
            await db.execute(f"UPDATE verdict SET {column} = id WHERE id = $1", verdict)
    duel = await db.fetchval(
        "INSERT INTO duel (user_id, title_a, title_b, outcome, context) "
        "VALUES ($1, 1, 2, 'A', 'tier_queue') RETURNING id",
        user,
    )
    with pytest.raises(asyncpg.CheckViolationError, match="duel_not_self"):
        await db.execute("UPDATE duel SET reask_of = id WHERE id = $1", duel)


# --- §4.1 rule 5 lives in the data, and now in a constraint --------------------------------


async def test_a_ledger_state_row_whose_kind_disagrees_with_its_title_is_refused(db):
    """`0009_scoring.sql:27-33` says `kind` is in the row "so no ranked query can sort across the
    partition by omission: §4.1 rule 5 lives in the data, not in a convention someone has to
    remember" -- and then nothing tied the copy to `title.kind`. A 'series' row for a movie title
    inserted, which is a board mixing the two partitions every read is written to keep apart.

    The composite FK is one constraint doing the work of two: it holds the reference and the
    agreement, which is why the single-column FK is dropped rather than kept beside it.
    """
    user = await _user(db)
    movie = await _title(db, 1)
    with pytest.raises(asyncpg.ForeignKeyViolationError, match="ledger_state_title_kind_fkey"):
        await db.execute(
            "INSERT INTO ledger_state (user_id, title_id, s, sigma, kind) "
            "VALUES ($1, $2, 0.5, 0.2, 'series')",
            user, movie,
        )
    await db.execute(
        "INSERT INTO ledger_state (user_id, title_id, s, sigma, kind) "
        "VALUES ($1, $2, 0.5, 0.2, 'movie')",
        user, movie,
    )
    assert await db.fetchval("SELECT count(*) FROM ledger_state WHERE user_id = $1", user) == 1


async def test_a_user_score_row_whose_kind_disagrees_with_its_title_is_refused(db):
    """The §5.1 half of the same rule: `user_score.kind` is what `user_score_rank` sorts within, so
    a disagreeing copy puts a film in the series chart with no query able to notice.

    The re-import's own path is asserted too, because it is the reason the FK carries ON UPDATE
    CASCADE: `load.py`'s `_upsert_titles` sets `kind` from EXCLUDED, so a corpus reclassification
    moves a title between partitions, and under the default NO ACTION that UPDATE would fail for
    any reclassified title a member has a score for -- a schema repair becoming an outage of the
    one job §10 promises.
    """
    user = await _user(db)
    movie = await _title(db, 1)
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) VALUES ('kind-v1', '{}', 'active')"
    )
    with pytest.raises(asyncpg.ForeignKeyViolationError, match="user_score_title_kind_fkey"):
        await db.execute(
            "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
            "VALUES ($1, $2, 'series', 'kind-v1', 0.5, 0)",
            user, movie,
        )
    await db.execute(
        "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
        "VALUES ($1, $2, 'movie', 'kind-v1', 0.5, 0)",
        user, movie,
    )
    await db.execute("UPDATE title SET kind = 'series' WHERE id = $1", movie)
    moved = await db.fetchval(
        "SELECT kind FROM user_score WHERE user_id = $1 AND title_id = $2", user, movie
    )
    assert moved == "series", "a reclassified title must take its derived rows with it"


# --- The K a tier edit was written under, backfilled over rows that were already there -----


async def test_the_tier_edit_k_column_is_backfilled_from_the_users_own_tier_set(pg_url, tmp_path):
    """0022's backfill, run over rows that existed before it. §4.2 tier_edit; decision 11.

    A database of its own rather than the `db` fixture's, for the reason `test_upgrade_drill.py`
    opens with: every other layer of this suite applies the migrations to an EMPTY database, so no
    `UPDATE ... SET` backfill in this repository has ever run over a row that was already there --
    and "the migration applied" and "the migration computed the right value" are different claims.
    This is the only test of 0022 that can make the second one.

    The value has to come through the TITLE, because `tier_edit` has no `kind` of its own and
    `ledger_cutpoints` is keyed (user_id, kind): the board a drop was made on is the board for that
    title's kind. So one person with a 12-label movie board and no series row gets 12 on a film and
    7 -- §4.2's default set -- on a series, and a person with no cutpoints row at all gets 7
    everywhere. A backfill that read the person's only row regardless of kind would stamp the series
    edit 12, which is precisely the mislabelling the column exists to end.
    """
    earlier = [version for version, _ in migrate.discover() if version < UNDER_TEST]
    admin, name, url = _sibling(pg_url, "_nlevels")
    await _recreate(admin, name)
    conn = await asyncpg.connect(url)
    try:
        directory = _stage(tmp_path, earlier[-1])
        assert await migrate.apply_all(conn, directory)
        patrick = await conn.fetchval(
            "INSERT INTO app_user (name, role) VALUES ('patrick', 'admin') RETURNING id"
        )
        jenny = await conn.fetchval(
            "INSERT INTO app_user (name, role) VALUES ('jenny', 'member') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO title (id, kind, name) VALUES (11, 'movie', 'a film'), "
            "(12, 'series', 'a series')"
        )
        await conn.execute(
            "INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set) VALUES "
            "($1, 'movie', ARRAY[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0,1.1], "
            "ARRAY['1','2','3','4','5','6','7','8','9','10','11','12'])",
            patrick,
        )
        for user, title in ((patrick, 11), (patrick, 12), (jenny, 11)):
            await conn.execute(
                "INSERT INTO tier_edit (user_id, title_id, tier, via) "
                "VALUES ($1, $2, 3, 'explicit')",
                user, title,
            )

        assert UNDER_TEST in _complete(directory)
        assert UNDER_TEST in await migrate.apply_all(conn, directory)

        levels = {
            (row["user_id"], row["title_id"]): row["n_levels"]
            for row in await conn.fetch("SELECT user_id, title_id, n_levels FROM tier_edit")
        }
        assert levels[(patrick, 11)] == 12, "the edit was made on his own 12-label movie board"
        assert levels[(patrick, 12)] == 7, (
            "he has no series cutpoints, so the series edit was made on the default set -- "
            "reading his movie row here would be the mislabelling the column exists to end"
        )
        assert levels[(jenny, 11)] == 7, "she has no cutpoints at all"
    finally:
        await conn.close()
        await _drop(admin, name)
