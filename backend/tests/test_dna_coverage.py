"""The thin-facet measurement, against a real Postgres. Spec v2.1 §8.4, §4.1 rules 1 and 2.

§8.4's fourth flywheel feed is "thin-facet titles" and decision 390 ships the measurement without
the threshold decision 329 has not taken. That split is the whole subject of this file: five of
its tests are about what the measurement counts, and two are about the number it must not carry.

Almost everything here is an integration test because almost everything it pins is a fact about
one SQL statement. The zeroes have to come back from a LEFT JOIN against `dna_facet`, the tier
exclusion is a join predicate on the sanctioned view, and the version scoping is a WHERE against
the row `db/dna_terms.active_version` picks; a unit test over a mapping built in Python would
agree with whatever the mapping was built from and would have nothing to say about any of them.

The two static tests are the ones worth reading twice. A measurement that acquires a threshold
stops being a measurement, and it acquires one in a single line -- a comprehension filtering the
counts, a default argument, a `>=` in a helper -- so `_numeric_literals` reads the module for any
number reaching its code at all and the self-test below feeds it exactly that line to prove it can
fail. The name guard is the other half: `placement/features.py:90-109` already owns `is_thin` for
a per-block test that answers a different question, and decision 329 exists because those two
were being spoken about as one.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from spielplan.dna import coverage as coverage_module
from spielplan.dna.coverage import facet_coverage
from spielplan.importer import dna as importer_dna
from spielplan.importer.report import ImportReport
from tests.fixtures import make_bundle as fx
from tests.test_static_contracts import VOCAB_V1_FACETS

MODULE = Path(coverage_module.__file__)

# §6.4's eleven, in §6.4's own order rather than alphabetically. The order is deliberate: the
# importer writes `dna_facet.ord` from `sorted(facet_names)` (`importer/dna.py:154`), so a
# fixture seeded alphabetically could not tell a mapping that follows `ord` from one that
# follows a sort or a dict's arrival order. Tied to the one declaration of the eleven below, so
# that a fixture naming a facet the vocabulary does not have fails here rather than in the
# assertion it was supposed to support.
FACET_ORDER = (
    "mood", "themes", "pacing", "structure", "visual", "sound",
    "characters", "place", "era", "sensibility", "register",
)

VOCAB = "v1"


def test_the_fixture_names_the_shipped_eleven():
    assert set(FACET_ORDER) == VOCAB_V1_FACETS
    assert len(FACET_ORDER) == len(VOCAB_V1_FACETS)


# --- the fixtures ----------------------------------------------------------------------


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
    """The extracted tier as the loader writes it: the whole `facet.term` id in `term` and the
    id's own prefix in `facet` (`importer/dna.app_facet`, `0018_read_layer.sql:41-45`). `facet`
    is overridable only so that one test can seed the shape those two exist to repair, and
    `provider` so that one can seed section 6.6's parallel mode, where `dna_tag`'s key
    `(title_id, version, term, provider)` makes one term named by three providers three rows."""
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
    """One title named in three of the eleven facets, and one named in none of them."""
    await _title(db, 1, "Heat")
    await _title(db, 2, "Paddington 2")
    await _vocabulary(db)
    await _tag(db, 1, "mood.bleak")
    await _tag(db, 1, "mood.tense")
    await _tag(db, 1, "themes.loyalty")
    await _tag(db, 1, "sound.percussive")


# --- what the measurement counts --------------------------------------------------------


async def test_every_declared_facet_is_a_key_and_the_silent_ones_are_zero(db, tagged):
    """§8.4 asks which facets the sources said nothing about, so the eight silent ones have to
    come back as zeroes. An absent key would say the vocabulary has no such facet, which is a
    different claim and the one thing the flywheel must not be told.

    The key order is asserted too, and it is `dna_facet.ord` rather than a sort this module
    performs -- which the fixture can only show by seeding `ord` out of alphabetical order,
    since the loader writes it alphabetically (the real-loader test below pins that).
    """
    covered = await facet_coverage(db, 1)

    assert list(covered) == list(FACET_ORDER)
    assert covered["mood"] == 2
    assert covered["themes"] == 1
    assert covered["sound"] == 1
    assert [facet for facet, n in covered.items() if n == 0] == [
        "pacing", "structure", "visual", "characters", "place", "era", "sensibility", "register",
    ]


async def test_a_title_nobody_has_named_reports_zeroes_rather_than_nothing(db, tagged):
    """The title the flywheel exists to find. A caller cannot tell an empty mapping from a
    vocabulary that failed to load, and this is the answer it most needs to be able to trust."""
    covered = await facet_coverage(db, 2)

    assert list(covered) == list(FACET_ORDER)
    assert set(covered.values()) == {0}


async def test_one_term_named_by_three_providers_is_one_term_of_coverage(db):
    """The measurement counts what the sources NAMED, and three providers agreeing on one term
    named one piece of the vocabulary once.

    `dna_tag`'s key is `(title_id, version, term, provider)`, which is section 6.6's parallel
    mode, while the bundle's imported tags carry one provider each. Counted as rows, a post-2025
    title three providers unanimously tagged with one mood reported `mood: 3` -- the same number
    as a bundle title carrying three distinct moods -- so §14 risk 2's "measure facet coverage of
    post-2025 titles" compared a population inflated threefold against one that was not, and
    ranked the unanimously thin title as the better covered of the two. [M5.4 review cycle 3,
    M54-DIM5-01]
    """
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
    """What the key order IS on an install, read through the loader that writes `dna_facet`.

    The fixture above seeds `ord` in §6.4's listing order so that a mapping following `ord` can be
    told from one following a sort -- and the loader writes `ord` from `sorted(facet_names)`
    (`importer/dna.py:154`), so on every install this app builds the two are the same order. No
    spec clause fixes an order over the eleven: §6.8 fixes a colour per facet and §6.4 lists axis
    examples, and the docstring that once cited §6.8 for a sequence cited a clause §6.8 does not
    contain. Pinned here, because the two tests above pass against a `dna_facet` state the shipped
    loader cannot produce. [M5.4 review cycle 3, M54-DIM5-02]
    """
    fx.make_bundle(tmp_path / "bundle")
    report = ImportReport()
    await importer_dna.load_vocabulary(
        db, tmp_path / "bundle" / "artifacts" / "dna_vocab" / VOCAB, VOCAB, report
    )
    assert report.ok, report.render()
    await _title(db, 1, "Heat")

    covered = await facet_coverage(db, 1)

    assert set(covered) == VOCAB_V1_FACETS
    assert list(covered) == sorted(VOCAB_V1_FACETS)


async def test_the_projected_tier_is_not_coverage(db, tagged):
    """§4.1 rule 1's two tiers are not two spellings of one thing. A projected row is inferred
    from the title's own keywords, so counting it would report coverage no extraction produced
    and would hide from §8.4 exactly the titles it is fed with.

    The measurement is taken twice with the projection written in between, because the claim is
    that the number does not move rather than that some number comes back.
    """
    before = await facet_coverage(db, 1)
    await _projected(db, 1, "pacing.languid")
    await _projected(db, 1, "mood.bleak")

    assert await facet_coverage(db, 1) == before


async def test_a_tag_under_a_superseded_vocabulary_is_invisible_and_addressable(db, tagged):
    """§14 risk 7: "every read is scoped to one version". A tag filed under the vocabulary a
    re-import superseded is not this title's coverage of the live one -- and it is not lost
    either, which is what the explicit `version` argument is for."""
    await _vocabulary(db, "v0", facets=("mood",))
    # Stamped older on purpose. `db/dna_terms.ACTIVE_VERSION` orders on `imported_at DESC` and
    # each of these statements commits on its own, so the vocabulary seeded second is the newest
    # one -- a superseded vocabulary written after the live one is the live one. Written the way
    # `test_library_read.py:154` writes it, for the same reason.
    await db.execute(
        "UPDATE dna_vocabulary SET imported_at = now() - interval '1 day' WHERE version = 'v0'"
    )
    await _tag(db, 2, "mood.cosy", version="v0")

    assert (await facet_coverage(db, 2))["mood"] == 0
    assert await facet_coverage(db, 2, version="v0") == {"mood": 1}


async def test_a_facet_label_the_vocabulary_does_not_declare_covers_nothing(db, tagged):
    """The shape `importer/dna.app_facet` and `0018_read_layer.sql:41-45` exist to repair: a row
    filed under the corpus's extraction label (`mood_tone`) rather than under §4.3's facet id,
    which once described 29,188 of 31,540 `dna_tag` rows [M4.9 finding 1].

    It counts for no facet, and that is the honest answer rather than a silent one: the keys of
    this mapping are the vocabulary's own, so a label the vocabulary does not declare covers none
    of them. Re-deriving the prefix here to rescue such a row would put the facet rule in a third
    place, which is how the first two came to disagree.
    """
    await _tag(db, 2, "mood_tone.wistful", facet="mood_tone")

    covered = await facet_coverage(db, 2)
    assert "mood_tone" not in covered
    assert set(covered.values()) == {0}


async def test_an_install_with_no_vocabulary_measures_nothing_and_raises_nothing(db):
    """§3.1: "a bundle-less app is a legal state". `db/dna_terms.active_version` answers None
    there, and a measurement that raised on it would turn first boot into an error report about
    a flywheel nobody has fed yet."""
    await _title(db, 1, "Heat")

    assert await facet_coverage(db, 1) == {}


# --- the number that must not be here ---------------------------------------------------


def _numeric_literals(source: str) -> list[str]:
    """Every number reaching a module's code, docstrings and SQL text excluded.

    The guard is "no number at all" rather than "no comparison against a number" because the
    threshold arrives in more shapes than one -- `if n >= 3`, `n / 3`, a `minimum: int = 2`
    default, a slice of the loudest few -- and each of them decides what "enough" means. The
    counts this module returns need no constant to compute, so the absence is cheap to keep and
    a single number is a genuine signal that decision 329 was taken by somebody's default.

    `ast.Constant` rather than a regex: the measurements the docstring cites (29,188 of 31,540
    rows, the corpus's `/ 3`) are prose about why the number is absent, and a reader that could
    not tell those from code would make the file unable to argue its own case. Booleans are
    excluded because `isinstance(True, int)` is true in Python and `return True` is not a
    threshold -- the name guard below is what covers that one.
    """
    return [
        f"line {node.lineno}: {node.value!r}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ]


def _declared_names(source: str) -> set[str]:
    """Every function, class and module-level name a source binds."""
    tree = ast.parse(source)
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_the_measurement_carries_no_threshold():
    """Decision 390: counts, no boolean, no `n`. Decision 329 -- what makes a title thin-facet
    -- is M5.6's to take with the queue that spends money on the answer, so a default chosen
    here would be a flywheel feed nobody approved."""
    offenders = _numeric_literals(MODULE.read_text(encoding="utf-8"))
    assert not offenders, (
        "decision 329 has not been taken: the thin-facet threshold is the owner's, and a "
        "number in this module is where it would arrive by accident.\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("a comprehension filter", "def f(c):\n    return {k: n for k, n in c if n >= 3}\n"),
        ("the corpus ratio", "def f(c):\n    return {k: min(1.0, n / 3) for k, n in c}\n"),
        ("a default argument", "async def f(conn, title_id, *, minimum=2):\n    return minimum\n"),
    ],
)
def test_the_threshold_guard_catches_a_real_violation(name, source):
    """A guard that cannot fail reads as coverage. Each of these is one line away from the
    shipped module, and the middle one is the corpus's own `coverage()` ported faithfully --
    which is exactly the port decision 390 declines."""
    assert _numeric_literals(source), f"the threshold guard would not catch: {name}"


def test_the_threshold_guard_reads_prose_as_prose():
    """The module argues its case with measured numbers in its docstrings, and a guard that
    could not tell those from code would force the argument out of the file."""
    assert not _numeric_literals('"""29,188 of 31,540 rows."""\ndef f(c):\n    return c\n')


def test_nothing_here_is_named_after_the_other_thinness_test():
    """`placement/features.py:90-109` owns `is_thin` for a per-BLOCK test -- at least one block
    dropped, or one that hit none of its declared columns. Decision 329 exists because the four
    "thin"s in the spec were being spoken about as one, and two functions of that name in one
    tree is how the conflation becomes permanent."""
    declared = _declared_names(MODULE.read_text(encoding="utf-8"))
    offenders = sorted(name for name in declared if "thin" in name.lower())
    assert not offenders, (
        "§8.4's per-facet coverage is not `features.BuiltVector.is_thin`, and decision 329 "
        f"names that conflation as the thing to prevent: {offenders}"
    )
