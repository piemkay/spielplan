"""`title_meta`, and the title card resolved out of it. Spec v2.1 §4.1, §6.0, §10.

§4.1 lists the table in the content spine with its rule attached:

    title_meta (multi-source, per-source rows kept — "one block = one droppable source")

That rule is about **storage**. It says a source can be dropped later without a re-import, so
the import must not collapse the rows; it does not say a reader must take a whole block from
one source. The corpus itself resolves per *field* — `mdc/export.py:34-45` walks a documented
`SOURCE_PRIORITY` in `_best(rows, field)`, treating None, '' and 0 as absent — and per field is
what the data requires: tmdb carries tagline and poster and plot, omdb carries a poster and no
tagline, and wikipedia is the only source carrying `plot_short`. A whole-block rule blanks
fields another source has.

The order is the corpus's, not this app's. Decision 162 makes the corpus a one-time seed for
content, so the seed carries the rule it was assembled under: `BUNDLE.json.source_priority`
when the bundle ships one, `SOURCE_PRIORITY` below with a report line when it does not.

THREE VALUES THE WALK MAY NOT TAKE, and each is this app's rule rather than the corpus's, so the
order stays the corpus's and only what is eligible for it narrows. [owner instruction of
2026-09-25 after the first household user test]

  * An `mpst` synopsis is never the overview (decision 499). On the real bundle the last resort
    was the only resort for 965 titles, and a card then led with a full retelling, ending
    included, up to 45,643 characters. The rows stay in `title_meta` (§4.1) and still feed a pack.
  * A plot another title also carries, from a source matched onto the title by page or dataset
    row rather than by the provider's own id, is absent (decision 499): 84 MPST synopses and 102
    Wikipedia pages are each attached to two or more titles, and Moulin Rouge (1952) showed the
    2001 film's plot. `shared_plot_texts` is the one definition, and `dna/packs.py` reads it too.
  * An image on a host this app may not serve is absent (decision 501, on decision 483's hosts),
    so `title.poster_path` is servable by construction: OMDb's IMDb-hosted poster used to win
    over TVmaze's for 157 titles.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import asyncpg

from spielplan.art.hosts import servable
from spielplan.importer.report import ImportReport

# `mdc/export.py:34-45`, verbatim. mpst sits last on purpose: its synopses are the longest plot
# text in the corpus and they are full retellings, ending included — a poor default and a good
# last resort.
SOURCE_PRIORITY: tuple[str, ...] = (
    "tmdb", "omdb", "trakt", "tvmaze", "wikipedia", "jellyfin", "wikidata",
    "letterboxd", "rottentomatoes", "metacritic", "mpst",
)

# The corpus's own name for each card field. `overview` reads two of them in order, because
# `plot_short` exists on exactly one source and is better than nothing.
_PLOT_FIELDS = ("plot_full", "plot_short")
_CARD_FIELDS = {"tagline": "tagline", "poster_path": "poster_url", "backdrop_path": "backdrop_url"}
_IMAGE_FIELDS = ("poster_path", "backdrop_path")

# Decision 499. Still in `SOURCE_PRIORITY`, where it orders nothing now that it cannot win the one
# field it carries; kept there so the tuple stays the corpus's verbatim.
NEVER_OVERVIEW = frozenset({"mpst"})

# Decision 499: the sources whose plot is a page or a dataset row MATCHED onto a title - Wikipedia
# by page title, MPST by an imdb id in a third-party CSV - rather than the provider's own record of
# it. Measured on the seeded install, those two are where a shared text means a wrong film (177
# MPST and 210 Wikipedia rows); the 31 shared tmdb texts, and trakt's mirror of them, are one
# novel's synopsis on each of its adaptations (Jane Eyre 1943/1983/1996), true of every member.
MATCHED_TEXT_SOURCES = ("wikipedia", "mpst")

# The same text on another title, in the same field, from any source. `btrim` so padding spaces do
# not make two copies of one synopsis differ. Two branches rather than one over a VALUES list so
# each EXISTS names the expression 0037's hash index is built on; a derive asks this about one title
# and would otherwise scan every synopsis in the install to answer.
_SHARED_PLOT = """
SELECT m.title_id, m.source, 'plot_full' AS field
  FROM title_meta m
 WHERE m.source = ANY($2::text[]) AND ($1::int[] IS NULL OR m.title_id = ANY($1::int[]))
   AND btrim(m.payload ->> 'plot_full') <> ''
   AND EXISTS (SELECT 1 FROM title_meta o
                WHERE btrim(o.payload ->> 'plot_full') = btrim(m.payload ->> 'plot_full')
                  AND o.title_id <> m.title_id)
UNION ALL
SELECT m.title_id, m.source, 'plot_short' AS field
  FROM title_meta m
 WHERE m.source = ANY($2::text[]) AND ($1::int[] IS NULL OR m.title_id = ANY($1::int[]))
   AND btrim(m.payload ->> 'plot_short') <> ''
   AND EXISTS (SELECT 1 FROM title_meta o
                WHERE btrim(o.payload ->> 'plot_short') = btrim(m.payload ->> 'plot_short')
                  AND o.title_id <> m.title_id)
"""


async def shared_plot_texts(
    conn: asyncpg.Connection, title_ids: Sequence[int] | None = None
) -> set[tuple[int, str, str]]:
    """`(title_id, source, field)` for every matched-source plot another title also carries.

    Decision 499's one definition, read by the card resolution below and by `dna/packs.py`, whose
    pack takes the LONGEST plot and so is where a collided Wikipedia page won most often (157
    titles). Both members of a pair are flagged, because nothing on the row says which film the
    text is about; the right one loses a text a better source usually outranks anyway.
    """
    ids = None if title_ids is None else list(title_ids)
    rows = await conn.fetch(_SHARED_PLOT, ids, list(MATCHED_TEXT_SOURCES))
    return {(r["title_id"], r["source"], r["field"]) for r in rows}


def card_fields(
    by_source: Mapping[str, Mapping[str, Any]],
    priority: Sequence[str],
    shared: Collection[tuple[str, str]] = (),
) -> dict[str, Any]:
    """One title's card, per field, from its per-source rows: `best()` over what is eligible.

    `shared` is this title's `(source, field)` pairs out of `shared_plot_texts`. Pure, so the
    import, §8 stage 3 and `ops/devstub.py` resolve a card by one function rather than three.
    """
    def eligible(field: str, keep) -> dict[str, Mapping[str, Any]]:
        return {s: row for s, row in by_source.items() if keep(s, (row or {}).get(field))}

    def plot(field: str) -> Any:
        return best(
            eligible(field, lambda s, _v: s not in NEVER_OVERVIEW and (s, field) not in shared),
            field, priority,
        )

    card: dict[str, Any] = {
        "overview": next((v for f in _PLOT_FIELDS if (v := plot(f)) is not None), None),
        "tagline": best(by_source, _CARD_FIELDS["tagline"], priority),
    }
    for pg in _IMAGE_FIELDS:
        src = _CARD_FIELDS[pg]
        card[pg] = best(eligible(src, lambda _s, v: servable(v)), src, priority)
    return card


def refused_images(by_source: Mapping[str, Mapping[str, Any]]) -> int:
    """How many image URLs these rows carry on a host this app may not serve (decision 501)."""
    return sum(
        1
        for row in by_source.values()
        for pg in _IMAGE_FIELDS
        if (v := (row or {}).get(_CARD_FIELDS[pg])) not in (None, "", 0) and not servable(v)
    )


def source_priority(bundle_root: Path | None, report: ImportReport | None = None) -> list[str]:
    """The per-field precedence this bundle was assembled under.

    `BUNDLE.json` is where the corpus already records the facts the importer has to obey —
    `display_only_tables`, `frozen_rating_source_ids`, `nullable_pk_columns` — so the order
    belongs beside them rather than as a constant in this app that a corpus-side change would
    silently invalidate.

    `report` is optional because §8 stage 3 has no `ImportReport` and no business building one to
    ask which order this install resolves by. The import's note answers that question once, for a
    whole catalog; a derive repeating it per acquisition would write the same line into a report
    nobody renders.

    A DERIVE CALLS THIS WITH `bundle_root=None` AND THEREFORE RESOLVES BY `SOURCE_PRIORITY`, and
    the sentence that used to stand here claimed the opposite - that "a derive cannot resolve a
    card by an order the import never used". It cannot read `BUNDLE.json`: `api/artifacts.py:152`
    records that an imported bundle "is deleted by its own import", no column persists the order,
    and §8 stage 3 has no path to one. So on an install whose bundle ships `source_priority`, the
    ~19,000 corpus titles carry cards resolved by the bundle's order and every acquired title
    carries a card resolved by the constant, silently, because almost every field agrees between
    two orders and the ones that do not look like a different source simply winning. Both shipped
    manifests omit the key, so this is latent rather than live - and the honest statement of the
    contract is the warning below rather than a promise the derive cannot keep. Persisting the
    order for the derive to read is a change to the import's load path, which M5.3 §8 puts outside
    this milestone. [M5.3 review cycle 1,
    m53-rev1-derive-resolves-by-the-constant-not-the-bundle-order]
    """
    shipped: object = None
    manifest = (bundle_root / "BUNDLE.json") if bundle_root else None
    if manifest is not None and manifest.is_file():
        shipped = json.loads(manifest.read_text(encoding="utf-8")).get("source_priority")

    if isinstance(shipped, list) and shipped and all(isinstance(s, str) for s in shipped):
        if report is not None:
            report.note(
                "source-priority",
                f"per-field source order read from the bundle: {', '.join(shipped)}",
                priority=list(shipped), origin="bundle",
            )
            if list(shipped) != list(SOURCE_PRIORITY):
                # A WARNING RATHER THAN A SILENT FORK, and it is the only surface that can say it.
                # The bundle's order governs this import; §8 stage 3 resolves an acquired title by
                # `SOURCE_PRIORITY` because it has no manifest to read, so an install taking this
                # branch ends with two resolution orders and nothing recording that they differ.
                # The import is where a person is already reading findings, and decision 335 makes
                # the split reach further than a card: the reviews gate names `title.overview`, so
                # its plot arm inherits whichever half a title is in. Not a failure - the import is
                # correct and the corpus's cards are right - which is why it is `warn` and the
                # import still completes.
                report.warn(
                    "source-priority",
                    "the bundle's order is not the app's own, and section 8 stage 3 resolves an "
                    f"acquired title's card by the app's ({', '.join(SOURCE_PRIORITY)}): titles "
                    "this import wrote and titles acquired later resolve by different orders",
                    priority=list(shipped), app_priority=list(SOURCE_PRIORITY),
                )
        return list(shipped)

    if report is not None:
        report.note(
            "source-priority",
            "bundle ships no `source_priority` — resolving the title card by the corpus's own "
            f"order ({', '.join(SOURCE_PRIORITY)})",
            priority=list(SOURCE_PRIORITY), origin="default",
        )
    return list(SOURCE_PRIORITY)


def best(by_source: Mapping[str, Mapping[str, Any]], field: str, priority: Sequence[str]) -> Any:
    """`mdc/export.py`'s `_best`, ported.

    The three absent values are the corpus's: NULL, '' and 0. Zero matters for `budget` and
    `revenue`, where it means unknown rather than free, and an empty string matters because a
    source that answered with nothing must not stop the walk.
    """
    for source in priority:
        value = (by_source.get(source) or {}).get(field)
        if value not in (None, "", 0):
            return value
    return None


def _payload_columns(db: sqlite3.Connection) -> list[str]:
    """Every shipped column but the two that become the Postgres row's primary key.

    Keeping `title_id` and `source` inside the payload as well would store the key twice and
    invite a reader to trust the copy over the column it is joined by.
    """
    columns = [r[1] for r in db.execute('PRAGMA table_info("title_meta")')]
    return [c for c in columns if c not in ("title_id", "source")]


def _meta_rows(db: sqlite3.Connection, columns: Sequence[str]) -> Iterator[tuple]:
    select = ", ".join(f'"{c}"' for c in ("title_id", "source", *columns))
    for row in db.execute(f"SELECT {select} FROM title_meta"):
        yield row[0], row[1], dict(zip(columns, row[2:], strict=True))


async def load_title_meta(
    conn: asyncpg.Connection, db: sqlite3.Connection, report: ImportReport
) -> int:
    """Load the bundle's per-source meta rows, one Postgres row per (title, source).

    Bespoke rather than a `TableMap` because the shapes do not correspond: the corpus ships 21
    typed columns and this app keeps `payload jsonb`, which is what lets a source the corpus
    adds arrive without a migration.
    """
    present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "title_meta" not in present:
        report.warn("title-meta", "bundle has no `title_meta` — the title card has no source")
        return 0

    columns = _payload_columns(db)
    rows = list(_meta_rows(db, columns))
    await conn.execute("DELETE FROM title_meta")
    # INSERT rather than COPY, which the rest of this package uses: asyncpg's COPY encoder is
    # binary and the json/jsonb codec `db/pool.py` registers is a text one, so a dict reaches
    # a binary COPY as "no binary format encoder for type jsonb". 46,318 rows is not the place
    # to trade a correct codec for a faster path.
    await conn.executemany(
        "INSERT INTO title_meta (title_id, source, payload) VALUES ($1, $2, $3)", rows
    )
    count = len(rows)
    report.table_counts["loaded:title_meta"] = count
    report.note(
        "title-meta",
        f"{count} per-source meta rows kept — §4.1's 'one block = one droppable source'",
        columns=columns,
    )
    return count


async def resolve_title_fields(
    conn: asyncpg.Connection,
    priority: Sequence[str],
    report: ImportReport | None = None,
    title_ids: Sequence[int] | None = None,
) -> None:
    """Resolve §6.0's card fields onto `title`, per field, keeping the per-source rows.

    Only titles that have a meta row are touched. A title with none must render the card
    without those fields (§6.0), and blanking one would also overwrite whatever §8's
    acquisition path wrote for a title the corpus never shipped. `title_ids` is the per-title
    half of that same sentence: §10's import resolves a whole catalog in one pass and passes
    None, while §8 stage 3 derives ONE title and names it, because a derive that ran this
    unscoped would rewrite every card and every trailer key in the install on every acquisition
    - and rewrite them silently, since almost every row it touched would get back the value it
    already had.

    A parameter rather than a sibling `resolve_title_fields_for`, and the requirement is what
    decides it: the per-title resolution must use "the same source priority and the same
    absent-value rule as the bundle importer rather than a second implementation"
    (`jellyfin-acquisition-eval-a-re-derive-is-idempotent`), and decision 335 then builds the
    reviews gate's plot half on `title.overview` precisely because the `plot_full` ->
    `plot_short` fallback has exactly one owner. Two entry points satisfy both sentences on the
    day they are written and part company on the day one of them is edited; one function cannot
    drift from itself.
    """
    # NULL for the import, an int[] for a derive - a predicate rather than a clause composed into
    # the statement, so that no branch anywhere can read an empty sequence as "every title":
    # `ANY('{}')` is false for every row, which is exactly what a caller whose scope came out
    # empty must write. [plan section 2.6]
    ids = None if title_ids is None else list(title_ids)

    grouped: dict[int, dict[str, Any]] = {}
    rows = await conn.fetch(
        "SELECT title_id, source, payload FROM title_meta"
        " WHERE ($1::int[] IS NULL OR title_id = ANY($1::int[]))",
        ids,
    )
    for row in rows:
        grouped.setdefault(row["title_id"], {})[row["source"]] = row["payload"]

    # Scoped like the read above. A derive's text is compared against every other title's, and
    # the other member of a pair it creates keeps its card until its own next resolution: this
    # function writes the titles it names and no others (decision 375).
    shared: dict[int, set[tuple[str, str]]] = {}
    for title_id, source, field in await shared_plot_texts(conn, ids):
        shared.setdefault(title_id, set()).add((source, field))

    updates = []
    without_overview = refused = 0
    for title_id, by_source in grouped.items():
        card = card_fields(by_source, priority, shared.get(title_id, ()))
        without_overview += card["overview"] is None and any(
            (row or {}).get(f) not in (None, "", 0)
            for row in by_source.values() for f in _PLOT_FIELDS
        )
        refused += refused_images(by_source)
        updates.append(
            (title_id, card["overview"], card["tagline"],
             card["poster_path"], card["backdrop_path"])
        )

    await conn.executemany(
        "UPDATE title SET overview = $2, tagline = $3, poster_path = $4, backdrop_path = $5 "
        " WHERE id = $1",
        updates,
    )

    # §4.3's `title_video` carries no source column into this app, so the trailer is chosen by
    # what it is rather than by who said so: a trailer before a teaser, YouTube before the
    # sites the player cannot embed. Scoped inside the subquery and not on the UPDATE's own
    # WHERE: the join alone would already confine the write, but a derive of one title has no
    # business reading and ordering every video row in the install to place one key.
    await conn.execute(
        """
        UPDATE title t SET trailer_key = v.key
          FROM (SELECT DISTINCT ON (title_id) title_id, key
                  FROM title_video
                 WHERE key <> '' AND ($1::int[] IS NULL OR title_id = ANY($1::int[]))
                 ORDER BY title_id, (lower(type) = 'trailer') DESC, (site = 'YouTube') DESC, key
               ) v
         WHERE v.title_id = t.id
        """,
        ids,
    )

    if report is None:
        return

    # The count follows the scope for the same reason §10 makes the report a per-table accounting
    # rather than a total: a line that is true of the database and false about the call it
    # describes is the shape of accounting this importer's own history says to refuse.
    trailers = await conn.fetchval(
        "SELECT count(*) FROM title WHERE trailer_key IS NOT NULL"
        " AND ($1::int[] IS NULL OR id = ANY($1::int[]))",
        ids,
    )
    report.note(
        "title-card",
        f"{len(updates)} title cards resolved per field from title_meta; "
        f"{trailers} carry a trailer key",
        titles=len(updates),
    )
    # The two narrowings stated where the operator reads the import, with the counts that make
    # them checkable: a card that lost its text or its art to a rule looks, on the screen, exactly
    # like a title no source had anything for.
    report.note(
        "title-card",
        f"{without_overview} titles carry plot text only from MPST or text another title shares, "
        f"and show no overview (decision 499); {refused} poster/backdrop URLs on hosts this app "
        "may not serve were skipped (decision 501)",
        without_overview=without_overview, refused_images=refused,
    )
