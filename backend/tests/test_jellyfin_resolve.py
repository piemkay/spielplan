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
    # Through `upsert_items`, not `upsert_item`: since M4.11 the representative `jellyfin_id` is
    # elected over the whole page-set rather than per item (§7.1), because one item cannot know
    # whether it is the only copy. The fill itself is still per item; only the pointer moved.
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
    replacement is counted rather than swallowed.

    Asserted through `upsert_items` since M4.11: "the old id is gone from the library" is the
    only thing that makes this a re-link rather than a second copy, and one item cannot see the
    library. That is also why the count is trustworthy again (§6.6's sync card)."""
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


# --- the fallback refuses ambiguity (M4.11, §7.1, finding cs-04) ---------------------------


async def test_a_name_matching_two_titles_is_refused_not_guessed(db):
    """The fourth key is the one §7.1 does not name, and the module docstring already called it
    "the only one that can be wrong". On the real corpus 2,438 titles share
    `(kind, lower(name))` and 573 groups still collide once the year is added, so
    `ORDER BY t.id LIMIT 1` was not a tie-break: it attached the item to whichever row was
    imported first and then wrote `is_owned`, `owned_checked_at` and the deep link onto it.
    An arbitrary match is worse than no match, because no match is *reported* --
    `report.unmatched` is what §7.2's acquisition half consumes at M5, and a wrong match is
    invisible for ever."""
    await _title(db, 20, "series", "The Bureau", 2015)
    await _title(db, 21, "series", "The Bureau", 2015)

    found = await resolve.resolve_title_id(
        db, item(Type="Series", Name="The Bureau", ProductionYear=2015)
    )
    assert found is None

    report = await resolve.upsert_items(
        db, [item(Type="Series", Name="The Bureau", ProductionYear=2015)]
    )
    assert report.unmatched == ["The Bureau"]
    assert report.matched == 0


async def test_an_item_with_no_production_year_never_matches_on_name_alone(db):
    """`($3::int IS NULL OR t.year = $3)` meant a Jellyfin item whose scraper found no year
    matched on name alone -- the widest possible version of the key the docstring distrusts
    most. Refusing it costs nothing a report does not recover: §7.1's three provider keys are
    unaffected, and an item with neither a provider id nor a year is not identified at all."""
    await _title(db, 22, "movie", "Tampopo", 1985)

    assert await resolve.resolve_title_id(db, item(Name="Tampopo", ProductionYear=None)) is None
    report = await resolve.upsert_items(db, [item(Name="Tampopo", ProductionYear=None)])
    assert report.unmatched == ["Tampopo"]
    assert await db.fetchval("SELECT is_owned FROM title WHERE id = 22") is False


# --- one title, all its copies (M4.11, §7.1, findings 5 and 6) -----------------------------


def _copies() -> list[dict]:
    """One film, two library entries -- the "Movies" and "Movies 4K" household §7.3's conflict
    rule is written for. Both carry the same ProviderIds because they are the same film."""
    return [
        item(Id="jf-1b", ProviderIds={"Imdb": "tt0113277"}),
        item(Id="jf-1", ProviderIds={"Imdb": "tt0113277"}),
    ]


async def test_every_copy_of_a_title_is_recorded_not_just_the_representative(db):
    """§7.1 keeps one `jellyfin_id` per title, and that column cannot answer "which items are
    this film". Without the answer, "not seen" cleared Played on one copy, the other kept the
    flag, and the next sweep's OR-collapse adopted it back -- §7.3's "app is authoritative for
    explicit user actions" failing on the library `_collapse`'s own docstring describes."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277")
    report = await resolve.upsert_items(db, _copies())

    rows = await db.fetch("SELECT jellyfin_id, title_id FROM title_jellyfin_item ORDER BY 1")
    assert [(r["jellyfin_id"], r["title_id"]) for r in rows] == [("jf-1", 1), ("jf-1b", 1)]
    # The shape the sweep codes against: every copy, the title set ownership may trust, and the
    # `kind` decision 210's series guards need without a second query per row.
    assert report.items == {"jf-1b": 1, "jf-1": 1}
    assert report.matched_title_ids == {1}
    assert report.kinds == {1: "movie"}
    assert report.as_dict()["matched_titles"] == 1


async def test_two_copies_of_one_title_are_one_decision_not_two_relinks(db):
    """Measured: every second live copy was counted as `relinked` and the churn guard was true
    for every copy but the current pointer, so the "~11,000 dead row versions" arithmetic the
    guard exists for was silently re-opened for exactly the duplicated titles. The pointer is
    decided once per sweep now, so an unchanged library writes nothing at all."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277")

    first = await resolve.upsert_items(db, _copies())
    before = await db.fetchval("SELECT updated_at FROM title WHERE id = 1")
    second = await resolve.upsert_items(db, _copies())
    after = await db.fetchval("SELECT updated_at FROM title WHERE id = 1")

    assert (first.relinked, second.relinked) == (0, 0)
    assert after == before, "a title whose copies did not change must not be rewritten"
    assert await db.fetchval("SELECT count(*) FROM title_jellyfin_item") == 2


async def test_the_representative_is_kept_while_its_copy_is_still_in_the_library(db):
    """The bug this replaces: `title.jellyfin_id` was written to the copy *this user* played,
    once per user sweep, in user-id order -- measured `jf-1, jf-1b, jf-1, jf-1b, jf-1, jf-1b`
    across three sweeps with two members on different copies. `playback.observe` resolves a
    session against that one column, so the first-listed member's finish prompt was lost
    permanently. The rule is deterministic and never reads a Played flag."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277", jellyfin_id="jf-1b")
    played_the_other = [dict(copy, UserData={"Played": copy["Id"] == "jf-1"})
                        for copy in _copies()]

    report = await resolve.upsert_items(db, played_the_other)

    assert await db.fetchval("SELECT jellyfin_id FROM title WHERE id = 1") == "jf-1b"
    assert report.relinked == 0


async def test_the_representative_falls_to_the_lowest_live_copy_when_its_own_is_gone(db):
    """A library rebuild changes item ids, and that is the only case §7.1 calls a re-link.
    Counting it only when the old id is absent from the page-set is what makes `relinked` on
    §6.6's sync card mean "the library was rebuilt" again rather than "this title has two
    copies"."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277", jellyfin_id="jf-gone")

    report = await resolve.upsert_items(db, _copies())

    assert await db.fetchval("SELECT jellyfin_id FROM title WHERE id = 1") == "jf-1"
    assert report.relinked == 1


async def test_a_copy_that_left_the_library_is_pruned_only_against_a_read_that_happened(db):
    """The prune exists because "unseen" must clear Played on every copy, and a dead item id in
    the map makes that write 404 for ever. It is separated from the pass itself on purpose: a
    Jellyfin outage or the page cap of step 1b yields an empty or truncated page-set, and a
    prune that trusted one would delete the map the next real sweep needs -- the same shape as
    the ownership falsification §7.2 gates on a completed read."""
    await _title(db, 1, "movie", "Heat", 1995, imdb_id="tt0113277")
    await resolve.upsert_items(db, _copies())

    nothing_read = resolve.ResolveReport()
    assert await resolve.prune_missing_items(db, nothing_read) == 0
    assert await db.fetchval("SELECT count(*) FROM title_jellyfin_item") == 2

    one_copy_left = await resolve.upsert_items(db, [item(Id="jf-1", ProviderIds={"Imdb": "tt0113277"})])
    assert await resolve.prune_missing_items(db, one_copy_left) == 1
    assert await db.fetchval("SELECT jellyfin_id FROM title_jellyfin_item") == "jf-1"
