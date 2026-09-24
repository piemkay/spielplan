"""Decision 337's run arithmetic and the per-provider write. Spec v2.1 §6.6, §4.1 rules 1 and 2.

§6.6 merges runs by "union with per-tag agreement as confidence" and §4.1 rule 2 makes agreement a
weight and never a filter, and neither states the function between them. Decision 337 ports the
corpus's: the share of runs that found a term, the count beside it, the highest salience any run
assigned, every run's evidence, and a tag one run of one found written at a confidence of 1.0. The
first half of this file pins that arithmetic with no database; the second half pins the write, one
`dna_tag` row per provider through `UNIQUE (title_id, version, term, provider)`, against Postgres,
because the four-column key and the cascade are the schema's and a test without them would be
asserting what the code meant rather than what the table kept.

A RUN IS ONE PROVIDER AT ONE PASS. Two tests exist because "counts runs" has two readings a port
could get wrong and still pass the other: two providers at one pass each must give exactly what two
passes of one provider give, and a term both providers found in three runs must weigh two of three,
not two providers of two.

The rule-2 half of the coverage row is not re-tested here. `test_landmine_guards.py`'s pair already
reads every module under `spielplan/`, this one included, and is registered on the row directly.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import itertools

import asyncpg
import pytest

from spielplan.dna.verify import VerifiedTag
from spielplan.llm import consensus

TITLE = 4242
OTHER_TITLE = 4343
V1 = "v1"
V0 = "v0"


def tag(term: str, salience: int = 2, quote: str | None = None, source: str = "trakt:comment"):
    """One tag as `verify_payload` hands it forward. The facet is the term's prefix, as it is for
    every facet this app's vocabulary carries."""
    return VerifiedTag(
        term=term, facet=term.split(".")[0], salience=salience, source=source,
        quote=quote or f"a quote about {term}",
    )


def weights(merged) -> dict[str, tuple[int, float, int]]:
    return {m.term: (m.salience, m.confidence, m.n_sources) for m in merged}


# --- the arithmetic, no database ------------------------------------------------------------


def test_two_passes_of_one_provider_weigh_each_term_by_the_share_that_found_it():
    merged, n_runs = consensus.merge_passes({
        "gemini:1": [tag("mood.tense"), tag("pacing.slow_burn")],
        "gemini:2": [tag("mood.tense"), tag("place.city")],
    })

    assert n_runs == 2
    assert [m.term for m in merged] == ["mood.tense", "pacing.slow_burn", "place.city"]
    assert weights(merged) == {
        "mood.tense": (2, 1.0, 2),
        "pacing.slow_burn": (2, 0.5, 1),
        "place.city": (2, 0.5, 1),
    }
    assert {m.providers for m in merged} == {("gemini",)}


def test_two_providers_at_one_pass_each_are_the_same_arithmetic_as_two_passes_of_one():
    first = [tag("mood.tense", 3), tag("pacing.slow_burn", 1)]
    second = [tag("mood.tense", 2), tag("place.city", 2)]

    one_provider, runs_of_one = consensus.merge_passes({"gemini:1": first, "gemini:2": second})
    two_providers, runs_of_two = consensus.merge_passes({"gemini:1": first, "anthropic:1": second})

    assert runs_of_one == runs_of_two == 2
    assert weights(one_provider) == weights(two_providers)
    assert {m.term: m.providers for m in two_providers} == {
        "mood.tense": ("anthropic", "gemini"),
        "pacing.slow_burn": ("gemini",),
        "place.city": ("anthropic",),
    }


def test_agreement_counts_runs_and_not_providers():
    """Both providers found `mood.tense`, so a merge counting providers would call it unanimous;
    it was found by two runs of three, and that is the weight it carries."""
    merged, n_runs = consensus.merge_passes({
        "gemini:1": [tag("mood.tense")],
        "gemini:2": [tag("place.city")],
        "anthropic:1": [tag("mood.tense")],
    })

    assert n_runs == 3
    assert weights(merged)["mood.tense"] == (2, 0.67, 2)
    assert weights(merged)["place.city"] == (2, 0.33, 1)


def test_a_tag_found_by_one_run_of_one_is_kept_at_a_confidence_of_one():
    merged, n_runs = consensus.merge_passes({"openai:1": [tag("mood.tense", 1)]})

    assert n_runs == 1
    assert weights(merged) == {"mood.tense": (1, 1.0, 1)}
    assert merged[0].providers == ("openai",)


def test_a_run_that_found_nothing_is_still_a_run():
    """`n_runs` is `len(passes)`, as the corpus counts it. A run that came back empty disagreed
    with every other run about every term, and leaving it out would double the weight of each."""
    merged, n_runs = consensus.merge_passes({"gemini:1": [tag("mood.tense")], "gemini:2": []})

    assert n_runs == 2
    assert weights(merged) == {"mood.tense": (2, 0.5, 1)}


def test_the_highest_salience_any_run_assigned_wins_in_every_run_order():
    runs = [("gemini:1", 1), ("anthropic:1", 3), ("openai:1", 2)]
    for order in itertools.permutations(runs):
        merged, _ = consensus.merge_passes({pid: [tag("mood.tense", level)] for pid, level in order})
        assert [m.salience for m in merged] == [3], order


def test_the_evidence_of_every_run_that_found_the_term_is_kept():
    """Not only the winner's: the run that assigned the top level quoted one sentence, and the two
    that assigned less quoted others, and every one of the three is a falsifiable reason the tag
    exists. Two runs quoting one sentence stay two items, told apart by their run."""
    merged, _ = consensus.merge_passes({
        "gemini:1": [tag("mood.tense", 1, "the air is thin")],
        "gemini:2": [tag("mood.tense", 3, "every scene holds its breath")],
        "anthropic:1": [tag("mood.tense", 2, "the air is thin", source="metacritic:critic")],
    })

    assert merged[0].evidence == (
        consensus.Evidence("gemini:1", "trakt:comment", "the air is thin"),
        consensus.Evidence("gemini:2", "trakt:comment", "every scene holds its breath"),
        consensus.Evidence("anthropic:1", "metacritic:critic", "the air is thin"),
    )


@pytest.mark.parametrize(
    "key", ["gemini", ":1", "gemini:", "gemini:0", "gemini:-1", "gemini:x", "gemini:١"]
)
def test_a_key_that_spells_no_run_is_refused(key):
    """The provider half of the key is written into `dna_tag`'s unique key, so a key that does not
    parse is refused -- even for a run that found nothing and so writes nothing -- rather than
    guessed at."""
    with pytest.raises(ValueError, match="names no run"):
        consensus.merge_passes({key: []})


def test_the_key_stage_6_spells_is_the_key_the_merge_reads():
    key = consensus.pass_id_for("anthropic", 2)

    assert key == "anthropic:2"
    assert consensus.provider_of(key) == "anthropic"


# --- the write, against Postgres ------------------------------------------------------------


async def seed(db) -> None:
    """Two titles and two vocabulary versions: the write must replace one title's tier under one
    version and touch neither neighbour."""
    await db.executemany(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, 'movie', $2, $3, true)",
        [(TITLE, "Heat", 1995), (OTHER_TITLE, "Thief", 1981)],
    )
    await db.executemany(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, 0, 0)",
        [(V0,), (V1,)],
    )


async def tier(db, title_id: int = TITLE, version: str = V1) -> dict:
    """The extracted tier as the table holds it: per (term, provider), the three weights and that
    row's own evidence as (quote, source, run)."""
    out = {}
    for row in await db.fetch(
        "SELECT id, term, provider, salience, confidence, n_sources FROM dna_tag "
        "WHERE title_id = $1 AND version = $2",
        title_id, version,
    ):
        evidence = [
            tuple(e)
            for e in await db.fetch(
                "SELECT quote, source, source_ref FROM dna_evidence WHERE dna_tag_id = $1 ORDER BY id",
                row["id"],
            )
        ]
        out[(row["term"], row["provider"])] = (
            row["salience"], round(row["confidence"], 2), row["n_sources"], evidence,
        )
    return out


async def test_a_term_two_providers_found_is_two_rows_with_the_pooled_weights_and_their_own_quotes(db):
    await seed(db)
    merged, n_runs = consensus.merge_passes({
        "gemini:1": [
            tag("mood.tense", 3, "the air is thin"),
            tag("place.city", 2, "the city is the fourth lead"),
        ],
        "anthropic:1": [
            tag("mood.tense", 1, "every scene holds its breath", source="metacritic:critic"),
        ],
    })

    written = await consensus.store_title(db, TITLE, V1, merged, n_runs=n_runs)

    assert written == 3
    assert await tier(db) == {
        ("mood.tense", "anthropic"): (
            3, 1.0, 2, [("every scene holds its breath", "metacritic:critic", "anthropic:1")],
        ),
        ("mood.tense", "gemini"): (3, 1.0, 2, [("the air is thin", "trakt:comment", "gemini:1")]),
        ("place.city", "gemini"): (
            2, 0.5, 1, [("the city is the fourth lead", "trakt:comment", "gemini:1")],
        ),
    }


async def test_a_second_store_replaces_the_first_whoever_wrote_it(db):
    """Replace, not accumulate. The first tier here is the bundle importer's `''` row and then an
    earlier extraction's; the second write leaves neither a stale term nor a duplicate-key error,
    their evidence goes with them through the cascade, and the other title and the other version
    keep exactly what they had."""
    await seed(db)
    for title_id, version, provider in ((TITLE, V1, ""), (TITLE, V0, ""), (OTHER_TITLE, V1, "")):
        tag_id = await db.fetchval(
            "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, n_sources, "
            "provider) VALUES ($1, $2, 'register.camp', 'register', 2, 1.0, 1, $3) RETURNING id",
            title_id, version, provider,
        )
        await db.execute(
            "INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, 'a knowing wink', 'x')",
            tag_id,
        )
    untouched = (await tier(db, TITLE, V0), await tier(db, OTHER_TITLE, V1))

    first, n_first = consensus.merge_passes(
        {"gemini:1": [tag("mood.tense"), tag("pacing.slow_burn")]}
    )
    await consensus.store_title(db, TITLE, V1, first, n_runs=n_first)
    second, n_second = consensus.merge_passes({
        "gemini:1": [tag("mood.tense"), tag("place.city")],
        "anthropic:1": [tag("mood.tense")],
    })
    written = await consensus.store_title(db, TITLE, V1, second, n_runs=n_second)

    assert written == 3
    assert sorted(await tier(db)) == [
        ("mood.tense", "anthropic"), ("mood.tense", "gemini"), ("place.city", "gemini"),
    ]
    assert (await tier(db, TITLE, V0), await tier(db, OTHER_TITLE, V1)) == untouched
    assert await db.fetchval("SELECT count(*) FROM dna_evidence") == 3 + 2


async def test_a_tag_one_run_in_three_found_is_written_and_not_dropped(db):
    await seed(db)
    merged, n_runs = consensus.merge_passes({
        "gemini:1": [tag("mood.tense", 2, "the air is thin"), tag("register.camp", 1, "a wink")],
        "gemini:2": [tag("mood.tense", 2, "the air is thin")],
        "openai:1": [tag("mood.tense", 2, "the air is thin")],
    })

    await consensus.store_title(db, TITLE, V1, merged, n_runs=n_runs)

    got = await tier(db)
    assert got[("register.camp", "gemini")] == (1, 0.33, 1, [("a wink", "trakt:comment", "gemini:1")])
    assert got[("mood.tense", "gemini")] == (
        2, 1.0, 3,
        [("the air is thin", "trakt:comment", "gemini:1"),
         ("the air is thin", "trakt:comment", "gemini:2")],
    )
    assert got[("mood.tense", "openai")] == (2, 1.0, 3, [("the air is thin", "trakt:comment", "openai:1")])


async def test_a_write_that_fails_part_way_leaves_the_previous_tier_standing(db):
    """The delete and the inserts are one transaction. The second write fails on its SECOND row --
    after the delete and after one insert have run -- on `dna_tag`'s salience CHECK, and what the
    title carried before is still exactly what it carries."""
    await seed(db)
    good, n_good = consensus.merge_passes({"gemini:1": [tag("mood.tense"), tag("pacing.slow_burn")]})
    await consensus.store_title(db, TITLE, V1, good, n_runs=n_good)
    before = await tier(db)

    bad, n_bad = consensus.merge_passes({"gemini:1": [tag("mood.tense"), tag("place.city", 5)]})
    with pytest.raises(asyncpg.CheckViolationError):
        await consensus.store_title(db, TITLE, V1, bad, n_runs=n_bad)

    assert await tier(db) == before


async def test_no_run_at_all_never_erases_a_tier_and_empty_runs_do(db):
    """Runs that all came back empty are a verdict -- nothing verified against this pack -- and
    replace the tier with nothing. No run at all is the absence of a verdict and deletes nothing."""
    await seed(db)
    merged, n_runs = consensus.merge_passes({"gemini:1": [tag("mood.tense")]})
    await consensus.store_title(db, TITLE, V1, merged, n_runs=n_runs)
    before = await tier(db)

    with pytest.raises(ValueError, match="no run was merged"):
        await consensus.store_title(db, TITLE, V1, [], n_runs=0)
    assert await tier(db) == before

    empty, n_empty = consensus.merge_passes({"gemini:1": [], "gemini:2": []})
    assert await consensus.store_title(db, TITLE, V1, empty, n_runs=n_empty) == 0
    assert await tier(db) == {}
