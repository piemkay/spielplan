"""The thin-facet measurement against a real Postgres (§8.4, decision 390). Needs TEST_DATABASE_URL."""

from __future__ import annotations

import pytest

from spielplan.dna.coverage import facet_coverage
from spielplan.importer import dna as importer_dna
from spielplan.importer.report import ImportReport
from tests.fixtures import make_bundle as fx

# §6.4's order, not alphabetical, so a mapping following `dna_facet.ord` is told from one following a sort.
FACET_ORDER = (
    "mood", "themes", "pacing", "structure", "visual", "sound",
    "characters", "place", "era", "sensibility", "register",
)

VOCAB = "v1"


async def _vocabulary(db, version: str = VOCAB, *, facets=FACET_ORDER) -> None:
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, $2, 0)",
        version, len(facets),
    )
    await db.executemany(
        "INSERT INTO dna_facet (version, facet, ord) VALUES ($1, $2, $3)",
        [(version, facet, i) for i, facet in enumerate(facets)],
    )


async def _title(db, title_id: int, name: str) -> None:
    await db.execute(
        "INSERT INTO title (id, kind, name, is_owned) VALUES ($1, 'movie', $2, true)",
        title_id, name,
    )


async def _tag(db, title_id: int, term: str, *, version: str = VOCAB, facet: str | None = None,
               salience: int = 3, provider: str = "") -> None:
    """`facet` is overridable for the shape `app_facet` repairs, `provider` for §6.6's parallel mode."""
    await db.execute(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, provider) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        title_id, version, term, facet or term.split(".", 1)[0], salience, provider,
    )


async def _projected(db, title_id: int, term: str, *, version: str = VOCAB) -> None:
    await db.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
        "VALUES ($1, $2, $3, $4, 2, 'keyword')",
        title_id, version, term, term.split(".", 1)[0],
    )


@pytest.fixture
async def tagged(db):
    await _title(db, 1, "Heat")
    await _title(db, 2, "Paddington 2")
    await _vocabulary(db)
    await _tag(db, 1, "mood.bleak")
    await _tag(db, 1, "mood.tense")
    await _tag(db, 1, "themes.loyalty")
    await _tag(db, 1, "sound.percussive")


async def test_every_declared_facet_is_a_key_and_the_silent_ones_are_zero(db, tagged):
    """An absent key would say the vocabulary has no such facet, which is a different claim."""
    covered = await facet_coverage(db, 1)

    assert list(covered) == list(FACET_ORDER)
    assert covered["mood"] == 2
    assert covered["themes"] == 1
    assert covered["sound"] == 1
    assert [facet for facet, n in covered.items() if n == 0] == [
        "pacing", "structure", "visual", "characters", "place", "era", "sensibility", "register",
    ]


async def test_a_title_nobody_has_named_reports_zeroes_rather_than_nothing(db, tagged):
    covered = await facet_coverage(db, 2)

    assert list(covered) == list(FACET_ORDER)
    assert set(covered.values()) == {0}


async def test_one_term_named_by_three_providers_is_one_term_of_coverage(db):
    """Three providers agreeing on one term named one piece of the vocabulary once."""
    await _title(db, 1, "Heat")
    await _title(db, 2, "Paddington 2")
    await _vocabulary(db)
    for provider in ("gemini37", "sonnet5", "terra"):
        await _tag(db, 1, "mood.bleak", provider=provider)
    for term in ("mood.bleak", "mood.tense", "mood.wry"):
        await _tag(db, 2, term)

    assert (await facet_coverage(db, 1))["mood"] == 1
    assert (await facet_coverage(db, 2))["mood"] == 3


async def test_the_order_a_real_install_returns_is_the_loaders_alphabetical_index(db, tmp_path):
    """The loader writes `ord` from `sorted(facet_names)`,
    so on a real install the order is alphabetical."""
    fx.make_bundle(tmp_path / "bundle")
    report = ImportReport()
    await importer_dna.load_vocabulary(
        db, tmp_path / "bundle" / "artifacts" / "dna_vocab" / VOCAB, VOCAB, report
    )
    assert report.ok, report.render()
    await _title(db, 1, "Heat")

    covered = await facet_coverage(db, 1)

    assert set(covered) == set(FACET_ORDER)
    assert list(covered) == sorted(FACET_ORDER)


async def test_the_projected_tier_is_not_coverage(db, tagged):
    """Measured twice with the projection in between: the claim is that the number does not move."""
    before = await facet_coverage(db, 1)
    await _projected(db, 1, "pacing.languid")
    await _projected(db, 1, "mood.bleak")

    assert await facet_coverage(db, 1) == before


async def test_a_tag_under_a_superseded_vocabulary_is_invisible_and_addressable(db, tagged):
    """§14 risk 7: "every read is scoped to one version"."""
    await _vocabulary(db, "v0", facets=("mood",))
    # Stamped older on purpose: `ACTIVE_VERSION` orders on `imported_at DESC`, and each statement commits.
    await db.execute(
        "UPDATE dna_vocabulary SET imported_at = now() - interval '1 day' WHERE version = 'v0'"
    )
    await _tag(db, 2, "mood.cosy", version="v0")

    assert (await facet_coverage(db, 2))["mood"] == 0
    assert await facet_coverage(db, 2, version="v0") == {"mood": 1}


async def test_a_facet_label_the_vocabulary_does_not_declare_covers_nothing(db, tagged):
    """Re-deriving the prefix here would put the facet rule in a third place."""
    await _tag(db, 2, "mood_tone.wistful", facet="mood_tone")

    covered = await facet_coverage(db, 2)
    assert "mood_tone" not in covered
    assert set(covered.values()) == {0}


async def test_an_install_with_no_vocabulary_measures_nothing_and_raises_nothing(db):
    """§3.1: "a bundle-less app is a legal state"."""
    await _title(db, 1, "Heat")

    assert await facet_coverage(db, 1) == {}
