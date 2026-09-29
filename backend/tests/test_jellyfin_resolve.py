"""Jellyfin item -> title, fill-never-clobber (§7.1, §4.1 rules 5 and 6). `imdb_id` is NULL on 21%
of titles and 315 tmdb ids are shared by movie/series pairs. Needs TEST_DATABASE_URL."""

from __future__ import annotations

from spielplan.connectors import resolve


async def _title(db, title_id, kind, name, year=None, **ids):
    await db.execute(
        """
        INSERT INTO title (id, kind, name, year, imdb_id, tmdb_id, tvdb_id, jellyfin_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        title_id, kind, name, year,
        ids.get("imdb_id"), ids.get("tmdb_id"), ids.get("tvdb_id"), ids.get("jellyfin_id"),
    )
    return title_id


def item(**kwargs):
    base = {"Id": "jf-1", "Name": "Heat", "Type": "Movie", "ProductionYear": 1995,
            "ProviderIds": {}}
    return {**base, **kwargs}


def test_provider_ids_are_read_case_insensitively():
    """Jellyfin versions and plugins disagree about capitalisation."""
    ids = resolve.identity(item(ProviderIds={"TMDB": "949", "Imdb": "tt0113277"}))
    assert ids == {"imdb_id": "tt0113277", "tmdb_id": 949, "tvdb_id": None}


def test_a_non_numeric_tmdb_id_does_not_crash_the_import():
    assert resolve.identity(item(ProviderIds={"Tmdb": "not-a-number"}))["tmdb_id"] is None


def test_only_movies_and_series_are_recognised():
    """§4.1 rule 5: `kind` is `movie | series`; an Episode row must not become a title."""
    assert resolve.kind_of(item(Type="Episode")) is None
    assert resolve.kind_of(item(Type="Movie")) == "movie"
    assert resolve.kind_of(item(Type="Series")) == "series"


async def test_an_imdb_match_is_qualified_by_kind(db):
    """A Series folder mapped onto a movie would take the movie's Played write, recursively."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277")
    as_movie = await resolve.resolve_title_id(db, item(ProviderIds={"Imdb": "tt0113277"}))
    as_series = await resolve.resolve_title_id(
        db, item(Type="Series", Name="Heat: The Series", ProviderIds={"Imdb": "tt0113277"})
    )
    assert (as_movie, as_series) == (1, None)


async def test_a_tmdb_match_is_qualified_by_kind(db):
    await _title(db, 1, "movie", "Shared", 2001, tmdb_id=11104)
    await _title(db, 2, "series", "Shared", 2001, tmdb_id=11104)

    as_movie = await resolve.resolve_title_id(db, item(Type="Movie", ProviderIds={"Tmdb": "11104"}))
    as_series = await resolve.resolve_title_id(
        db, item(Type="Series", ProviderIds={"Tmdb": "11104"})
    )
    assert (as_movie, as_series) == (1, 2)


async def test_an_item_with_no_provider_ids_falls_back_to_name_and_year(db):
    await _title(db, 8, "movie", "Tampopo", 1985)
    found = await resolve.resolve_title_id(db, item(Name="Tampopo", ProductionYear=1985))
    assert found == 8


async def test_the_name_fallback_matches_an_alias(db):
    await _title(db, 4, "movie", "Chungking Express", 1994)
    await db.execute(
        "INSERT INTO title_alias (title_id, alias) VALUES (4, '重慶森林')"
    )
    found = await resolve.resolve_title_id(db, item(Name="重慶森林", ProductionYear=1994))
    assert found == 4


async def test_the_name_fallback_respects_the_year(db):
    """"The Office" without a year matches two shows. The year is what makes the last-resort
    match defensible at all."""
    await _title(db, 10, "series", "The Office", 2001)
    await _title(db, 11, "series", "The Office", 2005)
    assert await resolve.resolve_title_id(db, item(Type="Series", Name="The Office",
                                                   ProductionYear=2005)) == 11


async def test_an_unresolvable_item_is_reported_and_creates_nothing(db):
    """§4.2: `title.id` is carried over from the corpus; minting one here could never be reconciled."""
    report = resolve.ResolveReport()
    assert await resolve.upsert_item(db, item(Name="Christmas 2019", ProductionYear=2019),
                                     report) is None
    assert [entry.name for entry in report.unmatched] == ["Christmas 2019"]
    assert await db.fetchval("SELECT count(*) FROM title") == 0


async def test_a_refused_item_is_reported_with_the_ids_its_consumer_keys_on(db):
    """Decision 323: stage 1 mints only on a provider id, so the report keys as `key_for_item` reads it."""
    report = resolve.ResolveReport()
    refused = item(Id="jf-99", Name="Christmas 2019", ProductionYear=2019,
                   ProviderIds={"Tmdb": "424242", "Imdb": ""})

    assert await resolve.upsert_item(db, refused, report) is None

    (entry,) = report.unmatched
    assert (entry.jellyfin_id, entry.name) == ("jf-99", "Christmas 2019")
    assert entry.provider_ids == {"tmdb": "424242"}, "lowercased, and an empty id is not an id"
    assert resolve.identity({"ProviderIds": entry.provider_ids}) == {
        "imdb_id": None, "tmdb_id": 424242, "tvdb_id": None
    }


def test_the_refusal_report_keeps_the_wire_shape_the_admin_card_renders():
    """Widened in memory, not on the wire: §6.6's card renders `unmatched` as a count."""
    report = resolve.ResolveReport()
    report.unmatched = [
        resolve.UnmatchedItem(jellyfin_id=f"jf-{n}", name=f"Item {n}", provider_ids={})
        for n in range(21)
    ]

    wire = report.as_dict()

    assert set(wire) == {"matched", "matched_titles", "unmatched", "unmatched_names",
                         "filled", "relinked"}
    assert wire["unmatched"] == 21
    assert wire["unmatched_names"] == [f"Item {n}" for n in range(20)]


async def test_a_null_identity_column_is_filled(db):
    # Through `upsert_items`: the representative `jellyfin_id` is elected over the whole page-set (§7.1).
    await _title(db, 3, "movie", "Paddington 2", 2017, tmdb_id=346648)
    report = await resolve.upsert_items(
        db, [item(Id="jf-3", Name="Paddington 2", ProductionYear=2017,
                  ProviderIds={"Tmdb": "346648", "Imdb": "tt4468740"})],
    )
    row = await db.fetchrow("SELECT imdb_id, jellyfin_id FROM title WHERE id = 3")
    assert row["imdb_id"] == "tt4468740"
    assert row["jellyfin_id"] == "jf-3"
    assert report.filled == {"imdb_id": 1}


async def test_an_existing_identity_column_is_never_overwritten(db):
    """The corpus is curated; Jellyfin's ProviderIds are a scraper's guess. The corpus wins."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277", tmdb_id=949)
    report = resolve.ResolveReport()
    await resolve.upsert_item(
        db, item(ProviderIds={"Imdb": "tt0113277", "Tmdb": "999999"}), report
    )
    row = await db.fetchrow("SELECT imdb_id, tmdb_id FROM title WHERE id = 1")
    assert (row["imdb_id"], row["tmdb_id"]) == ("tt0113277", 949)
    assert report.filled == {}


async def test_two_titles_sharing_a_tmdb_id_both_survive_the_upsert(db):
    await _title(db, 4, "movie", "Chungking Express", 1994, tmdb_id=11104)
    await _title(db, 5, "series", "Chungking Express", 1994, tmdb_id=11104)
    report = await resolve.upsert_items(
        db,
        [
            item(Id="jf-4", Type="Movie", Name="Chungking Express", ProductionYear=1994,
                 ProviderIds={"Tmdb": "11104"}),
            item(Id="jf-5", Type="Series", Name="Chungking Express", ProductionYear=1994,
                 ProviderIds={"Tmdb": "11104"}),
        ],
    )
    rows = await db.fetch("SELECT id, jellyfin_id FROM title ORDER BY id")
    assert [(r["id"], r["jellyfin_id"]) for r in rows] == [(4, "jf-4"), (5, "jf-5")]
    assert (report.matched, report.relinked) == (2, 0)


async def test_ownership_is_re_derived_not_trusted_stale(db):
    """§7.2: "is_owned = false … flag re-derived from Jellyfin, never trusted stale"."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277")
    assert await db.fetchval("SELECT is_owned FROM title WHERE id = 1") is False
    await resolve.upsert_items(db, [item(ProviderIds={"Imdb": "tt0113277"})])
    row = await db.fetchrow("SELECT is_owned, owned_checked_at FROM title WHERE id = 1")
    assert row["is_owned"] is True
    assert row["owned_checked_at"] is not None


async def test_a_title_the_sweep_un_owned_is_owned_again_when_it_comes_back(db):
    """`_falsify_ownership` writes a FRESH `owned_checked_at`, so `NOT is_owned` alone must flip it back."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277", jellyfin_id="jf-1")
    # `_falsify_ownership`'s own SET clause, seeded directly so the precondition stays visible.
    await db.execute(
        "UPDATE title SET is_owned = false, owned_checked_at = now() WHERE id = 1"
    )
    seeded = await db.fetchrow("SELECT is_owned, owned_checked_at FROM title WHERE id = 1")
    assert (seeded["is_owned"], seeded["owned_checked_at"] is None) == (False, False), (
        "the state under test is a fresh falsification, not a title no sweep has ever owned"
    )

    await resolve.upsert_items(db, [item(ProviderIds={"Imdb": "tt0113277"})])

    row = await db.fetchrow("SELECT is_owned, owned_checked_at FROM title WHERE id = 1")
    assert row["is_owned"] is True, (
        "a title the sweep un-owned must be owned again by the sweep that sees it return, "
        "inside the hour and not after it"
    )
    assert row["owned_checked_at"] > seeded["owned_checked_at"], (
        "the re-derivation has to be dated, or the next sweep cannot tell it from a stale flag"
    )


async def test_a_rebuilt_library_relinks_and_says_so(db):
    """The old id being gone from the library is what makes this a re-link; one item cannot see that."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277", jellyfin_id="old-id")
    report = await resolve.upsert_items(db, [item(Id="new-id", ProviderIds={"Imdb": "tt0113277"})])
    assert await db.fetchval("SELECT jellyfin_id FROM title WHERE id = 1") == "new-id"
    assert report.relinked == 1


async def test_reimporting_the_same_library_is_idempotent(db):
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277")
    items = [item(ProviderIds={"Imdb": "tt0113277"})]
    first = await resolve.upsert_items(db, items)
    second = await resolve.upsert_items(db, items)
    assert (first.matched, second.matched) == (1, 1)
    assert second.relinked == 0
    assert await db.fetchval("SELECT count(*) FROM title") == 1


async def test_an_already_linked_item_resolves_by_its_jellyfin_id(db):
    await _title(db, 1, "movie", "Renamed In Jellyfin", 1995, jellyfin_id="jf-1")
    assert await resolve.resolve_title_id(db, item(Id="jf-1", Name="Heat")) == 1


async def test_an_unchanged_row_is_not_rewritten(db):
    """Once per item per user every fifteen minutes: ~11,000 dead row versions per user per cycle."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277")
    payload = [item(ProviderIds={"Imdb": "tt0113277"})]

    await resolve.upsert_items(db, payload)
    before = await db.fetchval("SELECT updated_at FROM title WHERE id = 1")
    await resolve.upsert_items(db, payload)
    after = await db.fetchval("SELECT updated_at FROM title WHERE id = 1")

    assert after == before, "an unchanged row must not be rewritten"


async def test_a_changed_row_is_still_rewritten(db):
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277")
    await resolve.upsert_items(db, [item(ProviderIds={"Imdb": "tt0113277"})])
    before = await db.fetchval("SELECT updated_at FROM title WHERE id = 1")

    await resolve.upsert_items(db, [item(Id="jf-rebuilt", ProviderIds={"Imdb": "tt0113277"})])
    row = await db.fetchrow("SELECT jellyfin_id, updated_at FROM title WHERE id = 1")
    assert row["jellyfin_id"] == "jf-rebuilt"
    assert row["updated_at"] > before


async def test_a_name_matching_two_titles_is_refused_not_guessed(db):
    """573 `(kind, name, year)` groups collide in the corpus;
    a wrong match is invisible, no match is reported."""
    await _title(db, 20, "series", "The Bureau", 2015)
    await _title(db, 21, "series", "The Bureau", 2015)

    found = await resolve.resolve_title_id(
        db, item(Type="Series", Name="The Bureau", ProductionYear=2015)
    )
    assert found is None

    report = await resolve.upsert_items(
        db, [item(Type="Series", Name="The Bureau", ProductionYear=2015)]
    )
    assert [entry.name for entry in report.unmatched] == ["The Bureau"]
    assert report.matched == 0


async def test_an_item_with_no_production_year_never_matches_on_name_alone(db):
    """An item with neither a provider id nor a year is not identified at all."""
    await _title(db, 22, "movie", "Tampopo", 1985)

    assert await resolve.resolve_title_id(db, item(Name="Tampopo", ProductionYear=None)) is None
    report = await resolve.upsert_items(db, [item(Name="Tampopo", ProductionYear=None)])
    assert [entry.name for entry in report.unmatched] == ["Tampopo"]
    assert await db.fetchval("SELECT is_owned FROM title WHERE id = 22") is False


def _copies() -> list[dict]:
    """The "Movies" and "Movies 4K" household §7.3's conflict rule is written for."""
    return [
        item(Id="jf-1b", ProviderIds={"Imdb": "tt0113277"}),
        item(Id="jf-1", ProviderIds={"Imdb": "tt0113277"}),
    ]


async def test_every_copy_of_a_title_is_recorded_not_just_the_representative(db):
    """One `jellyfin_id` per title cannot answer "which
    items are this film", so "not seen" missed a copy."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277")
    report = await resolve.upsert_items(db, _copies())

    rows = await db.fetch("SELECT jellyfin_id, title_id FROM title_jellyfin_item ORDER BY 1")
    assert [(r["jellyfin_id"], r["title_id"]) for r in rows] == [("jf-1", 1), ("jf-1b", 1)]
    # Every copy, the title set ownership may trust, and the `kind` decision 210's series guards need.
    assert report.items == {"jf-1b": 1, "jf-1": 1}
    assert report.matched_title_ids == {1}
    assert report.kinds == {1: "movie"}
    assert report.as_dict()["matched_titles"] == 1


async def test_two_copies_of_one_title_are_one_decision_not_two_relinks(db):
    """The pointer is decided once per sweep, so an unchanged library writes nothing at all."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277")

    first = await resolve.upsert_items(db, _copies())
    before = await db.fetchval("SELECT updated_at FROM title WHERE id = 1")
    second = await resolve.upsert_items(db, _copies())
    after = await db.fetchval("SELECT updated_at FROM title WHERE id = 1")

    assert (first.relinked, second.relinked) == (0, 0)
    assert after == before, "a title whose copies did not change must not be rewritten"
    assert await db.fetchval("SELECT count(*) FROM title_jellyfin_item") == 2


async def test_the_representative_is_kept_while_its_copy_is_still_in_the_library(db):
    """Deterministic and never reads a Played flag: per-user election flip-flopped the pointer."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277", jellyfin_id="jf-1b")
    played_the_other = [dict(copy, UserData={"Played": copy["Id"] == "jf-1"})
                        for copy in _copies()]

    report = await resolve.upsert_items(db, played_the_other)

    assert await db.fetchval("SELECT jellyfin_id FROM title WHERE id = 1") == "jf-1b"
    assert report.relinked == 0


async def test_the_representative_falls_to_the_lowest_live_copy_when_its_own_is_gone(db):
    """`relinked` means "the library was rebuilt", not "this title has two copies"."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277", jellyfin_id="jf-gone")

    report = await resolve.upsert_items(db, _copies())

    assert await db.fetchval("SELECT jellyfin_id FROM title WHERE id = 1") == "jf-1"
    assert report.relinked == 1


async def test_a_copy_that_left_the_library_is_pruned_only_against_a_read_that_happened(db):
    """An outage or the page cap yields a truncated page-set, and a prune must not trust one."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277")
    await resolve.upsert_items(db, _copies())

    nothing_read = resolve.ResolveReport()
    assert await resolve.prune_missing_items(db, nothing_read) == 0
    assert await db.fetchval("SELECT count(*) FROM title_jellyfin_item") == 2

    one_copy_left = await resolve.upsert_items(db, [item(Id="jf-1", ProviderIds={"Imdb": "tt0113277"})])
    assert await resolve.prune_missing_items(db, one_copy_left) == 1
    assert await db.fetchval("SELECT jellyfin_id FROM title_jellyfin_item") == "jf-1"
