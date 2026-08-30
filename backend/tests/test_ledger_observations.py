"""The Ledger's database side. Spec v2.1 §4.1, §4.2, §5.2, §5.3, §6.1, §12, §13, decision 35.

Three questions, and the tests are grouped by which one they answer.

**What may the fit see?** §4.2 makes `verdict` append-only so that §5.2's fourth arm — "rewatch
re-ratings → new ordinal observation → drift signal for free" — has a history to be. That arm
exists only while `load_observations` returns *both* the superseded row and the live one, and
the way to delete it is not to delete code: it is to add `WHERE superseded_by IS NULL` to a
query, which looks like tidying. §13 pulls in the opposite direction on two other streams: the
uniform-random 10% "is the *only* data used to evaluate the tier model", so a pair the model
was fitted on is not held out, and a silent re-ask measures judgement noise rather than
supplying a second judgement. Four tests below are the guards on those three rules.

**What may write?** Nothing updates a verdict's value; only `undo` deletes a row, and only
within decision 35's block. A static half of that greps the package, because "no other write
path does this" is a claim about code that no runtime test can make.

**Does it fit inside §5.3's budgets?** Measured, with the numbers printed, on a library at the
spec's own scale. `test_a_full_map_refit_of_both_users_over_the_owned_library_lands_inside_the_budget`
builds 900 titles because §1 says "Ledger refit for 2 users over 839+ titles — seconds".

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import ast
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from spielplan.ledger import model, observations, refit
from spielplan.ledger.hyperparams import DEFAULTS, Hyperparams
from spielplan.ledger.observations import UndoRefused

PACKAGE = Path(__file__).resolve().parents[1] / "spielplan"

# A deterministic 64-d basis. No RNG in the fixture's embeddings: §5.3's budgets and §5.2's
# "same observations, same constants, same fit" are both claims that need the input to be the
# same on two machines.
def _embedding(title_id: int) -> np.ndarray:
    rng = np.random.default_rng(1000 + title_id)
    vector = rng.normal(size=64)
    return vector / (np.linalg.norm(vector) * 8.0)


def fixture_embeddings(title_ids):
    ids = list(title_ids)
    if not ids:
        return np.zeros((0, 64)), np.zeros(0, dtype=bool)
    return (
        np.stack([_embedding(t) for t in ids]),
        np.ones(len(ids), dtype=bool),
    )


async def make_titles(db, specs):
    """specs: [(id, kind, name)]."""
    await db.execute(
        """
        INSERT INTO title (id, kind, name, is_owned)
        SELECT x.id, x.kind, x.name, true
        FROM unnest($1::int[], $2::text[], $3::text[]) AS x(id, kind, name)
        """,
        [s[0] for s in specs],
        [s[1] for s in specs],
        [s[2] for s in specs],
    )


async def make_user(db, name, role="member"):
    return await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ($1, $2) RETURNING id", name, role
    )


@pytest.fixture
async def world(db):
    """One person and eight owned titles: six films, two series."""
    await make_titles(
        db,
        [(i, "movie" if i <= 6 else "series", f"Title {i}") for i in range(1, 9)],
    )
    return {"user": await make_user(db, "patrick", "admin")}


async def live_verdicts(db, user_id, title_id):
    return await db.fetch(
        "SELECT id, value, created_at, superseded_by, is_reask FROM verdict "
        "WHERE user_id = $1 AND title_id = $2 ORDER BY id",
        user_id,
        title_id,
    )


# --- §4.2: append-only ---------------------------------------------------------------------


async def test_a_re_rating_inserts_a_row_and_stamps_the_old_one_rather_than_mutating_it(db, world):
    """§4.2: a re-rating supersedes rather than overwrites.

    The assertion that matters is not "there are two rows" — it is that the FIRST row is
    byte-identical to what it was before the second write. A path that updated `value` in place
    and inserted an audit row would pass a row count and fail this.
    """
    user = world["user"]
    first = await observations.record_verdict(db, user_id=user, title_id=1, value=0)
    before = (await live_verdicts(db, user, 1))[0]

    second = await observations.record_verdict(db, user_id=user, title_id=1, value=2)
    rows = await live_verdicts(db, user, 1)

    assert len(rows) == 2
    assert rows[0]["id"] == first.row_id and rows[1]["id"] == second.row_id
    assert rows[0]["value"] == before["value"] == 0, "the earlier verdict's value was mutated"
    assert rows[0]["created_at"] == before["created_at"]
    assert rows[0]["superseded_by"] == second.row_id
    assert second.superseded_id == first.row_id
    assert [r["superseded_by"] is None for r in rows] == [False, True], "exactly one live row"

    third = await observations.record_verdict(db, user_id=user, title_id=1, value=1)
    rows = await live_verdicts(db, user, 1)
    assert [r["superseded_by"] for r in rows] == [second.row_id, third.row_id, None], (
        "a third rating must supersede only the second and leave the first's pointer alone"
    )


async def test_the_fit_sees_both_the_superseded_verdict_and_the_live_one(db, world):
    """§5.2's fourth arm: "Rewatch re-ratings — new ordinal observation — drift signal for free".

    This is the test that fails the moment anyone adds `WHERE superseded_by IS NULL` to the
    observation query, which is the single most likely regression in this subsystem. A fit that
    saw only the live row would have no history and no fourth arm, and nothing else in the
    suite would notice.
    """
    user = world["user"]
    await observations.record_verdict(db, user_id=user, title_id=1, value=0)
    await observations.record_verdict(db, user_id=user, title_id=1, value=2)

    loaded = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    obs = loaded.obs
    row = int(np.flatnonzero(obs.title_ids == 1)[0])
    levels = sorted(obs.ord_level[(obs.ord_index == row) & (obs.ord_arm == observations.ARM_VERDICT)])

    assert levels == [0, 2], "the superseded verdict is missing from the fit"
    assert loaded.n_verdicts == 2


async def test_the_insert_and_the_supersede_stamp_are_one_transaction(db, world, monkeypatch):
    """A half-superseded state — two live verdicts, or none — must not be reachable.

    The write is INSERT, then stamp, then the implied `seen`. Failing at the last step is the
    cheapest way to ask whether the first two share a transaction; if they did not, the new row
    would survive the failure and the old one would already be stamped.
    """
    user = world["user"]
    first = await observations.record_verdict(db, user_id=user, title_id=1, value=0)

    async def explode(*_args, **_kwargs):
        raise RuntimeError("Jellyfin fell over mid-write")

    monkeypatch.setattr(observations, "_set_state", explode)
    with pytest.raises(RuntimeError):
        await observations.record_verdict(db, user_id=user, title_id=1, value=2)

    rows = await live_verdicts(db, user, 1)
    assert [r["id"] for r in rows] == [first.row_id], "the failed insert survived"
    assert rows[0]["superseded_by"] is None, "a row was stamped as superseded by a row that is gone"


async def test_a_verdict_implies_seen_and_undo_restores_the_exact_prior_state(db, world):
    """§6.1: "Verdict implies `seen`." §7.3: `jf_synced_at` is the loop guard.

    So undo must put back the stamp as well as the state. Restoring `seen` with a NULL stamp
    where the row previously had one would make the next 15-minute sweep push a write nobody
    asked for — the compensating write would create work instead of undoing it.
    """
    user = world["user"]
    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state, jf_synced_at) VALUES ($1,$2,'unseen',$3)",
        user,
        1,
        stamp,
    )

    write = await observations.record_verdict(db, user_id=user, title_id=1, value=2)
    row = await db.fetchrow(
        "SELECT state, jf_synced_at FROM user_title WHERE user_id=$1 AND title_id=$2", user, 1
    )
    assert row["state"] == "seen" and row["jf_synced_at"] is None
    assert write.implied_seen is True

    await observations.undo(db, user_id=user, write=write)
    row = await db.fetchrow(
        "SELECT state, jf_synced_at FROM user_title WHERE user_id=$1 AND title_id=$2", user, 1
    )
    assert (row["state"], row["jf_synced_at"]) == ("unseen", stamp)
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE user_id = $1", user) == 0


async def test_not_seen_writes_no_observation_row_and_keeps_the_history(db, world):
    """§4.2, owner decision 2026-08-29: "no 'forgotten' state — 'seen, don't remember' is marked
    plain `unseen`; verdict/duel history is append-only and survives the flip"."""
    user = world["user"]
    await observations.record_verdict(db, user_id=user, title_id=1, value=2)
    write = await observations.record_not_seen(db, user_id=user, title_id=1)

    assert write.row_id is None
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id=$1 AND title_id=$2", user, 1
    ) == "unseen"
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE user_id=$1", user) == 1, (
        "the flip to unseen deleted the rating history"
    )

    await observations.undo(db, user_id=user, write=write)
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id=$1 AND title_id=$2", user, 1
    ) == "seen"


def _functions_containing(pattern: str) -> set[tuple[str, str]]:
    """(module path, enclosing function) for every line of the package matching `pattern`."""
    hits: set[tuple[str, str]] = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if pattern not in source:
            continue
        lines = {i for i, line in enumerate(source.splitlines(), 1) if pattern in line}
        tree = ast.parse(source)
        rel = path.relative_to(PACKAGE.parent).as_posix()
        for line in lines:
            enclosing = "<module>"
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
                    node.lineno <= line <= (node.end_lineno or node.lineno)
                ):
                    enclosing = node.name
            hits.add((rel, enclosing))
    return hits


def test_no_write_path_outside_undo_deletes_a_verdict_or_edits_its_value():
    """§4.2's append-only rule, as a property of the source rather than of one code path.

    A runtime test can only prove that the paths it calls behave. This asserts that no other
    path exists: across the whole package, `DELETE FROM verdict`, `DELETE FROM duel` and
    `UPDATE verdict SET value` occur only inside `ledger/observations.py::undo`, which is
    decision 35's compensating write and is scoped to one block.
    """
    for pattern in ("DELETE FROM verdict", "DELETE FROM duel", "UPDATE verdict SET value"):
        found = _functions_containing(pattern)
        assert found <= {("spielplan/ledger/observations.py", "undo")}, (
            f"{pattern!r} appears outside undo: {sorted(found)}"
        )
    # And the guard is not vacuous: undo really does contain the deletes it is allowed to.
    assert ("spielplan/ledger/observations.py", "undo") in _functions_containing(
        "DELETE FROM verdict"
    )


async def test_undo_unstamps_the_row_it_superseded_and_stops_at_the_block_boundary(db, world):
    """Decision 35: "an observation journal with compensating writes rather than a lastAction
    variable", scoped to the current block of 15.

    Two halves. Undoing a re-rating must leave the *earlier* verdict live again — a delete that
    forgot the stamp would leave a title with a history and no current rating. And an
    observation from before the block began is refused rather than silently reached: the
    person's "one more tap" ends at the block boundary, and a compensating write they cannot
    see is not an undo.
    """
    user = world["user"]
    first = await observations.record_verdict(db, user_id=user, title_id=1, value=0)
    second = await observations.record_verdict(db, user_id=user, title_id=1, value=2)

    result = await observations.undo(db, user_id=user, write=second)
    rows = await live_verdicts(db, user, 1)
    assert [r["id"] for r in rows] == [first.row_id]
    assert rows[0]["superseded_by"] is None, "the earlier verdict stayed superseded by a ghost"
    assert result.unsuperseded == (first.row_id,)

    later = datetime.now(UTC) + timedelta(seconds=5)
    with pytest.raises(UndoRefused):
        await observations.undo(db, user_id=user, write=first, block_started_at=later)
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE id = $1", first.row_id) == 1


async def test_undo_refuses_another_persons_observation(db, world):
    """The journal is per person. A row id is not an authorisation."""
    user = world["user"]
    other = await make_user(db, "jenny")
    write = await observations.record_verdict(db, user_id=user, title_id=1, value=2)
    with pytest.raises(UndoRefused):
        await observations.undo(db, user_id=other, write=write)


# --- §13 and §4.1 rule 5: what the fit may not see -------------------------------------------


async def test_the_uniform_random_held_out_duels_never_reach_the_fit(db, world):
    """§13 stream (a): "the 10% uniform-random comparison stream is the *only* data used to
    evaluate the tier model — adaptively-selected pairs inflate reliability (measured effect;
    the guard is non-negotiable)."

    A pair the model was fitted on is not held out. The rows stay in the table — the evaluation
    reads them — but the observation set must not contain them, or the project loses the one
    unbiased instrument it has and cannot tell that it has.
    """
    user = world["user"]
    for selection in ("random", "boundary", "exploration"):
        await observations.record_duel(
            db, user_id=user, title_a=1, title_b=2, outcome="A",
            context="profile_battle", selection=selection, decisive=False, hp=DEFAULTS,
        )
    await observations.record_duel(
        db, user_id=user, title_a=1, title_b=2, outcome="B",
        context="profile_battle", selection=observations.HELD_OUT, decisive=False, hp=DEFAULTS,
    )

    loaded = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert await db.fetchval("SELECT count(*) FROM duel WHERE user_id=$1", user) == 4
    assert loaded.n_duels == 3, "a held-out pair was trained on"
    assert loaded.n_held_out == 1
    assert set(loaded.obs.duel_outcome.tolist()) == {model.OUT_A}, (
        "the held-out row's outcome leaked into the fit"
    )


async def test_a_silent_re_ask_is_not_a_second_observation(db, world):
    """§13 stream (b): "a separate silent re-ask stream — ~10% of comparisons/verdicts re-asked
    after ≥3 days; ~200 re-asks measure the flip rate σ".

    A re-ask is the same judgement posed twice to measure judgement *noise*, not a second
    judgement about taste, so it is excluded from the fit — while still superseding, because it
    is the person's latest answer and the card shows it. The two facts are separate: the fit
    does not filter on `superseded_by` at all.

    The exclusion keys on `is_reask` (NOT NULL, DEFAULT false), never on `reask_of` (nullable,
    ON DELETE SET NULL). Asserted below by nulling `reask_of` — a rule keyed on it would start
    counting both rows at that moment, and would fail open rather than loud.
    """
    user = world["user"]
    original = await observations.record_verdict(db, user_id=user, title_id=1, value=2)
    reask = await observations.record_verdict(
        db, user_id=user, title_id=1, value=1, is_reask=True, reask_of=original.row_id
    )

    loaded = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert loaded.n_verdicts == 1 and loaded.n_reask == 1
    assert loaded.obs.ord_level.tolist() == [2], "the re-ask was counted as a second observation"

    rows = await live_verdicts(db, user, 1)
    assert rows[0]["superseded_by"] == reask.row_id, "the person's latest answer is not current"

    await db.execute("UPDATE verdict SET reask_of = NULL WHERE id = $1", reask.row_id)
    again = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert again.obs.ord_level.tolist() == [2], (
        "the exclusion depends on reask_of, so it fails open once a row is undone"
    )


async def test_the_fit_is_partitioned_by_kind_and_a_cross_kind_duel_is_refused(db, world):
    """§4.1 rule 5: "every ranking surface partitions by it (measured: the unpartitioned crowd
    top-10 is 8/10 TV series)."

    The refusal is at the write, not in the loader's filter: a row nothing ever reads is worse
    than an error, because the count the person sees and the count the fit uses then differ with
    no message anywhere.
    """
    user = world["user"]
    await observations.record_verdict(db, user_id=user, title_id=1, value=2)   # movie
    await observations.record_verdict(db, user_id=user, title_id=7, value=0)   # series

    movies = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    series = await observations.load_observations(
        db, user_id=user, kind="series", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert movies.obs.title_ids.tolist() == [1]
    assert series.obs.title_ids.tolist() == [7]

    with pytest.raises(ValueError, match="cross-kind"):
        await observations.record_duel(
            db, user_id=user, title_a=1, title_b=7, outcome="A", context="profile_battle"
        )
    assert await db.fetchval("SELECT count(*) FROM duel") == 0


async def test_a_margin_less_duel_carries_the_hesitant_weight_from_hyperparams(db, world):
    """§4.2: "margin optional: decisive vs hesitant". §6.1: "a persistent decisive toggle sets
    the margin weight (~1.6 vs 1.0)".

    §6.3's drag-drop neighbour duels are margin-*less*, which is not weightless: they are
    ordinary, non-decisive comparisons. The number comes from `hp`, so a corpus re-tune of
    `margin_hesitant` reaches them — asserted here by re-loading under a changed constant.
    """
    user = world["user"]
    await observations.record_duel(
        db, user_id=user, title_a=1, title_b=2, outcome="A", context="tier_insert"
    )
    await observations.record_duel(
        db, user_id=user, title_a=3, title_b=4, outcome="B", context="profile_battle",
        decisive=True, hp=DEFAULTS,
    )
    stored = await db.fetch("SELECT margin FROM duel WHERE user_id=$1 ORDER BY id", user)
    assert stored[0]["margin"] is None, "a neighbour duel invented a margin"
    assert stored[1]["margin"] == pytest.approx(DEFAULTS.margin_decisive)

    loaded = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    # `duel.margin` is `real`, so the decisive weight comes back as float4's nearest neighbour
    # to 1.6. §4.3 normalises margins by their mean before they weigh anything, so a seventh
    # decimal place cannot matter — but an exact comparison here would be asserting float4.
    assert loaded.obs.duel_margin.tolist() == pytest.approx(
        [DEFAULTS.margin_hesitant, DEFAULTS.margin_decisive], rel=1e-6
    )

    retuned = Hyperparams(margin_hesitant=0.5)
    loaded = await observations.load_observations(
        db, user_id=user, kind="movie", hp=retuned, embeddings=fixture_embeddings
    )
    assert loaded.obs.duel_margin[0] == 0.5, "the hesitant weight is hard-coded somewhere"


async def test_a_bundle_less_household_still_produces_an_observation_set(db, world):
    """§3.1 makes an empty artifact store a legal state, and M2's exit criterion is about two
    people rating. With no Backbone and no placement, e = 0 and s = μ + r: the fit still ranks
    what the person has rated."""
    user = world["user"]
    for title_id, value in ((1, 2), (2, 0), (3, 1)):
        await observations.record_verdict(db, user_id=user, title_id=title_id, value=value)

    loaded = await observations.load_observations(db, user_id=user, kind="movie", hp=DEFAULTS)
    assert not loaded.obs.embedded.any()
    assert loaded.obs.embeddings.shape == (3, 64)
    fit = model.fit(loaded.obs, DEFAULTS)
    assert np.all(np.isfinite(fit.s))
    assert fit.s[0] > fit.s[1], "with no bundle the fit stopped ranking what it was told"


# --- the nightly refit ------------------------------------------------------------------------


async def _rate(db, user, *, verdicts=(), duels=(), tier_edits=()):
    for title_id, value in verdicts:
        await observations.record_verdict(db, user_id=user, title_id=title_id, value=value)
    for a, b, outcome in duels:
        await observations.record_duel(
            db, user_id=user, title_a=a, title_b=b, outcome=outcome,
            context="profile_battle", decisive=False, hp=DEFAULTS,
        )
    for title_id, tier in tier_edits:
        await observations.record_tier_edit(db, user_id=user, title_id=title_id, tier=tier)


async def test_the_nightly_refit_writes_the_board_the_cutpoints_and_the_cache(db, world):
    """§5.3's "Ledger full MAP refit + cutpoints + σ", and §5.2's "the cutpoints ARE the
    displayed tier boundaries" — so `ledger_cutpoints.boundaries` is the tier arm's fitted
    vector and not a percentile of anything."""
    user = world["user"]
    await _rate(
        db, user,
        verdicts=[(1, 2), (2, 2), (3, 1), (4, 1), (5, 0), (6, 0)],
        duels=[(1, 2, "A"), (3, 4, "TIE"), (5, 6, "B"), (1, 5, "A")],
        tier_edits=[(1, 6), (5, 0)],
    )
    report = await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert report.fitted and report.converged, report.as_dict()
    assert (report.n_verdicts, report.n_duels, report.n_tier_edits) == (6, 4, 2)

    board = await refit.read_board(db, user_id=user, kind="movie")
    assert sorted(r.title_id for r in board) == [1, 2, 3, 4, 5, 6]
    assert {r.observed for r in board} == {True}
    assert all(np.isfinite(r.s) and r.sigma > 0 for r in board)
    ranked = [r.title_id for r in board]
    assert ranked.index(1) < ranked.index(3) < ranked.index(5), (
        "the board does not order the liked pile above the fine pile above the disliked one"
    )
    assert {r.tier for r in board} != {None} and all(0 <= r.tier <= 6 for r in board)
    assert await db.fetchval(
        "SELECT count(*) FROM ledger_state WHERE user_id=$1 AND kind='series'", user
    ) == 0, "the movie refit wrote into the series partition"

    cuts = await db.fetchrow(
        "SELECT boundaries, tier_set FROM ledger_cutpoints WHERE user_id=$1 AND kind='movie'", user
    )
    assert list(cuts["tier_set"]) == list(observations.DEFAULT_TIER_SET)
    assert len(cuts["boundaries"]) == len(observations.DEFAULT_TIER_SET) - 1
    assert list(cuts["boundaries"]) == sorted(cuts["boundaries"]), "§4.2: ordered ascending"
    assert list(cuts["boundaries"]) == pytest.approx(report.cutpoints)

    cache = await refit.load_cache(db, user_id=user, kind="movie", hp=DEFAULTS, lock=False)
    assert cache is not None and cache.n_observed == 6
    assert cache.title_ids.tolist() == sorted(cache.title_ids.tolist())
    # §4.3: "every constant comes from ledger_hyperparams.json". A cache built under other
    # constants is wrong, not stale, so the digest is a precondition and not a hint.
    assert await refit.load_cache(
        db, user_id=user, kind="movie", hp=Hyperparams(lambda_ridge=30.0), lock=False
    ) is None


async def test_every_owned_title_gets_a_coordinate_even_unrated(db, world):
    """§12's M2 exit criterion: "every owned title has a coordinate". §5.2 gives an unobserved
    title one at zero extra parameters — it has no r, so s = μ + ⟨v, e⟩ — and its σ is the σ it
    would have if it had never been rated, which is exactly what it has."""
    user = world["user"]
    await _rate(db, user, verdicts=[(1, 2), (2, 0), (3, 1)])
    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )

    rows = {r.title_id: r for r in await refit.read_board(db, user_id=user, kind="movie")}
    assert set(rows) == {1, 2, 3, 4, 5, 6}, "an owned film has no coordinate"
    assert [rows[t].observed for t in (1, 2, 3)] == [True, True, True]
    assert [rows[t].observed for t in (4, 5, 6)] == [False, False, False]
    assert all(np.isfinite(rows[t].s) for t in rows)
    unrated = await db.fetchrow(
        "SELECT sigma, sigma_prior FROM ledger_state WHERE user_id=$1 AND title_id=4", user
    )
    assert unrated["sigma"] == pytest.approx(unrated["sigma_prior"])
    assert rows[4].sigma > rows[1].sigma, "an unrated title is not less certain than a rated one"


async def test_the_displayed_weight_is_the_cdf_of_the_persons_own_s_per_kind(db, world):
    """§5.2: "the 0..1 weight is the empirical CDF of the user's own fitted `s` values, computed
    per kind (their best-ranked title → ~1.0, worst → ~0.0)".

    Per kind is the half that is easy to lose: a person whose films are all liked and whose two
    series are both disliked must still get a best-series near 1.0, because §4.1 rule 5 says the
    two surfaces are separate rankings and not one interleaved one.
    """
    user = world["user"]
    await _rate(db, user, verdicts=[(1, 2), (2, 2), (3, 2), (4, 1), (5, 0), (6, 0)])
    await _rate(db, user, verdicts=[(7, 0), (8, 0)], duels=[(7, 8, "A")])
    for kind in ("movie", "series"):
        await refit.refit_user(
            db, user_id=user, kind=kind, hp=DEFAULTS, embeddings=fixture_embeddings
        )

    films = [r for r in await refit.read_board(db, user_id=user, kind="movie") if r.observed]
    series = [r for r in await refit.read_board(db, user_id=user, kind="series") if r.observed]
    assert films[0].cdf == pytest.approx(1.0 - 1.0 / (2 * len(films))), "best film is not ~1.0"
    assert films[-1].cdf == pytest.approx(1.0 / (2 * len(films))), "worst film is not ~0.0"
    assert series[0].cdf == pytest.approx(0.75), (
        "the better of two disliked series was placed on the films' scale"
    )
    assert series[0].s < films[0].s, "the fixture is not actually testing a per-kind reference"


async def test_freshness_inflates_sigma_eff_and_never_the_fitted_sigma(db, world):
    """§5.2: "after 12 months untouched, a title's σ inflates Glicko-style at rate c per √month,
    capped at the prior σ" — "ambient recalibration rather than chores".

    Inflation is a display and queue quantity. Letting it into the likelihood would be
    re-weighting history by the calendar, which the spec's freshness sentence deliberately does
    not do, so `ledger_state.sigma` must be identical at both clock readings.
    """
    user = world["user"]
    await _rate(db, user, verdicts=[(1, 2), (2, 0), (3, 1), (4, 1)])
    now = datetime.now(UTC)

    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings, now=now
    )
    fresh = await db.fetchrow(
        "SELECT sigma, sigma_eff, sigma_prior FROM ledger_state WHERE user_id=$1 AND title_id=1",
        user,
    )
    assert fresh["sigma_eff"] == pytest.approx(fresh["sigma"]), "inflated inside the grace period"

    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings,
        now=now + timedelta(days=int(26 * refit.DAYS_PER_MONTH)),
    )
    stale = await db.fetchrow(
        "SELECT sigma, sigma_eff, sigma_prior FROM ledger_state WHERE user_id=$1 AND title_id=1",
        user,
    )
    assert stale["sigma"] == pytest.approx(fresh["sigma"]), "the calendar reached the likelihood"
    assert stale["sigma_eff"] > fresh["sigma_eff"]
    assert stale["sigma_eff"] <= stale["sigma_prior"] + 1e-12, "§5.2 caps inflation at the prior σ"


async def test_a_non_finite_fit_never_reaches_a_shelf(db, world, monkeypatch):
    """Postgres accepts NaN in a `double precision` column and sorts it ABOVE every real number,
    and `ledger_state_rank` is `(user_id, kind, s DESC)`.

    So one poisoned title is not a missing row: it is the top of every §6.0 shelf until somebody
    notices. Both halves are asserted — a single bad title is dropped and the rest of the board
    still lands, and a fit whose dense block has gone non-finite is refused outright so the
    previous board survives instead of being replaced by nothing.
    """
    user = world["user"]
    await _rate(db, user, verdicts=[(1, 2), (2, 0), (3, 1), (4, 1)])
    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    good = {r.title_id: r.s for r in await refit.read_board(db, user_id=user, kind="movie")}

    real_fit = model.fit

    def one_bad_title(obs, hp, **kwargs):
        fit = real_fit(obs, hp, **kwargs)
        s = fit.s.copy()
        s[0] = np.nan
        return type(fit)(**{**fit.__dict__, "s": s})

    monkeypatch.setattr(refit.model, "fit", one_bad_title)
    report = await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert report.rejected_nonfinite == 1
    board = await refit.read_board(db, user_id=user, kind="movie")
    assert all(np.isfinite(r.s) for r in board), "a NaN reached ledger_state"
    assert 1 not in {r.title_id for r in board if r.observed}

    def broken_block(obs, hp, **kwargs):
        fit = real_fit(obs, hp, **kwargs)
        return type(fit)(**{**fit.__dict__, "mu": float("nan")})

    monkeypatch.setattr(refit.model, "fit", broken_block)
    with pytest.raises(refit.RefitRefused):
        await refit.refit_user(
            db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
        )
    kept = {r.title_id: r.s for r in await refit.read_board(db, user_id=user, kind="movie")}
    assert kept[2] == pytest.approx(good[2]), "a refused fit still overwrote the board"


async def test_refit_all_covers_every_active_person_and_both_kinds(db, world):
    """§5.3's nightly row is over the household, and §4.1 rule 5 makes that two fits per person."""
    user = world["user"]
    other = await make_user(db, "jenny")
    await _rate(db, user, verdicts=[(1, 2), (2, 0), (3, 1)])
    await _rate(db, other, verdicts=[(7, 2), (8, 0)])

    reports = await refit.refit_all(db, DEFAULTS, embeddings=fixture_embeddings)
    by_key = {(r.user_id, r.kind): r for r in reports}
    assert set(by_key) == {(user, "movie"), (user, "series"), (other, "movie"), (other, "series")}
    assert by_key[(user, "movie")].fitted and by_key[(other, "series")].fitted
    assert not by_key[(user, "series")].fitted, "a fit was invented out of no observations"
    assert await db.fetchval(
        "SELECT count(*) FROM ledger_state WHERE user_id=$1 AND kind='series'", user
    ) == 0


# --- the <50 ms path ---------------------------------------------------------------------------


async def test_the_incremental_block_solve_is_a_stationary_point_of_the_same_objective(db, world):
    """The invariant the whole design rests on: incremental and nightly are one model at two
    resolutions, not two models.

    So after an incremental update the residual gradient of the FULL objective — assembled by
    `model` itself over every observation, at the cached (μ, v, γ, cuts, ψ) — must be zero at
    the titles that moved. A sign error in the local assembly, or a margin normalised over the
    local subset instead of the fit set, breaks this and nothing else in the suite would.
    """
    user = world["user"]
    await _rate(
        db, user,
        verdicts=[(1, 2), (2, 1), (3, 0), (4, 1), (5, 2)],
        duels=[(1, 2, "A"), (2, 3, "A"), (4, 5, "B"), (1, 4, "TIE")],
    )
    await db.execute("UPDATE duel SET margin = 1.6 WHERE title_a = 1 AND title_b = 2")
    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )

    # A duel is the hard case: two coupled coordinates, and a margin whose weight depends on the
    # mean over the whole fit set rather than over the two rows the block sees.
    await observations.record_duel(
        db, user_id=user, title_a=2, title_b=5, outcome="B",
        context="tier_queue", selection="boundary", decisive=True, hp=DEFAULTS,
    )
    delta = await refit.update_incrementally(
        db, user_id=user, kind="movie", title_ids=[2, 5], hp=DEFAULTS,
        embeddings=fixture_embeddings,
    )
    assert delta.fit_source == "incremental" and not delta.refit
    assert [r.title_id for r in delta.rows] == [2, 5]

    cache = await refit.load_cache(db, user_id=user, kind="movie", hp=DEFAULTS, lock=False)
    loaded = await observations.load_observations(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    residuals = np.array(
        [cache.r[int(np.flatnonzero(cache.title_ids == t)[0])] for t in loaded.obs.title_ids]
    )
    _gz, g_r, *_ = model._grad_hess(
        loaded.obs, DEFAULTS, cache.mu, cache.v, cache.gamma, cache.cuts, cache.log_nu,
        residuals, with_duels=True,
    )
    moved = [int(np.flatnonzero(loaded.obs.title_ids == t)[0]) for t in (2, 5)]
    assert np.max(np.abs(g_r[moved])) < 1e-7, (
        f"the block solve is not a stationary point of F: |g| = {np.abs(g_r[moved])}"
    )
    # And the answer it wrote is the answer it solved for.
    for row, i in zip(delta.rows, moved, strict=True):
        assert row.s == pytest.approx(cache.mu + loaded.obs.embeddings[i] @ cache.v + residuals[i])


async def test_the_incremental_path_serves_an_undo_with_the_same_call(db, world):
    """Decision 35's compensating write needs the Ledger put back too. `update_incrementally`
    re-reads the observations rather than being told what changed, so retracting a row and
    adding one are the same call — which is why there is no second, differently-wrong
    `revert_observation`."""
    user = world["user"]
    await _rate(db, user, verdicts=[(1, 1), (2, 1), (3, 1), (4, 1)], duels=[(1, 2, "A")])
    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    before = (await refit._read_rows(db, user_id=user, title_ids=[1]))[0]

    write = await observations.record_verdict(db, user_id=user, title_id=1, value=2)
    after = (await refit.update_incrementally(
        db, user_id=user, kind="movie", title_ids=[1], hp=DEFAULTS, embeddings=fixture_embeddings
    )).rows[0]
    assert after.s > before.s, "a 'liked' did not raise the title's score"
    assert after.sigma <= before.sigma + 1e-12, "an observation made the model less certain"

    await observations.undo(db, user_id=user, write=write)
    restored = (await refit.update_incrementally(
        db, user_id=user, kind="movie", title_ids=[1], hp=DEFAULTS, embeddings=fixture_embeddings
    )).rows[0]
    assert restored.s == pytest.approx(before.s, abs=1e-6), "undo did not put the score back"
    assert restored.sigma == pytest.approx(before.sigma, rel=1e-6)


async def test_a_cache_from_other_hyperparameters_is_refitted_rather_than_trusted(db, world):
    """§4.3: "every constant comes from `ledger_hyperparams.json`". A cache built under other
    constants does not produce a stale `s`, it produces a wrong one — so the digest mismatch
    triggers a full refit and the delta says plainly that it was not the <50 ms path."""
    user = world["user"]
    await _rate(db, user, verdicts=[(1, 2), (2, 0), (3, 1)])
    retuned = Hyperparams(lambda_ridge=30.0)

    delta = await refit.update_incrementally(
        db, user_id=user, kind="movie", title_ids=[1], hp=retuned, embeddings=fixture_embeddings
    )
    assert delta.refit is True and delta.fit_source == "nightly"
    assert delta.rows and np.isfinite(delta.rows[0].s)
    assert await db.fetchval(
        "SELECT hp_digest FROM ledger_fit WHERE user_id=$1 AND kind='movie'", user
    ) == retuned.digest()


# --- §5.3's budgets, measured --------------------------------------------------------------------

BUDGET_TITLES = 900          # §1: "Ledger refit for 2 users over 839+ titles — seconds"
BUDGET_VERDICTS = 100        # §5.2: "Aim for 50-100 in the first sitting or two"
BUDGET_DUELS = 300
BUDGET_TIER_EDITS = 20


async def _rate_at_scale(db, user, pool, rng, taste, *, n_verdicts, n_duels, n_edits):
    """One person's sitting over one kind, straight through SQL — the write path has its own
    tests, and a per-row round trip would put the fixture's cost inside the measurement."""
    truth = {int(t): float(_embedding(int(t)) @ taste) for t in pool}
    rated = rng.choice(pool, size=min(n_verdicts, len(pool)), replace=False)
    await db.execute(
        """
        INSERT INTO verdict (user_id, title_id, value)
        SELECT $1, x.title_id, x.value FROM unnest($2::int[], $3::smallint[]) AS x(title_id, value)
        """,
        user,
        [int(t) for t in rated],
        # §5.2's measured 5x lever: "spreading verdicts across all three classes matters ~5x
        # more than anything the corpus side can tune". A fixture that is 60% "liked" would be
        # measuring a labeller the spec tells the UI to warn about.
        [int(np.searchsorted([-0.002, 0.002], truth[int(t)])) for t in rated],
    )
    pairs = rng.choice(rated, size=(n_duels, 2))
    pairs = pairs[pairs[:, 0] != pairs[:, 1]]
    await db.execute(
        """
        INSERT INTO duel (user_id, title_a, title_b, outcome, margin, context, selection)
        SELECT $1, x.a, x.b, x.outcome, x.margin, 'profile_battle', 'random'
        FROM unnest($2::int[], $3::int[], $4::text[], $5::real[]) AS x(a, b, outcome, margin)
        """,
        user,
        [int(a) for a, _ in pairs],
        [int(b) for _, b in pairs],
        [
            "A" if truth[int(a)] > truth[int(b)] + 1e-3
            else "B" if truth[int(b)] > truth[int(a)] + 1e-3 else "TIE"
            for a, b in pairs
        ],
        [1.6 if i % 3 == 0 else 1.0 for i in range(len(pairs))],
    )
    edited = rng.choice(rated, size=min(n_edits, len(rated)), replace=False)
    await db.execute(
        """
        INSERT INTO tier_edit (user_id, title_id, tier, via)
        SELECT $1, x.title_id, x.tier, 'drag_drop'
        FROM unnest($2::int[], $3::smallint[]) AS x(title_id, tier)
        """,
        user,
        [int(t) for t in edited],
        [int(np.clip(3 + round(truth[int(t)] * 400), 0, 6)) for t in edited],
    )


async def _big_world(
    db,
    *,
    n_titles=BUDGET_TITLES,
    users=("patrick", "jenny"),
    n_verdicts=BUDGET_VERDICTS,
    n_duels=BUDGET_DUELS,
    n_edits=BUDGET_TIER_EDITS,
):
    """A library at the spec's scale, rated by two people across both kinds.

    Deterministic — a budget that depends on an RNG seed is a budget that is met on some runs —
    and both kinds are rated, because §4.1 rule 5 makes the nightly job two fits per person and
    a household where only the films are rated would measure half of it.
    """
    specs = [(i, "movie" if i % 3 else "series", f"Title {i}") for i in range(1, n_titles + 1)]
    await make_titles(db, specs)
    by_kind = {
        kind: [i for i, k, _ in specs if k == kind] for kind in ("movie", "series")
    }
    ids = []
    for index, name in enumerate(users):
        user = await make_user(db, name, "admin" if index == 0 else "member")
        ids.append(user)
        rng = np.random.default_rng(20 + index)
        taste = _embedding(7 + index)
        for kind, share in (("movie", 1.0), ("series", 0.4)):
            await _rate_at_scale(
                db, user, by_kind[kind], rng, taste,
                n_verdicts=max(4, int(n_verdicts * share)),
                n_duels=max(4, int(n_duels * share)),
                n_edits=max(2, int(n_edits * share)),
            )
    return ids


async def test_a_full_map_refit_of_both_users_over_the_owned_library_lands_inside_the_budget(db):
    """§5.3: "Ledger full MAP refit + cutpoints + σ — nightly — seconds", and §1's measured
    expectation: "Ledger refit for 2 users over 839+ titles — seconds (LBFGS, 64-d)".

    Two people × two kinds × 900 owned titles, each person with 100 verdicts, ~300 duels and 20
    tier edits — the shape §5.2 asks a first sitting to produce. The budget asserted is 60 s for
    the whole household, which is a generous reading of "seconds"; the measured number is
    printed so a regression shows up as a number moving rather than as a test going red one day.
    """
    users = await _big_world(db)
    started = time.perf_counter()
    reports = await refit.refit_all(db, DEFAULTS, embeddings=fixture_embeddings)
    elapsed = time.perf_counter() - started
    print(f"\nfull refit, {len(users)} users x 2 kinds x {BUDGET_TITLES} titles: {elapsed:.2f} s")
    for report in reports:
        print(f"  {report.as_dict()}")

    fitted = [r for r in reports if r.fitted]
    assert len(fitted) == 4, [r.as_dict() for r in reports]
    assert all(r.converged for r in fitted), "a nightly fit did not converge"
    assert all(len(r.cutpoints) == 6 and r.cutpoints == sorted(r.cutpoints) for r in fitted)
    assert elapsed < 60.0, f"§5.3's nightly refit took {elapsed:.1f} s"

    for user in users:
        for kind in ("movie", "series"):
            rows = await refit.read_board(db, user_id=user, kind=kind, limit=BUDGET_TITLES)
            owned = await db.fetchval(
                "SELECT count(*) FROM title WHERE is_owned AND kind = $1", kind
            )
            assert len(rows) == owned, "§12: every owned title must have a coordinate"
            assert all(np.isfinite(r.s) and r.sigma > 0 for r in rows)


async def test_an_incremental_update_lands_inside_the_fifty_millisecond_budget(db):
    """§5.3: "Ledger incremental update — every observation — <50 ms".

    Measured end to end against a real Postgres — the cache read, the block solve, and both
    writes — because that is what happens on a tap, and a budget measured on the numpy alone
    would be measuring the half that was never in doubt. Fifty observations, median reported,
    on the same 900-title library the nightly test uses.

    The paired assertion is the property the budget is a proxy for: an incremental update
    touches one or two titles. A path that re-solved the library could still come in under
    50 ms on a fast machine at this scale and would fail here on any machine.
    """
    users = await _big_world(db, users=("patrick",))
    user = users[0]
    await refit.refit_user(
        db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    unrated = [
        int(r["id"])
        for r in await db.fetch(
            """
            SELECT t.id FROM title t
            WHERE t.kind = 'movie' AND NOT EXISTS (
                SELECT 1 FROM verdict v WHERE v.user_id = $1 AND v.title_id = t.id)
            ORDER BY t.id LIMIT 60
            """,
            user,
        )
    ]

    timings: list[float] = []
    for index, title_id in enumerate(unrated):
        await observations.record_verdict(db, user_id=user, title_id=title_id, value=index % 3)
        started = time.perf_counter()
        delta = await refit.update_incrementally(
            db, user_id=user, kind="movie", title_ids=[title_id], hp=DEFAULTS,
            embeddings=fixture_embeddings,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        assert not delta.refit, "the cache went cold mid-run; this is not the incremental path"
        assert len(delta.rows) == 1, "an incremental update touched more than the title it was given"
        if index >= 10:                       # the first ten warm the connection and the caches
            timings.append(elapsed_ms)

    median = statistics.median(timings)
    print(
        f"\nincremental update over {BUDGET_TITLES} titles / "
        f"{BUDGET_VERDICTS} verdicts: median {median:.1f} ms, "
        f"p95 {sorted(timings)[int(0.95 * len(timings))]:.1f} ms, max {max(timings):.1f} ms"
    )
    assert median < 50.0, f"§5.3's <50 ms budget: median was {median:.1f} ms"


async def test_the_incremental_cost_does_not_grow_with_the_library(db):
    """The half of §5.3's budget that survives being run on a slow machine.

    A path that re-solved everything would still pass a wall-clock threshold on fast hardware at
    household scale. This times the same call against a small library and a large one and
    asserts the ratio is small — a statement about the algorithm rather than about the CPU.

    The *observation* count is held fixed and only the library grows, so the two runs differ in
    exactly the quantity the incremental path is supposed to be independent of. A version that
    re-fitted, or that rewrote every `ledger_state` row, would show the 15x here.
    """
    ratios = []
    for n_titles in (60, BUDGET_TITLES):
        users = await _big_world(
            db, n_titles=n_titles, users=(f"p{n_titles}",),
            n_verdicts=35, n_duels=100, n_edits=6,
        )
        user = users[0]
        await refit.refit_user(
            db, user_id=user, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
        )
        rated = [
            int(r["title_id"])
            for r in await db.fetch(
                "SELECT DISTINCT title_id FROM verdict WHERE user_id=$1 ORDER BY title_id LIMIT 25",
                user,
            )
        ]
        samples = []
        for index, title_id in enumerate(rated):
            await observations.record_verdict(
                db, user_id=user, title_id=title_id, value=(index + 1) % 3
            )
            started = time.perf_counter()
            await refit.update_incrementally(
                db, user_id=user, kind="movie", title_ids=[title_id], hp=DEFAULTS,
                embeddings=fixture_embeddings,
            )
            if index >= 5:
                samples.append((time.perf_counter() - started) * 1000.0)
        ratios.append(statistics.median(samples))
        await db.execute("DELETE FROM app_user WHERE id = $1", user)
        await db.execute("DELETE FROM title")

    small, large = ratios
    print(f"\nincremental median: {small:.1f} ms at 60 titles, {large:.1f} ms at {BUDGET_TITLES}")
    # Measured on this machine at 1.2x for a 15x library. The threshold leaves room for a loaded
    # CI box without leaving room for a re-solve, which would track the 15x.
    assert large / small < 2.5, (
        f"the incremental cost grew {large / small:.1f}x for a {BUDGET_TITLES / 60:.0f}x "
        "library — something is re-solving the whole fit"
    )
