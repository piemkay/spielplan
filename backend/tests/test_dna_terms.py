"""A vocabulary term by its label, never its id (§6.8).

Every id here is dotted and every label differs from its leaf, so a raw id cannot pass for words."""

from __future__ import annotations

from datetime import UTC, datetime

from spielplan.db import dna_terms


def test_label_of_names_a_term_by_its_shipped_label_or_its_leaf_in_plain_words():
    assert dna_terms.label_of("era.wwii", "World War II") == "World War II"
    assert dna_terms.label_of("era.wwii", " World War II ") == "World War II"
    assert dna_terms.label_of("pacing.relentless", None) == "relentless"
    assert dna_terms.label_of("characters.morally_grey", None) == "morally grey"
    assert dna_terms.label_of("characters.morally_grey", "   ") == "morally grey"
    assert dna_terms.label_of("era.renaissance_early_modern", "") == "renaissance early modern"
    assert dna_terms.label_of("obsession", None) == "obsession"


async def _vocabulary(db, version: str, imported_at: datetime, terms) -> None:
    await db.execute(
        "INSERT INTO dna_vocabulary (version, imported_at, facet_count, term_count) "
        "VALUES ($1, $2, 2, $3)",
        version, imported_at, len(terms),
    )
    await db.executemany(
        "INSERT INTO dna_facet (version, facet, ord) VALUES ($1, $2, $3)",
        [(version, "era", 0), (version, "pacing", 1)],
    )
    await db.executemany(
        "INSERT INTO dna_term (version, term, facet, label, gloss) VALUES ($1, $2, $3, $4, $5)",
        [(version, *t) for t in terms],
    )


async def test_labels_for_reads_the_active_vocabulary_and_never_answers_an_id(db):
    await _vocabulary(db, "v1", datetime(2026, 1, 1, tzinfo=UTC), [
        ("era.wwii", "era", "the war (superseded)", "old gloss"),
    ])
    await _vocabulary(db, "v2", datetime(2026, 9, 1, tzinfo=UTC), [
        ("era.wwii", "era", "World War II", "the Second World War as the ground"),
        ("pacing.relentless", "pacing", None, "never once lets the audience sit down"),
    ])

    got = await dna_terms.labels_for(
        db, ["era.wwii", "pacing.relentless", "era.wwii", "", "place.nowhere_else"]
    )

    assert got == {
        "era.wwii": {"label": "World War II", "gloss": "the Second World War as the ground"},
        "pacing.relentless": {"label": "relentless", "gloss": "never once lets the audience sit down"},
        "place.nowhere_else": {"label": "nowhere else", "gloss": None},
    }
    assert not any("." in v["label"] or "_" in v["label"] for v in got.values())
    assert await dna_terms.labels_for(db, []) == {}
