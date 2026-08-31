"""§6.3's database side. Spec v2.1 §6.3, §4.1 rules 1/2/5, §4.2, §5.2, §5.3, §6.7, §13.

Four questions, and the tests are grouped by which one they answer.

**What does a drop write?** §6.3 gives two shapes — a tier edit, and a tier edit plus two
margin-less duels — and the difference between them is a fact about the board rather than about
the arm. The duels are the interesting half: they carry the placement, so they have outcomes,
and writing them outcome-less would store the geometry and throw the judgement away.

**What does changing the tier set cost?** Decision 11 is four sentences and each one is a
separate way to get it wrong: cut the wrong distribution, run a refit inside an HTTP request,
delete observations along with the boundaries, or touch the other person's row.

**Is §13's guard actually enforced?** The uniform-random 10% must reach neither the fit nor the
selector nor any quality figure, and the evaluation must read nothing else. Three separate read
paths, three separate ways to leak.

**Do §6.3's filters obey §4.1?** A DNA predicate is where rules 1 and 2 both get broken by the
obvious implementation — one union that loses the tier, one confidence cut that deletes 44% of
the extracted tier.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np
import pytest

from spielplan.db import library
from spielplan.home import rail
from spielplan.ledger import observations, refit
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.rank import drop, evaluation, queue, read, tiers

PACKAGE = Path(__file__).resolve().parents[1] / "spielplan"


def _embedding(title_id: int) -> np.ndarray:
    rng = np.random.default_rng(1000 + title_id)
    vector = rng.normal(size=64)
    return vector / (np.linalg.norm(vector) * 8.0)


def fixture_embeddings(title_ids):
    ids = list(title_ids)
    if not ids:
        return np.zeros((0, 64)), np.zeros(0, dtype=bool)
    return np.stack([_embedding(t) for t in ids]), np.ones(len(ids), dtype=bool)


async def make_user(db, name, role="member"):
    return await db.fetchval(
        "INSERT INTO app_user (name, role) VALUES ($1, $2) RETURNING id", name, role
    )


@pytest.fixture
async def world(db):
    """Two people and twelve owned titles — ten films, two series. Two people because half of
    decision 11 is "one user changing their tier set never touches another's", which a
    single-user fixture cannot fail."""
    await db.execute(
        """
        INSERT INTO title (id, kind, name, year, runtime_min, is_owned)
        SELECT x.id, x.kind, x.name, x.year, x.runtime, true
        FROM unnest($1::int[], $2::text[], $3::text[], $4::int[], $5::int[])
             AS x(id, kind, name, year, runtime)
        """,
        list(range(1, 13)),
        ["movie"] * 10 + ["series"] * 2,
        [f"Title {i}" for i in range(1, 13)],
        [1995 + (i % 3) * 10 for i in range(1, 13)],
        [90 + (i * 13) % 80 for i in range(1, 13)],
    )
    return {
        "patrick": await make_user(db, "patrick", "admin"),
        "jenny": await make_user(db, "jenny"),
    }


async def rate(db, user, *, verdicts=(), duels=(), tier_edits=()):
    for title_id, value in verdicts:
        await observations.record_verdict(db, user_id=user, title_id=title_id, value=value)
    for a, b, outcome in duels:
        await observations.record_duel(
            db, user_id=user, title_a=a, title_b=b, outcome=outcome,
            context="profile_battle", decisive=False, hp=DEFAULTS,
        )
    for title_id, tier in tier_edits:
        await observations.record_tier_edit(db, user_id=user, title_id=title_id, tier=tier)


async def fitted(db, user, kind="movie"):
    return await refit.refit_user(
        db, user_id=user, kind=kind, hp=DEFAULTS, embeddings=fixture_embeddings
    )


@pytest.fixture
async def board_of(db, world):
    """A fitted movie board for Patrick: six verdicts across all three classes, four duels."""
    user = world["patrick"]
    await rate(
        db, user,
        verdicts=[(1, 2), (2, 2), (3, 1), (4, 1), (5, 0), (6, 0)],
        duels=[(1, 2, "A"), (3, 4, "TIE"), (5, 6, "B"), (1, 5, "A")],
    )
    report = await fitted(db, user)
    assert report.fitted, report.as_dict()
    return user


# --- §6.3: drag-and-drop writes observations ---------------------------------------------


async def test_dropping_into_a_tier_writes_one_tier_edit_and_no_duel(db, board_of):
    """§6.3: "dropping a title into a tier emits a `tier_edit`". A drop into an empty tier has
    no neighbours, so it emits that and nothing else."""
    result = await drop.drop(db, user_id=board_of, title_id=3, tier=5)

    edits = await db.fetch("SELECT title_id, tier, via FROM tier_edit WHERE user_id = $1", board_of)
    assert [(r["title_id"], r["tier"], r["via"]) for r in edits] == [(3, 5, "drag_drop")]
    assert await db.fetchval(
        "SELECT count(*) FROM duel WHERE user_id = $1 AND context = 'tier_insert'", board_of
    ) == 0
    assert result.neighbour_duels == 0


async def test_dropping_between_two_titles_writes_the_edit_and_two_margin_less_duels(db, board_of):
    """§6.3: "dropping it *between* two titles emits that edit **plus two margin-less duels**
    against its new neighbours"."""
    result = await drop.drop(db, user_id=board_of, title_id=4, tier=5, above=1, below=2)

    assert await db.fetchval(
        "SELECT count(*) FROM tier_edit WHERE user_id=$1 AND title_id=4 AND via='drag_drop'",
        board_of,
    ) == 1
    rows = await db.fetch(
        "SELECT title_a, title_b, outcome, margin, context, selection FROM duel "
        "WHERE user_id = $1 AND context = 'tier_insert' ORDER BY id",
        board_of,
    )
    assert len(rows) == 2 == result.neighbour_duels
    for row in rows:
        assert row["margin"] is None, "§6.3 says margin-less, and NULL is what that means"
        assert row["context"] == "tier_insert"
        # `selection` describes ADAPTIVE selection (0005). A drop is not adaptively selected by
        # anything, so it stays at the column's default and never claims a queue arm.
        assert row["selection"] == "random"


async def test_the_neighbour_duels_carry_the_placement_and_not_just_the_geometry(db, board_of):
    """The two duels are the point of the rule: the person said this title is under one and
    over another, which is an ordinal claim the tier arm cannot express. Written without
    outcomes they would record that a comparison happened and not what it said."""
    await drop.drop(db, user_id=board_of, title_id=4, tier=5, above=1, below=2)
    rows = await db.fetch(
        "SELECT title_a, title_b, outcome FROM duel WHERE user_id=$1 AND context='tier_insert' "
        "ORDER BY id",
        board_of,
    )
    assert (rows[0]["title_a"], rows[0]["title_b"], rows[0]["outcome"]) == (1, 4, "A")
    assert (rows[1]["title_a"], rows[1]["title_b"], rows[1]["outcome"]) == (4, 2, "A")


async def test_a_drop_at_the_end_of_a_tier_writes_the_one_duel_that_exists(db, board_of):
    """§6.3 says "between two titles" and is silent about the first and last slot of a tier.
    One neighbour is one duel: refusing the drop would make those slots unreachable, and
    inventing a second duel would put a comparison in the Ledger nobody made."""
    await drop.drop(db, user_id=board_of, title_id=4, tier=5, below=2)
    rows = await db.fetch(
        "SELECT title_a, title_b FROM duel WHERE user_id=$1 AND context='tier_insert'", board_of
    )
    assert [(r["title_a"], r["title_b"]) for r in rows] == [(4, 2)]


async def test_a_drop_is_one_transaction(db, board_of):
    """The edit and its neighbour duels are one gesture. A refused neighbour must leave no
    tier_edit behind, or the board would show a placement the person never completed."""
    before = await db.fetchval("SELECT count(*) FROM tier_edit WHERE user_id=$1", board_of)
    with pytest.raises(drop.DropRefused):
        await drop.drop(db, user_id=board_of, title_id=4, tier=99, above=1, below=2)
    with pytest.raises(drop.DropRefused):
        await drop.drop(db, user_id=board_of, title_id=4, tier=5, above=4)
    assert await db.fetchval("SELECT count(*) FROM tier_edit WHERE user_id=$1", board_of) == before


async def test_the_board_renders_the_drop_and_does_not_snap_it_back(db, board_of):
    """§6.3 end to end: the title the person dropped into S is in S on the board they get
    back, whatever the refit then does to `s`."""
    await drop.drop(db, user_id=board_of, title_id=5, tier=6)
    await fitted(db, board_of)

    tiers, cuts, _rows = await read.load(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    placed = {e.title_id: e for t in tiers for e in t.entries}
    assert placed[5].tier == 6 == placed[5].assigned_tier
    assert cuts.tier_set[6] == "S"


async def test_a_drop_narrates_itself_with_the_number_of_duels_it_wrote(db, board_of):
    """§6.7's own example line, and proposal 120's rule that a line is identical for pointer
    and touch when the semantics are."""
    both = await drop.drop(db, user_id=board_of, title_id=4, tier=5, above=1, below=2,
                           title_name="Drive")
    assert both.log == (
        "tier_edit(Drive → A+, via=drag_drop) + 2 margin-less duels vs new neighbours"
    )
    alone = await drop.drop(db, user_id=board_of, title_id=3, tier=0, title_name="Heat")
    assert alone.log == "tier_edit(Heat → F, via=drag_drop)"


async def test_tap_to_tier_writes_exactly_what_the_pointer_path_writes(db, board_of):
    """§6.3: "**On phones:** tap a title (it lifts), tap a tier (it drops) — the same
    `tier_edit` semantics". Same function, same `via`, so there is no second write path to
    drift."""
    await drop.drop(db, user_id=board_of, title_id=3, tier=4)
    await drop.drop(db, user_id=board_of, title_id=4, tier=4)
    rows = await db.fetch(
        "SELECT title_id, tier, via FROM tier_edit WHERE user_id=$1 ORDER BY id", board_of
    )
    assert [r["via"] for r in rows] == ["drag_drop", "drag_drop"]


# --- decision 11: the tier set is a per-user preference -----------------------------------


async def test_ledger_cutpoints_is_keyed_by_user_and_kind(db, world):
    """§4.2's block, and the reason decision 11 needs no new table."""
    columns = {
        r["column_name"]
        for r in await db.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'ledger_cutpoints'"
        )
    }
    assert {"user_id", "kind", "boundaries", "tier_set", "refit_requested_at"} <= columns
    key = await db.fetch(
        """
        SELECT a.attname FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'ledger_cutpoints'::regclass AND i.indisprimary
        """
    )
    assert {r["attname"] for r in key} == {"user_id", "kind"}


async def test_a_boundary_list_that_does_not_match_the_tier_set_is_refused(db, world):
    """§4.2: "length = |tier set| − 1". Enforced by the database, so no write path can produce
    a board with a boundary that indexes past its own labels."""
    import asyncpg

    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set) "
            "VALUES ($1, 'movie', ARRAY[0.0, 1.0], ARRAY['F','D','C','B'])",
            world["patrick"],
        )


async def test_saving_a_new_tier_set_reinitialises_to_equal_mass_quantiles(db, board_of):
    """Decision 11: "re-initialised to the **equal-mass quantiles** of that user's fitted `s`
    distribution for the new K". Their own distribution — the measured F3/D7/C15/… shape is
    authored for K = 7 and is not defined for any other K."""
    report = await tiers.save_tier_set(db, user_id=board_of, tier_set=["bad", "ok", "good"])
    assert report.k_changed and report.initialised["movie"] == "quantile"

    row = await db.fetchrow(
        "SELECT boundaries, tier_set FROM ledger_cutpoints WHERE user_id=$1 AND kind='movie'",
        board_of,
    )
    assert list(row["tier_set"]) == ["bad", "ok", "good"]
    assert len(row["boundaries"]) == 2, "§4.2: length = |tier set| - 1"

    s = np.asarray(
        [
            float(r["s"])
            for r in await db.fetch(
                "SELECT s FROM ledger_state WHERE user_id=$1 AND kind='movie' AND observed",
                board_of,
            )
        ]
    )
    assert np.allclose(list(row["boundaries"]), np.quantile(s, [1 / 3, 2 / 3]))


async def test_saving_a_new_tier_set_queues_a_refit_for_that_user_alone(db, board_of, world):
    """Decision 11: "a Ledger refit is queued for that user alone". Queued, not run: §5.3
    budgets a full MAP refit at "seconds", which does not belong inside a settings save."""
    await rate(db, world["jenny"], verdicts=[(1, 2), (2, 0)])
    await fitted(db, world["jenny"])
    await tiers.save_tier_set(db, user_id=world["jenny"], tier_set=["F", "D", "C", "B", "A", "A+", "S"])

    await tiers.save_tier_set(db, user_id=board_of, tier_set=["bad", "ok", "good"])

    owed = await tiers.refits_owed(db)
    assert {user for user, _kind in owed} == {board_of}
    assert {kind for _user, kind in owed} == {"movie", "series"}

    await tiers.clear_refit_request(db, user_id=board_of, kind="movie")
    assert [k for u, k in await tiers.refits_owed(db) if u == board_of] == ["series"]


async def test_a_relabel_at_the_same_size_keeps_the_learned_boundaries(db, board_of):
    """Decision 11's reason for the re-init is that "changing K invalidates that user's
    boundaries". Renaming F to E does not change K, so throwing away a fitted board would be
    the rule doing more than it says."""
    before = list(
        await db.fetchval(
            "SELECT boundaries FROM ledger_cutpoints WHERE user_id=$1 AND kind='movie'", board_of
        )
    )
    report = await tiers.save_tier_set(
        db, user_id=board_of, tier_set=["E", "D", "C", "B", "A", "A+", "S"]
    )
    after = list(
        await db.fetchval(
            "SELECT boundaries FROM ledger_cutpoints WHERE user_id=$1 AND kind='movie'", board_of
        )
    )
    assert not report.k_changed and not report.refit_queued
    assert report.initialised["movie"] == "kept"
    assert after == before
    assert await tiers.refits_owed(db) == []


async def test_saving_a_new_tier_set_leaves_the_tier_edit_rows_intact(db, board_of):
    """Decision 11: "tier *edits* are observations and survive the change, tier *boundaries*
    do not"."""
    await drop.drop(db, user_id=board_of, title_id=1, tier=6)
    await drop.drop(db, user_id=board_of, title_id=5, tier=0)
    before = await db.fetch(
        "SELECT id, title_id, tier, via FROM tier_edit WHERE user_id=$1 ORDER BY id", board_of
    )

    report = await tiers.save_tier_set(db, user_id=board_of, tier_set=["bad", "ok", "good"])
    after = await db.fetch(
        "SELECT id, title_id, tier, via FROM tier_edit WHERE user_id=$1 ORDER BY id", board_of
    )
    assert [dict(r) for r in after] == [dict(r) for r in before]
    assert report.tier_edits_kept == len(before)


async def test_a_shrunk_tier_set_still_fits_and_the_old_edits_still_count(db, board_of):
    """The consequence of keeping the rows: an edit written under K = 7 names a level that no
    longer exists under K = 3. `load_observations` clamps rather than crashing, so the next
    refit is a fit and not an IndexError, and the person's "top tier" stays top."""
    await drop.drop(db, user_id=board_of, title_id=1, tier=6)
    await tiers.save_tier_set(db, user_id=board_of, tier_set=["bad", "ok", "good"])

    report = await fitted(db, board_of)
    assert report.fitted and report.n_tier_edits == 1
    assert len(report.cutpoints) == 2
    top = await db.fetchval(
        "SELECT tier FROM ledger_state WHERE user_id=$1 AND title_id=1", board_of
    )
    assert 0 <= top <= 2

    # ...and the BOARD survives it too. This test used to stop one call short of the surface,
    # which is exactly where the M3 review found a 500: the fit clamped the stale level and
    # said so in a log line, while `board._band` indexed straight into a cutpoint array that
    # had shrunk underneath it. A person who used decision 11's own control lost `/rank`
    # permanently. Asserting the fit without asserting the read was the gap.
    rendered, cuts, _rows = await read.load(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    assert [t.label for t in rendered] == ["good", "ok", "bad"]
    placed = {e.title_id: e for t in rendered for e in t.entries}
    assert placed[1].tier == len(cuts.tier_set) - 1, "the top tier they had is the top they have"
    assert placed[1].assigned_tier == placed[1].tier


async def test_one_persons_tier_set_never_touches_anothers(db, board_of, world):
    """Decision 11's last sentence, and the half a single-user fixture cannot fail."""
    jenny = world["jenny"]
    await rate(db, jenny, verdicts=[(1, 2), (2, 1), (3, 0)])
    await fitted(db, jenny)
    before = await db.fetch(
        "SELECT kind, boundaries, tier_set FROM ledger_cutpoints WHERE user_id=$1 ORDER BY kind",
        jenny,
    )

    await tiers.save_tier_set(db, user_id=board_of, tier_set=["bad", "ok", "good"])

    after = await db.fetch(
        "SELECT kind, boundaries, tier_set FROM ledger_cutpoints WHERE user_id=$1 ORDER BY kind",
        jenny,
    )
    assert [dict(r) for r in after] == [dict(r) for r in before]
    assert [r for r in await tiers.refits_owed(db) if r[0] == jenny] == []


async def test_a_tier_set_the_board_could_not_render_is_refused(db, board_of):
    for bad in (
        [],
        ["only"],
        ["A", "A"],
        ["A", ""],
        [f"T{i}" for i in range(20)],
        # Long enough to push §6.7's rail line past its 400-character limit, which `rail.record`
        # enforces by raising — after the drop's transaction has committed.
        ["A" * 400, "B", "C"],
    ):
        with pytest.raises(tiers.TierSetRefused):
            await tiers.save_tier_set(db, user_id=board_of, tier_set=bad)


# --- §13 stream (a): the held-out 10% ------------------------------------------------------


async def test_a_held_out_pair_is_stored_with_its_own_discriminator(db, board_of):
    """§13 / proposal 146: "stored on the `duel` row's context, named in the §6.7 log line".
    The row has to be findable, which is what the partial index in 0005 is for."""
    await observations.record_duel(
        db, user_id=board_of, title_a=1, title_b=2, outcome="A",
        context="tier_queue", selection=queue.ARM_HOLDOUT,
    )
    rows = await db.fetch(
        "SELECT context, selection FROM duel WHERE user_id=$1 AND selection='uniform_holdout'",
        board_of,
    )
    assert [(r["context"], r["selection"]) for r in rows] == [("tier_queue", "uniform_holdout")]


async def test_the_selector_never_counts_a_held_out_comparison(db, board_of):
    """§13: the held-out stream feeds neither the selection rule nor any quality figure.

    `queue._exploration` picks the least-compared title, so this count *is* a selector input —
    counting held-out rows here would make the selector a reader of the evaluation stream in a
    way no test of `queue` alone could see."""
    for _ in range(5):
        await observations.record_duel(
            db, user_id=board_of, title_a=1, title_b=2, outcome="A",
            context="tier_queue", selection=queue.ARM_HOLDOUT,
        )
    await observations.record_duel(
        db, user_id=board_of, title_a=1, title_b=3, outcome="A",
        context="tier_queue", selection=queue.ARM_BOUNDARY,
    )

    counts = await read.comparison_counts(db, user_id=board_of, kind="movie")
    # Four profile battles from the fixture (1v2, 3v4, 5v6, 1v5) plus the one boundary pair.
    assert counts[1] == 3, "the five held-out pairs on title 1 must be invisible here"
    assert counts[2] == 1
    assert counts[3] == 2


async def test_the_held_out_stream_never_reaches_the_fit(db, board_of):
    """The exclusion half of the guard, at the seam the Rank surface writes through."""
    before = await fitted(db, board_of)
    for a, b in ((1, 2), (2, 3), (3, 4)):
        await observations.record_duel(
            db, user_id=board_of, title_a=a, title_b=b, outcome="A",
            context="tier_queue", selection=queue.ARM_HOLDOUT,
        )
    after = await fitted(db, board_of)
    assert after.n_duels == before.n_duels
    assert after.n_held_out == 3
    assert np.isclose(after.objective, before.objective)


async def test_the_evaluation_read_path_admits_only_held_out_rows(db, board_of):
    """§13: the uniform-random stream "is the *only* data used to evaluate the tier model".

    Adaptive pairs are written that would *agree* with the model, so a leak would show up as a
    higher rate rather than as an error — which is exactly the inflation §13 measured."""
    ranked = await refit.read_board(db, user_id=board_of, kind="movie")
    order = [r.title_id for r in ranked]
    best, worst = order[0], order[-1]

    # Ten adaptive pairs the model gets right, and two held-out pairs it gets wrong.
    for _ in range(10):
        await observations.record_duel(
            db, user_id=board_of, title_a=best, title_b=worst, outcome="A",
            context="tier_queue", selection=queue.ARM_BOUNDARY,
        )
    for _ in range(2):
        await observations.record_duel(
            db, user_id=board_of, title_a=best, title_b=worst, outcome="B",
            context="tier_queue", selection=queue.ARM_HOLDOUT,
        )

    agreement = await evaluation.held_out_agreement(db, user_id=board_of, kind="movie")
    assert agreement.pairs == 2, "the ten adaptive pairs are not evaluation data"
    assert agreement.decisive == 2 and agreement.agreed == 0
    assert agreement.rate == 0.0


async def test_the_evaluation_reports_nothing_rather_than_zero_when_nothing_is_held_out(db, board_of):
    agreement = await evaluation.held_out_agreement(db, user_id=board_of, kind="movie")
    assert agreement.pairs == 0
    assert agreement.rate is None, "a held-out sample of zero is 'not measured', not 'terrible'"


async def test_a_tie_is_counted_and_not_scored(db, board_of):
    """§4.2: "about the same" is first-class data. Scoring it either way needs a threshold on
    |Δs| nothing has measured, and inventing one inside the honesty instrument is the last
    place to put an unmeasured constant."""
    await observations.record_duel(
        db, user_id=board_of, title_a=1, title_b=2, outcome="TIE",
        context="tier_queue", selection=queue.ARM_HOLDOUT,
    )
    agreement = await evaluation.held_out_agreement(db, user_id=board_of, kind="movie")
    assert (agreement.pairs, agreement.ties, agreement.decisive) == (1, 1, 0)
    assert agreement.rate is None


async def test_only_the_evaluation_module_reads_the_held_out_stream():
    """A static half, because "no other read path does this" is a claim about code.

    Every query that filters on `uniform_holdout` is either the evaluation's admission or the
    fit's and the selector's exclusion. A new one appearing anywhere else is the leak §13's
    guard is about, and it would not fail any runtime test.
    """
    allowed = {
        "rank/evaluation.py": "admits only these rows (§13's one evaluation read path)",
        "ledger/observations.py": "defines the constant; excludes them from the nightly fit",
        "ledger/refit.py": "excludes them from the <50 ms incremental path and its mean margin",
        "rank/read.py": "excludes them from the selector's comparison counts",
        "rank/queue.py": "names the arm that writes them",
        "home/rail.py": "names the arm in the §6.7 log line (proposal 120)",
        "api/rank.py": "skips the incremental refit after a held-out answer, so the evaluation "
                       "stream cannot move the freshness clock the selector reads",
        # M4. 54b binds §13's guard to Tonight's round as well, so the stream has two more
        # deliberate readers — one that excludes it and one that admits only it, which is the
        # same pairing `ledger/observations.py` and `rank/evaluation.py` already make.
        "tonight/round.py": "names the arm, and excludes those answers from the posterior that "
                            "selection and stopping read (54b)",
        "tonight/evaluation.py": "admits only those rows (54b's one evaluation read path for "
                                 "the round)",
    }
    offenders = [
        rel for rel in _files_naming_the_held_out_stream(PACKAGE) if rel not in allowed
    ]
    assert not offenders, (
        "§13's held-out stream is read somewhere new; every read path has to be deliberate: "
        f"{offenders}"
    )


# Every spelling of the value. `ARM_HOLDOUT` is the name the rest of the package imports it
# under and is NOT a superstring of `HELD_OUT`, so the first version of this guard missed the
# one spelling a new reader would actually use — found by the M3 review.
_HELD_OUT_NAMES = ("uniform_holdout", "HELD_OUT", "ARM_HOLDOUT")


def _files_naming_the_held_out_stream(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if any(name in text for name in _HELD_OUT_NAMES):
            found.append(path.relative_to(root).as_posix())
    return found


def test_the_held_out_guard_catches_a_new_reader(tmp_path):
    """docs/TESTING.md: "A guard needs a self-test … a guard that cannot fail reads as coverage
    while providing none."

    Fed the shape a leak would actually take: a module that imports the constant by the name
    the package uses rather than spelling the string out.
    """
    package = tmp_path / "spielplan"
    (package / "scoring").mkdir(parents=True)
    leak = [
        "from spielplan.rank.queue import ARM_HOLDOUT",
        "SQL = 'SELECT 1 FROM duel WHERE selection = $1'  # bound to ARM_HOLDOUT",
    ]
    (package / "scoring" / "foldin.py").write_text("\n".join(leak), encoding="utf-8")
    (package / "innocent.py").write_text("x = 1\n", encoding="utf-8")

    assert _files_naming_the_held_out_stream(package) == ["scoring/foldin.py"]


# --- §6.7: the log line names the arm that drew the pair ----------------------------------


@pytest.mark.parametrize(
    ("arm", "phrase"),
    [
        (queue.ARM_BOUNDARY, "boundary-targeted"),
        (queue.ARM_EXPLORATION, "exploration"),
        (queue.ARM_HOLDOUT, "uniform-random, held out"),
    ],
)
async def test_a_queue_answer_stores_its_arm_and_the_log_line_names_the_same_one(
    db, board_of, arm, phrase
):
    """§6.7 + §13 / proposal 120: the prototype asserted "boundary-targeted pair (70/20/10
    policy)" unconditionally, so every tenth line lied about the one stream that must not be
    adaptively selected."""
    write = await observations.record_duel(
        db, user_id=board_of, title_a=1, title_b=2, outcome="A",
        context="tier_queue", selection=arm,
    )
    stored = await db.fetchval("SELECT selection FROM duel WHERE id = $1", write.row_id)
    assert stored == arm

    names = await read.names_for(db, [1, 2])
    line = rail.duel_line(names[1], names[2], "A", context="tier_queue", selection=arm)
    assert phrase in line
    assert line.endswith(phrase)


async def test_a_held_out_pair_is_never_narrated_as_boundary_targeted(db, board_of):
    line = rail.duel_line(
        "Heat", "Drive", "A", context="tier_queue", selection=queue.ARM_HOLDOUT
    )
    assert "boundary" not in line
    assert "held out" in line


async def test_an_arm_with_no_phrase_fails_loudly_rather_than_borrowing_one():
    """`duel.selection`'s CHECK and `ARM_PHRASES` have to stay in step; a new arm that rendered
    as whatever the previous branch said is proposal 120's bug in a new coat."""
    with pytest.raises(rail.RailError):
        rail.duel_line("a", "b", "A", context="tier_queue", selection="clairvoyance")

    check = await_free_check_values()
    assert check == set(rail.ARM_PHRASES), (
        "every value duel.selection admits needs a phrase, and no phrase may name a value the "
        "column refuses"
    )


def await_free_check_values() -> set[str]:
    """The `selection` CHECK's value set, read out of 0005 rather than out of a live database,
    so this stays a pure test that runs without Postgres."""
    import re

    sql = (PACKAGE.parent / "migrations" / "0005_ledger.sql").read_text(encoding="utf-8")
    match = re.search(r"CHECK \(selection IN \(([^)]*)\)\)", sql)
    assert match, "0005 no longer constrains duel.selection"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


# --- §6.3's filters, under §4.1 -----------------------------------------------------------


@pytest.fixture
async def tagged(db, board_of):
    """DNA rows on the fitted board, in **both** tiers and overlapping — §4.1 rule 1's
    "14,181 (title,term) pairs exist in both and must stay distinguishable", in miniature."""
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 1, 2) "
        "ON CONFLICT DO NOTHING"
    )
    await db.executemany(
        "INSERT INTO dna_facet (version, facet, ord) VALUES ($1, $2, $3) "
        "ON CONFLICT DO NOTHING",
        [("v1", "mood", 0)],
    )
    await db.executemany(
        "INSERT INTO dna_term (version, term, facet) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
        [("v1", "cosy", "mood"), ("v1", "bleak", "mood")],
    )
    await db.executemany(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, n_sources) "
        "VALUES ($1, 'v1', $2, 'mood', 3, 0.2, 1)",
        [(1, "cosy"), (2, "bleak")],
    )
    await db.executemany(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight) "
        "VALUES ($1, 'v1', $2, 'mood', 0.1)",
        [(1, "cosy"), (3, "cosy")],
    )
    return board_of


async def test_each_rank_filter_narrows_the_board_on_its_own(db, board_of):
    """§6.3's filter list, minus the two the M0 catalog already has tests for."""
    everything = await read.items(db, user_id=board_of, kind="movie")
    assert len(everything) == 6

    short = await read.items(
        db, user_id=board_of, kind="movie", filters=library.RankFilters(runtime_max=110)
    )
    assert 0 < len(short) < len(everything)
    ids = {i.title_id for i in short}
    runtimes = {
        int(r["id"]): int(r["runtime_min"])
        for r in await db.fetch("SELECT id, runtime_min FROM title WHERE id = ANY($1::int[])",
                                list(ids))
    }
    assert all(v <= 110 for v in runtimes.values())

    seen = await read.items(
        db, user_id=board_of, kind="movie", filters=library.RankFilters(seen="seen")
    )
    # Every verdict implies seen (§6.1), so the fitted board is entirely seen; `unseen` is the
    # side that has to come back empty, which is the direction a broken predicate gets wrong.
    assert len(seen) == len(everything)
    unseen = await read.items(
        db, user_id=board_of, kind="movie", filters=library.RankFilters(seen="unseen")
    )
    assert unseen == []


async def test_combining_rank_filters_intersects(db, board_of):
    both = await read.items(
        db, user_id=board_of, kind="movie",
        filters=library.RankFilters(runtime_max=140, decade=1995),
    )
    by_runtime = await read.items(
        db, user_id=board_of, kind="movie", filters=library.RankFilters(runtime_max=140)
    )
    by_decade = await read.items(
        db, user_id=board_of, kind="movie", filters=library.RankFilters(decade=1995)
    )
    assert {i.title_id for i in both} == (
        {i.title_id for i in by_runtime} & {i.title_id for i in by_decade}
    )


async def test_a_dna_predicate_matches_bare_and_facet_qualified_alike(db, tagged):
    """§6.3's own example is written qualified — "show only `mood.cosy`" — and `dna_tag` stores
    only the bare term, so the qualified form exists at query time or not at all."""
    bare = await read.items(
        db, user_id=tagged, kind="movie", filters=library.RankFilters(dna="cosy")
    )
    qualified = await read.items(
        db, user_id=tagged, kind="movie", filters=library.RankFilters(dna="mood.cosy")
    )
    assert {i.title_id for i in bare} == {i.title_id for i in qualified} == {1, 3}

    other = await read.items(
        db, user_id=tagged, kind="movie", filters=library.RankFilters(dna="bleak")
    )
    assert {i.title_id for i in other} == {2}

    nothing = await read.items(
        db, user_id=tagged, kind="movie", filters=library.RankFilters(dna="pacing.cosy")
    )
    assert nothing == [], "a wrong facet is a different predicate, not a looser one"


async def test_a_dna_predicate_reaches_both_tiers_and_keeps_them_apart(db, tagged):
    """§4.1 rule 1: the two tiers are "never merged, never unioned … every read joins carry a
    `tier` discriminator". A predicate that answered from `dna_tag` alone would silently say no
    for the projected tier; one that answered from a fresh UNION would lose the tier."""
    survivors = await read.items(
        db, user_id=tagged, kind="movie", filters=library.RankFilters(dna="cosy")
    )
    ids = [i.title_id for i in survivors]
    assert set(ids) == {1, 3}

    matched = await library.dna_tiers_for(db, title_ids=ids, dna="cosy")
    assert matched[1] == ["extracted", "projected"], "a pair in both tiers reports both"
    assert matched[3] == ["projected"]


async def test_no_rank_filter_puts_a_threshold_on_a_weight(db, tagged):
    """§4.1 rule 2: a 0.5 confidence cut deletes 44% of the extracted tier. Title 1's tag has
    confidence 0.2 and its projection weight 0.1 — both far under any cut somebody would reach
    for — and it has to survive.

    The static guard (`test_landmine_guards`) greps the package for the predicate; this is the
    behavioural half, on the one filter where the cut is tempting."""
    survivors = await read.items(
        db, user_id=tagged, kind="movie", filters=library.RankFilters(dna="cosy")
    )
    assert 1 in {i.title_id for i in survivors}


async def test_no_filter_suspends_the_kind_partition(db, world):
    """§4.1 rule 5. The Rank board is a surface that RANKS, so it partitions by kind under
    every filter — a DNA term shared across the two kinds is where a merge would show."""
    user = world["patrick"]
    await rate(db, user, verdicts=[(1, 2), (2, 0)])
    await rate(db, user, verdicts=[(11, 2), (12, 0)])
    await fitted(db, user, "movie")
    await fitted(db, user, "series")

    films = await read.items(db, user_id=user, kind="movie")
    series = await read.items(db, user_id=user, kind="series")
    assert {i.title_id for i in films} == {1, 2}
    assert {i.title_id for i in series} == {11, 12}

    filtered = await read.items(
        db, user_id=user, kind="series", filters=library.RankFilters(runtime_max=999)
    )
    assert {i.title_id for i in filtered} == {11, 12}


async def test_the_queue_pool_is_the_whole_board_and_not_the_filtered_view(db, board_of):
    """§6.3's filters are a way of looking at the board; the queue sharpens the *ranking*. A
    queue restricted to whatever the person last typed would sharpen one corner of it, and the
    identity proposal 157 asks for — badged set is queue set — would hold only on that corner.
    """
    filters = library.RankFilters(runtime_max=95)
    narrow = await read.items(db, user_id=board_of, kind="movie", filters=filters)
    assert len(narrow) < 6

    pool = await read.candidates(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    assert len(pool) == 6


async def test_the_board_and_the_queue_agree_about_who_is_eligible(db, board_of):
    """Proposal 157 end to end, through the database rather than through two fixtures."""
    tiers, cuts, rows = await read.load(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    badged = {e.title_id for t in tiers for e in t.entries if e.straddle is not None}
    eligible = {
        i.title_id for i in queue.eligible(rows, cuts=cuts.boundaries, hp=DEFAULTS)
    }
    assert badged == eligible


async def test_a_drawn_pair_is_two_titles_from_this_persons_board(db, board_of):
    pool = await read.candidates(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    ids = {c.title_id for c in pool}
    rng = random.Random(2)
    for _ in range(200):
        pair = queue.draw(pool, rng=rng)
        assert {pair.title_a, pair.title_b} <= ids
        assert pair.title_a != pair.title_b


def test_the_board_reads_the_displayed_sigma_not_the_fitted_one():
    """§5.2's freshness rule inflates the *displayed* σ after twelve untouched months, and the
    badges are a claim about how sure the model is now. Reading `sigma` would make a board that
    nobody has touched for two years look as settled as the day it was fitted."""
    source = (PACKAGE / "rank" / "read.py").read_text(encoding="utf-8")
    assert "COALESCE(ls.sigma_eff, ls.sigma)" in source


def test_the_board_is_the_rated_titles_and_not_the_owned_library():
    """§6.3: "every **rated** title". An owned title nobody has rated has a coordinate (§12's
    M2 exit criterion) and no business on a tier list."""
    source = (PACKAGE / "rank" / "read.py").read_text(encoding="utf-8")
    assert "ls.observed" in source


async def test_an_unrated_owned_title_is_not_on_the_board(db, board_of):
    """The behavioural half of the above: titles 7 to 10 are owned, placed by the nightly
    refit, and unrated."""
    everything = await read.items(db, user_id=board_of, kind="movie")
    assert {i.title_id for i in everything} == {1, 2, 3, 4, 5, 6}
    placed = await db.fetchval(
        "SELECT count(*) FROM ledger_state WHERE user_id=$1 AND kind='movie'", board_of
    )
    assert placed > 6, "the unrated titles are placed; they are just not on the tier list"


async def test_a_board_with_no_cutpoints_row_falls_back_to_the_prior_not_to_percentiles(db, world):
    """§5.2: the fitted cutpoints ARE the displayed boundaries, so there is no second set. With
    nothing fitted the fallback is the same prior the model would start from — which is not the
    same thing as cutting whatever population happens to be on screen."""
    from spielplan.ledger import model

    cuts = await read.cutpoints_of(db, user_id=world["jenny"], kind="movie")
    assert cuts.tier_set == observations.DEFAULT_TIER_SET
    assert np.allclose(cuts.boundaries, model.initial_cutpoints(7))


async def test_the_public_projection_carries_no_ungated_model_number(db, board_of):
    """Decision 117 gates every inline numeric annotation. A board row that shipped `s` and σ
    at the top level would route around `rail.redact`, which removes them from a `model` block
    and cannot remove them from somewhere it does not know about."""
    tiers, _cuts, _rows = await read.load(db, user_id=board_of, kind="movie", hp=DEFAULTS)
    payload = read.public(tiers)
    flat = repr(payload)
    for entry in (e for t in payload for e in t["entries"]):
        assert "s" not in entry and "sigma" not in entry
    assert "sigma" not in flat


# --- §13's instrument, sharpened by the M3 review ---------------------------------------------


async def test_the_evaluation_partitions_by_kind_on_both_sides_of_the_pair(db, board_of):
    """§4.1 rule 5, at the one seam where `record_duel`'s write-time refusal stops holding.

    A cross-kind duel cannot be written — but `title.kind` is not immutable: §10's re-import
    upserts it, so a corpus reclassification (miniseries -> series) retroactively makes an
    existing held-out duel cross-kind. `load_observations` joins BOTH sides for exactly this
    reason and says so; the evaluation joined one, so the fit and §13's figure would disagree
    about the population after any re-import that corrected a kind.
    """
    await observations.record_duel(
        db, user_id=board_of, title_a=1, title_b=2, outcome="A",
        context="tier_queue", selection=queue.ARM_HOLDOUT,
    )
    before = await evaluation.held_out_agreement(db, user_id=board_of, kind="movie")
    assert before.pairs == 1

    await db.execute("UPDATE title SET kind = 'series' WHERE id = 2")
    after = await evaluation.held_out_agreement(db, user_id=board_of, kind="movie")
    assert after.pairs == 0, (
        "a pair with one foot in each partition is not evidence about either (§4.1 rule 5)"
    )


async def test_the_evaluation_abstains_when_the_model_has_no_ordering(db, board_of):
    """The module refuses to score the PERSON's tie because scoring it needs a threshold on
    |delta s| that nothing has measured. `s_a == s_b` is the model's tie, and it was being
    folded into a confident prediction of "B" — the same unmeasured threshold, placed at zero
    and only in one direction.
    """
    await db.execute(
        "UPDATE ledger_state SET s = 0.5 WHERE user_id = $1 AND title_id IN (1, 2)", board_of
    )
    await observations.record_duel(
        db, user_id=board_of, title_a=1, title_b=2, outcome="A",
        context="tier_queue", selection=queue.ARM_HOLDOUT,
    )
    agreement = await evaluation.held_out_agreement(db, user_id=board_of, kind="movie")
    assert agreement.pairs == 1
    assert agreement.undecided == 1
    assert agreement.decisive == 0 and agreement.agreed == 0
    assert agreement.rate is None, "no ordering is an abstention, not a coin flip scored as B"
