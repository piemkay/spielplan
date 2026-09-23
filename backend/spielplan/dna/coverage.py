"""§8.4's thin-facet measurement: how much of the vocabulary one title's sources actually named.

Spec v2.1 §8.4 (`spec:408`), §14 risk 2 (`spec:498`), §14 risk 7 (`spec:503`), §4.1 rules 1 and 2
(`spec:93`), §3.1 (`spec:74`); decisions 329 and 390.

§8.4 feeds the extraction flywheel from four kinds of naming failure, and the fourth is
"thin-facet titles". Nothing in the spec says how thin is thin: §14 risk 2 asks for a measurement
and not a predicate -- "measure facet coverage of post-2025 titles" -- and no threshold appears
anywhere in the document. Decision 390 therefore lands the measurement here and leaves the
threshold to decision 329, which belongs to the milestone that builds the queue spending it: the
number decides which titles LLM money is spent on, so it is the owner's to set against a cost
estimate, not a default this lane can pick while nobody is looking. So this module answers in
counts. It returns no boolean, takes no `n`, and names no number at all, and
`test_dna_coverage.py` asserts that over this file's own source rather than trusting the next
editor to remember why the number is missing.

**FOUR DIFFERENT "THIN"S ARE IN PLAY, AND THIS IS THE FOURTH.** They are not the same test and
they do not answer about the same thing:

  * `placement/features.py:90-109`'s `BuiltVector.is_thin` -- a per-BLOCK test over one built
    feature vector: at least one block dropped, or one that hit none of its declared columns,
    with `UNENRICHABLE_BLOCKS` excluded. It decides §5.3's badge and whether reconciliation parks
    an acquisition job, and it is about the tower's input rather than about the vocabulary.
  * §5.3's census of what a fresh bundle ships thin (`spec:237`): "2 lack keywords, 3 lack any
    DNA row". A count of titles, not a test a title can be put to.
  * §4.1's forward reference to "§8's thinness test" (`spec:93`), which §8 never defines. The
    clause it sits in is about the genome BLOCK, so decision 329 re-points that sentence at
    `features.is_thin`; nothing in this module answers it.
  * §8.4's per-facet coverage -- this one, and only this one.

Which is why nothing here is named `is_thin` and nothing here returns a boolean. That name is
taken, by the first of the four, for a different question about a different subject; a second
`is_thin` in the tree is how two of the four quietly become one, and that conflation is precisely
what decision 329 exists to prevent. The same argument the sibling module makes about a second
`norm()` applies to a second thinness test: neither failure crashes, both just make two pieces of
code disagree about what they are measuring.

**THE FACET SET COMES FROM THE TABLE, NOT FROM A TUPLE IN THIS FILE.** `dna_facet` names the
eleven v1 facets once, per vocabulary version, and a hard-coded list here would be a twelfth
naming of them. The app's facet ids are the term prefixes (`mood`, `themes`, `characters`) while
the corpus files its extraction passes under different labels (`mood_tone`, `narrative_themes`,
`character_dynamics`), and the two namings once differed on 29,188 of 31,540 `dna_tag` rows and
206,151 of 223,136 `dna_projected` rows -- every one of them a label joining `dna_facet` nowhere
[M4.9 finding 1]. That is settled in two places already: `importer/dna.app_facet` derives the
facet at load, and `0018_read_layer.sql:41-45` rewrote the rows of every install seeded before it.
So this counts the stored `facet` column rather than re-deriving the prefix in a third place; a
third derivation is another chance for two of them to disagree, and 0018's own comment records
why the derivation is not as simple as it looks (`split_part` answers the whole string for an
undotted term, which is why that UPDATE carries a `LIKE` guard and why no CHECK constraint pins
the column).

**The corpus's `coverage()` is deliberately not ported.** `mdc/dna/store.py:229-241` returns
`min(1.0, n / 3)` per facet -- three tags treated as full coverage -- and labels itself a proxy in
its own docstring, noting that similarity does not threshold on it "because a genuine one-tag
pacing facet carries real signal that a threshold would throw away". That ratio is a threshold
wearing a ratio's clothes: the `/ 3` decides what "enough" means, which is exactly the decision
329 the owner has not taken. Its honesty is worth keeping and its number is not, so the number
stays out and the counts it was computed from are what this returns. Nothing in this app calls it
either, and a helper written for a caller that does not exist is the speculative abstraction the
house rules refuse.
"""

from __future__ import annotations

import asyncpg

from spielplan.db import dna_terms


async def facet_coverage(
    conn: asyncpg.Connection, title_id: int, *, version: str | None = None
) -> dict[str, int]:
    """How many distinct extracted-tier terms one title carries in each declared facet.

    **Every declared facet is a key, including the ones that count zero.** §8.4's question is
    which facets the sources said nothing about, and an absent key answers a different question
    from a zero: absent says the vocabulary has no such facet, zero says nobody named this title
    under one it has. The flywheel only wants the second, and a mapping that omitted the zeroes
    would hand it the one row shape it cannot read. The order is `dna_facet.ord`, so a caller
    gets one reproducible sequence rather than whatever the join returned first -- and on every
    install this app builds that is ALPHABETICAL, because the loader writes `ord` from
    `sorted(facet_names)` (`importer/dna.py:154`). No spec clause fixes an order over the
    eleven: §6.8 fixes a colour per facet and nothing else, and this paragraph once cited it for
    a sequence it does not contain. [M5.4 review cycle 3, M54-DIM5-02]

    **A TERM IS COUNTED ONCE HOWEVER MANY PROVIDERS NAMED IT.** `dna_tag`'s key is `(title_id,
    version, term, provider)` (`0004_dna.sql:84`), which is §6.6's parallel mode: three providers
    agreeing on one mood are three rows, while every imported bundle tag carries one provider. A
    count of rows therefore reported a title three providers unanimously tagged with one mood as
    `mood: 3`, the same number as a bundle title carrying three distinct moods, and §14 risk 2's
    comparison of post-2025 titles against the bundle read an inflated population against one
    that was not -- ranking the unanimously thin title as the better covered. Three providers
    agreeing named one piece of the vocabulary once. [M5.4 review cycle 3, M54-DIM5-01]

    **Extracted tier only, read through the sanctioned view.** §4.1 rule 1 keeps the two tiers
    separate, and `dna_tagged` (`0004_dna.sql:120-128`) is the one place they may be named
    together precisely because it carries the `tier` discriminator through -- so the tier is
    named in the join and the two tables never are. Projected rows are excluded because they are
    inferred from the title's own keywords rather than from anything a source said about it:
    counting them would report coverage the extraction never produced and hide from the flywheel
    the titles it exists to find. `salience`, `confidence` and `n_sources` are not read at all --
    this is a count, and the moment a weight decides which rows are counted it is the §4.1 rule 2
    cut that deletes 44% of the extracted tier.

    **One vocabulary version per answer.** `version` defaults to `db/dna_terms.active_version`,
    the single derivation of which vocabulary is live, and §14 risk 7 makes that binding: "every
    read is scoped to one version". A caller that already resolved a version passes it, so one
    request cannot straddle two. An install carrying no vocabulary at all gets an empty mapping
    rather than an exception -- §3.1 makes a bundle-less app a legal state, `active_version`
    already answers None there, and a measurement that raised on it would turn first boot into an
    error report about a flywheel nobody has fed yet.
    """
    scoped = await dna_terms.active_version(conn) if version is None else version
    if scoped is None:
        return {}

    # The declared facets are the left side of the join, which is what makes an unnamed facet a
    # zero rather than a missing row -- doing it the other way round and filling the gaps in
    # Python would put the vocabulary's facet list in two places again.
    rows = await conn.fetch(
        """
        SELECT f.facet, count(DISTINCT d.term) AS n
          FROM dna_facet f
          LEFT JOIN dna_tagged d
                 ON d.version = f.version
                AND d.facet = f.facet
                AND d.title_id = $2
                AND d.tier = 'extracted'
         WHERE f.version = $1
         GROUP BY f.facet, f.ord
         ORDER BY f.ord
        """,
        scoped, title_id,
    )
    return {row["facet"]: int(row["n"]) for row in rows}
