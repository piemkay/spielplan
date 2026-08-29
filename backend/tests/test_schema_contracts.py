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
