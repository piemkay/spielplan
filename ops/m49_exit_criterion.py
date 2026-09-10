"""M4.9's exit criterion, measured by hand against a real corpus bundle.

M4.9 makes the app read the rows the real corpus export actually ships. Its thesis is one
sentence -- one facet vocabulary end to end, a title card that names what it truncates, a
catalogue whose pagination and search are total and literal, a Home whose anchor and badges tell
the truth, and four loader tables that stop being dropped or misreported -- and every one of
those was a claim about the ARTIFACT rather than about the fixture. So, like
`ops/m45_exit_criterion.py`, this script deliberately does not use the fixture: the twelve
measures below are all zero on `backend/tests/fixtures/make_bundle.py`'s eight titles, because
eight titles cannot carry a cross-department credit collision at scale, 584 tie groups, or an
extraction axis that joins nothing.

The twelve measures, in the order §7's table states them:

  1.  Distinct client credit keys against credit rows, over the titles carrying a
      cross-department (title, person, job) triple.        [today 7,918 collisions]
  2.  Every one of those payloads renders past CAST & CREW -- the key expression the card uses
      is injective over both the folded twelve and the full list.        [today throws on all]
  3.  The count line and the disclosure are in the card's markup, and the payloads that fold
      are counted rather than assumed.                                   [today absent]
  4.  dna_tag / dna_projected rows whose facet is not a vocabulary facet id.
                                                            [today 29,188 and 206,151]
  5.  `dna_tag LEFT JOIN dna_facet USING (version, facet) WHERE f.facet IS NULL`.  [today 31,540]
  6.  Chips whose rendered text contains the facet twice.                [today 92.5% of tags]
  7.  Facets resolving to `--ink-4` rather than a `--facet-*` property.  [today 10 of 11]
  8.  Shelf 1's anchor is observed, and the tier its headline names is the latest `tier_edit` --
      immediately and after a full refit.                               [today neither]
  9.  Home cards badged "no crowd data yet" whose `e_source` is not `cold_tower`.
                                                            [today 111 of 130]
  10. The catalogue paged end to end with a row rewritten between pages: union == full set,
      nothing duplicated, nothing missing.                   [today 3/3, and 61/61 with an UPDATE]
  11. max(projected term weight) < min(extracted term weight).           [today 2.40 vs 1.00]
  12. Tables shipped and unaccounted for, with `title_company` counted rather than skipped.
                                                            [today 47,607 rows skipped]

MEASURE 2 IS NOT THE PLAN'S FORM, AND THE BROWSER WAS NOT SKIPPED FOR CONVENIENCE. §7 writes it
as "each of the 1,216 payloads mounted through the real `TitleDetail` keyed each, in
vitest/jsdom". That harness did not exist when this script was written: `frontend/package.json`
shipped neither `jsdom` nor `@testing-library/svelte`, and adding two devDependencies plus a
refreshed `package-lock.json` was an unrequested dependency change this milestone's surgical-diff
rule forbids. Finding 27 has since made the first of the two requested -- §6.7's drawer must drop
its log on close, the frame that would show the previous open's events is one round trip long, and
nothing but a mounted component can hold a response open for it -- so `jsdom` is here now.
`@testing-library/svelte` still is not, and mounting 1,216 payloads through the real card on every
run is the second half of the decision, which nothing has asked for. So measure 2 stays a
KEY-INJECTIVITY check over the payloads this script
dumps: for every title it asserts that `person_id + ':' + job` -- the expression
`TitleDetail.svelte` keys its `{#each}` on -- is injective over the first twelve credits and
over the whole list. That is exactly what Svelte 5's `each_key_duplicate` checks, at a cost
that lets it run on every change rather than once. The render itself is covered in a real
browser by `e2e/specs/04-title-card.spec.js::the worst cross-department titles open without a
console error`, which opens the three worst offenders and asserts nothing threw. Adopting §7's
stronger form later is a deliberate dependency decision, not an oversight here.

AND MEASURES 1 AND 2 ARE BOTH FINDING 5's -- the SQL half and the client half. §7's table
assigns no measure to finding 6 (the ordered `array_agg(character)`), and none is invented here:
nothing in this script reads `character`, because §6.0's card list does not name the field and
no surface renders it. Finding 6 is a determinism property of the payload and is closed by
`test_a_credit_is_one_row_per_person_and_job_across_department_spellings`, which reinserts the
rows in the opposite order. A heading citing a finding this script does not measure is the
instrument lying about its own coverage. [decision 197]

Run it against a live Postgres, with the bundle reachable:

    CORPUS_BUNDLE_DIR=/path/to/export_bundle/v20260828 \\
    TEST_DATABASE_URL=postgresql://... \\
      backend/.venv/Scripts/python ops/m49_exit_criterion.py

It creates and drops its own DATABASE, so it never runs against a household's data by
accident -- 0003 creates schemas of its own, so a search_path would not have isolated it.
Output is ASCII: Windows consoles crash on decorative glyphs.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import asyncpg  # noqa: E402
from spielplan.db import dna_terms, library, migrate, pool  # noqa: E402
from spielplan.home import rail, shelves  # noqa: E402
from spielplan.home import why as why_mod  # noqa: E402
from spielplan.importer import bundle as bundle_import  # noqa: E402
from spielplan.ledger import refit  # noqa: E402
from spielplan.ledger.hyperparams import DEFAULTS  # noqa: E402

# A bundle smaller than this is the fixture, whatever the directory is called. The refusal is
# the point of the script: M4.5's harness learned that a green run against `make_bundle.py`
# measures the fixture's opinion of the corpus, and every number in the table above is a
# property the fixture does not have. `make_bundle(pool_titles=700)` tops out at 708 titles;
# the export ships 19,071.
MIN_REAL_TITLES = 5_000

# `TitleDetail.svelte`'s CREDIT_FOLD. Restated here because measure 2 asks about the FOLDED
# slice as well as the whole list -- Svelte keys both renders, so a collision inside the first
# twelve throws before anyone opens the disclosure.
CREDIT_FOLD = 12

# §6.8: "a fixed colour per vocabulary facet (11)".
VOCABULARY_FACETS = 11

results: list[tuple[bool, str]] = []


def console(text: str) -> str:
    """`text` rendered in the encoding stdout actually has, escaping what it cannot carry.

    The same guard `ops/m45_exit_criterion.py` carries, and for the same reason: `check()`'s
    detail interpolates the importer's own finding messages, and dozens of the `report.fail` /
    `warn` / `note` literals in `backend/spielplan/importer/` carry an em dash. Under a cp850
    console, printing one raises UnicodeEncodeError from inside `print` -- a traceback where the
    diagnosis belongs, in the one script whose whole job is diagnosis.
    """
    encoding = sys.stdout.encoding or "ascii"
    return text.encode(encoding, "backslashreplace").decode(encoding)


def check(ok: bool, label: str, detail: str = "") -> bool:
    # Every line flushed: the import below takes minutes and a block-buffered stdout on a
    # redirected pipe holds everything until the process ends.
    results.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {console(label)}", flush=True)
    if detail:
        for line in detail.splitlines():
            print(f"         {console(line)}", flush=True)
    return bool(ok)


@dataclass
class Actor:
    """What `home.shelves.build_home` reads off the signed-in user, and nothing else."""

    id: int
    name: str


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def markup(relative: str) -> str:
    """A component with its commentary removed.

    Measure 6 asks whether a component CONCATENATES the facet onto the term, and this
    codebase argues its decisions in comments next to the code that carries them --
    `ShelfRow.svelte` quotes the very expression it stopped using, in the sentence saying
    why. Scanning the raw file therefore reports the repair as the defect. Both comment
    forms are stripped, because the label lives in markup and the constants live in the
    `<script>` above it.
    """
    text = re.sub(r"<!--.*?-->", "", source(relative), flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def discard_staged_artifacts(artifacts_root: Path | None) -> None:
    """Remove the staging tree, and say so when it survives.

    `shutil.rmtree(..., ignore_errors=True)` is a no-op on Windows for any file still open, and
    a cleanup that can silently not happen is the same shape as a check that can silently not
    fail. Nothing here opens an `ArtifactStore`, so unlike M4.5's harness there is no npz handle
    to close first -- but the tree is still named when it survives.
    """
    if artifacts_root is None:
        return
    shutil.rmtree(artifacts_root, ignore_errors=True)
    if artifacts_root.exists():
        print(f"  NOTE      staged artifacts survived cleanup at {artifacts_root}", flush=True)


# --- the household this script measures Home with ----------------------------------------------


async def seed_profile(conn: asyncpg.Connection, *, version: str) -> dict[str, object] | None:
    """One account with a ledger, built so shelf 1 has exactly one candidate anchor.

    Measures 8 and 9 are about a payload, and a payload needs a person. Everything below is
    written through the same columns `backend/tests/test_home.py`'s fixture writes, against real
    corpus titles chosen by query rather than by literal id.

    THE ANCHOR IS CHOSEN BY ASKING THE APP. `because_anchor` ships only when some pair of the
    anchor's own terms covers `SECTION_FLOOR` unseen owned titles, and which pairs do is a
    property of the imported corpus. Candidates are therefore run through `why.best_pair` --
    the very function the shelf calls -- before one is seeded, so a suppressed shelf here is a
    real result rather than a harness that picked badly.

    THE DECOY IS THE FALSIFIER for finding 15. It is `seen` with a very high fitted `s` and
    `observed` FALSE, which is the state §7.2's Jellyfin sync leaves behind: without
    `AND ls.observed` in the anchor SELECT it wins the ORDER BY outright and Home says "Because
    you put X in C" about a title with zero observations.
    """
    user_id = await conn.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('m49-exit', 'admin') RETURNING id"
    )
    candidates = [
        int(r["id"])
        for r in await conn.fetch(
            """
            SELECT d.title_id AS id, count(DISTINCT d.term) AS terms
              FROM dna_tagged d
              JOIN title t ON t.id = d.title_id
             WHERE d.version = $1 AND t.kind = 'movie' AND t.is_owned AND d.tier = 'extracted'
             GROUP BY d.title_id
            HAVING count(DISTINCT d.term) >= 2
             ORDER BY count(DISTINCT d.term) DESC, d.title_id
             LIMIT 40
            """,
            version,
        )
    ]
    anchor = None
    for candidate in candidates:
        pool_terms = await why_mod.terms_for(conn, candidate, version=version)
        pair = await why_mod.best_pair(
            conn,
            user_id=user_id,
            kind="movie",
            version=version,
            anchor_id=candidate,
            pool=pool_terms,
            floor=shelves.SECTION_FLOOR,
        )
        if pair is not None:
            anchor = candidate
            break
    if anchor is None:
        return None

    # Enough observations for §5.2's fit to have something to say, and spread over the three
    # verdict values because a ledger of one class fits a degenerate model.
    rated = [
        int(r["id"])
        for r in await conn.fetch(
            "SELECT id FROM title WHERE kind = 'movie' AND is_owned AND id <> $1"
            " ORDER BY id LIMIT 24",
            anchor,
        )
    ]
    for i, title_id in enumerate([anchor, *rated]):
        await conn.execute(
            "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, $2, $3)",
            user_id, title_id, i % 3,
        )
    # ONLY the anchor is `seen`, so it is the one title that can satisfy shelf 1's two
    # predicates and the measure is about which TIER the sentence names rather than about where
    # the optimiser moved a title.
    await conn.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, $2, 'seen')",
        user_id, anchor,
    )
    await conn.execute(
        """
        INSERT INTO ledger_state (user_id, title_id, s, sigma, cdf, tier, kind, observed)
        VALUES ($1, $2, 3.0, 0.2, 0.98, 4, 'movie', true)
        """,
        user_id, anchor,
    )
    decoy = await conn.fetchval(
        "SELECT id FROM title WHERE kind = 'movie' AND is_owned AND id <> $1 ORDER BY id DESC"
        " LIMIT 1",
        anchor,
    )
    await conn.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, $2, 'seen')",
        user_id, decoy,
    )
    await conn.execute(
        """
        INSERT INTO ledger_state (user_id, title_id, s, sigma, cdf, tier, kind, observed)
        VALUES ($1, $2, 99.0, 0.2, 0.99, 5, 'movie', false)
        """,
        user_id, decoy,
    )
    # §6.3: "the most recent `tier_edit` decides where a title renders, and the model decides it
    # only when there is no edit" (`rank/board.py:20-27`). Tier 0 is the bottom of
    # `DEFAULT_TIER_SET`, which is as far from the fitted tier 4 as the set goes.
    await conn.execute(
        "INSERT INTO tier_edit (user_id, title_id, tier, via) VALUES ($1, $2, 0, 'drag_drop')",
        user_id, anchor,
    )
    return {"user_id": user_id, "anchor": anchor, "decoy": decoy}


def no_crowd_data(card: dict) -> bool:
    """`PosterCard.svelte`'s badge expression, in Python, over one shelf card.

    Restated rather than imported because the component is JavaScript and this is the payload
    contract it consumes. Its precedence is the whole of finding 18: `e_source` first, then
    `item_n`, and `placement` only as a last resort -- so a payload that omits the first two
    reaches the stamp `0008_placement.sql` writes on any title with a Backbone row and
    `item_n < 90`, which is a different question from "has this title any crowd data".
    """
    if card.get("e_source"):
        return card["e_source"] == "cold_tower"
    return card.get("item_n") == 0 or (
        card.get("item_n") is None and card.get("placement") == "cold_tower"
    )


def shelf_cards(payload: dict) -> list[dict]:
    return [
        card
        for shelf in payload.get("shelves") or []
        for section in shelf.get("sections") or []
        for card in section.get("items") or []
    ]


async def main() -> int:
    bundle_dir = os.environ.get("CORPUS_BUNDLE_DIR")
    if not bundle_dir:
        print("CORPUS_BUNDLE_DIR is unset. This script measures a REAL bundle on purpose --")
        print("every number M4.9 exists to move is a property of the export, not the fixture.")
        return 2
    root = Path(bundle_dir)
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        print("TEST_DATABASE_URL is unset.")
        return 2

    content = root / "content.sqlite"
    if not content.is_file():
        print(f"no content.sqlite under {root} -- that is not an export bundle.")
        return 2
    with sqlite3.connect(f"file:{content}?mode=ro", uri=True) as probe:
        shipped_titles = probe.execute("SELECT count(*) FROM title").fetchone()[0]
    if shipped_titles < MIN_REAL_TITLES:
        print(f"{root} ships {shipped_titles:,} titles -- that is the fixture, not the corpus.")
        print("Refusing on purpose: none of the twelve measures below is falsifiable on it.")
        return 2

    print(f"\nM4.9 exit criterion -- bundle {root.name} ({shipped_titles:,} titles)\n", flush=True)

    admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
    scratch = f"spielplan_m49_exit_p{os.getpid()}"
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {scratch}")
    finally:
        await admin.close()

    conn: asyncpg.Connection | None = None
    artifacts_root: Path | None = None
    try:
        conn = await asyncpg.connect(dsn.rsplit("/", 1)[0] + f"/{scratch}")
        # The app's own connection setup, not a bare connect: `db/pool.py` registers the
        # json/jsonb codec every caller depends on. M4.5's harness died three times here,
        # because `title_meta.payload` fails with "expected str, got dict" without it -- a
        # defect in the harness that read exactly like a defect in the importer.
        await pool._init_connection(conn)
        artifacts_root = Path(tempfile.mkdtemp(prefix="spielplan-m49-exit-"))
        await migrate.apply_all(conn)

        print("0. Import (§10)", flush=True)
        began = time.perf_counter()
        b = bundle_import.Bundle.open(root)
        rep = await bundle_import.import_bundle(conn, b, artifacts_root)
        check(
            rep.ok,
            f"the bundle imports clean ({time.perf_counter() - began:.0f}s)",
            "\n".join(
                f"FAIL {f.rule}: {f.message[:150]}"
                for f in rep.findings
                if f.severity == "fail"
            )[:800],
        )
        version = await dna_terms.active_version(conn)
        check(bool(version), f"the active DNA vocabulary is {version!r}")

        # --- 1. the client's credit key -------------------------------------------------
        print("\n1. Credit keys (§4.1 dedupe at read time; finding 5)", flush=True)
        crossing = [
            int(r["title_id"])
            for r in await conn.fetch(
                """
                SELECT title_id FROM (
                    SELECT title_id, person_id, job
                      FROM credit
                     GROUP BY title_id, person_id, job
                    HAVING count(DISTINCT department) > 1
                ) x GROUP BY title_id ORDER BY title_id
                """
            )
        ]
        collisions = await conn.fetchval(
            """
            WITH grouped AS (
                SELECT c.title_id, c.person_id, p.name, c.job
                  FROM credit c JOIN person p ON p.id = c.person_id
                 WHERE c.title_id = ANY($1)
                 GROUP BY c.title_id, c.person_id, p.name, c.job
            )
            SELECT COALESCE(sum(rows_out - keys), 0) FROM (
                SELECT count(*) AS rows_out,
                       count(DISTINCT person_id::text || ':' || job) AS keys
                  FROM grouped GROUP BY title_id
            ) t
            """,
            crossing,
        )
        check(
            int(collisions or 0) == 0,
            f"distinct client credit keys equal credit rows on all {len(crossing):,} "
            f"cross-department titles (collisions {int(collisions or 0):,})",
        )

        # --- 2. the card renders past CAST & CREW ---------------------------------------
        print("\n2. Key injectivity over the dumped payloads (finding 5)", flush=True)
        rendered, duplicated, with_credits, folded_titles = 0, [], 0, 0
        for title_id in crossing:
            credits = await library.credits_for(conn, title_id)
            if credits:
                with_credits += 1
            if len(credits) > CREDIT_FOLD:
                folded_titles += 1
            keys = [f"{c['person_id']}:{c['job']}" for c in credits]
            folded = keys[:CREDIT_FOLD]
            if len(set(keys)) == len(keys) and len(set(folded)) == len(folded):
                rendered += 1
            elif len(duplicated) < 6:
                duplicated.append(f"title {title_id}: {len(keys) - len(set(keys))} duplicate keys")
        check(
            rendered == len(crossing),
            f"every payload keys injectively, folded and whole: {rendered:,}/{len(crossing):,}",
            "\n".join(duplicated),
        )

        # --- 3. the count line ----------------------------------------------------------
        print("\n3. The credit count line (§6.0's count-line discipline; finding 7)", flush=True)
        # The card's MARKUP, not its file. This codebase argues its decisions in comments beside
        # the code that carries them, so a count line commented out and left standing as prose
        # satisfies a whole-file `in` -- the shape of the compose guard that once passed on a
        # file of pure comments. Measure 6 already reads through `markup()`; this one did not.
        #
        # And the population is `folded_titles`, not `crossing`. `with_credits == len(crossing)`
        # was reported as the measurement and cannot fail: every member of `crossing` is there
        # because it HAS credit rows, and `credit.person_id` is NOT NULL REFERENCES person(id),
        # so `credits_for`'s join drops none of them. It stays in the predicate as what it
        # actually is -- a liveness assertion about `credits_for`, which measure 2 needs because
        # an empty credit list keys injectively -- while the number the operator reads is the
        # one the disclosure exists for. A bundle on which nothing folds has not exercised the
        # disclosure and must say so. [M49-CARD-1]
        card_markup = markup("frontend/src/lib/components/TitleDetail.svelte")
        has_count = 'data-testid="credit-count"' in card_markup
        has_fold = "CREDIT_FOLD" in card_markup and "credits-disclosure" in card_markup
        check(
            has_count and has_fold and folded_titles > 0 and with_credits == len(crossing),
            f"the card names what it hides, on {folded_titles:,}/{len(crossing):,} payloads "
            f"that carry more than {CREDIT_FOLD} credits",
            f"count line in the markup: {has_count}; fold and disclosure: {has_fold}; "
            f"credits read for {with_credits:,}/{len(crossing):,} (liveness, not a card "
            f"measurement: every crossing title has a credit row by construction)",
        )

        # --- 4. and 5. the facet vocabulary ---------------------------------------------
        print("\n4-5. One facet vocabulary (§4.1 rule 1, §4.3; findings 1 and 2)", flush=True)
        facets = [
            r["facet"] for r in await conn.fetch(
                "SELECT facet FROM dna_facet WHERE version = $1 ORDER BY facet", version
            )
        ]
        check(
            len(facets) == VOCABULARY_FACETS,
            f"the shipped vocabulary declares {len(facets)} facets",
            " ".join(facets),
        )
        strays = {}
        for table in ("dna_tag", "dna_projected"):
            strays[table] = await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE version = $1 AND NOT (facet = ANY($2))",
                version, facets,
            )
        check(
            strays["dna_tag"] == 0 and strays["dna_projected"] == 0,
            f"rows carrying a facet outside the vocabulary: dna_tag {strays['dna_tag']:,}, "
            f"dna_projected {strays['dna_projected']:,}",
        )
        orphans = await conn.fetchval(
            """
            SELECT count(*) FROM dna_tag g
              LEFT JOIN dna_facet f ON f.version = g.version AND f.facet = g.facet
             WHERE f.facet IS NULL
            """
        )
        check(orphans == 0, f"dna_tag rows joining no dna_facet row: {orphans:,}")

        # --- 6. the chip prints its term once -------------------------------------------
        print("\n6. The chip's label (§4.3; finding 3)", flush=True)
        # The population that COULD double: a term already carrying its facet as a prefix. If a
        # component concatenates the two, every one of these renders `facet.facet.term`.
        prefixed = await conn.fetchval(
            "SELECT (SELECT count(*) FROM dna_tag WHERE term LIKE facet || '.%')"
            "     + (SELECT count(*) FROM dna_projected WHERE term LIKE facet || '.%')"
        )
        doubling = []
        for path in (
            "frontend/src/lib/components/TitleDetail.svelte",
            "frontend/src/lib/components/ShelfRow.svelte",
        ):
            text = markup(path)
            for match in re.finditer(r"\{\s*(\w+)\.facet\s*\}\s*\.\s*\{\s*\1\.term\s*\}", text):
                doubling.append(f"{path}: {match.group(0)}")
        doubled = prefixed if doubling else 0
        check(
            doubled == 0,
            f"chips rendering the facet twice: {doubled:,} of {prefixed:,} prefixed rows",
            "\n".join(doubling),
        )

        # --- 7. the palette -------------------------------------------------------------
        print("\n7. The palette (§6.8; finding 4)", flush=True)
        css = source("frontend/src/lib/design.css")
        defined = set(re.findall(r"--facet-([a-z0-9_-]+)\s*:", css))
        declared = re.search(
            r"const FACETS = new Set\(\[(.*?)\]\)",
            source("frontend/src/lib/home.svelte.js"),
            re.S,
        )
        check(declared is not None, "home.svelte.js still declares the facet set this reads")
        known = set(re.findall(r"'([a-z_]+)'", declared.group(1) if declared else ""))
        neutral = sorted(f for f in facets if f not in known or f not in defined)
        check(
            not neutral,
            f"facets resolving to --ink-4 rather than a --facet-* property: "
            f"{len(neutral)} of {len(facets)}",
            " ".join(neutral),
        )

        # --- 8. and 9. Home ---------------------------------------------------------------
        print("\n8-9. Shelf 1's anchor and the cold badge (findings 15, 16, 18)", flush=True)
        profile = await seed_profile(conn, version=version)
        if profile is None:
            check(False, "no owned movie's terms cover a shelf -- shelf 1 cannot be measured")
        else:
            user = Actor(id=int(profile["user_id"]), name="m49-exit")

            async def home() -> dict:
                payload = await shelves.build_home(
                    conn, user=user, kinds=["movie"], bundle_version=rep.bundle_version,
                    now_local=datetime.now(UTC),
                )
                # The toggle is OFF for every account by default (decision 117), and finding 18
                # is precisely that the two fields the badge reads were removed with it.
                return rail.redact(payload, show_model=False)

            def section_one(payload: dict) -> dict | None:
                for shelf in payload.get("shelves") or []:
                    if shelf["id"] != "because_anchor":
                        continue
                    return next((s for s in shelf["sections"] if s["kind"] == "movie"), None)
                return None

            observed_flags = []
            named_tiers = []
            for stage in ("immediately", "after refit_user"):
                if stage == "after refit_user":
                    report = await refit.refit_user(
                        conn, user_id=user.id, kind="movie", hp=DEFAULTS
                    )
                    check(report.fitted, f"the full refit fitted ({report.n_observed} observed)")
                payload = await home()
                section = section_one(payload)
                if section is None:
                    check(False, f"shelf 1 did not ship {stage}")
                    observed_flags.append(False)
                    # Unequal on purpose: a suppressed shelf names no tier, and a pair of Nones
                    # would compare equal and let the check below pass on an absent sentence.
                    named_tiers.append((None, "shelf 1 did not ship"))
                    continue
                anchor_id = int(section["anchor"]["title_id"])
                observed_flags.append(
                    bool(
                        await conn.fetchval(
                            "SELECT observed FROM ledger_state WHERE user_id = $1"
                            " AND title_id = $2",
                            user.id, anchor_id,
                        )
                    )
                    and anchor_id == int(profile["anchor"])
                )
                # Resolved per build, not once: `refit_user` writes `ledger_cutpoints`, and
                # decision 11 lets the tier SET itself change with K. What the measure claims is
                # that the headline names the tier the OWNER assigned -- index 0, the bottom of
                # whatever set is current -- not that a particular letter survived a refit.
                assigned = (await shelves.tier_set_of(conn, user_id=user.id, kind="movie"))[0]
                named_tiers.append((section["anchor"]["tier"], assigned))
            check(
                all(observed_flags),
                f"shelf 1's anchor is the observed title, {observed_flags}",
            )
            check(
                bool(named_tiers) and all(named == assigned for named, assigned in named_tiers),
                "the headline names the tier its owner assigned, immediately and after a "
                f"refit: {named_tiers}",
            )

            cards = shelf_cards(await home())
            truth = {
                int(r["title_id"]): r["e_source"]
                for r in await conn.fetch(
                    "SELECT title_id, e_source FROM title_prior WHERE bundle_version = $1",
                    rep.bundle_version,
                )
            }
            false_badges = [
                card
                for card in cards
                if no_crowd_data(card) and truth.get(int(card["title_id"])) != "cold_tower"
            ]
            check(
                not false_badges and bool(cards),
                f"cards badged 'no crowd data yet' whose e_source is not cold_tower: "
                f"{len(false_badges)} of {len(cards)}",
                "\n".join(
                    f"title {c['title_id']}: e_source={c.get('e_source')!r} "
                    f"item_n={c.get('item_n')!r} placement={c.get('placement')!r} "
                    f"(prior says {truth.get(int(c['title_id']))!r})"
                    for c in false_badges[:6]
                ),
            )

        # --- 10. pagination is a total order ---------------------------------------------
        print("\n10. Catalogue pagination (§6.0; finding 11)", flush=True)
        ties = await conn.fetchval(
            "SELECT count(*) FROM (SELECT year, lower(name) FROM title GROUP BY 1, 2"
            " HAVING count(*) > 1) t"
        )
        undated = await conn.fetchval("SELECT count(*) FROM title WHERE year IS NULL")
        expected = {
            int(r["id"]) for r in await conn.fetch(
                "SELECT id FROM title WHERE kind = ANY($1)", ["movie", "series"]
            )
        }
        page, offset, walked, rewrote = 500, 0, [], False
        while True:
            rows, _total = await library.list_titles(
                conn, kinds=["movie", "series"], limit=page, offset=offset
            )
            if not rows:
                break
            walked.extend(int(r["id"]) for r in rows)
            offset += page
            if not rewrote:
                # A rewrite that does NOT touch the sort key, which is the case the tie-break
                # exists for: the UPDATE writes new heap tuples, Postgres is free to return tied
                # rows in the new physical order, and an ORDER BY that is not a total order then
                # silently drops and repeats. Setting a column to itself is still a rewrite.
                await conn.execute(
                    "UPDATE title SET is_owned = is_owned WHERE id = ANY($1)",
                    [int(r["id"]) for r in rows[: min(50, len(rows))]],
                )
                rewrote = True
        walked_set = set(walked)
        duplicated_n = len(walked) - len(walked_set)
        missing = expected - walked_set
        check(
            duplicated_n == 0 and not missing and walked_set == expected,
            f"paged {len(walked):,} rows over {ties:,} tie groups and {undated:,} undated "
            f"titles: {duplicated_n:,} duplicated, {len(missing):,} missing",
        )

        # --- 11. the two term-weight bands cannot cross ----------------------------------
        print("\n11. Term weights (§4.1 rules 1 and 2; decision 188; finding 20)", flush=True)
        band = await conn.fetchrow(
            f"""
            SELECT max(w) FILTER (WHERE tier = 'projected') AS projected_max,
                   min(w) FILTER (WHERE tier = 'extracted') AS extracted_min
              FROM (SELECT d.tier AS tier, {dna_terms.TERM_WEIGHT} AS w
                      FROM dna_tagged d WHERE d.version = $1) x
            """,
            version,
        )
        projected_max, extracted_min = band["projected_max"], band["extracted_min"]
        check(
            projected_max is not None
            and extracted_min is not None
            and float(projected_max) < float(extracted_min),
            f"max projected term weight {projected_max} < min extracted {extracted_min}",
        )

        # --- 12. every shipped table accounted for ---------------------------------------
        print("\n12. The loader's ledger (§10; findings 29-32; decision 193)", flush=True)
        counts = {k: v for k, v in rep.table_counts.items() if not k.startswith("loaded:")}
        loaded = {k[7:]: v for k, v in rep.table_counts.items() if k.startswith("loaded:")}
        with sqlite3.connect(f"file:{content}?mode=ro", uri=True) as db:
            shipped = {
                r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if not r[0].startswith("sqlite_")
            }
        accounted = set(counts) | set(loaded) | set(rep.skipped_tables)
        unaccounted = sorted(shipped - accounted)
        check(
            not unaccounted,
            f"tables shipped and unaccounted for: {len(unaccounted)} of {len(shipped)}",
            f"unaccounted: {unaccounted}" if unaccounted else "",
        )
        company = await conn.fetchval("SELECT count(*) FROM title_company")
        check(
            company > 0 and "title_company" not in rep.skipped_tables,
            f"title_company is loaded rather than skipped: {company:,} rows",
        )
    except Exception as exc:
        # The block that measures reports the run it could not finish. An import that lands no
        # rows does not raise -- `importer/bundle.py` returns its report instead -- so every
        # measure after it would fall through into a None comparison and the operator would get
        # a traceback where the sentence naming the cause belongs, and lose the tally.
        check(False, f"the run stopped on {type(exc).__name__}: {exc}", traceback.format_exc())
    finally:
        if conn is not None:
            await conn.close()
        discard_staged_artifacts(artifacts_root)
        admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        finally:
            await admin.close()

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    for ok, label in results:
        if not ok:
            print(f"  FAILED: {console(label)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
