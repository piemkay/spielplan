"""The trust boundary against a real pack, and what it refuses. Spec v2.1 §8 stage 7, §6.6, §4.1.

THIS FILE IS THE MILESTONE'S CENTREPIECE and the app's version of a measurement §8 makes about the
corpus: "100% catch rate on fabricated tags in the corpus pilot" (`spec:387`). The pack is not a
string written at the top of a test -- it is built by `dna/packs.py` out of `review_store.review`
rows, stored through M5.1's raw store and read back out of `dna_pack`, which is the only way the
claim means anything: a boundary tested against a pack a test wrote is a boundary that has never
met the thing it judges. `test_dna_verify.py` is the other half and takes the rules one at a time.

A NEGATIVE CONTROL NEEDS A POSITIVE ONE. The pilot's negative control was plausible tags carrying
invented quotes; a verifier that refused everything would score 100% against it and be worthless.
So the payload below carries four tags that are true of this title beside the twenty-four that are
not, and the measurement is both halves: every fabrication caught AND every genuine tag kept.

THE FIXTURE BUNDLE CANNOT SUPPLY THE PACK AND THAT IS A MEASUREMENT, NOT A REASON TO SKIP
(decision 391). It ships three `review_store.review` rows of 10, 6 and 1 words -- all three under
`packs.MIN_WORDS`, none from `rottentomatoes` -- so on the shipped rows the pack is a header and
nothing else and no quote could verify against it. The rows are therefore written here, and
`test_dna_packs.py` carries the fixture's own three verbatim and asserts they reach nothing.

§4.1 RULE 2 IS PINNED HERE BEFORE THE SCREEN THAT COULD BREAK IT EXISTS. §6.6 promises "review of
DNA rejects and low-evidence tags" and decision 341 settles what the second half may mean: an
ORDER BY on ascending confidence, never a WHERE. The two queries M5.6's screen will run are
written out below and executed, so the shape is a fact in the tree rather than an intention.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import re

import asyncpg
import pytest

from spielplan.core.config import settings
from spielplan.dna import adjudicate, packs, verify
from spielplan.dna.norm import norm

V1 = "v1"
TITLE = 1

# The eleven facets §6.4 declares, with the `ord` §6.8's palette reads them in. All eleven rather
# than the five this file names terms in, because `dna_term` foreign-keys `(version, facet)` and a
# vocabulary missing facets is not the vocabulary the repair's uniqueness rule is asked about.
FACETS = (
    "mood", "themes", "pacing", "structure", "visual",
    "sound", "characters", "place", "era", "sensibility", "register",
)

# Eight terms, including `themes.robots` -- which IS a real vocabulary-v1 term
# (`artifacts/dna_vocab/v1/vocab_v1_all.tsv:166`), and §8.4's "zero owned titles carry
# themes.robots" is a statement about coverage rather than about a term the vocabulary lacks. The
# package docstring argues that at length; it is seeded here so the file a reader lands in next
# does not quietly contradict it.
TERMS = {
    "mood.bleak": "mood",
    "pacing.slow_burn": "pacing",
    "place.city": "place",
    "visual.neon": "visual",
    "themes.heist": "themes",
    "themes.robots": "themes",
    "characters.professional": "characters",
    "structure.parallel_leads": "structure",
}

# Three reviews over three sources, every one clearing `packs.MIN_WORDS` -- asserted below rather
# than asserted here, because `word_count` is a GENERATED column and what the floor reads is
# Postgres's count and not one this file did in its head. They carry the markup their sources
# published, which is what makes the fold testable at all.
REVIEWS = (
    (
        "trakt",
        "The film is a **slow burn** that never raises its voice. Neon on wet asphalt, a crew "
        "that works at night, and the city's own rhythm carrying every scene it is given. "
        "[spoiler]The heist goes wrong[/spoiler] in the last reel and the picture does not "
        "flinch from any of it. Patient, bleak, and entirely sure of itself from the opening "
        "frame onward.",
        False,
    ),
    (
        "metacritic",
        "Two professionals on either side of the law, shot in parallel until the parallel is "
        "the point of the whole exercise. The city is the third lead and it is photographed "
        "like one, all cold glass and longer lenses than anyone else was using that year. "
        "Nothing here is hurried and nothing here is wasted, which is rarer than it sounds.",
        True,
    ),
    (
        "letterboxd",
        "I keep coming back to how little anyone in this film explains themselves to anybody "
        "else. The work is the character, the crew is the family, and the coffee shop scene "
        "says everything that two hours of dialogue could not have said half as well. A bleak "
        "picture that respects you enough to let you sit in it for a while.",
        False,
    ),
)

# Spans that are genuinely in the pack, three of them transcribed the way a reader transcribes
# one: the emphasis markers dropped, the spoiler tags dropped, the apostrophe curled. EXIT CHECK
# 3 is those three, and it is one of the two cases a strict port loses. The fourth is a plain
# literal substring and is here as the control -- it is what the other three would look like if
# the fold were doing nothing, which is a difference a test has to be able to see.
#
# NOTE WHERE THE SPOILER TAGS SIT IN THE REVIEW ABOVE: they interrupt the span rather than
# surrounding it. A tag wrapped neatly around a whole sentence leaves that sentence a literal
# substring of the pack, so a quote taken from it verifies with or without the fold and asserts
# nothing -- which is exactly what the first draft of this file did, and the measurement below is
# what caught it.
GENUINE = (
    ("pacing.slow_burn", "a slow burn that never raises its voice"),
    ("visual.neon", "Neon on wet asphalt"),
    # Spelled as an escape, not as the character: CLAUDE.md keeps test output ASCII because a
    # Windows cp1252 console cannot encode this one, and a failure here prints the source line.
    ("place.city", "the city\u2019s own rhythm carrying every scene"),
    ("themes.heist", "The heist goes wrong in the last reel"),
)

# Twelve ids that read exactly like vocabulary terms and are not in it. Every tail is absent from
# the seeded vocabulary, so none of them is rescuable by the prefix repair -- which the test
# asserts rather than assumes, because a "catch" that was really a refusal for a different reason
# would measure something else.
FABRICATED_TERMS = (
    "themes.time_travel", "mood.whimsical", "pacing.breakneck", "visual.handheld",
    "sound.silence", "characters.ensemble_cast", "place.desert", "era.nineties",
    "structure.flashback", "register.camp", "sensibility.earnest", "themes.surveillance",
)

# Twelve quotes in the register of the reviews above and absent from all three. This is the
# pilot's negative control in miniature: plausible tags with invented quotes, which the substring
# test alone caught at 100%.
FABRICATED_QUOTES = (
    "a slow burn that never raises its fists",
    "neon on dry asphalt",
    "the town's own rhythm carrying every scene",
    "the heist goes right in the last reel",
    "a crew that works at dawn",
    "two amateurs on either side of the law",
    "the city is the fourth lead",
    "all warm glass and shorter lenses",
    "everything here is hurried",
    "the diner scene says everything",
    "a hopeful picture that respects you",
    "the work is the family, the crew is the character",
)


@pytest.fixture
def raw_root(tmp_path, monkeypatch):
    """A raw store of this test's own, reached the way the worker reaches it.

    `test_acquire_rawstore.py`'s idiom, for its reason: `settings().raw_dir` takes no argument on
    purpose, so the root is set through `DATA_DIR` and the `lru_cache` is cleared on both sides.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().raw_dir
    settings.cache_clear()


async def seed(conn) -> None:
    """One title, one vocabulary, three reviews and one authored alias row."""
    await conn.execute(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, 'movie', $2, $3, true)",
        TITLE, "Heat", 1995,
    )
    await conn.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, $2, $3)",
        V1, len(FACETS), len(TERMS),
    )
    await conn.executemany(
        "INSERT INTO dna_facet (version, facet, ord) VALUES ($1, $2, $3)",
        [(V1, facet, n) for n, facet in enumerate(FACETS, start=1)],
    )
    await conn.executemany(
        "INSERT INTO dna_term (version, term, facet) VALUES ($1, $2, $3)",
        [(V1, term_id, facet) for term_id, facet in TERMS.items()],
    )
    await conn.execute(
        "INSERT INTO dna_alias (version, alias, term) VALUES ($1, $2, $3)",
        V1, "slow burn", "pacing.slow_burn",
    )
    await conn.executemany(
        "INSERT INTO review_store.review (title_id, source, body, is_critic) "
        "VALUES ($1, $2, $3, $4)",
        [(TITLE, source, body, critic) for source, body, critic in REVIEWS],
    )


async def install(conn) -> tuple[verify.Vocabulary, dict[int, str | None]]:
    """Seed, build the title's pack, put it in custody, and read back what stage 7 would read.

    The pack is read back out of `dna_pack` and the raw store rather than kept from `build_pack`'s
    return value, because that round trip is the thing decision 382 exists for: a verification
    that cannot be reproduced against the text it was made from is not auditable, and §6.6's
    reject review cannot explain a single refusal.
    """
    await seed(conn)
    text, info = await packs.build_pack(conn, TITLE)
    await packs.store_pack(conn, TITLE, V1, text, info)
    voc = await verify.load_vocabulary(conn)
    return voc, await verify.read_packs(conn, [TITLE], V1)


def payload(*tags: dict[str, object]) -> dict[str, object]:
    return {"titles": {str(TITLE): list(tags)}}


async def verdicts(conn, rejects, **kw) -> list[dict]:
    """Record the refusals and read the rows back as `dna_reject` holds them."""
    await verify.record_rejects(conn, rejects, **kw)
    rows = await conn.fetch("SELECT * FROM dna_reject ORDER BY id")
    return [dict(row) for row in rows]


# --- the pack these verdicts are reached against --------------------------------------------


async def test_every_seeded_review_clears_the_pack_floor(db):
    """The measurement the fixture bundle cannot supply (decision 391), taken against Postgres's
    own generated `word_count` rather than against a count this file did by eye. If one of these
    fell under 50 the pack would silently lose it and half this file would assert nothing."""
    await seed(db)
    counts = await db.fetch(
        "SELECT source, word_count FROM review_store.review WHERE title_id = $1 ORDER BY source",
        TITLE,
    )

    assert len(counts) == len(REVIEWS)
    assert all(row["word_count"] >= packs.MIN_WORDS for row in counts), [
        (row["source"], row["word_count"]) for row in counts
    ]


async def test_the_pack_is_read_back_out_of_custody_and_carries_its_markup(db, raw_root):
    """Decision 382's round trip, and the reason folding must happen at comparison time: the pack
    in custody still carries `**`, `[spoiler]` and the straight apostrophe its sources published.
    A builder that tidied them would make every quote transcribed across them unverifiable."""
    _voc, pack_text = await install(db)
    text = pack_text[TITLE]

    assert "**slow burn**" in text
    assert "[spoiler]" in text
    assert "city's own rhythm" in text
    assert await db.fetchval("SELECT pack_sha FROM dna_pack WHERE title_id = $1", TITLE) == (
        packs.sha(text)
    )


async def test_a_pack_row_whose_bytes_are_not_its_pack_raises_rather_than_verifying(db, raw_root):
    """The corpus's scar in this app's shape: its ingest derived the sha from whatever pack was on
    disk, which marked 825 titles current against a pack no pass had seen and hid 652. Answering
    `no_pack` here would be worse than raising, because it reads as a title nobody has packed
    rather than as an install verifying quotes against the wrong evidence."""
    await install(db)
    await db.execute("UPDATE dna_pack SET pack_sha = 'not-the-pack' WHERE title_id = $1", TITLE)

    with pytest.raises(OSError, match="wrong evidence"):
        await verify.read_pack(db, TITLE, V1)


async def test_a_title_this_install_has_never_packed_has_no_pack(db, raw_root):
    await install(db)

    assert await verify.read_pack(db, 999, V1) is None


# --- the measurement: 100% catch rate, with a positive control ------------------------------


async def test_no_fabricated_term_is_rescuable_by_any_repair_this_module_has(db, raw_root):
    """The measurement below counts catches, so the fabrications have to be genuinely absent --
    otherwise a "catch" could be a refusal for some other reason and the rate would be measuring
    the wrong thing. Asserted against the real loaded vocabulary and the real alias row."""
    voc, _packs = await install(db)

    assert [t for t in FABRICATED_TERMS if t in voc] == []
    assert [t for t in FABRICATED_TERMS if voc.resolve(t) is not None] == []
    assert voc.resolve("slow burn") == "pacing.slow_burn", (
        "the alias row must be live, or this file measures a vocabulary with no repair at all"
    )


async def test_every_fabricated_tag_in_a_schema_valid_payload_is_caught(db, raw_root):
    """EXIT CHECKS 1 AND 2, AND THE MILESTONE'S CENTRAL MEASUREMENT.

    Twenty-eight tags, every one schema-valid: four true of this title, twelve naming a term the
    vocabulary does not carry, twelve quoting a sentence no source wrote. §8 claims a 100% catch
    rate on fabricated tags in the corpus pilot; this is the app's version of that number, and it
    is asserted as 100% rather than as "most", because a boundary that catches most fabrications
    is a boundary whose output still cannot be trusted without reading it.
    """
    voc, pack_text = await install(db)
    tags = (
        [{"term": term_id, "quote": quote} for term_id, quote in GENUINE]
        + [{"term": term_id, "quote": GENUINE[0][1]} for term_id in FABRICATED_TERMS]
        + [{"term": "mood.bleak", "quote": quote} for quote in FABRICATED_QUOTES]
    )

    result = await verify.verify_payload(
        payload(*tags), pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )

    assert result.n_seen == 28
    kept = {t.term for t in result.tags[TITLE]}
    assert kept == {term_id for term_id, _ in GENUINE}, (
        "the positive control failed, so the catch rate below would be measuring a verifier that "
        "refuses everything"
    )

    fabricated = len(FABRICATED_TERMS) + len(FABRICATED_QUOTES)
    caught = [r for r in result.rejects if r.reason in ("unknown_term", "quote_unverified")]
    assert len(caught) / fabricated == 1.0
    assert sorted({r.reason for r in caught}) == ["quote_unverified", "unknown_term"]
    assert [r.term for r in caught if r.reason == "unknown_term"] == list(FABRICATED_TERMS)


async def test_not_one_refused_tag_reaches_the_extracted_tier(db, raw_root):
    """"Failures drop, never repaired" is about `dna_tag`, so `dna_tag` is what is counted. The
    extracted tier stays empty because this milestone writes no tags at all -- the writer is
    M5.5's -- and that is the strongest form of the assertion available here."""
    voc, pack_text = await install(db)
    tags = [{"term": term_id, "quote": GENUINE[0][1]} for term_id in FABRICATED_TERMS]

    result = await verify.verify_payload(
        payload(*tags), pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )
    rows = await verdicts(db, result.rejects, provider="anthropic:sonnet")

    assert await db.fetchval("SELECT count(*) FROM dna_tag") == 0
    assert len(rows) == len(FABRICATED_TERMS)


async def test_every_refusal_is_written_down_with_the_rule_it_broke(db, raw_root):
    """Decision 341, and the sentence §6.6 and §8 stage 7 are only both true together with: a tag
    that drops and is never written cannot be reviewed. The row carries what a person needs to
    judge it -- the term, the facet it would have had, the level it claimed and the quote it
    claimed to rest on -- because a reviewer with a rule name and nothing else cannot.

    THE LEVEL IS ON EVERY ROW THAT KNOWS ONE, WHICH IS DECISION 400. The salience assertion below
    read `[None, None, 9]` against a payload whose first two tags state 3 and 1: the level was
    read after every other arm had already appended its rejection, so the one row that recorded a
    level was the row where the level is itself the rule that broke -- the row a reviewer needs it
    least on, the rule name having said it. `unknown_term` and `quote_unverified` are what a
    reviewer actually triages, and on both the claim the provider made was discarded. `facet` is
    still NULL on the first row and that is not the same defect: a facet is not knowable for a
    term the vocabulary does not carry, and a stated level always is.
    """
    voc, pack_text = await install(db)
    result = await verify.verify_payload(
        payload(
            {"term": "themes.time_travel", "quote": GENUINE[0][1], "salience": 3},
            {"term": "mood.bleak", "quote": "a sentence nobody wrote", "salience": 1},
            {"term": "place.city", "quote": GENUINE[2][1], "salience": 9},
        ),
        pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )
    rows = await verdicts(db, result.rejects, run_id=42, provider="anthropic:sonnet")

    assert [row["rule_violated"] for row in rows] == ["unknown_term", "quote_unverified", "schema"]
    assert [row["term"] for row in rows] == ["themes.time_travel", "mood.bleak", "place.city"]
    assert [row["facet"] for row in rows] == [None, "mood", "place"]
    assert [row["salience"] for row in rows] == [3, 1, 9]
    assert rows[1]["quote"] == "a sentence nobody wrote"
    assert {row["run_id"] for row in rows} == {42}
    assert {row["provider"] for row in rows} == {"anthropic:sonnet"}
    assert {row["title_id"] for row in rows} == {TITLE}


async def test_the_reject_store_refuses_a_rule_nobody_declared(db, raw_root):
    """REASONS is a closed set and `0027` makes the schema hold it closed. A screen that filters
    on the rule name (§6.6) is only meaningful while nothing can write a name it does not know."""
    await install(db)
    invented = verify.Rejection(TITLE, "pilot", "mood.bleak", "looked_wrong")

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await verify.record_rejects(db, [invented])


async def test_recording_nothing_writes_nothing(db, raw_root):
    await install(db)

    assert await verify.record_rejects(db, []) == 0
    assert await db.fetchval("SELECT count(*) FROM dna_reject") == 0


async def test_a_refusal_naming_a_title_this_install_does_not_hold_is_written_with_no_title(
    db, raw_root
):
    """`0027` makes `dna_reject.title_id` nullable and says exactly why: "`unknown_title` is one
    of the seven reasons: a payload naming a title this install does not hold is refused, and the
    refusal is the row".

    `verify_payload` carries the payload's OWN id -- `int()` over an untrusted key -- and the
    column keeps its foreign key, so the row the migration describes was the one row that could
    never be written. The reason the split exists is that a person reading the reject review must
    be able to tell a provider inventing a title from an install that has not packed one, and a
    reason nothing can record answers neither question.
    """
    voc, pack_text = await install(db)
    result = await verify.verify_payload(
        {"titles": {"4242": [{"term": "mood.bleak", "quote": GENUINE[0][1]}]}},
        pass_id="pilot", voc=voc, packs=pack_text, ledger=db, allowed=[TITLE],
    )
    rows = await verdicts(db, result.rejects)

    assert await db.fetchval("SELECT count(*) FROM title WHERE id = 4242") == 0
    assert [row["rule_violated"] for row in rows] == ["unknown_title"]
    assert rows[0]["title_id"] is None


async def test_one_unwritable_title_id_does_not_discard_the_rest_of_the_passs_refusals(
    db, raw_root
):
    """`executemany` is atomic in asyncpg -- either every row lands or none does -- so a single
    transposed digit in a provider's title id discarded the whole pass's refusal record and
    handed the caller an exception instead of a count.

    That is decision 341 defeated by the cheapest possible provider mistake: the install's record
    of what it refused says the pass never happened, and §6.6's review shows an install that
    dropped nothing. It is also the "losing a whole batch" failure the module's key-alias comment
    is written against, arriving at the write rather than at the read -- and `_SMALLINT` already
    guards the identical door one column over, for the identical reason.
    """
    voc, pack_text = await install(db)
    result = await verify.verify_payload(
        {"titles": {
            str(TITLE): [
                {"term": "themes.time_travel", "quote": GENUINE[0][1]},
                {"term": "mood.bleak", "quote": "a sentence nobody wrote"},
            ],
            "4242": [{"term": "mood.bleak", "quote": GENUINE[0][1]}],
        }},
        pass_id="pilot", voc=voc, packs=pack_text, ledger=db, allowed=[TITLE],
    )
    rows = await verdicts(db, result.rejects, provider="anthropic:sonnet")

    assert [row["rule_violated"] for row in rows] == [
        "unknown_term", "quote_unverified", "unknown_title",
    ]
    assert [row["title_id"] for row in rows] == [TITLE, TITLE, None]



async def test_a_term_postgres_cannot_store_never_reaches_the_curation_ledger(db, raw_root):
    """DECISION 397, AND THE LEDGER IS WHY THIS IS A REFUSAL AND NOT ONLY A GUARD AT THE WRITE.

    `ledger=db` is the production shape -- `verify_payload`'s own docstring describes it and every
    call in this file passes it -- and a term the vocabulary cannot resolve is bound straight into
    `dna_adjudication`'s query as `$2`. So one NUL escape in one term raised
    `CharacterNotInRepertoireError` out of `verify_payload` ITSELF: no tag kept, no refusal
    recorded, a paid pass returned as a stack trace. `json.loads` accepts the escape and `norm()`
    and `str.strip()` both preserve it, so nothing upstream of this function removes it, and
    decision 392's own property test cannot see it: that test runs with no ledger at all, which is
    the one configuration in which no untrusted string reaches a database.

    The tag that survives is the assertion. A refusal that took the other two tags with it is
    indistinguishable from a provider that returned nothing.
    """
    voc, pack_text = await install(db)
    result = await verify.verify_payload(
        payload(
            {"term": "mood.bl\x00eak", "quote": GENUINE[0][1], "salience": 3},
            {"term": "themes.time_travel", "quote": GENUINE[0][1], "salience": 2},
            {"term": "pacing.slow_burn", "quote": GENUINE[0][1]},
        ),
        pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )
    rows = await verdicts(db, result.rejects, provider="anthropic:sonnet")

    assert [t.term for t in result.tags[TITLE]] == ["pacing.slow_burn"]
    assert [row["rule_violated"] for row in rows] == ["schema", "unknown_term"]
    assert [row["term"] for row in rows] == [None, "themes.time_travel"]
    assert [row["quote"] for row in rows] == [GENUINE[0][1], GENUINE[0][1]], (
        "the field that CAN be stored still is, or the row records only that something happened"
    )
    assert [row["salience"] for row in rows] == [3, 2]


async def test_one_unwritable_string_does_not_discard_the_rest_of_the_passs_refusals(
    db, raw_root
):
    """The other half of decision 397, at the write, where `Rejection` is a public type and
    `record_rejects` takes any iterable of them.

    `executemany` is atomic, so a term or a quote carrying a NUL or an unpaired surrogate
    discarded every genuine refusal in the same call and handed the caller an exception instead of
    a count -- decision 396's failure arriving through the two columns that decision did not
    guard, and decision 341's named failure exactly: a drop that leaves no row. M5.5 builds
    `Rejection`s of its own, which is why the guard is here as well as at the boundary: an
    invariant that holds because one caller happens to keep it is a property of that caller.
    """
    await install(db)
    refusals = [
        verify.Rejection(TITLE, "pilot", "themes.time_travel", "unknown_term",
                         quote="a slow burn that never raises its fists"),
        verify.Rejection(TITLE, "pilot", "mood.bl\x00eak", "unknown_term",
                         quote="neon on dry asphalt"),
        verify.Rejection(TITLE, "pilot", "place.city", "quote_unverified",
                         quote="neon on dry\ud800asphalt"),
        verify.Rejection(TITLE, "pilot", "visual.neon", "duplicate",
                         quote="Neon on wet asphalt"),
    ]
    rows = await verdicts(db, refusals, provider="anthropic:sonnet")

    assert len(rows) == len(refusals)
    assert [row["term"] for row in rows] == [
        "themes.time_travel", None, "place.city", "visual.neon",
    ]
    assert [row["quote"] for row in rows] == [
        "a slow burn that never raises its fists", "neon on dry asphalt", None,
        "Neon on wet asphalt",
    ]
    assert [row["rule_violated"] for row in rows] == [
        "unknown_term", "unknown_term", "quote_unverified", "duplicate",
    ]


async def test_a_level_the_column_cannot_hold_does_not_discard_the_rest_either(db, raw_root):
    """`_SMALLINT`'s own comment names this failure -- a value that "would otherwise take the
    whole `executemany` down with a range error and lose every OTHER refusal in the same run" --
    and the guard that answers it was applied inside `verify_payload` only, so the invariant the
    writer relies on was a property of one producer (decision 397).

    A boolean is the quieter half of the same column. `isinstance(True, int)` is true, so `True`
    passed the width test and asyncpg wrote a 1: a level no provider stated, in the column
    decision 386 exists to keep faithful to what one did.
    """
    await install(db)
    refusals = [
        verify.Rejection(TITLE, "pilot", "mood.bleak", "schema", salience=3,
                         quote="a bleak picture that respects you"),
        verify.Rejection(TITLE, "pilot", "place.city", "schema", salience=32768,
                         quote="the city is the third lead"),
        verify.Rejection(TITLE, "pilot", "visual.neon", "schema", salience=True,
                         quote="Neon on wet asphalt"),
        verify.Rejection(TITLE, "pilot", "themes.heist", "schema", salience=0,
                         quote="The heist goes wrong in the last reel"),
    ]
    rows = await verdicts(db, refusals)

    assert len(rows) == len(refusals)
    assert [row["salience"] for row in rows] == [3, None, None, 0], (
        "a number a reviewer cannot see beats a number nobody stated, and neither may cost the "
        "pass its other refusals"
    )


# --- the fold, against a pack that was really built (exit check 3) --------------------------


@pytest.mark.parametrize("index", range(len(GENUINE)))
async def test_a_quote_transcribed_across_the_packs_markup_verifies(db, raw_root, index):
    """EXIT CHECK 3, against the real thing rather than against a string in a test.

    Three of the four differ from the pack by markup alone: the emphasis markers, the BBCode
    spoiler tags, and a curled apostrophe where the source wrote a straight one. `norm()` is
    applied to BOTH sides and to nothing else -- the pack in custody is untouched, and the quote
    stored on the tag is the one the extractor produced.
    """
    voc, pack_text = await install(db)
    term_id, quote = GENUINE[index]

    result = await verify.verify_payload(
        payload({"term": term_id, "quote": quote}),
        pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )

    assert [t.term for t in result.tags[TITLE]] == [term_id]
    assert result.tags[TITLE][0].quote == quote
    assert norm(quote) in norm(pack_text[TITLE])


async def test_the_fold_is_what_carries_three_of_those_four_quotes(db, raw_root):
    """The measurement under the parametrised test above, taken rather than assumed. If all four
    were already literal substrings of the pack, that test would be green against a verifier with
    no fold at all -- and the three that are not are the three §8 stage 5 says the pack must keep
    the markup for."""
    _voc, pack_text = await install(db)
    literal = [quote for _term, quote in GENUINE if quote in pack_text[TITLE]]

    assert literal == ["Neon on wet asphalt"]


# --- the curation ledger reaching the boundary (exit checks 6 and 7) ------------------------


async def test_a_term_the_ledger_renames_passes_under_its_new_name(db, raw_root):
    """EXIT CHECK 6. A retired id the ledger re-points is not an unknown term -- it is a term the
    vocabulary renamed after the result file was written, and repairing it before the vocabulary
    check is what lets that file outlive a merge. Without it every ingest after a merge rejects
    the row and silently reverts the curation: measured at 496 rows, twice, on 2026-08-25."""
    voc, pack_text = await install(db)
    await db.execute(
        "INSERT INTO dna_adjudication (version, scope, term, verdict, target) "
        "VALUES ($1, 'global', 'themes.caper', 'rename', 'themes.heist')",
        V1,
    )

    result = await verify.verify_payload(
        payload({"term": "themes.caper", "quote": GENUINE[3][1]}),
        pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )

    assert [t.term for t in result.tags[TITLE]] == ["themes.heist"]
    assert result.tags[TITLE][0].facet == "themes"
    assert result.tags[TITLE][0].repaired is True


async def test_a_term_the_ledger_retires_is_recorded_as_adjudicated_and_not_as_garbage(
    db, raw_root
):
    """EXIT CHECK 7, and the split that earns its keep. A term the ledger DROPS is not an unknown
    term either -- it is a term the owner retired on the evidence, and counting the two together
    makes a routine retirement look like an extractor emitting garbage and hides the case that
    actually needs attention. Both are in one payload so the difference is the assertion."""
    voc, pack_text = await install(db)
    await db.execute(
        "INSERT INTO dna_adjudication (version, scope, term, verdict) "
        "VALUES ($1, 'global', 'themes.androids', 'drop')",
        V1,
    )

    result = await verify.verify_payload(
        payload(
            {"term": "themes.androids", "quote": GENUINE[0][1]},
            {"term": "themes.time_travel", "quote": GENUINE[0][1]},
        ),
        pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )
    rows = await verdicts(db, result.rejects)

    assert result.tags[TITLE] == []
    assert [row["rule_violated"] for row in rows] == ["adjudicated", "unknown_term"]
    assert rows[0]["term"] == "themes.androids", (
        "the term recorded is the one the payload offered; a rejection that renamed it would be "
        "a repair of the thing being rejected"
    )


async def test_a_rename_onto_a_term_the_vocabulary_does_not_carry_is_still_unknown(db, raw_root):
    """The ledger re-points, the vocabulary still does not know the target, and the rule is the
    vocabulary's: `term not in voc` is the check, and the ledger is a repair before it rather
    than an exemption from it."""
    voc, pack_text = await install(db)
    await db.execute(
        "INSERT INTO dna_adjudication (version, scope, term, verdict, target) "
        "VALUES ($1, 'global', 'themes.caper', 'rename', 'themes.long_con')",
        V1,
    )

    result = await verify.verify_payload(
        payload({"term": "themes.caper", "quote": GENUINE[3][1]}),
        pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )

    assert result.tags[TITLE] == []
    assert [r.reason for r in result.rejects] == ["unknown_term"]


@pytest.mark.parametrize("padding", ["  {}  ", "{}\n", "\t{}"], ids=["spaces", "newline", "tab"])
async def test_a_padded_term_reaches_the_ledger_as_the_vocabulary_reads_it(db, raw_root, padding):
    """EXIT CHECKS 6 AND 7 FOR THE SPELLING THE VOCABULARY ALREADY TOLERATES.

    `Vocabulary.resolve` strips its argument, so `"  mood.bleak  "` passes; the ledger was asked
    about the UNSTRIPPED string and matches `term = $2` exactly, while the importer stores the
    ledger's own terms stripped. So one value was read two ways inside one arm: a re-point that
    exists so a result file outlives a merge missed on a trailing newline and dropped the tag as
    `unknown_term`, and a curated retirement was recorded as `unknown_term` rather than
    `adjudicated` -- word for word the confusion `is_retired` exists to prevent.
    [M5.4 review cycle 3, M54-DIM3-C3-02]
    """
    voc, pack_text = await install(db)
    await db.execute(
        "INSERT INTO dna_adjudication (version, scope, term, verdict, target) "
        "VALUES ($1, 'global', 'themes.caper', 'rename', 'themes.heist'), "
        "($1, 'global', 'themes.androids', 'drop', NULL)",
        V1,
    )

    result = await verify.verify_payload(
        payload(
            {"term": padding.format("themes.caper"), "quote": GENUINE[3][1]},
            {"term": padding.format("themes.androids"), "quote": GENUINE[0][1]},
        ),
        pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )

    assert [t.term for t in result.tags[TITLE]] == ["themes.heist"]
    assert [r.reason for r in result.rejects] == ["adjudicated"]


async def test_the_ledger_is_only_consulted_for_a_term_the_vocabulary_does_not_carry(
    db, raw_root
):
    """The corpus's call site, and the reason the ledger read costs what it costs: a verdict on a
    term the vocabulary still carries is never reached, so the query count is the count of terms
    the vocabulary did not know and not the count of tags. A blanket retirement of a live term
    therefore changes nothing here, and removing the term from the vocabulary is what retires it.
    """
    voc, pack_text = await install(db)
    await db.execute(
        "INSERT INTO dna_adjudication (version, scope, term, verdict) "
        "VALUES ($1, 'global', 'mood.bleak', 'drop')",
        V1,
    )

    result = await verify.verify_payload(
        payload({"term": "mood.bleak", "quote": GENUINE[0][1]}),
        pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )

    assert [t.term for t in result.tags[TITLE]] == ["mood.bleak"]


async def test_a_drop_verdict_on_a_live_term_is_recorded_and_not_acted_on(db, raw_root):
    """The other half of the test above, stated rather than left to be discovered.

    `dna.adjudicate.is_retired` answers the ledger's question without asking the vocabulary first,
    so the package answers one question two ways depending on which door a caller knocks on --
    and `dna/adjudicate.py` is exported for M5.5 to knock on directly. The asymmetry is the port's
    arrangement and the cost of closing it is one ledger read per TAG, so it is a decision a
    milestone takes under its own number rather than a quiet edit to a call site. What is not
    acceptable is nobody being able to read it off the tree, which is what this asserts.
    """
    voc, pack_text = await install(db)
    await db.execute(
        "INSERT INTO dna_adjudication (version, scope, term, verdict) "
        "VALUES ($1, 'global', 'mood.bleak', 'drop')",
        V1,
    )

    assert await adjudicate.is_retired(db, "mood.bleak", TITLE, version=V1) is True
    result = await verify.verify_payload(
        payload({"term": "mood.bleak", "quote": GENUINE[0][1]}),
        pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )

    assert [t.term for t in result.tags[TITLE]] == ["mood.bleak"], (
        "the boundary passes a term the ledger retires, because the ledger is only consulted for "
        "a term the vocabulary does not carry; decision 163 makes removing it a migration"
    )


# --- §4.1 rule 1: every passed tag carries its quote (exit check 9) -------------------------


async def test_a_tag_that_carries_no_quote_is_refused_and_written_down(db, raw_root):
    """EXIT CHECK 9, at the payload. `0004_dna.sql:91-92` -- "a tag without its quote is
    unfalsifiable" -- and §4.1 rule 1 make this the one tag that could never be reviewed, so it
    is the one tag that must never be written. The constructor half of the same refusal is in
    `test_dna_verify.py`: the type cannot be built without a quote at all."""
    voc, pack_text = await install(db)
    result = await verify.verify_payload(
        payload({"term": "mood.bleak", "quote": ""}, {"term": "place.city"}),
        pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )
    rows = await verdicts(db, result.rejects)

    assert result.tags[TITLE] == []
    assert [row["rule_violated"] for row in rows] == ["schema", "schema"]
    assert await db.fetchval("SELECT count(*) FROM dna_evidence") == 0


async def test_every_tag_that_passes_carries_the_quote_it_passed_on(db, raw_root):
    voc, pack_text = await install(db)
    result = await verify.verify_payload(
        payload(*[{"term": t, "quote": q} for t, q in GENUINE]),
        pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )

    assert len(result.tags[TITLE]) == len(GENUINE)
    assert all(norm(tag.quote) for tag in result.tags[TITLE]), (
        "`norm()` and not `strip()`: the fold is the only reading of a quote this boundary has, "
        "and `**` survives a strip while carrying no evidence at all"
    )


# Twelve quotes that are not sentences at all: the markup and punctuation `norm()` removes, with
# nothing behind it. Against a real pack these were the OPPOSITE of the fabrications above -- not
# caught at 100% but passed at 100%, because each folds to "" and "" is a substring of every pack.
# The pilot's negative control never reached this class, which is why the measurement above did
# not see it (decision 392).
FOLDS_TO_NOTHING = (
    "**", "***", "*", "___", "[spoiler]", "[/spoiler]", "\u00ad", "\u00a0",
    "   ", "\t", "\n ", "**[/spoiler]___*",
)


async def test_a_quote_with_nothing_behind_the_markup_is_caught_against_a_real_pack(
    db, raw_root
):
    """The empty-fold class, measured the way the catch rate above is measured: against a pack
    `dna/packs.py` really built out of real rows and read back out of custody.

    Every one of these carried a term the vocabulary really holds onto a title whose pack never
    supported it, and none of them was a drop -- so there was no `dna_reject` row either, and the
    install's record said nothing had happened. §8.4's flywheel then reads coverage off exactly
    such a row. The reason is `schema` and REASONS gains no member: what broke is the declared
    contract's own requirement that a tag carry evidence.
    """
    voc, pack_text = await install(db)
    tags = [{"term": "themes.heist", "quote": quote} for quote in FOLDS_TO_NOTHING]

    result = await verify.verify_payload(
        payload(*tags), pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )
    rows = await verdicts(db, result.rejects)

    assert result.tags[TITLE] == []
    assert result.n_seen == len(FOLDS_TO_NOTHING)
    assert [row["rule_violated"] for row in rows] == ["schema"] * len(FOLDS_TO_NOTHING)
    assert [row["quote"] for row in rows] == list(FOLDS_TO_NOTHING), (
        "the reviewer has to see what was offered as evidence, or the row says only that "
        "something was refused"
    )


# --- §4.1 rule 2: low-evidence is an ordering (decision 341) ---------------------------------

# The two queries §6.6's reject review will run, written here before the screen exists so that the
# shape is a fact in the tree rather than an intention in a plan. The first is the refusals; the
# second is the "low-evidence tags" half of the same sentence, and it is the one rule 2 constrains:
# ascending confidence is an ORDER BY and nothing is deleted by it. There is no LIMIT, because an
# ORDER BY on a weight plus a LIMIT is a cut with the comparison left implicit -- which is the
# spelling `test_landmine_guards.py`'s TOP_N_PATTERN exists to catch.
REJECT_REVIEW = """
    SELECT r.title_id, r.term, r.facet, r.salience, r.quote, r.rule_violated, r.provider, r.at
      FROM dna_reject r
     WHERE r.title_id = $1
     ORDER BY r.at DESC, r.id DESC
"""
LOW_EVIDENCE_REVIEW = """
    SELECT t.term, t.facet, t.confidence, t.n_sources
      FROM dna_tag t
     WHERE t.title_id = $1 AND t.version = $2
     ORDER BY t.confidence ASC NULLS LAST, t.term
"""


async def test_the_reject_review_orders_by_recency_and_filters_only_on_the_title(db, raw_root):
    voc, pack_text = await install(db)
    result = await verify.verify_payload(
        payload(
            {"term": "themes.time_travel", "quote": GENUINE[0][1]},
            {"term": "mood.bleak", "quote": "a sentence nobody wrote"},
        ),
        pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )
    await verify.record_rejects(db, result.rejects)
    rows = await db.fetch(REJECT_REVIEW, TITLE)

    assert [row["rule_violated"] for row in rows] == ["quote_unverified", "unknown_term"]


async def test_the_low_evidence_half_is_an_ordering_and_never_a_filter(db, raw_root):
    """§4.1 rule 2: "salience, confidence, n_sources are WEIGHTS, NEVER FILTERS. No `WHERE
    confidence > x` anywhere (a 0.5 cut would delete 44% of the extracted tier)." Decision 341
    defines §6.6's "low-evidence" as this ordering and as nothing else, so the query is read for
    the shape as well as executed for the syntax -- a query that runs is not yet a query that
    keeps the rule.

    WHAT THIS TEST IS, AND WHAT HOLDS THE RULE (decision 401). `LOW_EVIDENCE_REVIEW` is defined
    twenty lines above, so no edit under `backend/spielplan/` can turn these assertions red --
    including the edit the clause exists to forbid, M5.6 shipping the screen with `WHERE
    t.confidence > $3`. What holds the rule is `test_landmine_guards.py`, which reads the package
    and the migrations and never the tests, so it will read that screen the day it is package
    code; and M5.6's own coverage row carries the e2e evidence for the surface. This pins the
    shape where the next reader looks for it and executes it against the live schema, which is
    worth having and is not enforcement. A guard that cannot fail reads as coverage, so what this
    one covers is written down rather than left to be discovered.
    """
    await install(db)
    where = re.search(r"\bWHERE\b(.*?)\bORDER BY\b", LOW_EVIDENCE_REVIEW, re.DOTALL).group(1)

    assert not [c for c in ("confidence", "salience", "n_sources", "weight") if c in where]
    assert "ORDER BY t.confidence ASC NULLS LAST" in LOW_EVIDENCE_REVIEW
    assert "LIMIT" not in LOW_EVIDENCE_REVIEW.upper()
    assert await db.fetch(LOW_EVIDENCE_REVIEW, TITLE, V1) == []


# --- the vocabulary a boundary reads --------------------------------------------------------


async def test_an_install_with_no_vocabulary_has_none_to_check_against(db):
    """§3.1's "a bundle-less app is a legal state". Returning an empty vocabulary instead would
    make every tag `unknown_term`, which is the count that cannot be told apart from an extractor
    collapsing -- so the boundary declines to check rather than issuing a verdict it cannot mean.
    """
    assert await verify.load_vocabulary(db) is None


async def test_the_loaded_vocabulary_is_scoped_to_one_version(db, raw_root):
    """§14 risk 7: "every read is scoped to one version". A second vocabulary exists here so
    there is something for the read to leak from."""
    await install(db)
    # Explicitly older, because `db/dna_terms.ACTIVE_VERSION` picks the most recent IMPORT and
    # not the highest version string -- a second row inserted now would BE the active one, and a
    # test that had not noticed would assert the scoping backwards.
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count, imported_at) "
        "VALUES ('v0', 1, 1, now() - interval '1 day')"
    )
    await db.execute("INSERT INTO dna_facet (version, facet, ord) VALUES ('v0', 'mood', 1)")
    await db.execute(
        "INSERT INTO dna_term (version, term, facet) VALUES ('v0', 'mood.elsewhere', 'mood')"
    )

    active = await verify.load_vocabulary(db)
    older = await verify.load_vocabulary(db, "v0")

    assert active.version == V1
    assert "mood.elsewhere" not in active
    assert set(older.terms) == {"mood.elsewhere"}


async def test_the_loaded_vocabulary_feeds_the_prefix_repair_the_whole_term_pool(db, raw_root):
    """The repair's refusal rule is a question about the WHOLE vocabulary -- is there exactly one
    term with this body -- so a loader that fed it a subset would make the repair answer where it
    should refuse. Asserted against the loaded object rather than a hand-built one."""
    voc, _packs = await install(db)

    assert voc.repair("plot_structure.slow_burn") == "pacing.slow_burn"
    assert voc.repair("narrative_themes.robots") == "themes.robots"
    assert voc.repair("mood.heist") is None
    assert set(voc.facets) == set(FACETS)
    assert set(voc.terms) == set(TERMS)


async def test_an_alias_row_the_vocabulary_does_not_carry_never_repairs_anything(db, raw_root):
    """`dna_alias` has no foreign key from `term` to `dna_term` (`0004_dna.sql:42-47` keys only
    `version`), so the table holds mappings onto terms the vocabulary never adopted -- and the
    loader's own comment at `importer/dna.py:207-208` says the opposite. The row is the authority.
    The INSERT is asserted to have SUCCEEDED before the skip is asserted, because an absence test
    passes just as well against a write that never landed."""
    voc, pack_text = await install(db)
    await db.execute(
        "INSERT INTO dna_alias (version, alias, term) VALUES ($1, 'long con', 'themes.long_con')",
        V1,
    )
    reloaded = await verify.load_vocabulary(db)

    assert await db.fetchval("SELECT count(*) FROM dna_alias WHERE alias = 'long con'") == 1
    assert await db.fetchval("SELECT count(*) FROM dna_term WHERE term = 'themes.long_con'") == 0
    assert reloaded.alias_of("long con") is None

    result = await verify.verify_payload(
        payload({"term": "long con", "quote": GENUINE[3][1]}),
        pass_id="pilot", voc=reloaded, packs=pack_text, ledger=db,
    )

    assert [r.reason for r in result.rejects] == ["unknown_term"]
