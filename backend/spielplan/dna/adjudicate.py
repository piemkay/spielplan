"""The curation ledger the trust boundary consults. Spec v2.1 §8 stage 3, §14.5, §14 risk 5.

A tag can be quote-verified and still be wrong: the quote is a metaphor, or praise, or describes
the novel rather than the film, or names a production fact.  Verification is mechanical and cannot
see any of that, so those calls are **adjudicated by hand and written down** — the same principle
the S matrix follows, for the same reason.

Until 2026-08-25 those verdicts were applied as one-off database surgery
(`scripts/dna_vocab/repair/apply_*.py`).  That put them outside the derivation: `mdc dna ingest`
rebuilds the extracted tier from the result files every run, so every ingest silently reverted
them — 787 rows and half the register facet, twice in one afternoon, before the cause was found.
Moving them here restores the invariant the pipeline is supposed to have:

    dna_tag = f(result files, vocabulary, adjudications)

with all three versioned and none of them a mutation nobody can replay.

**The ledger never invents a tag.**  Every action removes or re-points evidence that an extractor
produced and verification accepted; nothing is created here, so a tag still traces to a verbatim
quote in its own title's pack.

Order matters and mirrors the scripts this replaces: evidence drops, then per-title verdicts, then
blanket term rules as a sweep over what the per-title rows did not name.  A per-title verdict
therefore always beats the blanket rule for its term, which is what lets the audit keep 29
`central_couple` rows while dropping 92.

Everything above this line is `mdc/dna/adjudication.py`'s own module docstring, ported and
re-wrapped to 108 columns with nothing added or dropped; its subject is the corpus project, where
the scar was measured. The argument binds here too and not only there: §8 stage 3 carries the same
sentence into this app -- "a derive that regenerates rows without re-applying them silently
reverts curated fixes (§14.5)" (`spec:378`) -- and §14 risk 5 (`spec:501`) names the scar by its
row count among the invariants a per-title derive must port "or inherit the bugs they were built
to kill".

**THIS FILE IS THE READER AND NOT THE WRITER.** `importer/dna.load_adjudications` (`:223-329`)
fills `dna_adjudication` from the bundle's `adjudications_<version>.tsv`, and decision 247 puts it
on the path a models-only re-import takes, so the ledger this reads is replaced wholesale at every
import. That is why nothing here is cached the way the corpus caches its parsed TSV per version
(`mdc/dna/adjudication.py:66,99`): the corpus reads a file that cannot change under a running
ingest, while this table is DELETEd and re-INSERTed inside an import transaction
(`importer/dna.py:317-323`), and a process-lifetime cache would go on serving verdicts the
household has already retracted. One query per call instead, over the `(version, term)` index
`0015_seed.sql:165` added for it -- and the verifier reaches this module only for a term the
vocabulary does not carry (`mdc/dna/store.py:177-199`), so the call count is the count of terms
the vocabulary did not know and not the count of tags.

**THE VERDICT VOCABULARY IS READ ACROSS BOTH SPELLINGS, CASE-FOLDED (decision 389).** This is the
port's one real change, and the reason is that two projects wrote two vocabularies into one
column. `0004_dna.sql:138` documents `keep | rename | drop | merge`; `mdc/dna/adjudication.py`
writes `REPOINT`, `DROP` and `DROP_EVIDENCE`; `importer/dna.py:276` stores the TSV's `action`
column verbatim with no CHECK to make either spelling wrong, and the fixture bundle writes
lowercase `drop` and `rename` (`tests/fixtures/make_bundle.py:873-877`). Measured on the shipped
ledger -- `data/dna_vocab/v1/adjudications_v1.tsv`, 828 rows -- the file carries 467 `DROP`, 354
`REPOINT` and 7 `DROP_EVIDENCE`, and not one `KEEP` or `MERGE`: the app's documented vocabulary
and the corpus's shipped one overlap in no single string. A reader that knows one of them treats
the other as "no verdict", which turns a curated retirement into `unknown_term` -- exactly the
confusion `is_retired` exists to prevent, because it makes a routine retirement look like an
extractor emitting garbage and hides the case that actually needs attention.

So both are read and neither is guessed at. A verdict string this reader does not know is ignored
and COUNTED: `unknown_verdicts` below is that count, and it is a count over the LEDGER rather than
a counter incremented as tags arrive, because the number decision 389 wants visible is "the ledger
holds verdicts nobody has taught this reader" -- which is true of a run in which no tag happened
to name those terms, and a hot-path counter would report zero for it.

**THE PER-TITLE ARM IS REAL IN THIS APP.** `0015_seed.sql:159-166` dropped `dna_adjudication`'s
`(version, term)` primary key and added `id`, `scope`, `title_id`, `quote` and `source` precisely
so the 817 title-scoped verdicts survive the round trip §6.6's editor makes. Applicability is
decided by `title_id` and never by `scope`, because `scope` is the third column the two projects
spell differently: the shipped ledger writes `term` for its 11 blanket rows (every one of them
with an empty `title_id`), the app's loader defaults the column to `global` (`importer/dna.py:274`)
and refuses a `title` row that carries no id (`:282-287`). `title_id` carries one meaning in both
projects -- a row with an id speaks for that title, a row without one speaks for every title --
and reading `scope` instead would make a `global` row that does carry an id apply to every title,
which is not what `mdc/dna/adjudication.load_ledger:96-99` does with the same row.

**EVERY READ IS SCOPED TO ONE VERSION, AND THE CALLER SUPPLIES IT.** §14 risk 7 (`spec:503`) is
binding -- "every read is scoped to one version" -- and `dna_adjudication.version` is a foreign key
to `dna_vocabulary`, so a literal default here would do to reads what `load_adjudications`'
docstring says a literal default would do to writes: "file 828 curated verdicts under a vocabulary
the install may not be on". `version` is therefore a required keyword argument with no default, the
shape `db/library.dna_for` already uses for the same reason (`db/library.py:472-478`): the caller
resolves it once through `db/dna_terms.active_version` and hands the same string to every tag of
one title, so one verification cannot mix two vocabularies. `None` is the pre-import state and not
an error -- there is then no ledger to consult, and both functions answer as an empty ledger does.

**NOTHING HERE FOLDS A TERM OR A QUOTE.** Terms are matched exactly, in SQL, as the corpus matches
them in Python. The one case-fold in this file is `_verdict_key`, over a five-value verdict enum;
it is not `norm()` (the pack/quote fold, which lives alone in `dna/norm.py` and must stay there)
and not the alias map's `alias_key()`, it never sees a term or a quote, and it is deliberately
named for neither.
"""

from __future__ import annotations

import asyncpg

# Decision 389's two vocabularies, folded into one set each. `rename` is the column comment's
# spelling (`0004_dna.sql:138`) and `repoint` the corpus's (`mdc/dna/adjudication.py:48`).
#
# `merge` re-points as well, and that is a decision rather than a synonym: a merge and a re-point
# name the same thing to a trust boundary -- this id is now that id -- and they differ only in
# what the WRITER does when the target is already present on the title, which is to keep the
# higher salience and union the evidence (`mdc/dna/adjudication._merge_into:103-121`). That work
# needs the title's other tags in hand and belongs to whatever writes `dna_tag`; a verifier
# deciding whether a term is admissible has one tag and no reason to care.
REPOINT_VERDICTS = frozenset({"rename", "repoint", "merge"})

# The one verdict that retires a term outright rather than re-pointing it. 467 of the shipped
# ledger's 828 rows, so this is the ordinary case and not the exotic one.
RETIRE_VERDICTS = frozenset({"drop"})

# A verdict that changes nothing by itself and still answers for its title. `keep` appears only in
# the app's column comment -- the shipped ledger has none -- and the only reason to write one is to
# except a title from a blanket rule, so it must reach the per-title arm and stop the sweep there.
# Silently letting the blanket rule apply anyway would make the row unwritable.
KEEP_VERDICTS = frozenset({"keep"})

# Evidence-level, and this reader does not act on it (decision 389). `DROP_EVIDENCE` says one quote
# was false, not that the term was wrong: the corpus's own `apply` drops that evidence row and
# leaves the tag standing when other evidence survives. A boundary asked "is this term admissible
# on this title" has nothing to read in it.
EVIDENCE_VERDICTS = frozenset({"drop_evidence"})

# Verdicts about the TERM. The first of these on a title is that title's verdict and ends the
# search, which is how a per-title row beats the blanket rule.
TERM_VERDICTS = REPOINT_VERDICTS | RETIRE_VERDICTS | KEEP_VERDICTS

# Everything this reader has been taught. What falls outside it is reported by `unknown_verdicts`
# and acted on nowhere, so a ledger that grows a fifth verdict is visible in that count and changes
# no tag until somebody teaches this module what it means (decision 389).
KNOWN_VERDICTS = TERM_VERDICTS | EVIDENCE_VERDICTS

# One statement for both functions, and one round trip for both arms of the rule: this title's
# rows and the blanket rows come back together, ordered so that the per-title arm is simply the
# front of the list. `title_id = $3` is false for every row when `$3` is NULL, which is the
# blanket-only read the corpus gets by passing `title_id=None`.
#
# `ORDER BY title_id IS NULL` sorts false before true, so this title's verdicts precede the
# blanket sweep; `id` then holds file order within each arm, because `id` is the `bigserial`
# `0015_seed.sql:160` added and `load_adjudications` inserts the parsed rows in the order the TSV
# wrote them (`importer/dna.py:320-323`). The corpus reads its two arms out of two lists built in
# file order, so file order is what "first rule wins" means there, and this is that order.
_LEDGER_FOR_TERM = """
    SELECT title_id, verdict, target
      FROM dna_adjudication
     WHERE version = $1 AND term = $2 AND (title_id = $3 OR title_id IS NULL)
     ORDER BY title_id IS NULL, id
"""


def _verdict_key(verdict: object) -> str:
    """One verdict string as a comparable key: stripped and case-folded, nothing else.

    Not a normalisation of anything a reader would recognise as text -- see the module docstring's
    last paragraph. `str()` rather than a cast because `verdict` is `NOT NULL` in the table and a
    NULL still arrives from a hand-written test row or a future outer join, and `None` must key to
    "" rather than to "none", which is a string somebody could plausibly file as a verdict.
    """
    return str(verdict or "").strip().lower()


async def _verdicts_for(
    conn: asyncpg.Connection, term: str, title_id: int | None, version: str
) -> list[asyncpg.Record]:
    """This title's verdicts on one term, then the blanket ones, in the order they were written."""
    return await conn.fetch(_LEDGER_FOR_TERM, version, term, title_id)


async def rename(
    conn: asyncpg.Connection, term: str, title_id: int | None = None, *, version: str | None
) -> str | None:
    """The term a retired id was re-pointed to, or None.

    Consulted by the verifier when the vocabulary does not know an id, so a result file written
    before a merge still ingests.  A per-title verdict wins over the blanket rule, and a term the
    ledger DROPS rather than re-points returns None — it is genuinely gone, and the row should be
    rejected rather than quietly kept.

    Ported from `mdc/dna/adjudication.rename` (`:212-234`); the paragraph above is its docstring
    with one change -- the corpus names `mdc.dna.store.verify_payload` where this says "the
    verifier", because that function is M5.5's and does not exist yet. The two arms stay two
    loops rather than one pass for
    the reason the corpus wrote them that way: in the per-title arm a `drop` is terminal, and in
    the blanket sweep it is not, so a term carrying both a blanket re-point and a blanket
    retirement re-points whichever order they were filed in. Answering None there instead would
    read as a difference in this function and show up as one in `dna_reject.rule_violated`.

    **One named change, and it is decision 389's -- the one `is_retired` below already carries.**
    The corpus's per-title arm knows only a re-point and a retirement, so a `keep` fell through
    to the blanket sweep and the title it excepted was re-pointed anyway, which is the row
    `KEEP_VERDICTS`' own comment calls unwritable; and a re-point naming no target fell through
    the same way, while `is_retired` let both end the search. Two readers of one ledger that
    disagree about which per-title row is a title's verdict answer two questions under one name.
    So the first per-title verdict about the TERM ends the search here too, answering its target
    when it re-points to one and None otherwise; `drop_evidence` and a verdict nobody taught this
    reader still fall through, for `is_retired`'s reason. [M5.4 review cycle 3, M54-DIM3-C3-03]
    """
    if not version:
        return None
    rows = await _verdicts_for(conn, term, title_id, version)

    for row in rows:
        if row["title_id"] is None:
            continue
        verdict = _verdict_key(row["verdict"])
        if verdict in TERM_VERDICTS:
            target = (row["target"] or "").strip()
            return target if verdict in REPOINT_VERDICTS and target else None

    for row in rows:
        if row["title_id"] is not None:
            continue
        verdict = _verdict_key(row["verdict"])
        target = (row["target"] or "").strip()
        if verdict in REPOINT_VERDICTS and target:
            return target
    return None


async def is_retired(
    conn: asyncpg.Connection, term: str, title_id: int | None = None, *, version: str | None
) -> bool:
    """True when the ledger drops this term outright rather than re-pointing it.

    Lets verification separate "the owner retired this on the evidence" from "the extractor
    emitted something that is not a term" — they are the same rejection mechanically and
    completely different signals to read.  Counting the two together makes a routine retirement
    look like an extractor emitting garbage, and hides the case that actually needs attention.

    Ported from `mdc/dna/adjudication.is_retired` (`:237-252`), whose docstring is the first
    paragraph; the second is `mdc/dna/store.py:189-193`, the comment at the call site that says
    what the split buys, brought here because in this app the call site is M5.5's file and a
    function whose whole purpose lives in another milestone's comment is a function somebody
    deletes.

    **One named change, and it is decision 389's.** The corpus's per-title arm lets the first row
    for the term decide whatever its action is; this one lets the first row decide whose verdict is
    about the TERM. A `drop_evidence` row is about one quote (`EVIDENCE_VERDICTS`) and a verdict
    this reader has never been taught is not a verdict at all, so neither may answer False and stop
    -- that would let an evidence note or an unrecognised string silently overrule a blanket
    retirement and relabel it `unknown_term`, which is the confusion the paragraph above says this
    function exists to prevent. The shipped ledger cannot tell the two readings apart today: its 7
    `DROP_EVIDENCE` rows name seven terms and none of those terms carries a blanket rule, so the
    difference is latent and would first appear the day an audit writes one.
    """
    if not version:
        return False
    rows = await _verdicts_for(conn, term, title_id, version)

    for row in rows:
        if row["title_id"] is None:
            continue
        verdict = _verdict_key(row["verdict"])
        if verdict in TERM_VERDICTS:
            return verdict in RETIRE_VERDICTS

    return any(
        _verdict_key(row["verdict"]) in RETIRE_VERDICTS
        for row in rows if row["title_id"] is None
    )


async def unknown_verdicts(conn: asyncpg.Connection, *, version: str | None) -> dict[str, int]:
    """The verdict spellings this reader has not been taught, with the rows each one holds.

    Decision 389's other half: an unknown verdict is ignored and counted, never guessed at, so
    that a ledger which grows a fifth verdict is visible rather than silently inert. Keyed on the
    stored spelling and not on the folded key, because the point of the number is to send a reader
    to the rows -- a count filed under `drop_evidnece` is findable and one filed under a tidied
    version of it is not.

    A query over the whole ledger rather than a tally of what this run happened to look up: the
    two answer different questions, and the one worth publishing is about the ledger. It is not
    called on the verification path and is not meant to be -- a run reports it once, beside the
    reject counts §6.6's board reads.
    """
    if not version:
        return {}
    rows = await conn.fetch(
        "SELECT verdict, count(*) AS n FROM dna_adjudication WHERE version = $1 GROUP BY verdict",
        version,
    )
    return {
        row["verdict"]: int(row["n"])
        for row in rows if _verdict_key(row["verdict"]) not in KNOWN_VERDICTS
    }


__all__ = ["is_retired", "rename", "unknown_verdicts"]
