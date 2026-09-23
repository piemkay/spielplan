"""The trust boundary, against a pack this file constructs. Spec v2.1 §8 stage 7, §9, §4.1 rule 1.

§9 says what is being tested here and why it is the most important file in the milestone: "The
schema is a cost-saving device, not the guarantee - the guarantee is the validator" (`spec:418`).
A provider that returns well-formed JSON has satisfied the schema and said nothing about whether
its tags are true, so every assertion below is about the three mechanical checks that decide it.

TWO FILES, SPLIT WHERE THE EVIDENCE IS. This one is the payload-shaped half: the pack is a string
written at the top of this module, the vocabulary is six terms, and nothing touches Postgres, so
each rule can be put under a payload built to break exactly it. `test_dna_reject.py` is the other
half -- a real pack built by `dna/packs.py` out of real rows, the shipped reject store, the
adjudication ledger, and the 100%-catch-rate measurement §8 claims. Neither is a substitute for
the other: a boundary asserted only against synthetic strings has never met a pack, and a boundary
asserted only end to end cannot say WHICH rule refused a tag.

THE TWO CASES A STRICT PORT LOSES ARE BOTH HERE (M5.4-plan.md §7, checks 3 and 5). A quote
transcribed across `**`, `[spoiler]` and a smart apostrophe must PASS, because the pack carries
what its sources published and the fold is what makes the substring test mean anything. And all
four key-alias spellings of `term` and of `quote` must be read, because a run that emitted `"id"`
instead of `"term"` once had all 161 of its tags logged as "bad term" by a strict consumer -- a
reading indistinguishable from the extractor having collapsed.

Integration tests are skipped without TEST_DATABASE_URL; there are none in this file.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

from spielplan.dna import verify
from spielplan.dna.aliases import alias_key

MODULE = Path(verify.__file__)
SOURCE = MODULE.read_text(encoding="utf-8")
MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0027_dna_extraction.sql"

# `test_landmine_guards.py` belongs to no milestone in this wave, so what `0027` claims about it
# is read OUT OF IT rather than restated here: a restated guard is a second guard, and a second
# guard agrees with the first until the day it does not. Loaded by path and under a name of its
# own, because importing a sibling test module by its own name would give the collector two
# objects for one file.
GUARD = Path(__file__).resolve().parent / "test_landmine_guards.py"

# Six terms over six of vocabulary v1's eleven facets, chosen for the shapes the repair has to
# answer rather than for realism: one body carried by exactly one facet (`slow_burn`, `bleak`),
# and one body carried by two (`neon`, in `visual` and `sound`), which is the case the repair
# must refuse rather than guess at.
TERMS = {
    "mood.bleak": "mood",
    "pacing.slow_burn": "pacing",
    "place.city": "place",
    "themes.robots": "themes",
    "visual.neon": "visual",
    "sound.neon": "sound",
}
FACETS = ("mood", "pacing", "place", "themes", "visual", "sound")

# Two authored rows. The first is the ordinary case -- a bare phrase the owner already decided
# names a term. The second exists only to pin the ORDER: `odd.slow_burn` is a spelling the prefix
# repair would resolve on its own, and to a different term, so a test can tell which arm answered.
ALIASES = {
    alias_key("slow burn"): ("pacing", "pacing.slow_burn"),
    alias_key("odd.slow_burn"): ("mood", "mood.bleak"),
}

VOC = verify.Vocabulary.build("v1", TERMS, FACETS, ALIASES)

# A pack in the shape `dna/packs.py` writes one, carrying the markup its sources published: the
# markdown emphasis and BBCode spoiler tags `norm()`'s own docstring measured across the library
# at 7,334 and 1,336 occurrences. It is NOT tidied here, because tidying it at build time is the
# failure the fold exists to prevent and it fails silently.
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

# The same span as a human transcribes it: the markers gone and the apostrophe curled, which is
# what a WordPress source and a careful reader both produce. Written as an escape so a Windows
# cp1252 console never meets the codepoint in a failure message.
TRANSCRIBED = "a slow burn of a film and the city\u2019s own rhythm"

# One word different, and the difference is the whole of what the substring test catches: the
# pilot's negative control was plausible tags carrying invented quotes, and it was caught at 100%
# by this test alone.
FABRICATED = "a slow burn of a novel and the city\u2019s own rhythm"

PLAIN = "A crew works the city at night."
TITLE = 7


def tag(term: str = "mood.bleak", quote: str = PLAIN, **extra: object) -> dict[str, object]:
    """One tag in the shape a provider emits it, with the two required fields filled."""
    row: dict[str, object] = {"term": term, "quote": quote}
    row.update(extra)
    return row


async def run(*tags: object, packs: dict[int, str | None] | None = None, **kw: object):
    """`verify_payload` over one title, with this file's vocabulary and pack."""
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


# --- rule 1: term in vocabulary ------------------------------------------------------------


async def test_a_fabricated_term_is_absent_from_the_output_and_recorded():
    """Exit check 1. The tag is schema-valid in every other way, which is the point: a provider
    that returns well-formed JSON has satisfied the schema and said nothing about the term."""
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
    """A provider that names a facet is making a claim the vocabulary already settles. M4.9
    finding 1 measured what happens when the two namings are allowed to disagree: 29,188 of
    31,540 `dna_tag` rows filed under a facet `dna_facet` does not declare."""
    result = await run(tag(term="sound.neon", facet="visual"))

    assert result.tags[TITLE][0].facet == "sound"


@pytest.mark.parametrize(
    ("offered", "expected"),
    [
        ("plot_structure.slow_burn", "pacing.slow_burn"),
        ("mood_tone.bleak", "mood.bleak"),
        ("narrative_themes.robots", "themes.robots"),
        # The corpus's second arm, which needed no port: a head spelled with the other separator
        # is not a declared facet either, so it takes the same path as any other wrong prefix.
        ("mood-tone.bleak", "mood.bleak"),
        # Surrounding whitespace is stripped before anything else, exactly as the corpus strips it.
        ("  plot_structure.slow_burn  ", "pacing.slow_burn"),
        # A head that case-folds to the facet which really carries the body names that facet in
        # another case and claims nothing else, so the case-folded head guard still recalls it.
        # [M5.4 review cycle 3, M54-DIM3-C3-01]
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
        # THE CASE THE HEAD GUARD EXISTS FOR, and the only one that separates it from the
        # uniqueness rule. `mood` IS a declared facet and `slow_burn` is a body exactly one term
        # carries, so a repair here would be available and would move the tag from the facet the
        # extractor named into another one. That is a change of meaning, not a recall of a
        # spelling: the extractor claimed this film has a MOOD called slow_burn, and the prefix
        # repair only ever recalls a prefix somebody mis-remembered.
        ("mood.slow_burn", "unknown_term"),
        # THE SAME CLAIM IN ANOTHER CASE, which is why the head guard case-folds. An LLM
        # title-cases a head as readily as it mis-remembers one, and an exact-case guard read
        # `Mood` as "not a declared facet" -- so `Mood.slow_burn` was kept as `pacing.slow_burn`,
        # filed under a facet the extractor never named, while `mood.slow_burn` above was
        # refused: one spelling for the rule and another for the code, decision 392's shape.
        # [M5.4 review cycle 3, M54-DIM3-C3-01]
        ("Mood.slow_burn", "unknown_term"),
        ("MOOD.slow_burn", "unknown_term"),
        ("Themes.slow_burn", "unknown_term"),
        ("mood .slow_burn", "unknown_term"),
        ("Mood.robots", "unknown_term"),
        ("mood.neon", "unknown_term"),
        ("mood.nonexistent", "unknown_term"),
        # Nothing to take a prefix off.
        ("slowburn", "unknown_term"),
        # `_first` reads "" as a field the extractor did not fill, so this never reaches the
        # repair at all -- it is a missing term, which is a different rejection and says so.
        ("", "schema"),
    ],
)
async def test_the_prefix_repair_refuses_rather_than_guessing(offered, reason):
    result = await run(tag(term=offered))

    assert result.tags[TITLE] == []
    assert reasons(result) == [reason]


def test_the_prefix_repair_never_changes_the_term_body():
    """The property the corpus's own docstring claims, asserted as a property rather than by
    example: `repair` only ever rewrites the PREFIX, so it cannot invent a tag the extractor did
    not mean. Everything it returns is a key of the vocabulary whose body is the body it was
    given, or None."""
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
    """A map row is a decision somebody took; the prefix repair is a mechanical guess about a
    mistake. `odd.slow_burn` is a spelling both arms can answer, and they answer differently."""
    assert VOC.repair("odd.slow_burn") == "pacing.slow_burn"

    result = await run(tag(term="odd.slow_burn"))

    assert terms(result) == ["mood.bleak"]


async def test_the_ledger_is_not_invented_when_there_is_none_to_read():
    """`ledger=None` is the install that cannot read `dna_adjudication`. It drops the same tag
    under the label that claims less, and never keeps one: naming a retirement nobody consulted
    would be the boundary asserting a curation decision it never read."""
    result = await run(tag(term="themes.timetravel"), ledger=None)

    assert reasons(result) == ["unknown_term"]
    assert result.rejects[0].detail == "not in vocabulary"


# --- rule 2: the quote is a substring of THAT title's pack ---------------------------------


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
    """EXIT CHECK 3, and one of the two a strict port loses.

    The pack carries `**slow burn**`, `[spoiler]film[/spoiler]` and a straight apostrophe, and the
    quote carries none of the markup and a curled one. Folding at comparison time is what makes
    those the same span; stripping at pack-build time would make this test pass against a pack no
    extractor could transcribe this quote FROM, which is the silent half of the same failure.
    """
    result = await run(tag(quote=TRANSCRIBED))

    assert terms(result) == ["mood.bleak"]
    assert result.tags[TITLE][0].quote == TRANSCRIBED, (
        "the quote stored is the one the extractor produced; the fold is a comparison and not a "
        "rewrite of the evidence"
    )


async def test_the_pack_this_title_has_is_the_pack_its_quotes_are_checked_against():
    """"THAT title's pack" is the whole of rule 2. A quote lifted from another title's pack is
    exactly what an extractor confusing two rows in one unit produces."""
    other = "# Other (2001)\n[trakt:1]\nA wholly unrelated sentence about a different film.\n"
    result = await verify.verify_payload(
        {"titles": {str(TITLE): [tag(quote="a wholly unrelated sentence")]}},
        pass_id="p1", voc=VOC, packs={TITLE: PACK, 99: other},
    )

    assert reasons(result) == ["quote_unverified"]


async def test_an_authored_alias_pointing_outside_the_vocabulary_drops_the_tag():
    """`resolve`'s docstring promises that everything it returns is a key of `terms` or None, and
    `verify_payload` spends that promise on `voc.terms[term]` with no test of its own.

    The authored arm returned whatever the map said. On this install the map is joined to
    `dna_term` in `dna/aliases.py`, but `Vocabulary.build` takes it from its caller and validates
    nothing, and `dna_alias` carries no foreign key from `term` to `dna_term` at all -- so the
    table really does hold rows naming terms the vocabulary does not, and a map built from one
    turned a dropped tag into a `KeyError` out of the trust boundary. The type holds its own
    invariant now, and the tag drops under the label it always deserved.
    """
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
    """The fall-through, which is the difference between refusing the ARM and refusing the tag:
    an authored row pointing at nothing is not a decision that answered, so it does not get to
    stop the derived repair below it from recalling the term the extractor meant."""
    voc = verify.Vocabulary.build(
        "v1", TERMS, FACETS, {alias_key("odd.bleak"): ("mood", "mood.gone")},
    )
    result = await verify.verify_payload(
        {"titles": {str(TITLE): [tag(term="odd.bleak")]}},
        pass_id="p1", voc=voc, packs={TITLE: PACK},
    )

    assert [t.term for t in result.tags[TITLE]] == ["mood.bleak"]


async def test_a_title_with_no_pack_is_refused_rather_than_passed():
    """`no_pack`, and never a silent pass: a quote that cannot be checked is precisely what an
    invented one looks like. The reason is its own member of REASONS so that an install missing
    its packs does not read as an extractor emitting garbage."""
    result = await run(tag(), packs={TITLE: None})

    assert result.tags == {}
    assert reasons(result) == ["no_pack"]
    assert result.n_seen == 0, "a tag under a title with no pack is never even looked at"


async def test_a_title_the_payload_invented_is_refused_as_unknown_title():
    result = await run(tag(), allowed=[1, 2, 3])

    assert result.tags == {}
    assert reasons(result) == ["unknown_title"]


async def test_omitting_the_unit_costs_the_label_and_never_the_refusal():
    """`allowed` is the unit's title ids, and it is the only thing that can tell a title this
    install does not hold from one it holds and has not packed.

    Without it the same payload falls through to `packs` and is refused as `no_pack`, which drops
    the same tag under the less informative of the two -- exactly the trade `ledger=None` makes
    for `adjudicated` against `unknown_term`, and stated on `verify_payload` for the same reason.
    An operator reading a spike in `no_pack` goes to the pack builder; the tag that produced it
    was a provider inventing a title id. What the omission never does is keep a tag.
    """
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


# --- rule 3: salience within {1, 2, 3} (decision 386) ---------------------------------------


@pytest.mark.parametrize("stated", [0, 4, -1, 99])
async def test_a_stated_level_outside_the_declared_domain_is_refused_and_recorded(stated):
    """EXIT CHECK 4, and decision 386's whole content.

    The corpus clamps (`max(1, min(3, sal))`), which promotes a 0 to a 1 -- and a promotion is a
    repair, which is the one thing §8 stage 7 says this boundary never does. It is also the only
    way to write a row `dna_tag`'s own CHECK would have refused. The reason is `schema` because
    what broke is the contract's declared domain, and REASONS gains no member for it.
    """
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
    """A provider that answers "2" or 2.0 has made a claim the domain holds, and reading a
    SPELLING is not repairing it.

    3.4 was in this list and is out, which is the whole of decision 386 applied to the line under
    it: 3.4 is not inside the domain, so a test named for reading a level inside the domain was
    pinning the truncation that put it there. Every parameter here now states a level the domain
    actually holds.
    """
    result = await run(tag(salience=stated))

    assert result.tags[TITLE][0].salience == int(float(stated))


@pytest.mark.parametrize("stated", [3.9, 2.9, 1.0001, 1.9999, 3.4, "3.9"])
async def test_a_non_integral_level_is_refused_rather_than_truncated(stated):
    """DECISION 386, READ AGAINST THE LINE THAT WAS SUPPOSED TO CARRY IT.

    `int(float(stated))` never tested the provider's claim against {1,2,3}; it tested
    `trunc(claim)`, so every one of these was repaired into the domain and kept. A stated 3.9
    stored as a 3 is decision 386's own words -- "indistinguishable from a real 3" -- and the only
    difference from the clamp it refuses is the width of the interval repaired over. The control
    below is what keeps this from being a test that simply refuses more: an integral SPELLING is
    still read, because reading a spelling is not repairing a claim.
    """
    result = await run(tag(salience=stated))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema"]
    assert "outside the declared domain" in result.rejects[0].detail
    assert str(float(stated)) in result.rejects[0].detail, (
        "the value that broke the domain is what a reviewer needs to see, and a truncated one is "
        "a number the provider never stated"
    )


async def test_an_integral_spelling_of_three_is_still_read_as_three():
    """The control under the test above: refusing the non-integral values must not cost the
    spellings that are claims the domain holds."""
    result = await run(tag(salience="3e0"), tag(term="place.city", salience=" 2 "))

    assert [t.salience for t in result.tags[TITLE]] == [3, 2]


@pytest.mark.parametrize("stated", [0.5, -0.5])
async def test_a_level_below_one_is_recorded_as_what_was_stated_and_never_as_zero(stated):
    """The sharper half, and the one decision 341 is about. `int(float(0.5))` was 0, so the tag
    dropped -- correctly -- and the `dna_reject` row said the provider stated 0. A reviewer
    opening that refusal read a number nobody uttered, on the screen this table exists to make
    reviewable, and `_storable` would have written it into the column."""
    result = await run(tag(salience=stated))

    assert reasons(result) == ["schema"]
    assert str(stated) in result.rejects[0].detail
    assert result.rejects[0].salience is None, (
        "asyncpg truncates a float bound into a smallint silently, so the column takes nothing "
        "rather than a number nobody stated"
    )


@pytest.mark.parametrize("stated", [True, False])
async def test_a_boolean_states_no_level_and_is_refused(stated):
    """`float(True)` is 1.0, so a provider whose adapter renders a tri-state field as a boolean
    had every `true` read as the bottom of the scale -- a claim it never made. It is refused and
    not defaulted, because `DEFAULT_SALIENCE` is for a field the extractor did not fill and this
    one was filled with something that is not a number."""
    result = await run(tag(salience=stated))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema"]
    assert "not a number" in result.rejects[0].detail


async def test_a_tag_that_states_no_level_takes_the_middle_of_the_scale():
    """Ported (`or 2`), and not the clamp wearing another hat: a field the extractor did not fill
    is a field it made no claim about. A field it DID fill with 0 is refused above."""
    result = await run(tag())

    assert result.tags[TITLE][0].salience == verify.DEFAULT_SALIENCE == 2


@pytest.mark.parametrize(
    "stated",
    [
        "very high",
        # The four that raised `OverflowError` straight out of the boundary, past a clause naming
        # two of the three exceptions its own conversion raises (decision 392). `json.loads`
        # produces the first of these from the bare `Infinity` literal by default, so a provider
        # needs no unusual spelling at all, and `10 ** 400` is the same door through `float()`.
        float("inf"), "inf", "Infinity", "1e400", 10 ** 400,
        # And the two that already reached this refusal, kept here as the controls that say the
        # arm was right about them and only ever named the wrong exceptions.
        "nan", float("nan"),
    ],
)
async def test_a_level_that_is_not_a_number_is_refused_rather_than_defaulted(stated):
    """Decision 386's own words -- "the value breaks the contract's declared domain" -- applied
    to the same kind of value. The corpus swallows this to 2, which invents a claim the extractor
    never made."""
    result = await run(tag(salience=stated))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema"]
    assert "not a number" in result.rejects[0].detail


async def test_a_level_too_wide_for_the_reject_column_is_dropped_rather_than_clamped():
    """`dna_reject.salience` is a `smallint` and a payload is untrusted about its numbers too. The
    row still names the term, the quote and the rule; only the number the column cannot hold is
    absent, and the alternative is an `executemany` range error that loses every OTHER refusal in
    the same run -- "losing a whole batch", by a different door."""
    result = await run(tag(salience=10 ** 20))

    assert reasons(result) == ["schema"]
    assert result.rejects[0].salience is None
    assert verify._storable(32767) == 32767
    assert verify._storable(-32768) == -32768
    assert verify._storable(32768) is None


# --- the key-alias tolerance (exit check 5) -------------------------------------------------


@pytest.mark.parametrize("key", verify.TERM_KEYS)
async def test_every_spelling_of_the_term_field_is_read(key):
    """EXIT CHECK 5, parametrised over the module's own tuple so that shortening the tuple
    shortens this test rather than silently reducing what it covers."""
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
    """The scar the four lists were written for, replayed. One observed run emitted `"id"` for
    `"term"` and omitted `"source"` entirely while its own self-verification reported zero errors;
    a strict consumer logged all 161 of its tags as "bad term", which is indistinguishable from a
    catastrophic quality collapse. Three tags stand for the 161."""
    result = await run(
        {"id": "mood.bleak", "quote": PLAIN},
        {"id": "place.city", "quote": PLAIN},
        {"id": "pacing.slow_burn", "quote": TRANSCRIBED},
    )

    assert sorted(terms(result)) == ["mood.bleak", "pacing.slow_burn", "place.city"]
    assert result.rejects == []
    assert [t.source for t in result.tags[TITLE]] == ["", "", ""]


def test_every_key_alias_list_is_unambiguous():
    """The one rule the corpus's comment puts on the lists: "every alias must be unambiguous". A
    key appearing on two lists would make one field's spelling silently answer for another's."""
    lists = (verify.TERM_KEYS, verify.QUOTE_KEYS, verify.SOURCE_KEYS, verify.SALIENCE_KEYS)
    seen: set[str] = set()
    for keys in lists:
        assert len(set(keys)) == len(keys)
        assert not (set(keys) & seen), f"{sorted(set(keys) & seen)} is read as two fields"
        seen |= set(keys)


# --- what the payload itself can get wrong --------------------------------------------------


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
    """`_first` treats "" as absent, which is ported and is the right reading twice over: a field
    filled with the empty string is a field the extractor did not fill, and §4.1 rule 1 refuses
    the tag either way."""
    result = await run({"term": "mood.bleak", "quote": ""})

    assert reasons(result) == ["schema"]


# Everything `norm()` reads as nothing, in the two classes the boundary used to answer
# differently: the markup-only spellings survived `str.strip()` and were KEPT, and the
# whitespace-only ones raised `ValueError` out of `verify_payload` from `VerifiedTag`. Both are
# one question now and both are refused (decision 392). The last one is the shape that made the
# first class dangerous rather than merely wrong -- it is not obviously empty to a reader.
FOLDS_TO_NOTHING = (
    "**", "***", "*", "___", "[spoiler]", "[/spoiler]", "\u00ad", "\u00a0",
    "   ", "\t", "\n ", "**[/spoiler]___*",
)


@pytest.mark.parametrize("quote", FOLDS_TO_NOTHING)
async def test_a_quote_that_folds_to_nothing_is_a_missing_quote(quote):
    """DECISION 392, AND THE HOLE IT CLOSES IN RULE 2.

    `norm()` is the only reading of a quote this boundary has, and every string above folds to the
    empty string under it -- which is a substring of every pack, so rule 2 passed each one
    VACUOUSLY: `themes.robots` attached to a title whose pack never mentions a robot, kept, and
    with no `dna_reject` row because it was not a drop. Evidence that folds to nothing is not
    evidence, so it is a missing quote and is recorded beside one, under the same `schema`.
    """
    result = await run(tag(term="themes.robots", quote=quote))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema"]
    assert result.rejects[0].quote == quote, (
        "the reviewer has to see what was offered as evidence, or the row says only that "
        "something was refused"
    )


async def test_the_fold_is_what_makes_that_refusal_necessary():
    """The measurement under the test above: without the refusal these are not near misses, they
    are unconditional passes, because `""` is a substring of every string there has ever been."""
    assert [q for q in FOLDS_TO_NOTHING if verify.norm(q) != ""] == []
    assert all(verify.norm(q) in verify.norm(PACK) for q in FOLDS_TO_NOTHING)
    assert "robot" not in verify.norm(PACK), "the pack must not carry the term this attack claims"



# The invisible formatters a scraped review body routinely carries. U+200B is a line-break hint
# in HTML, U+FEFF arrives as the BOM of a decoded page, and the rest are the same family;
# §4.1 rule 8 says outright that the corpus "legitimately contains CJK, RTL scripts, ZWSP
# and emoji", so a pack holding one is the ordinary case and not a contrived input. Python's `\s`
# matches none of them and the ported fold table carries the soft hyphen and none of its
# siblings, so every one of these survived `norm()` NON-EMPTY -- past decision 392's refusal, and
# admitted by rule 2 against any pack carrying the same character (decision 398).
INVISIBLE = ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u180e")


@pytest.mark.parametrize("invisible", INVISIBLE, ids=[f"U+{ord(c):04X}" for c in INVISIBLE])
async def test_a_quote_that_renders_as_nothing_is_a_missing_quote(invisible):
    """DECISION 398, AND THE CLASS DECISION 392 LEFT ONE CHARACTER WIDE.

    The tag is a fabrication of exactly the shape this milestone exists to catch -- `themes.robots`
    on a pack in which no robot appears -- and its evidence is one character that renders as
    nothing. It passed BOTH halves of §4.1 rule 1's promise: the constructor asked `norm()`
    and got a non-empty string back, and rule 2 found that string in the pack, because the pack's
    own source published the same character. What §6.6 would then show a reviewer is a tag whose
    evidence column is blank, with no `dna_reject` row anywhere, because nothing was dropped.
    """
    result = await run(tag(term="themes.robots", quote=invisible),
                       packs={TITLE: PACK + invisible})

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema"]
    assert result.rejects[0].quote == invisible, (
        "the reviewer has to see what was offered as evidence, even when what was offered "
        "prints as nothing"
    )


def test_the_fold_reads_an_invisible_character_as_nothing_and_the_pack_still_carries_it():
    """The measurement under the test above, so its refusal cannot be a `quote_unverified`
    wearing a `schema` label: the character IS in the pack, which is the whole precondition, and
    the fold now reads it as what it renders as. `str.strip()` sees every one of them as
    non-empty exactly as it does the markup class, which is why the question belongs to the one
    fold rather than to a second reading of a quote."""
    for invisible in INVISIBLE:
        assert invisible in PACK + invisible
        assert invisible.strip() == invisible
        assert verify.norm(invisible) == ""


async def test_a_quote_transcribed_without_the_packs_invisible_hint_verifies():
    """THE ADMITTING HALF OF THE SAME FOLD, which is the half this boundary is paid for.

    A scraped body carrying a zero-width space inside a word made the pack and a correct
    transcription of it two different strings, so a genuine tag was dropped as `quote_unverified`
    -- the silent failure `norm()`'s own docstring is written against, arriving through the
    characters Python's whitespace class happens not to cover.
    """
    hinted = PACK.replace("the city", "the ci\u200bty")
    result = await run(tag(term="place.city", quote=PLAIN), packs={TITLE: hinted})

    assert terms(result) == ["place.city"]
    assert PLAIN not in hinted, "the pack must not already carry the quote verbatim"



# Values an adapter produces when it renders a structured evidence object as something that is
# not a span of prose: an offset, an index, a flag, the object itself. `render_pack` writes
# `[plot:1]`, `[<src>:<n>]` and a `# <name> (<year>)` header into every pack it builds, so the
# digits of a small integer and of a year are substrings of essentially every pack this app has.
NOT_TEXT = (1995, 1, 0, True, ["a slow burn"], {"span": "a slow burn"})


@pytest.mark.parametrize("quote", NOT_TEXT, ids=[type(q).__name__ for q in NOT_TEXT])
async def test_a_quote_that_is_not_text_is_a_missing_quote(quote):
    """DECISION 399. `norm()` takes `Any` and opens with `str(s)`, which is right for a fold --
    nothing a payload contains may make it raise -- and is not a reading of a type. So a JSON
    number reached rule 2 as the `str()` of itself and verified against the pack's own header:
    `{"term": "mood.bleak", "evidence": 1}` was KEPT, with `1` stored as the span it rests on.

    It is the refusal `_level` already makes one field over for a boolean (decision 394) -- a
    value whose JSON type is a category error is refused rather than coerced into a claim nobody
    made -- and it is not a minimum quote length, which the spec does not state and this boundary
    does not invent: `{"quote": "a"}` still passes, for the same reason `norm()` still coerces.
    """
    result = await run(tag(quote=quote))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema"]
    assert result.rejects[0].quote == str(quote)


def test_the_pack_this_file_writes_really_carries_those_digits():
    """The measurement under the test above: the refusal has to be the type check and not a
    substring test that happened to fail."""
    assert verify.norm("1995") in verify.norm(PACK)
    assert verify.norm("1") in verify.norm(PACK)


# The same category error one field over. `["slow burn"]` is the case that was KEPT: `str()` of it
# is "['slow burn']", `alias_key` strips brackets and quotes as surrounding punctuation, and this
# file's authored row answered the result.
NOT_A_TERM = (["slow burn"], ["mood.bleak"], ["mood.bleak", "place.city"], {"id": "mood.bleak"},
              7, True)


@pytest.mark.parametrize("term", NOT_A_TERM, ids=["list-alias", "list-term", "list-of-two", "dict",
                                                  "int", "bool"])
async def test_a_term_that_is_not_text_is_refused_rather_than_read_through_its_brackets(term):
    """Decision 399's question asked of the term. A one-element array reached `resolve` as the
    `str()` of itself and came out the other side as `pacing.slow_burn` with `repaired=True` -- a
    repair nobody made, and the only flag M5.5 has that the boundary rewrote anything -- while a
    two-element array of the same malformation dropped as `unknown_term`. §8 stage 7 says failures
    drop and are never repaired, so a value that is not text is refused under `schema` whatever its
    `str()` happens to fold to, and the value is still recorded for §6.6 to show.
    [M5.4 review cycle 3, M54-DIM3-C3-05]
    """
    result = await run(tag(term=term))

    assert result.tags[TITLE] == []
    assert reasons(result) == ["schema"]
    assert result.rejects[0].term == str(term)
    assert result.rejects[0].quote == PLAIN



# The two spellings of text a Postgres `text` column cannot hold, both of which `json.loads`
# accepts from a provider and both of which `norm()` and `str.strip()` preserve: a NUL escape,
# which the server refuses (22021), and a lone surrogate escape, which UTF-8 cannot encode at
# all. `api/auth.py:37` and `api/admin.py:68` exist because this app has already met the first
# at its HTTP edge (sec-04), and a provider payload reaches no such edge.
NUL = "mood.bl\x00eak"
SURROGATE = "mood.bl\ud800eak"


async def test_text_postgres_cannot_store_is_refused_before_anything_binds_it():
    """DECISION 397 AT THE BOUNDARY, AND THE LAST TWO ASSERTIONS ARE THE POINT.

    A term the vocabulary cannot resolve is bound into `dna_adjudication`'s query, so one NUL in
    one term raised out of `verify_payload` itself -- no tag kept, no refusal recorded, a paid
    pass returned as a stack trace, which is the failure decision 392's `OverflowError` arm was
    written to close arriving through the check above it. The same two strings then take
    `record_rejects`' atomic `executemany` down and lose every other refusal in the pass.

    Refusing here is what keeps the row worth reading: the field that CAN be stored still is, so
    a reviewer sees the quote a refused term rested on and the term a refused quote was offered
    for, rather than a row with both columns blank.
    """
    result = await run(
        tag(term=NUL), tag(quote=NUL), tag(term=SURROGATE), tag(quote=SURROGATE),
        tag(term="place.city"),
    )

    assert terms(result) == ["place.city"]
    assert reasons(result) == ["schema"] * 4
    assert [r.term for r in result.rejects] == [None, "mood.bleak", None, "mood.bleak"]
    assert [r.quote for r in result.rejects] == [PLAIN, None, PLAIN, None]


async def test_no_untrusted_value_raises_out_of_the_boundary():
    """DECISION 392'S SECOND HALF, over the four shapes that used to abort a whole pass.

    A raise is not a drop. It records nothing, never reaches `record_rejects`, and takes every
    OTHER tag and every other refusal in the same payload with it -- so a paid call over
    twenty-eight tags returned no verdict at all because one field was a space. The two good tags
    surviving beside the four bad ones is the whole assertion.
    """
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
    """The first tag never became a tag, so the second is the first of its term. Adding to the
    seen set before the remaining checks would turn one bad tag into two rejections and hide the
    good one."""
    result = await run(tag(term="place.city", quote=FABRICATED), tag(term="place.city"))

    assert terms(result) == ["place.city"]
    assert reasons(result) == ["quote_unverified"]


async def test_n_seen_counts_every_tag_the_boundary_looked_at():
    result = await run(tag(), tag(term="nope.nope"), tag(quote=FABRICATED), "junk")

    assert result.n_seen == 4
    assert result.n_kept == 1


# --- decision 392: no tag leaves this function unaccounted for -------------------------------


@pytest.mark.parametrize("second", ["07", " 7 ", "+7", 7])
async def test_two_payload_keys_naming_one_title_lose_no_tag(second):
    """A payload is untrusted about its KEYS as well as about its values, and `int()` reads every
    spelling above as 7.

    `res.tags[tid] = kept` assigned rather than accumulated, so the second key's list replaced the
    first's: two tags that had passed all three checks against a real pack vanished into neither
    `tags` nor `rejects`. That is decision 341's named failure -- a drop that leaves no row --
    inverted onto GOOD data, which makes it invisible in any rejection count, and it is the
    "losing a whole batch to a field's shape" reading the key-alias tolerance was measured into
    existence to prevent, arriving one level up at the title key.
    """
    result = await verify.verify_payload(
        {"titles": {"7": [tag(term="place.city"), tag(term="mood.bleak")],
                    second: [tag(term="visual.neon")]}},
        pass_id="p1", voc=VOC, packs={TITLE: PACK},
    )

    assert sorted(terms(result)) == ["mood.bleak", "place.city", "visual.neon"]
    assert reasons(result) == []
    assert result.n_seen == 3


async def test_a_term_repeated_under_a_second_spelling_of_one_title_key_is_recorded():
    """Accumulating rather than refusing the second key is what keeps the duplicate rule working
    across them: the repeat is dropped once, and the drop is a row."""
    result = await verify.verify_payload(
        {"titles": {"7": [tag(term="place.city")], "07": [tag(term="place.city")]}},
        pass_id="p1", voc=VOC, packs={TITLE: PACK},
    )

    assert terms(result) == ["place.city"]
    assert reasons(result) == ["duplicate"]


# `verify_payload`'s own reading of a title key, which the classifier below has to share for the
# one payload shape its proxy cannot see. Not a second reading: `int(raw_tid)` IS the module's
# reading, and a key it refuses reaches no title at all.
def _folds_to(key: object) -> int | None:
    try:
        return int(key)
    except (TypeError, ValueError):
        return None


# Payloads chosen for the arms they reach rather than for realism: every tag-level refusal, both
# orders of a colliding title key, one that collides two value SHAPES onto a single title, the
# values that used to raise, and the title-level arms that the identity is deliberately NOT
# stated over.
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
    """DECISION 392, AS A PROPERTY RATHER THAN AS A CASE.

    Every tag the boundary examined is either kept or recorded, so `n_seen` equals `n_kept` plus
    the TAG-LEVEL rejections. The scoping is not a hedge: a title refused whole is one rejection
    for the whole title and its tags are never examined, which `PassResult`'s own docstring states
    and `test_a_title_with_no_pack_is_refused_rather_than_passed` asserts -- so an identity
    written over every rejection would be false about payloads that are perfectly well handled,
    and an assertion that is false when nothing is wrong tells a reader nothing when something is.
    """
    result = await verify.verify_payload(
        payload, pass_id="p1", voc=VOC, packs={TITLE: PACK, 8: None},
    )
    # A title whose tags were examined has a list in `tags`, empty or not, because the tag loop
    # cannot run until one is there -- so a rejection naming a title this result holds no list
    # for is a title-level refusal by construction. THE CONVERSE IS FALSE, and the payload that
    # binds "7" to a list and "07" to a `str` is in `ACCOUNTED` above to hold it: three of the
    # four title-level arms are judged on the RESOLVED title and so cannot coexist with a list
    # for it, but `not isinstance(tags, list)` is judged per payload KEY, so that title reaches
    # the arm with its list already in hand and the refusal names a title this result DOES hold
    # a list for. Subtracting those, counted off the payload, is the narrowest honest
    # correction: the proxy stays and the one shape it cannot see comes off it, rather than the
    # module carrying a field for a test. Left in, it reddens a payload where nothing is
    # wrong -- the tag is kept, the refusal is a row -- and an inflated right-hand side is also
    # how a genuinely lost tag would read as green, which is the one failure decision 341
    # exists to make impossible.
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


# --- rule 1 of §4.1: a tag without its quote is unfalsifiable (exit check 9) ----------------


@pytest.mark.parametrize("quote", ["", *FOLDS_TO_NOTHING])
def test_a_verified_tag_cannot_be_constructed_without_its_quote(quote):
    """EXIT CHECK 9. The writer of a `dna_tag` row is M5.5's, so the refusal is placed at the
    constructor of the only object this module hands forward: a tag that does not carry the quote
    it was verified against cannot exist, and therefore cannot be written.

    THE MARKUP-ONLY SPELLINGS ARE WHY THE LIST MOVED TO `FOLDS_TO_NOTHING`. The type tested
    `str.strip()` while rule 2 tested `norm()`, so `VerifiedTag(quote="**")` built without
    complaint and the two halves of one promise were asking different questions of different
    functions (decision 392). They are one question now, and the two lists cannot drift apart
    because they are one list.
    """
    with pytest.raises(ValueError, match="unfalsifiable"):
        verify.VerifiedTag(
            term="mood.bleak", facet="mood", salience=2, source="", quote=quote,
        )



@pytest.mark.parametrize("quote", NOT_TEXT, ids=[type(q).__name__ for q in NOT_TEXT])
def test_a_verified_tag_cannot_be_constructed_from_a_quote_that_is_not_text(quote):
    """EXIT CHECK 9 AGAINST DECISION 399'S CLASS. The annotation said `str` and the constructor
    asked `norm()`, which stringifies -- so `VerifiedTag(quote=1995)` built, and the `.quote` it
    carried forward to M5.5's writer was an `int`. The refusal belongs here for named change 6's
    reason: the writer of a `dna_tag` row is M5.5's, so the only place M5.4 owns is the type it
    hands forward."""
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
    """DECISION 397 ON THE TYPE, WHERE IT HAD BEEN APPLIED TO EVERY WRITER BUT THIS ONE.

    `norm()` drops what prints nothing (decision 398), and U+0000 is Cc while a lone surrogate is
    Cs, so the fold reads each of these as a clean sentence -- the first assertion below is that
    hole, stated -- while the raw string the type carries forward is one `dna_evidence.quote`
    cannot hold. `verify_payload` refuses both before it ever builds a tag, which is exactly what
    made this the guard that held only for the caller that did not need it: M5.5 builds a
    `VerifiedTag` for itself, and one NUL in one quote of an `executemany` loses every GOOD tag in
    the batch with nothing to say they existed. [M5.4 review cycle 3, M54-C3-DIM2-02]
    """
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
    """Exit check 9 over the boundary's OUTPUT, asked in `norm()` because decision 392 is the rule.

    The assertion was `t.quote.strip()` over two well-formed tags, which is the predicate decision
    392 was taken against -- every quote offered below survives `str.strip()` -- asked of inputs
    that could not fail it, so it stayed green with both of that decision's guards deleted. The
    tags that fold to nothing ride along so the question has something to refuse, and the good
    two are asserted by name so an empty list cannot satisfy `all()`.
    [M5.4 review cycle 3, M54-C3-DIM2-04]
    """
    result = await run(
        tag(), tag(term="place.city", quote=TRANSCRIBED),
        *(tag(term="themes.robots", quote=q) for q in (*FOLDS_TO_NOTHING, *INVISIBLE)),
    )

    assert all(verify.norm(t.quote) for t in result.tags[TITLE])
    assert [t.term for t in result.tags[TITLE]] == ["mood.bleak", "place.city"]


# --- exit check 8: no repair path exists, asserted by reading the module ---------------------


def _bound_names(node: ast.AST) -> list[str]:
    """Every name one node binds, across every spelling Python has for binding one.

    A FIELD DECLARATION IS NOT A WRITE and is the one shape excluded: `term: str` inside
    `VerifiedTag` and `Rejection` is an `AnnAssign` with no value, which declares what a tag's
    term IS rather than changing one.

    THE LAST FIVE ARMS ARE NOT COMPLETENESS FOR ITS OWN SAKE. The guard above this one is scoped
    to one NAME, so its whole value is that no statement in `dna/verify.py` can bind `term` where
    it is not looking -- and a comprehension target, a `with ... as`, an `except ... as`, an
    `import ... as` and a nested parameter all bind a name while binding no `Name` node in a
    `Store` context that the four statement shapes reach. A guard with a blind spot is a guard a
    maintainer satisfies by accident.
    """
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
    """Every statement in one module that binds the name `term`."""
    tree = ast.parse(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        if "term" in _bound_names(node):
            hits.append(" ".join((ast.get_source_segment(source, node) or "").split())[:120])
    return hits


def test_no_statement_in_the_verifier_writes_a_term_outside_the_two_named_repairs():
    """EXIT CHECK 8, and the assertion the whole milestone turns on.

    §8 stage 7 says "Failures drop, never repaired", and the way that rule is lost is not by
    somebody arguing against it -- it is by a term one character off a real one looking like a
    typo worth fixing. So the module is read rather than trusted: exactly two statements bind a
    term, and both are named repairs that run BEFORE the vocabulary check. The resolve above it
    is the alias map and the prefix repair, neither of which rewrites a term body; the second is
    the curation ledger's own rename, which is the owner's decision and not the code's.
    """
    writes = _term_writes(SOURCE)

    assert len(writes) == 2, "\n".join(["a third statement writes a term:", *writes])
    assert "voc.resolve(" in writes[0], writes[0]
    assert "_renamed(" in writes[1], writes[1]


def test_the_only_thing_the_rename_wrapper_calls_is_the_curation_ledger():
    """`_renamed` is the second named write's whole body, so a rewrite hidden inside it would be
    a rewrite the guard above reports as one of the two it permits."""
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
    """The other spelling of a repair: `tag.term = corrected`, which binds no name at all and so
    is invisible to the guard above."""
    tree = ast.parse(SOURCE)
    hits = [
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and node.attr in ("term", "quote", "facet")
    ]

    assert hits == []


def test_every_verified_tag_is_built_from_the_term_the_checks_agreed_on():
    """A third repair could also arrive as an expression inside the constructor itself."""
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
        # The five the guard could not see, every one of which binds `term` while binding no
        # `Name` in a `Store` context that the shapes above reach. A body-rewriting repair placed
        # inside `Vocabulary.repair` -- the function already NAMED for repairs, and so the single
        # likeliest home for "a term one character off a real one looks like a typo to fix" --
        # passed all four static guards in this file while rescuing fabricated ids.
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
    """Every id one substitution, deletion or insertion away from this one.

    Capitals and the space are in the alphabet because a lowercase-only one could never write the
    head that let `Mood.slow_burn` through, and a property search that cannot generate the case it
    exists for is the example test again. [M5.4 review cycle 3, M54-DIM3-C3-01]
    """
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_. "
    out = set()
    for i in range(len(term_id)):
        out.add(term_id[:i] + term_id[i + 1:])
        out |= {term_id[:i] + c + term_id[i + 1:] for c in alphabet}
    for i in range(len(term_id) + 1):
        out |= {term_id[:i] + c + term_id[i:] for c in alphabet}
    return out - {term_id}


def test_the_prefix_repair_never_changes_a_body_under_any_single_character_edit():
    """THE BEHAVIOURAL HALF OF EXIT CHECK 8, STATED AS THE PROPERTY THE DOCSTRING CLAIMS.

    The static guards above assert what the module does not CONTAIN, and they are scoped to the
    name `term` -- so a rewrite placed inside `Vocabulary.repair`, which binds `term_id`, is
    invisible to every one of them. The example-based test beside this one offers twelve hand-
    picked ids, and any repair arm those twelve do not trigger walks straight past it too.

    So the property `repair`'s own docstring states -- "it only ever rewrites the PREFIX, never
    the term body" -- is asserted exhaustively instead, over every single-character edit of every
    term this file's vocabulary carries. It is cheap, it is green against the shipped module, and
    it goes red the day a near-miss repair lands, which is the only thing that makes the rule a
    rule rather than a paragraph.

    AND THE FACET CLAIM IS ASSERTED BESIDE THE BODY, because the body-only assertion is what let
    `Mood.slow_burn` become `pacing.slow_burn`: the body was preserved and the facet the extractor
    named was not. A head that case-folds to a declared facet is a claim about which facet the
    tag belongs to, so a repair that files it anywhere else is a violation of the same property.
    [M5.4 review cycle 3, M54-DIM3-C3-01]
    """
    declared = {facet.casefold() for facet in FACETS}
    violations = []
    for term_id in TERMS:
        for offered in _single_character_edits(term_id):
            got = VOC.repair(offered)
            if got is None:
                continue
            # Stripped first, as `repair` strips and as the example test above compares: the
            # space in the alphabet writes padding, and padding is not a body.
            if got.partition(".")[2] != offered.strip().partition(".")[2]:
                violations.append(f"{offered!r} -> {got!r} rewrote the body")
            named = offered.partition(".")[0].strip().casefold()
            if named in declared and TERMS[got].casefold() != named:
                violations.append(f"{offered!r} -> {got!r} left the facet it named")

    assert violations == [], "the repair broke its property:\n  " + "\n  ".join(violations[:20])


# --- the vocabulary of a rejection -----------------------------------------------------------


def test_reasons_is_the_ported_tuple_in_its_ported_order():
    """Ported verbatim from `mdc/dna/store.py:60-61`. The order is not decoration: `0027`'s CHECK
    lists it in the same order, and §6.6's filter reads the same seven."""
    assert verify.REASONS == (
        "schema", "unknown_term", "adjudicated", "quote_unverified", "unknown_title",
        "no_pack", "duplicate",
    )


def test_the_reject_store_constrains_itself_to_exactly_the_ported_reasons():
    """The tuple and the CHECK are two copies of one closed set, and a set that is closed in the
    code and open in the schema is not closed. Decision 341 makes the closure the point: §6.6's
    filter has a fixed vocabulary, and a spike in one reason is readable as itself."""
    sql = MIGRATION.read_text(encoding="utf-8")
    clause = re.search(r"rule_violated\s+IN\s*\((.*?)\)", sql, re.DOTALL)

    assert clause is not None, "0027 no longer constrains rule_violated"
    assert tuple(re.findall(r"'([a-z_]+)'", clause.group(1))) == verify.REASONS



def _weight_guard():
    spec = importlib.util.spec_from_file_location("dna_weight_guard_under_read", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migrations_claim_about_the_weight_guard_is_the_claim_the_guard_supports():
    """DECISION 401. `0027` said "no `WHERE confidence > x` anywhere", enforced over SQL and
    Python by test_landmine_guards.py" -- and what that guard enforces is COMPARISONS. Two shapes
    select rows on a weight without comparing it to anything, and both are natural at the screen
    `dna_reject` exists for: `WHERE salience IS NOT NULL`, which over `dna_tagged` deletes the
    entire projected tier because `0004_dna.sql:127` emits `NULL::real AS salience` for it, and
    the Python `if row["confidence"]:`, which `_COMPARISON_OPS` cannot see because it carries no
    `Is`/`IsNot` and the scan walks only `ast.Compare`.

    Widening the guard is not this milestone's to do and no offending code exists today, so the
    honest repair is the sentence: the migration now claims what is enforced and names the two
    blind spots as owed against M5.6, which is the milestone that will write the screen and stand
    on the claim. The positive controls are here so this cannot pass by loading a guard that no
    longer works, and the two negative ones go red the day somebody widens it -- which is when
    `0027`'s paragraph has to be read again.
    """
    guard = _weight_guard()
    sql = MIGRATION.read_text(encoding="utf-8")

    assert guard._weight_filters("SELECT term FROM dna_tag WHERE confidence > 0.5")
    assert guard._python_weight_comparisons("keep = [r for r in rows if r['confidence'] > 0.5]")
    assert guard._weight_filters("SELECT term FROM dna_tagged WHERE salience IS NOT NULL") == []
    assert guard._python_weight_comparisons("keep = [r for r in rows if r['confidence']]") == []
    assert "WHERE salience IS NOT NULL" in sql, (
        "0027 has to name the first blind spot, or its paragraph claims an enforcement the "
        "guard does not perform"
    )
    assert 'if row["confidence"]:' in sql, (
        "0027 has to name the second blind spot; the Python arm is the half a reader is least "
        "likely to check"
    )


def test_the_pipeline_marker_is_ported_with_its_reason():
    """Recorded per title so a run planner can tell tags produced by an earlier configuration
    from current ones - an older pipeline's output is stale evidence, not merely older."""
    assert verify.PIPELINE == "library/v1"


def test_no_provider_client_is_imported_by_the_trust_boundary():
    """§9's separation, asserted as a fact about imports rather than left to convention. The
    corpus keeps it the same way: `mdc/dna/store.py` imports its adjudication ledger, its packs,
    its similarity layer and its vocabulary, and does not import its LLM client at all."""
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
