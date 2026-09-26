"""The curation ledger, read the way the trust boundary reads it (§8 stage 3, decision 389).
Rows are written here: the bundle's two verdicts cannot show a per-title verdict beating a blanket one.
Needs TEST_DATABASE_URL."""

from __future__ import annotations

import pytest

from spielplan.dna import adjudicate

V1 = "v1"
V2 = "v2"


@pytest.fixture
async def ledger(db):
    """Two vocabularies, because §14 risk 7's version scoping is only testable where a second one exists."""
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
    """`scope` defaults exactly as `importer/dna.py` defaults it."""
    await db.execute(
        "INSERT INTO dna_adjudication (version, scope, title_id, term, verdict, target) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        version, scope or ("title" if title_id is not None else "global"), title_id, term,
        action, target,
    )


async def test_a_blanket_rename_re_points_the_term_for_every_title(ledger):
    await _verdict(ledger, "themes.satire", "rename", target="mood.satirical")

    assert await adjudicate.rename(ledger, "themes.satire", 7, version=V1) == "mood.satirical"
    assert await adjudicate.rename(ledger, "themes.satire", version=V1) == "mood.satirical"
    assert await adjudicate.is_retired(ledger, "themes.satire", 7, version=V1) is False


async def test_a_per_title_verdict_beats_the_blanket_rule_for_its_term(ledger):
    await _verdict(ledger, "themes.allegory", "rename", target="structure.allegory")
    await _verdict(ledger, "themes.allegory", "rename", target="mood.dreamlike", title_id=12)

    assert await adjudicate.rename(ledger, "themes.allegory", 12, version=V1) == "mood.dreamlike"
    # A per-title row excepts its own title and says nothing about the rest.
    assert await adjudicate.rename(ledger, "themes.allegory", 13, version=V1) == "structure.allegory"


async def test_a_per_title_drop_answers_no_rename_even_under_a_blanket_rename(ledger):
    """The per-title arm treats a drop as terminal rather than falling through to the sweep."""
    await _verdict(ledger, "mood.cerebral", "rename", target="mood.cold")
    await _verdict(ledger, "mood.cerebral", "drop", title_id=9)

    assert await adjudicate.rename(ledger, "mood.cerebral", 9, version=V1) is None
    assert await adjudicate.is_retired(ledger, "mood.cerebral", 9, version=V1) is True
    # And nothing has happened to any other title.
    assert await adjudicate.rename(ledger, "mood.cerebral", 10, version=V1) == "mood.cold"
    assert await adjudicate.is_retired(ledger, "mood.cerebral", 10, version=V1) is False


async def test_a_blanket_drop_retires_the_term_for_every_title(ledger):
    await _verdict(ledger, "sensibility.midnight_movie", "drop")

    assert await adjudicate.is_retired(ledger, "sensibility.midnight_movie", 4, version=V1) is True
    assert await adjudicate.is_retired(ledger, "sensibility.midnight_movie", version=V1) is True
    assert await adjudicate.rename(ledger, "sensibility.midnight_movie", 4, version=V1) is None


async def test_a_per_title_keep_excepts_its_title_from_a_blanket_retirement(ledger):
    """`keep` re-points and retires nothing, but ends the search
    on its title; skipping it would let the sweep apply."""
    await _verdict(ledger, "characters.central_couple", "drop")
    await _verdict(ledger, "characters.central_couple", "keep", title_id=29)

    assert await adjudicate.is_retired(ledger, "characters.central_couple", 29, version=V1) is False
    assert await adjudicate.is_retired(ledger, "characters.central_couple", 92, version=V1) is True


async def test_a_per_title_keep_excepts_its_title_from_a_blanket_re_point(ledger):
    """A `keep` used to fall through `rename` to the sweep,
    tagging the excepted title with the blanket target."""
    await _verdict(ledger, "characters.central_couple", "rename", target="characters.duo")
    await _verdict(ledger, "characters.central_couple", "keep", title_id=29)

    assert await adjudicate.rename(ledger, "characters.central_couple", 29, version=V1) is None
    assert await adjudicate.rename(ledger, "characters.central_couple", 92, version=V1) == (
        "characters.duo"
    )
    assert await adjudicate.is_retired(ledger, "characters.central_couple", 29, version=V1) is False


async def test_both_functions_agree_about_which_per_title_row_is_that_titles_verdict(ledger):
    """The first per-title row about the TERM is the title's verdict, for `rename` as for `is_retired`."""
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


@pytest.mark.parametrize("action", ["rename", "REPOINT", "Repoint", "repoint", "RENAME", "merge"])
async def test_every_spelling_of_a_re_point_re_points(ledger, action):
    """`merge` is a re-point to a trust boundary; merging salience and evidence is the writer's job."""
    await _verdict(ledger, "themes.road_trip", action, target="structure.road_movie")

    assert await adjudicate.rename(ledger, "themes.road_trip", 3, version=V1) == "structure.road_movie"
    assert await adjudicate.is_retired(ledger, "themes.road_trip", 3, version=V1) is False


@pytest.mark.parametrize("action", ["drop", "DROP", "Drop", " drop "])
async def test_every_spelling_of_a_retirement_retires(ledger, action):
    """The padded spelling: the importer strips the field, but a hand-edited ledger may not."""
    await _verdict(ledger, "sensibility.crowd_energy", action)

    assert await adjudicate.is_retired(ledger, "sensibility.crowd_energy", 5, version=V1) is True
    assert await adjudicate.rename(ledger, "sensibility.crowd_energy", 5, version=V1) is None


async def test_a_drop_evidence_row_never_answers_for_the_term(ledger):
    """`DROP_EVIDENCE` says one quote was false, not that
    the term was wrong, so it never answers for the term."""
    await _verdict(ledger, "themes.time_travel", "drop")
    await _verdict(ledger, "themes.time_travel", "DROP_EVIDENCE", title_id=5647)

    assert await adjudicate.is_retired(ledger, "themes.time_travel", 5647, version=V1) is True
    # And on a title whose only verdict is the evidence note, the term is untouched.
    await _verdict(ledger, "mood.bleak", "DROP_EVIDENCE", title_id=5647)
    assert await adjudicate.is_retired(ledger, "mood.bleak", 5647, version=V1) is False
    assert await adjudicate.rename(ledger, "mood.bleak", 5647, version=V1) is None


async def test_an_unknown_verdict_is_ignored_rather_than_guessed_at(ledger):
    """A guess would apply a curated verdict in a direction nobody authored."""
    await _verdict(ledger, "mood.wholesome", "retire")
    await _verdict(ledger, "themes.parody", "alias", target="structure.parody", title_id=8)

    assert await adjudicate.is_retired(ledger, "mood.wholesome", 8, version=V1) is False
    assert await adjudicate.rename(ledger, "mood.wholesome", 8, version=V1) is None
    assert await adjudicate.rename(ledger, "themes.parody", 8, version=V1) is None


async def test_an_unknown_per_title_verdict_does_not_shadow_the_blanket_rule(ledger):
    """Letting an unknown string stop the search would hide a curated retirement behind a typo."""
    await _verdict(ledger, "themes.tragedy", "drop")
    await _verdict(ledger, "themes.tragedy", "revisit", title_id=14)

    assert await adjudicate.is_retired(ledger, "themes.tragedy", 14, version=V1) is True


async def test_unknown_verdicts_counts_the_spellings_nobody_taught_this_reader(ledger):
    """Keyed on the stored spelling, because the number exists to send a reader to the rows."""
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


async def test_an_empty_ledger_answers_rather_than_raising(ledger):
    assert await adjudicate.rename(ledger, "mood.cosy", 1, version=V1) is None
    assert await adjudicate.is_retired(ledger, "mood.cosy", 1, version=V1) is False
    assert await adjudicate.unknown_verdicts(ledger, version=V1) == {}


async def test_no_active_vocabulary_is_the_pre_import_state_and_not_an_error(db):
    """None means "no ledger to consult", never a read across every version."""
    assert await adjudicate.rename(db, "mood.cosy", 1, version=None) is None
    assert await adjudicate.is_retired(db, "mood.cosy", 1, version=None) is False
    assert await adjudicate.unknown_verdicts(db, version=None) == {}


async def test_a_re_point_that_names_no_target_is_not_a_re_point(ledger):
    """Every shipped `DROP` leaves `target` blank; a re-point without one would return ""."""
    await _verdict(ledger, "themes.magic_realism", "rename", target="")
    await _verdict(ledger, "characters.ensemble_cast", "repoint", target="   ", title_id=6)

    assert await adjudicate.rename(ledger, "themes.magic_realism", 6, version=V1) is None
    assert await adjudicate.rename(ledger, "characters.ensemble_cast", 6, version=V1) is None


async def test_a_verdict_filed_under_another_vocabulary_is_invisible(ledger):
    """A re-import leaves two vocabularies coexisting; a verdict belongs to one of them."""
    await _verdict(ledger, "mood.cosy", "drop", version=V2)
    await _verdict(ledger, "cozy", "rename", target="mood.cosy", version=V2)

    assert await adjudicate.is_retired(ledger, "mood.cosy", 1, version=V1) is False
    assert await adjudicate.rename(ledger, "cozy", 1, version=V1) is None
    assert await adjudicate.is_retired(ledger, "mood.cosy", 1, version=V2) is True
    assert await adjudicate.rename(ledger, "cozy", 1, version=V2) == "mood.cosy"


async def test_the_corpus_spelling_of_the_blanket_scope_reads_as_blanket(ledger):
    """Applicability is read off `title_id`, not `scope`, which the two projects spell three ways."""
    await _verdict(ledger, "themes.life_affirming", "REPOINT", target="mood.feel_good",
                   scope="term")

    assert await adjudicate.rename(ledger, "themes.life_affirming", 2, version=V1) == "mood.feel_good"


async def test_a_row_carrying_a_title_id_speaks_for_that_title_whatever_its_scope_says(ledger):
    """A rule with a title id is that title's, as the corpus's own classifier reads it."""
    await _verdict(ledger, "structure.dream_logic", "drop", title_id=9, scope="global")

    assert await adjudicate.is_retired(ledger, "structure.dream_logic", 9, version=V1) is True
    assert await adjudicate.is_retired(ledger, "structure.dream_logic", 10, version=V1) is False


async def test_the_per_title_arm_is_read_before_the_blanket_arm_in_one_query(ledger):
    """The per-title row is written LAST, so an unordered
    read returns the blanket target about half the time."""
    for i in range(5):
        await _verdict(ledger, "themes.trauma", "rename", target=f"mood.blanket_{i}")
    await _verdict(ledger, "themes.trauma", "rename", target="mood.per_title", title_id=21)

    assert await adjudicate.rename(ledger, "themes.trauma", 21, version=V1) == "mood.per_title"
    assert await adjudicate.rename(ledger, "themes.trauma", 22, version=V1) == "mood.blanket_0"
