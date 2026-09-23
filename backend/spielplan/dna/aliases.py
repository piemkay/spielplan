"""The alias map, read for the first time: a raw keyword spelling -> the term it names.

Spec v2.1 §8 stage 8 ("per-title alias-map projection of its keywords ... same alias map"),
§4.1 rule 1, §14 risk 7; decisions 383, 384, 188, 163.

`dna_alias` has been loaded since M4.5 and read by nobody. Before this module, `grep -rn
dna_alias backend/spielplan/` returned the loader (`importer/dna.py:181-220`) and the archive's
table list (`backup/movie_data.py:141`) and nothing else, and a table nothing reads is a table
whose fidelity gaps nothing has ever had to notice. Stage 8 is the first caller, so the three
gaps M5.4-plan.md §2.4 measured are closed here, at the read.

**TWO NORMALISATIONS, AND THEY ARE NOT INTERCHANGEABLE.** `norm()` in `norm.py` is the
quote-comparison fold: it decides whether an extractor's quote is a substring of the pack, and
`test_dna_norm.py` asserts it is the only function of its kind in the package, because a second
one makes the pack writer and the verifier disagree about what two strings are and good tags are
dropped with nothing in the log. `alias_key()` below does a different job on the other side of
the pipeline: it keys a lookup table, so that "slow-burn", "slow burn", "Slow-Burn" and "The slow
burn" reach one row. Neither is a better version of the other and neither may be folded into the
other -- `norm()` lowercases and folds smart punctuation but keeps hyphens and leading articles,
which would leave "slow-burn" and "slow burn" as two keys; `alias_key()` drops a leading article,
which inside a quote comparison would let a quote verify against a pack that does not carry it.
Nothing here is named `norm` or `normalise`, so a search for a second fold finds one module or a
bug rather than an argument about which one was meant.

**GAP 1: `kind='lexicon'` ROWS NEVER PROJECT.** The corpus skips them
(`mdc/dna/project.py:144-149`) for a measured reason, carried verbatim in the skip's own comment
below. The app's loader reads only `raw_term` and `vocab_term` (`importer/dna.py:200-215`) and
drops the column entirely, so until the loader fills it a lexicon row is stored indistinguishably
from a projecting one. `0027` adds `dna_alias.kind` and decision 383 records the one-line loader
fill as owed rather than taken: `importer/dna.py` belongs to a milestone building in parallel.
The consequence is stated rather than hidden -- a NULL `kind` reads as "not known to be lexicon",
which is every shipped row on this install today, so the rule is armed and currently excludes
nothing.

**GAP 2: THE KEY IS THE NORMALISED RAW TERM.** The corpus keys the map on
`mdc/aspects/prompt.normalise` output because the term pool the map was built from is keyed on it
too, and its own docstring warns that "Using a different normalisation here would silently drop
most of the map". The app stores `raw_term` VERBATIM, so the stored spelling and the incoming
keyword must BOTH pass through `alias_key()` on every read -- normalising one side only is the
same silent drop one step later.

**GAP 3: A ROW WHOSE TERM THE VOCABULARY DOES NOT CARRY IS SKIPPED.** `dna_alias` has no foreign
key from `term` to `dna_term` (`0004_dna.sql:42-47` foreign-keys `version` alone), so the table
holds mappings onto terms the vocabulary never adopted. The loader's own comment at
`importer/dna.py:207-208` says the opposite -- "`dna_alias.term` is NOT NULL, so those are a
constraint violation mid-transaction rather than a row" -- but NOT NULL is a statement about the
absence of a value, not about the existence of a term. The row is the authority and the comment
is wrong, which is why the read below joins `dna_term` rather than trusting either. A mapping
onto a term no facet declares projects a `dna_projected` row whose `facet` joins nothing, which
is the defect M4.9 finding 1 measured at 206,151 of 223,136 projected rows.

**THE SECOND HALF OF THE MAP.** A term's own id is a legitimate raw spelling of itself, added
with `setdefault` so an explicit map row always wins -- the corpus's order, and the one that
matters, because an authored row is a decision and the implicit one is only a default. The corpus
also keys each term's `label` and its authored `aliases`; this app's `dna_term` stores neither,
deliberately (`importer/dna.py:143-146`: they are vocabulary-construction evidence, and the label
is the term id minus its facet prefix, so storing it would be storing a substring of the key).
Those spellings reach this install through `alias_map_v1.tsv`'s own rows instead, which is where
the bundle puts them.

**`register` TAKES NO IMPLICIT SELF-MAPPING.** The register proposal's uniform migration
protocol, rule 2: "Register's projected tier is built **only** from the explicit projection map
... The twins' LLM/ML keyword surfaces become extraction lexicon and query-bridge entries, not
projection sources, until each surface is individually adjudicated into the map." The facet is
named here by this app's facet id -- `register`, the term prefix -- and never by the corpus's
extraction label `register_audience`: the two namings are the confusion M4.9 finding 1 measured
at 29,188 of 31,540 `dna_tag` rows, and `dna_facet`, `dna_term` and §6.8's palette all key on the
prefix.

**WHAT IS NOT PORTED, AND WHY IT COULD NOT BE (decision 384).** `mdc/dna/project.py` carries
`agreement_weight` (`:86`) and `PROJECTION_CAP = 0.45` (`:106`) over the terms listed in
`projection_capped_v1.txt`, tuned on the 2026-08-21 vocab audit grid: the cap on that list beats
both no-cap and the all-81-term 0.3 variant (fail@5 7->5, great@5 156 against 155 and 147). None
of it ships here and `models/artifacts.VOCAB_FILES` keeps its four names, because there is
nothing for a 0.45 ceiling to bound: decision 188 keeps `n_sources` a raw COUNT in
`dna_projected.weight` and `db/dna_terms.TERM_WEIGHT` applies the saturating `0.30 * c / (1 + c)`
at read time, so the number this app writes into that column is not on the corpus's 0..1 scale at
all. A milestone that re-scales the column re-opens the question deliberately rather than
inheriting a constant that measured something else.

**ONE VOCABULARY VERSION PER READ.** §14 risk 7 makes it binding and decision 163 refuses a
second version, so the caller either names the version it is already scoped to or gets the active
one from `db.dna_terms.active_version` -- never a literal `"v1"`, and never a second derivation
of "which vocabulary is live" (`dna_terms`'s own docstring records what the two copies of that
rule cost the last time there were two). `None` from that function is the M0 and pre-seed state
rather than an error, so an install with no vocabulary reads an empty map.
"""

from __future__ import annotations

import re

import asyncpg

from spielplan.db.dna_terms import active_version

_PUNCT = re.compile(r"^[\s\"'`\-–—.,;:()\[\]]+|[\s\"'`\-–—.,;:()\[\]]+$")
_WS = re.compile(r"\s+")
_ARTICLE = re.compile(r"^(?:the|a|an)\s+")

# §6.4's facet id, not the corpus's extraction label `register_audience`. See the module
# docstring's paragraph on the register facet: this app keys every facet on the term prefix.
_EXPLICIT_MAP_ONLY_FACET = "register"


def alias_key(phrase: str) -> str:
    """The alias map's lookup key: the exact-duplicate key.  Deliberately light.

    Real synonym resolution is pass 2's job and needs embeddings; anything
    clever here would pre-empt that decision with a worse method.  This only
    collapses spellings of the *same* phrase: case, whitespace, surrounding
    punctuation, hyphen-vs-space ("slow-burn" / "slow burn") and a leading
    article.  Plurals are left alone - "long take" and "long takes" go to the
    clustering as two phrases, which is where that call belongs.

    Ported from `mdc/aspects/prompt.normalise`; the paragraph above and the body below are the
    corpus's, verbatim, and only the name changed -- it changed because the name it arrived with
    is the one name this package may not spell twice.  "Pass 2" and "the clustering" above are
    corpus machinery this app does not have, which makes the lightness MORE load-bearing rather
    than less: nothing downstream recovers a spelling this key misses, so the map simply does not
    contain it.  THIS IS NOT `norm()` -- see the module docstring -- and the two may never be
    folded together in either direction.
    """
    p = (phrase or "").lower().replace("’", "'")
    p = p.replace("-", " ").replace("/", " ")
    p = _WS.sub(" ", p)
    p = _PUNCT.sub("", p)
    p = _ARTICLE.sub("", p)
    return p.strip()


async def load_alias_map(
    conn: asyncpg.Connection, version: str | None = None
) -> dict[str, tuple[str, str]]:
    """Normalised raw term -> (facet, vocabulary term id), for one vocabulary version.

    Keyed on :func:`alias_key` output, which is what the term pool the alias map was built from
    is also keyed on.  Using a different normalisation here would silently drop most of the map.

    The two sentences above are the corpus's own (`mdc/dna/project.py:129-134`, with the function
    renamed) and they are the whole contract of this function: a caller must pass its keywords
    through `alias_key` as well, because the app stores `raw_term` exactly as the bundle spelled
    it and a map keyed one way read another way answers nothing.
    """
    if version is None:
        version = await active_version(conn)
    if version is None:
        return {}

    # The JOIN is gap 3, and it is a join rather than a filter because `dna_alias` has no foreign
    # key to `dna_term`: an unadopted term is stored, not rejected, whatever the loader's comment
    # says. The facet comes from `dna_term` and not from the map file's own `facet` column, which
    # the loader does not store -- the facet that travels with a mapping has to be the one
    # `dna_facet`, §6.4's axes and §6.8's palette all key on, or the projected row it produces
    # joins nothing.
    #
    # ORDER BY the raw alias, because two raw spellings can fold to one key: "slow-burn" and
    # "slow burn" are two rows under `PRIMARY KEY (version, alias)` and one key here. The corpus
    # resolved that collision by file order, which a table does not have, and an unordered read
    # would make `dna_projected` depend on whichever row the planner happened to return last --
    # §8 stage 8's output has to be reproducible from its inputs.
    #
    # `COLLATE "C"` BECAUSE THE SENTENCE ABOVE CLAIMS MORE THAN A BARE `ORDER BY` DELIVERS. The
    # map is last-write-wins below, so where two spellings fold to one key the winner IS the sort
    # order -- and the sort order of a `text` column is the cluster's collation, which
    # `docker-compose.yml` never pins: the image's default decides it. Under `en_US.utf8` the
    # order is "slow burn", "slow-burn", "Slow Burn"; under `C` it is "Slow Burn", "slow burn",
    # "slow-burn", and a different row wins. Two installs holding byte-identical `dna_alias` rows
    # would then derive different `dna_projected` rows, and README's Recovery path -- restore into
    # a rebuilt box -- is exactly how one install becomes the other. Naming the collation makes
    # the tie-break a property of this statement rather than of whoever ran `initdb`.
    # [M5.4 review cycle 1, M54-DIM3-03]
    rows = await conn.fetch(
        """
        SELECT a.alias, a.kind, t.term, t.facet
          FROM dna_alias a
          JOIN dna_term t ON t.version = a.version AND t.term = a.term
         WHERE a.version = $1
         ORDER BY a.alias COLLATE "C"
        """,
        version,
    )

    # Both loops drop a spelling that folds to nothing rather than keying it on "": one empty key
    # would answer every keyword that folds to nothing with whichever row wrote that key last.
    # The corpus's `if key:`, and the reason it is there.
    out: dict[str, tuple[str, str]] = {}
    for row in rows:
        # kind='lexicon' rows are extraction-lexicon / query-bridge only: they never project.
        # Used by the register migration (v3.1) so the mood twins' keyword surfaces cannot walk
        # projected rows into the register facet through the back door (measured: Django and
        # Hostel both inheriting register.pulp, cos 0.894).
        #
        # The corpus's own comment (`mdc/dna/project.py:144-149`), re-wrapped to this file's
        # width and otherwise verbatim. Two named changes to the test under it: the match is
        # case-folded, because the column is hand-authored in a TSV and `Lexicon` is the same
        # decision as `lexicon`; and NULL reads as "not known to be lexicon", which is every row
        # on this install until the loader fills the column (decision 383).
        if (row["kind"] or "").strip().casefold() == "lexicon":
            continue
        key = alias_key(row["alias"])
        if key:
            out[key] = (row["facet"], row["term"])

    # A term's own id is a legitimate raw spelling of itself -- EXCEPT register terms, whose
    # projected tier is explicit-map-only per the register proposal's migration protocol rule 2.
    # `setdefault`, so an authored map row always beats the default.
    for row in await conn.fetch(
        "SELECT term, facet FROM dna_term WHERE version = $1 ORDER BY term", version
    ):
        if row["facet"] == _EXPLICIT_MAP_ONLY_FACET:
            continue
        key = alias_key(row["term"])
        if key:
            out.setdefault(key, (row["facet"], row["term"]))
    return out


__all__ = ["alias_key", "load_alias_map"]
