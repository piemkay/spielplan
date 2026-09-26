"""§6.6 Data's reject review: two orderings and no filter (decision 446). Needs TEST_DATABASE_URL.

§4.1 forbids a confidence cut, so the low-evidence tests seed a NULL and a 0.01 and require both."""

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


async def test_rejects_come_newest_first_and_are_bounded_on_recency(db, bundle_dir):
    """The bound drops the OLDEST rows; dropping any other would choose which refusals the operator sees."""
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
    """Out-of-range `salience` values are stored verbatim precisely so they can be read here."""
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
    """Decision 396 writes `unknown_title` refusals with a NULL title, so the read must be a LEFT JOIN."""
    await _install(db, bundle_dir)
    await _reject(db, 1, title_id=None, rule="unknown_title")
    await _reject(db, 2)

    rows = await review.rejects(db)

    assert [(r["title_id"], r["name"], r["rule_violated"]) for r in rows] == [
        (TITLE, "Prisoners", "quote_unverified"), (None, None, "unknown_title")
    ]


async def test_low_evidence_is_ascending_confidence_with_nulls_last_then_n_sources(db, bundle_dir):
    """NULLS LAST: a NULL confidence means nobody measured it, not the weakest reading."""
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
    assert await review.low_evidence(db, TITLE) == {"title_id": TITLE, "version": None, "tags": []}
    assert await review.rejects(db) == []
