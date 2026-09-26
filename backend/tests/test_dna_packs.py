"""The pack, the craft supplement and pack custody (§8 stage 5). The pack must keep its sources' markup.
The fixture's three short reviews cannot make a pack (decision 391), so rows are inserted here.
Needs TEST_DATABASE_URL for the database half."""

from __future__ import annotations

import hashlib
import json

import asyncpg
import pytest

from spielplan.acquire import rawstore
from spielplan.core.config import settings
from spielplan.dna import craft, packs, verify
from spielplan.dna.norm import norm

# `make_bundle.py`'s review rows verbatim, with the word counts `word_count` computes.
FIXTURE_REVIEWS = (
    ("metacritic", "A city film that keeps its distance and earns it.", 10),
    ("trakt", "Bleak, and it does not blink.", 6),
    # Escapes, not characters: a failure prints the source line to a cp1252 console.
    ("letterboxd", "\u738b\u5bb6\u885b\u306e\u6620\u50cf\u306f\u4eca\u3082\u65b0\u3057\u3044\u3002", 1),
)


def long_review(word: str, words: int = 60) -> str:
    """Distinguishable by its first word, so an assertion about WHICH reviews survived reads as one."""
    return " ".join([word] * words)


@pytest.fixture
def raw_root(tmp_path, monkeypatch):
    """`settings().raw_dir` takes no argument, so the root goes through `DATA_DIR`."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings.cache_clear()
    yield settings().raw_dir
    settings.cache_clear()


def blocks(text: str) -> list[tuple[str, str]]:
    """Parsed, not a golden string, so a test about the interleave does not fail on a header change."""
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines):
        if line.startswith("[") and line.endswith("]") and i + 1 < len(lines):
            out.append((line[1:-1], lines[i + 1]))
    return out


def test_the_interleave_alternates_across_sources_rather_than_concatenating():
    """Round n of every source precedes round n+1 of any:
    concatenate-then-shuffle would pass a weaker claim."""
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
    """Six sources of twelve: a concatenating builder fills sixty places from five and drops the sixth."""
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
    """Seven sources of twelve: without the inner check the loop emits sixty-three."""
    rows = [(f"src{i}", long_review(f"s{i}-{n}")) for i in range(7) for n in range(12)]
    text, info = packs.render_pack(1, "Heat", 1995, "movie", None, None, None, rows)

    assert info.n_reviews == packs.MAX_REVIEWS
    markers = [m for m, _body in blocks(text) if ":" in m]
    assert [int(m.split(":")[1]) for m in markers[-4:]] == [9, 9, 9, 9]


def test_a_review_is_cut_at_the_character_cap():
    """Both directions: `<=` instead of a slice is off by one on exactly one input length."""
    at_cap = "x" * packs.MAX_CHARS
    over = "y" * (packs.MAX_CHARS + 500)
    text, _info = packs.render_pack(
        1, "Heat", 1995, "movie", None, None, None, [("a", at_cap), ("b", over)],
    )
    bodies = {m.split(":")[0]: body for m, body in blocks(text)}
    assert bodies["a"] == at_cap
    assert bodies["b"] == "y" * packs.MAX_CHARS


def test_the_plot_is_cut_at_its_own_cap():
    text, _info = packs.render_pack(
        1, "Heat", 1995, "movie", None, None, "z" * 10_000, [],
    )
    assert dict(blocks(text))["plot:1"] == "z" * packs.MAX_PLOT_CHARS


def test_a_series_header_tells_the_extractor_to_describe_the_show_as_a_whole():
    """One pack for the whole show; the header sentence is the entire mechanism, so it is verbatim."""
    text, _info = packs.render_pack(7, "The Bear", 2022, "series", 3, 28, None, [])
    assert "[type] series" in text
    assert ("[note] Series: 3 season(s), 28 episodes. Reviews below may discuss different "
            "seasons; describe the show AS A WHOLE.") in text


def test_a_film_header_carries_no_series_note():
    text, _info = packs.render_pack(1, "Heat", 1995, "movie", None, None, None, [])
    assert "[type] film" in text
    assert "AS A WHOLE" not in text


def test_a_series_with_no_counts_says_it_does_not_know_rather_than_claiming_none():
    """A header claiming "0 season(s)" would tell the model something false."""
    text, _info = packs.render_pack(7, "The Bear", 2022, "series", None, None, None, [])
    assert "[note] Series: ? season(s), ? episodes." in text


def test_the_largest_count_any_source_claims_is_the_one_the_header_states():
    """The counts disagree per source; the largest is stated, and a non-digit value is dropped."""
    rows = [{"season_count": "1"}, {"season_count": "3"}, {"season_count": None}]
    assert packs._largest_count(rows, "season_count") == 3
    assert packs._largest_count([{"season_count": "3 (ordered)"}], "season_count") is None
    assert packs._largest_count([], "season_count") is None


def test_clean_strips_html_tags_and_collapses_whitespace():
    """`clean()` is the whole of what the builder removes, and this is the list."""
    assert packs.clean("<p>a  <b>bold</b>\n\n claim</p>") == "a bold claim"
    assert packs.clean(None) == ""
    assert packs.clean("   ") == ""


def test_the_pack_keeps_the_markup_its_sources_published():
    """A "cleaner" pack verifies FEWER quotes: the fold needs the markup to still be there."""
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
    """A sha derived from anything but the pack text marked
    825 titles current against a pack no pass saw."""
    text, info = packs.render_pack(1, "Heat", 1995, "movie", None, None, "A plot.", [])
    assert info.sha == packs.sha(text)
    assert info.chars == len(text)


def test_two_packs_that_differ_by_one_character_get_different_shas():
    a, info_a = packs.render_pack(1, "Heat", 1995, "movie", None, None, "A plot.", [])
    b, info_b = packs.render_pack(1, "Heat", 1995, "movie", None, None, "A plot!", [])
    assert a != b
    assert info_a.sha != info_b.sha


def test_a_title_with_no_plot_and_no_reviews_still_gets_a_pack():
    """"Nothing to extract" is stage 4's verdict, not the builder's."""
    text, info = packs.render_pack(8, "Tampopo", 1985, "movie", None, None, None, [])
    assert text.startswith("# Tampopo (1985)")
    assert info.n_reviews == 0
    assert info.n_sources == 0


def test_augmenting_twice_leaves_one_supplement():
    """A second copy would double every craft quote's chance of verifying."""
    base = "# Heat (1995)\n[type] film\n\n[plot:1]\nA plot.\n"
    sup = f"{craft.SENTINEL}\n[note] more\n\n[wiki:music]\nminimalist piano pieces\n"

    once = craft.apply_supplement(base, sup)
    twice = craft.apply_supplement(once, sup)

    assert once == twice
    assert once.count(craft.SENTINEL) == 1


def test_an_augmented_pack_keeps_the_base_as_an_exact_prefix():
    """Rewriting the body would drop every previously verified quote."""
    base = "# Heat (1995)\n[type] film\n\n[plot:1]\nA **plot**.\n"
    sup = f"{craft.SENTINEL}\n[note] more\n\n[wiki:music]\nminimalist piano pieces\n"

    once = craft.apply_supplement(base, sup)

    assert once.startswith(base.rstrip("\n"))
    assert craft.base_pack(once) == base
    assert norm("A plot") in norm(once), "a quote that verified against the base stopped doing so"


def test_an_empty_supplement_strips_one_that_is_already_there():
    """An article deleted upstream: the pack goes back to its base."""
    base = "# Heat (1995)\n[type] film\n"
    once = craft.apply_supplement(base, f"{craft.SENTINEL}\n[wiki:music]\nprose\n")
    assert craft.apply_supplement(once, "") == base


def test_a_nested_craft_heading_is_found_without_walking_the_tree():
    """`=== Music ===` under `== Production ==` is found without walking the tree."""
    extract = ("== Production ==\nHow it came to exist.\n"
               "=== Music ===\nMinimalist piano pieces, punctuated with light percussion.\n"
               "== Reception ==\nCritics liked it.\n")
    found = dict(craft.sections(extract))
    assert set(found) == {"Production", "Music", "Reception"}
    assert "Minimalist piano pieces" in found["Music"]


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
    """`title.overview` is the per-field choice; the pack wants the most evidence, so longest wins."""
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
    """Carried by exactly one source, and better than nothing."""
    await seed_title(db)
    await db.execute(
        "INSERT INTO title_meta (title_id, source, payload) VALUES ($1, $2, $3)",
        1, "wikipedia", {"plot_short": "A one-line synthetic summary."},
    )

    text, _info = await packs.build_pack(db, 1)
    assert dict(blocks(text))["plot:1"] == "A one-line synthetic summary."


async def test_two_sources_whose_plots_tie_pick_the_same_one_after_a_re_import(db):
    """`load_title_meta` re-inserts on every import, so a tie without a tie-break moved `pack_sha`."""
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


def test_a_shared_plot_gives_way_to_the_next_longest_and_the_order_is_retaken():
    """The field is dropped, not the row, and the ranking is retaken over what is left."""
    rows = [
        {"source": "wikipedia", "plot_full": "w" * 50, "plot_short": "its own one-liner"},
        {"source": "tmdb", "plot_full": "t" * 10, "plot_short": None},
    ]
    assert packs._pick_plot(rows, set()) == "w" * 50
    assert packs._pick_plot(rows, {("wikipedia", "plot_full")}) == "t" * 10
    assert packs._pick_plot(
        rows, {("wikipedia", "plot_full"), ("tmdb", "plot_full")}
    ) == "its own one-liner"
    assert packs._pick_plot([], set()) is None


async def test_a_plot_another_title_carries_never_reaches_the_pack(db):
    """157 seeded titles had another film's Wikipedia page as their longest text (decision 499)."""
    await seed_title(db)
    await seed_title(db, 2, name="Oppenheimer", year=2023)
    collided = "A 1959 Senate committee questions a synthetic physicist at length. " * 20
    await db.executemany(
        "INSERT INTO title_meta (title_id, source, payload) VALUES ($1, $2, $3)",
        [
            (1, "tmdb", {"plot_full": "A short plot of its own."}),
            (1, "wikipedia", {"plot_full": collided}),
            (2, "wikipedia", {"plot_full": collided}),
        ],
    )
    text, _info = await packs.build_pack(db, 1)
    assert dict(blocks(text))["plot:1"] == "A short plot of its own."

    retelling = "A synthetic retelling of this film alone, ending included. " * 10
    await db.execute(
        "INSERT INTO title_meta (title_id, source, payload) VALUES (1, 'mpst', $1)",
        {"plot_full": retelling},
    )
    text, _info = await packs.build_pack(db, 1)
    assert dict(blocks(text))["plot:1"] == packs.clean(retelling)


async def test_a_review_under_the_word_floor_never_reaches_the_pack(db):
    """`word_count` is GENERATED STORED, so the floor reads Postgres's count."""
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
    """No `helpful_yes`, so equal-length reviews tie; an unstable tie changes the pack's sha."""
    await seed_title(db)
    await add_reviews(db, 1, [("trakt", long_review(f"tie{n}"), False) for n in range(20)])

    first, info_first = await packs.build_pack(db, 1)
    second, info_second = await packs.build_pack(db, 1)

    assert first == second
    assert info_first.sha == info_second.sha


async def test_the_series_header_reads_its_counts_from_the_meta_payload(db):
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
    """When the fixture gains a real review this fails, and the measurement gets re-taken."""
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


async def test_store_pack_writes_one_index_row_and_bytes_that_read_back_unchanged(db, raw_root):
    """The bytes `rawstore.read` returns are the bytes whose digest `pack_sha` records."""
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
    """The raw store keeps every write; the index answers with one row per (title, version)."""
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
    """`render_pack` takes no version, so an unchanged title's pack is byte-identical under v1 and v2."""
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
    """The header names the title, not its id, so two
    titles can share a pack; the key includes the title."""
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
    """After `craft.augment`, the base `PackInfo` is the only value in scope that fits the signature."""
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
    """SET NULL made "evidence deleted" read as "never packed"; RESTRICT refuses the delete."""
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
    """The schema refuses the NULL state itself, not only the route to it."""
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
    """Inherited and kept; 0028's `llm_call` references this value."""
    text = "# Heat (1995)\n[type] film\n"
    full = hashlib.sha256(text.encode("utf-8")).hexdigest()

    assert packs.sha(text) == full[:16]
    assert len(packs.sha(text)) == 16 and len(full) == 64


async def seed_wikipedia_article(db, title_id, extract, *, key="jellyfin:abc") -> int:
    """The task row is not decoration: documents are found
    by joining `entity_key` to `acquisition_task.key`."""
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
    """Parasite's pack says nothing of its score; its Wikipedia article does."""
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
    """A builder that rebuilt the base instead of cutting at the sentinel would pass either rule alone."""
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
    """A review in both halves would be quotable from two places in one pack."""
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
    """The Kaggle source is a movies dataset matched by RT
    link: a same-named series gets a film's reviews."""
    await seed_title(db, 7, name="The Bear", year=2022, kind="series")
    await add_reviews(db, 7, [("rottentomatoes", long_review("blurb", 20), True)])

    assert await craft.rt_critics(db, 7) == []


async def test_this_install_produces_no_craft_supplement_at_all(db, raw_root):
    """When the fixture changes this fails, and the measurement gets re-taken."""
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
