"""Identity: which url is a title's, what an adapter may write back, and whether the page fits.

Spec v2.1 §4.1 (the title spine, and rule 6 on the provider-id columns), §8 stage 2
(`spec:365-368`); decisions 372, 374.

PORT VERDICT: **ported with named changes** from `mdc/ids.py` - `slugify` (`:77-81`), `valid_imdb`
(`:84-88`), `ID_FIELDS` (`:19-22`) and `set_ids` (`:178-189`) - plus the two per-title reads the
corpus spells inline in its two scraped adapters (`mdc/sources/rottentomatoes.py:58-61`,
`mdc/sources/metacritic.py:46-51`).

`loose_name` AND ITS `_FOLD` TABLE ARE NOT HERE, and were until M5.3's first review cycle. They
were a verbatim second copy of `derive/ids.py`'s pair, kept to feed a second copy of
`known_people`; `known_people` now calls the derive's and the pair had no other caller. What that
copy cost is written out at `known_people`. [M5.3 review cycle 1, m53-c1-slug-03]

WHY THERE IS A MODULE HERE AT ALL, since `sources/base.py` deliberately did not take `title_row`.
Five of the eight adapters ask "is this a usable IMDb id", two ask "what slug would this title
have", and six write identity back - so the alternative to one module is the same four functions
copied into six files, and decision 372's rule is a rule about a WRITE. A rule enforced in six
places is a rule enforced in five after the next adapter lands. `_htmlutil` is the sibling shape
one concern over and the underscore is the same one: `base._is_adapter` skips a leading
underscore, so nothing here is imported as a source kind.

THE WRITE IS THE POINT OF THE MODULE. `set_ids` is the only statement any adapter issues against
`title`, `ID_COLUMNS` is the whole list of columns it will accept, and it raises rather than
ignoring anything else - so decision 372's "an adapter writes bytes and identity, and nothing
else" is a refusal in code and not a convention in a docstring. `test_sources_adapters.py` reads
all eight modules with `ast` and fails on a write that goes round it.

WHAT IS DELIBERATELY NOT TAKEN. The corpus's `set_ids` swallows a `sqlite3.IntegrityError`
(`:188-189`), which is its answer to the UNIQUE constraints its schema carries on these columns.
This schema carries none - §4.1 rule 6, "do not add UNIQUE constraints on tmdb_id/trakt_id/slugs
(315/171/... duplicate values exist, mostly legitimate movie/series pairs)"
(`0003_content.sql:24-25`), restated by `0026_acquisition_sources.sql` for the two columns it
adds - so there is no integrity error to swallow and a bare `except` here would hide a real one.
`upsert_person`, `clean_name`, `classify_role` and `keep_credit` are NOT here either: they turn a
payload into derived rows, which is §8 stage 3's, and the plan puts them in `derive/ids.py`.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import asyncpg

# Every column on `title` that decision 372 lets a stage-2 adapter write, and no other. The eight
# the decision enumerates, plus two it does not and that are argued rather than assumed:
#
#   * `letterboxd_slug`, because decision 374 says so in as many words - Letterboxd is not one of
#     §8 stage 2's eight sources and is not crawled, and `wikidata:resolve` yields the slug
#     anyway (P6127), so "the column is filled, the source is not crawled".
#   * `trakt_id`, which arrives in the same `ids` blob as `trakt_slug` on the one Trakt request
#     that is made either way (`mdc/sources/trakt.py:56-58` writes exactly this), sits in the
#     same identity block of `0003_content.sql` (`:34-42`) and is the corpus's own `ID_FIELDS`
#     (`mdc/ids.py:19-22`). Decision 372's list is about identity against CARD FIELDS - the
#     sentence it ends on is "every field a card renders ... is written by stage 3" - and a
#     provider's own numeric id for this film is not one.
#
# `jellyfin_id` is absent on purpose although it is an identity column: §7.1's resolver owns it
# (`connectors/resolve.py:306`), it is not a fact any third-party source knows, and a source
# adapter that could write it could point a household's library row at another film.
ID_COLUMNS = frozenset({
    "imdb_id", "tmdb_id", "tvdb_id", "trakt_id", "trakt_slug", "letterboxd_slug",
    "rt_slug", "metacritic_slug", "wikidata_id", "wikipedia_title",
})

def slugify(s: str) -> str:
    """`mdc/ids.py:77-81`, verbatim. The shape both scraped sources build a candidate path from."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s or "untitled"


def valid_imdb(imdb_id: Any) -> str | None:
    """`mdc/ids.py:84-88`, verbatim. Five of the eight sources are keyed on this and nothing else.

    Deliberately NOT `stages._IMDB_ID` (`acquire/stages.py:285`, `tt\\d{7,}`). That pattern
    judges what Jellyfin handed the app for a MINT, where a short id is a malformed provider id
    (`MALFORMED_PROVIDER_ID`); this one judges what is already on the row for a FETCH, and IMDb
    ids of five and six digits are real - `tt0001`-era shorts are in the corpus. A stage-2 source
    that refused them would silently skip the oldest films in the library.
    """
    if not imdb_id:
        return None
    s = str(imdb_id).strip()
    return s if re.fullmatch(r"tt\d{5,10}", s) else None


async def title_row(conn: asyncpg.Connection, title_id: int) -> asyncpg.Record | None:
    """The row every adapter reads before it decides which url to ask for.

    `sources/base.py` records why the corpus's `title_row` (`mdc/sources/base.py:110-114`) was not
    ported there - it is `sqlite3`, and this app reads `title` through asyncpg. This is that read,
    once, in the package that needs it. A SELECT list and not `SELECT *`: the columns an adapter
    is allowed to know about are its subject's identity and the three facts that decide which url
    to build (`kind`, `name`, `year`), and a `*` here would hand every adapter the card fields
    decision 372 says stage 3 owns - which is how one of them comes to read `overview` and decide
    it may as well write it.
    """
    return await conn.fetchrow(
        "SELECT id, kind, name, original_name, year, imdb_id, tmdb_id, tvdb_id, trakt_id,"
        "       trakt_slug, letterboxd_slug, rt_slug, metacritic_slug, wikidata_id,"
        "       wikipedia_title"
        "  FROM title WHERE id = $1",
        title_id,
    )


async def set_ids(conn: asyncpg.Connection, title_id: int, **ids: Any) -> list[str]:
    """Fill identity columns that are still empty. Returns the column names it offered.

    FILL-NEVER-CLOBBER, SPELLED AS `COALESCE(existing, new)` because decision 372 spells it that
    way and because the one statement has no window in it. `connectors/resolve.py:208` takes the
    read-then-write shape for the same rule and can afford to: it needs the pre-state anyway, to
    build the guard that stops an unchanged library rewriting eleven thousand rows a cycle. An
    adapter has no such guard to build and no reason to hold a row between two statements while
    another drain of the same title is running.

    NEVER AN OVERWRITE, AND THE REASON IS NOT TIDINESS. §4.1's rule 6 says these columns carry
    legitimate duplicates, so "TMDB now says 9" is not evidence that the 7 on the row is wrong -
    it is evidence that one of them is a different work. Under decision 162 content seeds once,
    so a clobber here is unrecoverable by any gesture the household has. The corpus makes the
    same choice for the same reason at `mdc/sources/tmdb.py:143-152`, where only the fields the
    row does not already carry are passed to `set_ids` at all.

    NEVER A `kind` FLIP. `mdc/sources/tmdb.py:81-84` flips `title.kind` when IMDb and TMDB
    disagree; `kind` is not in `ID_COLUMNS` and this function refuses it. §4.1 rule 5 makes
    `kind` the partition every ranking surface reads ("the unpartitioned crowd top-10 is 8/10 TV
    series"), and decision 162 makes the write permanent - so a provider disagreeing about a
    boxed set is not licence to move a title between two ranking universes after placement.

    An empty or None value is dropped rather than written: a provider that omits a field has not
    said the field is empty, and `COALESCE(col, NULL)` would be a no-op write costing a dead row
    version per drain.
    """
    clean = {k: v for k, v in ids.items() if v not in (None, "")}
    unknown = sorted(k for k in clean if k not in ID_COLUMNS)
    if unknown:
        # A ValueError and not a filter. A caller passing `overview=` or `kind=` has misread
        # decision 372, and silently dropping the argument would let stage 2 look like it wrote a
        # card field that it did not - which is worse than either outcome it is between.
        raise ValueError(
            f"a stage 2 source may not write {', '.join(unknown)} on title: decision 372 lets an "
            "adapter write the raw bytes and the identity columns and nothing else, and every "
            "card field, title_meta payload, credit, review and display row is stage 3's"
        )
    if not clean:
        return []
    columns = sorted(clean)
    sets = ", ".join(f"{col} = COALESCE({col}, ${i})" for i, col in enumerate(columns, start=2))
    # `updated_at` beside them, which `connectors/resolve.py:214` already does on this table for
    # this kind of write. It is the row's own bookkeeping rather than a fact a card renders, and
    # a row whose identity changed without its timestamp moving is one no later sweep can order.
    await conn.execute(
        f"UPDATE title SET {sets}, updated_at = now() WHERE id = $1",
        title_id, *(clean[col] for col in columns),
    )
    return columns


async def known_people(conn: asyncpg.Connection, title_id: int) -> set[str]:
    """The directors and billed cast this app already holds for a title, as `loose_name` keys.

    `mdc/sources/rottentomatoes.py:58-61` and `mdc/sources/metacritic.py:46-51` are one query
    written twice; this is it once, and ONCE MEANS `derive/ids.known_people` - the same function
    stage 3's `_refuses` asks the same question of, reached by the deferred import
    `belongs_to_title` below already uses and for the same reason.

    THIS FUNCTION USED TO CARRY A SECOND QUERY, justified by a named change that was factually
    wrong: it said "THE CORPUS FILTERS ON `role_class` AND THIS SCHEMA DOES NOT HAVE IT" and that
    `importer/load.py:236` maps "three of the four columns across". `credit.role_class` is added by
    `0015_seed.sql:54` with its own index at `:55`, and `importer/load.py:235-238` maps it
    explicitly under a comment saying that losing it "is losing the credit block". The substitute
    filter - `job = 'Director' OR department IN ('Acting','Actor')` - is not the same set: a
    co-director or a series director is `role_class = 'director'` with a job neither literal
    matches (`derive/ids._JOB_MAP`), so stage 2's set was strictly narrower than stage 3's and a
    Metacritic page naming our co-director and none of our billed cast was refused at stage 2 and
    accepted at stage 3. `derive/ids.py`'s own change note 4 states the rule: "two implementations
    of 'is this the same human' is how a page starts being refused for one source and accepted for
    the other". [M5.3 review cycle 1, m53-c1-slug-03]

    THE `credit` HALF IS EMPTY ON A FIRST ACQUISITION AND THAT USED TO BE THE WHOLE FUNCTION.
    Stage 2 runs before stage 3, so a title stage 1 has just minted carries no credits at all, and
    `page_belongs_to_title` reads an empty set as "nothing on the page contradicts us" and falls
    back to `abs(page_year - year) <= 2`. The paragraph here called that "the ordinary case rather
    than a defect" on the ground that the set is non-empty "on a re-run and on a title the bundle
    already carried". The second half is true - `placement/reconcile._park_thin` parks thin BUNDLE
    titles at stage 2 and those carry the corpus's credits. The first half is not:
    `pipeline._resume_index` answers the BOARD's stage, so a title past stage 2 re-enters it only
    when §8 stage 4's window closes on it (decision 421), thirty days after the guess was refused. So on
    the acquisition that decides the slug, for every title §8's pipeline exists for, the identity
    check was a year comparison alone - and measured against the shipped capture of the real
    Metacritic page for Black Orpheus, a restoration dated 2006 against a 1959 film, that refuses a
    page the corpus accepted. Refusing costs the title both scraped sources until then, and for good
    if the title clears stage 4 on the rest: `metacritic:reviews` resolves the same path, hits the
    same refusal and issues no review request, and decision 335 counts distinct review sources.

    SO THE EVIDENCE IS TAKEN FROM WHERE IT ALREADY IS. `tmdb:detail` runs at priority 20 and the
    two scraped sources at 76 and 77, so by the time either asks, this run's TMDB document - cast
    included, `append_to_response` fetches it in the same request - is in the raw store. Stage 3
    already judges these same bytes with that cast unioned in (`derive/rebuild._refuses`' named
    change 2); reading it here is what stops the two stages disagreeing about one document in the
    direction that costs the review text. Nothing is written and no request is made, so decision
    372 is untouched: this is the same deferred reach into the derive that `belongs_to_title`
    below already makes, for the same reason. [M5.3 review cycle 2, m53-c2-slug-01]
    """
    from spielplan.derive.ids import known_people as derived

    return await derived(conn, title_id) | await _fetched_cast(conn, title_id)


async def _fetched_cast(conn: asyncpg.Connection, title_id: int) -> set[str]:
    """The directors and billed cast of the TMDB detail document this title already holds.

    THREE DEFERRED IMPORTS AND NO FOURTH IMPLEMENTATION. `rebuild.REQUIRED_DOCUMENTS` is decision
    334's required source "as the two kinds it arrives under", `parse.parse_document` is the one
    dispatcher, and `rebuild._cast` is the one reading of "which parsed credits vouch for a page"
    - private by name and shared the way `derive/rebuild.py` shares `importer/load._timestamp`,
    because a second copy here is exactly the defect cycle 1 removed from this module when it
    deleted its second `known_people`. Deferred into the call for `belongs_to_title`'s stated
    reason: `base.load_all()` imports this package to enumerate kinds, and the parse layer must
    not arrive behind that.

    AN `OSError` OUT OF `rawstore.read` IS NOT CAUGHT, which is `derive/rebuild.py`'s rule and its
    reason: decision 361 makes that exception mean "the file under this path is not this row's
    document", and evidence read out of another film's bytes is the one thing worse here than no
    evidence. It surfaces as that adapter's note under decision 334 and the slug is not written.

    An empty set when there is no such document, which is honest and is the pre-M5.3 behaviour: a
    title whose TMDB kind was never asked - no key, no `tmdb_id`, a 404 - is judged on the year,
    and `page_belongs_to_title` is the one deciding what that is worth.
    """
    from spielplan.acquire import rawstore
    from spielplan.derive import parse, rebuild

    row = await conn.fetchrow(_TMDB_DETAIL, str(title_id), sorted(rebuild.REQUIRED_DOCUMENTS))
    if row is None:
        return set()
    content = await rawstore.read(conn, row["id"])
    return rebuild._cast(parse.parse_document("tmdb", row["kind"], content))


# The newest good TMDB detail document filed under one of this title's tasks. THE TASK JOIN IS
# `acquire/board.py`'s and `derive/rebuild._DOCUMENTS`' - a document belongs to a title when it is
# filed under the key of a task that belongs to that title, because `raw_document` carries no
# `title_id` and correctly so: the key predates the title row under decision 322. `d.ok` because a
# 404's failure row carries no bytes, and newest-first because a second drain's answer is the one
# this run's slug should be judged against. [M5.3 review cycle 2, m53-c2-slug-01]
_TMDB_DETAIL = """
SELECT d.id, d.kind
  FROM raw_document d
 WHERE d.ok
   AND d.source || ':' || d.kind = ANY($2::text[])
   AND d.entity_key IN (
           SELECT key FROM acquisition_task WHERE payload ->> 'title_id' = $1::text
       )
 ORDER BY d.fetched_at DESC, d.id DESC
 LIMIT 1
"""


def belongs_to_title(content: bytes, *, year: int | None, people: set[str],
                     mode: str, people_decide: bool = True) -> bool:
    """Is this scraped page really about the title we asked for? The one predicate, borrowed.

    `page_belongs_to_title` is `mdc/parse/titles.py:933` and it is §8 stage 3's, ported under
    `derive/`. It is called from stage 2 because that is the only place it can do its job: a page
    that turns out to be a different film must not get this title's slug, and the slug is what
    every later request for this source is built from.

    THE IMPORT IS DEFERRED INTO THE CALL, where the corpus's is at module scope
    (`mdc/sources/rottentomatoes.py:24`). §8's stages are ordered, and `base.load_all()` imports
    every module in this package merely to enumerate kinds - for the driver, and for §6.6's board
    - so a module-scope import of the parse layer would make "which sources exist" drag stage 3's
    parsers, their regexes and their imports in behind it. Deferred, the cost is paid by the two
    adapters that scrape and by nothing else.

    THERE IS NO SECOND COPY OF THE PREDICATE and that is asserted rather than promised:
    `test_sources_adapters.py` reads this function's body and fails if it stops resolving to
    `spielplan.derive.parse.page_belongs_to_title`. A wrong page writes another film's reviews
    into this title's pack, and §8 stage 7's quote verification would then pass every one of them
    - a quote from the wrong film is a genuine substring of the wrong film's pack.
    """
    from spielplan.derive.parse import page_belongs_to_title

    return page_belongs_to_title(
        content, year=year, people=people, mode=mode, people_decide=people_decide
    )


__all__ = [
    "ID_COLUMNS",
    "belongs_to_title",
    "known_people",
    "set_ids",
    "slugify",
    "title_row",
    "valid_imdb",
]
