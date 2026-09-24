"""§6.6 Data's review of DNA rejects and low-evidence tags: two orderings, and never a filter.

Spec v2.1 §6.6 Data as v2.1.3 words it ("The review is two orderings and never a filter: the DNA
rejects newest first, and a title's extracted tags by ascending confidence, which is what
low-evidence means"), §4.1 rule 2, §8 stage 7; decisions 341, 396, 401 and 446.

TWO LISTS, BECAUSE THE TWO HALVES OF §6.6'S PHRASE ARE ABOUT TWO TABLES. A reject is what stage 7
refused, and `dna_reject` records it with the rule it broke; a low-evidence tag is one stage 7 let
through, and it lives in `dna_tag`. Decision 341 gave `dna_reject` no confidence column because what
it records never earned one, so plan D4's "ordered ascending by confidence" can only be the second
list's, and decision 446 says so.

LOW-EVIDENCE IS AN ORDERING. §4.1 rule 2 makes the three DNA weights weights and never filters, and
measures what one cut costs: "a 0.5 cut would delete 44% of the extracted tier". A review screen is
where that cut is most tempting - its subject is the weakest tags - so the second read returns every
extracted tag the title carries at the active version, weakest first, and the only predicates it
has are the title and the version. The first read is bounded, and the bound is on recency: the rows
not shown are the oldest refusals, never the ones some weight ranked last.

NEITHER READ ACCEPTS ANYTHING. §8 stage 7: "Failures drop, never repaired". The review's one action
is writing a ledger row, which `curated/adjudications` does; there is no route from a rejected tag
back into `dna_tag`, and a module that offered one would undo the trust boundary stage 7 is.

WHAT HOLDS THIS STATICALLY. `test_landmine_guards.py` reads this file with the rest of the package.
Decision 401 records two shapes that package-wide guard is blind to, a null test on a weight and a
truthiness test in Python, and decision 446 answers them with a guard over the review's own files;
this module writes neither, and keeps its bounded read above the unbounded one so that the ranked
read and the bounded one are never read by that guard as one statement.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from spielplan.db import dna_terms

# How many refusals the review lists, newest first. A screen's worth for an operator reading what
# this install has been dropping lately; the rows past it are the oldest, and the per-rule index
# 0027 builds (`dna_reject_rule`, newest first) is there for the drill-down the screen offers next.
REJECT_LIMIT = 200

# A LEFT JOIN because decision 396 writes an `unknown_title` refusal with a NULL title, and an inner
# join would drop exactly the refusal that says the extractor answered about a film nobody asked for.
_REJECTS = """
    SELECT r.id, r.title_id, t.name, t.year, r.term, r.facet, r.salience, r.quote,
           r.rule_violated, r.provider, r.at
      FROM dna_reject r LEFT JOIN title t ON t.id = r.title_id
     ORDER BY r.at DESC, r.id DESC
     LIMIT $1
"""


async def rejects(conn: asyncpg.Connection, *, limit: int = REJECT_LIMIT) -> list[dict[str, Any]]:
    """The newest refusals, each with the title it named, the quote offered and the rule it broke.

    Plan D4: "Each row shows the term, the quote, the salience and the rule violated". The rows are
    returned as stored: `dna_reject` keeps an out-of-range level verbatim precisely so this screen
    can show what the extractor claimed.
    """
    return [dict(row) for row in await conn.fetch(_REJECTS, limit)]


# Decision 446's ordering, and the query `test_dna_reject.py` documents as `LOW_EVIDENCE_REVIEW`,
# with the tie-breaks it names. NULLS LAST because a NULL confidence means nobody measured it, which
# is not the claim that it is the weakest measured reading. `provider` last so that two providers'
# readings of one term, which decision 337's parallel mode keeps side by side, come back in a stable
# sequence.
_LOW_EVIDENCE = """
    SELECT t.term, t.facet, t.salience, t.confidence, t.n_sources, t.provider
      FROM dna_tag t
     WHERE t.title_id = $1 AND t.version = $2
     ORDER BY t.confidence ASC NULLS LAST, t.n_sources ASC NULLS LAST, t.term, t.provider
"""


async def low_evidence(conn: asyncpg.Connection, title_id: int) -> dict[str, Any]:
    """Every extracted tag of one title at the active vocabulary, weakest first.

    Scoped to the active version because §14 risk 7 scopes every read to one, and a title that has
    been extracted under two vocabularies must not show the older one's tags as this one's. No
    vocabulary is §3.1's bundle-less state, which has no tags to show either.
    """
    version = await dna_terms.active_version(conn)
    if version is None:
        return {"title_id": title_id, "version": None, "tags": []}
    rows = await conn.fetch(_LOW_EVIDENCE, title_id, version)
    return {"title_id": title_id, "version": version, "tags": [dict(row) for row in rows]}
