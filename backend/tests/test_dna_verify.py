"""The trust boundary against a pack this file constructs (§8 stage 7, §9): each rule under a payload
built to break exactly it. `test_dna_reject.py` is the real-pack half. No database."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from spielplan.dna import verify
from spielplan.dna.aliases import alias_key

MODULE = Path(verify.__file__)
SOURCE = MODULE.read_text(encoding="utf-8")
MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0027_dna_extraction.sql"

# One body carried by exactly one facet, and one (`neon`) carried by two, which the repair must refuse.
TERMS = {
    "mood.bleak": "mood",
    "pacing.slow_burn": "pacing",
    "place.city": "place",
    "themes.robots": "themes",
    "visual.neon": "visual",
    "sound.neon": "sound",
}
FACETS = ("mood", "pacing", "place", "themes", "visual", "sound")

# The second row pins the ORDER: the prefix repair would resolve `odd.slow_burn` to a different term.
ALIASES = {
    alias_key("slow burn"): ("pacing", "pacing.slow_burn"),
    alias_key("odd.slow_burn"): ("mood", "mood.bleak"),
}

VOC = verify.Vocabulary.build("v1", TERMS, FACETS, ALIASES)

# The markup its sources published, NOT tidied: tidying at build time fails silently.
PACK = (
    "# Heat (1995)\n"
    "[type] film\n"
    "\n"
    "[plot:1]\n"
    "A crew works the city at night.\n"
    "\n"
    "[trakt:1]\n"
    "He said it was a **slow burn** of a [spoiler]film[/spoiler] and the city's own rhythm.\n"
)

# Markers gone and the apostrophe curled; an escape keeps a cp1252 console away from the codepoint.
TRANSCRIBED = "a slow burn of a film and the city\u2019s own rhythm"

# One word different: the difference the substring test catches.
FABRICATED = "a slow burn of a novel and the city\u2019s own rhythm"

PLAIN = "A crew works the city at night."
TITLE = 7


def tag(term: str = "mood.bleak", quote: str = PLAIN, **extra: object) -> dict[str, object]:
    row: dict[str, object] = {"term": term, "quote": quote}
    row.update(extra)
    return row


async def run(*tags: object, packs: dict[int, str | None] | None = None, **kw: object):
    return await verify.verify_payload(
        {"titles": {str(TITLE): list(tags)}},
        pass_id="p1",
        voc=VOC,
        packs={TITLE: PACK} if packs is None else packs,
        **kw,
    )


def reasons(result) -> list[str]:
    return [r.reason for r in result.rejects]


def terms(result) -> list[str]:
    return [t.term for t in result.tags[TITLE]]


async def test_a_fabricated_term_is_absent_from_the_output_and_recorded():
    """Schema-valid in every other way: well-formed JSON says nothing about the term."""
    result = await run(tag(term="themes.timetravel"))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["unknown_term"]
    assert result.rejects[0].term == "themes.timetravel"
    assert result.rejects[0].quote == PLAIN, (
        "a refusal a person cannot see the quote for is a refusal nobody can review (decision 341)"
    )


async def test_a_term_the_vocabulary_carries_passes_untouched():
    result = await run(tag(term="place.city"))

    assert terms(result) == ["place.city"]
    assert result.tags[TITLE][0].facet == "place"
    assert result.tags[TITLE][0].repaired is False


async def test_the_facet_comes_from_the_vocabulary_and_not_from_the_payload():
    """Filed under the payload's facet, 29,188 of 31,540 `dna_tag` rows named an undeclared facet."""
    result = await run(tag(term="sound.neon", facet="visual"))

    assert result.tags[TITLE][0].facet == "sound"


@pytest.mark.parametrize(
    ("offered", "expected"),
    [
        ("plot_structure.slow_burn", "pacing.slow_burn"),
        ("mood_tone.bleak", "mood.bleak"),
        ("narrative_themes.robots", "themes.robots"),
        # A head spelled with the other separator is not a declared facet either.
        ("mood-tone.bleak", "mood.bleak"),
        # Surrounding whitespace is stripped before anything else, exactly as the corpus strips it.
        ("  plot_structure.slow_burn  ", "pacing.slow_burn"),
        # A head that case-folds to the carrying facet claims nothing else, so it is still recalled.
        ("Mood.bleak", "mood.bleak"),
    ],
)
async def test_the_prefix_repair_recalls_a_term_the_extractor_meant(offered, expected):
    """Without this, Haiku 4.5 produced 59% invalid ids and almost every one was this mistake."""
    result = await run(tag(term=offered))

    assert terms(result) == [expected]
    assert result.tags[TITLE][0].repaired is True


@pytest.mark.parametrize(
    ("offered", "reason"),
    [
        # Two facets carry the body `neon`, so choosing one would be choosing a meaning.
        ("whatever.neon", "unknown_term"),
        # `mood` IS a declared facet: moving the tag to `pacing`
        # would change its meaning, not recall a spelling.
        ("mood.slow_burn", "unknown_term"),
        # The same claim in another case, which is why the head guard case-folds.
        ("Mood.slow_burn", "unknown_term"),
        ("MOOD.slow_burn", "unknown_term"),
        ("Themes.slow_burn", "unknown_term"),
        ("mood .slow_burn", "unknown_term"),
        ("Mood.robots", "unknown_term"),
        ("mood.neon", "unknown_term"),
        ("mood.nonexistent", "unknown_term"),
        # Nothing to take a prefix off.
        ("slowburn", "unknown_term"),
        # `_first` reads "" as unfilled, so this is a missing term, a different rejection.
        ("", "schema"),
    ],
)
async def test_the_prefix_repair_refuses_rather_than_guessing(offered, reason):
    result = await run(tag(term=offered))

    assert result.tags[TITLE] == []
    assert reasons(result) == [reason]


def test_the_prefix_repair_never_changes_the_term_body():
    """Everything `repair` returns is a vocabulary key with the body it was given, or None."""
    offered = [
        "plot_structure.slow_burn", "mood_tone.bleak", "narrative_themes.robots",
        "mood-tone.bleak", "whatever.neon", "mood.neon", "mood.slow_burn", "slowburn", "",
        "a.b.c", "character_dynamics.city", "PLACE.city",
    ]
    for term_id in offered:
        repaired = VOC.repair(term_id)
        if repaired is None:
            continue
        assert repaired in TERMS, f"{term_id!r} was repaired to a term nobody carries"
        assert repaired.partition(".")[2] == term_id.strip().partition(".")[2], (
            f"{term_id!r} -> {repaired!r} rewrote the term body, which is how a repair invents "
            "a tag the extractor never emitted"
        )


async def test_an_authored_alias_row_repairs_a_bare_phrase():
    """§8 stage 7's "after alias repair", and the first thing to ever read `dna_alias`."""
    result = await run(tag(term="Slow-Burn"))

    assert terms(result) == ["pacing.slow_burn"]
    assert result.tags[TITLE][0].repaired is True


async def test_an_authored_alias_beats_the_derived_prefix_repair():
    """A map row is a decision; the prefix repair is a mechanical guess. Both can answer this spelling."""
    assert VOC.repair("odd.slow_burn") == "pacing.slow_burn"

    result = await run(tag(term="odd.slow_burn"))

    assert terms(result) == ["mood.bleak"]


async def test_the_ledger_is_not_invented_when_there_is_none_to_read():
    """`ledger=None` drops under the label that claims less and never keeps a tag."""
    result = await run(tag(term="themes.timetravel"), ledger=None)

    assert reasons(result) == ["unknown_term"]
    assert result.rejects[0].detail == "not in vocabulary"


async def test_a_quote_that_is_not_in_the_pack_is_refused():
    """Exit check 2, and the check §8 credits with the pilot's 100% catch rate on its own."""
    result = await run(tag(quote=FABRICATED))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["quote_unverified"]
    assert result.rejects[0].quote == FABRICATED
    assert result.rejects[0].facet == "mood", (
        "the facet a refused term would have had is what section 6.6's screen groups by"
    )


async def test_a_quote_transcribed_across_markup_and_a_smart_apostrophe_passes():
    """Folding at comparison time makes the markup-free transcription the same span as the pack's."""
    result = await run(tag(quote=TRANSCRIBED))

    assert terms(result) == ["mood.bleak"]
    assert result.tags[TITLE][0].quote == TRANSCRIBED, (
        "the quote stored is the one the extractor produced; the fold is a comparison and not a "
        "rewrite of the evidence"
    )


async def test_the_pack_this_title_has_is_the_pack_its_quotes_are_checked_against():
    """A quote from another title's pack is what an extractor confusing two rows produces."""
    other = "# Other (2001)\n[trakt:1]\nA wholly unrelated sentence about a different film.\n"
    result = await verify.verify_payload(
        {"titles": {str(TITLE): [tag(quote="a wholly unrelated sentence")]}},
        pass_id="p1", voc=VOC, packs={TITLE: PACK, 99: other},
    )

    assert reasons(result) == ["quote_unverified"]


async def test_an_authored_alias_pointing_outside_the_vocabulary_drops_the_tag():
    """`dna_alias` has no FK to `dna_term`, so the type checks its own invariant and the tag drops."""
    voc = verify.Vocabulary.build(
        "v1", TERMS, FACETS, {alias_key("asimov"): ("themes", "themes.asimov_robots")},
    )
    result = await verify.verify_payload(
        {"titles": {str(TITLE): [tag(term="asimov")]}},
        pass_id="p1", voc=voc, packs={TITLE: PACK},
    )

    assert result.tags[TITLE] == []
    assert reasons(result) == ["unknown_term"]


async def test_an_authored_alias_pointing_nowhere_does_not_suppress_the_prefix_repair():
    """An alias pointing at nothing did not answer, so the prefix repair still runs."""
    voc = verify.Vocabulary.build(
        "v1", TERMS, FACETS, {alias_key("odd.bleak"): ("mood", "mood.gone")},
    )
    result = await verify.verify_payload(
        {"titles": {str(TITLE): [tag(term="odd.bleak")]}},
        pass_id="p1", voc=voc, packs={TITLE: PACK},
    )

    assert [t.term for t in result.tags[TITLE]] == ["mood.bleak"]


async def test_a_title_with_no_pack_is_refused_rather_than_passed():
    """A quote that cannot be checked looks exactly like an invented one; `no_pack` is its own reason."""
    result = await run(tag(), packs={TITLE: None})

    assert result.tags == {}
    assert reasons(result) == ["no_pack"]
    assert result.n_seen == 0, "a tag under a title with no pack is never even looked at"


async def test_a_title_the_payload_invented_is_refused_as_unknown_title():
    result = await run(tag(), allowed=[1, 2, 3])

    assert result.tags == {}
    assert reasons(result) == ["unknown_title"]


async def test_omitting_the_unit_costs_the_label_and_never_the_refusal():
    """Without `allowed` a foreign title id reads as
    `no_pack`, the less informative label; nothing is kept."""
    invented = {"titles": {"4242": [tag()]}}

    named = await verify.verify_payload(
        invented, pass_id="p1", voc=VOC, packs={TITLE: PACK}, allowed=[TITLE],
    )
    unnamed = await verify.verify_payload(
        invented, pass_id="p1", voc=VOC, packs={TITLE: PACK},
    )

    assert reasons(named) == ["unknown_title"]
    assert reasons(unnamed) == ["no_pack"]
    assert named.tags == unnamed.tags == {}


@pytest.mark.parametrize("stated", [0, 4, -1, 99])
async def test_a_stated_level_outside_the_declared_domain_is_refused_and_recorded(stated):
    """Decision 386: the corpus's clamp promotes 0 to 1, which is a repair; the boundary refuses instead."""
    result = await run(tag(salience=stated))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema"]
    assert result.rejects[0].salience == stated, (
        "the value that broke the domain is what a reviewer needs to see"
    )


async def test_a_clamp_would_have_kept_the_tag_this_test_drops():
    """The corpus's expression, run here, so the difference is a measurement and not a claim."""
    assert max(1, min(3, 0)) == 1
    assert max(1, min(3, 4)) == 3

    result = await run(tag(salience=0), tag(term="place.city", salience=4))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema", "schema"]


@pytest.mark.parametrize("stated", [1, 2, 3, "2", 2.0, "3", 3.0])
async def test_a_stated_level_inside_the_domain_is_read_as_its_integer(stated):
    """"2" and 2.0 are spellings of a level the domain holds."""
    result = await run(tag(salience=stated))

    assert result.tags[TITLE][0].salience == int(float(stated))


@pytest.mark.parametrize("stated", [3.9, 2.9, 1.0001, 1.9999, 3.4, "3.9"])
async def test_a_non_integral_level_is_refused_rather_than_truncated(stated):
    """`int(float(x))` truncated 3.9 into the domain; a non-integral level is refused."""
    result = await run(tag(salience=stated))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema"]
    assert "outside the declared domain" in result.rejects[0].detail
    assert str(float(stated)) in result.rejects[0].detail, (
        "the value that broke the domain is what a reviewer needs to see, and a truncated one is "
        "a number the provider never stated"
    )


async def test_an_integral_spelling_of_three_is_still_read_as_three():
    """The control: an integral spelling is still read."""
    result = await run(tag(salience="3e0"), tag(term="place.city", salience=" 2 "))

    assert [t.salience for t in result.tags[TITLE]] == [3, 2]


@pytest.mark.parametrize("stated", [0.5, -0.5])
async def test_a_level_below_one_is_recorded_as_what_was_stated_and_never_as_zero(stated):
    """`int(float(0.5))` was 0, so the reject row named a number nobody stated."""
    result = await run(tag(salience=stated))

    assert reasons(result) == ["schema"]
    assert str(stated) in result.rejects[0].detail
    assert result.rejects[0].salience is None, (
        "asyncpg truncates a float bound into a smallint silently, so the column takes nothing "
        "rather than a number nobody stated"
    )


@pytest.mark.parametrize("stated", [True, False])
async def test_a_boolean_states_no_level_and_is_refused(stated):
    """`float(True)` is 1.0; a boolean is refused, not defaulted."""
    result = await run(tag(salience=stated))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema"]
    assert "not a number" in result.rejects[0].detail


async def test_a_tag_that_states_no_level_takes_the_middle_of_the_scale():
    """An unfilled field makes no claim, so it takes the middle; a filled 0 is refused above."""
    result = await run(tag())

    assert result.tags[TITLE][0].salience == verify.DEFAULT_SALIENCE == 2


@pytest.mark.parametrize(
    "stated",
    [
        "very high",
        # These raised `OverflowError` out of the boundary; `json.loads` accepts `Infinity` by default.
        float("inf"), "inf", "Infinity", "1e400", 10 ** 400,
        # The controls: these already reached the refusal.
        "nan", float("nan"),
    ],
)
async def test_a_level_that_is_not_a_number_is_refused_rather_than_defaulted(stated):
    """The corpus swallows this to 2, inventing a claim the extractor never made."""
    result = await run(tag(salience=stated))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema"]
    assert "not a number" in result.rejects[0].detail


async def test_a_level_too_wide_for_the_reject_column_is_dropped_rather_than_clamped():
    """`dna_reject.salience` is a smallint; a wider value would take the whole `executemany` down."""
    result = await run(tag(salience=10 ** 20))

    assert reasons(result) == ["schema"]
    assert result.rejects[0].salience is None
    assert verify._storable(32767) == 32767
    assert verify._storable(-32768) == -32768
    assert verify._storable(32768) is None


@pytest.mark.parametrize("key", verify.TERM_KEYS)
async def test_every_spelling_of_the_term_field_is_read(key):
    """Over the module's own tuple, so shortening the tuple shortens this test."""
    result = await run({key: "place.city", "quote": PLAIN})

    assert terms(result) == ["place.city"]


@pytest.mark.parametrize("key", verify.QUOTE_KEYS)
async def test_every_spelling_of_the_quote_field_is_read(key):
    result = await run({"term": "place.city", key: PLAIN})

    assert terms(result) == ["place.city"]


@pytest.mark.parametrize("key", verify.SALIENCE_KEYS)
async def test_every_spelling_of_the_salience_field_is_read(key):
    result = await run({"term": "place.city", "quote": PLAIN, key: 3})

    assert result.tags[TITLE][0].salience == 3


@pytest.mark.parametrize("key", verify.SOURCE_KEYS)
async def test_every_spelling_of_the_source_field_is_read(key):
    result = await run({"term": "place.city", "quote": PLAIN, key: "trakt:1"})

    assert result.tags[TITLE][0].source == "trakt:1"


async def test_the_run_that_emitted_id_and_no_source_keeps_all_its_tags():
    """One run emitted `"id"` for `"term"` and a strict consumer logged all 161 tags as "bad term"."""
    result = await run(
        {"id": "mood.bleak", "quote": PLAIN},
        {"id": "place.city", "quote": PLAIN},
        {"id": "pacing.slow_burn", "quote": TRANSCRIBED},
    )

    assert sorted(terms(result)) == ["mood.bleak", "pacing.slow_burn", "place.city"]
    assert result.rejects == []
    assert [t.source for t in result.tags[TITLE]] == ["", "", ""]


def test_every_key_alias_list_is_unambiguous():
    """A key on two lists would make one field's spelling silently answer for another's."""
    lists = (verify.TERM_KEYS, verify.QUOTE_KEYS, verify.SOURCE_KEYS, verify.SALIENCE_KEYS)
    seen: set[str] = set()
    for keys in lists:
        assert len(set(keys)) == len(keys)
        assert not (set(keys) & seen), f"{sorted(set(keys) & seen)} is read as two fields"
        seen |= set(keys)


async def test_a_titles_value_that_is_not_an_object_is_one_schema_rejection():
    result = await verify.verify_payload(
        {"titles": [{"term": "mood.bleak"}]}, pass_id="p1", voc=VOC, packs={},
    )

    assert result.tags == {}
    assert reasons(result) == ["schema"]
    assert result.rejects[0].title_id is None


async def test_a_payload_with_no_titles_object_at_all_is_one_schema_rejection():
    result = await verify.verify_payload({}, pass_id="p1", voc=VOC, packs={})

    assert reasons(result) == ["schema"]


async def test_a_non_numeric_title_key_is_refused_and_named():
    result = await verify.verify_payload(
        {"titles": {"heat": [tag()]}}, pass_id="p1", voc=VOC, packs={TITLE: PACK},
    )

    assert result.tags == {}
    assert reasons(result) == ["schema"]
    assert "'heat'" in result.rejects[0].detail


async def test_a_titles_entry_that_is_not_a_list_is_refused():
    result = await verify.verify_payload(
        {"titles": {str(TITLE): {"term": "mood.bleak"}}},
        pass_id="p1", voc=VOC, packs={TITLE: PACK},
    )

    assert reasons(result) == ["schema"]
    assert "dict" in result.rejects[0].detail


async def test_a_tag_that_is_not_an_object_is_refused_without_stopping_the_others():
    result = await run("mood.bleak", tag(term="place.city"))

    assert terms(result) == ["place.city"]
    assert reasons(result) == ["schema"]
    assert result.n_seen == 2


async def test_a_tag_with_no_term_is_refused():
    result = await run({"quote": PLAIN})

    assert reasons(result) == ["schema"]
    assert result.rejects[0].term is None


async def test_a_tag_with_no_quote_is_refused():
    result = await run({"term": "mood.bleak"})

    assert reasons(result) == ["schema"]
    assert result.rejects[0].term == "mood.bleak"


async def test_an_empty_quote_is_a_missing_quote():
    """A field filled with "" is unfilled, and §4.1 rule 1 refuses the tag either way."""
    result = await run({"term": "mood.bleak", "quote": ""})

    assert reasons(result) == ["schema"]


# Everything `norm()` reads as nothing; both classes are one question now (decision 392).
FOLDS_TO_NOTHING = (
    "**", "***", "*", "___", "[spoiler]", "[/spoiler]", "\u00ad", "\u00a0",
    "   ", "\t", "\n ", "**[/spoiler]___*",
)


@pytest.mark.parametrize("quote", FOLDS_TO_NOTHING)
async def test_a_quote_that_folds_to_nothing_is_a_missing_quote(quote):
    """"" is a substring of every pack, so these passed rule 2 vacuously."""
    result = await run(tag(term="themes.robots", quote=quote))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema"]
    assert result.rejects[0].quote == quote, (
        "the reviewer has to see what was offered as evidence, or the row says only that "
        "something was refused"
    )


async def test_the_fold_is_what_makes_that_refusal_necessary():
    """Without the refusal these are unconditional passes."""
    assert [q for q in FOLDS_TO_NOTHING if verify.norm(q) != ""] == []
    assert all(verify.norm(q) in verify.norm(PACK) for q in FOLDS_TO_NOTHING)
    assert "robot" not in verify.norm(PACK), "the pack must not carry the term this attack claims"



# Invisible formatters scraped bodies carry; `\s` matches none and they survived `norm()` NON-EMPTY.
INVISIBLE = ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u180e")


@pytest.mark.parametrize("invisible", INVISIBLE, ids=[f"U+{ord(c):04X}" for c in INVISIBLE])
async def test_a_quote_that_renders_as_nothing_is_a_missing_quote(invisible):
    """Decision 398: one invisible character in pack and quote passed both halves of rule 1."""
    result = await run(tag(term="themes.robots", quote=invisible),
                       packs={TITLE: PACK + invisible})

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema"]
    assert result.rejects[0].quote == invisible, (
        "the reviewer has to see what was offered as evidence, even when what was offered "
        "prints as nothing"
    )


def test_the_fold_reads_an_invisible_character_as_nothing_and_the_pack_still_carries_it():
    """The character IS in the pack, so the refusal is not a `quote_unverified` in disguise."""
    for invisible in INVISIBLE:
        assert invisible in PACK + invisible
        assert invisible.strip() == invisible
        assert verify.norm(invisible) == ""


async def test_a_quote_transcribed_without_the_packs_invisible_hint_verifies():
    """The admitting half: an invisible hint inside a pack word must not drop a genuine quote."""
    hinted = PACK.replace("the city", "the ci\u200bty")
    result = await run(tag(term="place.city", quote=PLAIN), packs={TITLE: hinted})

    assert terms(result) == ["place.city"]
    assert PLAIN not in hinted, "the pack must not already carry the quote verbatim"



# Every pack header contains digits, so a number's `str()` would verify against it.
NOT_TEXT = (1995, 1, 0, True, ["a slow burn"], {"span": "a slow burn"})


@pytest.mark.parametrize("quote", NOT_TEXT, ids=[type(q).__name__ for q in NOT_TEXT])
async def test_a_quote_that_is_not_text_is_a_missing_quote(quote):
    """Decision 399: a quote that is not text is refused; this is not a minimum length."""
    result = await run(tag(quote=quote))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema"]
    assert result.rejects[0].quote == str(quote)


def test_the_pack_this_file_writes_really_carries_those_digits():
    """The refusal has to be the type check, not a failed substring test."""
    assert verify.norm("1995") in verify.norm(PACK)
    assert verify.norm("1") in verify.norm(PACK)


# `["slow burn"]` was KEPT: its `str()` folds through `alias_key` to an authored row.
NOT_A_TERM = (["slow burn"], ["mood.bleak"], ["mood.bleak", "place.city"], {"id": "mood.bleak"},
              7, True)


@pytest.mark.parametrize("term", NOT_A_TERM, ids=["list-alias", "list-term", "list-of-two", "dict",
                                                  "int", "bool"])
async def test_a_term_that_is_not_text_is_refused_rather_than_read_through_its_brackets(term):
    """A value that is not text is refused under `schema`, never repaired via its `str()`."""
    result = await run(tag(term=term))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema"]
    assert result.rejects[0].term == str(term)
    assert result.rejects[0].quote == PLAIN



# A NUL (Postgres refuses it) and a lone surrogate (UTF-8 cannot encode it); `json.loads` accepts both.
NUL = "mood.bl\x00eak"
SURROGATE = "mood.bl\ud800eak"


async def test_text_postgres_cannot_store_is_refused_before_anything_binds_it():
    """Refused before anything binds it; the storable field is still recorded."""
    result = await run(
        tag(term=NUL), tag(quote=NUL), tag(term=SURROGATE), tag(quote=SURROGATE),
        tag(term="place.city"),
    )

    assert terms(result) == ["place.city"]
    assert reasons(result) == ["schema"] * 4
    assert [r.term for r in result.rejects] == [None, "mood.bleak", None, "mood.bleak"]
    assert [r.quote for r in result.rejects] == [PLAIN, None, PLAIN, None]


async def test_no_untrusted_value_raises_out_of_the_boundary():
    """A raise records nothing and loses every other tag and refusal in the payload."""
    result = await run(
        tag(term="place.city"),
        tag(term="mood.bleak", quote="   "),
        tag(term="themes.robots", salience="inf"),
        tag(term="visual.neon", salience=float("nan")),
        tag(term="sound.neon", salience=float("inf")),
        tag(term="pacing.slow_burn", quote=TRANSCRIBED),
    )

    assert sorted(terms(result)) == ["pacing.slow_burn", "place.city"]
    assert reasons(result) == ["schema"] * 4


async def test_a_term_emitted_twice_for_one_title_is_kept_once():
    result = await run(tag(term="place.city"), tag(term="place.city"))

    assert terms(result) == ["place.city"]
    assert reasons(result) == ["duplicate"]


async def test_a_term_refused_on_an_earlier_rule_does_not_make_the_next_one_a_duplicate():
    """The first tag never became a tag, so the second is the first of its term."""
    result = await run(tag(term="place.city", quote=FABRICATED), tag(term="place.city"))

    assert terms(result) == ["place.city"]
    assert reasons(result) == ["quote_unverified"]


async def test_n_seen_counts_every_tag_the_boundary_looked_at():
    result = await run(tag(), tag(term="nope.nope"), tag(quote=FABRICATED), "junk")

    assert result.n_seen == 4
    assert result.n_kept == 1


@pytest.mark.parametrize("second", ["07", " 7 ", "+7", 7])
async def test_two_payload_keys_naming_one_title_lose_no_tag(second):
    """`int()` reads all these keys as 7; the second must accumulate, not replace the first's tags."""
    result = await verify.verify_payload(
        {"titles": {"7": [tag(term="place.city"), tag(term="mood.bleak")],
                    second: [tag(term="visual.neon")]}},
        pass_id="p1", voc=VOC, packs={TITLE: PACK},
    )

    assert sorted(terms(result)) == ["mood.bleak", "place.city", "visual.neon"]
    assert reasons(result) == []
    assert result.n_seen == 3


async def test_a_term_repeated_under_a_second_spelling_of_one_title_key_is_recorded():
    """Accumulating keeps the duplicate rule working across the two keys."""
    result = await verify.verify_payload(
        {"titles": {"7": [tag(term="place.city")], "07": [tag(term="place.city")]}},
        pass_id="p1", voc=VOC, packs={TITLE: PACK},
    )

    assert terms(result) == ["place.city"]
    assert reasons(result) == ["duplicate"]


# `verify_payload`'s own reading of a title key, shared with the classifier below.
def _folds_to(key: object) -> int | None:
    try:
        return int(key)
    except (TypeError, ValueError):
        return None


# Chosen for the arms they reach, including the title-level arms the identity excludes.
ACCOUNTED = (
    {"titles": {"7": [tag(), tag(term="nope.nope"), tag(quote=FABRICATED), "junk"]}},
    {"titles": {"7": [tag(term="place.city")], "07": [tag(term="place.city")]}},
    {"titles": {"07": [tag(term="place.city")], "7": [tag(term="mood.bleak")]}},
    {"titles": {"7": [tag()], "07": "junk"}},
    {"titles": {"7": [tag(quote="**"), tag(salience="inf"), tag(term="x", salience=9)]}},
    {"titles": {"7": [{"term": "mood.bleak"}, {"quote": PLAIN}, tag(salience=0)]}},
    {"titles": {"7": [tag(quote=1995), tag(term=NUL), tag(quote="\u200b")]}},
    {"titles": {"7": [], "nope": [tag()]}},
    {"titles": {"7": [tag()], "8": [tag(), tag(term="place.city")]}},
)


@pytest.mark.parametrize("payload", ACCOUNTED)
async def test_the_accounting_identity_holds_over_every_tag_the_boundary_looked_at(payload):
    """Every examined tag is kept or recorded; title-level refusals
    are excluded because their tags are never examined."""
    result = await verify.verify_payload(
        payload, pass_id="p1", voc=VOC, packs={TITLE: PACK, 8: None},
    )
    # A list-typed and a str-typed key for one title: the one title-level refusal that coexists with a list.
    examined = set(result.tags)
    shape_collisions = sum(1 for key, tags in payload["titles"].items()
                           if not isinstance(tags, list) and _folds_to(key) in examined)
    tag_level = [r for r in result.rejects if r.title_id in examined]

    assert result.n_seen == result.n_kept + len(tag_level) - shape_collisions


async def test_a_payload_that_names_itself_keeps_its_own_pass_id():
    """Ported: "a file that names itself keeps its identity through a rename"."""
    result = await verify.verify_payload(
        {"pass": "sonnet-b", "titles": {str(TITLE): [tag(), tag(term="themes.timetravel")]}},
        pass_id="p1", voc=VOC, packs={TITLE: PACK},
    )

    assert result.pass_id == "sonnet-b"
    assert [r.pass_id for r in result.rejects] == ["sonnet-b"], (
        "the rejection has to name the pass it came out of, or a reject review cannot tell two "
        "providers' refusals apart in section 6.6's parallel mode"
    )


@pytest.mark.parametrize("quote", ["", *FOLDS_TO_NOTHING])
def test_a_verified_tag_cannot_be_constructed_without_its_quote(quote):
    """The only place M5.4 owns is the type it hands forward;
    `str.strip()` and `norm()` now ask one question."""
    with pytest.raises(ValueError, match="unfalsifiable"):
        verify.VerifiedTag(
            term="mood.bleak", facet="mood", salience=2, source="", quote=quote,
        )



@pytest.mark.parametrize("quote", NOT_TEXT, ids=[type(q).__name__ for q in NOT_TEXT])
def test_a_verified_tag_cannot_be_constructed_from_a_quote_that_is_not_text(quote):
    """`norm()` stringifies, so `VerifiedTag(quote=1995)` built and carried an `int`."""
    with pytest.raises(ValueError, match="unfalsifiable"):
        verify.VerifiedTag(
            term="mood.bleak", facet="mood", salience=2, source="", quote=quote,
        )


@pytest.mark.parametrize(
    "quote",
    [NUL, SURROGATE, "a slow\x00 burn of a film", "a slow\ud800 burn of a film"],
    ids=["nul", "surrogate", "nul-inside-a-sentence", "surrogate-inside-a-sentence"],
)
def test_a_verified_tag_cannot_carry_a_quote_postgres_cannot_store(quote):
    """`norm()` drops NUL and lone surrogates, but the raw string would break the writer's batch."""
    assert verify.norm(quote), "the fold must read these as text, or this test proves nothing"
    with pytest.raises(ValueError, match="cannot store"):
        verify.VerifiedTag(
            term="mood.bleak", facet="mood", salience=2, source="", quote=quote,
        )


def test_a_verified_tag_that_carries_its_quote_is_built_without_complaint():
    built = verify.VerifiedTag(
        term="mood.bleak", facet="mood", salience=2, source="trakt:1", quote=PLAIN,
    )

    assert built.quote == PLAIN
    assert built.repaired is False


async def test_every_tag_the_boundary_passes_carries_a_quote():
    """Asked in `norm()`, with tags that fold to nothing riding along so there is something to refuse."""
    result = await run(
        tag(), tag(term="place.city", quote=TRANSCRIBED),
        *(tag(term="themes.robots", quote=q) for q in (*FOLDS_TO_NOTHING, *INVISIBLE)),
    )

    assert all(verify.norm(t.quote) for t in result.tags[TITLE])
    assert [t.term for t in result.tags[TITLE]] == ["mood.bleak", "place.city"]


def _bound_names(node: ast.AST) -> list[str]:
    """Every binding form, except a bare annotation, which declares what a term IS."""
    if isinstance(node, ast.Assign):
        targets: list[ast.expr] = list(node.targets)
    elif isinstance(
        node, (ast.AugAssign, ast.For, ast.AsyncFor, ast.NamedExpr, ast.comprehension)
    ) or (isinstance(node, ast.AnnAssign) and node.value is not None):
        targets = [node.target]
    elif isinstance(node, ast.withitem) and node.optional_vars is not None:
        targets = [node.optional_vars]
    elif isinstance(node, (ast.ExceptHandler, ast.alias)):
        bound = node.name if isinstance(node, ast.ExceptHandler) else node.asname
        return [bound] if bound else []
    elif isinstance(node, ast.arg):
        return [node.arg]
    else:
        return []
    return [
        child.id for target in targets for child in ast.walk(target)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
    ]


def _term_writes(source: str) -> list[str]:
    tree = ast.parse(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        if "term" in _bound_names(node):
            hits.append(" ".join((ast.get_source_segment(source, node) or "").split())[:120])
    return hits


def test_no_statement_in_the_verifier_writes_a_term_outside_the_two_named_repairs():
    """§8 stage 7: "Failures drop, never repaired". Exactly
    two statements bind a term, both named repairs."""
    writes = _term_writes(SOURCE)

    assert len(writes) == 2, "\n".join(["a third statement writes a term:", *writes])
    assert "voc.resolve(" in writes[0], writes[0]
    assert "_renamed(" in writes[1], writes[1]


def test_the_only_thing_the_rename_wrapper_calls_is_the_curation_ledger():
    """`_renamed` is the second write's whole body, so a hidden rewrite there would pass the guard above."""
    tree = ast.parse(SOURCE)
    wrapper = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_renamed"
    )
    called = {
        ast.get_source_segment(SOURCE, node.func)
        for node in ast.walk(wrapper) if isinstance(node, ast.Call)
    }

    assert called == {"adjudicate.rename"}


def test_nothing_in_the_verifier_assigns_to_a_term_attribute():
    """`tag.term = corrected` binds no name, so it is invisible to the guard above."""
    tree = ast.parse(SOURCE)
    hits = [
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and node.attr in ("term", "quote", "facet")
    ]

    assert hits == []


def test_every_verified_tag_is_built_from_the_term_the_checks_agreed_on():
    """A third repair could arrive as an expression inside the constructor."""
    tree = ast.parse(SOURCE)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "VerifiedTag"
    ]

    assert calls, "the verifier builds no tags at all"
    for call in calls:
        offered = {kw.arg: ast.get_source_segment(SOURCE, kw.value) for kw in call.keywords}
        assert offered.get("term") == "term", offered


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("term = voc.resolve(x)", 1),
        ("term = voc.resolve(x)\nterm = fix(term)", 2),
        ("for term in candidates:\n    pass", 1),
        ("term += '!'", 1),
        ("if (term := repair(x)):\n    pass", 1),
        # Five shapes that bind `term` without a `Name` in `Store`
        # context; a repair inside `Vocabulary.repair` hid there.
        ("[term for term in xs]", 1),
        ("(term for term in xs)", 1),
        ("with open(p) as term:\n    pass", 1),
        ("try:\n    pass\nexcept ValueError as term:\n    pass", 1),
        ("import re as term", 1),
        ("def outer():\n    def inner(term):\n        pass", 1),
        # The shape that must NOT count: a dataclass field declaring what a term is.
        ("import dataclasses\n@dataclasses.dataclass\nclass T:\n    term: str", 0),
        ("other = 1", 0),
        ("[other for other in xs]", 0),
    ],
)
def test_the_term_write_guard_sees_each_shape_a_repair_would_take(snippet, expected):
    """A guard that cannot fail is not a guard, and this one has to see eleven spellings."""
    assert len(_term_writes(snippet)) == expected


def _single_character_edits(term_id: str) -> set[str]:
    """Capitals and space are in the alphabet, or the `Mood.slow_burn` head could never be generated."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_. "
    out = set()
    for i in range(len(term_id)):
        out.add(term_id[:i] + term_id[i + 1:])
        out |= {term_id[:i] + c + term_id[i + 1:] for c in alphabet}
    for i in range(len(term_id) + 1):
        out |= {term_id[:i] + c + term_id[i:] for c in alphabet}
    return out - {term_id}


def test_the_prefix_repair_never_changes_a_body_under_any_single_character_edit():
    """Exhaustive over single-character edits: neither the body nor a case-folded facet may change."""
    declared = {facet.casefold() for facet in FACETS}
    violations = []
    for term_id in TERMS:
        for offered in _single_character_edits(term_id):
            got = VOC.repair(offered)
            if got is None:
                continue
            # Stripped first, as `repair` strips: padding is not a body.
            if got.partition(".")[2] != offered.strip().partition(".")[2]:
                violations.append(f"{offered!r} -> {got!r} rewrote the body")
            named = offered.partition(".")[0].strip().casefold()
            if named in declared and TERMS[got].casefold() != named:
                violations.append(f"{offered!r} -> {got!r} left the facet it named")

    assert violations == [], "the repair broke its property:\n  " + "\n  ".join(violations[:20])


def test_reasons_is_the_ported_tuple_in_its_ported_order():
    """`0027`'s CHECK lists the same seven in the same order."""
    assert verify.REASONS == (
        "schema", "unknown_term", "adjudicated", "quote_unverified", "unknown_title",
        "no_pack", "duplicate",
    )


def test_the_reject_store_constrains_itself_to_exactly_the_ported_reasons():
    """A set closed in the code and open in the schema is not closed."""
    sql = MIGRATION.read_text(encoding="utf-8")
    clause = re.search(r"rule_violated\s+IN\s*\((.*?)\)", sql, re.DOTALL)

    assert clause is not None, "0027 no longer constrains rule_violated"
    assert tuple(re.findall(r"'([a-z_]+)'", clause.group(1))) == verify.REASONS


def test_the_pipeline_marker_is_ported_with_its_reason():
    """An older pipeline's output is stale evidence, not merely older."""
    assert verify.PIPELINE == "library/v1"


def test_no_provider_client_is_imported_by_the_trust_boundary():
    """§9's separation, asserted as a fact about imports."""
    tree = ast.parse(SOURCE)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert not [m for m in imported if "llm" in m or "provider" in m or "anthropic" in m], (
        f"a validator one import away from the thing it judges: {sorted(imported)}"
    )
