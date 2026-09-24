"""§6.6 Data's reject review: two orderings and no filter. Spec v2.1 §6.6 Data, §4.1 rule 2, §8 stage 7.

Decision 446: "The review shows two lists: `dna_reject` rows newest first, bounded by a LIMIT on
recency and never on a weight; a title's low-evidence tags: its extracted `dna_tag` rows ordered by
confidence ASC NULLS LAST, then `n_sources` ASC, then term, with no predicate and no LIMIT on any
weight". Decision 341 is why the second half is not over `dna_reject`: the table carries no
confidence, because what it records is what stage 7 refused, and "low-evidence" is a property of
what it let through.

WHAT A FILTER WOULD LOOK LIKE HERE, so the tests below know what they are refusing. §4.1: "No `WHERE
confidence > x` anywhere (a 0.5 cut would delete 44% of the extracted tier)". The review is the one
screen whose whole subject is the weakest tags, which makes a "hide the noise" cut the natural edit
-- so the low-evidence tests seed a NULL confidence and a 0.01 beside the rest and require both,
and require the count to be the title's whole extracted tier at the version. The static half is
`test_landmine_guards.py`, which reads `dna/review.py` with the rest of the package.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spielplan.dna import review, verify
from spielplan.importer import dna
from spielplan.importer.report import ImportReport
from tests.fixtures import make_bundle as fx

TITLE = 2
NEIGHBOUR = 3
EPOCH = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def bundle_dir(tmp_path):
    fx.make_bundle(tmp_path / "bundle")
    return tmp_path / "bundle"


async def _install(conn, bundle_dir) -> None:
    await conn.executemany(
        "INSERT INTO title (id, kind, name, year) VALUES ($1, $2, $3, $4)",
        [(t[0], t[1], t[2], t[4]) for t in fx.TITLES],
    )
    report = ImportReport()
    await dna.load_vocabulary(conn, bundle_dir / "artifacts" / "dna_vocab" / "v1", "v1", report)
    assert report.ok, report.render()


async def _reject(conn, n: int, *, title_id: int | None = TITLE, rule: str = "quote_unverified",
                  salience: int | None = 2) -> None:
    """One refusal, `n` minutes after EPOCH so recency is the test's to set and not the clock's."""
    await conn.execute(
        "INSERT INTO dna_reject (title_id, term, facet, salience, quote, rule_violated, provider, at)"
        " VALUES ($1, $2, 'mood', $3, $4, $5, 'gemini', $6)",
        title_id, f"mood.term_{n}", salience, f"a sentence nobody wrote, number {n}", rule,
        EPOCH + timedelta(minutes=n),
    )


async def _tag(conn, title_id: int, term: str, confidence: float | None, n_sources: int | None,
               *, version: str = "v1", provider: str = "") -> None:
    await conn.execute(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, n_sources, provider)"
        " VALUES ($1, $2, $3, split_part($3, '.', 1), 2, $4, $5, $6)",
        title_id, version, term, confidence, n_sources, provider,
    )


# --- the first half: what stage 7 refused, newest first -----------------------------------------


async def test_rejects_come_newest_first_and_are_bounded_on_recency(db, bundle_dir):
    """The screen opens on "what has this install been dropping lately" (`0027_dna_extraction.sql`
    over `dna_reject_rule`), so the newest row is first and the bound drops the OLDEST rows -- a
    bound that dropped anything else would be choosing which refusals the operator sees."""
    await _install(db, bundle_dir)
    for n in (3, 1, 5, 2, 4):
        await _reject(db, n)

    rows = await review.rejects(db, limit=3)

    assert [r["term"] for r in rows] == ["mood.term_5", "mood.term_4", "mood.term_3"]

    for n in range(6, review.REJECT_LIMIT + 7):
        await _reject(db, n)
    everything = await review.rejects(db)

    assert len(everything) == review.REJECT_LIMIT
    assert everything[0]["term"] == f"mood.term_{review.REJECT_LIMIT + 6}"
    assert "mood.term_1" not in {r["term"] for r in everything}


async def test_every_rule_a_reject_can_carry_is_listed_with_what_the_reviewer_reads(db, bundle_dir):
    """Plan D4: "Each row shows the term, the quote, the salience and the rule violated". Every
    value of `verify.REASONS` is seeded and every one comes back, so the review has no reason it
    silently leaves out -- including `salience` values stage 7 refused for being out of range,
    which `dna_reject` stores verbatim precisely so that they can be read here."""
    await _install(db, bundle_dir)
    for n, rule in enumerate(verify.REASONS):
        await _reject(db, n, rule=rule, salience=(0, 4, None, 2, 3, 1, 2)[n])

    rows = await review.rejects(db)

    assert sorted(r["rule_violated"] for r in rows) == sorted(verify.REASONS)
    newest = rows[0]
    assert set(newest) >= {
        "id", "title_id", "name", "year", "term", "facet", "salience", "quote", "rule_violated",
        "provider", "at",
    }
    assert (newest["name"], newest["year"]) == ("Prisoners", 2013)
    assert newest["quote"] == f"a sentence nobody wrote, number {len(verify.REASONS) - 1}"
    assert [r["salience"] for r in rows] == [2, 1, 3, 2, None, 4, 0]


async def test_a_reject_naming_a_title_this_install_does_not_hold_is_still_listed(db, bundle_dir):
    """Decision 396 writes an `unknown_title` refusal with a NULL title, so the read is a LEFT JOIN:
    an inner join would drop exactly the refusal that says the extractor answered about a film this
    install never asked about."""
    await _install(db, bundle_dir)
    await _reject(db, 1, title_id=None, rule="unknown_title")
    await _reject(db, 2)

    rows = await review.rejects(db)

    assert [(r["title_id"], r["name"], r["rule_violated"]) for r in rows] == [
        (TITLE, "Prisoners", "quote_unverified"), (None, None, "unknown_title")
    ]


# --- the second half: a title's extracted tags, weakest first, none left out --------------------


async def test_low_evidence_is_ascending_confidence_with_nulls_last_then_n_sources(db, bundle_dir):
    """Decision 446's ordering exactly. NULLS LAST because a NULL confidence means nobody measured
    it (`0004_dna.sql:81` declares the column nullable and calls it a weight), which is not the
    same claim as the weakest measured reading -- and `n_sources` next because two tags read with
    the same confidence are told apart by how many runs found them (decision 337)."""
    await _install(db, bundle_dir)
    await _tag(db, TITLE, "mood.dread", 0.9, 3)
    await _tag(db, TITLE, "themes.obsession", None, 1)
    await _tag(db, TITLE, "pacing.patient", 0.5, 3)
    await _tag(db, TITLE, "visual.neon", 0.01, 2)
    await _tag(db, TITLE, "sound.score_forward", 0.5, 1)
    await _tag(db, TITLE, "place.domestic", 0.5, None)
    await _tag(db, TITLE, "era.period", 0.5, 1)

    listed = await review.low_evidence(db, TITLE)

    assert listed["title_id"] == TITLE and listed["version"] == "v1"
    assert [t["term"] for t in listed["tags"]] == [
        "visual.neon", "era.period", "sound.score_forward", "pacing.patient", "place.domestic",
        "mood.dread", "themes.obsession",
    ]
    assert set(listed["tags"][0]) >= {"term", "facet", "salience", "confidence", "n_sources", "provider"}


async def test_nothing_is_filtered_out_of_the_low_evidence_list(db, bundle_dir):
    """§4.1 rule 2, asserted on the rows rather than on the query text: the tag nobody measured and
    the tag measured at 0.01 are both there, and the count is the title's whole extracted tier at
    the active version. What is excluded is another title and another vocabulary -- scoping by the
    title and by §14 risk 7's "every read is scoped to one version", neither of which is a weight.
    """
    await _install(db, bundle_dir)
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count, imported_at)"
        " VALUES ('v0', 1, 1, now() - interval '1 day')"
    )
    await _tag(db, TITLE, "mood.dread", None, None)
    await _tag(db, TITLE, "mood.cosy", 0.01, 1)
    await _tag(db, TITLE, "mood.cosy", 0.02, 1, provider="anthropic")
    await _tag(db, TITLE, "themes.obsession", 0.99, 8)
    await _tag(db, NEIGHBOUR, "mood.dread", 0.001, 1)
    await _tag(db, TITLE, "mood.dread", 0.001, 1, version="v0")

    listed = await review.low_evidence(db, TITLE)

    by_term = [(t["term"], t["confidence"], t["provider"]) for t in listed["tags"]]
    assert ("mood.dread", None, "") in by_term
    assert any(term == "mood.cosy" and provider == "" for term, _c, provider in by_term)
    assert len(listed["tags"]) == await db.fetchval(
        "SELECT count(*) FROM dna_tag WHERE title_id = $1 AND version = 'v1'", TITLE
    ) == 4


async def test_an_install_with_no_vocabulary_has_no_low_evidence_tags(db):
    """§3.1's bundle-less app is a legal state: there is no version to scope to and no tag either."""
    assert await review.low_evidence(db, TITLE) == {"title_id": TITLE, "version": None, "tags": []}
    assert await review.rejects(db) == []
