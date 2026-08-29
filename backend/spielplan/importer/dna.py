"""Load the naming layer. Spec v2.1 §4.1 rule 1, §4.3, §6.4.

This module is deliberately separate from `load.py` and loads the two tiers with two separate
statements. There is no function here that takes a "tier" parameter and no query that unions
them: rule 1 is "never merged, never unioned", and the cheapest way to keep that true is to
make merging require writing new code rather than passing a different argument.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import asyncpg

from spielplan.importer.report import ImportReport

# §6.8: "A fixed colour per vocabulary facet (11)." Shipped with the vocabulary when the
# bundle carries one; this is the fallback so the app has a palette on day one.
DEFAULT_FACET_COLOURS = {
    "mood": "#c8613a", "themes": "#3f7f6f", "pacing": "#8b6bd6", "structure": "#c9a227",
    "visual": "#4d86c6", "sound": "#c25f8e", "character": "#5fae7a", "place": "#b98046",
    "era": "#7f7fd6", "sensibility": "#4fa3a3", "register": "#b06a6a",
}


async def load_vocabulary(
    conn: asyncpg.Connection, vocab_dir: Path, version: str, report: ImportReport
) -> None:
    """Load `dna_vocab/<version>/` — terms, facets, aliases, adjudications, axis definitions."""
    terms_path = vocab_dir / "terms.tsv"
    if not terms_path.is_file():
        report.warn("vocabulary", f"{terms_path.name} missing — the naming layer stays empty")
        return

    facets: dict[str, int] = {}
    terms: list[tuple[str, str, str, str | None]] = []
    with terms_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            facet = row["facet"].strip()
            facets.setdefault(facet, len(facets))
            terms.append((version, row["term"].strip(), facet, (row.get("gloss") or "").strip() or None))

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

    aliases_path = vocab_dir / "aliases.tsv"
    if aliases_path.is_file():
        with aliases_path.open(encoding="utf-8", newline="") as fh:
            rows = [
                (version, r["alias"].strip(), r["term"].strip())
                for r in csv.DictReader(fh, delimiter="\t")
            ]
        await conn.executemany(
            "INSERT INTO dna_alias (version, alias, term) VALUES ($1, $2, $3) "
            "ON CONFLICT (version, alias) DO NOTHING",
            rows,
        )
        report.note("vocabulary", f"{len(rows)} alias mappings")

    adj_path = vocab_dir / "adjudications.tsv"
    if adj_path.is_file():
        with adj_path.open(encoding="utf-8", newline="") as fh:
            rows = [
                (version, r["term"].strip(), r["verdict"].strip(),
                 (r.get("target") or "").strip() or None, (r.get("note") or "").strip() or None)
                for r in csv.DictReader(fh, delimiter="\t")
            ]
        await conn.executemany(
            "INSERT INTO dna_adjudication (version, term, verdict, target, note) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (version, term) DO UPDATE "
            "SET verdict = EXCLUDED.verdict, target = EXCLUDED.target, note = EXCLUDED.note",
            rows,
        )
        report.note(
            "adjudications",
            f"{len(rows)} DNA adjudications loaded — §8 stage 3 re-applies them at every derive",
        )

    await _load_axes(conn, vocab_dir, version, report)


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
    """Tier 1 — extracted, quote-verified. Loaded on its own, with its evidence."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "dna_tag" not in tables:
        report.fail("rule1-two-tiers", "bundle has no dna_tag table")
        return

    id_map: dict[int, int] = {}
    rows = db.execute(
        "SELECT id, title_id, term, facet, salience, confidence, n_sources, provider FROM dna_tag"
    ).fetchall()
    for old_id, title_id, term, facet, salience, confidence, n_sources, provider in rows:
        new_id = await conn.fetchval(
            """
            INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence,
                                 n_sources, provider)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (title_id, version, term, provider) DO UPDATE SET salience = EXCLUDED.salience
            RETURNING id
            """,
            title_id, version, term, facet, salience, confidence, n_sources, provider,
        )
        id_map[old_id] = new_id
    report.table_counts["loaded:dna_tag"] = len(rows)

    if "dna_evidence" in tables:
        ev = [
            (id_map[t], quote, source, ref)
            for t, quote, source, ref in db.execute(
                "SELECT dna_tag_id, quote, source, source_ref FROM dna_evidence"
            )
            if t in id_map
        ]
        await conn.executemany(
            "INSERT INTO dna_evidence (dna_tag_id, quote, source, source_ref) VALUES ($1,$2,$3,$4)",
            ev,
        )
        report.table_counts["loaded:dna_evidence"] = len(ev)


async def load_projected(
    conn: asyncpg.Connection, db: sqlite3.Connection, version: str, report: ImportReport
) -> None:
    """Tier 2 — projected, inferred. A separate statement, on purpose (rule 1)."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "dna_projected" not in tables:
        report.fail("rule1-two-tiers", "bundle has no dna_projected table")
        return
    rows = [
        (title_id, version, term, facet, weight, via)
        for title_id, term, facet, weight, via in db.execute(
            "SELECT title_id, term, facet, weight, via FROM dna_projected"
        )
    ]
    await conn.executemany(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
        "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (title_id, version, term) DO NOTHING",
        rows,
    )
    report.table_counts["loaded:dna_projected"] = len(rows)


async def load_corrections(conn: asyncpg.Connection, path: Path, report: ImportReport) -> None:
    """§4.3/§8 stage 3: `corrections_v1.tsv` — the credit-corrections ledger travels with the
    bundle and is applied at every derive. §14.5: 'a derive that regenerates rows without
    re-applying them silently reverts curated fixes' — the 787-rows-reverted-twice scar."""
    if not path.is_file():
        report.warn("corrections", "corrections_v1.tsv absent — curated credit fixes will not "
                                   "survive the next derive")
        return
    with path.open(encoding="utf-8", newline="") as fh:
        rows = [
            (
                int(r["title_id"]) if (r.get("title_id") or "").strip() else None,
                (r.get("person_name") or "").strip() or None,
                r["field"].strip(),
                (r.get("old_value") or "").strip() or None,
                (r.get("new_value") or "").strip() or None,
                (r.get("note") or "").strip() or None,
            )
            for r in csv.DictReader(fh, delimiter="\t")
        ]
    await conn.executemany(
        "INSERT INTO credit_correction (title_id, person_name, field, old_value, new_value, note) "
        "VALUES ($1,$2,$3,$4,$5,$6)",
        rows,
    )
    report.note("corrections", f"{len(rows)} credit corrections loaded and re-applied at derive")


async def load_seed_list(conn: asyncpg.Connection, path: Path, report: ImportReport) -> None:
    """§4.3: the 100-title decade-stratified onboarding list (§6.1 first-run queue seed)."""
    if not path.is_file():
        report.warn("seed-list", "seed_list.json absent — the first rating queue falls back to "
                                 "P(seen) ordering alone")
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload["titles"] if isinstance(payload, dict) else payload
    rows = [
        (i, int(item["title_id"]) if isinstance(item, dict) else int(item),
         int(item["decade"]) if isinstance(item, dict) and "decade" in item else None)
        for i, item in enumerate(items)
    ]
    await conn.executemany(
        "INSERT INTO seed_list (position, title_id, decade) VALUES ($1,$2,$3) "
        "ON CONFLICT (position) DO UPDATE SET title_id = EXCLUDED.title_id",
        rows,
    )
    report.note("seed-list", f"{len(rows)}-title decade-stratified seed list loaded")
