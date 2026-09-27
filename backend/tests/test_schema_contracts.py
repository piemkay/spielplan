"""Constraints asserted by trying to violate them: a CHECK never tried is a comment with punctuation."""

from __future__ import annotations

import re

import asyncpg
import pytest

from spielplan.db import migrate
from tests.helpers import create_database, drop_database, sibling
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


async def test_kind_is_not_null_and_constrained_to_two_values(db):
    with pytest.raises(asyncpg.NotNullViolationError):
        await db.execute("INSERT INTO title (id, kind, name) VALUES (900, NULL, 'x')")
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute("INSERT INTO title (id, kind, name) VALUES (901, 'episode', 'x')")


async def test_kind_is_indexed(db):
    indexes = await db.fetch(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'title' AND schemaname = 'public'"
    )
    defs = " ".join(r["indexdef"] for r in indexes)
    assert "(kind)" in defs or "(kind, " in defs


async def test_the_database_refuses_a_renumbered_rating_source(db):
    """The validator catches this at import; the CHECK is the second line. These ids key fitted_cuts."""
    await db.execute("INSERT INTO rating_source (id, name) VALUES (1, 'ok')")
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute("INSERT INTO rating_source (id, name) VALUES (99, 'renumbered')")


async def test_duplicate_tmdb_ids_are_accepted(db):
    """315 duplicates exist and are legitimate, mostly movie/series pairs."""
    await db.execute("INSERT INTO title (id, kind, name, tmdb_id) VALUES (910, 'movie', 'a', 42)")
    await db.execute("INSERT INTO title (id, kind, name, tmdb_id) VALUES (911, 'series', 'b', 42)")
    assert await db.fetchval("SELECT count(*) FROM title WHERE tmdb_id = 42") == 2


async def test_dna_tag_provider_is_not_null_so_its_unique_index_fires(db):
    """NULLs are distinct in a unique index, so a NULL `provider` defeated it. Default and NOT NULL
    are separate assertions: neither implies the other."""
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


async def test_seen_state_has_exactly_two_values(db):
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
    """22% of random pairs are genuine ties."""
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
    """The held-out stream must be separable by query, or §13's guard is unenforceable."""
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
    """Decision 11 makes the tier set per-user, so the invariant holds per row."""
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


async def test_only_one_bundle_can_be_active(db):
    # 0015's `artifact_bundle_one_seed` allows one seed row, so the second is a model bundle.
    for version, kind in (("v1", "seed"), ("v2", "model")):
        await db.execute(
            "INSERT INTO artifact_bundle (version, manifest, state, kind)"
            " VALUES ($1, '{}', 'staged', $2)",
            version, kind,
        )
    await db.execute("UPDATE artifact_bundle SET state = 'active' WHERE version = 'v1'")
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.execute("UPDATE artifact_bundle SET state = 'active' WHERE version = 'v2'")


async def _bundle(db, version: str, state: str) -> str:
    """`kind = 'model'`: `artifact_bundle_one_seed` (0015) allows one seed row per install."""
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state, kind) "
        "VALUES ($1, '{}', $2, 'model')",
        version, state,
    )
    return version


async def test_the_active_bundle_row_cannot_be_deleted(db):
    """The message names version and state: a bare FK error says nothing about why the row exists."""
    await _bundle(db, "active-v1", "active")
    with pytest.raises(asyncpg.RaiseError, match="active-v1 is state active and is provenance"):
        await db.execute("DELETE FROM artifact_bundle WHERE version = 'active-v1'")
    assert await db.fetchval(
        "SELECT state FROM artifact_bundle WHERE version = 'active-v1'"
    ) == "active"


async def test_a_superseded_bundle_row_cannot_be_deleted_either(db):
    """Deleting it would cascade §5.1's scores and §5.3's priors and SET NULL the title's basis, so
    the survivors are asserted, not just the raise."""
    user = await _user(db)
    target = await _title(db, 1)
    await _bundle(db, "sup-v1", "superseded")
    await db.execute(
        "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
        "VALUES ($1, $2, 'movie', 'sup-v1', 0.5, 0)",
        user, target,
    )
    await db.execute(
        "INSERT INTO title_prior (title_id, bundle_version, b, item_n, gate, e_source) "
        "VALUES ($1, 'sup-v1', 0.1, 120, 0.92, 'backbone')",
        target,
    )
    await db.execute(
        "UPDATE title SET placement = 'warm', placement_bundle = 'sup-v1', placement_at = now() "
        "WHERE id = $1",
        target,
    )

    with pytest.raises(asyncpg.RaiseError, match="sup-v1 is state superseded and is provenance"):
        await db.execute("DELETE FROM artifact_bundle WHERE version = 'sup-v1'")

    assert await db.fetchval(
        "SELECT count(*) FROM user_score WHERE bundle_version = 'sup-v1'"
    ) == 1
    assert await db.fetchval(
        "SELECT count(*) FROM title_prior WHERE bundle_version = 'sup-v1'"
    ) == 1
    assert await db.fetchval(
        "SELECT placement_bundle FROM title WHERE id = $1", target
    ) == "sup-v1", "the title still names the basis its coordinate was computed in"


async def test_a_staged_or_failed_row_may_be_deleted(db):
    """Nothing cites staged or failed rows, so they may go. 'validated' is on the other side: the
    importer writes it inside the flip transaction (decision 253)."""
    await _bundle(db, "staged-v1", "staged")
    await _bundle(db, "failed-v1", "failed")
    await _bundle(db, "validated-v1", "validated")

    await db.execute("DELETE FROM artifact_bundle WHERE version IN ('staged-v1', 'failed-v1')")
    assert await db.fetchval("SELECT count(*) FROM artifact_bundle") == 1

    with pytest.raises(asyncpg.RaiseError, match="validated-v1 is state validated"):
        await db.execute("DELETE FROM artifact_bundle WHERE version = 'validated-v1'")


async def test_a_staged_or_failed_row_something_still_cites_is_refused_with_the_rule(db):
    """A hand-built row can falsify decision 249's premise. The arms fail differently: SET NULL
    trips a CHECK, while `session.bundle_version` RESTRICTs."""
    target = await _title(db, 1)
    await _bundle(db, "failed-v1", "failed")
    await db.execute(
        "UPDATE title SET placement = 'cold_tower', placement_bundle = 'failed-v1', "
        "placement_at = now() WHERE id = $1",
        target,
    )
    with pytest.raises(asyncpg.RaiseError, match="failed-v1 is state failed and is provenance"):
        await db.execute("DELETE FROM artifact_bundle WHERE version = 'failed-v1'")

    host = await _user(db)
    await _bundle(db, "staged-v1", "staged")
    await db.execute(
        "INSERT INTO session (room_code, host_user_id, kind, bundle_version) "
        "VALUES ('MX-2210', $1, 'movie', 'staged-v1')",
        host,
    )
    with pytest.raises(asyncpg.RaiseError, match="staged-v1 is state staged and is provenance"):
        await db.execute("DELETE FROM artifact_bundle WHERE version = 'staged-v1'")

    assert await db.fetchval("SELECT count(*) FROM artifact_bundle") == 2


async def test_the_rule_asks_every_table_that_can_be_citing_the_row(db):
    """The trigger's six tables are hand-kept, so the catalog is asked for every FK. `user_vector`
    is excluded by decision 249: a NULL stamp already marks it stale."""
    referencing = [
        r["referencing"] for r in await db.fetch(
            "SELECT DISTINCT conrelid::regclass::text AS referencing FROM pg_constraint "
            " WHERE contype = 'f' AND confrelid = 'artifact_bundle'::regclass ORDER BY 1"
        )
    ]
    assert "user_vector" in referencing, "the documented exception is still a foreign key"
    body = await db.fetchval(
        "SELECT prosrc FROM pg_proc WHERE proname = 'artifact_bundle_is_provenance'"
    )
    missing = [
        table for table in referencing
        if table != "user_vector" and not re.search(rf"FROM\s+{table}\b", body)
    ]
    assert not missing, (
        f"these tables can cite an artifact_bundle row and the rule does not ask them: {missing}"
        " -- a staged or failed row one of them points at would be deleted, and the operator"
        " would get that table's constraint name instead of decision 249"
    )


async def test_a_placed_title_with_no_placement_bundle_is_refused_by_the_check(pg_url, tmp_path):
    """A database of its own: every other layer migrates an EMPTY database, so this backfill's
    UPDATE would never run over a row."""
    # Not `UNDER_TEST` (0022): that drill stages the migrations below it.
    under_test = "0023_import_state"
    earlier = [version for version, _ in migrate.discover() if version < under_test]
    admin, name, url = sibling(pg_url, "_basis")
    await create_database(admin, name)
    conn = await asyncpg.connect(url)
    try:
        directory = _stage(tmp_path, earlier[-1])
        assert await migrate.apply_all(conn, directory)
        await conn.execute(
            "INSERT INTO artifact_bundle (version, manifest, state) "
            "VALUES ('basis-v1', '{}', 'active')"
        )
        await conn.execute(
            "INSERT INTO title (id, kind, name, is_owned) VALUES "
            "(21, 'movie', 'stranded', true), (22, 'movie', 'placed', true)"
        )
        await conn.execute(
            "UPDATE title SET placement = 'cold_tower', placement_at = now() WHERE id = 21"
        )
        await conn.execute(
            "UPDATE title SET placement = 'warm', placement_bundle = 'basis-v1', "
            "placement_at = now() WHERE id = 22"
        )
        assert await conn.fetchval(
            "SELECT count(*) FROM title WHERE is_owned AND placement = 'unplaced'"
        ) == 0, "the defect as M2 reads it: nothing appears to be waiting for a coordinate"

        assert under_test in _complete(directory)
        assert under_test in await migrate.apply_all(conn, directory)

        stranded = await conn.fetchrow(
            "SELECT placement, placement_at, placement_bundle FROM title WHERE id = 21"
        )
        assert stranded["placement"] == "unplaced", "a coordinate with no basis is not a placement"
        # The backfill clears the stamp because it dated a placement that never happened.
        assert stranded["placement_at"] is None, "this row's stamp dated a placement that never was"

        placed = await conn.fetchrow(
            "SELECT placement, placement_at, placement_bundle FROM title WHERE id = 22"
        )
        assert (placed["placement"], placed["placement_bundle"]) == ("warm", "basis-v1"), (
            "the backfill must not touch a title whose placement does name its basis"
        )
        assert placed["placement_at"] is not None, "nor clear the stamp of a real placement"
        assert await conn.fetchval(
            "SELECT count(*) FROM title WHERE is_owned AND placement = 'unplaced'"
        ) == 1, "M2's index now counts the title that has no coordinate"

        # Both halves are reachable: a writer stamping `placement` alone, and the FK's SET NULL.
        with pytest.raises(asyncpg.CheckViolationError, match="title_placement_has_basis"):
            await conn.execute(
                "INSERT INTO title (id, kind, name, placement) "
                "VALUES (23, 'movie', 'no basis', 'cold_tower')"
            )
        with pytest.raises(asyncpg.CheckViolationError, match="title_placement_has_basis"):
            await conn.execute("UPDATE title SET placement_bundle = NULL WHERE id = 22")
        with pytest.raises(asyncpg.CheckViolationError, match="title_placement_has_basis"):
            await conn.execute("UPDATE title SET placement = 'unplaced' WHERE id = 22")
    finally:
        await conn.close()
        await drop_database(admin, name)


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


async def _session(db, host: int, *, kind: str = "movie", budget: int = 130) -> int:
    """A session's NOT NULL basis needs a bundle row to exist (§10)."""
    await db.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) VALUES ('test-v1', '{}', 'active') "
        "ON CONFLICT DO NOTHING"
    )
    # `session_room_code_live` is unique among live rooms, so a distinct code per call.
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
    """Decision 154: `EITHER` lifts both and `NEITHER` lowers both; the prototype's `NO_PULL` is refused."""
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
    """The spelling is `uniform_holdout`, as in `duel.selection`: a second spelling breaks exclusions."""
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
    """§14 risk 6 wants the rate of each; an undefined fourth value makes it unreadable."""
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
    """Both directions: either alone passes for an implementation that never stamps the column."""
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
    """Two guests on the host phone is the designed case; a (session_id, user_id) key drops one."""
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
    """A member arriving twice must re-attach; two seats would skew every per-participant average."""
    user = await _user(db)
    other = await _user(db, name="jenny")
    sid = await _session(db, user)
    await _seat(db, sid, user_id=user, role="host", seat=1)
    await _seat(db, sid, user_id=other, role="member", seat=2)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _seat(db, sid, user_id=user, role="member", seat=3)


async def test_a_ballot_is_one_row_per_participant_and_title(db):
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


# Decision 173 ships no `dna_axis_weight` rows, so release data never writes `reserved = true`.
_PACE = {"pace": {"slow": -1.0, "fast": 1.0}}


async def _slate_session(db, *, titles, scores, tilts):
    """The pool goes straight into `session.context`: a real pool needs scores, fold-in and a bundle."""
    host = await _user(db, name="patrick")
    other = await _user(db, name="jenny")
    sid = await _session(db, host)
    for title_id in titles:
        await _title(db, title_id)
    seats = [
        await _seat(db, sid, user_id=host, role="host", seat=1),
        await _seat(db, sid, user_id=other, role="member", seat=2),
    ]
    for pid, tilt in zip(seats, tilts, strict=True):
        await db.execute("UPDATE session_participant SET tilt = $2 WHERE id = $1", pid, tilt)
    await db.execute(
        "UPDATE session SET state = 'voting', context = $2 WHERE id = $1",
        sid,
        {"pool": {
            "candidates": {str(t): {"name": f"t{t}"} for t in titles},
            # Per seat: D is Ledger divergence, and identical scores give D = 0 and nothing to reserve.
            "scores": {
                str(t): {str(p): v for p, v in zip(seats, scores[t], strict=True)}
                for t in titles
            },
            "dna": {str(t): v for t, v in titles.items()},
            "axes": _PACE,
            "version": "test-v1",
        }},
    )
    return sid, seats


async def test_a_slate_row_is_not_reserved_until_something_reserves_it(db):
    """NOT NULL DEFAULT false: every stored row predates any reservation (decision 220)."""
    user = await _user(db)
    sid = await _session(db, user)
    title = await _title(db, 1)
    await db.execute(
        "INSERT INTO session_result (session_id, title_id, rank, slot, group_score) "
        "VALUES ($1, $2, 1, 'finalist', 0.5)",
        sid, title,
    )
    assert await db.fetchval(
        "SELECT reserved FROM session_result WHERE session_id = $1", sid
    ) is False, "a slate written without a reservation carries none"
    with pytest.raises(asyncpg.NotNullViolationError):
        await db.execute("UPDATE session_result SET reserved = NULL WHERE session_id = $1", sid)
    # Orthogonal to the slot: `slot IN ('finalist','wildcard')` filters keep counting it.
    await db.execute(
        "UPDATE session_result SET reserved = true WHERE session_id = $1", sid
    )
    assert await db.fetchval(
        "SELECT count(*) FROM session_result WHERE session_id = $1 AND slot = 'finalist'", sid
    ) == 1


async def test_the_reserved_finalist_is_the_one_the_stored_slate_labels(db):
    """Through `play.finish` and `result.slate`: rule, INSERT and reveal each used to drop the field."""
    from spielplan.tonight import play
    from spielplan.tonight import result as result_rules

    titles = {1: {"slow": 1.0}, 2: {"slow": 1.0}, 3: {"slow": 1.0}, 4: {"slow": 1.0},
              5: {"fast": 1.0}}
    sid, seats = await _slate_session(
        db, titles=titles,
        # Group scores 0.95/0.90/0.85/0.80/0.30; the members are 0.60 apart on the leader, so D = 0.30.
        scores={1: (1.25, 0.65), 2: (0.90, 0.90), 3: (0.85, 0.85), 4: (0.80, 0.80),
                5: (0.30, 0.30)},
        tilts=({"slow": 1.0}, {"fast": 1.0}),
    )
    slate = await play.finish(db, sid)

    assert slate.contested == "pace", "the fixture only says anything while the split surfaces"
    assert slate.reserved is not None and slate.reserved in slate.finalists
    rows = await db.fetch(
        "SELECT title_id, rank, slot, reserved FROM session_result "
        " WHERE session_id = $1 ORDER BY rank",
        sid,
    )
    assert [r["title_id"] for r in rows if r["reserved"]] == [slate.reserved], (
        "exactly the counterweight, and exactly one of them"
    )
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    assert [r["title_id"] for r in rows[:3]] == slate.finalists, (
        "the stored ranks read in slate order, so ORDER BY rank lists the finalists first"
    )
    assert rows[3]["slot"] == "wildcard"

    # Every seat votes first: the reveal refuses until every ballot is in (54e).
    from spielplan.tonight import ballot as ballot_rules

    for seat in seats:
        await ballot_rules.submit(db, participant_id=seat, approved=[slate.finalists[0]])
    reveal = await result_rules.slate(
        db, sid, [], {"chosen_title_id": slate.finalists[0], "approval_share": 0.5,
                      "participants": 2},
    )
    assert [c["title_id"] for c in reveal["finalists"] if c["reserved"]] == [slate.reserved], (
        "the reveal can say which card is the other side of the split"
    )
    assert reveal["wildcard"]["reserved"] is False


# Each test below attempts the write the OLD constraint accepted.

# Named once: the backfill test stages the release before it.
UNDER_TEST = "0022_model_basis"

# The tables 0022 moved from CASCADE to RESTRICT, shared by the delete and the count below.
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
    """`duel` and `session_answer` get two rows each: a title sits on either side."""
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
    """Taste data is the one thing this app cannot re-derive; until 0022 CASCADE emptied it silently."""
    target = await _observed(db)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.execute("DELETE FROM title WHERE id = $1", target)


async def test_every_observation_row_survives_the_refused_delete(db):
    """The DELETE is atomic, so these counts cannot tell a partial RESTRICT from a whole one; the
    catalogue test below does."""
    target = await _observed(db)
    before = {name: await db.fetchval(q, target) for name, q in _OBSERVATIONS.items()}
    missing = [name for name, count in before.items() if not count]
    assert not missing, f"the fixture wrote no row for {missing}"

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.execute("DELETE FROM title WHERE id = $1", target)

    after = {name: await db.fetchval(q, target) for name, q in _OBSERVATIONS.items()}
    assert after == before, f"the refused delete still took rows: {after}, was {before}"
    assert await db.fetchval("SELECT count(*) FROM title WHERE id = $1", target) == 1


# Spelled out, not derived from `_OBSERVATIONS`: a derivation would be the guess under test.
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
    """Read off the catalogue: a raise hides which of the ten refused. The column is asserted beside
    the action, and the tables swept so a new column cannot arrive on CASCADE."""
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
    """Derived tables stay on CASCADE. `display.platform_rating` has no cross-schema FK (rule 3), so
    it is left as an orphan for the importer to reap."""
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
    """Decision 239: only the reload path sees the id set change, so it reaps the orphan."""
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


async def test_cutpoints_refuse_an_empty_or_one_level_tier_set(db):
    """`array_length('{}', 1)` is NULL, so the old CHECK passed exactly the rows it should refuse."""
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
    """The fitter's cone is CLOSED: coincident cutpoints are legal, so a strict-increase CHECK
    would fail a nightly refit's INSERT."""
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


async def test_a_rate_session_cannot_name_one_kind_twice(db):
    """Containment ignores duplicates, so `['movie','movie']` passed. The canonical order is pinned:
    `normalise_kinds` filters KINDS."""
    user = await _user(db)
    for kinds in ("ARRAY['movie','movie']", "ARRAY['series','movie']"):
        with pytest.raises(asyncpg.CheckViolationError, match="rate_session_kinds_distinct"):
            await db.execute(
                f"INSERT INTO rate_session (user_id, kinds) VALUES ($1, {kinds})", user
            )
    # Ended: `rate_session_one_live` allows one live session per user.
    for kinds in ("ARRAY['movie']", "ARRAY['series']", "ARRAY['movie','series']"):
        await db.execute(
            f"INSERT INTO rate_session (user_id, kinds, ended_at) VALUES ($1, {kinds}, now())",
            user,
        )
    assert await db.fetchval("SELECT count(*) FROM rate_session WHERE user_id = $1", user) == 3


async def test_a_session_seat_below_one_is_refused(db):
    """The seat is 1-based; the unique (session_id, seat) index accepted -3."""
    user = await _user(db)
    sid = await _session(db, user)
    for seat in (-3, 0):
        with pytest.raises(asyncpg.CheckViolationError, match="seat_positive"):
            await _seat(db, sid, user_id=user, role="host", seat=seat)
    assert await _seat(db, sid, user_id=user, role="host", seat=1)


async def test_a_verdict_can_neither_supersede_nor_re_ask_itself(db):
    """Both columns are nullable, hence IS DISTINCT FROM. `duel.reask_of` has the same constraint."""
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


async def test_a_ledger_state_row_whose_kind_disagrees_with_its_title_is_refused(db):
    """A composite FK holds the reference and the kind agreement, so the single-column FK is dropped."""
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
    """ON UPDATE CASCADE: a re-import reclassifying `kind` must move scored titles, not fail."""
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


async def test_the_tier_edit_k_column_is_backfilled_from_the_users_own_tier_set(pg_url, tmp_path):
    """A database of its own, so the backfill runs over existing rows. K comes through the title's
    kind: 12 on a film, 7 (the default) on a series or with no cutpoints row."""
    earlier = [version for version, _ in migrate.discover() if version < UNDER_TEST]
    admin, name, url = sibling(pg_url, "_nlevels")
    await create_database(admin, name)
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
        await drop_database(admin, name)
