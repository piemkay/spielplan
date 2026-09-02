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
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import NamedTuple

import asyncpg

from spielplan.importer.report import ImportReport

# §6.8: "A fixed colour per vocabulary facet (11)." Shipped with the vocabulary when the
# bundle carries one; this is the fallback so the app has a palette on day one.
DEFAULT_FACET_COLOURS = {
    "mood": "#c8613a", "themes": "#3f7f6f", "pacing": "#8b6bd6", "structure": "#c9a227",
    "visual": "#4d86c6", "sound": "#c25f8e", "character": "#5fae7a", "place": "#b98046",
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
                facet = term.split(".", 1)[0] if "." in term else file_facet
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
    await _load_adjudications(conn, vocab_dir / f"adjudications_{version}.tsv", version, report)
    await _load_axes(conn, vocab_dir, version, report)


async def _load_aliases(
    conn: asyncpg.Connection, path: Path, version: str, report: ImportReport
) -> None:
    """§8 stage 8 projects the second tier through this map, so a map that loads as empty makes
    `dna_projected` unreproducible in-app. The file is `alias_map_<version>.tsv` and its two
    load-bearing columns are `raw_term` and `vocab_term`, not `alias` and `term`."""
    if not path.is_file():
        report.warn("vocabulary", f"{path.name} absent — the projected tier has no alias map")
        return

    rows: list[tuple[str, str, str]] = []
    unmapped = 0
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if not {"raw_term", "vocab_term"} <= set(reader.fieldnames or []):
            report.fail(
                "vocabulary",
                f"{path.name}: header {reader.fieldnames} carries no raw_term/vocab_term column",
            )
            return
        for row in reader:
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


async def _load_adjudications(
    conn: asyncpg.Connection, path: Path, version: str, report: ImportReport
) -> None:
    """§8 stage 3 re-applies these at every derive; §14.5: "a derive that regenerates rows
    without re-applying them silently reverts curated fixes".

    The ledger is keyed **per title**: `scope, title_id, term, action, …`. Keyed on
    (version, term) instead, an upsert keeps the last verdict for a term and drops every other
    title's — no failure, no count, and in the direction that loses data.
    """
    if not path.is_file():
        report.warn("adjudications", f"{path.name} absent — no curated DNA verdicts to re-apply")
        return

    rows: list[tuple] = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        missing = [c for c in ADJUDICATION_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            report.fail(
                "adjudications",
                f"{path.name}: header is not the shipped ledger — missing column(s) "
                f"{', '.join(missing)}",
                missing=missing,
            )
            return
        for row in reader:
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

    # The ledger is authored upstream and travels with the bundle, so the bundle's copy is the
    # whole truth for its version; replacing it is what makes a re-import idempotent without a
    # key the data does not have (§10: a re-import is a planned admin event, not an append).
    await conn.execute("DELETE FROM dna_adjudication WHERE version = $1", version)
    await conn.executemany(
        "INSERT INTO dna_adjudication (version, scope, title_id, term, verdict, target, quote, "
        "source, note) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
        rows,
    )
    per_title = sum(1 for r in rows if r[2] is not None)
    report.note(
        "adjudications",
        f"{len(rows)} DNA adjudications loaded ({per_title} scoped to a single title) — "
        "§8 stage 3 re-applies them at every derive",
        adjudications=len(rows), per_title=per_title,
    )


async def _load_axes(
    conn: asyncpg.Connection, vocab_dir: Path, version: str, report: ImportReport
) -> None:
    """§6.4: 'Axis definitions are a shipped, authored artifact: one TSV per vocabulary-v1 facet
    (left pole, right pole, term → weight ∈ [−1, 1])'. Deterministic — no nightly rebuild, no
    Procrustes anchoring, no map shift on bundle re-import."""
    axes_dir = vocab_dir / "axes"
    if not axes_dir.is_dir():
        report.warn(
            "axes",
            "no dna_vocab/<v>/axes/ in the bundle — the Map surface has no axes to plot and "
            "will render its no-axes state",
        )
        return

    loaded = 0
    for path in sorted(axes_dir.glob("*.tsv")):
        facet = path.stem
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh, delimiter="\t")
            header = next(reader, None)
            if not header or len(header) < 2:
                report.warn("axes", f"{path.name}: no pole header line; skipped")
                continue
            left, right = header[0].strip(), header[1].strip()
            weights: list[tuple[str, str, str, float]] = []
            for row in reader:
                if len(row) < 2 or not row[0].strip():
                    continue
                try:
                    w = float(row[1])
                except ValueError:
                    report.warn("axes", f"{path.name}: non-numeric weight for {row[0]!r}; skipped")
                    continue
                if not -1.0 <= w <= 1.0:
                    report.fail("axes", f"{path.name}: weight {w} for {row[0]!r} is outside [-1, 1]")
                    continue
                weights.append((version, facet, row[0].strip(), w))

        await conn.execute(
            "INSERT INTO dna_axis (version, facet, left_pole, right_pole) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (version, facet) DO UPDATE SET left_pole = EXCLUDED.left_pole, "
            "right_pole = EXCLUDED.right_pole",
            version, facet, left, right,
        )
        await conn.executemany(
            "INSERT INTO dna_axis_weight (version, facet, term, weight) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (version, facet, term) DO UPDATE SET weight = EXCLUDED.weight",
            weights,
        )
        loaded += 1
    report.note("axes", f"{loaded} authored axis definition(s) loaded", facets=loaded)


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
        # column; a single-provider export leaves it NULL rather than guessing an LLM name.
        (title_id, version, term, facet, salience, confidence, runs_found)
        for title_id, term, facet, salience, confidence, runs_found in db.execute(
            "SELECT title_id, term, facet, salience, confidence, runs_found FROM dna_tag"
        )
    ]
    # `UNIQUE (title_id, version, term, provider)` was meant to make this idempotent, but a NULL
    # component makes the arbiter index miss every row, so a second import appended the whole
    # tier again. §10 calls a re-import a planned admin event: the tier is replaced instead,
    # and `dna_evidence` follows it through ON DELETE CASCADE.
    await conn.execute("DELETE FROM dna_tag WHERE version = $1", version)
    await conn.executemany(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, n_sources) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7)",
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
        (title_id, version, term, facet, n_sources, _via(sources))
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
    """
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        header = reader.fieldnames or []
        missing = [c for c in CORRECTIONS_COLUMNS if c not in header]
        if missing:
            report.fail(
                "corrections",
                f"{path.name}: header {header} is not the shipped ledger — missing column(s) "
                f"{', '.join(missing)}",
                missing=missing,
            )
            return []

        rows: list[Correction] = []
        for row in reader:
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
        report.warn("corrections", "corrections_v1.tsv absent — curated credit fixes will not "
                                   "survive the next derive")
        return
    rows = parse_corrections(path, report)
    if not rows:
        return
    # Decision 162 makes a model re-import a repeated event, and the INSERT below had no clear,
    # so every re-import appended the whole ledger again. The bundle's copy is authoritative for
    # the bundle, and nothing else writes this table yet, so it is replaced rather than merged.
    await conn.execute("DELETE FROM credit_correction")
    await conn.executemany(
        "INSERT INTO credit_correction (title_id, field, new_value, evidence, note) "
        "VALUES ($1,$2,$3,$4,$5)",
        rows,
    )
    report.note("corrections", f"{len(rows)} credit corrections loaded and re-applied at derive")


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
    """
    if not isinstance(item, dict):
        return None
    year = item.get("year")
    return (int(year) // 10) * 10 if isinstance(year, int | float) else None


async def load_seed_list(conn: asyncpg.Connection, path: Path, report: ImportReport) -> None:
    """§4.3: the 100-title decade-stratified onboarding list (§6.1 first-run queue seed)."""
    if not path.is_file():
        report.warn("seed-list", "seed_list.json absent — the first rating queue falls back to "
                                 "P(seen) ordering alone")
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload["titles"] if isinstance(payload, dict) else payload
    rows = [
        (i, int(item["title_id"]) if isinstance(item, dict) else int(item), _decade(item))
        for i, item in enumerate(items)
    ]
    await conn.executemany(
        "INSERT INTO seed_list (position, title_id, decade) VALUES ($1,$2,$3) "
        "ON CONFLICT (position) DO UPDATE SET title_id = EXCLUDED.title_id, "
        "decade = EXCLUDED.decade",
        rows,
    )
    undated = sum(1 for _, _, decade in rows if decade is None)
    report.note(
        "seed-list",
        f"{len(rows)}-title decade-stratified seed list loaded across "
        f"{len({d for _, _, d in rows if d is not None})} decade(s); {undated} carry no year",
        titles=len(rows), undated=undated,
    )
