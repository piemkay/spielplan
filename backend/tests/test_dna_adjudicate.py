"""The curation ledger, read the way the trust boundary reads it. Spec v2.1 §8 stage 3, §14.5.

`dna_adjudication` has been written since M4.5 and read by nothing. These tests are the first
consumer, and they are integration tests over Postgres rather than unit tests over a parsed file
because the thing being asserted is a disagreement between two projects about what is stored in
one column: `0004_dna.sql:138` documents `keep | rename | drop | merge`, `mdc/dna/adjudication.py`
writes `REPOINT`/`DROP`/`DROP_EVIDENCE`, and `importer/dna.py:276` stores whatever the TSV's
`action` column said with no CHECK between them. A fixture that parsed strings in Python would
assert about this file's idea of the ledger; these rows go through the table.

**The rows are written here rather than taken from the fixture bundle.** The bundle ships exactly
two verdicts (`tests/fixtures/make_bundle.py:873-877`) and they conflict for this purpose: title 1
drops `mood.cosy` while the blanket rule renames `cozy` TO `mood.cosy`, so between them they
cannot show a per-title verdict beating a blanket one on the same term -- the case the whole
per-title arm exists for, and the case `0015_seed.sql:159-166` changed the primary key to make
representable. Every row below is written in the shape `load_adjudications` writes
(`importer/dna.py:318-323`), which is what keeps these tests about the app's ledger rather than
about a shape invented in a test file.

The verdict names are asserted in both projects' spellings and in both cases, because decision 389
turns on the claim that a reader knowing one vocabulary silently treats the other as "no verdict"
-- which is not a crash but a relabelling, a curated retirement arriving at §6.6 as
`unknown_term`, indistinguishable from an extractor emitting garbage.
"""

from __future__ import annotations

import pytest

from spielplan.dna import adjudicate

V1 = "v1"
V2 = "v2"


@pytest.fixture
async def ledger(db):
    """Two imported vocabularies, both with an empty ledger.

    Two rather than one because `dna_adjudication.version` is a foreign key to `dna_vocabulary`
    and §14 risk 7's "every read is scoped to one version" is only testable where a second version
    exists to leak from. A fresh `dna_vocabulary` row is the whole install for this module's
    purposes: the ledger carries no foreign key to `dna_term` or to `title`, so a verdict may name
    a term this vocabulary never carried and a title this install never acquired -- which is the
    point of `0015_seed.sql:157-158`'s note that a curated verdict "must outlive a title this
    install has not acquired".
    """
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, 0, 0), "
        "($2, 0, 0)",
        V1, V2,
    )
    return db


async def _verdict(
    db, term: str, action: str, *, target: str | None = None, title_id: int | None = None,
    version: str = V1, scope: str | None = None,
) -> None:
    """One ledger row, written the way the importer writes one.

    `scope` defaults exactly as `importer/dna.py:274` defaults it -- `title` when the row names a
    title and `global` otherwise -- so a test that does not care about the column gets the
    spelling a real import would have stored, and a test that does care passes it explicitly.
    """
    await db.execute(
        "INSERT INTO dna_adjudication (version, scope, title_id, term, verdict, target) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        version, scope or ("title" if title_id is not None else "global"), title_id, term,
        action, target,
    )


# --- the blanket rule and the per-title verdict -------------------------------------------


async def test_a_blanket_rename_re_points_the_term_for_every_title(ledger):
    """The ordinary case, and the one that makes a result file outlive a vocabulary merge.

    354 of the shipped ledger's 828 rows are re-points, 9 of them blanket, and without this the
    tag they describe is rejected as `unknown_term` at every ingest after the merge -- measured
    at 496 rows, twice (`mdc/dna/store.py:177-184`).
    """
    await _verdict(ledger, "themes.satire", "rename", target="mood.satirical")

    assert await adjudicate.rename(ledger, "themes.satire", 7, version=V1) == "mood.satirical"
    assert await adjudicate.rename(ledger, "themes.satire", version=V1) == "mood.satirical"
    assert await adjudicate.is_retired(ledger, "themes.satire", 7, version=V1) is False


async def test_a_per_title_verdict_beats_the_blanket_rule_for_its_term(ledger):
    """The case the fixture bundle cannot express and the primary key was changed to hold.

    Both rows are re-points of one term and they disagree about the target. The audit's own
    example is the shape: 29 `central_couple` rows kept while 92 were dropped, which is only
    writable if the row naming a title outranks the row naming none.
    """
    await _verdict(ledger, "themes.allegory", "rename", target="structure.allegory")
    await _verdict(ledger, "themes.allegory", "rename", target="mood.dreamlike", title_id=12)

    assert await adjudicate.rename(ledger, "themes.allegory", 12, version=V1) == "mood.dreamlike"
    # Every other title still gets the blanket answer: a per-title row excepts its own title and
    # says nothing about the rest, which is what makes the sweep a sweep.
    assert await adjudicate.rename(ledger, "themes.allegory", 13, version=V1) == "structure.allegory"


async def test_a_per_title_drop_answers_no_rename_even_under_a_blanket_rename(ledger):
    """A term the ledger drops is genuinely gone, and returning the blanket target would keep it.

    This is the corpus's own ordering in `rename` (`mdc/dna/adjudication.py:225-234`): the
    per-title arm treats a drop as terminal rather than falling through to the sweep. Both
    functions are asserted together because the pair is the signal -- `rename` says there is
    nowhere to put this tag and `is_retired` says why, and the verifier writes `adjudicated`
    rather than `unknown_term` on the strength of the second.
    """
    await _verdict(ledger, "mood.cerebral", "rename", target="mood.cold")
    await _verdict(ledger, "mood.cerebral", "drop", title_id=9)

    assert await adjudicate.rename(ledger, "mood.cerebral", 9, version=V1) is None
    assert await adjudicate.is_retired(ledger, "mood.cerebral", 9, version=V1) is True
    # And nothing has happened to any other title.
    assert await adjudicate.rename(ledger, "mood.cerebral", 10, version=V1) == "mood.cold"
    assert await adjudicate.is_retired(ledger, "mood.cerebral", 10, version=V1) is False


async def test_a_blanket_drop_retires_the_term_for_every_title(ledger):
    """2 of the shipped ledger's 11 blanket rows are retirements ("term retired by the audit")."""
    await _verdict(ledger, "sensibility.midnight_movie", "drop")

    assert await adjudicate.is_retired(ledger, "sensibility.midnight_movie", 4, version=V1) is True
    assert await adjudicate.is_retired(ledger, "sensibility.midnight_movie", version=V1) is True
    assert await adjudicate.rename(ledger, "sensibility.midnight_movie", 4, version=V1) is None


async def test_a_per_title_keep_excepts_its_title_from_a_blanket_retirement(ledger):
    """`keep` is in the app's vocabulary and in no shipped row, so its meaning is decided here.

    It changes nothing by itself -- it re-points nothing and retires nothing -- and it is still a
    verdict about the term, so it ends the search on its title. The alternative reading, that a
    `keep` is simply skipped, makes the row unwritable: the only reason to file one is to except a
    title from a sweep, and a skipped `keep` would let the sweep apply anyway.
    """
    await _verdict(ledger, "characters.central_couple", "drop")
    await _verdict(ledger, "characters.central_couple", "keep", title_id=29)

    assert await adjudicate.is_retired(ledger, "characters.central_couple", 29, version=V1) is False
    assert await adjudicate.is_retired(ledger, "characters.central_couple", 92, version=V1) is True


async def test_a_per_title_keep_excepts_its_title_from_a_blanket_re_point(ledger):
    """The same exception filed against the other blanket verdict, and the half that was missing.

    `is_retired` let the first per-title verdict about the term end the search and `rename` did
    not: its per-title loop knew only re-points and retirements, so a `keep` fell through to the
    sweep and the excepted title was tagged with exactly the blanket target the exception was
    filed to prevent -- with no `dna_reject` row, a write decision 162 makes permanent, and
    `is_retired` answering False for the same row so nothing downstream could tell. The audit's
    own example, 29 `central_couple` rows kept while 92 moved, was not writable through `rename`.
    [M5.4 review cycle 3, M54-DIM3-C3-03]
    """
    await _verdict(ledger, "characters.central_couple", "rename", target="characters.duo")
    await _verdict(ledger, "characters.central_couple", "keep", title_id=29)

    assert await adjudicate.rename(ledger, "characters.central_couple", 29, version=V1) is None
    assert await adjudicate.rename(ledger, "characters.central_couple", 92, version=V1) == (
        "characters.duo"
    )
    assert await adjudicate.is_retired(ledger, "characters.central_couple", 29, version=V1) is False


async def test_both_functions_agree_about_which_per_title_row_is_that_titles_verdict(ledger):
    """The first per-title row about the TERM is the title's verdict, for `rename` as for
    `is_retired` -- two readers of one ledger that disagree about which row speaks for a title
    answer two different questions under one name. A `keep` filed before a re-point is that
    title's verdict, and so is a re-point that names no target: it is a verdict about the term
    that re-points nothing, so it stops the sweep and hands the tag to the vocabulary check.
    [M5.4 review cycle 3, M54-DIM3-C3-03]
    """
    await _verdict(ledger, "themes.war", "keep", title_id=3)
    await _verdict(ledger, "themes.war", "rename", target="themes.ww", title_id=3)
    await _verdict(ledger, "characters.central_couple", "rename", target="characters.duo")
    await _verdict(ledger, "characters.central_couple", "repoint", target="", title_id=31)

    assert await adjudicate.rename(ledger, "themes.war", 3, version=V1) is None
    assert await adjudicate.is_retired(ledger, "themes.war", 3, version=V1) is False
    assert await adjudicate.rename(ledger, "characters.central_couple", 31, version=V1) is None
    assert await adjudicate.is_retired(ledger, "characters.central_couple", 31, version=V1) is False


async def test_a_verdict_for_another_title_does_not_reach_this_one(ledger):
    """817 of the 828 rows name a title, so most of the ledger must be invisible to most reads."""
    await _verdict(ledger, "themes.trauma", "drop", title_id=21)

    assert await adjudicate.is_retired(ledger, "themes.trauma", 22, version=V1) is False
    assert await adjudicate.rename(ledger, "themes.trauma", 22, version=V1) is None
    assert await adjudicate.is_retired(ledger, "themes.trauma", 21, version=V1) is True


# --- both projects' vocabularies, both cases ----------------------------------------------


@pytest.mark.parametrize("action", ["rename", "REPOINT", "Repoint", "repoint", "RENAME", "merge"])
async def test_every_spelling_of_a_re_point_re_points(ledger, action):
    """Decision 389: `rename` is `0004_dna.sql:138`'s word, `repoint` is the corpus's, and the
    ledger that ships carries 354 rows of the second spelling in upper case.

    `merge` is here rather than in a test of its own because to a trust boundary it says the same
    thing -- this id is now that id. What a merge adds is the writer's job: keeping the higher
    salience and unioning the evidence when the target is already on the title
    (`mdc/dna/adjudication._merge_into:103-121`), which needs the title's other tags and is not a
    question about whether one term is admissible.
    """
    await _verdict(ledger, "themes.road_trip", action, target="structure.road_movie")

    assert await adjudicate.rename(ledger, "themes.road_trip", 3, version=V1) == "structure.road_movie"
    assert await adjudicate.is_retired(ledger, "themes.road_trip", 3, version=V1) is False


@pytest.mark.parametrize("action", ["drop", "DROP", "Drop", " drop "])
async def test_every_spelling_of_a_retirement_retires(ledger, action):
    """The shipped file writes `DROP` 467 times; the fixture bundle writes `drop`. A reader that
    knew only one of them would report the other as no verdict at all, which is the relabelling
    decision 389 exists to prevent. The padded spelling is here because the importer strips the
    field (`importer/dna.py:276`) and a hand-edited ledger reaching the table another way should
    not turn a retirement into an unknown string on a space.
    """
    await _verdict(ledger, "sensibility.crowd_energy", action)

    assert await adjudicate.is_retired(ledger, "sensibility.crowd_energy", 5, version=V1) is True
    assert await adjudicate.rename(ledger, "sensibility.crowd_energy", 5, version=V1) is None


async def test_a_drop_evidence_row_never_answers_for_the_term(ledger):
    """`DROP_EVIDENCE` says one quote was false, not that the term was wrong -- 7 shipped rows.

    The named change decision 389 makes to the port. The corpus's per-title arm lets the first row
    for a term decide whatever its action is, so a `DROP_EVIDENCE` row would answer False and stop,
    overruling a blanket retirement and sending it to §6.6 as `unknown_term` -- the confusion
    `is_retired` exists to prevent. The shipped ledger cannot tell the two readings apart (none of
    its 7 evidence rows names a blanket-ruled term), so this is the only place the change is
    visible at all.
    """
    await _verdict(ledger, "themes.time_travel", "drop")
    await _verdict(ledger, "themes.time_travel", "DROP_EVIDENCE", title_id=5647)

    assert await adjudicate.is_retired(ledger, "themes.time_travel", 5647, version=V1) is True
    # And on a title whose only verdict is the evidence note, the term is untouched.
    await _verdict(ledger, "mood.bleak", "DROP_EVIDENCE", title_id=5647)
    assert await adjudicate.is_retired(ledger, "mood.bleak", 5647, version=V1) is False
    assert await adjudicate.rename(ledger, "mood.bleak", 5647, version=V1) is None


async def test_an_unknown_verdict_is_ignored_rather_than_guessed_at(ledger):
    """A fifth verdict changes no tag until somebody teaches this module what it means.

    Guessing is the tempting failure -- `retire` looks like `drop` and `alias` looks like a
    re-point -- and a guess here is a curated verdict applied in a direction nobody authored.
    Ignoring it leaves the tag to the vocabulary check, which is where an id nobody recognises
    already belongs.
    """
    await _verdict(ledger, "mood.wholesome", "retire")
    await _verdict(ledger, "themes.parody", "alias", target="structure.parody", title_id=8)

    assert await adjudicate.is_retired(ledger, "mood.wholesome", 8, version=V1) is False
    assert await adjudicate.rename(ledger, "mood.wholesome", 8, version=V1) is None
    assert await adjudicate.rename(ledger, "themes.parody", 8, version=V1) is None


async def test_an_unknown_per_title_verdict_does_not_shadow_the_blanket_rule(ledger):
    """The other half of "ignored": an unknown string may not answer for its title either.

    Letting it stop the search would be acting on it -- the row would change the answer while
    nobody could say what it meant -- and it would do so in the direction that hides a curated
    retirement behind a typo.
    """
    await _verdict(ledger, "themes.tragedy", "drop")
    await _verdict(ledger, "themes.tragedy", "revisit", title_id=14)

    assert await adjudicate.is_retired(ledger, "themes.tragedy", 14, version=V1) is True


async def test_unknown_verdicts_counts_the_spellings_nobody_taught_this_reader(ledger):
    """Decision 389's other half: ignored AND counted, so an inert ledger is visible as one.

    Keyed on the stored spelling rather than on the folded key, because the number exists to send
    a reader to the rows. The known vocabulary is absent from the count in both projects'
    spellings and in both cases, which is what distinguishes "nobody has taught this reader" from
    "this reader only knows the app's half".
    """
    await _verdict(ledger, "a.one", "DROP")
    await _verdict(ledger, "a.two", "rename", target="a.three")
    await _verdict(ledger, "a.four", "DROP_EVIDENCE")
    await _verdict(ledger, "a.five", "keep")
    await _verdict(ledger, "a.six", "retire")
    await _verdict(ledger, "a.seven", "retire")
    await _verdict(ledger, "a.eight", "Alias")
    await _verdict(ledger, "a.nine", "drop", version=V2)

    assert await adjudicate.unknown_verdicts(ledger, version=V1) == {"retire": 2, "Alias": 1}
    assert await adjudicate.unknown_verdicts(ledger, version=V2) == {}


# --- the states that must not raise --------------------------------------------------------


async def test_an_empty_ledger_answers_rather_than_raising(ledger):
    """An install with no curated verdicts is a legitimate state, not a missing file.

    The corpus says so of its absent TSV ("a fresh vocabulary has no verdicts yet",
    `mdc/dna/adjudication.load_ledger:73-76`) and it is truer here: decision 247's loader leaves
    the table empty rather than failing when a bundle carries no ledger.
    """
    assert await adjudicate.rename(ledger, "mood.cosy", 1, version=V1) is None
    assert await adjudicate.is_retired(ledger, "mood.cosy", 1, version=V1) is False
    assert await adjudicate.unknown_verdicts(ledger, version=V1) == {}


async def test_no_active_vocabulary_is_the_pre_import_state_and_not_an_error(db):
    """`db/dna_terms.active_version` answers None on an install that has imported no bundle, and
    every caller here passes what it resolved. None must therefore mean "no ledger to consult"
    rather than a crash or, worse, a read across every version -- the shape `db/library.dna_for`
    already uses for the same argument (`db/library.py:472-481`). Takes the bare `db` and not the
    `ledger` fixture, because the state being asserted is the one with no `dna_vocabulary` row.
    """
    assert await adjudicate.rename(db, "mood.cosy", 1, version=None) is None
    assert await adjudicate.is_retired(db, "mood.cosy", 1, version=None) is False
    assert await adjudicate.unknown_verdicts(db, version=None) == {}


async def test_a_re_point_that_names_no_target_is_not_a_re_point(ledger):
    """`target` is nullable (`0004_dna.sql:139`) and the TSV's column is often empty -- every
    `DROP` row in the shipped ledger leaves it blank. A re-point without one would otherwise
    return the empty string, which the vocabulary check would then reject as an unknown term
    while `is_retired` said False: a verdict rendered as garbage from the extractor.
    """
    await _verdict(ledger, "themes.magic_realism", "rename", target="")
    await _verdict(ledger, "characters.ensemble_cast", "repoint", target="   ", title_id=6)

    assert await adjudicate.rename(ledger, "themes.magic_realism", 6, version=V1) is None
    assert await adjudicate.rename(ledger, "characters.ensemble_cast", 6, version=V1) is None


# --- version scoping and the scope column ---------------------------------------------------


async def test_a_verdict_filed_under_another_vocabulary_is_invisible(ledger):
    """§14 risk 7 (`spec:503`): "every read is scoped to one version".

    §10 warns that a re-import leaves two vocabularies coexisting, and a verdict authored against
    one of them is a statement about that vocabulary's terms. Reading across both would apply a
    retirement the household never made on the vocabulary it is actually running.
    """
    await _verdict(ledger, "mood.cosy", "drop", version=V2)
    await _verdict(ledger, "cozy", "rename", target="mood.cosy", version=V2)

    assert await adjudicate.is_retired(ledger, "mood.cosy", 1, version=V1) is False
    assert await adjudicate.rename(ledger, "cozy", 1, version=V1) is None
    assert await adjudicate.is_retired(ledger, "mood.cosy", 1, version=V2) is True
    assert await adjudicate.rename(ledger, "cozy", 1, version=V2) == "mood.cosy"


async def test_the_corpus_spelling_of_the_blanket_scope_reads_as_blanket(ledger):
    """All 11 blanket rows in the shipped ledger say `term`; the app's loader writes `global`.

    Applicability is read off `title_id`, which both projects agree about, and not off `scope`,
    which they spell three ways between them. A reader keyed on the scope string would treat the
    entire shipped blanket arm as unrecognised and drop 11 curated rules without saying so.
    """
    await _verdict(ledger, "themes.life_affirming", "REPOINT", target="mood.feel_good",
                   scope="term")

    assert await adjudicate.rename(ledger, "themes.life_affirming", 2, version=V1) == "mood.feel_good"


async def test_a_row_carrying_a_title_id_speaks_for_that_title_whatever_its_scope_says(ledger):
    """`scope` defaults to `global` (`0015_seed.sql:161`) and the loader only refuses the opposite
    mismatch -- a `title` row with no id (`importer/dna.py:282-287`) -- so a `global` row carrying
    a title id is storable and has to mean something. It means what the corpus's own classifier
    means by it: a rule with a title id is that title's (`mdc/dna/adjudication.py:96-99`).
    """
    await _verdict(ledger, "structure.dream_logic", "drop", title_id=9, scope="global")

    assert await adjudicate.is_retired(ledger, "structure.dream_logic", 9, version=V1) is True
    assert await adjudicate.is_retired(ledger, "structure.dream_logic", 10, version=V1) is False


async def test_the_per_title_arm_is_read_before_the_blanket_arm_in_one_query(ledger):
    """The ordering is the rule, so it is asserted rather than left to the row order the planner
    happens to produce. Six rows for one term, the per-title one written LAST: an unordered read
    returns the blanket target roughly as often as not, which is the kind of defect that passes a
    suite for a year and then reverts a curated verdict on a Tuesday.
    """
    for i in range(5):
        await _verdict(ledger, "themes.trauma", "rename", target=f"mood.blanket_{i}")
    await _verdict(ledger, "themes.trauma", "rename", target="mood.per_title", title_id=21)

    assert await adjudicate.rename(ledger, "themes.trauma", 21, version=V1) == "mood.per_title"
    assert await adjudicate.rename(ledger, "themes.trauma", 22, version=V1) == "mood.blanket_0"
