"""Jellyfin item -> title, fill-never-clobber. Spec v2.1 §7.1, §4.1 rules 5 and 6.

Two corpus measurements are the whole reason this resolver has the shape it does, and both
are asserted here rather than trusted:

  * `imdb_id` is NULL on 21% of titles, so it can never be the only join key;
  * 315 `tmdb_id` values are legitimately duplicated, nearly all a movie and a series sharing
    an id — which is why every tmdb/tvdb match is qualified by `kind` and why §4.1 rule 6 bans
    the UNIQUE constraint that would "fix" them by losing one.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

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


# --- identity extraction -----------------------------------------------------------------


def test_provider_ids_are_read_case_insensitively():
    """Jellyfin versions and plugins disagree about capitalisation; a case-sensitive lookup
    silently drops identity on some libraries."""
    ids = resolve.identity(item(ProviderIds={"TMDB": "949", "Imdb": "tt0113277"}))
    assert ids == {"imdb_id": "tt0113277", "tmdb_id": 949, "tvdb_id": None}


def test_a_non_numeric_tmdb_id_does_not_crash_the_import():
    assert resolve.identity(item(ProviderIds={"Tmdb": "not-a-number"}))["tmdb_id"] is None


def test_only_movies_and_series_are_recognised():
    """§4.1 rule 5: `kind` is `movie | series` and nothing else. An Episode row arriving from
    a library scan must not become a title."""
    assert resolve.kind_of(item(Type="Episode")) is None
    assert resolve.kind_of(item(Type="Movie")) == "movie"
    assert resolve.kind_of(item(Type="Series")) == "series"


# --- resolution order ---------------------------------------------------------------------


async def test_imdb_id_resolves_across_kinds(db):
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277")
    found = await resolve.resolve_title_id(db, item(ProviderIds={"Imdb": "tt0113277"}))
    assert found == 1


async def test_a_tmdb_match_is_qualified_by_kind(db):
    """The 315-duplicate case, made concrete: one tmdb_id, two titles, two kinds."""
    await _title(db, 1, "movie", "Shared", 2001, tmdb_id=11104)
    await _title(db, 2, "series", "Shared", 2001, tmdb_id=11104)

    as_movie = await resolve.resolve_title_id(db, item(Type="Movie", ProviderIds={"Tmdb": "11104"}))
    as_series = await resolve.resolve_title_id(
        db, item(Type="Series", ProviderIds={"Tmdb": "11104"})
    )
    assert (as_movie, as_series) == (1, 2)


async def test_an_item_with_no_provider_ids_falls_back_to_name_and_year(db):
    """21% of titles carry no imdb id, and plenty of Jellyfin libraries carry none at all."""
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
    """§4.2: `title.id` is "carried over verbatim from the corpus". Minting one here would
    create a row no bundle could ever reconcile. Acquiring genuinely new titles is §8, at M5."""
    report = resolve.ResolveReport()
    assert await resolve.upsert_item(db, item(Name="Christmas 2019", ProductionYear=2019),
                                     report) is None
    assert report.unmatched == ["Christmas 2019"]
    assert await db.fetchval("SELECT count(*) FROM title") == 0


# --- fill, never clobber -------------------------------------------------------------------


async def test_a_null_identity_column_is_filled(db):
    await _title(db, 3, "movie", "Paddington 2", 2017, tmdb_id=346648)
    report = resolve.ResolveReport()
    await resolve.upsert_item(
        db, item(Id="jf-3", Name="Paddington 2", ProductionYear=2017,
                 ProviderIds={"Tmdb": "346648", "Imdb": "tt4468740"}),
        report,
    )
    row = await db.fetchrow("SELECT imdb_id, jellyfin_id FROM title WHERE id = 3")
    assert row["imdb_id"] == "tt4468740"
    assert row["jellyfin_id"] == "jf-3"
    assert report.filled == {"imdb_id": 1}


async def test_an_existing_identity_column_is_never_overwritten(db):
    """The bundle is derived from a curated corpus; Jellyfin's ProviderIds are whatever a
    scraper guessed. When they disagree the corpus wins."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277", tmdb_id=949)
    report = resolve.ResolveReport()
    await resolve.upsert_item(
        db, item(ProviderIds={"Imdb": "tt0113277", "Tmdb": "999999"}), report
    )
    row = await db.fetchrow("SELECT imdb_id, tmdb_id FROM title WHERE id = 1")
    assert (row["imdb_id"], row["tmdb_id"]) == ("tt0113277", 949)
    assert report.filled == {}


async def test_two_titles_sharing_a_tmdb_id_both_survive_the_upsert(db):
    """§4.1 rule 6 in action: no UNIQUE constraint, so the pair is preserved, not deduplicated."""
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


async def test_a_rebuilt_library_relinks_and_says_so(db):
    """Jellyfin item ids change when a library is rebuilt. The current server's id is the
    useful one — the deep link and the Played write both need it — so it is replaced, and the
    replacement is counted rather than swallowed."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277", jellyfin_id="old-id")
    report = resolve.ResolveReport()
    await resolve.upsert_item(db, item(Id="new-id", ProviderIds={"Imdb": "tt0113277"}), report)
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
    """Cheapest and most certain: this server already told us which title this is."""
    await _title(db, 1, "movie", "Renamed In Jellyfin", 1995, jellyfin_id="jf-1")
    assert await resolve.resolve_title_id(db, item(Id="jf-1", Name="Heat")) == 1


async def test_an_unchanged_row_is_not_rewritten(db):
    """This runs once per item per linked user every fifteen minutes. An unconditional write
    over an unchanged library is ~11,000 dead row versions per user per cycle — two million a
    day on a box the spec sizes at 4 vCPU."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277")
    payload = [item(ProviderIds={"Imdb": "tt0113277"})]

    await resolve.upsert_items(db, payload)
    before = await db.fetchval("SELECT updated_at FROM title WHERE id = 1")
    await resolve.upsert_items(db, payload)
    after = await db.fetchval("SELECT updated_at FROM title WHERE id = 1")

    assert after == before, "an unchanged row must not be rewritten"


async def test_a_changed_row_is_still_rewritten(db):
    """The guard must not become a way to miss a real change."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277")
    await resolve.upsert_items(db, [item(ProviderIds={"Imdb": "tt0113277"})])
    before = await db.fetchval("SELECT updated_at FROM title WHERE id = 1")

    await resolve.upsert_items(db, [item(Id="jf-rebuilt", ProviderIds={"Imdb": "tt0113277"})])
    row = await db.fetchrow("SELECT jellyfin_id, updated_at FROM title WHERE id = 1")
    assert row["jellyfin_id"] == "jf-rebuilt"
    assert row["updated_at"] > before
