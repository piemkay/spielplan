"""The two curated ledgers §8 stage 3 ends with: DNA verdicts at ingest, credit facts last.

Spec v2.1 §8 stage 3 -- the derive "ends by applying BOTH curated ledgers ... exactly as the corpus
rebuild runs corrections last: a derive that regenerates rows without re-applying them silently
reverts curated fixes (§14.5)" (`spec:369-374`) -- and §14 risk 5, which names the scar the rule is
made of: "adjudication- AND corrections-at-derivation - two distinct ledgers: DNA verdicts at
ingest, source-credit facts at rebuild - or inherit the bugs they were built to kill (the
787-rows-reverted-twice scar)" (`spec:489`). `0004_dna.sql:131-133` already carries the sentence as
a comment over the two tables. Decisions 326, 334, 375, 376.

TWO LEDGERS, TWO CALLS, TWO POINTS -- AND THAT IS THE REQUIREMENT AND NOT AN ARRANGEMENT. The
ledgers answer different questions about different tables at different moments: an adjudication
rules over `dna_tag` rows that already exist when the derive starts, and a correction rules over
`credit` rows THIS derive has just regenerated. A single pass at the end would still leave the
corrected credit in place afterwards and would pass any test that asks only "is the correction
present" -- which is exactly why `spec_coverage.toml`'s gate row spends its last clause on "the two
ledgers are applied at their own points rather than merged into one pass". The order is what the
scar is about: `mdc/corrections.py`'s own header records that its rule was learned "the expensive
way" one layer over, and merging the two calls is the shape that re-learns it.

WHY THIS FILE HOLDS BOTH AND `rebuild.py` HOLDS NEITHER. The appliers are the only writers in the
derive that do not read the raw store, and they must stay callable on their own: §6.6's three ledger
editors (M5.6) re-apply a ledger the household just edited without re-deriving anything, and M5.4's
stage 8 writes `dna_tag` rows that have to be adjudicated at their own ingest rather than at the next
derive. A ledger applier inlined into `derive_title` would be re-implemented by both.

PORT VERDICTS, unit by unit.

  * `mdc/dna/adjudication.py::apply` (`:126-190`) -- **ported with three named changes.** The order
    is its order and is the requirement (`:22-28`): "evidence drops, then per-title verdicts, then
    blanket term rules as a sweep over what the per-title rows did not name. A per-title verdict
    therefore always beats the blanket rule for its term, which is what lets the audit keep 29
    `central_couple` rows while dropping 92."
  * `mdc/dna/adjudication.py::_merge_into` (`:103-123`) -- **ported with one named change**, the
    provider scope below. Keeps the higher salience and unions the evidence, "exactly as
    `apply_db_repair.repoint_tag` did - a re-pointed row is the same reading of the same film, so
    its quote belongs to the surviving tag".
  * `mdc/dna/adjudication.py`'s `load_ledger`, `Rule`, `Ledger`, `_cache`, `stats`, `rename` and
    `is_retired` (`:38-100`, `:193-252`) -- **not ported.** They read and cache a TSV; this app's
    ledger is a table `importer/dna.load_adjudications` already fills, and the three lookup helpers
    serve the corpus's own verification pass, which is M5.4's stage 7 and not stage 3's business.
  * `mdc/corrections.py::apply` (`:78-137`) -- **ported with three named changes** (the evidence
    refusal moves, `role_class`, and the per-title scope).
  * `mdc/corrections.py::load` and `ledger_path` (`:52-69`) -- **not ported**, same reason:
    `importer/dna.parse_corrections` reads the file and `credit_correction` holds the result.

NAMED CHANGE 1: THE VERDICT VOCABULARY IS TWO VOCABULARIES AND BOTH ARE REAL. `0004_dna.sql:137`
declares `verdict` as "keep | rename | drop | merge" and decision 376 maps those four; the ledger
the corpus actually ships spells its column `action` and fills it with `DROP`, `REPOINT` and
`DROP_EVIDENCE` (829 rows, 467/354/7), and `importer/dna.py:286` stores that string VERBATIM -- so a
real install's `dna_adjudication.verdict` holds the corpus's three and a fixture's holds the app's
four. There is no CHECK constraint to make either wrong. `_VERDICTS` below folds both onto one set of
actions rather than choosing a winner, because an applier that understood only one of them would
silently do nothing on precisely the install the exit criterion runs against, and "nothing" is what
§14.5's scar looks like from the outside. Decision 376's own title says "how the app's four verdicts
map onto the corpus's three actions", and this is that map, made total.

NAMED CHANGE 2: A MERGE IS SCOPED TO ONE PROVIDER. The corpus merges onto any tag carrying the target
term, because its tags are one list per title and it has no provider at all. This schema does:
`dna_tag` is `UNIQUE (title_id, version, term, provider)` (`0004_dna.sql:84`) precisely so §6.6's
parallel mode can hold two providers' reading of one film side by side, and a merge across providers
would collapse the distinction the column exists for -- one row where two extractions disagreed, with
the higher salience and no record that anything was folded. `0018_read_layer.sql:68-70` is what makes
that key real (it collapses NULL to `''` and sets NOT NULL, "so the semantics of 'no provider
recorded' stay one value instead of two"), so a bare `=` would do; the survivor is looked up with
`provider IS NOT DISTINCT FROM` because that is the comparison the unique index itself makes, and the
merge and the constraint deciding "same row" differently is the one way this can raise in production.

NAMED CHANGE 3: THE EVIDENCE REFUSAL MOVES FROM THE LOADER TO THE APPLIER. `mdc/corrections.py:64-66`
drops an evidence-less row while READING the file -- "a correction without evidence is an opinion" --
and this app cannot: §6.6 has the ledger editor writing `corrections_v1.tsv` back out, so a row the
import dropped would vanish from the file the household edits, and `0015_seed.sql:169-176` added the
`evidence` column for exactly that round trip ("dropping the column on import would mean exporting a
ledger whose claims can no longer be checked"). The row is therefore stored and refused HERE, where
the refusal costs nothing and is counted.

NAMED CHANGE 4: THE INSERTED CREDIT CARRIES THIS APP'S `role_class`. The corpus writes
`role_class = 'crew'`; this schema's vocabulary is closed and does not contain that word
(`0015_seed.sql:50-53`: "director|writer|dp|composer|editor|prod_designer|cast ... what the feature
contract's `p:<role_class>:<name>` grammar is built from"). A corrected composer credited as `crew`
would be a credit §3.1's feature contract cannot name -- the correction applied and the card still
wrong. The class is read from `derive/ids.classify_role`, the same call every parsed credit goes
through, rather than written as a literal, so the corrected row and the row it replaces are classed
by one rule.

NAMED CHANGE 5: BOTH APPLIERS TAKE ONE TITLE. The corpus walks its whole ledger because its unit is
a wholesale rebuild; §8's unit is one title, and decision 375 makes the derive's every write scoped
to it. `apply_corrections` therefore selects `WHERE title_id = $1` and the corpus's `unknown_title`
branch survives as a guard on the title rather than as a per-row lookup: a ledger row naming a title
this install has never acquired is simply never selected, which is what `credit_correction.title_id`
carrying no foreign key is for (`0015_seed.sql:157-158`).

NEITHER APPLIER FILTERS ON `origin`. Decision 326 adds the column so the IMPORTER can tell which
rows its DELETE owns; a curated fix is a curated fix whoever authored it, and an applier that skipped
household rows would make the §6.6 editors write to a table nothing reads -- the same defect one
milestone earlier, with the household on the losing side of it.

THE LEDGER NEVER INVENTS A TAG. Every action below removes or re-points a row something else
produced, so a surviving tag still traces to a verbatim quote in its own title's pack (§4.1 rule 1).
The one place that could break it is a re-point onto a term the vocabulary does not know, and it is
refused and counted rather than written.
"""

from __future__ import annotations

import asyncpg

from spielplan.db import dna_terms
from spielplan.derive import ids

# The two vocabularies folded onto one set of actions. See NAMED CHANGE 1; `rename` and `merge` are
# decision 376's two spellings of the corpus's one `REPOINT`, and they differ only in what the owner
# meant, never in what happens to the row.
KEEP = "keep"
REPOINT = "repoint"
DROP = "drop"
DROP_EVIDENCE = "drop_evidence"

_VERDICTS = {
    "keep": KEEP,
    "rename": REPOINT,
    "merge": REPOINT,
    "repoint": REPOINT,
    "drop": DROP,
    "drop_evidence": DROP_EVIDENCE,
}

# The two kinds `corrections_v1.tsv` ships, which `importer/dna.py:703` stores in `field`: the
# ledger's `kind` column IS the app's `field` column (`0015_seed.sql:170-172`, "kind is the credit
# field, value the asserted truth"). A row carrying anything else is counted and passed over rather
# than guessed at -- `mdc/corrections.py:60-61` filters the same two at load time, and the day the
# ledger grows a third kind, applying it as a composer replace is worse than not applying it.
CORRECTION_KINDS = ("composer", "composer_add")

# What a corrected music credit looks like, which is `mdc/corrections.py:113-121` verbatim except
# for `role_class` (NAMED CHANGE 4). `source` is the provenance that keeps the row out of every
# scope decision 375's re-derive deletes by: no parsed document is ever labelled `correction`, so
# `rebuild._replace` cannot take this row back, and `apply_corrections` is the only writer that
# CAN remove it -- which for one milestone it could and did not. See `_reclaim`: a row the ledger
# has stopped asserting has to be taken back here or nowhere, because the corpus's answer to the
# same question is `mdc rebuild` dropping the derived layer wholesale, and decision 375 is exactly
# the line that stopped this app doing that. [M5.3 review cycle 2, m53-c2-scar-01]
CORRECTION_SOURCE = "correction"
_MUSIC_DEPARTMENT = "Sound"
_MUSIC_JOB = "Original Music Composer"

# The per-title half of the ledger and its blanket half, split the way `mdc/dna/adjudication.py:93`
# splits them: a row is per-title when it says so AND names a title, and everything else -- including
# the corpus's `scope = 'term'` and this table's `scope = 'global'` default -- is a blanket rule.
# `id` is the FILE's order, because `load_adjudications` inserts the parsed rows in one
# `executemany` in the order it read them, and the corpus's `by_term` list is ordered for the same
# reason: two blanket rules touching one term apply in the order the owner wrote them.
#
# `origin DESC` IS DECISION 423, AND IT SORTS THE HOUSEHOLD FIRST BECAUSE THIS APPLIER IS
# FIRST-WINS. `_rule` acts on `rule["term"]`, and `_repoint` and the DROP arm both move the tag off
# that term -- so a second rule naming it finds no row and does nothing. Household first is
# therefore household effective, which is what §6.6's "survives every future re-derive" means for a
# verdict the owner typed. The second thing this clause buys is idempotence, and it is why the
# ordering is a defect in THIS milestone rather than a question for M5.6: `id` is a bigserial and
# `importer/dna.py`'s `load_adjudications` DELETEs the bundle's rows and re-INSERTs them on every
# models-only import (decision 247), so a byte-identical reload renumbers every bundle row ABOVE
# every household row and flips which verdict takes effect -- a re-import of an unchanged file
# changing what the next derive produces. Decision 326 says "the appliers do NOT filter on origin",
# and this does not: every row is selected and applied, in an order that is now stable across an
# import instead of being whatever the sequence last handed out.
# [M5.3 review cycle 1, m53-c1-dim326-01; decision 423]
_TITLE_RULES = """
    SELECT term, verdict, target, quote FROM dna_adjudication
     WHERE version = $1 AND scope = 'title' AND title_id = $2
     ORDER BY origin DESC, id
"""
_BLANKET_RULES = """
    SELECT term, verdict, target, quote FROM dna_adjudication
     WHERE version = $1 AND NOT (scope = 'title' AND title_id IS NOT NULL)
     ORDER BY origin DESC, id
"""

_TAGS_FOR_TERM = "SELECT id, provider FROM dna_tag WHERE title_id = $1 AND version = $2" \
                 " AND term = $3 ORDER BY id"

_FACET_OF = "SELECT facet FROM dna_term WHERE version = $1 AND term = $2"

# `id <> $4` because a tag whose term is ALREADY the target is its own survivor otherwise, and the
# merge would delete the row it just kept.
_MERGE_TARGET = """
    SELECT id FROM dna_tag
     WHERE title_id = $1 AND version = $2 AND term = $3 AND id <> $4
       AND provider IS NOT DISTINCT FROM $5
     ORDER BY id LIMIT 1
"""

# GREATEST rather than a comparison in Python, and not only for the round trip: §4.1 rule 2 makes
# these two columns weights and never filters, and a Python `if src["salience"] > keep["salience"]`
# is the shape `test_landmine_guards.py::test_no_weight_column_is_compared_in_python` refuses. The
# COALESCE on `n_sources` is the corpus's `max(other.get("runs_found") or 1, ...)`: the column is
# nullable here and a NULL means "nobody counted", not "zero sources".
_TAG_MERGE = """
    UPDATE dna_tag AS keep
       SET salience = GREATEST(keep.salience, src.salience),
           n_sources = GREATEST(COALESCE(keep.n_sources, 1), COALESCE(src.n_sources, 1))
      FROM dna_tag AS src
     WHERE keep.id = $1 AND src.id = $2
"""

# The credits THIS applier minted for a title, which is the one set it may take back. Keyed on
# `source = 'correction'` rather than on the job text `_MUSIC_CREDITS` matches, because the
# question is not "is this a music credit" but "did this function write it": a source's own
# composer row is the thing a correction overrules, and reclaiming it would be the applier
# deleting evidence instead of its own bookkeeping. `<> ALL` over an EMPTY array is TRUE for every
# row, which is the withdrawn-ledger case and is the reason `_reclaim` runs before the early
# return above it. [M5.3 review cycle 2, m53-c2-scar-01]
_STRANDED_CREDITS = """
    SELECT c.id FROM credit c JOIN person p ON p.id = c.person_id
     WHERE c.title_id = $1 AND c.source = $2 AND p.name <> ALL($3::text[])
     ORDER BY c.id
"""

# The music credits a `composer` correction replaces, matched by `mdc/corrections.py:100-103`'s own
# predicate. It is deliberately wider than `role_class = 'composer'`: the ledger's six rows exist
# because upstream got the music credit wrong, and a source that filed one under "Music Supervisor"
# has produced exactly the row the correction is about -- `classify_role` returns None for it, so a
# class-based match would leave it standing beside the corrected name.
_MUSIC_CREDITS = """
    SELECT c.id, p.name FROM credit c JOIN person p ON p.id = c.person_id
     WHERE c.title_id = $1
       AND (lower(c.job) LIKE '%composer%' OR lower(c.job) LIKE '%music%')
     ORDER BY c.id
"""


def _bump(stats: dict[str, int], key: str) -> None:
    stats[key] = stats.get(key, 0) + 1


async def apply_adjudications(conn: asyncpg.Connection, title_id: int) -> dict[str, int]:
    """Rule the curated DNA ledger over this title's tags, at the derive's INGEST point.

    Decision 376 settles what it runs against and why that is not "whatever this run extracted":
    M5.3 writes no `dna_tag` rows at all, so an applier written against a fresh extraction would
    have nothing to run against and would be written twice. It rules over the rows the title
    already carries for the active vocabulary version, whatever wrote them -- the bundle importer
    today, M5.4's stage 8 tomorrow -- which is what makes stage 8 inherit a working applier instead
    of a second implementation of one.

    THE ACTIVE VERSION IS `db/dna_terms.active_version`'S ANSWER and not a second reading of
    `dna_vocabulary`. That module argues the ordering it takes ("not `version DESC` alone, because
    version strings sort lexicographically and not in import order"), and a ledger applied under one
    version while the shelves read another is a curated verdict the household cannot see the effect
    of. No vocabulary at all is the M0 state, not an error: there are no tags to rule over either.

    Returns counts by outcome, as `mdc/dna/adjudication.py::apply` does, so the caller can put them
    on §6.6's board rather than in a log nothing reads.
    """
    version = await dna_terms.active_version(conn)
    if version is None:
        return {}

    rules = [dict(row) for row in await conn.fetch(_TITLE_RULES, version, title_id)]
    blanket = [dict(row) for row in await conn.fetch(_BLANKET_RULES, version)]
    if not rules and not blanket:
        return {}
    stats: dict[str, int] = {"rules": len(rules) + len(blanket)}

    # 1. Evidence-level drops, before anything moves a term. The corpus runs this phase over the
    #    PER-TITLE rules only (`mdc/dna/adjudication.py:148-167` iterates `rules`, and the blanket
    #    sweep at `:180-190` handles two actions and not this one), and so does this: a quote is a
    #    fact about one title's pack, and a blanket rule naming one cannot mean anything.
    for rule in rules:
        if _VERDICTS.get((rule["verdict"] or "").strip().lower()) == DROP_EVIDENCE:
            await _drop_evidence(conn, title_id, version, rule, stats)

    # 2. Per-title verdicts. `handled` is what stops phase 3 touching a term the owner has already
    #    ruled on FOR THIS TITLE -- the property the corpus states as "29 `central_couple` rows kept
    #    while 92 are dropped", and the only reason a `keep` verdict has anything to do.
    handled: set[str] = set()
    for rule in rules:
        action = _VERDICTS.get((rule["verdict"] or "").strip().lower())
        if action == DROP_EVIDENCE:
            continue
        handled.add(rule["term"])
        await _rule(conn, title_id, version, rule, action, stats)

    # 3. The blanket sweep, over what the per-title rows did not name.
    for rule in blanket:
        if rule["term"] in handled:
            continue
        action = _VERDICTS.get((rule["verdict"] or "").strip().lower())
        if action == DROP_EVIDENCE:
            # Faithful to the corpus, which reaches neither of its two blanket arms for this action.
            continue
        await _rule(conn, title_id, version, rule, action, stats)

    return stats


async def _rule(
    conn: asyncpg.Connection,
    title_id: int,
    version: str,
    rule: dict,
    action: str | None,
    stats: dict[str, int],
) -> None:
    """One DROP, REPOINT or KEEP verdict against one term. The two phases above share it verbatim.

    A verdict this applier does not recognise does nothing and is counted, which is the corpus's
    behaviour by omission (its `if`/`elif` chain simply falls through) made visible. `keep` is
    counted for the same reason: decision 376 makes it an explicit no-op that SHADOWS a blanket
    rule, and a verdict whose whole effect is on another rule leaves no other trace to read.
    """
    if action == DROP:
        gone = await conn.fetch(
            "DELETE FROM dna_tag WHERE title_id = $1 AND version = $2 AND term = $3 RETURNING id",
            title_id, version, rule["term"],
        )
        for _ in gone:
            _bump(stats, "dropped")
    elif action == REPOINT and (rule["target"] or "").strip():
        await _repoint(conn, title_id, version, rule["term"], rule["target"].strip(), stats)
    elif action == KEEP:
        _bump(stats, "kept")
    else:
        _bump(stats, "unreadable_verdict")


async def _repoint(
    conn: asyncpg.Connection,
    title_id: int,
    version: str,
    term: str,
    target: str,
    stats: dict[str, int],
) -> None:
    """Re-point every tag carrying `term` onto `target`, merging where the target is already there.

    THE VOCABULARY DECIDES WHETHER THIS RUNS AT ALL, which is `mdc/dna/adjudication.py:174-177`'s
    `repoint_target_unknown` and is the one place the ledger could invent a term: a re-point onto a
    word `dna_term` does not carry would put a tag on a title that the facet colours, the shelves
    and §6.8's map have no row for. It is refused and counted, and the tag keeps the term it had --
    which is wrong in the way the owner already said, and therefore visible.
    """
    facet = await conn.fetchval(_FACET_OF, version, target)
    if facet is None:
        _bump(stats, "repoint_target_unknown")
        return
    for row in await conn.fetch(_TAGS_FOR_TERM, title_id, version, term):
        survivor = await conn.fetchval(
            _MERGE_TARGET, title_id, version, target, row["id"], row["provider"]
        )
        if survivor is None:
            await conn.execute(
                "UPDATE dna_tag SET term = $2, facet = $3 WHERE id = $1", row["id"], target, facet
            )
        else:
            await conn.execute(_TAG_MERGE, survivor, row["id"])
            # The quote moves with the reading it supports, which is `_merge_into`'s "a re-pointed
            # row is the same reading of the same film, so its quote belongs to the surviving tag".
            # Moved before the delete, because `dna_evidence.dna_tag_id` is ON DELETE CASCADE and a
            # delete-then-move would destroy the evidence rule 1 says makes the tag falsifiable.
            await conn.execute(
                "UPDATE dna_evidence SET dna_tag_id = $1 WHERE dna_tag_id = $2", survivor, row["id"]
            )
            await conn.execute("DELETE FROM dna_tag WHERE id = $1", row["id"])
        _bump(stats, "repointed")


async def _drop_evidence(
    conn: asyncpg.Connection,
    title_id: int,
    version: str,
    rule: dict,
    stats: dict[str, int],
) -> None:
    """Drop the quote this verdict names, and the tag if nothing is left to hold it up.

    `mdc/dna/adjudication.py:148-167` twice over: the match is a case-insensitive SUBSTRING, because
    the ledger's `quote` column carries the phrase the owner ruled on and `dna_evidence.quote` carries
    the sentence it came out of; and a rule with NO quote drops every evidence row for the term, which
    is that function's `kept = [...] if rule.quote else []` and reads as "this term has no admissible
    evidence on this title at all".

    A TAG LEFT WITH NO QUOTE IS DROPPED, and it is §4.1 rule 1 rather than a convenience: "a tag
    without its quote is unfalsifiable", so a tag whose last quote the owner has just ruled false has
    nothing left to trace to. That also means a DROP_EVIDENCE verdict naming a term whose tag carries
    no evidence rows at all drops it -- the corpus's behaviour exactly, and the honest one: this app's
    importer refuses an extracted tag arriving without evidence (`0004_dna.sql:90-91`), so such a row
    is already outside the invariant.
    """
    quote = (rule["quote"] or "").strip()
    for row in await conn.fetch(_TAGS_FOR_TERM, title_id, version, rule["term"]):
        if quote:
            dropped = await conn.fetch(
                "DELETE FROM dna_evidence WHERE dna_tag_id = $1"
                " AND strpos(lower(quote), lower($2)) > 0 RETURNING id",
                row["id"], quote,
            )
        else:
            dropped = await conn.fetch(
                "DELETE FROM dna_evidence WHERE dna_tag_id = $1 RETURNING id", row["id"]
            )
        if dropped:
            _bump(stats, "evidence_dropped")
        if not await conn.fetchval(
            "SELECT count(*) FROM dna_evidence WHERE dna_tag_id = $1", row["id"]
        ):
            await conn.execute("DELETE FROM dna_tag WHERE id = $1", row["id"])
            _bump(stats, "dropped")


async def apply_corrections(conn: asyncpg.Connection, title_id: int) -> dict[str, int]:
    """Apply this title's credit corrections. LAST, after everything else the derive writes.

    `mdc/corrections.py`'s header is the design rationale and is quoted rather than paraphrased,
    because the reason is the whole of why this function exists at the END of the derive:

        "The derived layer is a pure function of the raw layer plus the parser code, and `mdc
        rebuild` drops and rebuilds it wholesale. That is the right design, and it means a fix
        typed straight into the database is not a fix: the next rebuild deletes it. The DNA layer
        learned this the expensive way - hand-adjudicated curation applied as one-off SQL was
        silently reverted by every re-ingest until it moved into a ledger."

        "Applied last, and only to named rows. Every entry identifies one title and one claim,
        carries the evidence that settles it, and is applied by `apply` at the end of `rebuild`.
        Nothing is inferred; a correction with no evidence line is refused."

    THESE ARE NOT PARSER BUGS, which is the sentence that stops the next reader fixing this in the
    wrong file: "The parsers read what the sources say. TMDB credits *Invasion* to Bobby Krlic
    because Krlic scored season 2 and the row is season 1 ... No parser change fixes a source that
    is wrong, so the correction is recorded against the title and applied after the rebuild that
    would otherwise overwrite it."

    IDEMPOTENT BY `already_correct`, which is the property that lets it run after every single
    derive: "a correction already in place is a no-op, so this can run after every rebuild without
    accumulating duplicates". The check is on the person NAMES the title's music credits carry, and
    it is re-read per ledger row so that a `composer` and a `composer_add` on one title compose in
    the order the owner wrote them.

    `ORDER BY origin, id` IS DECISION 423, AND HERE IT SORTS THE HOUSEHOLD LAST, because this
    applier is LAST-wins: a `composer` row DELETEs every music credit the title has and inserts
    one, so whichever row is applied last is the one on the card. That is the opposite clause from
    `_TITLE_RULES`' above and the same rule -- the household's curated fix is the one that takes
    effect -- because the two appliers compose in opposite directions. Without it, a routine
    models-only re-import silently reverses the pair: `credit_correction.id` is a bigserial,
    `importer/dna.py`'s `load_corrections` DELETEs the bundle's rows and re-INSERTs them on every
    such import (decision 247), and the re-inserted bundle row therefore lands ABOVE the household
    row that was winning the day before. Decision 326 kept the household's ROW across that import;
    this keeps its FIX, which is what §6.6 promises and what decision 171's Cost paragraph -- "an
    in-app-authored correction ... is wiped" -- was about. It is also plain idempotence: a reload
    of a byte-identical TSV must not change what the next derive writes.
    Decision 326's "the appliers do NOT filter on origin" is untouched: every row is still
    selected and still applied, in an order that no longer moves when a sequence does.
    [M5.3 review cycle 1, m53-c1-dim326-01; decision 423]

    IT ALSO TAKES BACK WHAT THE LEDGER HAS STOPPED ASSERTING, and that half was missing for a
    milestone. The row this function mints carries `source = 'correction'`, which is precisely the
    label decision 375's scoped delete can never reach -- so when a correction is withdrawn,
    re-pointed at another name, or made inapplicable by having its evidence cleared, the credit it
    invented outlived it while `_write` restored the source's own composer beside it. The card then
    showed two composers, §3.1's `p:composer:<name>` feature carried the invented one, and the
    board said `{'rows': 0}`. Nothing self-healed it: the `composer` arm's DELETE fires only while
    a live `composer` row is being applied, `composer_add` never deletes at all, and no derive can
    remove a row no scope contains.

    That state is not exotic. `importer/dna.load_corrections` DELETEs `origin = 'bundle'` and
    re-INSERTs whatever the new TSV holds on every models-only import -- decision 247's recurring
    path, and its own comment says "a bundle ledger that arrives shorter still ends shorter" -- so
    a bundle that drops or re-points one of its six corrections strands a credit without any editor
    existing yet. `_reclaim` runs BEFORE the early return, because the emptied ledger is the case
    the early return would skip, and it deletes only rows this applier itself minted and labelled.
    The count is reported as `withdrawn` so §6.6's board says what happened rather than showing a
    silent difference. [M5.3 review cycle 2, m53-c2-scar-01]

    Returns counts by outcome, as the corpus does.
    """
    rows = await conn.fetch(
        "SELECT field, new_value, evidence FROM credit_correction WHERE title_id = $1"
        " ORDER BY origin, id",
        title_id,
    )
    stats: dict[str, int] = {"rows": len(rows)}
    # The names the ledger still asserts, by the same three tests the loop below applies -- a known
    # kind, evidence, a value -- so a row refused there is a row whose credit is reclaimed here.
    asserted = {
        name
        for row in rows
        if (row["field"] or "").strip() in CORRECTION_KINDS
        and (row["evidence"] or "").strip()
        if (name := (row["new_value"] or "").strip())
    }
    withdrawn = await _reclaim(conn, title_id, asserted)
    if withdrawn:
        stats["withdrawn"] = withdrawn
    if not rows:
        return stats
    if await conn.fetchval("SELECT 1 FROM title WHERE id = $1", title_id) is None:
        # `mdc/corrections.py:92-94`'s `unknown_title`, and it is a real state rather than a
        # defensive one: `credit_correction.title_id` carries no foreign key on purpose, so the
        # ledger legitimately names titles this install has never acquired (`0015_seed.sql:157-158`).
        # Counted and skipped -- a curated fix for a film nobody owns is not an error to raise on.
        stats["unknown_title"] = len(rows)
        return stats

    for row in rows:
        kind = (row["field"] or "").strip()
        if kind not in CORRECTION_KINDS:
            _bump(stats, "unknown_kind")
            continue
        if not (row["evidence"] or "").strip():
            _bump(stats, "no_evidence")      # a correction without evidence is an opinion
            continue
        name = (row["new_value"] or "").strip()
        if not name:
            _bump(stats, "no_value")
            continue

        existing = await conn.fetch(_MUSIC_CREDITS, title_id)
        have = {record["name"] for record in existing}
        if kind == "composer":
            if have == {name}:
                _bump(stats, "already_correct")
                continue
            await conn.execute(
                "DELETE FROM credit WHERE id = ANY($1::bigint[])",
                [record["id"] for record in existing],
            )
            await _credit(conn, title_id, name)
            _bump(stats, "replaced")
        else:
            if name in have:
                _bump(stats, "already_correct")
                continue
            await _credit(conn, title_id, name)
            _bump(stats, "added")

    return stats


async def _reclaim(conn: asyncpg.Connection, title_id: int, asserted: set[str]) -> int:
    """Delete the credits this applier minted for names the ledger no longer asserts.

    Two statements rather than one `DELETE ... USING`, which is the shape the `composer` arm above
    already takes: the ids are read first so the count is the rows that went rather than a status
    string parsed back, and a reader of §6.6's `withdrawn` figure is reading the same number the
    delete used.
    """
    stranded = await conn.fetch(_STRANDED_CREDITS, title_id, CORRECTION_SOURCE, sorted(asserted))
    if stranded:
        await conn.execute(
            "DELETE FROM credit WHERE id = ANY($1::bigint[])", [row["id"] for row in stranded],
        )
    return len(stranded)


async def _credit(conn: asyncpg.Connection, title_id: int, name: str) -> None:
    """The one corrected credit, minted through the derive's own person rule.

    `derive/ids.upsert_person` and not a local lookup: it is the only implementation of "fill, never
    clobber" for a human and the only one that asserts the minted id lands in this app's half of
    §4.1's partition. `mdc/corrections.py:71-77` writes its own two-line `_person_id` because the
    corpus has one id space; here a person minted below the floor "silently acquires the credits of
    whoever held that id in the corpus", and a correction is the last place to open that door.

    `billing_order` stays NULL: it is the cast's billing and this is a crew credit, and
    `derive/ids.keep_credit` reads it only for `role_class = 'cast'`.
    """
    person_id = await ids.upsert_person(conn, name=name)
    await conn.execute(
        "INSERT INTO credit (title_id, person_id, department, job, role_class, source)"
        " VALUES ($1, $2, $3, $4, $5, $6)",
        title_id, person_id, _MUSIC_DEPARTMENT, _MUSIC_JOB,
        ids.classify_role(_MUSIC_DEPARTMENT, _MUSIC_JOB), CORRECTION_SOURCE,
    )
