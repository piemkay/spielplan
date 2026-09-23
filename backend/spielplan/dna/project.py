"""Stage 8, the projected tier: one title's own keywords, mapped through the alias map.

Spec v2.1 §8 stage 8 (`spec:393`), §5.3's job row (`spec:238`), §4.1 rules 1 and 2, §14 risk 7;
decisions 163, 188, 384, 385, 387.

**PER-TITLE, WHERE THE CORPUS IS DELIBERATELY WHOLESALE.** `mdc/dna/project.py:170-172` refuses
the incremental form in its own docstring -- "Rebuild `dna_projected` from the term inventories.
Wholesale, not incremental -- it is a pure function of the inputs, so a partial update would only
be a way to get it subtly wrong" -- and §5.3's job row knows it, naming the difference out loud:
"DNA projection for a new title (per-title incremental -- new code; the corpus `dna project` is
deliberately wholesale)" (`spec:238`). §8 stage 8 says NEW CODE for that reason and not for want
of a port. The corpus's objection is real and narrower than it reads: its wholesale form DELETEs
the whole table and rewrites it, so a PARTIAL version of that leaves a table half derived from
one alias map and half from another. This is not a partial rebuild. It is the same pure function
restricted to the inputs of one title, and one title's projected rows depend on no other title's
rows at any point in the derivation -- the corpus's own `project` builds `per[tid]` per title and
never reads across titles (`:177-186`). `UNIQUE (title_id, version, term)` (`0004_dna.sql:113`)
is what makes the restriction safe rather than merely convenient: every write below is keyed
inside the title it was handed, so a re-run cannot reach a row belonging to another.

**WHAT THE RESTRICTION COSTS, SAID RATHER THAN HIDDEN.** A term this function wrote and a later
run no longer derives is not removed. The corpus gets that for free from its DELETE; here the
only way to know a row is stale would be to know this function wrote it, and `dna_projected`
carries no producer column -- the bundle's own 223,136 rows sit in the same table under the same
shape. Deleting a title's rows on the strength of a guess would delete the importer's, which is
the one thing this milestone is told not to do, so the row stays and an inventory that stops
naming a term leaves a claim standing. That is a smaller wrong than the alternative and it is
still a wrong; the wholesale re-derivation that would fix it is a different question with its own
cost, and a milestone that wants it re-opens it deliberately.

**IT DOES NOT RE-PROJECT THE BUNDLE'S EXISTING ROWS, AND IT REFUSES A BUNDLE TITLE TO MAKE THAT
TRUE.** Those rows came out of `content.sqlite` through `importer/dna.load_projected`: §8 stage 8
runs inside the acquisition pipeline, over a title minted at or above 1e9 with `origin =
'acquired'` (`0008_placement.sql:46-48`, §4.1's minting rule), and §5.3's job row budgets
"DNA projection for a new title". This paragraph used to rest the property on no caller this
milestone ships handing it a bundle title, which is true only because decision 387 ships no
caller at all -- and measured, a bundle title handed in had its importer row's corpus `n_sources`
overwritten by a local inventory count, on the one column `db/dna_terms.TERM_WEIGHT` ranks both
populations by. Decision 162 seeds content once and `importer/bundle.py` names `load_projected`
among the tiers it forbids re-importing, so that write could never have been undone. The
refusal is therefore not an admission rule this module invents but the plan's non-goal
(M5.4-plan.md §8) and decision 162 made enforceable, and it raises rather than answering 0
because a caller that reaches here with a bundle title has made a mistake about which
population it is in. What it costs is stated rather than hidden: §5.3 parks a thin bundle title
as an acquisition job, and whether such a title may ever be projected -- adding rows beside
content the corpus shipped -- is "a different question with its own cost", the plan's words,
for the milestone that routes one through stage 8. [M5.4 review cycle 3, M54-DIM5-04]

**THE WEIGHT IS A COUNT OF INVENTORIES, NOT THE CORPUS'S AGREEMENT LADDER (decision 385).**
`mdc/dna/project.py:82-89` maps 1/2/3+ sources onto 0.3/0.6/1.0 so that a projected tag sits on
the same 0..1 scale as a salience; this app does not. `importer/dna.load_projected` writes the
bundle's raw `n_sources` into `dna_projected.weight`, decision 188 keeps it raw rather than
re-scaling at import (which would need the re-import decision 162 does not provide), and
`db/dna_terms.TERM_WEIGHT` bounds it at read time with the saturating `0.30 * (c / (1 + c))`.
So the column is a COUNT here and a fraction there, and writing the corpus's ladder into it would
put acquired titles on a different scale from bundle titles through the one expression both are
read by: a three-source projection would reach the read layer as 0.30 * (1.0 / 2.0) = 0.15 where
the importer's identical row reaches it as 0.30 * (3.0 / 4.0) = 0.225, and a one-source
projection as 0.0692 against 0.15. That is M4.9 finding 20 -- the projected tier and the
extracted tier ranked on scales that were never the same -- reopened through a new path, and it
would show up as Home's why-line and Tonight's vectors preferring bundle titles over acquired
ones for a reason no surface names.

**`via` IS THE KEYWORD THAT PRODUCED THE ROW, NOT THE INVENTORY THAT SAID IT (decision 393).** The
two columns answer two questions and the weight above already answers the count one.
`0004_dna.sql:111` declares this one as "the keyword/alias that produced it" and M5.4's own plan
repeats the phrase, and the bundle side agrees: `importer/dna._via` flattens rows whose payload is
`["keyword:obsession", "keyword:heist"]`, which are spellings and not sources. Measured on the
fixture bundle this install ships, `(title 1, themes.obsession)` carries
`via = keyword:obsession, keyword:heist` beside a count of two, while
`(title 2, structure.procedural)` carries ONE keyword beside that same count of two -- so the two
columns demonstrably disagree in length there, which settles that `sources` is not a source list.
Writing inventory names here instead put two units in one text column with no reader able to tell
the populations apart, and left "which keyword produced this term" answerable for a bundle title
and unanswerable for an acquired one. The `keyword:` prefix is the bundle's own spelling and is
matched, for the same reason the weight matches the importer's unit rather than the corpus's.

**FACET: THE TERM'S OWN PREFIX, DERIVED ONCE (M4.9 finding 1).** `importer/dna.app_facet` owns
that rule and applies it at load into `dna_term.facet`, which `dna/aliases.load_alias_map` joins
and carries through -- so the facet written below is `app_facet`'s answer, arrived at through the
one place the vocabulary already stores it. Re-deriving `split_part(term, '.', 1)` here would be
a third copy of a rule that cost 206,151 of 223,136 projected rows a facet that joined nothing
the last time it lived in more than one place, and `dna_projected` has no FK to `dna_facet` to
notice a wrong label. `dna/norm.py`'s single-definition guard exists one module over for exactly
this failure shape.

**WHAT IS DELIBERATELY NOT USED: the `weight` column on `title_keyword`.** MovieLens genome
relevance and user-tag counts both encode popularity, and genome relevance is measured to
correlate rho ~= 0.51 with per-title tag count (§8). Folding that in would import a popularity
confound into a representation that is supposed to describe the film. That paragraph is
`mdc/dna/project.py:47-51`'s, with two characters transliterated for a cp1252 console. On this
app it is a decision already taken twice over and worth recording anyway: `0003_content.sql:99`
declares `title_keyword` as (title_id, keyword, source) with no weight column at all, and
`importer/load.py:156-159` maps only those three out of a bundle whose own `title_keyword` DOES
ship `weight REAL DEFAULT 1.0` (`tests/fixtures/make_bundle.py:418-420`). So the confound is
excluded by the schema rather than by this code, and a later importer that widened the table
would re-open the question without meaning to.

**THE PROJECTION CAP IS NOT PORTED (decision 384).** `mdc/dna/project.py:94-121` caps a projected
row at 0.45 for the terms listed in `projection_capped_v1.txt`, tuned on the 2026-08-21 vocab
audit grid, where the cap on that list beat both no-cap and the all-81-term 0.3 variant (fail@5
7->5, great@5 156 against 155 and 147, Shawshank kin-median 91->85, canaries hold). None of it
ships: there is nothing for a 0.45 ceiling to bound, because the number this app writes is not on
the corpus's 0..1 scale at all, and `models/artifacts.VOCAB_FILES` keeps its four names rather
than reading a fifth file. The measurement is recorded here so a milestone that re-scales the
column re-opens the cap deliberately instead of inheriting a constant that measured something
else.

**ONE VOCABULARY VERSION PER RUN.** §14 risk 7 makes it binding -- "every read is scoped to one
version" (`spec:503`) -- and decision 163 refuses a second one, so the caller either names the
version it is already scoped to or gets the active one from `db/dna_terms.active_version`, never
a literal `"v1"` and never a second derivation of which vocabulary is live. `None` from that
function is the M0 and pre-seed state rather than an error (§3.1), so an install with no
vocabulary projects nothing and says so by writing nothing.

**NO WORKER JOB, AND THAT IS THE DECISION RATHER THAN THE OMISSION (decision 387).**
`worker.py:1136` registers `Job("dna-projection", "M5", "acquisition", "<1 s")` with no `run`,
and this milestone leaves it that way: `worker.py` and `acquire/stages.py` belong to the
milestone that holds both halves of the wiring. What §5.3 gives a budget is the work, not the
registration, so `test_dna_project.py` times this function directly -- the shape
`test_placement.py::test_cold_tower_placement_of_one_title_stays_under_one_second` already uses
one milestone earlier, where it times `reconcile.reconcile` and not the job that reaches it.
"""

from __future__ import annotations

import asyncpg

from spielplan.db import dna_terms
from spielplan.dna.aliases import alias_key, load_alias_map

# The inventories worth projecting, and why each is here:
#   llm:*            pass-0 memory tags -- three models, independent of each other
#   movielens        the tag genome, already relevance-thresholded upstream
#   movielens_tags   free user tags
#   mpst             a 71-tag controlled set over 4,440 titles
#   tmdb, wikidata   crawled keyword inventories
#
# `title_aspect` (pass-1's 30 phrases per title) is deliberately absent: it is
# only ~30% complete, and its phrases are the material the vocabulary was
# curated FROM, so projecting them back would be close to circular.
#
# The ten lines above are `mdc/dna/project.py:68-77`'s, verbatim but for the two em-dashes. The
# corpus's `sources=` PARAMETER is not ported with the tuple: nothing in this app chooses a
# different set, and an argument no caller passes is a knob rather than a decision. A later
# milestone that acquires an inventory this list does not name edits the list, which is the edit
# a reader can see.
DEFAULT_SOURCES = (
    "llm:gemini37", "llm:sonnet5", "llm:terra",
    "movielens", "movielens_tags", "mpst", "tmdb", "wikidata",
)


async def project_title(
    conn: asyncpg.Connection, title_id: int, *, version: str | None = None
) -> int:
    """Project one title's keywords onto the vocabulary. Returns the number of terms written.

    Idempotent by construction and not by luck: the derivation reads only rows that belong to
    this title and this vocabulary version, and the write is an upsert on the key
    `0004_dna.sql:113` already declares, so running it twice writes the same values into the same
    rows. `created_at` is deliberately absent from the SET list -- a row whose values did not
    change did not arrive again, and §8 stage 7's audit trail would be reading a timestamp that
    means "last swept" while looking like "first derived".

    Rule 2 holds over the whole function: the count below is written and never compared, nothing
    is admitted or refused by its size, and a term named by one inventory is stored exactly as a
    term named by six is. §4.1 measures what a cut here would cost at 65% of projected pairs
    (`mdc/dna/project.py:38-41`: "65% of projected pairs have a single source, and dropping them
    would discard most of the signal along with most of the noise"). Rule 1 holds too and holds
    trivially: this writes the projected tier and reads neither the extracted one nor the
    sanctioned view, so there is no statement here in which the two tiers could meet.
    """
    origin = await conn.fetchval("SELECT origin FROM title WHERE id = $1", title_id)
    if origin != "acquired":
        raise ValueError(
            f"title {title_id} has origin {origin!r} and stage 8 projects only an acquired title: "
            "a bundle title's projected rows are content decision 162 seeds once, and a re-derive "
            "here would overwrite them with no import able to restore them"
        )
    scoped = await dna_terms.active_version(conn) if version is None else version
    if scoped is None:
        return 0

    amap = await load_alias_map(conn, scoped)

    keywords = await conn.fetch(
        """
        SELECT k.keyword, k.source
          FROM title_keyword k
         WHERE k.title_id = $1
           AND k.source = ANY($2::text[])
        """,
        title_id, list(DEFAULT_SOURCES),
    )

    # Both sides of the lookup pass through `alias_key`, which is the contract
    # `load_alias_map`'s docstring states and the corpus's `:183` performs in the same position:
    # the app stores `raw_term` exactly as the bundle spelled it, so a map keyed one way and read
    # another answers nothing and answers it silently. The set is what makes the count a count of
    # INVENTORIES rather than of rows -- two MovieLens keywords naming one term are one source,
    # which is the whole content of the claim the weight carries. `produced` is the other set and
    # the other question: which SPELLINGS reached this term, which is what `via` has always been
    # declared to hold and what the bundle's own rows carry (decision 393). One loop, because a
    # second pass over the same keywords is a second chance for the two to disagree about which
    # rows matched.
    named: dict[tuple[str, str], set[str]] = {}
    produced: dict[tuple[str, str], set[str]] = {}
    for row in keywords:
        keyword = row["keyword"] or ""
        hit = amap.get(alias_key(keyword))
        if hit is None:
            continue
        named.setdefault(hit, set()).add(row["source"])
        produced.setdefault(hit, set()).add(f"keyword:{keyword}")

    # Sorted by (facet, term) so the statement order is a function of the inputs: a failure mid
    # batch then reproduces from the same rows rather than from whichever order a dict happened
    # to have. `float` because `0004_dna.sql:110` is `real`, and because a count crossing into
    # that column as an int is the one place the unit could be read as an index.
    rows = [
        (title_id, scoped, term, facet, float(len(inventories)),
         ", ".join(sorted(produced[(facet, term)])))
        for (facet, term), inventories in sorted(named.items())
    ]
    if not rows:
        return 0

    await conn.executemany(
        """
        INSERT INTO dna_projected (title_id, version, term, facet, weight, via)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (title_id, version, term)
        DO UPDATE SET facet = EXCLUDED.facet, via = EXCLUDED.via, weight = EXCLUDED.weight
        """,
        rows,
    )
    return len(rows)


__all__ = ["DEFAULT_SOURCES", "project_title"]
