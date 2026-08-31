"""The constraints, asserted by trying to violate them. Spec v2.1 §4.1, §4.2, §10.

`test_migrations.py` checks the schema's *shape* against PGlite. These check its *behaviour*
against a real server: a CHECK constraint that is never tried is a comment with punctuation.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import asyncpg
import pytest


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
    for version in ("v1", "v2"):
        await db.execute(
            "INSERT INTO artifact_bundle (version, manifest, state) VALUES ($1, '{}', 'staged')",
            version,
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
