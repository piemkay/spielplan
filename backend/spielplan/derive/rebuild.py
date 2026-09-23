"""§8 stage 3's derive: one title, the documents already in the raw store, and the rows they become.

Spec v2.1 §8 stage 3 ("per-title parse of raw docs into title_meta/credit/review/..., keyed
`entity_key`", `spec:359`), §8's preamble ("All fetched bytes land in the app's own raw store, so
re-parsing is free forever", `spec:398`), §4.1's content spine and §14 risk 5; decisions 162, 334,
345, 361, 372, 375.

PORT VERDICT, unit by unit, against `mdc/parse/rebuild.py` (990 lines).

  * `_dispatch` (`:322-542`) -- **ported, and already here.** It is the one reusable core of that
    file, and the parsers phase landed it as `derive/parse.parse_document` plus
    `derive/reviews.parse_document`, keyed on `(source, kind head)` exactly as the corpus's chain
    of `if source == ... and kind == ...` is. What remains of the dispatcher in this module is the
    table below saying which documents carry REVIEWS, because the corpus expresses that by calling
    `add_reviews` in six of its branches and in none of the others -- a fact about the dispatch
    that had nowhere else to live.
  * `_verify_scraped_pages` (`:544-602`) -- **ported with two named changes**, see `_refuses`.
  * `_clear_derived` (`:96-101`) -- **deliberately NOT ported**, and it is the line §8's preamble
    warns about. It is `DELETE FROM <every derived table>` with no predicate at all, which is
    right for a wholesale rebuild of nineteen thousand titles from a store holding all of them,
    and catastrophic for a derive whose unit is one title. Decision 375 is the replacement and
    `_replace` below is where it lives.
  * `Emitter`, `INSERTS` and `REVIEW_INSERT` (`:25-92`) -- **replaced.** The corpus buffers rows
    across titles and flushes at 2,000 because it is walking a corpus; a per-title derive has tens
    of rows and one transaction, so the buffer is a list and the INSERT is `executemany`. What the
    corpus's hand-written INSERT list also carried is the column mapping, and this module does not
    copy that either -- see THE COLUMN MAP below.
  * the `corrections.apply` call at the tail of `rebuild` (`:225-230`) -- **ported into
    `derive/ledgers.py`**, and its comment is the reason this module's last line is where it is:
    "Last, because the rebuild it follows has just deleted everything it fixes." The corpus has no
    matching call for the DNA ledger because that one runs in `mdc dna ingest` rather than here; §8
    stage 3 owns both, so `derive_title` makes two calls at two points and `ledgers.py` argues why
    merging them is the shape §14.5 is a scar from.
  * `_title_id_maps`, `_wikidata_labels`, `_blog_index`, `_drop_impossible_reviews`,
    `_dedupe_reviews` and `BOILERPLATE_TITLES` -- **not ported.** Every one is a whole-corpus index
    or a cross-title statistic: "any review text that turns up under more than one title is dropped
    from all of them" (`:546-555`) is not a question one title can be asked, and the four blog
    handlers behind `_blog_index` are not among §8 stage 2's eight sources.

THE COLUMN MAP IS `importer/load.MAPPINGS` AND NOT A SECOND COPY OF IT. The parsers emit rows under
the CORPUS's own column names -- `derive/parse.py`'s named change 2 says so and says why -- and
`MAPPINGS` is already the app's one statement of which corpus column lands in which Postgres
column, with rule 6's empty-string coalesces and the two type transforms (`_award_won`,
`_primary_role`) attached. So this module looks the mapping up by target rather than restating it.
That is the argument D3 made one file over for `resolve_title_fields`: a derive spelling its own
`{"body": "award", "won": "result"}` would agree with the importer on the day it was written and
part company on the day either moved, and §4.1's "tables mirror the corpus export" is a claim about
both paths at once. `display.platform_rating.platform <- source` and `title_alias.kind <- source`
are not oversights in that map; they are the two places this schema keeps a row's provenance under
another name, and `SCOPE_COLUMN` follows the map rather than the word "source".

DECISION 375'S REPLACE-BY-SCOPE, IN THE SENTENCE THE REST OF THIS MODULE IMPLEMENTS. For each
derived table the delete is `WHERE title_id = $1 AND <provenance column> = ANY($2)`, where the scope
is the sources THIS RUN re-derived: the row-source label of every document it read, union the
`source` on every row it produced for that table. Never `WHERE title_id = $1` alone -- a derive that
ran while TMDB was down would erase the credits a previous successful derive wrote, and under
decision 162 content seeds once, so nothing takes that back. The labels are in the scope as well as
the row sources because a parse that produced NOTHING still has to clear last run's rows for the
source it was about, and `ParsedTitle.row_sources` is empty precisely then
(`derive/parse.py:1389-1394` makes the label knowable without a successful parse for this reason).

WHAT THAT SCOPE CANNOT REACH, stated rather than left to be found. A source that stops reporting a
fact it used to report keeps its last row: OMDb files IMDb's, Rotten Tomatoes' and Metacritic's
scores under their own names (`derive/parse.py:570-579`), so an OMDb response that has dropped its
Metacritic entry leaves the previous one standing, because 'metacritic' is no longer in the scope
that would delete it. The corpus does not meet this case because it drops every derived table
first. The bytes are still in the store, so the repair is a re-derive after the row is removed and
never a re-crawl -- and `award` below is the same shape for a different reason.

DECISION 420 IS THE SECOND AXIS, AND IT IS THE ONE `source` CANNOT BE. The scope above says which
CRAWL produced a row. It says nothing about who WROTE it, and in two tables the bundle importer
writes under the same names a crawl does: `importer/reviews.py` copies the corpus's own `source`
column verbatim, so a bundle review body is already filed under 'trakt' or 'metacritic', and
`award` was worse, because `importer/load.MAPPINGS` drops the corpus's `award.source` and the
delete had no predicate but `title_id`. So a corpus title -- one of the thin ones §12's M2 row
parks "as acquisition jobs for M5 enrichment" -- that reached this stage lost the bundle's review
bodies under every label the crawl touched, and the bundle's structured awards the moment any OMDb
or Wikidata document was read, and got one crawl's first page back instead. None of it is
recoverable: the bundle ships no `data/raw/` for those rows, OMDb's free-text `Awards` blurb is not
IMDb's per-category rows, and under decision 162 content seeds once. `0026` gives both tables an
`origin` column and both deletes below name `origin = 'derived'`, so this module can take back only
rows it can rebuild - and, because the corpus crawled the same sources, it does not write a row the
bundle already holds a second time beside it (`_replace`, `_reviews`). THE CREDIT HALF IS
DELIBERATELY NOT TREATED THIS WAY and `ops/m53_exit_criterion.py` argues it at the line: a
re-fetched TMDB cast is the same cast under the same label, so a title whose credits this stage
re-derives has lost nothing it cannot ask for again. [M5.3 review cycle 2, m53-c2-d375-01]

`award` IS STILL THE ONE DERIVED TABLE THIS SCHEMA GIVES NO SOURCE COLUMN, which is a different
sentence from the one above and a different column. The corpus ships `award.source` and
`importer/load.MAPPINGS` maps four of its columns and drops that one, so there is nothing to scope
the CRAWL axis on. The delete is therefore by `title_id` and the author axis, guarded the only way
it can be: it runs when and only when this run read a document from a source that emits awards,
which is OMDb (`derive/parse.py:583-585`) and a Wikidata entity (`:1075`). A run that read neither
leaves the table alone, and a run that read OMDb still leaves every award the bundle wrote. That is
decision 375 degraded exactly as far as the schema forces and no further: an OMDb response cannot
lose a Wikidata-sourced award row it never wrote, because nothing in the data says which of the two
wrote it -- and the column that would fix THAT is one migration and one `MAPPINGS` line away.

A HOLE IN THE RAW STORE IS NOT A FAILURE, AND A DAMAGED FILE IS. A title whose `tmdb:detail`
document never arrived derives whatever else it holds and says so in
`DeriveReport.required_missing`; decision 334 makes what follows stage 2's question and not this
one's. `rawstore.read` raising `OSError` is the opposite case and is deliberately NOT caught:
decision 361 makes that exception mean "the file under this path is not this row's document", and
under decision 162 what a derive writes from somebody else's bytes cannot be taken back.

THIS MODULE READS THE STORE AND NEVER FETCHES, which is the whole of §8's promise and is asserted
rather than promised: `test_derive_parse.py::test_no_module_in_the_derive_package_can_reach_the_network`
reads every file under `spielplan/derive/` with `ast` and fails on an import of `httpx`, `requests`,
`urllib.request`, `socket`, `spielplan.acquire.fetch` or `spielplan.connectors`. `acquire.rawstore`
is not on that list and cannot be: it imports no HTTP client either, and its own `read` argues that
a read which could fall back to a fetch would make "re-parsing is free forever" a promise about the
common case only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import asyncpg

from spielplan.acquire import rawstore
from spielplan.derive import ids, ledgers, parse, reviews
from spielplan.importer import meta
from spielplan.importer.load import MAPPINGS, TableMap, _timestamp
from spielplan.sources._ids import set_ids

# `_timestamp` is imported rather than re-derived for `importer/reviews.py:119`'s reason, which
# does exactly this from the same module: a review's `created_date` is a string on both paths and
# `published_at` is `timestamptz`, and a second best-effort ISO parser is a second answer to the
# question of what an unparseable date becomes. Private by name and shared by convention between
# one package's two readers of the same corpus shapes.

# `(store source, kind head)` -> the `source` this document's REVIEWS are filed under.
#
# `derive/parse.parsed_sources()` answers the same question for title rows and this table cannot be
# derived from it: `metacritic:page` produces title rows and no reviews while `metacritic:reviews`
# produces reviews and no title rows, and `trakt:summary` against `trakt:comments` splits the same
# way. The corpus states it by calling `add_reviews` in six branches of `_dispatch` and not in the
# others; six entries here, the same six.
#
# `tmdb:reviews` is present although no adapter in this app fetches it. `derive/parse.py`'s own
# paragraph on `parse_letterboxd_page` makes the argument: the raw store is append-only and a
# re-parse path that cannot read a kind the install may hold is a hole in §8 stage 3 rather than a
# saving. It costs one line and no crawl.
REVIEW_DOCUMENTS: Mapping[tuple[str, str], str] = {
    ("tmdb", "movie_detail"): "tmdb",
    ("tmdb", "tv_detail"): "tmdb",
    ("tmdb", "reviews"): "tmdb",
    ("trakt", "comments"): "trakt",
    ("metacritic", "reviews"): "metacritic",
    ("wikipedia", "article"): "wikipedia",
}

# The two sources reached by a slug GUESSED from the title, and how each page is judged.
# `mdc/parse/rebuild.py:588-591`'s `SCRAPED_PAGES`, minus letterboxd (decision 374) and carrying
# its `people_decide=review_source is not None` already evaluated: a Metacritic page brings review
# text, so a disjoint cast is enough to refuse it, while a Rotten Tomatoes page brings two
# percentages and an English-dub cast list, so either signal may vouch for it.
SCRAPED_PAGES: Mapping[str, tuple[str, bool]] = {
    "metacritic": ("metacritic", True),
    "rottentomatoes": ("rt", False),
}

# Which document of a scraped source carries the identity evidence. The corpus verifies
# `kind='page:main'` and nothing else (`:574-575`), because a reviews page is a list of prose and
# carries neither the cast block nor the release year that decide the question.
SCRAPED_EVIDENCE = "page"

# The sources that emit an `award` row, and the whole of what `award`'s per-title delete is guarded
# by. Spelled out because `award` carries no source column to read it off instead, so nothing
# in the data can answer the question -- and a source added to `derive/parse.py` without being
# added here would have its awards silently dropped, which is why
# `test_derive_rebuild.py::test_no_source_outside_the_award_guard_emits_an_award` reads the answer
# back off the captured responses rather than trusting this line.
#
# `wikidata` is unreachable through `parse.parse_document` today: decision 374 ports only
# `wikidata:resolve`, and `_JSON_PARSERS` routes no Wikidata kind at all. It is named for
# `tmdb:reviews`'s reason one table up -- `parse_wikidata_entity` emits awards, the raw store is
# append-only, and a guard that had to be widened on the day a kind became routable is a guard
# that silently drops rows until someone notices.
AWARD_SOURCES = frozenset({"omdb", "wikidata"})

# Decision 334's one required source, as the two kinds it arrives under. Named so `DeriveReport`
# can answer "did the document the pack needs ever arrive" without this module deciding what
# follows from the answer -- that is stage 2's park and stage 4's gate, never a derive's.
REQUIRED_DOCUMENTS = frozenset({"tmdb:movie_detail", "tmdb:tv_detail"})

# The one value this module writes into the `origin` column `0026` adds to `award` and
# `review_store.review`, and the only value its two deletes there will match. Decision 420: the
# other author of those two tables is the bundle importer, whose rows no crawl can reproduce.
DERIVED_ORIGIN = "derived"

# target table -> the column carrying a row's provenance, which is what the delete scopes on.
# Two of these are not spelled `source`, and that is `importer/load.py`'s mapping rather than a
# choice made here: `title_alias.kind` takes the corpus's `source` (`:141-149`) and
# `display.platform_rating.platform` takes it too (`:286-288`), because a score is filed under
# whose score it is. `award` is None -- it has an `origin` and no `source`, which are the two axes
# the module docstring separates -- and the docstring says what follows from that.
SCOPE_COLUMN: Mapping[str, str | None] = {
    "title_alias": "kind",
    "title_genre": "source",
    "title_keyword": "source",
    "title_language": "source",
    "title_country": "source",
    "title_company": "source",
    "title_video": "source",
    "credit": "source",
    "award": None,
    "display.platform_rating": "platform",
}

# `derive/parse.TABLES` names the table it emits for; one of them lives in another schema.
TARGET_OF: Mapping[str, str] = {"platform_rating": "display.platform_rating"}

# The seven multi-row tables whose key is natural, so two sources agreeing collide on it. TMDB
# names a film's primary language once in `spoken_languages` and again in `original_language`, and
# its country twice the same way, so `parse_tmdb_detail` emits the duplicate row and the corpus
# writes all seven through `INSERT OR IGNORE` (`mdc/parse/rebuild.py:40-53`). Without this clause
# the first acquisition to reach one dies on a unique violation; deduplicating in the parser
# instead would be the wrong end, because §4.1 keeps two sources agreeing as information and the
# key is where the app already says so.
IGNORE_DUPLICATES = frozenset({"title_alias", "title_genre", "title_keyword", "title_language",
                               "title_country", "title_company", "title_video"})

# `display.platform_rating` is the eighth and takes the other half of the corpus's pair,
# `INSERT OR REPLACE` (`:53-54`). Its key is `(title_id, platform, metric)` and two documents of
# one run land on it: OMDb relays Metacritic's Metascore under `platform = 'metacritic'` while the
# Metacritic page carries its own. Last write wins, which is the corpus's answer, and this module
# reads documents in a deterministic order, so which one that is does not move between runs.
_RATING_CONFLICT = (
    " ON CONFLICT (title_id, platform, metric) DO UPDATE SET"
    " score = EXCLUDED.score, scale = EXCLUDED.scale, votes = EXCLUDED.votes"
)

# `title_meta`'s key is `(title_id, source)` and decision 375 keeps it that way: the table is §4.1's
# "one block = one droppable source", and a derive that collapsed it would destroy the thing
# `SOURCE_PRIORITY` exists to order. 0015 records what a coarser key cost when three sibling tables
# were keyed without `source` -- 17,342 / 19,092 / 32,463 duplicate groups (`0015_seed.sql:190-210`)
# and a COPY that rolled the whole seed back. One row per source per run, so the conflict clause is
# a net under a document pair (a `movie_detail` and a `tv_detail` under one title, after a kind
# flip upstream) rather than the ordinary path.
_META_CONFLICT = (
    " ON CONFLICT (title_id, source) DO UPDATE SET"
    " payload = EXCLUDED.payload, fetched_at = EXCLUDED.fetched_at"
)

# The newest successful document per `(source, kind, page)` among the keys this title's tasks were
# filed under. `rawstore.latest`'s rule -- `ok` only, newest by `fetched_at` then `id` -- asked once
# for every pair the title actually holds rather than once per pair some list says it should.
#
# `page` IS IN THE KEY AND `rawstore.latest` HAS NO ROOM FOR IT. `sources/trakt.py:160-162` writes
# "Unique per (sort, page) or the raw store's newest-per-page view collapses two sorts into one
# document", so a `latest` that ignored `page` would return one of Trakt's three comment sorts and
# silently drop the other two -- two thirds of the rating-stratified review text stage 4 then counts.
#
# THE TASK JOIN IS `acquire/board.py:92-101`'s, NOT A SECOND SPELLING OF IT. That module states the
# seam: "a document belongs to a title when it is filed under the key of a task that belongs to
# that title", because `raw_document` carries no `title_id` and correctly so -- the key predates the
# title row under decision 322. A derive that reconstructed `jellyfin:<id>` from `title.jellyfin_id`
# would read a different set of documents than the board shows an operator chasing this same title.
#
# `d.url` IS SELECTED BECAUSE IT IS THE ONLY THING TYING A SCRAPED REVIEW TO THE PAGE IT CAME FROM.
# This query resolves each `(source, kind, page)` to its newest ok row INDEPENDENTLY, so
# `metacritic:page:main` and `metacritic:reviews:critics` are chosen without reference to each
# other -- and the refusal below judges the `page:main` alone. A run in which those two documents
# were fetched under different slugs therefore used to write another film's critic and user reviews
# into this title's pack with `refused` empty, which is the exact state §8 stage 7's quote
# verification cannot see: the quote IS a genuine substring of the wrong film's pack. `url` carries
# the resolved path already (`sources/_views.capture` stores `response.request_url`, and the review
# views hang off the page path), so the tie needs no new column and no second fetch.
# [M5.3 review cycle 1, m53-c1-slug-02]
_DOCUMENTS = """
SELECT DISTINCT ON (d.source, d.kind, d.page)
       d.id, d.source, d.kind, d.page, d.fetched_at, d.url
  FROM raw_document d
 WHERE d.ok
   AND d.entity_key IN (
           SELECT key FROM acquisition_task WHERE payload ->> 'title_id' = $1::text
       )
 ORDER BY d.source, d.kind, d.page, d.fetched_at DESC, d.id DESC
"""

_REVIEW_COLUMNS = ("title_id", "source", "author", "url", "rating", "published_at",
                   "is_critic", "body")

# The card fields §6.0 renders are `resolve_title_fields`'s, per field, across every source this
# title carries. These three are not on that list and live on `title` rather than in `title_meta`,
# so the derive resolves them by the same `best()` and writes them here.
#
# COALESCE AND NOT AN OVERWRITE, which is the one place this module's rule differs from
# `resolve_title_fields`'s. That function may blank a tagline, because a title with a meta row has
# some source's answer for it; its own docstring gives the reason a blank is refused elsewhere --
# "blanking one would also overwrite whatever §8's acquisition path wrote for a title the corpus
# never shipped" (`importer/meta.py:157-159`). `year` and `runtime_min` ARE what that path wrote:
# `acquire/stages._mint` takes both off the Jellyfin item the household owns, so a source that
# merely omits the field must not erase the library's own answer. `original_language` is the same
# fact one column over, and `importer/load.py:117-121` records what its absence costs -- "Without
# the column here the seed leaves it NULL and the block never fires on a real bundle", which is
# §4.2's per-title `lang:` feature not firing for any acquired title.
_TITLE_FIELDS = ("year", "runtime_min", "original_language")


@dataclass(frozen=True)
class _Document:
    """One raw document, read once and handed to both parsers."""

    source: str
    kind: str
    fetched_at: datetime | None
    content: bytes
    title_label: str | None
    review_label: str | None
    # The url this document was fetched under, which for the two scraped sources is the path the
    # identity check ruled on. Carried on the document rather than looked up again because
    # `_DOCUMENTS` has already read it and a second query would be a second answer.
    url: str = ""

    @property
    def head(self) -> str:
        """The part of the kind before the colon, which is what chooses a parser.

        The store records `page:main` and `reviews:critics`, and `derive/parse.parse_document`
        splits the same way; this module splits it too, to look one document up in the two tables
        above before deciding whether to read its bytes at all.
        """
        return self.kind.split(":", 1)[0]


@dataclass(frozen=True)
class DeriveReport:
    """What one derive read, refused and wrote. Returned rather than logged.

    §6.6's board shows `acquisition_job.detail`, which is the stage's to write; a derive that
    logged instead would put the only account of what it did somewhere the board cannot reach, and
    decision 345 makes that board the one window onto the raw store. Every field is a count or a
    name, never bytes.
    """

    title_id: int
    documents: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    rows: Mapping[str, int] = field(default_factory=dict)
    people: int = 0
    # TWO COUNTS AND NOT ONE, for the same reason there are two calls: §14.5 names "two distinct
    # ledgers", and a board line reading "3 curated rows applied" cannot tell an operator which
    # ledger applied them or whether the other one ran at all. `derive/ledgers.py`'s two appliers
    # return counts by outcome exactly so this row can carry them apart.
    adjudications: Mapping[str, int] = field(default_factory=dict)
    corrections: Mapping[str, int] = field(default_factory=dict)

    @property
    def required_missing(self) -> bool:
        """No `tmdb:detail` document, which is decision 334's one required source.

        A fact and not a verdict. What follows from it is stage 2's park and stage 4's gate; a
        derive that turned it into an exception would fail a title whose other seven sources
        answered perfectly well, and the bytes of those seven are already on disk.
        """
        return not (REQUIRED_DOCUMENTS & set(self.documents))


def _values(tmap: TableMap, row: Mapping[str, Any], title_id: int) -> tuple:
    """One parsed row as `tmap.pg_columns`-ordered values, through the importer's own coercions.

    `importer/load._rows:394-434` is the other caller of this rule and the order is its order:
    transforms first, because they turn the corpus's representation into this app's, then rule 6's
    empty-string coalesce, then the boolean and timestamp casts. A parsed row that omits a mapped
    column yields NULL for it, which is the honest reading -- a parser that had nothing to say
    about `character` says nothing rather than ''.
    """
    out: list[Any] = []
    for column in tmap.pg_columns:
        value = title_id if column == "title_id" else row.get(tmap.columns[column])
        if column in tmap.transforms:
            value = tmap.transforms[column](value)
        if value is None and column in tmap.coalesce_empty:
            value = ""
        if column in tmap.bool_columns:
            value = tmap.bool_defaults.get(column) if value is None else bool(value)
        if column in tmap.timestamp_columns:
            value = _timestamp(value)
        out.append(value)
    return tuple(out)


_TARGETS: Mapping[str, TableMap] = {tmap.target: tmap for tmap in MAPPINGS}


async def _replace(
    conn: asyncpg.Connection,
    target: str,
    *,
    title_id: int,
    scope: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    conflict: str = "",
    origin: str = "",
) -> int:
    """Delete this title's rows for `scope`, then insert `rows`. Decision 375, in one place.

    The delete runs even when `rows` is empty, which is the half that makes a re-derive honest: a
    source whose response no longer carries genres must lose the genres it carried last time. It
    does not run for a source outside `scope`, which is the half that makes it safe.

    THE COUNT IS READ BACK RATHER THAN ASSUMED FROM `len(rows)`, and the difference is on the
    ordinary path rather than at an edge. Eight of the ten targets carry a conflict clause because
    two sources of one run collide on a natural key -- the two paragraphs above `IGNORE_DUPLICATES`
    and `_RATING_CONFLICT` say which collisions and why they are information rather than a bug --
    so the list handed to `executemany` is longer than the rows that land. `stages.derive` puts
    this number into `acquisition_job.detail` and `0005_ledger.sql:137` makes that a product
    surface, where "12 platform ratings" for ten rows is a number that never contradicts itself and
    is therefore trusted. One `count(*)` per target, inside the caller's transaction and over the
    predicate the delete already used: after a delete-then-insert those rows ARE this run's, so the
    count is exact without a second statement shape to keep in step with the first.
    [M5.3 review cycle 1, m53-rev1-derive-report-rows-overstates-what-landed]

    `origin` IS PASSED BY THE ONE TARGET WHOSE SCOPE COLUMN IS None, and the two travel together
    rather than independently: `award` is deleted by `title_id` and the author axis decision 420
    adds, so the INSERT has to name that author or the next derive cannot find its own rows again,
    and the count has to read the same predicate or it would report the bundle's awards as this
    run's arrivals. A target with a scope column passes nothing and this is a no-op for it.

    AND A ROW THE OTHER AUTHOR ALREADY HOLDS IS NOT WRITTEN BESIDE IT, which is the half of
    decision 420 that keeping the bundle's rows makes necessary. The corpus read OMDb's blurb too,
    so a corpus title's summary award rows are the ones this parse emits again, value for value;
    with the bundle's rows no longer deleted, inserting them would double them, and `award` has no
    natural key to refuse the second. Compared over the mapped columns, which are the whole row
    the importer's mapping writes. [M5.3 review cycle 2, m53-c2-d375-01]
    """
    tmap = _TARGETS[target]
    column = SCOPE_COLUMN[target]
    if column is not None:
        await conn.execute(
            f"DELETE FROM {target} WHERE title_id = $1 AND {column} = ANY($2::text[])",
            title_id, list(scope),
        )
    if not rows:
        return 0
    columns = [*tmap.pg_columns, "origin"] if origin else tmap.pg_columns
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    # Quoted, as `importer/load._rows:410` quotes the other side of the same mapping. `credit`
    # carries a column called `character`, which Postgres parses as a type name in several
    # positions, and a column list is not a place to find out which ones.
    names = ", ".join(f'"{column}"' for column in columns)
    values = [_values(tmap, row, title_id) for row in rows]
    if origin:
        mapped = ", ".join(f'"{column}"' for column in tmap.pg_columns)
        held = {tuple(row) for row in await conn.fetch(
            f"SELECT {mapped} FROM {target} WHERE title_id = $1 AND origin <> $2",
            title_id, origin,
        )}
        values = [row + (origin,) for row in values if row not in held]
        if not values:
            return 0
    await conn.executemany(
        f"INSERT INTO {target} ({names}) VALUES ({placeholders}){conflict}",
        values,
    )
    if column is None:
        # `award`, whose DELETE `_write` has just run over this run's own rows -- so the author
        # axis is the scope and counting it is the same statement one predicate over.
        return await conn.fetchval(
            f"SELECT count(*) FROM {target} WHERE title_id = $1 AND origin = $2",
            title_id, origin,
        )
    return await conn.fetchval(
        f"SELECT count(*) FROM {target} WHERE title_id = $1 AND {column} = ANY($2::text[])",
        title_id, list(scope),
    )


def _refuses(doc: _Document, *, year: int | None, people: set[str]) -> bool:
    """Is this scraped page about a different film? `mdc/parse/rebuild.py:544-602`, per title.

    IT IS NOT REDUNDANT WITH STAGE 2'S CHECK, which is the first thing a reader will doubt.
    `sources/rottentomatoes.py:135-139` states the rule it works under in its own words: "The bytes
    stay in the store either way - it is append-only, and a page that turned out to be another film
    is still the honest record of what the guess returned. What it does not get is the slug." So
    the store holds refused pages under `ok = true`, and a derive reading every ok document would
    parse another film's scorecard into this title -- the failure §8 stage 7's quote verification
    cannot catch either, because a quote from the wrong film IS a genuine substring of the wrong
    film's pack.

    TWO NAMED CHANGES from the corpus's version.

      1. **It runs before the write rather than after it.** `_verify_scraped_pages` is a sweep at
         the end of a rebuild that DELETEs the ratings and reviews a wrong page produced; here the
         page is judged first and its rows are never written. The same outcome with one fewer state
         the database passes through -- and under decision 162 a row that was written and deleted
         is not the same as a row that never existed, because anything reading in between saw it.
      2. **The cast it compares against is this run's, not the database's.** The corpus can read
         `credit` because its sweep runs after the whole rebuild has written it; a per-title derive
         judging the page before writing would see the credits of the PREVIOUS derive, and on a
         title acquired minutes ago would see none at all. `derive_title` therefore builds the set
         from the credit rows the JSON sources produced in this same pass, unioned with what the
         row already carries -- strictly more evidence than either alone.

    THE SLUG IS NOT CLEARED, where the corpus clears it (`:601-602`), AND NOTHING WILL REVISIT IT.
    §8 stage 2 owns `title`'s identity columns (decision 372), so not writing one here is right.
    What this paragraph used to add -- that a refusal is "a fact for the next stage-2 run to act
    on" -- is false, and saying so is worth the lines because a later reader will otherwise go
    looking for a repair that does not exist. Stage 2 runs again for a title past it only when §8
    stage 4's window closes on it (decision 421), because `pipeline._resume_index` answers the
    BOARD's stage. When it does run, both scraped adapters short-circuit on a slug the row already
    carries -- `metacritic.resolve_path` returns the supplied candidate before it touches a
    connection and `rottentomatoes.page` gates the check on `guessed` -- and `_ids.set_ids` is
    COALESCE, so no adapter can overwrite or clear one. The refusal does reach a person:
    `DeriveReport.refused` lands in `acquisition_job.detail`, which the admin job route
    (`api/acquisition.job_detail`) returns. No code acts on it, and decision 330 gives an operator
    no gesture until M5.6.

    So the steady state is safe and dead, which is the honest sentence and not a comfortable one:
    the page is refused on every derive, `_under` drops the review views with it and nothing wrong
    is written, while the row keeps another film's provider id under §4.1 rule 6 and every stage-2
    run for the title spends polite requests on that film's pages. Clearing it here would also be
    the wrong repair rather than merely a forbidden one -- `page_belongs_to_title` names the cases
    where a disjoint cast is a false negative (a restoration dated to its re-release, an anime page
    listing the English dub), so a stage-3 refusal is not proof the slug is wrong. No milestone owns
    this column's repair today. [M5.3 review cycle 2, m53-c2-slug-02]
    """
    mode, people_decide = SCRAPED_PAGES[doc.source]
    return not parse.page_belongs_to_title(
        doc.content, year=year, people=people, mode=mode, people_decide=people_decide
    )


def _under(url: str, page_url: str | None) -> bool:
    """Was this document fetched from the page url the identity check accepted, or beneath it?

    The two scraped sources put every document of a title under one path: `sources/metacritic.py`
    fetches the page at `<base>/<path>/` and its two review views at `<base>/<path>/critic-reviews/`
    and `<base>/<path>/user-reviews/`, and Rotten Tomatoes has the page alone. So a prefix test on
    the accepted page's own url answers "is this document about the film that page was proved to be
    about" with no new column, no `View.meta` and no second fetch.

    `None` -- no accepted `page:main` for this source under this title -- is FALSE and deliberately
    so. That is the case where the identity check never ran at all, and a rule that read it as
    permission would close half the hole it exists for: `metacritic:reviews` resolves and fetches a
    path of its own when the page kind failed, so "no page" is exactly when the reviews are least
    vouched for. [M5.3 review cycle 1, m53-c1-slug-02]
    """
    if not page_url or not url:
        return False
    base = page_url if page_url.endswith("/") else page_url + "/"
    return url == page_url or url.startswith(base)


def _cast(result: parse.ParsedTitle) -> set[str]:
    """This document's directors and billed cast as loose-match keys.

    `derive/ids.known_people` is the same set read from `credit`, and its `role_class IN
    ('director', 'cast')` is the filter repeated here rather than a free-text job match, for the
    reason it gives: the vocabulary is closed (`0015_seed.sql:50-53`) and "Director" would also
    match "Assistant Director" -- someone who worked on the film and is credited on neither page.
    """
    return {
        key
        for row in result.table("credit") if row.get("role_class") in ("director", "cast")
        if (key := ids.loose_name((row.get("person") or {}).get("name")))
    }


async def derive_title(
    conn: asyncpg.Connection, title_id: int, *, priority: Sequence[str] | None = None
) -> DeriveReport:
    """Re-derive one title from the documents already in the raw store. §8 stage 3.

    ONE TRANSACTION, which decision 375 requires and §14 risk 5 is about. Eleven tables are deleted
    and re-inserted here; a derive committing table by table would leave a title with its old
    credits and its new reviews if the worker died between two of them, and that half-state is
    indistinguishable on §6.6's board from a title that simply has thin credits.

    `priority` is `importer/meta.source_priority`'s answer and defaults to it. The default reads no
    `BUNDLE.json` because there is none left to read -- `api/artifacts.py:152` records that the
    imported bundle "is deleted by its own import" -- so what it returns is `meta.SOURCE_PRIORITY`,
    the app's own order, and NOT whatever order the bundle was assembled under. Both shipped
    manifests omit `source_priority`, so the two agree today; a bundle that shipped one would be
    honoured for the titles the import wrote and not for the titles this function derives, which is
    a fork `meta.source_priority` now warns about at the one moment a person is reading findings.
    [M5.3 review cycle 1, m53-rev1-derive-resolves-by-the-constant-not-the-bundle-order]

    ORDER IS WRITTEN INTO THE ROWS AND NOT LEFT TO A SEQUENCE. `credit` and `review` carry
    `bigserial` keys, so a re-derive gives every row a new id; what must not move is the order a
    card reads them in, and that is `credit.billing_order` taken from the document's own billing
    (`0015_seed.sql:53` renamed the column from `ord`). The documents themselves are read in a
    deterministic order -- `(source, kind, page)` -- so the insert order is a property of the data
    on both runs rather than of when the rows happened to be written.
    """
    title = await conn.fetchrow("SELECT id, year FROM title WHERE id = $1", title_id)
    if title is None:
        # A LookupError and not an empty report, for `derive/gate.measure`'s stated reason: a
        # missing title and a title with nothing derived are different facts, and a derive
        # returning zeros for the first would put "0 rows" on the board for a row that is absent.
        raise LookupError(f"title {title_id} does not exist: there is nothing to derive")

    order = list(priority) if priority is not None else meta.source_priority(None)
    parseable = parse.parsed_sources()

    read: list[_Document] = []
    labels: set[str] = set()
    # `str(title_id)`, as `acquire/board.py:214` passes it: `payload ->> 'title_id'` is text, so
    # the placeholder is a text one and asyncpg refuses the int outright rather than comparing a
    # number to a string and matching nothing.
    for row in await conn.fetch(_DOCUMENTS, str(title_id)):
        head = row["kind"].split(":", 1)[0]
        title_label = parseable.get((row["source"], head))
        review_label = REVIEW_DOCUMENTS.get((row["source"], head))
        if title_label is None and review_label is None:
            # A document no parser claims. `tmdb:find`, `wikipedia:search` and `tvmaze:lookup` are
            # each fetched for an identifier stage 2 has already taken off them, so there is
            # nothing here to derive and no reason to pay for the disk read.
            continue
        read.append(_Document(
            source=row["source"], kind=row["kind"], fetched_at=row["fetched_at"],
            content=await rawstore.read(conn, row["id"]),
            title_label=title_label, review_label=review_label, url=row["url"] or "",
        ))
        labels.update(label for label in (title_label, review_label) if label)

    # Pass one: the sources that are not reached by a guessed slug. They supply the cast the two
    # that are will be judged against, which is `_refuses` change 2.
    people = await ids.known_people(conn, title_id)
    parsed: list[tuple[_Document, parse.ParsedTitle]] = []
    for doc in read:
        if doc.source in SCRAPED_PAGES:
            continue
        result = parse.parse_document(doc.source, doc.kind, doc.content)
        parsed.append((doc, result))
        people |= _cast(result)

    # Pass two: judge each scraped source on its own `page:main`, then take the rows and the
    # reviews of every document whose source survived. A refused source keeps its label in the
    # scope, so the rows a previous derive wrote from the wrong page are deleted and not replaced --
    # which is what the corpus's sweep does with two DELETEs (`:595-600`).
    refused: dict[str, str] = {}
    proved: dict[str, str] = {}
    for doc in read:
        if doc.source in SCRAPED_PAGES and doc.head == SCRAPED_EVIDENCE:
            if _refuses(doc, year=title["year"], people=people):
                refused[doc.source] = f"{doc.source}:{doc.kind}: a different title of the same name"
            else:
                proved[doc.source] = doc.url

    review_rows: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    unproved: list[str] = []
    for doc in read:
        if doc.source in refused:
            continue
        # AND EVERY OTHER DOCUMENT OF A SCRAPED SOURCE HAS TO HANG OFF THE PAGE THAT PASSED.
        # `_refuses` above judges one document and the verdict used to be recorded per SOURCE, so
        # `metacritic:reviews:critics` inherited the verdict of whatever `page:main` happened to be
        # newest -- including one fetched from a different slug on a later drain, and including the
        # case where there is no accepted `page:main` under this title at all. Both are ordinary:
        # `metacritic:reviews` resolves the path itself when the page kind failed, a supplied slug
        # is taken as given at stage 2 by design, and a 404 on a review view stores `ok = false`
        # and so leaves last run's rows the newest ok ones. The url is the tie, `proved` holds the
        # one that passed, and a document outside it is dropped rather than parsed -- exit check
        # 9's "nothing written", applied to the half of the source that carries the review text.
        # It keeps its label in `labels`, so a previous derive's rows from the wrong page go and
        # nothing replaces them. [M5.3 review cycle 1, m53-c1-slug-02]
        if doc.source in SCRAPED_PAGES and not _under(doc.url, proved.get(doc.source)):
            unproved.append(f"{doc.source}:{doc.kind}: not fetched from the page this title proved")
            continue
        if doc.source in SCRAPED_PAGES and doc.title_label is not None:
            parsed.append((doc, parse.parse_document(doc.source, doc.kind, doc.content)))
        if doc.review_label is None:
            continue
        for review in reviews.parse_document(doc.source, doc.kind, doc.content):
            review_rows.setdefault(review.source, []).append(
                (review.fingerprint(), reviews.review_row(review, title_id))
            )

    async with conn.transaction():
        # THE INGEST POINT, which is where §14.5 puts the DNA half: "adjudication- AND
        # corrections-at-derivation - two distinct ledgers: DNA verdicts at INGEST, source-credit
        # facts at REBUILD". Nothing in M5.3 writes a `dna_tag`, so the ingest this call sits at is
        # the head of the derive's own write phase, ruling over the tags the title already carries
        # (decision 376). M5.4's stage 8 writes tags of its own and calls the same applier at ITS
        # ingest; that is why the function takes a connection and a title rather than this run's
        # rows, and why it is not folded into `_write` where only a derive could reach it.
        adjudications = await ledgers.apply_adjudications(conn, title_id)
        written = await _write(conn, title_id, parsed, review_rows, labels, order)
        # LAST, AND THE WORD IS THE REQUIREMENT. `_write` has just regenerated this title's credits
        # from the raw store, including the music credit the ledger exists to overrule; a correction
        # applied before that line is a correction the same transaction then silently reverts, which
        # is §14.5's scar in one function. Two calls at two points and never one merged pass: a pass
        # that ran both here would leave the corrected credit in place and still be wrong, because
        # it would adjudicate tags after the rows they are extracted from have moved under them.
        corrections = await ledgers.apply_corrections(conn, title_id)

    return DeriveReport(
        title_id=title_id,
        documents=tuple(f"{doc.source}:{doc.kind}" for doc in read),
        # Both kinds of refusal on one field, because §6.6's board asks one question of it -- what
        # did this derive decline to believe -- and a second list beside it would be a second
        # answer an operator has to join by hand. Sorted within each kind so the line is stable
        # across two derives of the same documents, which `_snapshot`'s equality depends on.
        refused=tuple(refused[source] for source in sorted(refused)) + tuple(sorted(unproved)),
        sources=tuple(sorted(labels)),
        rows={table: count for table, count in sorted(written.items()) if count},
        people=written.get("person", 0),
        adjudications=adjudications,
        corrections=corrections,
    )


async def _write(
    conn: asyncpg.Connection,
    title_id: int,
    parsed: Sequence[tuple[_Document, parse.ParsedTitle]],
    review_rows: Mapping[str, list[tuple[str, dict[str, Any]]]],
    labels: set[str],
    order: Sequence[str],
) -> dict[str, int]:
    """Everything the derive writes, inside the caller's one transaction.

    Split out so `derive_title` reads as the sequence §8 stage 3 describes -- read, parse, judge,
    write -- rather than as one function with a transaction three screens into it.
    """
    by_table: dict[str, list[Mapping[str, Any]]] = {target: [] for target in SCOPE_COLUMN}
    meta_rows: dict[str, tuple[Mapping[str, Any], Any]] = {}
    for doc, result in parsed:
        for table in parse.TABLES:
            if table == "title_meta":
                for row in result.table(table):
                    meta_rows[str(row.get("source"))] = (row, doc.fetched_at)
                continue
            by_table[TARGET_OF.get(table, table)].extend(result.table(table))

    written: dict[str, int] = {}
    written["person"] = await _people(conn, by_table)

    for target, rows in by_table.items():
        if target == "award":
            continue
        scope = sorted(labels | {str(row["source"]) for row in rows if row.get("source")})
        conflict = " ON CONFLICT DO NOTHING" if target in IGNORE_DUPLICATES else ""
        if target == "display.platform_rating":
            conflict = _RATING_CONFLICT
        written[target] = await _replace(
            conn, target, title_id=title_id, scope=scope, rows=rows, conflict=conflict
        )

    if labels & AWARD_SOURCES:
        # `origin` AND NOT `title_id` ALONE, which is decision 420 and the one place in this
        # module where the delete cannot also name a source. The bundle's awards are IMDb's,
        # per category and per year, and nothing this stage crawls can produce them again -- OMDb
        # answers with a free-text blurb -- so a delete by `title_id` alone destroyed them on the
        # first enrichment of a corpus title and decision 162 left no way back.
        # [M5.3 review cycle 2, m53-c2-d375-01]
        await conn.execute(
            "DELETE FROM award WHERE title_id = $1 AND origin = $2", title_id, DERIVED_ORIGIN,
        )
        written["award"] = await _replace(
            conn, "award", title_id=title_id, scope=(), rows=by_table["award"],
            origin=DERIVED_ORIGIN,
        )

    written["title_meta"] = await _meta(conn, title_id, meta_rows, labels)
    written["review"] = await _reviews(conn, title_id, review_rows, labels)
    await _resolve(conn, title_id, order)
    return written


async def _people(
    conn: asyncpg.Connection, by_table: dict[str, list[Mapping[str, Any]]]
) -> int:
    """Resolve every credit's human to a `person` row, then put the id on the row.

    `derive/parse.py`'s named change 1 is why this is here and not in the parser: a credit carries
    the person AS DATA so that a parser needs no connection, and `upsert_person` is therefore the
    derive's call, made once per distinct human rather than once per credit -- a series' aggregate
    crew lists one director under four jobs, and four lookups would be three too many.

    The id lands on a COPY of the row, because `ParsedTitle.rows` holds mappings a frozen dataclass
    handed out and a derive that mutated them would be writing into the parse result.
    """
    minted: dict[tuple[Any, Any, Any], int] = {}
    resolved: list[Mapping[str, Any]] = []
    for row in by_table["credit"]:
        person = dict(row.get("person") or {})
        person.setdefault("name", "?")
        key = (person.get("name"), person.get("imdb_id"), person.get("tmdb_id"))
        if key not in minted:
            minted[key] = await ids.upsert_person(conn, **person)
        resolved.append({**row, "person_id": minted[key]})
    by_table["credit"] = resolved
    return len(minted)


async def _meta(
    conn: asyncpg.Connection,
    title_id: int,
    rows: Mapping[str, tuple[Mapping[str, Any], Any]],
    labels: set[str],
) -> int:
    """`title_meta`, one row per source, never collapsed. §4.1's "one block = one droppable source".

    The payload is the parsed row minus the two columns that became the key, which is
    `importer/meta._payload_columns:99-106`'s rule and carries its reason: "Keeping `title_id` and
    `source` inside the payload as well would store the key twice and invite a reader to trust the
    copy over the column it is joined by."

    `fetched_at` IS THE DOCUMENT'S AND NOT `now()`. The column exists for one fact -- when this
    source last said this -- and the derive is the only writer that can know it, because the
    importer loads a table the corpus assembled at some other time and leaves it NULL. Taking the
    raw document's own timestamp also keeps the row deterministic across two derives of the same
    bytes, which `now()` would not.
    """
    await conn.execute(
        "DELETE FROM title_meta WHERE title_id = $1 AND source = ANY($2::text[])",
        title_id, sorted(labels),
    )
    values = [
        (title_id, source, {k: v for k, v in row.items() if k not in ("title_id", "source")},
         fetched_at)
        for source, (row, fetched_at) in sorted(rows.items())
    ]
    if values:
        await conn.executemany(
            "INSERT INTO title_meta (title_id, source, payload, fetched_at)"
            f" VALUES ($1, $2, $3, $4){_META_CONFLICT}",
            values,
        )
    return len(values)


async def _reviews(
    conn: asyncpg.Connection,
    title_id: int,
    by_source: Mapping[str, list[tuple[str, dict[str, Any]]]],
    labels: set[str],
) -> int:
    """`review_store.review`, replaced by source. These are the rows stage 4's gate then counts.

    DEDUPED WITHIN A SOURCE BY `ParsedReview.fingerprint`, because two documents of ONE source
    carry the same review: Metacritic's critic and user pages overlap, and `_mc_from_json` already
    dedupes on `body[:120]` within a document but cannot see across two
    (`derive/reviews.py:116-129` says exactly this and exists for it). The table carries
    `bigserial` and no natural key, so a duplicate is not refused by the database -- and decision
    335's `sum(word_count)` would count it twice, which is a title clearing stage 4's gate on one
    review filed under two urls.

    AND REPLACED BY AUTHOR AS WELL AS BY SOURCE, which is decision 420 and the half `source` alone
    could not carry. `importer/reviews.py` files the corpus's review bodies under the corpus's own
    `source` column, so 'trakt' and 'metacritic' name both this crawl's rows and the bundle's; a
    delete scoped on the label alone therefore took a corpus title's review bodies -- the ones §10
    ships "for future re-extraction and text embedding", which no §8 stage 2 source can fetch back
    -- and left this crawl's first page in their place, while decision 335's gate counted what was
    left. [M5.3 review cycle 2, m53-c2-d375-01]

    AND A REVIEW THE BUNDLE ALREADY HOLDS IS SKIPPED, by the rule the paragraph above the last one
    applies within a source, stretched across the two authors. The corpus crawled these same
    sources, so keeping its rows means this parse re-emits many of them, and the duplicate would
    count twice in decision 335's `sum(word_count)` for exactly the reason given there. "The same
    review" is `ParsedReview.fingerprint`'s own definition for a row with no source id -- the
    author and the first 400 characters of the body -- read off the stored columns, because a
    bundle row carries no fingerprint. [M5.3 review cycle 2, m53-c2-d375-01]
    """
    await conn.execute(
        "DELETE FROM review_store.review"
        " WHERE title_id = $1 AND source = ANY($2::text[]) AND origin = $3",
        title_id, sorted(labels), DERIVED_ORIGIN,
    )
    held = {tuple(row) for row in await conn.fetch(
        "SELECT source, coalesce(author, ''), left(body, 400) FROM review_store.review"
        " WHERE title_id = $1 AND origin <> $2",
        title_id, DERIVED_ORIGIN,
    )}
    placeholders = ", ".join(f"${i}" for i in range(1, len(_REVIEW_COLUMNS) + 1))
    total = 0
    for source in sorted(by_source):
        seen: set[str] = set()
        values = []
        for mark, row in by_source[source]:
            if mark in seen:
                continue
            seen.add(mark)
            if (row.get("source"), row.get("author") or "", (row.get("body") or "")[:400]) in held:
                continue
            row["published_at"] = _timestamp(row.get("published_at"))
            values.append(tuple(row.get(column) for column in _REVIEW_COLUMNS))
        if not values:
            continue
        # `origin` is a literal in the statement rather than a bind or the column's default, for
        # the reason decision 326 gives one table over: the writer that must be findable again
        # says so in its own INSERT, and a default is a fact about the table rather than about
        # this writer.
        await conn.executemany(
            f"INSERT INTO review_store.review ({', '.join(_REVIEW_COLUMNS)}, origin)"
            f" VALUES ({placeholders}, '{DERIVED_ORIGIN}')",
            values,
        )
        total += len(values)
    return total


async def _resolve(conn: asyncpg.Connection, title_id: int, order: Sequence[str]) -> None:
    """The card, the three `title` fields below it, and the two identity columns a parse yielded.

    `resolve_title_fields` is called with this title's id and NOT unscoped: `importer/meta.py:160-164`
    records what the unscoped call would do here -- "a derive that ran this unscoped would rewrite
    every card and every trailer key in the install on every acquisition - and rewrite them
    silently, since almost every row it touched would get back the value it already had."
    """
    await meta.resolve_title_fields(conn, order, title_ids=[title_id])

    rows = await conn.fetch("SELECT source, payload FROM title_meta WHERE title_id = $1", title_id)
    by_source = {row["source"]: row["payload"] for row in rows}
    if not by_source:
        return

    resolved = {name: meta.best(by_source, name, order) for name in _TITLE_FIELDS}
    await conn.execute(
        "UPDATE title SET year = COALESCE(year, $2), runtime_min = COALESCE(runtime_min, $3),"
        " original_language = COALESCE(original_language, $4) WHERE id = $1",
        title_id, _int(resolved["year"]), _int(resolved["runtime_min"]),
        resolved["original_language"],
    )

    # Fill-never-clobber through the one implementation of that rule (`sources/_ids.set_ids`),
    # which spells it `COALESCE(existing, new)` and refuses a card field by name, so it cannot be
    # misused to write one. Decision 372 gives identity to stage 2; this is the case that
    # decision's other side names -- the parse yielded the identifier and no adapter wrote it,
    # which is what a title reaching `wikipedia:article` through a household-supplied article title
    # looks like, or a `wikidata` block the bundle imported for a title stage 2 never resolved.
    await set_ids(
        conn, title_id,
        wikipedia_title=_extra(by_source.get("wikipedia"), "title"),
        wikidata_id=_extra(by_source.get("wikidata"), "qid"),
    )


def _extra(payload: Any, key: str) -> Any:
    """One value out of a `title_meta` payload's `extra`, which the parsers store as a JSON STRING.

    `derive/parse.py` builds it with `json.dumps` on every path (`:273`, `:535`, `:887`, `:1091`),
    so `extra` is text inside the jsonb payload rather than a nested object, and a reader assuming
    a mapping would silently find nothing. The bundle importer stores the corpus's own column
    verbatim, which is the same string.
    """
    blob = (payload or {}).get("extra") if isinstance(payload, Mapping) else None
    if isinstance(blob, Mapping):
        return blob.get(key)
    try:
        parsed = json.loads(blob)
    except (TypeError, ValueError):
        return None
    return parsed.get(key) if isinstance(parsed, Mapping) else None


def _int(value: Any) -> int | None:
    """`title.year` is `smallint` and `runtime_min` is `integer`; a source may answer with a float.

    OMDb's runtime is repaired out of "116 min" and is already an int, but TVmaze and Trakt answer
    with JSON numbers and `meta.best` returns whatever the payload held. An unusable value becomes
    NULL rather than raising: decision 334 makes a source that answered nonsense the same fact as a
    source that did not answer at all.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
