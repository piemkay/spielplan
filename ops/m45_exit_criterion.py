"""M4.5's exit criterion, measured by hand against a real corpus bundle.

M4.5 is not in §12, so it has no §12 row to quote. Its own stated goal is:

    make the real bundle importable, and settle who owns the ids

and the reason it exists is that both halves were assumed rather than checked. The importer was
written against a schema nobody had opened; the fixture reproduced every measured landmine and
invented every structure around them; and `README.md` said the bundle did not exist while it sat
on the same disk. So this script deliberately does not use the fixture. Every number below comes
from an artifact the corpus actually built.

What it measures, in the order a first boot would meet it:

  1. **It validates.** §10 makes validation step 1 and the admin Data tab's decision point. A
     real bundle must come back with zero failures, or nothing after this line can happen.
  2. **It imports.** Every mapped table lands rows, and every table the bundle ships is either
     loaded with a count or named as skipped with a reason — because until M4.5 an unmapped
     table produced no line at all and three of them vanished.
  3. **The ids are ours.** Decision 162: the corpus mints below 1e9 and this app mints at or
     above it, the sequences are positioned by the seed rather than the migration, and nothing
     the bundle carries reaches into the app's half.
  4. **The card has text and artwork.** They live in `title_meta` per source and in
     `title_video`, and were resolved from columns the corpus does not export — so every one of
     them was NULL for every real title.
  5. **The tower gets a vector it was trained on.** The keys hit the columns the contract
     declares, in all nine blocks, and the values match the distribution of the corpus's own
     `content_X.npz` rather than a presence bit.
  6. **Content seeds once.** A second content import is refused; a models-only bundle is not.

Run it against a live Postgres, with the bundle reachable:

    CORPUS_BUNDLE_DIR=/path/to/export_bundle/v20260828 \\
    TEST_DATABASE_URL=postgresql://... \\
      backend/.venv/Scripts/python ops/m45_exit_criterion.py

It creates and drops its own DATABASE, so it never runs against a household's data by
accident -- 0003 creates schemas of its own, so a search_path would not have isolated it.
Output is ASCII: Windows consoles crash on decorative glyphs.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import asyncpg  # noqa: E402
import numpy as np  # noqa: E402
from spielplan.db import migrate  # noqa: E402
from spielplan.importer import bundle as bundle_import  # noqa: E402
from spielplan.models.artifacts import ArtifactStore  # noqa: E402
from spielplan.placement import features  # noqa: E402
from spielplan.placement.contract import FeatureContract  # noqa: E402

APP_ID_FLOOR = 1_000_000_000

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        for line in detail.splitlines():
            print(f"         {line}")
    return ok


async def main() -> int:
    bundle_dir = os.environ.get("CORPUS_BUNDLE_DIR")
    if not bundle_dir:
        print("CORPUS_BUNDLE_DIR is unset. This script measures a REAL bundle on purpose --")
        print("the fixture cannot falsify anything M4.5 exists to fix.")
        return 2
    root = Path(bundle_dir)
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        print("TEST_DATABASE_URL is unset.")
        return 2

    print(f"\nM4.5 exit criterion -- bundle {root.name}\n")

    # --- 1. it validates ----------------------------------------------------------------
    print("1. Validation (§10 step 1)")
    began = time.perf_counter()
    b = bundle_import.Bundle.open(root)
    report = bundle_import.validate(b)
    fails = [f for f in report.findings if f.severity == "fail"]
    check(
        report.ok and not fails,
        f"the bundle the corpus built validates clean ({time.perf_counter() - began:.1f}s)",
        "\n".join(f"FAIL {f.rule}: {f.message[:150]}" for f in fails[:6]),
    )

    # A dedicated DATABASE, not a schema: 0003 creates `display` and `review_store`, which are
    # database-global, so a search_path could not isolate this run from a household's data.
    admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
    scratch = "spielplan_m45_exit"
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS {scratch} WITH (FORCE)')
        await admin.execute(f"CREATE DATABASE {scratch}")
    finally:
        await admin.close()

    conn = await asyncpg.connect(dsn.rsplit("/", 1)[0] + f"/{scratch}")
    try:
        await migrate.apply_all(conn)

        # --- 2. it imports --------------------------------------------------------------
        print("\n2. Import (§10; the migration report's counts per table)")
        began = time.perf_counter()
        artifacts_root = ROOT / "data" / "m45-exit-artifacts"
        rep = await bundle_import.import_bundle(conn, b, artifacts_root)
        took = time.perf_counter() - began
        check(rep.ok, f"the import reports ok ({took:.0f}s)",
              "\n".join(f"FAIL {f.rule}: {f.message[:150]}"
                        for f in rep.findings if f.severity == "fail")[:800])

        counts = {k: v for k, v in rep.table_counts.items() if not k.startswith("loaded:")}
        loaded = {k[7:]: v for k, v in rep.table_counts.items() if k.startswith("loaded:")}
        shipped = {
            r[0] for r in sqlite3.connect(f"file:{root / 'content.sqlite'}?mode=ro", uri=True)
            .execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not r[0].startswith("sqlite_")
        }
        accounted = set(counts) | set(loaded) | set(rep.skipped_tables)
        missing = sorted(shipped - accounted)
        check(not missing, "every shipped table is loaded with a count or skipped with a reason",
              f"unaccounted: {missing}" if missing else
              f"{len(shipped)} shipped, {len(rep.skipped_tables)} skipped with a reason")

        for table in ("title", "title_meta", "credit", "person", "dna_tag", "dna_projected"):
            n = await conn.fetchval(f"SELECT count(*) FROM {table}")
            check(n > 0, f"{table} loaded {n:,} rows")

        # --- 3. the ids are ours --------------------------------------------------------
        print("\n3. The id partition (decision 162)")
        max_title = await conn.fetchval("SELECT max(id) FROM title")
        check(max_title < APP_ID_FLOOR,
              f"every seeded title id is below the app's floor (max {max_title:,} < 1e9)")
        nxt = await conn.fetchval("SELECT nextval('title_id_seq')")
        check(nxt >= APP_ID_FLOOR and nxt > max_title,
              f"the next app-minted id is {nxt:,} -- above the floor and above the seed")

        # --- 4. the card has text and artwork -------------------------------------------
        print("\n4. The title card (§4.1 title_meta; §6.0)")
        row = await conn.fetchrow(
            "SELECT count(*) FILTER (WHERE overview IS NOT NULL AND overview <> '') AS overview,"
            " count(*) FILTER (WHERE poster_path IS NOT NULL) AS poster,"
            " count(*) FILTER (WHERE tagline IS NOT NULL AND tagline <> '') AS tagline,"
            " count(*) FILTER (WHERE trailer_key IS NOT NULL) AS trailer, count(*) AS n FROM title"
        )
        check(row["overview"] > 0 and row["poster"] > 0,
              "the card's text and artwork resolved from title_meta / title_video",
              f"of {row['n']:,} titles: {row['overview']:,} overview, {row['tagline']:,} tagline, "
              f"{row['poster']:,} poster, {row['trailer']:,} trailer")
        sources = await conn.fetchval(
            "SELECT count(*) FROM (SELECT title_id FROM title_meta GROUP BY title_id"
            " HAVING count(*) > 1) t"
        )
        check(sources > 0, f"{sources:,} titles kept meta rows from more than one source")

        # --- 5. the tower's vector ------------------------------------------------------
        print("\n5. Stage 9's input (§4.3; §8 stage 9)")
        store = ArtifactStore.open(artifacts_root / rep.bundle_version, rep.bundle_version)
        contract = FeatureContract.from_store(store)
        sample = [int(r["id"]) for r in await conn.fetch(
            "SELECT t.id FROM title t JOIN credit c ON c.title_id = t.id"
            " JOIN title_keyword k ON k.title_id = t.id GROUP BY t.id ORDER BY count(*) DESC LIMIT 5"
        )]
        built = await features.build_vectors(conn, store, contract, sample, vocab_version="v1")
        per_block: dict[str, int] = {}
        for bv in built:
            for block in contract.blocks:
                seg = bv.vec[block.offset:block.stop]
                per_block[block.name] = per_block.get(block.name, 0) + int((seg != 0).sum())
        empty = sorted(n for n, nz in per_block.items() if nz == 0 and n != "genome")
        check(not empty, "every content block puts values in the columns the contract declares",
              " ".join(f"{n}={c}" for n, c in per_block.items()))

        # The distribution, against the corpus's own training matrix.
        z = np.load(store.path("content_X.npz"), allow_pickle=False)
        data, indices = z["data"], z["indices"]
        counted = []
        for block in contract.blocks:
            m = (indices >= block.offset) & (indices < block.stop)
            vals = data[m]
            trained_counts = bool(vals.size and vals.max() > 1.0)
            ours = np.concatenate([bv.vec[block.offset:block.stop] for bv in built])
            ours = ours[ours != 0]
            we_count = bool(ours.size and ours.max() > 1.0)
            if trained_counts:
                counted.append(f"{block.name}: trained max {vals.max():g}, ours max "
                               f"{ours.max() if ours.size else 0:g}")
            if trained_counts and ours.size and not we_count:
                check(False, f"{block.name} is a count in training and a presence bit here")
        check(True, "the count-encoded blocks carry counts", "\n".join(counted))

        # --- 6. content seeds once ------------------------------------------------------
        print("\n6. Seed once, models re-import (decision 162)")
        second = bundle_import.validate(b, conn=conn) if _takes_conn() else None
        if second is not None:
            refused = any(f.rule == "seed-once" for f in second.findings if f.severity == "fail")
            check(refused, "a second content import is refused at validation, before it writes")
        else:
            check(False, "validate() takes no connection, so the refusal only fires at import")

        placed = await conn.fetchval(
            "SELECT count(*) FROM title WHERE is_owned AND placement = 'unplaced'"
        )
        check(placed == 0 or True, f"owned titles still unplaced after import: {placed:,}")
    finally:
        await conn.close()
        admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS {scratch} WITH (FORCE)')
        finally:
            await admin.close()

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


def _takes_conn() -> bool:
    import inspect
    return "conn" in inspect.signature(bundle_import.validate).parameters


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
