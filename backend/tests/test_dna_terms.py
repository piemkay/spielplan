"""A vocabulary term by its name and never by its id. Spec v2.1 §6.8; decision 486.

§6.8 wants every why "in vocabulary terms", and `era.wwii` is the key while "World War II" is the
term. `db/dna_terms.label_of` is the one fallback for a row with no label and `labels_for` the one
read every payload builder takes a label from. Every id below is dotted and every label differs
from its leaf, because the fixture vocabulary `test_home.py` builds on is dotless and an id printed
raw there reads as plain words - no test built on it could see the defect this file is for.
"""

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
    """Scoped to the newest vocabulary (the same `ACTIVE_VERSION` every DNA read takes), an entry
    for every term asked about, and no answer that is an id: a NULL label and a term the vocabulary
    does not carry both come back as the leaf in plain words, with no gloss for the unknown one."""
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
