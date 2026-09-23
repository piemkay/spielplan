"""Load the naming layer. Spec v2.1 §4.1 rule 1, §4.3, §6.4.

This module is deliberately separate from `load.py` and loads the two tiers with two separate
statements. There is no function here that takes a "tier" parameter and no query that unions
them: rule 1 is "never merged, never unioned", and the cheapest way to keep that true is to
make merging require writing new code rather than passing a different argument.

**The file names and the column names are the corpus's.** Until M4.5 they were this repo's
reading of §4.3: `terms.tsv`, `aliases.tsv`, `adjudications.tsv` — three files no bundle
contains — and a `dna_tag` with an `id` column that upstream does not have. The naming layer
therefore loaded nothing at all from a real bundle, and said so only as one "missing file"
warning. `tests/fixtures/real_bundle_shapes.json` is the authority for every name below.

**The four curated ledgers are this module's public surface for a models-only re-import.**
Decision 247: `load_corrections`, `load_seed_list`, `load_adjudications` and `load_axes`, and
nothing else — never `load_vocabulary`'s term tables, `load_tags` or `load_projected`, which
are the content tiers decision 162 forbids a re-import from carrying. Those four are what
travels with a model bundle and what §8 stage 3 re-applies at every derive, and each of them
replaces what it finds rather than merging with it: the bundle's copy is the whole truth for its
version, and a ledger that arrives shorter has to end shorter. **For the rows the bundle
authored**, which is a qualifier decision 326 adds to two of the four: §6.6 gives the household
an editor over `credit_correction` and `dna_adjudication`, so from M5.6 those two tables hold
rows no bundle can supply and no re-import may take. `origin` is what tells them apart and the
two DELETEs below name it; the other two ledgers have no editor and stay whole-table replacements.
The one thing none of them may do is treat an absent, unreadable or EMPTY file as an instruction
to delete — omission is not destructive, which is the guard `parse_corrections` has always kept,
`load_adjudications` keeps under decision 247, `load_seed_list` under decision 260 and
`load_axes` under decision 264. Four ledgers, four guards: the count is written out because for
one cycle this sentence named three of them while all four cleared. Zero is not a length a
ledger can ask for: it is indistinguishable from an export that did not finish writing.
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from pathlib import Path
from typing import NamedTuple

import asyncpg

# The validator owns the one opener of a curated TSV, and the three readers below borrow it
# rather than each growing a handler of its own: §10 promises the operator a report, and one
# latin-1 byte in a hand-edited ledger came out of a route as a `UnicodeDecodeError` instead.
# The direction closes no cycle and is the only one that does not: `validate.py` imports
# `report` at module scope, reaches `load` and `vocab` inside functions, and never imports this
# module. [M4.14 step B2, finding 2.12]
from spielplan.importer import validate as validator
from spielplan.importer.report import ImportReport

# §6.8: "A fixed colour per vocabulary facet (11)." Shipped with the vocabulary when the
# bundle carries one; this is the fallback so the app has a palette on day one.
# `characters`, not `character`: every shipped vocabulary file is `vocab_characters_v1.tsv` and
# every term prefix is `characters`, so the singular spelled a twelfth facet that no row can ever
# have and left the real one at the neutral colour. The same misspelling is in six other places
# and M4.9 moves all seven together -- a palette keyed on a name the data does not use is the
# defect, not the spelling. [M4.9 finding 4]
DEFAULT_FACET_COLOURS = {
    "mood": "#c8613a", "themes": "#3f7f6f", "pacing": "#8b6bd6", "structure": "#c9a227",
    "visual": "#4d86c6", "sound": "#c25f8e", "characters": "#5fae7a", "place": "#b98046",
    "era": "#7f7fd6", "sensibility": "#4fa3a3", "register": "#b06a6a",
}

# The shipped per-title verdict ledger. The app read `term, verdict, target, note` — four names
# of which only `term` and `target` exist — and keyed the table on (version, term), which
# collapses 817 per-title verdicts onto one row per term.
ADJUDICATION_COLUMNS = ("scope", "title_id", "term", "action", "target", "quote", "source", "note")

# The shipped credit-corrections ledger. `kind` is the credit field the correction is about
# (composer, director, …), `value` is the asserted truth, `evidence` is what makes it checkable.
CORRECTIONS_COLUMNS = ("kind", "title_id", "value", "evidence", "note")


class Correction(NamedTuple):
    """One `corrections_v1.tsv` row, mapped onto `credit_correction`.

    `kind`/`value` become `field`/`new_value` because the ledger asserts what a credit **is**,
    not a diff from what it was; `old_value` and `person_name` stay empty rather than being
    invented from a column the file does not carry.
    """

    title_id: int | None
    field: str
    new_value: str | None
    evidence: str | None
    note: str | None


def app_facet(term: str, shipped: str) -> str:
    """The facet this app keys on, for a term the corpus shipped under `shipped`.

    §4.3 gives a vocabulary id as `facet.term` (`characters.amateur_sleuth`), and the corpus
    files the extraction pass that found the tag under a different name — `character_dynamics`,
    `mood_tone`, `narrative_themes`. **They are two namings of two different things**: the
    extraction label says which pass ran, the facet id says which of §6.4's eleven vocabularies
    the term belongs to. `dna_facet`, `dna_term`, §6.4's axes and §6.8's fixed-colour-per-facet
    palette all key on the second, so it is the second the app stores.

    One function for the rule because keeping it in three places is how the two vocabularies
    diverged: `load_vocabulary` derived the facet from the prefix while `load_tags` and
    `load_projected` copied the shipped column verbatim, and `0004_dna.sql:73-88` gives neither
    tag table an FK to `dna_facet` to notice — 29,188 of 31,540 `dna_tag` rows and 206,151 of
    223,136 `dna_projected` rows on the shipped bundle held a label that joined nothing. The
    extraction label is not lost: `validate.py`'s rule-1 block counts the rows it differs on, so
    the corpus's own naming survives as a §10 report line rather than as data. [M4.9 finding 1]

    An undotted term keeps the facet it arrived with, because the prefix rule has nothing to say
    about a vocabulary that is not `facet.term` — the same fallback `load_vocabulary` has always
    applied to the file's own facet name.
    """
    return term.split(".", 1)[0] if "." in term else shipped


async def load_vocabulary(
    conn: asyncpg.Connection, vocab_dir: Path, version: str, report: ImportReport
) -> None:
    """Load `dna_vocab/<version>/` — the per-facet vocabulary TSVs, the alias map, the
    per-title adjudications, and §6.4's authored axis definitions.

    §4.3 calls the directory "vocabulary TSVs, alias map, S matrix, adjudications" — plural
    TSVs, one per facet, named `vocab_<facet>_<version>.tsv`. The term id already carries its
    facet (`mood.dread`), so the facet is the prefix; rebuilding it from the file name as well
    produces `mood.mood.dread` and every join against `dna_tag.term` misses.
    """
    facet_names: set[str] = set()
    terms: list[tuple[str, str, str, str | None]] = []
    for path in sorted(vocab_dir.glob(f"vocab_*_{version}.tsv")):
        # `vocab_pacing_axes_v1.tsv` matches this glob and is a different artifact: per-term
        # axis coordinates (`id, ax_tempo, ax_pressure, …`) with no label and no gloss. Taken
        # for a facet vocabulary it invents a twelfth facet named `pacing_axes`.
        file_facet = path.stem[len("vocab_"):-len(f"_{version}")]
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            if not {"id", "label"} <= set(reader.fieldnames or []):
                report.note("vocabulary", f"{path.name}: not a facet vocabulary; not loaded")
                continue
            for row in reader:
                term = (row.get("id") or "").strip()
                if not term:
                    continue
                facet = app_facet(term, file_facet)
                facet_names.add(facet)
                terms.append((version, term, facet, (row.get("gloss") or "").strip() or None))

    # The shipped columns this schema does not carry — label, aliases, df_lb/df_ub, hub_ub, the
    # anchors — are vocabulary-*construction* evidence: they are how the corpus decided a term
    # earns its place, and no app surface reads them. `label` in particular is the term id minus
    # its facet prefix, so storing it would be storing a substring of the key.
    if not terms:
        report.warn(
            "vocabulary",
            f"no vocab_<facet>_{version}.tsv in {vocab_dir.name}/ — the naming layer stays empty",
        )
        return

    facets = {facet: i for i, facet in enumerate(sorted(facet_names))}
    await conn.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, $2, $3) "
        "ON CONFLICT (version) DO UPDATE SET facet_count = EXCLUDED.facet_count, "
        "term_count = EXCLUDED.term_count",
        version, len(facets), len(terms),
    )
    await conn.executemany(
        "INSERT INTO dna_facet (version, facet, ord, colour) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (version, facet) DO NOTHING",
        [(version, f, i, DEFAULT_FACET_COLOURS.get(f)) for f, i in facets.items()],
    )
    await conn.executemany(
        "INSERT INTO dna_term (version, term, facet, gloss) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (version, term) DO NOTHING",
        terms,
    )
    report.note("vocabulary", f"vocabulary {version}: {len(facets)} facets, {len(terms)} terms",
                facets=len(facets), terms=len(terms))

    await _load_aliases(conn, vocab_dir / f"alias_map_{version}.tsv", version, report)
    await load_adjudications(conn, vocab_dir, version, report)
    # The axis definitions key on a facet the vocabulary has just declared -- `dna_axis` has an
    # FK to `dna_facet` -- so the set travels rather than being rediscovered from a file stem.
    await load_axes(conn, vocab_dir, version, report, set(facets))


async def _load_aliases(
    conn: asyncpg.Connection, path: Path, version: str, report: ImportReport
) -> None:
    """§8 stage 8 projects the second tier through this map, so a map that loads as empty makes
    `dna_projected` unreproducible in-app. The file is `alias_map_<version>.tsv` and its two
    load-bearing columns are `raw_term` and `vocab_term`, not `alias` and `term`.

    Read through `validate._read_tsv` rather than opened here. This is one of the hand-edited
    curated files, so a stray latin-1 byte is exactly what reaches it, and the header check is
    the same rule one column over. Private, and staying private: the alias map belongs to the
    vocabulary tier decision 162 forbids a models-only re-import from reloading, so it is not on
    the surface decision 247 opens. [M4.14 step B2, finding 2.12]
    """
    if not path.is_file():
        report.warn("vocabulary", f"{path.name} absent — the projected tier has no alias map")
        return

    parsed = validator._read_tsv(path, report, "vocabulary", ("raw_term", "vocab_term"))
    if parsed is None:
        return

    rows: list[tuple[str, str, str]] = []
    unmapped = 0
    for row in parsed:
        alias = (row.get("raw_term") or "").strip()
        term = (row.get("vocab_term") or "").strip()
        # The map also carries raw terms the vocabulary did not adopt; `dna_alias.term` is
        # NOT NULL, so those are a constraint violation mid-transaction rather than a row.
        if not alias or not term:
            unmapped += 1
            continue
        rows.append((version, alias, term))

    await conn.executemany(
        "INSERT INTO dna_alias (version, alias, term) VALUES ($1, $2, $3) "
        "ON CONFLICT (version, alias) DO NOTHING",
        rows,
    )
    report.note("vocabulary", f"{len(rows)} alias mappings ({unmapped} raw terms map to nothing)",
                aliases=len(rows), unmapped=unmapped)


async def load_adjudications(
    conn: asyncpg.Connection, vocab_dir: Path, version: str, report: ImportReport
) -> None:
    """§8 stage 3 re-applies these at every derive; §14.5: "a derive that regenerates rows
    without re-applying them silently reverts curated fixes".

    The ledger is keyed **per title**: `scope, title_id, term, action, …`. Keyed on
    (version, term) instead, an upsert keeps the last verdict for a term and drops every other
    title's — no failure, no count, and in the direction that loses data.

    **Public, and taking the directory rather than the file**, because decision 247 makes this
    one of the four curated ledgers a models-only re-import loads: `bundle.py` reaches it
    directly on a bundle that carries no `content.sqlite` at all, where `load_vocabulary` —
    whose term tables are the content tier decision 162 forbids re-importing — must not run.
    The file is `adjudications_<version>.tsv`, so the directory and the version are what a caller
    can supply; the version is the caller's and never a literal, because on a models-only import
    it is the install's ACTIVE `dna_vocabulary` version (decision 247 guard 3) and
    `dna_adjudication.version` is an FK to that table — a default of "v1" here would file 828
    curated verdicts under a vocabulary the install may not be on. [M4.14 step C2, finding 2.15]
    """
    path = vocab_dir / f"adjudications_{version}.tsv"
    if not path.is_file():
        # decision 266's shape, for the branch decision 247 newly put on the recurring path. This
        # sentence and the two like it in this module read as statements about the INSTALL, and
        # on a models-only re-import - the only kind decision 162 says will arrive again - all
        # three are false: the loader returns before it touches anything, so what is stored is
        # exactly what was stored. Measured through the real import: the three ledgers unlinked
        # from a models-only bundle left credit_correction, seed_list and dna_adjudication at
        # their counts and printed three losses. The file is what is absent, so the file is what
        # the line names, and the count is what makes "nothing changed" checkable - the same
        # vocabulary the parses-to-nothing branch below already uses.
        # [M4.14 cycle 4, m414-c4-dim247-02, decisions 247 and 266]
        stored = await conn.fetchval(
            "SELECT count(*) FROM dna_adjudication WHERE version = $1", version
        )
        report.warn(
            "adjudications",
            f"{path.name} absent - this bundle carries no curated DNA verdicts; the {stored} "
            f"already stored under {version} are left in place and not re-applied (decision 247)"
            if stored else
            f"{path.name} absent - no curated DNA verdicts to re-apply",
            stored=stored,
        )
        return

    parsed = validator._read_tsv(path, report, "adjudications", ADJUDICATION_COLUMNS)
    if parsed is None:
        return

    rows: list[tuple] = []
    for row in parsed:
        scope = (row["scope"] or "").strip() or "global"
        term = (row["term"] or "").strip()
        verdict = (row["action"] or "").strip()
        raw_id = (row["title_id"] or "").strip()
        title_id = int(raw_id) if raw_id.isdigit() else None
        if not term or not verdict:
            report.warn("adjudications", f"{path.name}: a row carries no term or no action")
            continue
        if scope == "title" and title_id is None:
            report.warn(
                "adjudications",
                f"{path.name}: a title-scoped verdict on {term} carries no title_id",
            )
            continue
        rows.append((
            version, scope, title_id, term, verdict,
            (row["target"] or "").strip() or None, (row["quote"] or "").strip() or None,
            (row["source"] or "").strip() or None, (row["note"] or "").strip() or None,
        ))

    # Decision 247 guard 2. `parse_corrections` has refused to let a ledger that parses to
    # nothing pass as a silent zero since M4.5 and this loader did not, and the asymmetry is
    # sharper here because this one DELETEs first: an `adjudications_v1.tsv` present and empty
    # — the shape a half-finished upstream export writes — cleared 828 curated verdicts
    # and inserted none, with the report saying "0 DNA adjudications loaded" and nothing saying
    # what was lost. §14.5's scar is a derive that does not re-apply them; this was the import
    # doing the deleting itself. Omission may not be destructive, and the count is what makes the
    # refusal checkable rather than merely quiet.
    if not rows:
        stored = await conn.fetchval(
            "SELECT count(*) FROM dna_adjudication WHERE version = $1", version
        )
        report.warn(
            "adjudications",
            f"{path.name} parses to no verdicts; the {stored} already stored under {version} "
            "are left in place rather than replaced (decision 247)",
            stored=stored,
        )
        return

    # The ledger is authored upstream and travels with the bundle, so the bundle's copy is the
    # whole truth for its version; replacing it is what makes a re-import idempotent without a
    # key the data does not have (§10: a re-import is a planned admin event, not an append).
    #
    # WHOSE COPY, THOUGH, is the half decision 326 settles. §6.6's second ledger editor writes DNA
    # verdicts into this same table, and a household verdict is not the bundle's to replace: it
    # names a title this install acquired and a term this household argued about, and no upstream
    # export will ever carry it back. So the version scope gains a provenance scope. The argument
    # for the column, and decision 171's probe of what the unscoped form costs, are written out at
    # `load_corrections` -- that is the ledger the probe actually measured, and one statement of
    # the reason is what keeps the two from drifting apart. `origin` is a literal in the INSERT
    # rather than the column's DEFAULT for the same reason it is named in the DELETE: which rows a
    # re-import owns is the one question a reader of these two statements has.
    await conn.execute(
        "DELETE FROM dna_adjudication WHERE version = $1 AND origin = 'bundle'", version
    )
    await conn.executemany(
        "INSERT INTO dna_adjudication (version, scope, title_id, term, verdict, target, quote, "
        "source, note, origin) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'bundle')",
        rows,
    )
    per_title = sum(1 for r in rows if r[2] is not None)
    report.note(
        "adjudications",
        f"{len(rows)} DNA adjudications loaded ({per_title} scoped to a single title) — "
        "§8 stage 3 re-applies them at every derive",
        adjudications=len(rows), per_title=per_title,
    )


async def load_axes(
    conn: asyncpg.Connection, vocab_dir: Path, version: str, report: ImportReport,
    facets: set[str] | None = None,
) -> None:
    """§6.4: 'Axis definitions are a shipped, authored artifact: one TSV per vocabulary-v1 facet
    (left pole, right pole, term → weight ∈ [−1, 1]) … shipped in `dna_vocab/v1/`'.
    Deterministic — no nightly rebuild, no Procrustes anchoring, no map shift on re-import.

    **`dna_vocab/<version>/` is the sentence's own words, and the `axes/` subdirectory this
    loader used to read was an invention of this file's — unreachable by construction.** The
    corpus exporter copies the regular files of `data/dna_vocab/v1/` and does not descend into
    subdirectories, so an axis authored into `axes/` could never travel in a bundle at all. Five
    milestones read the empty table as "upstream has not authored them yet"; half of it was
    "this app waits on a path no export can fill", and the spec was right the whole time.
    Decision 173 ships the release without axes and moves the loader onto §6.4's path, so that
    the day the corpus does author them they arrive. [decision 173]

    The candidate set is therefore every TSV beside the vocabulary files, and the **pole header
    line** is what tells an axis definition from its neighbours: an axis opens with two poles
    (`heavy<TAB>light`), every other artifact in this directory opens with a row of column names,
    and the narrowest of those is four wide (`s_matrix_v1.tsv`: facet, a, b, s). A file that does
    not open with exactly two poles is counted and passed over rather than reported as a
    malformed axis — `vocab_mood_v1.tsv` is not a broken axis, it is a vocabulary, and a warning
    per neighbour would bury the one line an operator has to read.

    `vocab_pacing_axes_v1.tsv` is the file that rule exists for. It is per-term axis
    *coordinates* (`id, ax_tempo, ax_pressure, …`) — seven named columns, no label, no gloss and
    no poles — and read as an axis definition it would key `dna_axis` on a facet named
    `vocab_pacing_axes_v1` and print that raw id at a household in §6.2 step 5's copy. Its header
    is seven cells wide, so the pole rule refuses it; the facet check below refuses it again.

    That second check earns its place rather than doubling the first: `dna_axis` carries
    `FOREIGN KEY (version, facet) REFERENCES dna_facet` (`0004_dna.sql:57`), so a stem the
    vocabulary does not know raises a ForeignKeyViolation in the middle of the import
    transaction. §10 promises the operator a report, and an uncaught exception is not one — and
    the stem is a real trap, because decision 191's own prose spells the artifact
    `axis_<facet>_v1.tsv`, which names a facet called `axis_mood_v1`.

    **Public, and its facet set optional**, because decision 247 counts §6.4's axis TSVs among
    the four curated ledgers a models-only re-import loads: on that bundle `load_vocabulary` does
    not run, so there is no freshly declared facet set to travel and the authority is the one
    `dna_axis`'s FK actually checks against — `dna_facet` at this version, which is the
    vocabulary the install is already on (decision 163 refuses a bundle that would change it).
    Read here rather than at the call site, so the rule that a stem must be a facet keeps one
    statement and the FK cannot be satisfied against a set assembled somewhere else.
    [M4.14 step C2, finding 2.15]
    """
    if facets is None:
        facets = {
            r["facet"]
            for r in await conn.fetch("SELECT facet FROM dna_facet WHERE version = $1", version)
        }
    loaded = 0
    not_an_axis = 0
    unreadable: list[str] = []
    # This loader opens every TSV beside the vocabulary files — that IS the candidate rule, and
    # the pole header is what tells an axis from its neighbours — so it reads files it does not
    # own, and one latin-1 byte in the alias map or in the 828-row adjudications ledger came out
    # of `load_vocabulary` as a `UnicodeDecodeError`, with the naming layer half loaded and no
    # finding recorded. §10 promises a report. A warn and not a failure, because decision 173
    # ships the release with no authored axis at all, so a neighbour this loader cannot decode is
    # not a broken bundle; and it says only what this loader knows — that the file was not read
    # as an axis — because where the file has an owner, that owner reports the byte itself
    # (`_load_aliases` and `load_adjudications` both do). [M4.14 step B2, finding 2.12]
    for path in sorted(vocab_dir.glob("*.tsv")):
        try:
            with path.open(encoding="utf-8", newline="") as fh:
                reader = csv.reader(fh, delimiter="\t")
                header = next(reader, None)
                poles = [c.strip() for c in header] if header else []
                if len(poles) != 2 or not all(poles):
                    not_an_axis += 1
                    continue
                facet = path.stem
                if facet not in facets:
                    report.warn(
                        "axes",
                        f"{path.name}: poles {poles[0]!r}/{poles[1]!r}, but {facet!r} is not a "
                        "vocabulary facet — an axis file is named for the facet it turns",
                    )
                    continue
                left, right = poles
                weights: list[tuple[str, str, str, float]] = []
                for row in reader:
                    if len(row) < 2 or not row[0].strip():
                        continue
                    try:
                        w = float(row[1])
                    except ValueError:
                        report.warn(
                            "axes", f"{path.name}: non-numeric weight for {row[0]!r}; skipped"
                        )
                        continue
                    if not -1.0 <= w <= 1.0:
                        report.fail(
                            "axes", f"{path.name}: weight {w} for {row[0]!r} is outside [-1, 1]"
                        )
                        continue
                    weights.append((version, facet, row[0].strip(), w))
        except (OSError, UnicodeDecodeError, csv.Error):
            unreadable.append(path.name)
            continue

        # Decision 264: guard 2, on the fourth and last of the DELETE-first ledgers. The two
        # paths above that DECLINE a file `continue` before any write, and there is a third that
        # does not: a `<facet>.tsv` opening with a valid two-pole header whose body parses to no
        # usable weight row is ACCEPTED here, and it is the one that WRITES. It reached the
        # DELETE below with `weights == []`, cleared the installed weights for the facet,
        # inserted none, counted the file in `loaded` and reported success - which is the shape a
        # truncated or de-authored upstream export takes, and the exact loss the warning at the
        # foot of this function describes: `tonight/dna.axes_for` returns nothing for the facet,
        # `combine.contested_facet` cannot contest on it, `session_result.conflict` is NULL and
        # 54c's widest-axis tie-break is 0.0. Decision 163 pins the version across every
        # re-import, so the key the next bundle would collide on never changes and the only cure
        # is a corrected export from the corpus.
        #
        # Not in tension with decision 261's replacement: an axis re-authored SHORTER still ends
        # shorter, because `weights` is non-empty there. Zero is the one length indistinguishable
        # from an export that did not finish writing - the rule the module docstring states for
        # all four ledgers and that `parse_corrections`, `load_adjudications` (decision 247 guard
        # 2) and `load_seed_list` (decision 260) each already keep. Before the pole upsert and
        # not only before the DELETE, because a file this loader will not read weights out of is
        # a file it has no reason to believe the poles of either.
        # [M4.14 cycle 2, m414-c2-dim247-axes-empty-file-wipes-the-facet, decision 264]
        if not weights:
            stored = await conn.fetchval(
                "SELECT count(*) FROM dna_axis_weight WHERE version = $1 AND facet = $2",
                version, facet,
            )
            report.warn(
                "axes",
                f"{path.name} parses to no axis weights; the {stored} already stored for facet "
                f"{facet!r} under {version} are left in place rather than replaced (decision 264)",
                facet=facet, stored=stored,
            )
            continue

        await conn.execute(
            "INSERT INTO dna_axis (version, facet, left_pole, right_pole) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (version, facet) DO UPDATE SET left_pole = EXCLUDED.left_pole, "
            "right_pole = EXCLUDED.right_pole",
            version, facet, left, right,
        )
        # Decision 261: the bundle's copy is the whole truth for this FACET, which is the rule
        # the module docstring states for all four of decision 247's ledgers and the one this
        # loader did not keep. The upsert alone left a term the corpus had removed turning the
        # axis at its installed weight for ever - decision 163 pins the version across every
        # re-import, so the key it collides on never changes - and `tonight/dna.axes_for` reads
        # every row at that version, so a stale weight moves both the numerator and the engaged
        # weight of `combine.axis_position`, and with it `contested_facet` and 54c's tie-break.
        #
        # Per facet and not per version: this loop has already ACCEPTED this file (the pole
        # header parsed and the stem is a facet the vocabulary knows), and the facets it skipped
        # above are not this file's to clear. THREE paths reach this block or decline before it:
        # the two that decline a file outright `continue` above, and the third - accepted, and
        # parsing to no weight at all - is decision 264's guard, which returns the file to the
        # declining side. Stated as three because it was stated as two, and the one the count
        # left out is the one that writes.
        # [M4.14 cycle 1, M414-REV-247-02, decision 261; cycle 2, decision 264]
        await conn.execute(
            "DELETE FROM dna_axis_weight WHERE version = $1 AND facet = $2", version, facet
        )
        await conn.executemany(
            "INSERT INTO dna_axis_weight (version, facet, term, weight) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (version, facet, term) DO UPDATE SET weight = EXCLUDED.weight",
            weights,
        )
        loaded += 1

    # Both surfaces, because naming only the Map is what let this gap read as cosmetic for five
    # milestones. Without `dna_axis_weight`, `tonight/dna.axes_for` returns {},
    # `combine.contested_facet` iterates zero axes and returns None, so `session_result.conflict`
    # is NULL on every evening a household ever plays and §14 risk 6 watches a split rate that is
    # a permanent 0 — and 54c's widest-axis tie-break resolves to 0.0 for every pair it is asked
    # about. The bundle is not broken by this (decision 173 ships without axes deliberately), so
    # it is a warn and not a fail; it is said out loud so that "no split ever surfaced" is read
    # as the missing artifact rather than as a household that never disagreed.
    if not loaded:
        # THE CONSEQUENCES ARE THE INSTALL'S AND THE ABSENCE IS THE BUNDLE'S, and this sentence
        # asserted all three about an install it had not read. Decision 266's own Cost paragraph
        # names it - "false of an install whose weights are intact - the same defect from the
        # other side" - and it is reachable because decision 247's step C2 put this loader on the
        # models-only path, where the install can already hold weights and every declining branch
        # above leaves them exactly where they are. Measured: a models-only re-import with the
        # three axis TSVs unlinked left all six `dna_axis_weight` rows byte-identical and printed
        # that the Map had no axes to plot and that `session_result.conflict` is NULL on every
        # evening. The Map/conflict/54c clause is kept for the install that HAS those
        # consequences, which is the count coming back 0 - decision 173 makes that the shipped
        # state, and it is the line an operator actually has to act on.
        # [M4.14 cycle 4, m414-c4-dim247-01, decisions 247 and 266]
        stored = await conn.fetchval(
            "SELECT count(*) FROM dna_axis_weight WHERE version = $1", version
        )
        report.warn(
            "axes",
            f"no authored axis definition in dna_vocab/{version}/ in this bundle; the {stored} "
            f"already stored under {version} are left in place and not re-applied (decision 247)"
            if stored else
            f"no authored axis definition in dna_vocab/{version}/ - the Map surface has no axes "
            "to plot and renders its no-axes state, and Tonight's split surfacing (§6.2 step 5) "
            "is off: session_result.conflict is NULL on every evening and 54c's widest-axis "
            "tie-break is 0.0 for every pair",
            stored=stored,
        )
    if unreadable:
        report.warn(
            "axes",
            f"{len(unreadable)} file(s) in dna_vocab/{version}/ are not readable as UTF-8 text "
            "and were not read as axis definitions",
            files=unreadable,
        )
    # §10 asks for counts, and zero is the count that matters here, so the line is unconditional.
    report.note(
        "axes",
        f"{loaded} authored axis definition(s) loaded ({not_an_axis} file(s) in "
        f"dna_vocab/{version}/ open with column names rather than two poles)",
        facets=loaded, not_axes=not_an_axis,
    )


async def load_tags(
    conn: asyncpg.Connection, db: sqlite3.Connection, version: str, report: ImportReport
) -> None:
    """Tier 1 — extracted, quote-verified. Loaded on its own, with its evidence.

    Upstream carries no surrogate key: `dna_tag`'s primary key is (title_id, term) and
    `dna_evidence` is keyed by that same pair, not by a `dna_tag_id`. So the evidence rows can
    only be attached after the tags land, and the join is on the pair the bundle actually has.
    """
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "dna_tag" not in tables:
        report.fail("rule1-two-tiers", "bundle has no dna_tag table")
        return

    rows = [
        # `runs_found` — how many extraction runs turned the tag up — is this schema's
        # `n_sources`: rule 2, "a weight, never a filter". The corpus exports no `provider`
        # column at all, so `''` is written rather than NULL: 0018 section 2 makes the column
        # NOT NULL because a NULL component made `UNIQUE (title_id, version, term, provider)`
        # match nothing, and "no provider recorded" has to be one value for the arbiter to see
        # it. §6.6's parallel extraction mode writes a real name here; this is its absence, not
        # a guess at an LLM's.
        #
        # `app_facet` and not the shipped `facet`: see its docstring. [M4.9 finding 1]
        (title_id, version, term, app_facet(term, facet), salience, confidence, runs_found, "")
        for title_id, term, facet, salience, confidence, runs_found in db.execute(
            "SELECT title_id, term, facet, salience, confidence, runs_found FROM dna_tag"
        )
    ]
    # `UNIQUE (title_id, version, term, provider)` was meant to make this idempotent, but a NULL
    # component makes the arbiter index miss every row, so a second import appended the whole
    # tier again. §10 calls a re-import a planned admin event: the tier is replaced instead,
    # and `dna_evidence` follows it through ON DELETE CASCADE.
    #
    # The arbiter fires now — 0018 collapses NULL to '' and makes the column NOT NULL — and the
    # DELETE stays anyway: an upsert would leave behind the rows of a *previous* vocabulary
    # revision that this bundle no longer ships, and §10 calls a re-import a planned admin event
    # with a migration report, so replacing the tier is the behaviour that report describes.
    await conn.execute("DELETE FROM dna_tag WHERE version = $1", version)
    await conn.executemany(
        "INSERT INTO dna_tag "
        "(title_id, version, term, facet, salience, confidence, n_sources, provider) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
        rows,
    )
    report.table_counts["loaded:dna_tag"] = len(rows)

    if "dna_evidence" not in tables:
        report.fail("rule1-evidence", "bundle has no dna_evidence table — rule 1: 'a tag without "
                                      "its quote is unfalsifiable'")
        return

    id_map = {
        (r["title_id"], r["term"]): r["id"]
        for r in await conn.fetch(
            "SELECT id, title_id, term FROM dna_tag WHERE version = $1", version
        )
    }
    evidence: list[tuple[int, str, str, str | None]] = []
    orphaned = 0
    for title_id, term, pass_id, src, quote in db.execute(
        "SELECT title_id, term, pass_id, src, quote FROM dna_evidence"
    ):
        tag_id = id_map.get((title_id, term))
        if tag_id is None:
            orphaned += 1
            continue
        # `source` is NOT NULL here and `src` is nullable upstream. Dropping the row would drop
        # the quote, which rule 1 calls the falsifiable part of a tag, so an unattributed quote
        # is labelled rather than discarded.
        evidence.append((tag_id, quote, src or "unknown", pass_id))

    await conn.executemany(
        "INSERT INTO dna_evidence (dna_tag_id, quote, source, source_ref) VALUES ($1,$2,$3,$4)",
        evidence,
    )
    report.table_counts["loaded:dna_evidence"] = len(evidence)
    if orphaned:
        report.warn(
            "rule1-evidence",
            f"{orphaned} evidence quote(s) name a (title, term) with no extracted tag",
            orphaned=orphaned,
        )


async def load_projected(
    conn: asyncpg.Connection, db: sqlite3.Connection, version: str, report: ImportReport
) -> None:
    """Tier 2 — projected, inferred. A separate statement, on purpose (rule 1).

    Upstream is (title_id, term, facet, n_sources, sources): `n_sources` IS the weight — rule 2,
    "a weight, never a filter" — and `sources` is a JSON array naming what produced the row.
    """
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "dna_projected" not in tables:
        report.fail("rule1-two-tiers", "bundle has no dna_projected table")
        return
    rows = [
        # `app_facet` for the same reason as the extracted tier, and it matters more here: the
        # projected tier is where 206,151 of 223,136 shipped rows carried the extraction label,
        # so §6.8's palette and every per-facet aggregate over this table were empty. [finding 1]
        (title_id, version, term, app_facet(term, facet), n_sources, _via(sources))
        for title_id, term, facet, n_sources, sources in db.execute(
            "SELECT title_id, term, facet, n_sources, sources FROM dna_projected"
        )
    ]
    await conn.execute("DELETE FROM dna_projected WHERE version = $1", version)
    await conn.executemany(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
        "VALUES ($1,$2,$3,$4,$5,$6)",
        rows,
    )
    report.table_counts["loaded:dna_projected"] = len(rows)


def _via(sources: str | None) -> str | None:
    """`dna_projected.via` is "the keyword/alias that produced it" and is `text` that reaches
    the UI through `db.library.dna_for`. Upstream ships the whole provenance list as a JSON
    array, so it is flattened here rather than stored as a literal `["keyword:heist"]`."""
    if not sources:
        return None
    try:
        parsed = json.loads(sources)
    except ValueError:
        return sources
    return ", ".join(str(s) for s in parsed) if isinstance(parsed, list) else str(parsed)


def parse_corrections(path: Path, report: ImportReport) -> list[Correction]:
    """§4.3/§8 stage 3: `corrections_v1.tsv` — the credit-corrections ledger travels with the
    bundle and is applied at every derive. §14.5: 'a derive that regenerates rows without
    re-applying them silently reverts curated fixes' — the 787-rows-reverted-twice scar.

    Parsing is separate from the write because §10 promises a *report*: the loader read
    `r["field"]`, a column no shipped ledger has, so a real bundle raised `KeyError` — a stack
    trace where the operator was owed a validation failure naming the column.

    The open itself moved to `validate._read_tsv` for the same reason one column over: this is a
    hand-edited six-row ledger, so a stray latin-1 byte in it is the likeliest thing that is not
    a missing column, and until M4.14 that left `/validate` holding a `UnicodeDecodeError` with
    no finding at all. Unreadable and empty stay different answers: the first records a failure,
    the second the warning below, and `load_corrections` declines to replace the stored ledger on
    either — which is the guard decision 247 then asks `load_adjudications` to keep as well.
    [M4.14 step B2, finding 2.12]
    """
    parsed = validator._read_tsv(path, report, "corrections", CORRECTIONS_COLUMNS)
    if parsed is None:
        return []

    rows: list[Correction] = []
    for row in parsed:
        field = (row["kind"] or "").strip()
        raw_id = (row["title_id"] or "").strip()
        if raw_id and not raw_id.isdigit():
            report.warn(
                "corrections", f"{path.name}: {field or 'a row'} names title {raw_id!r}, "
                               "which is not a title id; skipped",
            )
            continue
        rows.append(Correction(
            int(raw_id) if raw_id else None,
            field,
            (row["value"] or "").strip() or None,
            (row["evidence"] or "").strip() or None,
            (row["note"] or "").strip() or None,
        ))

    if not rows:
        # A ledger that parses to nothing has the same effect as one that was never applied,
        # which is §14.5's scar exactly — so it may not pass as a silent zero.
        report.warn("corrections", f"{path.name} parses to no corrections — curated credit "
                                   "fixes will not survive the next derive")
    return rows


async def load_corrections(conn: asyncpg.Connection, path: Path, report: ImportReport) -> None:
    """Write the parsed ledger. See `parse_corrections` for the shape and the §14.5 argument."""
    if not path.is_file():
        # The same repair as `load_adjudications`' absent branch, and argued there: "curated
        # credit fixes will not survive the next derive" is §14.5's scar and is true of an
        # install that has none, but on the models-only path this loader now runs on it was said
        # over a `credit_correction` table this import had just declined to touch.
        # [M4.14 cycle 4, m414-c4-dim247-02, decisions 247 and 266]
        stored = await conn.fetchval("SELECT count(*) FROM credit_correction")
        report.warn(
            "corrections",
            f"corrections_v1.tsv absent - this bundle carries no credit ledger; the {stored} "
            "already stored are left in place and not re-applied (decision 247)"
            if stored else
            "corrections_v1.tsv absent - curated credit fixes will not survive the next derive",
            stored=stored,
        )
        return
    rows = parse_corrections(path, report)
    if not rows:
        return
    # The bundle's copy is the whole truth FOR THE ROWS THE BUNDLE WROTE, and the capitals are
    # there because the clause that used to stand here was "nothing else writes this table" -- a
    # measurement, offered as a rule, with a dated expiry nobody read as one. §6.6 promises the
    # household three ledger editors, and decision 171's Cost paragraph named the collision the
    # day the measurement would stop holding: "`DELETE FROM credit_correction` is unscoped, so
    # when §6.6's ledger editors land, an in-app-authored correction absent from the next bundle's
    # TSV is wiped (probed: P4 removed the app row)". That was accepted then because nothing wrote
    # household rows. M5.3 is the milestone that ends the condition -- it builds the appliers those
    # editors feed -- so decision 326 scopes the DELETE here rather than at M5.6, because a
    # provenance column added AFTER the first household row exists has to guess where that row
    # came from, and guessing wrong in this direction is the wipe the probe measured.
    #
    # Decision 247's rule is unchanged for the rows it was about: a bundle ledger that arrives
    # shorter still ends shorter, and on an install that has never opened an editor every row is
    # the bundle's, so the scoped DELETE and the unscoped one do the same thing. That is the point
    # -- the scope costs nothing until it is the only thing standing between a household's curated
    # fix and a routine re-import. `origin` is written as a literal in the INSERT rather than left
    # to the column's DEFAULT (0026_acquisition_sources.sql) because which rows a re-import claims
    # is the question a reader of these two statements has, and the answer belongs at the call site
    # rather than in whichever migration happened to add the column.
    #
    # What this comment used to argue was the re-import path, and that path did not reach this
    # function at all. Decision 162 makes a models-only bundle the recurring one, and `bundle.py`
    # held this call inside `if db is not None:` — a branch a models-only import never enters —
    # so the clear defended a repetition that could not happen while the six shipped corrections
    # were dropped on every re-import, with `report.table_counts` empty and no line in the report
    # to notice it by. Decision 247 puts the four curated ledgers back on the model path, which
    # is what makes this argument true rather than merely plausible.
    # [M4.14 finding 2.15; decisions 247, 171 and 326]
    await conn.execute("DELETE FROM credit_correction WHERE origin = 'bundle'")
    await conn.executemany(
        "INSERT INTO credit_correction (title_id, field, new_value, evidence, note, origin) "
        "VALUES ($1,$2,$3,$4,$5,'bundle')",
        rows,
    )
    # WHAT THIS LINE MAY CLAIM CHANGED THE DAY THE APPLIER LANDED, AND IT MAY NOT CLAIM MORE.
    # Until M5.3 it read "stored for §8 stage 3 (M5) - nothing applies them yet, so no credit on
    # any card reflects them", over a comment saying grep finds exactly two readers of
    # `credit_correction`: this writer and `backup/movie_data.py`. Both were true; both are now
    # false. `derive/ledgers.apply_corrections` is the third reader, and §8 stage 3 calls it per
    # title and last, after everything that derive has just regenerated (§14.5, decision 326).
    #
    # So the note names the applier and the MOMENT, because neither of the two sentences a reader
    # reaches for is the fact. An operator told "nothing applies them" on an install that has the
    # applier concludes the pipeline is broken and goes looking for the defect; one told "applied"
    # concludes the curated fixes are already on the cards and stops checking. What is true is
    # narrower than either: a correction reaches a card when its title is next derived, and the
    # rows this import just stored have not been derived against yet.
    #
    # `applied=0` STAYS and stays honest, because it counts what THIS IMPORT did, which is the
    # only thing an import report can count. The importer applies none -- that half has not moved
    # and is what the key was always about. What it may no longer be read as is a statement about
    # the install, which is why the message above it now names who does apply them.
    #
    # "re-applied at derive" is still refused outright, and is now the EASY thing to write rather
    # than the plainly wrong thing: the derive really does apply these, so the phrase is no longer
    # false about the pipeline, only about these rows. A report sentence in the past tense about an
    # event that has not happened is how §14.5's scar gets earned a third time -- the first two
    # were earned by believing an application had happened when it had not.
    #
    # Import-time patching of `credit` is deliberately not done — §8 stage 3 owns that, and a
    # second implementation of it here is how a derive silently reverts curated fixes. That half
    # is unchanged, and it is the reason this function writes a ledger and stops.
    # [M4.9 finding 32; §14.5; decision 326]
    report.note(
        "corrections",
        f"{len(rows)} credit correction(s) stored; §8 stage 3's derive applies them per title "
        "and last (derive/ledgers.py), so a card reflects one only after its title is next "
        "derived - this import applies none",
        corrections=len(rows), applied=0,
    )


def _decade(item: object) -> int | None:
    """The decade of a shipped onboarding entry, from its `year`.

    §4.3 calls this "the 100-title decade-stratified onboarding list", and the stratification is
    the reason §6.1 seeds the first rating queue from it. The corpus does not ship the decade:
    an entry is `kind, pct_dislike, pct_like, pct_ok, raters, title, title_id, year`, so
    `item["decade"]` was absent on every real bundle and the whole column loaded NULL — the same
    row count, the same 100 titles, and the one property the list exists for gone.

    A title whose year the corpus never resolved keeps a NULL decade rather than failing the
    bundle: §6.1 still wants the title in the queue, and a hole in the stratification is a
    smaller loss than a refused seed.

    AND THAT PROMISE COVERED ONE SPELLING OF "NEVER RESOLVED" OUT OF THREE. `year: null` returned
    None correctly; `NaN` raised ValueError and `Infinity` raised OverflowError, neither of which
    is an `asyncpg.PostgresError`, a `sqlite3.DatabaseError` or an `OSError` - so
    `import_bundle`'s named-refusal arm could not see them, `except BaseException` re-raised, and
    the operator's report line was `the import did not run to a report: ValueError: cannot
    convert float NaN to integer`: a Python type name for a defect in a named file, after the
    1.04 GB copy, for a refusal `/validate` had just said would not happen. `NaN` is precisely
    how an unresolved year arrives from a frame - `json.dumps` writes the bare literal and
    `json.loads` reads it back - so it is this docstring's own case, spelled the way the
    exporter's language spells it.
    The finite bound is the third: `seed_list.decade` is a smallint (`0003_content.sql`), so a
    year outside it (an epoch stamp written into the field) derived a decade asyncpg refused with
    a DataError - the named arm, but reported as "no rule in this importer named this refusal
    first". A year this app cannot turn into a storable decade is the same case as a year the
    corpus never resolved, and it takes the same answer.
    [M4.14 cycle 4, m414-c4-dim247-03]
    """
    if not isinstance(item, dict):
        return None
    year = item.get("year")
    if not isinstance(year, int | float) or not math.isfinite(year):
        return None
    decade = (int(year) // 10) * 10
    return decade if -32768 <= decade <= 32767 else None


async def load_seed_list(conn: asyncpg.Connection, path: Path, report: ImportReport) -> None:
    """§4.3: the 100-title decade-stratified onboarding list (§6.1 first-run queue seed).

    **The bundle's copy is the whole truth**, which is the rule `load_corrections` keeps one
    function above and this one did not. The upsert it replaces was `ON CONFLICT (position) DO
    UPDATE` with no preceding clear, so a refreshed list shorter than the installed one left its
    own tail behind: executed, a three-entry list over the fixture's eight left eight rows with
    positions 3-7 still naming the old ids. `rate/queue.py` joins the whole table and counts the
    whole table, so §6.1's first run would have offered a merge of two onboarding lists — with
    the same row count a correct load produces, which is why nothing noticed. Decision 162 makes
    the models-only bundle the recurring one and decision 247 is what makes this loader run on
    one at all, so the clear has to land first or the wiring ships the merge.
    [M4.14 step C1, finding 2.15]

    **An entry naming a title this install never seeded is skipped and counted.** `seed_list
    .title_id` is a NOT NULL foreign key to `title(id)` (`0003_content.sql`), so a single unknown
    id aborts the whole import on a violation that names a constraint rather than a file — and
    decision 248 makes the case ordinary rather than exotic: the corpus's catalogue is not frozen
    at this install's seed, so a later bundle's onboarding list can name a title this household
    never acquired. §6.1 wants the titles it can offer, and the count is what keeps the shortfall
    from being silent. `validate._validate_seed_list` says the same number before the transaction
    opens; this is the half that happens inside it. (decision 247 guard 1)

    **A list that parses to nothing is a refusal to replace, not an instruction to delete.**
    Decision 260 extends decision 247's guard 2 to the third DELETE-first ledger: `[]` and
    `{"titles": []}` are what a half-finished upstream export writes, and they wiped the
    installed list behind a note reading "0-title decade-stratified seed list loaded". A shorter
    list still ends shorter; zero is the one length indistinguishable from an export that did not
    finish. [M4.14 cycle 1, M414-REV-247-01, decision 260]

    The parse is deliberately not defensive. `validate_artifacts` has already refused a payload
    that is not a list of objects carrying an integer `title_id`, so the `int(item)` fallback this
    used to keep was a second, quieter reading of a shape the validator no longer admits — and
    two readings of one file is how the two come to disagree. [M4.14 step B2]
    """
    if not path.is_file():
        # The sharpest of the three, and argued at `load_adjudications`' absent branch: with rows
        # still stored, "falls back to P(seen) ordering alone" is the INVERSE of what happens -
        # `rate/queue.py` LEFT JOINs the table, orders on `s.seed_position ASC NULLS LAST` ahead
        # of `p_seen DESC`, counts it for §6.1's first run and prints "seed list position N of M"
        # on the very next card. The operator was told §6.1's onboarding seed was gone while the
        # app's own why-line said it was in use, and their remedy - re-export, or restore - is
        # work against a defect that does not exist.
        # [M4.14 cycle 4, m414-c4-dim247-02, decisions 247 and 266]
        stored = await conn.fetchval("SELECT count(*) FROM seed_list")
        report.warn(
            "seed-list",
            f"seed_list.json absent - this bundle carries no onboarding list; the {stored} "
            "already stored are left in place and not re-applied (decision 247)"
            if stored else
            "seed_list.json absent - the first rating queue falls back to P(seen) ordering alone",
            stored=stored,
        )
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload["titles"] if isinstance(payload, dict) else payload
    ids = [int(item["title_id"]) for item in items]
    known = {
        r["id"]
        for r in await conn.fetch("SELECT id FROM title WHERE id = ANY($1::int[])", ids)
    }

    rows: list[tuple[int, int, int | None]] = []
    unseeded: list[int] = []
    for title_id, item in zip(ids, items, strict=True):
        if title_id not in known:
            unseeded.append(title_id)
            continue
        # Renumbered over what loaded rather than carried across from the bundle's own index:
        # `position` is a smallint PRIMARY KEY meaning "the Nth title offered" and nothing reads
        # it as a pointer back into the file, so a hole would only record a skip the report
        # already carries by name.
        rows.append((len(rows), title_id, _decade(item)))

    if not rows:
        # Decision 260: guard 2, extended to the third DELETE-first ledger. `load_corrections`
        # returns before its own DELETE when the ledger parses to nothing and `load_adjudications`
        # does the same naming the stored count, and this loader - the one decision 247 newly put
        # on the recurring path, and the one that gained a DELETE this milestone - did not. A
        # `seed_list.json` present and parsing to zero usable entries, which is the shape a
        # half-finished upstream export writes, therefore cleared the household's onboarding list
        # and wrote nothing back, behind a note reading "0-title decade-stratified seed list
        # loaded". `rate/queue.py` counts this table for section 6.1's first run and LEFT JOINs
        # it for the ordering, so the seed became the P(seen) fallback silently.
        #
        # Not in tension with guard 1: a ledger that arrives SHORTER still ends shorter, because
        # `rows` is non-empty there. Zero is the one length that cannot be told apart from an
        # export that did not finish. [M4.14 cycle 1, M414-REV-247-01, decision 260]
        #
        # AND THE TWO CAUSES ARE SAID APART, because `rows` is what is left after guard 1 has
        # dropped every unknown id rather than what the file parsed to. A list that parsed
        # perfectly and named only titles this install never seeded - decision 248's ordinary
        # case taken to its limit, a corpus that renumbered its catalogue - reached this branch
        # and reported "parses to no onboarding titles", byte-identical to the line an empty
        # export gets, above guard 1's own warn and its `return`. The two have different
        # remedies: re-export the file, or seed the titles it names. Measured: two well-formed
        # entries naming unknown ids produced exactly the empty-file sentence with the skipped
        # count and the ids dropped. [M4.14 cycle 4, m414-c4-dim247-04]
        stored = await conn.fetchval("SELECT count(*) FROM seed_list")
        if unseeded:
            report.warn(
                "seed-list",
                f"all {len(unseeded)} of {path.name}'s onboarding entry(ies) name a title this "
                f"install never seeded (first: {unseeded[:5]}); none is usable, so the {stored} "
                "already stored are left in place rather than replaced (decisions 247 and 260)",
                skipped=len(unseeded), title_ids=unseeded[:20], stored=stored,
            )
        else:
            report.warn(
                "seed-list",
                f"{path.name} parses to no onboarding titles; the {stored} already stored are "
                "left in place rather than replaced (decision 260)",
                stored=stored,
            )
        return

    await conn.execute("DELETE FROM seed_list")
    await conn.executemany(
        "INSERT INTO seed_list (position, title_id, decade) VALUES ($1,$2,$3)",
        rows,
    )
    if unseeded:
        report.warn(
            "seed-list",
            f"{len(unseeded)} onboarding entry(ies) name a title this install never seeded "
            f"(first: {unseeded[:5]}); they are skipped and the first rating queue is that much "
            "shorter (decision 247)",
            skipped=len(unseeded), title_ids=unseeded[:20],
        )
    undated = sum(1 for _, _, decade in rows if decade is None)
    report.note(
        "seed-list",
        f"{len(rows)}-title decade-stratified seed list loaded across "
        f"{len({d for _, _, d in rows if d is not None})} decade(s); {undated} carry no year",
        titles=len(rows), undated=undated,
    )
