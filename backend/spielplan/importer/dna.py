"""Load the naming layer (§4.1 rule 1, §4.3, §6.4). The two tiers load separately, never unioned.

The four curated ledgers also load on models-only re-imports (decision 247); each replaces only the
bundle's rows (decision 326) and never treats an absent, unreadable or empty file as a delete.
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from pathlib import Path
from typing import NamedTuple

import asyncpg

# The validator owns the one TSV opener, so a bad byte becomes a finding. No import cycle.
from spielplan.importer import validate as validator
from spielplan.importer.report import ImportReport

# §6.8's fixed colour per facet; the fallback palette. `characters`, plural, as the data spells it.
DEFAULT_FACET_COLOURS = {
    "mood": "#c8613a", "themes": "#3f7f6f", "pacing": "#8b6bd6", "structure": "#c9a227",
    "visual": "#4d86c6", "sound": "#c25f8e", "characters": "#5fae7a", "place": "#b98046",
    "era": "#7f7fd6", "sensibility": "#4fa3a3", "register": "#b06a6a",
}

# The shipped per-title verdict ledger's columns.
ADJUDICATION_COLUMNS = ("scope", "title_id", "term", "action", "target", "quote", "source", "note")

# The shipped credit-corrections ledger: `kind` is the credit field, `value` the asserted truth.
CORRECTIONS_COLUMNS = ("kind", "title_id", "value", "evidence", "note")


class Correction(NamedTuple):
    """One `corrections_v1.tsv` row, mapped onto `credit_correction`.

    `old_value` and `person_name` stay empty rather than invented.
    """

    title_id: int | None
    field: str
    new_value: str | None
    evidence: str | None
    note: str | None


def app_facet(term: str, shipped: str) -> str:
    """The facet this app keys on, for a term the corpus shipped under `shipped`.

    The term's own prefix (`characters.x` -> `characters`), not the corpus's extraction label; an
    undotted term keeps the facet it arrived with.
    """
    return term.split(".", 1)[0] if "." in term else shipped


class VocabTerm(NamedTuple):
    """One row of a `vocab_<facet>_<version>.tsv`, as far as this app keeps it."""

    term: str
    facet: str
    label: str | None
    gloss: str | None


def read_vocabulary(vocab_dir: Path, version: str, report: ImportReport) -> list[VocabTerm]:
    """The per-facet vocabulary TSVs under `dna_vocab/<version>/`, parsed; shared by both `dna_term`
    writers.
    """
    terms: list[VocabTerm] = []
    for path in sorted(vocab_dir.glob(f"vocab_*_{version}.tsv")):
        # `vocab_pacing_axes_v1.tsv` matches the glob but is axis coordinates, not a facet vocabulary.
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
                terms.append(VocabTerm(
                    term,
                    app_facet(term, file_facet),
                    (row.get("label") or "").strip() or None,
                    (row.get("gloss") or "").strip() or None,
                ))
    return terms


async def load_vocabulary(
    conn: asyncpg.Connection, vocab_dir: Path, version: str, report: ImportReport
) -> None:
    """Load `dna_vocab/<version>/` — the per-facet vocabulary TSVs, the alias map and the
    per-title adjudications.

    The term id already carries its facet (`mood.dread`); never prefix it again.
    """
    terms = read_vocabulary(vocab_dir, version, report)
    facet_names = {t.facet for t in terms}

    # Construction-only columns are not stored. `label` is stored as shipped (decision 486).
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
        "INSERT INTO dna_term (version, term, facet, label, gloss) VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (version, term) DO NOTHING",
        [(version, t.term, t.facet, t.label, t.gloss) for t in terms],
    )
    report.note("vocabulary", f"vocabulary {version}: {len(facets)} facets, {len(terms)} terms",
                facets=len(facets), terms=len(terms))

    await _load_aliases(conn, vocab_dir / f"alias_map_{version}.tsv", version, report)
    await load_adjudications(conn, vocab_dir, version, report)


async def backfill_labels(conn: asyncpg.Connection, vocab_dir: Path, version: str) -> int:
    """Fill `dna_term.label` from the staged TSVs where it is NULL, and return how many were filled.

    For installs seeded before 0030. Idempotent UPDATE only; an absent directory fills nothing.
    """
    shipped = [t for t in read_vocabulary(vocab_dir, version, ImportReport()) if t.label]
    if not shipped:
        return 0
    return await conn.fetchval(
        "WITH filled AS ("
        "  UPDATE dna_term d SET label = s.label"
        "    FROM unnest($2::text[], $3::text[]) AS s(term, label)"
        "   WHERE d.version = $1 AND d.term = s.term AND d.label IS NULL"
        "  RETURNING 1) "
        "SELECT count(*)::int FROM filled",
        version, [t.term for t in shipped], [t.label for t in shipped],
    )


async def _load_aliases(
    conn: asyncpg.Connection, path: Path, version: str, report: ImportReport
) -> None:
    """Load `alias_map_<version>.tsv` (`raw_term` -> `vocab_term`, plus `kind`); §8 stage 8 projects
    through it. Private: the alias map is vocabulary tier, not a curated ledger (decision 162).
    """
    if not path.is_file():
        report.warn("vocabulary", f"{path.name} absent — the projected tier has no alias map")
        return

    parsed = validator._read_tsv(path, report, "vocabulary", ("raw_term", "vocab_term"))
    if parsed is None:
        return

    rows: list[tuple[str, str, str, str | None]] = []
    unmapped = 0
    for row in parsed:
        alias = (row.get("raw_term") or "").strip()
        term = (row.get("vocab_term") or "").strip()
        # Unadopted raw terms have no `term`; skip them.
        if not alias or not term:
            unmapped += 1
            continue
        # `kind` as shipped, NULL when absent (decision 500): lexicon rows must never project.
        rows.append((version, alias, term, (row.get("kind") or "").strip() or None))

    await conn.executemany(
        "INSERT INTO dna_alias (version, alias, term, kind) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (version, alias) DO NOTHING",
        rows,
    )
    lexicon = sum(1 for r in rows if (r[3] or "").casefold() == "lexicon")
    report.note("vocabulary", f"{len(rows)} alias mappings ({unmapped} raw terms map to nothing; "
                f"{lexicon} are extraction lexicon and never project)",
                aliases=len(rows), unmapped=unmapped, lexicon=lexicon)


async def load_adjudications(
    conn: asyncpg.Connection, vocab_dir: Path, version: str, report: ImportReport
) -> None:
    """Load the per-title adjudications ledger, `adjudications_<version>.tsv` (§14.5).

    Keyed per title, never (version, term). `version` is the caller's, never a literal: it is an FK
    (decision 247 guard 3).
    """
    path = vocab_dir / f"adjudications_{version}.tsv"
    if not path.is_file():
        # The file is absent: say so, with the unchanged stored count (decision 266).
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

    # Decision 247 guard 2: an empty ledger must not clear the stored one.
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

    # Replaces only the bundle's rows (decision 326); household verdicts survive. `origin` is a
    # literal in the INSERT so the claimed rows are visible here.
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


async def load_tags(
    conn: asyncpg.Connection, db: sqlite3.Connection, version: str, report: ImportReport
) -> None:
    """Tier 1 — extracted, quote-verified. Loaded on its own, with its evidence.

    Upstream has no surrogate key: evidence joins on (title_id, term) after the tags land.
    """
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "dna_tag" not in tables:
        report.fail("rule1-two-tiers", "bundle has no dna_tag table")
        return

    rows = [
        # `runs_found` is `n_sources` (rule 2). No provider upstream, so `''` (NOT NULL since 0018).
        (title_id, version, term, app_facet(term, facet), salience, confidence, runs_found, "")
        for title_id, term, facet, salience, confidence, runs_found in db.execute(
            "SELECT title_id, term, facet, salience, confidence, runs_found FROM dna_tag"
        )
    ]
    # Replace the tier: an upsert would keep a previous vocabulary revision's rows. Evidence cascades.
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
        # `source` is NOT NULL here; label an unattributed quote rather than drop it (rule 1).
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

    `n_sources` IS the weight (rule 2); `sources` names what produced the row.
    """
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "dna_projected" not in tables:
        report.fail("rule1-two-tiers", "bundle has no dna_projected table")
        return
    rows = [
        # `app_facet`, as for the extracted tier.
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
    """`dna_projected.via` is "the keyword/alias that produced it", flattened from upstream's JSON array.
    """
    if not sources:
        return None
    try:
        parsed = json.loads(sources)
    except ValueError:
        return sources
    return ", ".join(str(s) for s in parsed) if isinstance(parsed, list) else str(parsed)


def parse_corrections(path: Path, report: ImportReport) -> list[Correction]:
    """Parse `corrections_v1.tsv`, the credit-corrections ledger applied at every derive (§14.5).

    Separate from the write so a bad column or byte becomes a report line. Unreadable and empty
    are different answers, and neither replaces the stored ledger.
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
        # An empty ledger may not pass as a silent zero (§14.5).
        report.warn("corrections", f"{path.name} parses to no corrections — curated credit "
                                   "fixes will not survive the next derive")
    return rows


async def load_corrections(conn: asyncpg.Connection, path: Path, report: ImportReport) -> None:
    """Write the parsed ledger. See `parse_corrections` for the shape and the §14.5 argument."""
    if not path.is_file():
        # The file is absent: report the stored count, which this import did not touch.
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
    # Replaces only the bundle's rows (decision 326): household corrections survive a re-import.
    # `origin` is a literal in the INSERT so the claimed rows are visible here.
    await conn.execute("DELETE FROM credit_correction WHERE origin = 'bundle'")
    await conn.executemany(
        "INSERT INTO credit_correction (title_id, field, new_value, evidence, note, origin) "
        "VALUES ($1,$2,$3,$4,$5,'bundle')",
        rows,
    )
    # Say who applies these and when: the next derive of each title. This import applied none, so
    # `applied=0` is honest; import-time patching of `credit` is §8 stage 3's job, not this one's.
    report.note(
        "corrections",
        f"{len(rows)} credit correction(s) stored; §8 stage 3's derive applies them per title "
        "and last (derive/ledgers.py), so a card reflects one only after its title is next "
        "derived - this import applies none",
        corrections=len(rows), applied=0,
    )


def _decade(item: object) -> int | None:
    """The decade of a shipped onboarding entry, from its `year`.

    The corpus ships no decade. A null, NaN, infinite or smallint-overflowing year gives a NULL decade
    rather than failing the import.
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

    Replaces the whole table, so a shorter list ends shorter. Unknown title ids are skipped and counted
    (decision 248). A list parsing to nothing does not delete (decision 260).
    """
    if not path.is_file():
        # The file is absent: the stored list is still in use, so say that.
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
        # Renumbered over what loaded; `position` is not a pointer back into the file.
        rows.append((len(rows), title_id, _decade(item)))

    if not rows:
        # Decision 260: zero usable entries must not clear the list. Empty file and "all ids unknown" are
        # reported apart: their remedies differ.
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
    await _report_thin_seed_cards(conn, report, loaded=len(rows))


async def _report_thin_seed_cards(
    conn: asyncpg.Connection, report: ImportReport, *, loaded: int
) -> None:
    """How many onboarding titles will reach the first Rate cards with nothing to recognise.

    No poster, or only an MPST plot; reported so a thin list is visible before a household meets it.
    """
    row = await conn.fetchrow(
        """
        SELECT count(*) FILTER (
                   WHERE t.overview IS NULL OR btrim(t.overview) = ''
                      OR EXISTS (SELECT 1 FROM title_meta m
                                  WHERE m.title_id = t.id AND m.source = 'mpst'
                                    AND btrim(m.payload->>'plot_full') = btrim(t.overview))
               ) AS no_text,
               count(*) FILTER (WHERE t.poster_path IS NULL) AS no_poster
          FROM seed_list sl JOIN title t ON t.id = sl.title_id
        """
    )
    no_text, no_poster = int(row["no_text"]), int(row["no_poster"])
    if no_text or no_poster:
        report.warn(
            "seed-list-cards",
            f"of the {loaded} onboarding titles, {no_text} carry no plot line a Rate card can "
            f"show and {no_poster} no poster, so a first rating sitting meets them as bare names "
            "(section 6.1)",
            no_text=no_text, no_poster=no_poster, titles=loaded,
        )
