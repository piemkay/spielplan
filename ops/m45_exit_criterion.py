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
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import asyncpg  # noqa: E402
import numpy as np  # noqa: E402
from spielplan.db import migrate, pool  # noqa: E402
from spielplan.importer import bundle as bundle_import  # noqa: E402
from spielplan.models.artifacts import ArtifactStore  # noqa: E402
from spielplan.placement import features, reconcile  # noqa: E402
from spielplan.placement.contract import FeatureContract  # noqa: E402

APP_ID_FLOOR = 1_000_000_000

results: list[tuple[bool, str]] = []


def console(text: str) -> str:
    """`text` rendered in the encoding stdout actually has, escaping what it cannot carry.

    Not everything this script prints is this script's text. `check()`'s detail interpolates
    the importer's own finding messages below, and 51 of the `report.fail`/`warn`/`note` literals in
    `backend/spielplan/importer/` carry an em dash -- 26 in `validate.py` alone, most of them
    inside the 150-character slice. Under `PYTHONIOENCODING=cp850`, the code page CLAUDE.md's
    ASCII rule is about, printing one raises UnicodeEncodeError from inside `print`: a bundle
    that fails validation printed "[FAIL] the bundle the corpus built validates clean" and
    then a traceback where the reason should have been, which is the failure path of the one
    script whose job is diagnosis. The static guard reads this file's own literals and cannot
    see a string authored three packages away, so the escape has to happen where the foreign
    text meets the console. `backslashreplace` names the codepoint rather than dropping it,
    the same way the guard's own message survives its own subject.
    [M4.8 ti-non-ascii-in-console-output-violates-the-projects-own-rule]
    """
    encoding = sys.stdout.encoding or "ascii"
    return text.encode(encoding, "backslashreplace").decode(encoding)


def check(ok: bool, label: str, detail: str = "") -> bool:
    # Every line flushed. The import step below takes minutes, and this script's first run
    # hung for 9m35s leaving no record of which check it had reached: a block-buffered
    # stdout on a redirected pipe holds everything until the process ends, and a harness
    # whose whole purpose is to report cannot report from a buffer.
    # [M4.8 dd22-m45-exit-script-harness-hygiene]
    results.append((ok, label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {console(label)}", flush=True)
    if detail:
        for line in detail.splitlines():
            print(f"         {console(line)}", flush=True)
    return ok


def discard_staged_artifacts(artifacts_root: Path | None, store: ArtifactStore | None) -> None:
    """Remove the staging tree -- after closing everything that still holds a handle into it.

    `shutil.rmtree(..., ignore_errors=True)` is a no-op on Windows for any file still open:
    the unlink raises PermissionError (WinError 32) and `ignore_errors` swallows it. Two
    handles reach into the staged tree by the time this runs -- the `content_X.npz` read in
    section 5, and the `review_text_emb.npz` that `placement/features.py:190` loads through
    `ArtifactStore.npz`, which caches the NpzFile for the store's lifetime. Measured on this
    workstation, the only machine that has the bundle: 11.4 MB and its directories survived
    every run, forever, with no message, while every never-opened file in the same tree was
    removed. `_cache` is private and reached anyway, for the reason `pool._init_connection`
    and `reconcile._vocab_version` are reached below: a harness measures what the app does,
    and the app has no public way to let go. When the tree survives regardless, say which
    path -- a cleanup that can silently not happen is the same shape as a check that can
    silently not fail. [M4.8 dd22-m45-exit-script-harness-hygiene]
    """
    if store is not None:
        for handle in store._cache.values():
            closer = getattr(handle, "close", None)
            if closer is not None:
                closer()
        store._cache.clear()
    if artifacts_root is None:
        return
    shutil.rmtree(artifacts_root, ignore_errors=True)
    if artifacts_root.exists():
        print(f"  NOTE      staged artifacts survived cleanup at {artifacts_root}", flush=True)


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

    print(f"\nM4.5 exit criterion -- bundle {root.name}\n", flush=True)

    # --- 1. it validates ----------------------------------------------------------------
    print("1. Validation (§10 step 1)", flush=True)
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
    scratch = f"spielplan_m45_exit_p{os.getpid()}"
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS {scratch} WITH (FORCE)')
        await admin.execute(f"CREATE DATABASE {scratch}")
    finally:
        await admin.close()

    # The block that creates the database is the block that drops it. Everything that can fail
    # after the CREATE -- the connect, the codec registration this script has already been
    # bitten by once, the mkdtemp -- happens inside the `try`, because the scratch name now
    # carries this run's pid: the fixed name used to be cleaned up by the *next* run's
    # `DROP DATABASE IF EXISTS`, and pid-suffixing it (which is what stops two concurrent runs
    # dropping each other's database mid-import) removed that accidental second chance. An
    # orphan nothing will ever name again is a leak on the household's own server.
    # [M4.8 dd22-m45-exit-script-harness-hygiene]
    conn: asyncpg.Connection | None = None
    artifacts_root: Path | None = None
    store: ArtifactStore | None = None
    try:
        conn = await asyncpg.connect(dsn.rsplit("/", 1)[0] + f"/{scratch}")
        # The app's own connection setup, not a bare connect: `db/pool.py` registers the
        # json/jsonb codec every caller depends on, and a harness that skips it measures a
        # database the app never talks to. Without it `title_meta.payload` fails with
        # "expected str, got dict" -- which this script found, and which was a defect in the
        # script rather than in the importer.
        await pool._init_connection(conn)
        # A temporary directory, removed in the `finally` below. Staging into `ROOT/data/`
        # left 205 MB of extracted artifacts inside the working tree per run, which the next
        # run neither reused nor removed. [M4.8 dd22-m45-exit-script-harness-hygiene]
        artifacts_root = Path(tempfile.mkdtemp(prefix="spielplan-m45-exit-"))
        await migrate.apply_all(conn)

        # --- 2. it imports --------------------------------------------------------------
        print("\n2. Import (§10; the migration report's counts per table)", flush=True)
        began = time.perf_counter()
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
        print("\n3. The id partition (decision 162)", flush=True)
        max_title = await conn.fetchval("SELECT max(id) FROM title")
        check(max_title < APP_ID_FLOOR,
              f"every seeded title id is below the app's floor (max {max_title:,} < 1e9)")
        nxt = await conn.fetchval("SELECT nextval('title_id_seq')")
        check(nxt >= APP_ID_FLOOR and nxt > max_title,
              f"the next app-minted id is {nxt:,} -- above the floor and above the seed")

        # --- 4. the card has text and artwork -------------------------------------------
        print("\n4. The title card (§4.1 title_meta; §6.0)", flush=True)
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
        print("\n5. Stage 9's input (§4.3; §8 stage 9)", flush=True)
        store = ArtifactStore.open(artifacts_root / rep.bundle_version, rep.bundle_version)
        contract = FeatureContract.from_store(store)
        # The sample MUST include titles from the extracted tier: only 2,016 of 19,071 carry a
        # `dna_tag`, so a sample chosen by credit or keyword volume contains none of them and
        # reports dna_x as an empty block -- a harness artefact that reads exactly like the
        # defect this milestone exists to fix.
        sample = [int(r["id"]) for r in await conn.fetch(
            "SELECT t.id FROM title t"
            "  JOIN dna_tag d ON d.title_id = t.id"
            "  JOIN credit c ON c.title_id = t.id"
            "  JOIN title_keyword k ON k.title_id = t.id"
            " GROUP BY t.id ORDER BY count(*) DESC LIMIT 5"
        )]
        check(bool(sample), f"sampled {len(sample)} titles that carry every block's inputs")
        # The version the app would resolve, not a literal: `reconcile._vocab_version`
        # reads the store's manifest and falls back to the newest imported vocabulary, so
        # a hard-coded "v1" turns the first bundle built on v2 into a FAIL that says the
        # feature blocks are empty when what is empty is the join. Reached through the
        # module rather than copied, because copying the resolution is how the literal got
        # here. (placement/reconcile.py's `_vocab_version`, the same call
        # `reconcile_placements` makes before `place_titles`.)
        vocab = await reconcile._vocab_version(conn, store)
        built = await features.build_vectors(conn, store, contract, sample, vocab_version=vocab)
        per_block: dict[str, int] = {}
        for bv in built:
            for block in contract.blocks:
                seg = bv.vec[block.offset:block.stop]
                per_block[block.name] = per_block.get(block.name, 0) + int((seg != 0).sum())
        empty = sorted(n for n, nz in per_block.items() if nz == 0 and n != "genome")
        check(not empty, "every content block puts values in the columns the contract declares",
              " ".join(f"{n}={c}" for n, c in per_block.items()))

        # The distribution, against the corpus's own training matrix. Opened in a `with`: an
        # NpzFile is a live zip handle, and one left open makes the `finally`'s removal of the
        # tree it lives in a silent no-op on Windows. The two arrays below are read out of the
        # archive here and hold no handle of their own.
        # [M4.8 dd22-m45-exit-script-harness-hygiene]
        with np.load(store.path("content_X.npz"), allow_pickle=False) as z:
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
        # A reading, printed as one. This was a `check()` whose predicate was the literal
        # true: a summary of the loop above recorded as a PASS, which is a check that
        # cannot fail counting toward a published score. The predicate that *can* fail
        # already fires inside the loop -- one `check(False, ...)` per block that trains
        # on counts and arrives here as a presence bit -- so what is left over is the
        # numbers, and numbers are printed.
        # [M4.8 finding 19; ti-m45-exit-criterion-two-checks-cannot-fail]
        print("  the count-encoded blocks, trained max against ours:", flush=True)
        for line in counted:
            print(f"         {console(line)}", flush=True)

        # --- 6. content seeds once ------------------------------------------------------
        print("\n6. Seed once, models re-import (decision 162)", flush=True)
        # §10 makes validation step 1 and the admin Data tab's decision point, so the refusal has
        # to fire there and not only at import -- after the operator has committed.
        second = await bundle_import.validate_for_install(conn, b)
        refused = any(f.rule == "seed-once" for f in second.findings if f.severity == "fail")
        check(refused, "a second content import is refused at validation, before it writes",
              next((f.message[:160] for f in second.findings if f.rule == "seed-once"), ""))

        placed = await conn.fetchval(
            "SELECT count(*) FROM title WHERE is_owned AND placement = 'unplaced'"
        )
        # The predicate is the query's own answer, with no literal disjoined onto it. This
        # query is verbatim §12's M2 exit criterion and the invariant
        # `backend/migrations/0008_placement.sql:63-64` builds a partial index for; the
        # count is genuinely 0 on v20260828, so the literal that used to widen it concealed
        # nothing today and made it impossible to notice the day the count stops being 0.
        # [M4.8 finding 19]
        check(placed == 0, f"owned titles still unplaced after import: {placed:,}")
    except Exception as exc:
        # The block that measures reports the run it could not finish, rather than propagating
        # it. Everything from section 3 on stands on rows in `title`, and an import that lands
        # none does not raise: `importer/bundle.py` returns its report when
        # `validate_for_install` fails and catches its own `_Rollback` when the load does, so
        # section 2's `[FAIL] title loaded 0 rows` fell straight through into `max(id)`, which
        # is None on an empty table, and `None < APP_ID_FLOOR` raised TypeError. The operator
        # got a traceback about a comparison where the sentence naming the cause belongs, and
        # lost the sections below it and the `passed/total` tally -- the failure path of the
        # one script whose job is diagnosis, the same shape `console()` above was written for.
        # Reported as the failed check it is, with the traceback as its detail, so the exit
        # code stays non-zero and the run still ends in a score.
        # [M4.8 review cycle 2: m48-rev2-m45-raises-where-m4-was-taught-to-report]
        check(False, f"the run stopped on {type(exc).__name__}: {exc}", traceback.format_exc())
    finally:
        if conn is not None:
            await conn.close()
        discard_staged_artifacts(artifacts_root, store)
        admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS {scratch} WITH (FORCE)')
        finally:
            await admin.close()

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
