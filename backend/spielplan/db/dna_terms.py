"""The read layer's two DNA constants: how loudly a term speaks, and which vocabulary is live.

Spec v2.1 §4.1 rules 1 and 2, §4.3, §6.2 steps 4-5, §6.8, §10; decision 188.

Both fragments below existed twice before this module did, and both copies had drifted from the
rows the corpus actually ships. That is the argument for the file: they are read-layer SQL over
the `dna_tagged` view, so neither Home nor Tonight owns them, and importing one domain package
from the other for a shared constant is worse coupling than a shared module underneath both.

**THE TERM WEIGHT.** §4.1 calls the extracted tier quote-verified and the projected tier
inferred, so extracted speaks louder while both stay fully admissible. The shipped expression
said `0.30 * COALESCE(d.confidence, 0.5)` and its comment claimed the ranges 0.73..1.00 and
0.00..0.30 — but `importer/dna.py` stores the projection's `n_sources` (1..8) in
`dna_projected.weight` and `0004_dna.sql:127-128` re-exposes that column to this view as
`confidence`, so the projected branch actually ran to 2.40 and outranked every extracted tag.
Measured on 400 random tagged movie titles: 37 of the 48 that carry an extracted term had a
projection as their loudest, and that is the term the winner card's match line and Home's
why-line name first. [M4.9 finding 20]

Decision 188 keeps `n_sources` a weight rather than re-scaling it at import (which would need a
re-import that decision 162 does not provide) and bounds it here instead, saturating rather than
clamped: `0.30 * (c / (1 + c))` is monotone in `c`, so a projection supported by six sources
still outranks one supported by two — a clamp at 0.30 would have flattened every count from four
upwards into one number and thrown away the ordering the column exists to carry. The ranges the
form actually produces: extracted 0.733..1.00 for salience 1..3, projected 0.15..0.267 for
`n_sources` 1..8, and 0.10 for the NULL default. The two bands cannot cross, which is what
`test_library_read.py::test_no_projected_term_outweighs_any_extracted_term` asserts over an
imported bundle rather than over this arithmetic.

The `GREATEST(..., 0.0)` is the precondition that sentence needs, written into the SQL rather
than assumed of the producer. `c / (1 + c)` is monotone and bounded by 1 only for `c >= 0`: it
is undefined at `c = -1` and runs to 6 for `c` just above it, so a single projected row with a
negative `n_sources` would either 500 every Home shelf build and every Tonight `vectors_for`
through the `dna_tagged` scan, or make an inferred term the loudest on its title at 1.80 against
an extracted cap of 1.00 -- which is finding 20 reopened through a path the linear form could
not reach. Nothing bounds the input: `0004_dna.sql:110` is `weight real` with no CHECK, 0018
adds none, and `importer/dna.load_projected` copies the bundle's `n_sources` straight through
while `importer/validate.py` range-checks only `dna_tag.salience`. §8 stage 7 makes the bundle a
trust boundary, so the read layer does not assume a count is non-negative. Clamping the INPUT at
its own floor is not a §4.1 rule 2 filter: no row is admitted or refused by it, and the
saturating shape above 0 is untouched. [M4.9 review cycle 1: M49-D188-03]

**§4.1 RULE 2.** `salience`, `confidence` and `n_sources` appear here in arithmetic and in no
comparison anywhere — not in a WHERE, not in a HAVING, not in a truncating ORDER BY. They decide
how loudly a term speaks; they never decide which term or which title is admitted, which is what
makes the 0.5 cut that would delete 44% of the extracted tier unrepresentable rather than merely
discouraged. `test_landmine_guards.py` reads this file with the rest of the package.

**THE ACTIVE VOCABULARY.** §4.3 ships `dna_vocab/<version>/` and §10 warns that a bundle
re-import leaves two vocabularies coexisting; `home/why.py` already scoped the shelves to the
newest row because "a household that has imported two bundles has two versions and the shelves
must not mix them", while the title card, the catalog/Rank DNA predicate and §6.4's wander
neighbours carried no version predicate at all. [M4.9 finding 10]

It is written twice, as a scalar subquery and as a coroutine, from one string — because the
catalog's WHERE builder (`db/library._filters`) is synchronous and shared with the Rank board,
so there is no point in it at which a version could be awaited and threaded in. Two spellings of
one statement is not the second notion of "the active vocabulary" the plan forbids; two
statements would be.
"""

from __future__ import annotations

import asyncpg

# Read by `home/why.py` (as TERM_RANK) and `tonight/dna.py`, and by nothing else. Both bind the
# `dna_tagged` view to `d`, which is why the fragment can name `d.tier` outright.
TERM_WEIGHT = """
        CASE d.tier
            WHEN 'extracted' THEN 0.60 + 0.40 * (COALESCE(d.salience, 1.0) / 3.0)
            ELSE 0.30 * (GREATEST(COALESCE(d.confidence, 0.5), 0.0)
                         / (1.0 + GREATEST(COALESCE(d.confidence, 0.5), 0.0)))
        END
"""

# The newest imported vocabulary, as a scalar subquery, ordered on both columns and on neither
# alone. Not `version DESC` alone, because version strings sort lexicographically and not in
# import order (`v9` sorts above `v20260828`). Not `imported_at DESC` alone, because
# `imported_at` defaults to `now()`, which is transaction-stable: two DIFFERENT versions written
# inside one transaction -- an import that ships two `dna_vocab/<version>/` directories, or a
# restore that replays the table -- carry the same timestamp to the microsecond, and the shelves,
# the card and the catalog filter could then resolve different rows inside one request.
# `version` is `text PRIMARY KEY` (0004_dna.sql:18) and `importer/dna.py:134` upserts on it, so
# two rows sharing a version string is the one tie that CANNOT arise -- which is why the
# tie-break is on the column that can. [M4.9 review cycle 1: M49-REV1-04]
ACTIVE_VERSION = """
        (SELECT v.version FROM dna_vocabulary v ORDER BY v.imported_at DESC, v.version DESC
          LIMIT 1)
"""


async def active_version(conn: asyncpg.Connection) -> str | None:
    """The vocabulary every DNA read is scoped to, or None when no bundle has been imported.

    None is the M0 state and the pre-seed state, not an error: a caller that gets it has nothing
    to scope to, and the SQL form above answers NULL in the same situation.
    """
    return await conn.fetchval(f"SELECT {ACTIVE_VERSION}")


__all__ = ["ACTIVE_VERSION", "TERM_WEIGHT", "active_version"]
