"""The trust boundary against a real pack built from review rows (§8 stage 7): every fabrication caught
and every genuine tag kept. The fixture cannot supply the pack (decision 391). Needs TEST_DATABASE_URL."""

from __future__ import annotations

import re

import asyncpg
import pytest

from spielplan.core.config import settings
from spielplan.dna import adjudicate, packs, verify
from spielplan.dna.norm import norm

V1 = "v1"
TITLE = 1

# All eleven facets: `dna_term` foreign-keys `(version, facet)`.
FACETS = (
    "mood", "themes", "pacing", "structure", "visual",
    "sound", "characters", "place", "era", "sensibility", "register",
)

# `themes.robots` IS a vocabulary-v1 term; §8.4's claim about it is about coverage.
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

# Every review clears `packs.MIN_WORDS` (asserted below) and carries its source's markup.
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

# Three genuine spans transcribed as a reader would (markup dropped, apostrophe curled) and one
# literal control. The spoiler tags interrupt the span, or the quote would verify without the fold.
GENUINE = (
    ("pacing.slow_burn", "a slow burn that never raises its voice"),
    ("visual.neon", "Neon on wet asphalt"),
    # Escaped: a failure prints the source line to a cp1252 console.
    ("place.city", "the city\u2019s own rhythm carrying every scene"),
    ("themes.heist", "The heist goes wrong in the last reel"),
)

# Every tail is absent from the vocabulary, so the prefix repair cannot rescue any of them.
FABRICATED_TERMS = (
    "themes.time_travel", "mood.whimsical", "pacing.breakneck", "visual.handheld",
    "sound.silence", "characters.ensemble_cast", "place.desert", "era.nineties",
    "structure.flashback", "register.camp", "sensibility.earnest", "themes.surveillance",
)

# The pilot's negative control in miniature: plausible tags with invented quotes.
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
    """`settings().raw_dir` takes no argument, so the root goes through `DATA_DIR`."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().raw_dir
    settings.cache_clear()


async def seed(conn) -> None:
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
    """Read back out of `dna_pack` and the raw store: that round trip is what decision 382 exists for."""
    await seed(conn)
    text, info = await packs.build_pack(conn, TITLE)
    await packs.store_pack(conn, TITLE, V1, text, info)
    voc = await verify.load_vocabulary(conn)
    return voc, await verify.read_packs(conn, [TITLE], V1)


def payload(*tags: dict[str, object]) -> dict[str, object]:
    return {"titles": {str(TITLE): list(tags)}}


async def verdicts(conn, rejects, **kw) -> list[dict]:
    await verify.record_rejects(conn, rejects, **kw)
    rows = await conn.fetch("SELECT * FROM dna_reject ORDER BY id")
    return [dict(row) for row in rows]


async def test_every_seeded_review_clears_the_pack_floor(db):
    """Postgres's generated `word_count`: a review under 50 words would silently leave the pack."""
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
    """The pack in custody still carries its sources' `**`, `[spoiler]` and straight apostrophe."""
    _voc, pack_text = await install(db)
    text = pack_text[TITLE]

    assert "**slow burn**" in text
    assert "[spoiler]" in text
    assert "city's own rhythm" in text
    assert await db.fetchval("SELECT pack_sha FROM dna_pack WHERE title_id = $1", TITLE) == (
        packs.sha(text)
    )


async def test_a_pack_row_whose_bytes_are_not_its_pack_raises_rather_than_verifying(db, raw_root):
    """`no_pack` would read as never packed rather than as verifying against the wrong evidence."""
    await install(db)
    await db.execute("UPDATE dna_pack SET pack_sha = 'not-the-pack' WHERE title_id = $1", TITLE)

    with pytest.raises(OSError, match="wrong evidence"):
        await verify.read_pack(db, TITLE, V1)


async def test_a_title_this_install_has_never_packed_has_no_pack(db, raw_root):
    await install(db)

    assert await verify.read_pack(db, 999, V1) is None


async def test_no_fabricated_term_is_rescuable_by_any_repair_this_module_has(db, raw_root):
    """The fabrications must be genuinely absent, or a "catch" could be a refusal for another reason."""
    voc, _packs = await install(db)

    assert [t for t in FABRICATED_TERMS if t in voc] == []
    assert [t for t in FABRICATED_TERMS if voc.resolve(t) is not None] == []
    assert voc.resolve("slow burn") == "pacing.slow_burn", (
        "the alias row must be live, or this file measures a vocabulary with no repair at all"
    )


async def test_every_fabricated_tag_in_a_schema_valid_payload_is_caught(db, raw_root):
    """100%, not "most": a boundary that catches most fabrications cannot be trusted unread."""
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
    """"Failures drop, never repaired" is about `dna_tag`, so `dna_tag` is what is counted."""
    voc, pack_text = await install(db)
    tags = [{"term": term_id, "quote": GENUINE[0][1]} for term_id in FABRICATED_TERMS]

    result = await verify.verify_payload(
        payload(*tags), pass_id="pilot", voc=voc, packs=pack_text, ledger=db,
    )
    rows = await verdicts(db, result.rejects, provider="anthropic:sonnet")

    assert await db.fetchval("SELECT count(*) FROM dna_tag") == 0
    assert len(rows) == len(FABRICATED_TERMS)


async def test_every_refusal_is_written_down_with_the_rule_it_broke(db, raw_root):
    """Decision 400: the stated level is recorded on every row that knows one."""
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
    """REASONS is closed and `0027` holds it closed, so a filter on the rule name stays meaningful."""
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
    """`dna_reject.title_id` is nullable, so an `unknown_title` refusal can be written at all."""
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
    """`executemany` is atomic in asyncpg: one bad title id discarded the whole pass's refusals."""
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
    """A NUL in a term reached `dna_adjudication`'s query and raised out of `verify_payload` itself."""
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
    """The write guards itself: `Rejection` is public and M5.5 builds its own."""
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
    """`isinstance(True, int)` is true, so a boolean passed the width test and wrote a 1."""
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


@pytest.mark.parametrize("index", range(len(GENUINE)))
async def test_a_quote_transcribed_across_the_packs_markup_verifies(db, raw_root, index):
    """`norm()` is applied to BOTH sides and nothing else; the stored quote is the extractor's."""
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
    """If all four were literal substrings, the test above would pass with no fold at all."""
    _voc, pack_text = await install(db)
    literal = [quote for _term, quote in GENUINE if quote in pack_text[TITLE]]

    assert literal == ["Neon on wet asphalt"]


async def test_a_term_the_ledger_renames_passes_under_its_new_name(db, raw_root):
    """Without the re-point every ingest after a merge silently reverts the curation."""
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
    """A retirement counted as `unknown_term` looks like an extractor emitting garbage."""
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
    """The ledger is a repair before the vocabulary check, not an exemption from it."""
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
    """`Vocabulary.resolve` strips, so the ledger must be asked about the stripped term too."""
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
    """The query count is the count of terms the vocabulary did not know, not of tags."""
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
    """`is_retired` asked directly answers differently; this pins the asymmetry so it can be read."""
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


async def test_a_tag_that_carries_no_quote_is_refused_and_written_down(db, raw_root):
    """§4.1 rule 1: a tag without its quote is unfalsifiable."""
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


# Pure markup folds to "", a substring of every pack: passed at 100% until decision 392.
FOLDS_TO_NOTHING = (
    "**", "***", "*", "___", "[spoiler]", "[/spoiler]", "\u00ad", "\u00a0",
    "   ", "\t", "\n ", "**[/spoiler]___*",
)


async def test_a_quote_with_nothing_behind_the_markup_is_caught_against_a_real_pack(
    db, raw_root
):
    """These wrote no `dna_reject` row either, so the install's record said nothing happened."""
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


# No LIMIT: an ORDER BY on a weight plus a LIMIT is a cut with the comparison left implicit.
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
    """This pins the shape and runs it against the live
    schema; the package guard is what enforces rule 2."""
    await install(db)
    where = re.search(r"\bWHERE\b(.*?)\bORDER BY\b", LOW_EVIDENCE_REVIEW, re.DOTALL).group(1)

    assert not [c for c in ("confidence", "salience", "n_sources", "weight") if c in where]
    assert "ORDER BY t.confidence ASC NULLS LAST" in LOW_EVIDENCE_REVIEW
    assert "LIMIT" not in LOW_EVIDENCE_REVIEW.upper()
    assert await db.fetch(LOW_EVIDENCE_REVIEW, TITLE, V1) == []


async def test_an_install_with_no_vocabulary_has_none_to_check_against(db):
    """An empty vocabulary would make every tag `unknown_term`, indistinguishable from a collapse."""
    assert await verify.load_vocabulary(db) is None


async def test_the_loaded_vocabulary_is_scoped_to_one_version(db, raw_root):
    """§14 risk 7: a second vocabulary exists here so there is something for the read to leak from."""
    await install(db)
    # Explicitly older: `ACTIVE_VERSION` picks the most recent IMPORT, not the highest version string.
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
    """The repair asks whether exactly one term has this body, so it needs the whole vocabulary."""
    voc, _packs = await install(db)

    assert voc.repair("plot_structure.slow_burn") == "pacing.slow_burn"
    assert voc.repair("narrative_themes.robots") == "themes.robots"
    assert voc.repair("mood.heist") is None
    assert set(voc.facets) == set(FACETS)
    assert set(voc.terms) == set(TERMS)


async def test_an_alias_row_the_vocabulary_does_not_carry_never_repairs_anything(db, raw_root):
    """The INSERT is asserted to have succeeded first: an
    absence test passes against a write that never landed."""
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
