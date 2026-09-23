"""The pack, the craft supplement and pack custody. Spec v2.1 §8 stage 5, §14 risk 2.

Registered under `data-rules-one-quote-normalisation-for-the-pack-and-the-verifier`, whose claim
has two halves and whose second half is this file's: "A quote transcribed across bold markers, a
spoiler tag or a smart apostrophe verifies against a pack that carries the markup verbatim."
`test_dna_norm.py` owns the fold. What is asserted here is the other side of the same sentence --
that the pack the fold is applied to still HAS the markup -- because a pack builder that tidied
its input would make every one of that file's tests pass against a corpus of quotes that can no
longer be transcribed from anything.

Two layers, split where the corpus's own function could not be split at all. `render_pack` is a
pure function of rows, so the four caps, the interleave, the plot cap and the series header are
asserted with no database; the 50-word floor is a WHERE clause in `_REVIEWS` and is asserted
against Postgres, along with the three things that only real rows can say -- that the plot is the
longest across sources, that two runs order equal-length reviews the same way, and that
`store_pack` leaves bytes that read back unchanged.

THE FIXTURE BUNDLE CANNOT EXPRESS A PACK AND THAT IS A MEASUREMENT, NOT A REASON TO SKIP
(decision 391). It ships three `review_store.review` rows at 10, 6 and 1 words, none of them from
`rottentomatoes`, every one under the 50-word floor, and no `raw_document` row of any source --
so on the shipped data the pack is a header and a plot, the craft supplement is empty, and every
interesting assertion below would be green against a builder that returned the empty string. So
the rows are inserted here. Two tests state the measurement itself rather than leaving it in a
docstring: the fixture's own three reviews are carried in verbatim and asserted to reach nothing.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import hashlib
import json

import asyncpg
import pytest

from spielplan.acquire import rawstore
from spielplan.core.config import settings
from spielplan.dna import craft, packs, verify
from spielplan.dna.norm import norm

# `make_bundle.py:552-558`, verbatim: the whole of this install's review corpus until §8 fetches
# something. The word counts are what `review_store.review.word_count` computes for them.
FIXTURE_REVIEWS = (
    ("metacritic", "A city film that keeps its distance and earns it.", 10),
    ("trakt", "Bleak, and it does not blink.", 6),
    # Spelled as escapes, not as the characters: CLAUDE.md keeps console and test output
    # ASCII because a Windows cp1252 console cannot encode these, and a failure here prints
    # the source line. The VALUE is `make_bundle.py`'s row to the codepoint, which is what
    # section 4.1 rule 8's "never 'clean' non-ASCII" requires of it.
    ("letterboxd", "\u738b\u5bb6\u885b\u306e\u6620\u50cf\u306f\u4eca\u3082\u65b0\u3057\u3044\u3002", 1),
)


def long_review(word: str, words: int = 60) -> str:
    """A review body that clears `MIN_WORDS`, made of one repeated word.

    Distinguishable by its first word and uninteresting everywhere else, so an assertion about
    WHICH reviews survived a cap reads as one rather than as a diff of prose.
    """
    return " ".join([word] * words)


@pytest.fixture
def raw_root(tmp_path, monkeypatch):
    """A raw store of this test's own, reached the way the worker reaches it.

    The idiom is `test_acquire_rawstore.py`'s and the reason is its: `settings().raw_dir` takes no
    argument on purpose, so the root is set through `DATA_DIR` and the `lru_cache` is cleared on
    both sides.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().raw_dir
    settings.cache_clear()


def blocks(text: str) -> list[tuple[str, str]]:
    """The pack's `[marker]`/body pairs, in order.

    The pack's format is markers on their own line with the body on the next, which is what makes
    it readable to an extractor and trivial to read back here. Parsing it rather than asserting
    against a golden string keeps a test about the INTERLEAVE from failing on a header change.
    """
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines):
        if line.startswith("[") and line.endswith("]") and i + 1 < len(lines):
            out.append((line[1:-1], lines[i + 1]))
    return out


# --- the caps and the interleave, over rows constructed here --------------------------


def test_the_interleave_alternates_across_sources_rather_than_concatenating():
    """`mdc/dna/packs.py:168-170`: "taking source A's twelve before source B's first means a
    truncated pack is a single-source pack, which is exactly the bias the spread exists to
    avoid."

    Asserted as the SHAPE of the whole sequence and not as "source B appears somewhere": a
    builder that concatenated and then shuffled would satisfy the weaker claim while leaving the
    truncation bias exactly where it was. Round n of every source precedes round n+1 of any of
    them, which is the property the `while` loop has and concatenation does not.
    """
    rows = [(src, long_review(f"{src}{n}"))
            for src in ("aaa", "bbb", "ccc") for n in range(12)]
    text, info = packs.render_pack(1, "Heat", 1995, "movie", None, None, None, rows)

    markers = [m for m, _body in blocks(text) if ":" in m]
    rounds = [int(m.split(":")[1]) for m in markers]
    assert rounds == sorted(rounds), (
        "a source's reviews were emitted in a run rather than one per round: "
        f"{markers[:8]}"
    )
    assert [m.split(":")[0] for m in markers[:6]] == ["aaa", "bbb", "ccc", "aaa", "bbb", "ccc"]
    assert info.n_sources == 3


def test_a_truncated_pack_still_carries_every_source():
    """The interleave's whole point, stated where it bites.

    Thirty reviews from each of three sources is ninety candidates for sixty places, so the total
    cap truncates. Under concatenation the pack would be source `aaa` twelve times and then `bbb`
    twelve and then `ccc` -- which happens to fit here, so the failing case is built the other
    way: six sources of twelve, where a concatenating builder fills sixty places from five
    sources and drops the sixth entirely.
    """
    rows = [(f"src{i}", long_review(f"src{i}-{n}")) for i in range(6) for n in range(12)]
    text, info = packs.render_pack(1, "Heat", 1995, "movie", None, None, None, rows)

    sources = {m.split(":")[0] for m, _body in blocks(text) if ":" in m}
    assert info.n_reviews == packs.MAX_REVIEWS
    assert sources == {f"src{i}" for i in range(6)}, (
        "the total cap dropped a whole source, which is the single-source pack the interleave "
        f"exists to prevent: kept {sorted(sources)}"
    )


def test_no_source_contributes_more_than_the_per_source_cap():
    """`MAX_PER_SOURCE = 12`, "so no single reviewer culture dominates"."""
    rows = [("trakt", long_review(f"t{n}")) for n in range(40)]
    text, info = packs.render_pack(1, "Heat", 1995, "movie", None, None, None, rows)

    assert info.n_reviews == packs.MAX_PER_SOURCE
    assert len(blocks(text)) == packs.MAX_PER_SOURCE


def test_the_total_cap_stops_mid_round_rather_than_overshooting():
    """`MAX_REVIEWS = 60`, at the boundary the outer `while` alone does not hold.

    Seven sources of twelve is eighty-four candidates: eight full rounds are fifty-six, and the
    ninth has room for four of its seven. The inner `len(picked) < MAX_REVIEWS` is what stops it
    there, and a loop that only checked the outer condition would emit sixty-three -- a pack over
    its measured token budget, which is the cap's whole purpose.
    """
    rows = [(f"src{i}", long_review(f"s{i}-{n}")) for i in range(7) for n in range(12)]
    text, info = packs.render_pack(1, "Heat", 1995, "movie", None, None, None, rows)

    assert info.n_reviews == packs.MAX_REVIEWS
    markers = [m for m, _body in blocks(text) if ":" in m]
    assert [int(m.split(":")[1]) for m in markers[-4:]] == [9, 9, 9, 9]


def test_a_review_is_cut_at_the_character_cap():
    """`MAX_CHARS = 1500`, per review. Asserted at the boundary in both directions, because a
    cap written as `<=` instead of a slice is off by one against exactly one input length."""
    at_cap = "x" * packs.MAX_CHARS
    over = "y" * (packs.MAX_CHARS + 500)
    text, _info = packs.render_pack(
        1, "Heat", 1995, "movie", None, None, None, [("a", at_cap), ("b", over)],
    )
    bodies = {m.split(":")[0]: body for m, body in blocks(text)}
    assert bodies["a"] == at_cap
    assert bodies["b"] == "y" * packs.MAX_CHARS


def test_the_plot_is_cut_at_its_own_cap():
    """`MAX_PLOT_CHARS = 4000`, which is a different cap from the per-review one and has to stay
    one: a plot is the only block in the pack that is not a review, and the corpus gives it
    almost three times the room for the obvious reason."""
    text, _info = packs.render_pack(
        1, "Heat", 1995, "movie", None, None, "z" * 10_000, [],
    )
    assert dict(blocks(text))["plot:1"] == "z" * packs.MAX_PLOT_CHARS


# --- the header ------------------------------------------------------------------------


def test_a_series_header_tells_the_extractor_to_describe_the_show_as_a_whole():
    """The ported series rule (`mdc/dna/packs.py:20-28`): one pack for the whole show, and the
    header saying so is the entire mechanism. Its named failure mode is an anthology like The
    White Lotus, where any single `place` tag misrepresents two thirds of the show -- so the
    sentence the extractor reads is load-bearing and is asserted verbatim."""
    text, _info = packs.render_pack(7, "The Bear", 2022, "series", 3, 28, None, [])
    assert "[type] series" in text
    assert ("[note] Series: 3 season(s), 28 episodes. Reviews below may discuss different "
            "seasons; describe the show AS A WHOLE.") in text


def test_a_film_header_carries_no_series_note():
    text, _info = packs.render_pack(1, "Heat", 1995, "movie", None, None, None, [])
    assert "[type] film" in text
    assert "AS A WHOLE" not in text


def test_a_series_with_no_counts_says_it_does_not_know_rather_than_claiming_none():
    """`{seasons or '?'}` is the corpus's spelling and it matters more here than there: named
    port change 2 reads the counts out of `title_meta.payload`, where a source that never
    carried the field is indistinguishable from one that carried a zero. A header claiming "0
    season(s)" would be this app telling a model something false about a show."""
    text, _info = packs.render_pack(7, "The Bear", 2022, "series", None, None, None, [])
    assert "[note] Series: ? season(s), ? episodes." in text


def test_the_largest_count_any_source_claims_is_the_one_the_header_states():
    """Named port change 2. `title` carries neither column, so the counts are per source and can
    disagree; the header's job is to tell the extractor the scope of what it is reading, and a
    stale source saying "1 season" over reviews of three is the failure that matters. A value
    that is not a run of digits is not a count and is dropped rather than guessed at."""
    rows = [{"season_count": "1"}, {"season_count": "3"}, {"season_count": None}]
    assert packs._largest_count(rows, "season_count") == 3
    assert packs._largest_count([{"season_count": "3 (ordered)"}], "season_count") is None
    assert packs._largest_count([], "season_count") is None


# --- what the pack keeps, which is everything its sources published ---------------------


def test_clean_strips_html_tags_and_collapses_whitespace():
    """`clean()` is the whole of what the builder removes, and this is the list."""
    assert packs.clean("<p>a  <b>bold</b>\n\n claim</p>") == "a bold claim"
    assert packs.clean(None) == ""
    assert packs.clean("   ") == ""


def test_the_pack_keeps_the_markup_its_sources_published():
    """B4, and the half of this file's coverage row that lives here.

    A "cleaner" pack verifies FEWER quotes, not more. An extractor transcribing a span across
    `**` or `[spoiler]` produces a quote that is right about the film, and it verifies only
    because both the pack and the quote pass through `norm()` at the same moment -- so the
    markup has to still be in the pack for the fold to have anything to do. The second assertion
    is the one that would go green on a builder that stripped: the transcribed quote has to
    verify, which it can only do if the fold is closing a gap that exists.
    """
    body = "The **daughter** never comes home, and [spoiler]nobody says so[/spoiler]."
    text, _info = packs.render_pack(1, "Prisoners", 2013, "movie", None, None, None,
                                    [("trakt", body)])

    assert "**daughter**" in text, "the pack stripped markdown emphasis at build time"
    assert "[spoiler]" in text, "the pack stripped a BBCode spoiler tag at build time"
    transcribed = "The daughter never comes home"
    assert transcribed not in text
    assert norm(transcribed) in norm(text), (
        "a quote transcribed across the markup no longer verifies against the pack, which is "
        "what stripping at build time costs and what folding at comparison time buys"
    )


def test_the_sha_is_the_digest_of_the_text_the_extraction_reads():
    """`mdc/dna/store.store_title`'s scar: a sha derived from anything other than the pack text
    itself marked 825 titles current against a pack no pass had seen and hid 652."""
    text, info = packs.render_pack(1, "Heat", 1995, "movie", None, None, "A plot.", [])
    assert info.sha == packs.sha(text)
    assert info.chars == len(text)


def test_two_packs_that_differ_by_one_character_get_different_shas():
    a, info_a = packs.render_pack(1, "Heat", 1995, "movie", None, None, "A plot.", [])
    b, info_b = packs.render_pack(1, "Heat", 1995, "movie", None, None, "A plot!", [])
    assert a != b
    assert info_a.sha != info_b.sha


def test_a_title_with_no_plot_and_no_reviews_still_gets_a_pack():
    """"There is nothing to extract from" is §8 stage 4's verdict and not the builder's: stage 4
    is the reviews gate that parks a thin title on a 30-day window (decision 336), and a builder
    that answered the same question a second time would answer it with a different rule."""
    text, info = packs.render_pack(8, "Tampopo", 1985, "movie", None, None, None, [])
    assert text.startswith("# Tampopo (1985)")
    assert info.n_reviews == 0
    assert info.n_sources == 0


# --- the craft supplement's two rules, with no database ---------------------------------


def test_augmenting_twice_leaves_one_supplement():
    """`SENTINEL` is the whole of the idempotence, and stacking is the failure it prevents: a
    second copy would double every craft quote's chance of verifying against text the extractor
    never saw once."""
    base = "# Heat (1995)\n[type] film\n\n[plot:1]\nA plot.\n"
    sup = f"{craft.SENTINEL}\n[note] more\n\n[wiki:music]\nminimalist piano pieces\n"

    once = craft.apply_supplement(base, sup)
    twice = craft.apply_supplement(once, sup)

    assert once == twice
    assert once.count(craft.SENTINEL) == 1


def test_an_augmented_pack_keeps_the_base_as_an_exact_prefix():
    """The correctness requirement the ported docstring argues: an augmented pack that keeps the
    original text as an exact prefix leaves every previously verified quote verifiable, and one
    that rewrote the body would drop them all as `quote_unverified` on the next pass."""
    base = "# Heat (1995)\n[type] film\n\n[plot:1]\nA **plot**.\n"
    sup = f"{craft.SENTINEL}\n[note] more\n\n[wiki:music]\nminimalist piano pieces\n"

    once = craft.apply_supplement(base, sup)

    assert once.startswith(base.rstrip("\n"))
    assert craft.base_pack(once) == base
    assert norm("A plot") in norm(once), "a quote that verified against the base stopped doing so"


def test_an_empty_supplement_strips_one_that_is_already_there():
    """The other direction of "re-augmenting replaces it", and the one a caller hits when a
    title's article is deleted upstream: the pack goes back to its base rather than keeping a
    supplement nothing can still be checked against."""
    base = "# Heat (1995)\n[type] film\n"
    once = craft.apply_supplement(base, f"{craft.SENTINEL}\n[wiki:music]\nprose\n")
    assert craft.apply_supplement(once, "") == base


def test_a_nested_craft_heading_is_found_without_walking_the_tree():
    """`CRAFT_SECTIONS`'s own comment: "`=== Music ===` under `== Production ==` is found
    without walking the tree". The parent is in no group, so what a reader gets is the child."""
    extract = ("== Production ==\nHow it came to exist.\n"
               "=== Music ===\nMinimalist piano pieces, punctuated with light percussion.\n"
               "== Reception ==\nCritics liked it.\n")
    found = dict(craft.sections(extract))
    assert set(found) == {"Production", "Music", "Reception"}
    assert "Minimalist piano pieces" in found["Music"]


# --- the database half ------------------------------------------------------------------


async def seed_title(conn, title_id=1, *, name="Heat", year=1995, kind="movie") -> int:
    await conn.execute(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, $2, $3, $4, true)",
        title_id, kind, name, year,
    )
    return title_id


async def add_reviews(conn, title_id, rows) -> None:
    await conn.executemany(
        "INSERT INTO review_store.review (title_id, source, body, is_critic) "
        "VALUES ($1, $2, $3, $4)",
        [(title_id, src, body, critic) for src, body, critic in rows],
    )


async def test_the_plot_comes_from_the_longest_payload_across_sources(db):
    """Named port change 1, and the reason it is not `title.overview`.

    `title.overview` is the per-field resolved choice and would answer `tmdb` here, because
    `importer/meta.SOURCE_PRIORITY` puts tmdb first. A pack wants the most EVIDENCE, so the
    ORDER BY is the corpus's: longest wins, whatever source it came from.
    """
    await seed_title(db)
    await db.executemany(
        "INSERT INTO title_meta (title_id, source, payload) VALUES ($1, $2, $3)",
        [
            (1, "tmdb", {"plot_full": "A short one."}),
            (1, "omdb", {"plot_full": "A considerably longer synthetic plot text."}),
            (1, "wikipedia", {"plot_short": "A one-line synthetic summary."}),
        ],
    )

    text, _info = await packs.build_pack(db, 1)
    assert dict(blocks(text))["plot:1"] == "A considerably longer synthetic plot text."


async def test_plot_short_is_used_only_where_no_source_carries_a_full_plot(db):
    """`payload ->> 'plot_short'` is the corpus's fallback and `importer/meta.py:41` records why
    it exists at all: it is carried by exactly one source and is better than nothing."""
    await seed_title(db)
    await db.execute(
        "INSERT INTO title_meta (title_id, source, payload) VALUES ($1, $2, $3)",
        1, "wikipedia", {"plot_short": "A one-line synthetic summary."},
    )

    text, _info = await packs.build_pack(db, 1)
    assert dict(blocks(text))["plot:1"] == "A one-line synthetic summary."


async def test_two_sources_whose_plots_tie_pick_the_same_one_after_a_re_import(db):
    """The plot query's own version of named change 3, which it did not carry.

    `title_meta` is one row PER SOURCE, the sort key is `length(plot_full)`, and it is 0 for every
    row that carries only a `plot_short` -- so a title whose sources tie has no ordering at all,
    and `LIMIT 1` took whichever row the planner returned first. `importer/meta.load_title_meta`
    does DELETE then a bulk INSERT on every import, so the heap really does reorder under rows
    that did not change: this test re-inserts one of them, which is what a re-import does to all
    of them, and asserts the pack and therefore `sha()` did not move. Without the tie-break the
    same logical rows produced two different `dna_pack.pack_sha` values, which is decision 382's
    reproducibility failing on a re-import that changed nothing.
    """
    await seed_title(db)
    await db.executemany(
        "INSERT INTO title_meta (title_id, source, payload) VALUES ($1, $2, $3)",
        [
            (1, "tmdb", {"plot_short": "AAAA a synthetic one-liner."}),
            (1, "wikipedia", {"plot_short": "BBBB a synthetic one-liner."}),
        ],
    )

    _first, before = await packs.build_pack(db, 1)

    await db.execute("DELETE FROM title_meta WHERE title_id = 1 AND source = 'tmdb'")
    await db.execute(
        "INSERT INTO title_meta (title_id, source, payload) VALUES ($1, $2, $3)",
        1, "tmdb", {"plot_short": "AAAA a synthetic one-liner."},
    )

    _again, after = await packs.build_pack(db, 1)
    assert after.sha == before.sha, (
        "the same rows built two different packs, so pack_sha moved under a verdict with nothing "
        "in the log to say why"
    )


async def test_a_review_under_the_word_floor_never_reaches_the_pack(db):
    """`MIN_WORDS = 50`: "below this a 'review' is a rating with a sentence". The floor is a
    WHERE clause in `_REVIEWS`, so it is asserted here rather than in the no-DB half -- and
    `word_count` is GENERATED STORED, so what the floor reads is Postgres's count and not one
    this test computed."""
    await seed_title(db)
    await add_reviews(db, 1, [
        ("trakt", long_review("keep", 50), False),
        ("trakt", long_review("drop", 49), False),
    ])

    text, info = await packs.build_pack(db, 1)
    assert info.n_reviews == 1
    assert "keep" in text
    assert "drop" not in text, "a 49-word rating-with-a-sentence reached the pack"


async def test_reviews_of_equal_length_are_ordered_deterministically(db):
    """Named port change 3, and the reason the `id` tiebreak is not tidiness.

    `review_store.review` has no `helpful_yes`, so the corpus's first ORDER BY term is gone and
    every review of equal length is a tie. A tie that Postgres resolves differently between two
    runs changes which twelve survive `MAX_PER_SOURCE`, which changes the pack text, which
    changes `sha()` -- and every quote verified against the old text becomes unverifiable with
    nothing in the log to say why. Twenty ties in one source, built twice.
    """
    await seed_title(db)
    await add_reviews(db, 1, [("trakt", long_review(f"tie{n}"), False) for n in range(20)])

    first, info_first = await packs.build_pack(db, 1)
    second, info_second = await packs.build_pack(db, 1)

    assert first == second
    assert info_first.sha == info_second.sha


async def test_the_series_header_reads_its_counts_from_the_meta_payload(db):
    """Named port change 2 against real rows: `title` carries neither column, the payload does,
    and two sources disagreeing is the ordinary case rather than a corrupt one."""
    await seed_title(db, 7, name="The Bear", year=2022, kind="series")
    await db.executemany(
        "INSERT INTO title_meta (title_id, source, payload) VALUES ($1, $2, $3)",
        [
            (7, "tmdb", {"season_count": 2, "episode_count": 18}),
            (7, "tvmaze", {"season_count": 3, "episode_count": 28}),
        ],
    )

    text, _info = await packs.build_pack(db, 7)
    assert "[note] Series: 3 season(s), 28 episodes." in text


async def test_a_title_this_install_does_not_hold_has_no_pack(db):
    """None for a missing title row and for nothing else, exactly as the corpus returns it."""
    assert await packs.build_pack(db, 4242) is None


async def test_the_three_review_rows_this_install_ships_cannot_make_a_pack(db):
    """Decision 391, as an assertion instead of a docstring.

    The fixture bundle's whole review corpus is these three rows. Every one is under the 50-word
    floor, so the pack any of their titles gets is a header and nothing else -- which is what
    makes every cap and interleave test above insert its own rows, and what §8 stage 4's reviews
    gate exists to park on (decision 336). Stating it here means the day the fixture gains a real
    review, this test fails and somebody re-reads the measurement rather than inheriting it.
    """
    await seed_title(db)
    await add_reviews(db, 1, [(src, body, False) for src, body, _words in FIXTURE_REVIEWS])

    counts = await db.fetch(
        "SELECT source, word_count FROM review_store.review WHERE title_id = 1 ORDER BY source"
    )
    assert {r["source"]: r["word_count"] for r in counts} == {
        src: words for src, _body, words in FIXTURE_REVIEWS
    }

    _text, info = await packs.build_pack(db, 1)
    assert info.n_reviews == 0
    assert info.n_sources == 0


# --- custody: decision 382 ---------------------------------------------------------------


async def test_store_pack_writes_one_index_row_and_bytes_that_read_back_unchanged(db, raw_root):
    """Decision 382, and the whole of what makes §8 stage 7 auditable.

    Stage 7 verifies a quote against THAT TITLE'S PACK, so the text has to be retained and
    addressable or the verdict cannot be reproduced and §6.6's reject review cannot explain a
    single refusal. The assertion that matters is the round trip: the bytes `rawstore.read` hands
    back are the bytes whose digest `dna_pack.pack_sha` records, to the byte.
    """
    await seed_title(db)
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 11, 582)"
    )
    await add_reviews(db, 1, [("trakt", long_review("evidence"), False)])

    text, info = await packs.build_pack(db, 1)
    doc_id = await packs.store_pack(db, 1, "v1", text, info, entity_key="jellyfin:abc")

    row = await db.fetchrow("SELECT * FROM dna_pack WHERE title_id = 1 AND version = 'v1'")
    assert row["pack_sha"] == info.sha == packs.sha(text)
    assert row["raw_document_id"] == doc_id
    assert (row["n_reviews"], row["chars"]) == (1, len(text))

    doc = await db.fetchrow("SELECT * FROM raw_document WHERE id = $1", doc_id)
    assert (doc["source"], doc["kind"], doc["url"]) == ("pack", "dna", "pack:title:1")
    assert doc["entity_key"] == "jellyfin:abc", (
        "a pack filed under anything but the acquisition task's key is a document the board in "
        "section 6.6 can never show, and decision 345 makes that board the only window on it"
    )
    assert (await rawstore.read(db, doc_id)).decode("utf-8") == text


async def test_rebuilding_a_pack_replaces_its_index_row_rather_than_adding_one(db, raw_root):
    """One row per (title, version), upserted. The raw store keeps every write -- that is its
    own rule and `built_at` says which one this row names -- but the index has to answer "what
    is this title's pack under v1" with one row, or a verifier has a choice to make."""
    await seed_title(db)
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 11, 582)"
    )
    await add_reviews(db, 1, [("trakt", long_review("first"), False)])

    text, info = await packs.build_pack(db, 1)
    await packs.store_pack(db, 1, "v1", text, info)

    await add_reviews(db, 1, [("metacritic", long_review("second"), True)])
    rebuilt, rebuilt_info = await packs.build_pack(db, 1)
    second_doc = await packs.store_pack(db, 1, "v1", rebuilt, rebuilt_info)

    assert rebuilt_info.sha != info.sha
    rows = await db.fetch("SELECT * FROM dna_pack WHERE title_id = 1")
    assert len(rows) == 1
    assert rows[0]["pack_sha"] == rebuilt_info.sha
    assert rows[0]["raw_document_id"] == second_doc
    assert await db.fetchval("SELECT count(*) FROM raw_document WHERE source = 'pack'") == 2


async def test_one_titles_pack_is_storable_under_two_vocabulary_versions(db, raw_root):
    """§14 risk 7: "Two vocabularies in the tables is a state this build does not create and must
    still survive."

    `render_pack` takes no version and emits none, so the same title's pack under v1 and under v2
    is byte-identical whenever its plot and reviews have not changed -- which decision 162 makes
    the ordinary case, content being seeded once. A column-level UNIQUE on `pack_sha` refused the
    second version's INSERT for the whole unchanged library, while `read_pack` scopes its read to
    the version and so demanded exactly the row the writer could not create. The constraint is
    scoped to the version now, which is where every other table in this family keeps it -- and to
    the title as well since decision 403, for the collision the next test measures.
    """
    await seed_title(db)
    await db.executemany(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, 11, 582)",
        [("v1",), ("v2",)],
    )
    await add_reviews(db, 1, [("trakt", long_review("evidence"), False)])

    text, info = await packs.build_pack(db, 1)
    await packs.store_pack(db, 1, "v1", text, info)
    await packs.store_pack(db, 1, "v2", text, info)

    rows = await db.fetch("SELECT version, pack_sha FROM dna_pack WHERE title_id = 1 ORDER BY 1")
    assert [(r["version"], r["pack_sha"]) for r in rows] == [("v1", info.sha), ("v2", info.sha)]


async def test_two_titles_whose_packs_are_byte_identical_are_both_in_custody(db, raw_root):
    """`render_pack` writes the title's NAME and YEAR into the header and never its id, and
    `build_pack` gives a title with no plot and no reviews a two-line pack on purpose -- so two
    title rows for one work, or any two unenriched titles sharing a name, a year and a kind,
    produce one pack and one sha. A key scoped to (version, pack_sha) refused the second with a
    bare `UniqueViolationError` that `store_pack`'s upsert does not cover: that title was then
    `no_pack` forever, and a title whose rebuilt pack came to equal another's kept a row naming
    its OLD pack, which `read_pack` returned without raising because the row and its bytes still
    agreed. The key is scoped to the title now (decision 403). [M5.4 review cycle 3, M54-C3-PACK-02]
    """
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 11, 582)"
    )
    for title_id in (1, 2, 10):
        await seed_title(db, title_id)
    await add_reviews(db, 10, [("trakt", long_review("evidence"), False)])

    thin, thin_info = await packs.build_pack(db, 1)
    assert (await packs.build_pack(db, 2))[0] == thin
    await packs.store_pack(db, 1, "v1", thin, thin_info)
    await packs.store_pack(db, 2, "v1", thin, thin_info)

    enriched, enriched_info = await packs.build_pack(db, 10)
    await packs.store_pack(db, 10, "v1", enriched, enriched_info)
    await db.execute("DELETE FROM review_store.review WHERE title_id = 10")
    rebuilt, rebuilt_info = await packs.build_pack(db, 10)
    assert rebuilt == thin, "the upsert path needs the rebuilt pack to collide, or it tests nothing"
    await packs.store_pack(db, 10, "v1", rebuilt, rebuilt_info)

    rows = await db.fetch("SELECT title_id, pack_sha FROM dna_pack ORDER BY title_id")
    assert [(r["title_id"], r["pack_sha"]) for r in rows] == [
        (1, thin_info.sha), (2, thin_info.sha), (10, thin_info.sha),
    ]
    for title_id in (1, 2, 10):
        assert await verify.read_pack(db, title_id, "v1") == thin


async def test_storing_a_pack_the_info_does_not_describe_is_refused(db, raw_root):
    """`text` and `info` are independent parameters and nothing but this guard related them.

    The caller that would get it wrong is the natural one: `craft.augment` returns a `CraftInfo`,
    which `store_pack` cannot take, so the only value in scope after an augment that satisfies the
    signature is the BASE pack's `PackInfo`. Storing that writes a row naming a pack the raw store
    does not hold, and `read_pack` then raises for that title on every pass instead of verifying
    -- the corpus's `store_title` scar in this app's shape, which decision 382 exists to prevent.
    """
    await seed_title(db)
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 11, 582)"
    )
    await add_reviews(db, 1, [("trakt", long_review("evidence"), False)])

    text, info = await packs.build_pack(db, 1)
    augmented = text + "\n[wiki:1]\nA craft section this info does not describe.\n"

    with pytest.raises(ValueError, match="pack custody"):
        await packs.store_pack(db, 1, "v1", augmented, info)

    assert await db.fetchval("SELECT count(*) FROM dna_pack") == 0


async def test_the_document_a_pack_row_names_cannot_be_deleted_out_from_under_it(db, raw_root):
    """`ON DELETE SET NULL` made two states one state: `read_pack` answered None on a NULL
    `raw_document_id` exactly as it did on a missing row, so "the evidence for verdicts already
    reached was deleted while this row still names its sha" read as "this install has never packed
    this title". The plan's own risk is the rule -- the pack has to be retained or stage 7 is
    unauditable -- so the delete is refused instead."""
    await seed_title(db)
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 11, 582)"
    )
    await add_reviews(db, 1, [("trakt", long_review("evidence"), False)])

    text, info = await packs.build_pack(db, 1)
    doc_id = await packs.store_pack(db, 1, "v1", text, info)

    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await db.execute("DELETE FROM raw_document WHERE id = $1", doc_id)


async def test_a_pack_row_cannot_name_no_document_at_all(db, raw_root):
    """RESTRICT closed the one ROUTE to a NULL `raw_document_id` and left the column able to hold
    one, so the state the test above refuses to create by deletion stayed one UPDATE away -- a
    retention job, an operator's repair, a later migration -- and `read_pack` answered None for it,
    which `verify_payload` recorded as `no_pack`: "never packed", the misreading `0027` argues
    against at length. The schema now refuses the state and not just the route.
    [M5.4 review cycle 3, M54-C3-PACK-04; decision 395]
    """
    await seed_title(db)
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ('v1', 11, 582)"
    )
    text, info = await packs.build_pack(db, 1)
    doc_id = await packs.store_pack(db, 1, "v1", text, info)

    with pytest.raises(asyncpg.exceptions.NotNullViolationError):
        await db.execute("UPDATE dna_pack SET raw_document_id = NULL WHERE title_id = 1")

    assert await db.fetchval("SELECT raw_document_id FROM dna_pack WHERE title_id = 1") == doc_id


def test_the_pack_digest_is_the_first_sixteen_characters_of_a_full_sha256():
    """The truncation is inherited and is kept, so it is pinned rather than left to be
    rediscovered: decision 382 makes this value what 0028's `llm_call` references, and a reader
    meeting a 16-character "sha256" cannot otherwise tell a measured width from a copied line."""
    text = "# Heat (1995)\n[type] film\n"
    full = hashlib.sha256(text.encode("utf-8")).hexdigest()

    assert packs.sha(text) == full[:16]
    assert len(packs.sha(text)) == 16 and len(full) == 64


# --- the craft supplement against real rows ------------------------------------------------


async def seed_wikipedia_article(db, title_id, extract, *, key="jellyfin:abc") -> int:
    """One `wikipedia:article` document, filed the way §8 stage 2 will file it.

    The shape is `action=query&prop=extracts&explaintext=1`, which is what `mdc/sources/
    wikipedia.py:148` asks for and what `craft.sections` slices. The task row is not decoration:
    named port change 1 finds a title's documents by joining `entity_key` to
    `acquisition_task.key`, so a document with no task is a document no reader can reach.
    """
    await db.execute(
        "INSERT INTO acquisition_task (kind, key, payload) VALUES ('acquire-title', $1, $2)",
        key, {"title_id": title_id},
    )
    return await rawstore.store(
        db, source="wikipedia", kind="article", url=f"https://en.wikipedia.org/w/api.php?{key}",
        entity_key=key,
        content=json.dumps({"query": {"pages": [{"extract": extract}]}}).encode("utf-8"),
    )


async def test_a_wikipedia_article_in_the_raw_store_yields_its_craft_sections(db, raw_root):
    """The wiki half of §8 stage 5's supplement, end to end.

    Parasite is the corpus's own example and the reason this half exists: its 44k-char pack
    carries no discussion of its score at all, while its Wikipedia article -- already on disk --
    calls it "minimalist piano pieces, punctuated with light percussion".
    """
    await seed_title(db, 9, name="Parasite", year=2019)
    await seed_wikipedia_article(db, 9, (
        "== Plot ==\nA family schemes.\n"
        "== Production ==\nShot in Seoul over 77 days with a purpose-built house set.\n"
        "=== Music ===\nJung Jae-il's score is minimalist piano pieces, punctuated with light "
        "percussion, and it runs under the film's long silences rather than over them.\n"
        "== Reception ==\nCritics liked it.\n"
    ))

    found = await craft.wiki_craft(db, 9)
    assert [marker for marker, _text in found] == ["music"]
    assert "minimalist piano pieces" in dict(found)["music"]


async def test_the_craft_supplement_is_appended_to_a_real_pack_and_is_idempotent(db, raw_root):
    """`augment` over rows, which is where the two rules meet the store.

    The base pack has to survive as an exact prefix and a second augmentation must replace rather
    than stack -- asserted together because a builder that rebuilt the base instead of cutting at
    the sentinel would pass either one alone.
    """
    await seed_title(db, 9, name="Parasite", year=2019)
    await add_reviews(db, 9, [("trakt", long_review("evidence"), False)])
    await seed_wikipedia_article(db, 9, (
        "== Music ==\nJung Jae-il's score is minimalist piano pieces, punctuated with light "
        "percussion, and it runs under the film's long silences rather than over them.\n"
    ))

    base, _info = await packs.build_pack(db, 9)
    once, info = await craft.augment(db, 9, base)
    twice, _again = await craft.augment(db, 9, once)

    assert once.startswith(base.rstrip("\n"))
    assert craft.base_pack(once) == base
    assert once == twice
    assert once.count(craft.SENTINEL) == 1
    assert info.n_sections == 1
    assert info.added > 0
    assert norm("minimalist piano pieces") in norm(once)


async def test_a_rotten_tomatoes_critic_blurb_below_the_pack_floor_reaches_the_supplement(
    db, raw_root
):
    """The RT half, and named port change 3's bound.

    "77,395 of the 77,420 Rotten Tomatoes critic reviews over the library are dropped by the word
    floor (median 25 words)", and they are the only large pool of critic prose there is. The
    bound above is what keeps the 25 that clear the floor from being carried twice: a review in
    both halves would be quotable from two places in one pack. Four rows, one of each kind that
    the reader has to tell apart.
    """
    await seed_title(db, 9, name="Parasite", year=2019)
    await add_reviews(db, 9, [
        ("rottentomatoes", long_review("blurb", 20), True),
        ("rottentomatoes", long_review("verdict", 5), True),
        ("rottentomatoes", long_review("user", 20), False),
        ("rottentomatoes", long_review("essay", 60), True),
    ])

    blurbs = await craft.rt_critics(db, 9)
    assert len(blurbs) == 1
    assert blurbs[0].startswith("blurb"), (
        "the supplement took a verdict under RT_MIN_WORDS, a non-critic row, or an essay the "
        f"base pack already carries: {[b.split()[0] for b in blurbs]}"
    )

    text, _info = await packs.build_pack(db, 9)
    assert "essay" in text and "blurb" not in text


async def test_a_series_gets_no_rotten_tomatoes_blurbs(db, raw_root):
    """SERIES GET NONE, ported with its measurement: the Kaggle source is a *movies* dataset
    matched by RT link, so a series sharing a slug with a film collects that film's reviews --
    HBO's Euphoria carries 14 notices for the 2017 Vikander/Green film of the same name."""
    await seed_title(db, 7, name="The Bear", year=2022, kind="series")
    await add_reviews(db, 7, [("rottentomatoes", long_review("blurb", 20), True)])

    assert await craft.rt_critics(db, 7) == []


async def test_this_install_produces_no_craft_supplement_at_all(db, raw_root):
    """Decision 391's other half, stated as an assertion for the reason the first one is.

    A title carrying exactly what the fixture bundle ships -- three short reviews, no
    `rottentomatoes` row, and no `raw_document` of any source -- gets an empty supplement and an
    unchanged pack. Neither half is broken: there is nothing on this install for either to read,
    and §8 stage 2's wikipedia fetcher is M5.3's. The day one of those changes, this test fails
    and the docstring's measurement gets re-taken instead of quietly ageing.
    """
    await seed_title(db)
    await add_reviews(db, 1, [(src, body, False) for src, body, _w in FIXTURE_REVIEWS])

    assert await db.fetchval("SELECT count(*) FROM raw_document") == 0
    assert await craft.wiki_craft(db, 1) == []
    assert await craft.rt_critics(db, 1) == []
    assert await craft.supplement(db, 1) == ("", 0, 0, 0)

    base, _info = await packs.build_pack(db, 1)
    augmented, info = await craft.augment(db, 1, base)
    assert augmented == base
    assert info.added == 0
