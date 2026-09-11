"""M4.13's exit criterion, measured against a bundle the corpus actually built.

M4.13 is not in §12, so it measures its own stated goal:

    a fit knows the basis it was computed in -- the bundle version is threaded rather than
    inferred, §10's invariant has production callers, "basis" widens from bundle to
    bundle + K + coordinate, and the jobs around the fit stop losing, inventing or
    mis-scaling the work it rests on

against a real export, in a database it creates and drops. **It refuses to run on the fixture
on purpose.** None of the numbers below is reachable from `make_bundle.py`: the fixture ships
one `cold_mask` row and the corpus 2,879, no 14,397-row Backbone whose row norms span four
orders of magnitude, and no thin population for §5.1's middle line to apply to. The one row is
there so the pipeline this script measures end to end is exercised in CI as well (cs-01,
`test_the_shared_fixtures_flagged_row_is_swept_and_served_from_the_tower`); a scale model of a
population is still not the population, which is why this script exists.

SIX CHECKS AND ONE REPORT (decision 240). Pass is 6/6; the report prints numbers and attaches
no verdict. The numbering is the plan's, so its table and this run read side by side -- which
is why there is no check 4 here and a `4. REPORT ONLY` heading in its place.

  1. after a models-only re-import, every fitted (user, kind) carries the STAGED bundle
     version, `load_cache` returns a FitCache across the flip, and the first tap reports
     `refit = False`                                    (was: the outgoing version, a refused
                                                         cache, and a full fit on the tap path)
  2. with the active row flipped under a pinned store, the scoring entrypoint answers 409 and
     the refit entrypoint raises, both naming the loaded and the active version, and
     `assert_matches` has production callers                (was: no caller anywhere in the app)
  3. owned titles served with a zero coordinate: 0                (was 182, 159 at item_n >= 90)
  4. REPORT ONLY, no verdict pending decision 236: the distribution of
     ((1-g)*||e_hat||)/(g*||E||) over the rows §5.1's middle line applies to, and
     max abs(user_score) for a fitted member         (read p10 83.3 / median 530.8 / p90 5,542.9
                                                      and -15.85..+8.22 when the plan measured it)
  5. a board fitted at K=7 with 200 tier edits, re-read at K=12 and at K=4: mean rendered-tier
     meaning shift under 5 percentile points, under 10% of a subsequent drop stream landing in
     a tier the refitted board renders for nobody, and `load_cache` None immediately after
     `save_tier_set`                                   (was 20-28 points and 30-60% in tension)
  6. `DELETE FROM title` for a title carrying a verdict, a duel and a `session_outcome` raises
     ForeignKeyViolationError with all three rows still present, while a title carrying only
     derived rows still deletes                   (was: the observations cascaded away silently)
  7. `resolve_embeddings(standard_embeddings(...))` equals `serve.coordinates(...)` for every
     title                     (differed by ||de|| 1.81 / 0.72 / 0.35 at item_n 6 / 30 / 55)

WHAT THIS SCRIPT IS NOT ALLOWED TO DO. M4.5's close-out found three of that harness's four
failures were the harness talking to a different database, or a different code path, than the
app. So every number below is read back out of the scratch database or computed by the app's
own function -- never out of a variable this script set -- the connection is set up through
`db/pool.py` the way the app sets one up, and the four traps `ops/m45_exit_criterion.py`
documents are copied rather than rediscovered: a dedicated DATABASE and not a `search_path`
(0003 creates schemas of its own), `pool._init_connection` and not a bare `asyncpg.connect`
(the json codec), `validate_for_install` and not `validate` (the install-state refusals live
there), and a sample that REQUIRES the row it is measuring (one drawn without it reports a
harness artefact in the shape of the defect).

OWNERSHIP IS THIS SCRIPT'S OWN, AND IT HAS TO BE. §7.2 makes `is_owned` a Jellyfin fact
re-derived per install, and a scratch database has no Jellyfin -- so checks 3, 5 and 7 would
have no library at all. The owned set is therefore built to CONTAIN the population each defect
lived in (the cold-masked rows with support, a warm slice and a thin slice, evenly spaced over
the id range so the choice is deterministic rather than a favourite), and check 3 FAILS rather
than passes when that population turns out to be empty. The plan's "182 owned" is a count from
the owner's own install; what is asserted here is the zero, with the population it was measured
over printed beside it.

Run it against a live Postgres, with the bundle reachable:

    CORPUS_BUNDLE_DIR=/path/to/export_bundle/v20260828 \\
    TEST_DATABASE_URL=postgresql://... \\
      backend/.venv/Scripts/python ops/m413_exit_criterion.py

It creates and drops its own DATABASE, so it never runs against a household's data by accident
-- 0003 creates schemas of its own, so a search_path would not have isolated it -- and it
stages the artifacts into a temporary DATA_DIR that it removes. Output is ASCII: Windows
consoles crash on decorative glyphs.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# Set before the first `spielplan` import, because `settings()` is `lru_cache`d and decision 181
# made §2's required config a refusal at construction: a process with no SESSION_SECRET raises
# rather than signing cookies with a constant from the public repository. A throwaway key is all
# the encryption here means, and DATABASE_URL is deliberately left alone -- every connection
# below names the scratch database explicitly. DATA_DIR is set later, once the staging tree
# exists, and the cache cleared there: check 2 goes THROUGH `worker._active_store`, which reads
# `settings().artifacts_dir`, rather than around it.
os.environ["SESSION_SECRET"] = "m413-exit-criterion-session-secret-not-a-real-one"
os.environ["SECRETS_KEY"] = "m413-exit-criterion-secrets-key-not-a-real-one"
os.environ.setdefault("PUBLIC_URL", "http://localhost:8080")

import asyncpg  # noqa: E402
import numpy as np  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from spielplan import worker  # noqa: E402
from spielplan.api import artifacts as artifacts_api  # noqa: E402
from spielplan.api import rate as rate_api  # noqa: E402
from spielplan.core.config import settings  # noqa: E402
from spielplan.db import migrate, pool  # noqa: E402
from spielplan.importer import bundle as bundle_import  # noqa: E402
from spielplan.ledger import observations, refit  # noqa: E402
from spielplan.ledger.hyperparams import load as load_hp  # noqa: E402
from spielplan.models.artifacts import ArtifactStore  # noqa: E402
from spielplan.placement import reconcile as placement  # noqa: E402
from spielplan.rank import tiers as rank_tiers  # noqa: E402
from spielplan.scoring import backbone as bb  # noqa: E402
from spielplan.scoring import foldin, serve  # noqa: E402

# The owned library this run measures over. Three populations, because the three checks that
# read it ask different questions: the cold-masked rows with support ARE defect 5 (`cold_mask`
# true, E written as zeros, `item_n` large enough that the gate rounds to 1.0), the thin slice
# is the set §5.1's middle line exists for and the only one the blend report can measure, and
# the warm slice is the control that must keep reading its Backbone row outright.
N_COLD_OWNED = 240
N_WARM_OWNED = 120
N_THIN_OWNED = 120

# Enough labels for a fit that means something, few enough to stay inside §5.3's "seconds".
VERDICTS = 48
EDITS = 200
DROPS = 60
K_FITTED, K_GROWN, K_SHRUNK = 7, 12, 4

# Decision 11's control writes one set per user. Twelve is `rank/tiers.py`'s MAX_TIERS and four
# is a shrink past most of the edits -- the two directions defect 8 was simulated in.
TIER_SETS: dict[int, tuple[str, ...]] = {
    K_FITTED: ("F", "D", "C", "B", "A", "A+", "S"),
    K_GROWN: ("F", "D-", "D", "C-", "C", "C+", "B", "B+", "A-", "A", "A+", "S"),
    K_SHRUNK: ("no", "ok", "good", "S"),
}

# `_SAFE_VERSION` in `importer/bundle.py` accepts this alphabet, and each string becomes a
# directory name under the artifacts root as well as an `artifact_bundle` key. Two of them: the
# models-only bundle check 1 imports, and the staged-but-never-imported version check 2 flips to.
SECOND_BUNDLE_SUFFIX = "-m413-models"
# Not a suffix of the active version, deliberately: check 2 asserts that the refusal names BOTH
# versions, and a name that contains the other one makes "both are in this line" true of a line
# that mentions only one. A constant that shares no substring with either real version is the
# cheapest way for that assertion to mean what it says.
WINDOW_BUNDLE_VERSION = "m413-window-basis"

results: list[tuple[bool, str]] = []


class PreconditionFailed(RuntimeError):
    """A seeding write or a setup read this run cannot continue past.

    Named rather than raised bare, and carrying the precondition in its message, because the
    exit code is FOR the precondition: `ops/m4_exit_criterion.py` counted 24 refused writes as
    verdicts written and printed the number it had asked for. `main`'s handlers turn one of
    these into failed checks with the sentence attached, so a run that could not be set up says
    which precondition broke instead of ending in a traceback where the diagnosis belongs.
    """


def console(text: str) -> str:
    """`text` rendered in the encoding stdout actually has, escaping what it cannot carry.

    The same guard the three scripts before this one carry, and here it is load-bearing twice
    over. The measured values below interpolate the importer's own finding messages -- 51 of
    the `report.fail` literals in `backend/spielplan/importer/` carry an em dash -- AND the
    corpus's own title names, of which 104 of v20260828's 19,071 leave the OEM code page, and
    checks 3, 6 and 7 each name a title. Under `PYTHONIOENCODING=cp850`, printing one raises
    UnicodeEncodeError from inside `print`: the script dies on the line it was reporting from
    and the operator gets a traceback where the diagnosis belongs. `backslashreplace` names the
    codepoint rather than dropping it, the same way the static guard's own message survives its
    own subject. [M4.8 ti-non-ascii-in-console-output-violates-the-projects-own-rule]
    """
    encoding = sys.stdout.encoding or "ascii"
    return text.encode(encoding, "backslashreplace").decode(encoding)


def check(ok: bool, label: str, measured: str, detail: str = "") -> None:
    """One line per numbered check, carrying the numbers and not only the verdict.

    The verdict is the FIRST argument, as in the three scripts before this one:
    `test_static_contracts.py::test_no_milestone_exit_check_has_a_constant_predicate` reads
    `check`'s first positional argument and reports a truthy literal there as a verdict settled
    before the run, so a signature taking the number first would make every call here look like
    the defect that guard exists to catch.

    Exactly six of these are recorded, which is what makes the published score mean "6/6":
    `ops/m45_exit_criterion.py` counts every assertion it happens to make, so its denominator
    drifts with the harness rather than with the criterion. Flushed per line, because the
    import below takes minutes and a block-buffered stdout on a redirected pipe holds
    everything until the process ends -- a harness whose whole purpose is to report cannot
    report from a buffer.
    """
    results.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {console(label)}", flush=True)
    print(f"         {console(measured)}", flush=True)
    if detail:
        for line in detail.splitlines():
            print(f"         {console(line)}", flush=True)


def note(text: str) -> None:
    """A printed reading with no verdict attached. The report section's line, and nothing else's.

    Deliberately not `check(True, ...)`: a summary recorded as a PASS is a certificate rather
    than a measurement, it counts toward the published score, and it is one of the two shapes
    `test_no_milestone_exit_check_has_a_constant_predicate` exists to report. [M4.8 finding 19]
    """
    for line in text.splitlines():
        print(f"         {console(line)}", flush=True)


def require(ok: bool, precondition: str) -> None:
    """Stop the run on a refused seeding write or a refused setup read, naming what broke."""
    if not ok:
        raise PreconditionFailed(precondition)


class Captured(logging.Handler):
    """Every log line the app emits while one check runs.

    Check 2's scoring refusal puts the two versions in the LOG and the operator's sentence in
    the 409 body -- `api/rate.py::_assert_active_basis` logs `assert_matches`' message and
    raises `RESTART_REQUIRED` -- so "names both versions" is a claim about a log record, and is
    read as one rather than asserted against a response that deliberately does not carry it.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def __enter__(self) -> Captured:
        logging.getLogger().addHandler(self)
        return self

    def __exit__(self, *_exc: object) -> None:
        logging.getLogger().removeHandler(self)


def identity_token(kind: str | None, imdb_id: str | None, tmdb_id: int | None) -> str:
    """Decision 162's per-row identity, in the two spellings `importer/validate.py` parses.

    `imdb:<imdb_id>` where the exporter had one, `tmdb:<tmdb_id>:<kind>` where it did not --
    written to the letter of `_validate_identity`'s own comparison, because a token this harness
    spells differently would fail the import for a reason that is about the harness.
    """
    if imdb_id:
        return f"imdb:{imdb_id}"
    return f"tmdb:{'' if tmdb_id is None else tmdb_id}:{kind or ''}"


def identity_array_name() -> str:
    """The array name `importer/validate.py` looks for, read from it rather than restated."""
    from spielplan.importer import validate as validator

    return str(validator.IDENTITY_ARRAY)


def stage_window_bundle(artifacts_root: Path, source: str, version: str) -> Path:
    """A second STAGED version carrying the same artifacts, for §10's window and nothing else.

    Check 2 needs two versions and a pinned store, not two imports: what it measures is the gap
    between a flip and the restart §10 demands. Minting its own means it does not inherit check
    1's outcome -- on the first run of this script check 1 was refused for a reason of its own and
    check 2 then reported "this install has one bundle row", which is a harness telling you about
    a harness.
    """
    target = artifacts_root / version
    if target.exists():
        shutil.rmtree(target)
    try:
        shutil.copytree(artifacts_root / source, target, copy_function=os.link)
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(artifacts_root / source, target)
    return target


def stage_models_only(source: Path, target: Path, version: str, spine: dict[int, Any]) -> Path:
    """A second bundle: the same artifacts under a new version, no content, and an identity array.

    Decision 162 fixes the order -- content seeds once, models re-import -- so §10's swap is
    only ever reachable through a bundle with no `content.sqlite`, and `Bundle.kind` reads that
    absence rather than a flag an operator sets. This is `test_import_integration.py`'s recipe
    at corpus scale: the identity record copied with a new `bundle_version`, and the artifacts
    tree carried.

    THE `title_identity` ARRAY IS WRITTEN HERE BECAUSE NO CORPUS BUNDLE CARRIES ONE, and this run
    is how that was found out. Decision 162 requires "an identity column row-aligned to its title
    ids so a corpus-side re-identification is caught rather than trusted", and
    `importer/validate.py` makes its absence a hard FAIL on a models-only bundle -- which is
    correct, and which means v20260828 cannot be re-imported models-only at all: `mdc
    export-bundle` does not write the array, so §10's swap sequence is unreachable with any bundle
    the corpus has built. That is an exporter ask and not something to route around, so what
    happens here is the exporter's one line and nothing more: the tokens are built from the
    INSTALL's own spine, which is the corpus's own rows carried over verbatim at the seed, so the
    array asserts nothing this bundle did not already say. Said out loud because a harness that
    manufactures a fact is the failure mode M4.5's close-out was about -- and the fact manufactured
    here is not the one under test: check 1 measures the STAMP, and decision 162's identity check
    has tests of its own.

    Hard links rather than a byte copy. `artifacts/` is 205 MB on v20260828 and `import_bundle`
    copies it AGAIN into the staging root, so copying here would move 410 MB to measure a stamp;
    `os.link` is an ordinary NTFS operation and needs no privilege. A filesystem that refuses it
    falls back to the copy rather than failing a run over an optimisation. `backbone.npz` is the
    one file that must NOT stay a link: a hard link is the same inode, so writing the rewritten
    archive over it would write into the corpus's own export. It is unlinked first, deliberately.
    """
    target.mkdir(parents=True, exist_ok=True)
    identity = json.loads((source / "BUNDLE.json").read_text(encoding="utf-8"))
    identity["bundle_version"] = version
    (target / "BUNDLE.json").write_text(json.dumps(identity), encoding="utf-8")
    artifacts = target / "artifacts"
    if artifacts.exists():
        shutil.rmtree(artifacts)
    try:
        shutil.copytree(source / "artifacts", artifacts, copy_function=os.link)
    except OSError:
        shutil.rmtree(artifacts, ignore_errors=True)
        shutil.copytree(source / "artifacts", artifacts)

    with np.load(artifacts / "backbone.npz", allow_pickle=False) as archive:
        arrays = {name: archive[name] for name in archive.files}
    ids = [int(t) for t in np.asarray(arrays["title_ids"]).reshape(-1)]
    unknown = [t for t in ids if t not in spine][:5]
    require(
        not unknown,
        f"backbone.npz names title ids this install does not carry ({unknown}), so no identity "
        "array can be written for them and the models-only import would be refused for a reason "
        "that is not the one under test",
    )
    arrays[identity_array_name()] = np.asarray(
        [identity_token(*spine[t]) for t in ids], dtype="<U48"
    )
    (artifacts / "backbone.npz").unlink()
    np.savez(artifacts / "backbone.npz", **arrays)
    return target


def discard_staging(data_root: Path | None, stores: list[ArtifactStore]) -> None:
    """Remove the staging tree -- after closing everything that still holds a handle into it.

    `shutil.rmtree(..., ignore_errors=True)` is a no-op on Windows for any file still open: the
    unlink raises PermissionError (WinError 32) and `ignore_errors` swallows it. Three kinds of
    handle reach into this tree by the time the run ends -- `Backbone.open` and
    `placement.warm_title_ids` both go through `ArtifactStore.npz`, which caches the NpzFile for
    the store's lifetime, and `features.build_vectors` opens `review_text_emb.npz` the same way.
    Measured during M4.5 on the only workstation that has the bundle: 11.4 MB survived every run
    forever, with no message. `_cache` is private and reached anyway, for the reason
    `pool._init_connection` is reached below -- a harness measures what the app does, and the
    app has no public way to let go. When the tree survives regardless, say which path: a
    cleanup that can silently not happen is the same shape as a check that can silently not
    fail. [M4.8 dd22-m45-exit-script-harness-hygiene]
    """
    for store in stores:
        for handle in store._cache.values():
            closer = getattr(handle, "close", None)
            if closer is not None:
                closer()
        store._cache.clear()
    bb.forget_cached()
    if data_root is None:
        return
    shutil.rmtree(data_root, ignore_errors=True)
    if data_root.exists():
        print(f"  NOTE      staged artifacts survived cleanup at {data_root}", flush=True)


def _dsn_from_env_test() -> str | None:
    """`.env.test`'s TEST_DATABASE_URL, read the way `backend/tests/conftest.py` reads it.

    The same convenience and the same limit as `ops/m411_exit_criterion.py`: an untracked file
    names the server, so the line below is the only place this script decides which host it is
    allowed to create a database on.
    """
    path = ROOT / ".env.test"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "TEST_DATABASE_URL":
            return value.strip()
    return None


# --- the install every check reads ------------------------------------------------------------


@dataclass
class Install:
    """One scratch database, one staging tree, one household, and the basis it serves.

    A class for the reason `ops/m411_exit_criterion.py`'s `Household` is one: the checks share
    an install, and the two that change it -- check 1 activates a second bundle, check 2 flips
    the active row and puts it back -- have to hand the next check a basis it can read. `adopt`
    is the whole of that: it re-reads the store, the Backbone and the hyperparameters the way a
    restarted process would, which is what §10's swap sequence ends in.
    """

    conn: asyncpg.Connection
    bundle_root: Path
    work: Path
    artifacts_root: Path
    version: str = ""
    store: ArtifactStore = field(default_factory=ArtifactStore.empty)
    backbone: Any = None
    hp: Any = None
    patrick: int = 0
    jenny: int = 0
    owned: list[int] = field(default_factory=list)
    cold_owned: list[int] = field(default_factory=list)
    warm_owned: list[int] = field(default_factory=list)
    thin_owned: list[int] = field(default_factory=list)
    support: dict[int, int] = field(default_factory=dict)
    cold: dict[int, bool] = field(default_factory=dict)
    rated: list[int] = field(default_factory=list)
    stores: list[ArtifactStore] = field(default_factory=list)

    async def adopt(self, version: str) -> None:
        """Re-pin this process on `version`, the way a restart re-pins the app's."""
        self.version = version
        self.store = ArtifactStore.open(self.artifacts_root / version, version)
        self.store.assert_not_broken()
        self.backbone = bb.load_for(self.store)
        self.hp, _notes = load_hp(self.store)
        self.stores.append(self.store)

    def embeddings(self) -> Any:
        """§5.1's coordinate for every title, in the basis this process is pinned on.

        One expression, one version: `standard_embeddings` takes both halves and they must
        describe the SAME bundle, which is the whole of data-01. Built per call rather than
        cached on the install, because `adopt` changes both halves at once and a source held
        across that would be the defect wearing a harness. [M4.13, data-01]
        """
        return observations.standard_embeddings(
            self.conn, self.backbone, bundle_version=self.version
        )

    def unrated_owned_movie(self) -> int:
        """An owned movie nobody has rated yet -- the subject of a first tap or a delete."""
        for title_id in self.owned:
            if title_id not in self.rated:
                return title_id
        raise PreconditionFailed(
            "every owned movie carries a verdict, so no check can use one that does not"
        )


def _spread(values: np.ndarray, wanted: int) -> list[int]:
    """`wanted` ids spread evenly over `values`, deterministically.

    Evenly spaced rather than the first N: corpus ids are assigned by the export's own order,
    so the lowest ones are a slice of one region of the catalogue and the highest another. The
    choice has to be reproducible between runs -- a sample drawn at random makes two runs
    disagree about a count and neither of them wrong -- and it must not be a favourite.
    """
    if values.size == 0 or wanted <= 0:
        return []
    step = max(1, int(values.size) // wanted)
    return [int(v) for v in values[::step][:wanted]]


async def _movie_ids(conn: asyncpg.Connection, ids: list[int]) -> np.ndarray:
    """Those of `ids` that are movies in this install, ascending.

    §4.1 rule 5 partitions the ranking by kind and `refit_user` reads one board at a time, so a
    population mixed across the partition would measure two boards and report one number.
    """
    rows = await conn.fetch(
        "SELECT id FROM title WHERE id = ANY($1::int[]) AND kind = 'movie' ORDER BY id", ids
    )
    return np.asarray([int(r["id"]) for r in rows], dtype=np.int64)


async def installed_spine(conn: asyncpg.Connection) -> dict[int, Any]:
    """The install's `title` rows in the shape an identity token is checked against.

    The same columns `importer/bundle.py::installed_spine` reads, minus the name it does not need,
    so the tokens written into the second bundle are compared against exactly the rows they were
    built from.
    """
    rows = await conn.fetch("SELECT id, kind, imdb_id, tmdb_id FROM title")
    return {int(r["id"]): (r["kind"], r["imdb_id"], r["tmdb_id"]) for r in rows}


async def _make_active(conn: asyncpg.Connection, version: str) -> None:
    """Flip the active row, which is §10's step 4 and the only thing check 2 needs of it.

    Two statements rather than the importer's, because what check 2 is about is the WINDOW
    between a flip and the restart §10 demands: the importer's own flip is inside a transaction
    with a rebuild that would refuse, which is exactly the path this check must not take.
    """
    await conn.execute(
        "UPDATE artifact_bundle SET state = 'superseded' WHERE state = 'active' AND version <> $1",
        version,
    )
    await conn.execute("UPDATE artifact_bundle SET state = 'active' WHERE version = $1", version)


def assert_matches_callers() -> list[str]:
    """Every CALL of `assert_matches` under `backend/spielplan`, with its line.

    The plan writes this as `grep -rn assert_matches backend/spielplan`; read with `ast` instead,
    because the grep's answer on this tree is dominated by prose. Four docstrings and a module
    header argue about this invariant at length -- `models/artifacts.py`, `worker._active_store`,
    `api/rate.py::_assert_active_basis` -- and a guard that counted those would have gone green
    on the tree where the method had no caller at all, which is the state M4.13 opened in. A
    call is an `ast.Call` on an attribute of that name, and nothing else is.
    """
    out: list[str] = []
    for path in sorted((ROOT / "backend" / "spielplan").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr == "assert_matches":
                out.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    return out


# --- 0. the install --------------------------------------------------------------------------


async def build_install(conn: asyncpg.Connection, bundle_root: Path, work: Path) -> Install:
    """Import the real bundle, give the household a library, and rate part of it.

    Every refusal here is a PRECONDITION and not a check: a run that cannot import the bundle
    has measured nothing, and saying so in one sentence is the difference between this script
    and a traceback. The seeding goes through `observations.record_verdict` rather than §6.1's
    routes -- the same exemption `ops/m45_exit_criterion.py` has for writing through the
    importer -- because what is under test here is the basis a fit is computed in, not the route
    that records a tap.
    """
    artifacts_root = work / "artifacts"
    ctx = Install(conn=conn, bundle_root=bundle_root, work=work, artifacts_root=artifacts_root)

    bundle = bundle_import.Bundle.open(bundle_root)
    require(
        bundle.content_db is not None,
        f"{bundle_root} carries no content.sqlite, so it is a models-only bundle and this run "
        "has no library to seed: CORPUS_BUNDLE_DIR must name a full export",
    )
    began = time.perf_counter()
    first = await bundle_import.import_bundle(conn, bundle, artifacts_root)
    fails = [f"{f.rule}: {f.message[:200]}" for f in first.findings if f.severity == "fail"]
    require(
        first.ok and not fails,
        "the bundle did not import, so there is no install to measure: " + "; ".join(fails[:3]),
    )
    print(f"         imported {bundle.version} in {time.perf_counter() - began:.0f}s", flush=True)
    await ctx.adopt(first.bundle_version)
    require(
        not ctx.backbone.is_empty,
        "the staged bundle carries no usable backbone.npz, so no coordinate in this run is the "
        "app's own",
    )

    # The three populations, read off the shipped arrays rather than off the loaded Backbone:
    # `cold_row_mask` is exactly what excludes a zero row from `row_of`, so after `Backbone.open`
    # those rows are indistinguishable from absent ones -- and telling them apart is the whole of
    # check 3. `store.npz` is the app's reader and caches the handle, which `discard_staging`
    # closes.
    npz = ctx.store.npz("backbone.npz")
    ids = np.asarray(npz["title_ids"]).astype(np.int64).reshape(-1)
    support = np.asarray(npz["item_n"]).astype(np.int64).reshape(-1)
    cold = bb.cold_row_mask(npz, ids.size)
    warm_enough = support >= bb.WARM_SUPPORT
    ctx.support = {int(t): int(n) for t, n in zip(ids, support, strict=True)}
    ctx.cold = {int(t): bool(flag) for t, flag in zip(ids, cold, strict=True)}

    ctx.cold_owned = _spread(await _movie_ids(conn, [int(t) for t in ids[cold & warm_enough]]),
                             N_COLD_OWNED)
    ctx.warm_owned = _spread(await _movie_ids(conn, [int(t) for t in ids[~cold & warm_enough]]),
                             N_WARM_OWNED)
    ctx.thin_owned = _spread(await _movie_ids(conn, [int(t) for t in ids[~cold & ~warm_enough]]),
                             N_THIN_OWNED)
    ctx.owned = sorted({*ctx.cold_owned, *ctx.warm_owned, *ctx.thin_owned})
    require(
        bool(ctx.cold_owned) and bool(ctx.thin_owned),
        "this bundle has no cold-masked row with support, or no row below WARM_SUPPORT, so "
        "checks 3 and 4 have no population and would pass by measuring nothing: "
        f"cold-with-support {int((cold & warm_enough).sum())}, thin {int((~cold & ~warm_enough).sum())}",
    )
    await conn.execute("UPDATE title SET is_owned = true WHERE id = ANY($1::int[])", ctx.owned)

    # The sweep, as §5.3 triggers it once ownership is known. Through `reconcile` and not through
    # `classify_warm` beside it: the sweep classifies first on its own (every scope that can place
    # a title does), and a second caller here would be a second answer to "who is excused" -- the
    # question defect 5 lived inside, where a title was excused by support while carrying no
    # coordinate to be excused for.
    sweep = await placement.reconcile(
        conn, ctx.store, bundle_version=ctx.version, scope="owned_missing"
    )
    priors = await serve.materialise_priors(conn, ctx.backbone, bundle_version=ctx.version)
    print(
        f"         owned {len(ctx.owned)} movies (cold-masked {len(ctx.cold_owned)}, warm "
        f"{len(ctx.warm_owned)}, thin {len(ctx.thin_owned)}); stamped warm {sweep.warm}, "
        f"demoted {sweep.demoted}; placed {sweep.placed}, parked {sweep.parked_thin}, failed "
        f"{sweep.failed}; "
        f"priors {priors.written}, uncoordinated owned {len(priors.uncoordinated_owned)}",
        flush=True,
    )

    ctx.patrick = await conn.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('Patrick', 'admin') RETURNING id"
    )
    ctx.jenny = await conn.fetchval(
        "INSERT INTO app_user (name, role) VALUES ('Jenny', 'member') RETURNING id"
    )

    # A board with labels in all three verdict classes, drawn across all three populations: a
    # fit over one class has no ordering to learn, and a fit over warm titles alone would leave
    # §5.1's middle line -- the set this milestone is about -- out of the basis it is fitted in.
    subjects = [
        title_id
        for group in (ctx.cold_owned, ctx.warm_owned, ctx.thin_owned)
        for title_id in group[: VERDICTS // 3]
    ]
    for position, title_id in enumerate(subjects):
        await observations.record_verdict(
            conn, user_id=ctx.patrick, title_id=title_id, value=(position * 7) % 3
        )
        ctx.rated.append(title_id)
    for position, title_id in enumerate(subjects[:12]):
        await observations.record_verdict(
            conn, user_id=ctx.jenny, title_id=title_id, value=(position * 5) % 3
        )
    seeded = await conn.fetchval("SELECT count(*) FROM verdict")
    require(
        seeded == len(subjects) + 12,
        f"the seeding wrote {seeded} verdicts where {len(subjects) + 12} were recorded, so a "
        "write was refused and this run would measure a board nobody rated",
    )
    print(f"         seeded {seeded} verdicts over {len(subjects)} titles", flush=True)
    return ctx


# --- 1. the stamp names the basis the fit was computed in --------------------------------------


async def check_one(ctx: Install) -> tuple[bool, str, str]:
    """§10 step 3 is "a FULL Personal Ledger MAP refit" against the STAGED bundle.

    The three facts are one fact, read at the three places it has to hold. `_rebuild_ledger_refit`
    ignored the version it was handed: `standard_embeddings` passed none, so `_placement_pairs`
    took its `$2 IS NULL` branch and joined `b.state = 'active'` -- during a pre-flip rebuild the
    OUTGOING bundle -- and `refit_all` stamped the fit from `active_bundle_version`, the outgoing
    version again. So the one step whose whole purpose is to re-express every fitted number in
    the new basis read the old placements and claimed the old version (measured
    ||v_step3 - v_correct|| = 0.397 against ||v_correct|| = 0.782). After the flip `load_cache`
    then refused that fit on its stamp, so the first tap per (user, kind) refitted on the request
    path against the still-old in-process Backbone and stamped THAT mixed-basis fit with the new
    version, which `load_cache` trusted until the next nightly (||v_tap - v_correct|| = 0.643).

    `lock = False` on the cache read: `FOR UPDATE` outside a transaction is a lock released
    before it is used, and what is being asked here is whether the row is acceptable, not who
    holds it. [M4.13, data-01]
    """
    conn = ctx.conn
    outgoing = ctx.version
    reports = await refit.refit_all(
        conn, ctx.hp, embeddings=ctx.embeddings(), bundle_version=outgoing
    )
    fitted = [r for r in reports if r.fitted]
    require(
        bool(fitted),
        "the first refit fitted no board at all, so nothing in this check could be stale: "
        + "; ".join(f"{r.user_id}/{r.kind}: {r.error}" for r in reports[:4]),
    )
    before = sorted({r["bundle_version"] for r in await conn.fetch(
        "SELECT bundle_version FROM ledger_fit"
    )})

    second = stage_models_only(
        ctx.bundle_root, ctx.work / "second-bundle", outgoing + SECOND_BUNDLE_SUFFIX,
        await installed_spine(conn),
    )
    staged = bundle_import.Bundle.open(second)
    require(
        staged.content_db is None and staged.version != outgoing,
        f"the second bundle is {staged.version!r} and carries content {staged.content_db!r}; "
        "decision 162 makes a re-import models-only and it must be a different version",
    )
    began = time.perf_counter()
    report = await bundle_import.import_bundle(conn, staged, ctx.artifacts_root)
    fails = [f"{f.rule}: {f.message[:200]}" for f in report.findings if f.severity == "fail"]
    require(
        report.ok and not fails,
        "the models-only re-import was refused, so the spec section 10 swap never happened: "
        + "; ".join(fails[:3]),
    )
    took = time.perf_counter() - began
    await ctx.adopt(staged.version)

    rows = await conn.fetch(
        "SELECT user_id, kind, bundle_version, n_observed FROM ledger_fit ORDER BY user_id, kind"
    )
    stamped = sorted({r["bundle_version"] for r in rows})
    cache = await refit.load_cache(
        conn, user_id=ctx.patrick, kind="movie", hp=ctx.hp, lock=False
    )

    tap = ctx.unrated_owned_movie()
    await observations.record_verdict(conn, user_id=ctx.patrick, title_id=tap, value=2)
    ctx.rated.append(tap)
    answer = await refit.update_incrementally_reporting(
        conn, user_id=ctx.patrick, kind="movie", title_ids=[tap], hp=ctx.hp,
        embeddings=ctx.embeddings(),
    ) or {}

    ok = (
        bool(rows)
        and stamped == [staged.version]
        and cache is not None
        and answer.get("applied") is True
        and answer.get("refit") is False
    )
    return ok, (
        f"before the re-import {len(rows)} fit(s) stamped {before}; after it stamped {stamped} "
        f"(staged {staged.version!r}, re-import {took:.0f}s); load_cache after the flip returned "
        f"{'a FitCache' if cache is not None else 'None'}; first tap on title {tap} "
        f"applied={answer.get('applied')!r} refit={answer.get('refit')!r} "
        f"reason={answer.get('reason')!r}"
    ), ""


# --- 2. a stale basis refuses, on both entrypoints ---------------------------------------------


async def check_two(ctx: Install) -> tuple[bool, str, str]:
    """§10's invariant, in the window between a flip and the restart §10 demands.

    "No process may score or refit with a loaded bundle version different from the active row" --
    and until M4.13 nothing in the app called the method that says so. This process pins its
    store and its Backbone at boot, so between a flip made in another process and the restart,
    `app.state.backbone` is the OUTGOING basis while every `title_placement` row, every
    `user_vector` and every `ledger_fit` stamp the flip made visible is the incoming one.

    Two entrypoints, because the two refusals are differently shaped and a household needs both.
    The scoring path answers 409 carrying `api/artifacts.py::RESTART_REQUIRED` -- the operator's
    own sentence, in the body -- and puts the two versions in the log; the refit path raises, and
    the worker's `_tick` logs the job as failed and retries on the next one. The interleaving is
    injected where it really happens rather than stubbed: `_active_store` loads the store and
    THEN resolves the active row, and an import running in the backend process can flip that row
    in between. [M4.13, arch-03]
    """
    conn = ctx.conn
    callers = assert_matches_callers()
    production = [c for c in callers if "models/artifacts.py" not in c]
    pinned = ctx.store
    # A version of this check's own, stamped `validated`: §10 flips to a bundle that validated,
    # and `artifact_bundle_one_active` is a partial unique index, so two actives are not a state
    # this check could reach even by mistake.
    incoming = WINDOW_BUNDLE_VERSION
    stage_window_bundle(ctx.artifacts_root, ctx.version, incoming)
    # An empty dict and not the string "{}": `pool._init_connection` registers the json codec, so
    # asyncpg encodes a jsonb parameter from a mapping and refuses a str. The harness talks to the
    # database the app talks to, which is the whole point of going through that function.
    # `kind = 'model'` and not the column default: 0015 puts a partial unique index on kind =
    # 'seed', which is how decision 162 keeps one record that content was ever seeded -- so a row
    # inserted without the kind collides with the seed's and this check would report a harness.
    await conn.execute(
        "INSERT INTO artifact_bundle (version, manifest, state, kind) "
        "VALUES ($1, $2::jsonb, 'validated', 'model') ON CONFLICT (version) DO NOTHING",
        incoming, {},
    )
    require(
        await conn.fetchval("SELECT count(*) FROM artifact_bundle") >= 2,
        "this install still has one bundle row, so there is no second version to flip to and "
        "the spec section 10 window cannot be entered",
    )

    scoring: HTTPException | None = None
    refusal: RuntimeError | None = None
    flipped: list[str] = []
    # Two handles on one method: the bound classmethod to call through, and the descriptor out of
    # `__dict__` to put back. Restoring the bound one would leave a classmethod's `cls` baked into
    # a class attribute -- it happens to still work, and a harness that leaves the app subtly
    # rewired is how a later check comes to measure this one's leftovers.
    real = ArtifactStore.load_active
    original = ArtifactStore.__dict__["load_active"]
    try:
        await _make_active(conn, incoming)
        with Captured() as captured:
            request = _request_with(pinned)
            try:
                await rate_api._assert_active_basis(request, conn)
            except HTTPException as exc:
                scoring = exc

        async def flips_under_us(connection: Any, artifacts_dir: Path) -> ArtifactStore:
            store = await real(connection, artifacts_dir)
            if not flipped:
                flipped.append(str(store.version))
                await _make_active(connection, ctx.version)
            return store

        ArtifactStore.load_active = flips_under_us  # type: ignore[assignment]
        try:
            await worker._active_store(conn)
        except RuntimeError as exc:
            refusal = exc
    finally:
        ArtifactStore.load_active = original  # type: ignore[assignment]
        # Whatever happened above, the rest of the run serves the version it was pinned on. A
        # check that leaves the install in its own intermediate state makes the next check's
        # failure name the wrong milestone.
        await _make_active(conn, ctx.version)

    detail = (scoring.detail if scoring is not None else {}) or {}
    names_both = [line for line in captured.lines if incoming in line and ctx.version in line]
    refusal_text = "" if refusal is None else str(refusal)
    ok = (
        scoring is not None
        and scoring.status_code == 409
        and isinstance(detail, dict)
        and detail.get("reason") == "bundle_swapped"
        and detail.get("message") == artifacts_api.RESTART_REQUIRED
        and bool(names_both)
        and refusal is not None
        and ctx.version in refusal_text
        and incoming in refusal_text
        and flipped == [incoming]
        and len(production) >= 2
    )
    return ok, (
        f"scoring entrypoint: {getattr(scoring, 'status_code', None)} {detail!r}; the log names "
        f"both versions on {len(names_both)} line(s); refit entrypoint raised "
        f"{type(refusal).__name__ if refusal else None}: {refusal_text[:160]}; the flip landed "
        f"inside the job after it loaded {flipped}; assert_matches is called from "
        f"{len(production)} production site(s) beside its own module: {production}"
    ), ""


def _request_with(store: ArtifactStore) -> Any:
    """The one attribute `_assert_active_basis` reads off a request: the pinned store.

    A stub rather than a TestClient because what is under test is the guard, not the routing:
    `api/rate.py` reaches `request.app.state.artifacts` and nothing else, and standing an ASGI
    app up would add a lifespan that re-pins the very store this check needs left stale.
    """
    from types import SimpleNamespace

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(artifacts=store)))


# --- 3. no owned title is served at a zero coordinate -----------------------------------------


async def check_three(ctx: Install) -> tuple[bool, str, str]:
    """Defect 5, which was invisible because every surface called it warm.

    `backbone.npz` writes E as zeros for every row `cold_mask` flags -- 2,879 of 14,397 on
    v20260828, max ||E|| 9.4e-14 -- and keeps the real coordinate in `E_hat`/`b_hat`, two arrays
    §4.3 never names. Read as a coordinate, a zero row is worse than an absent one: the personal
    term <v_u, e(t)> is exactly 0 for every user for ever, `item_n` is often large so the gate
    rounds to 1.0, `e_source` says 'backbone', and §12's M2 criterion -- which counts a title as
    coordinated whenever `e_source` is not 'none' -- passes it. `placement.warm_title_ids`
    excused 1,915 of them from the sweep that exists to place them.

    The population is asserted, not assumed: a run whose owned library happens to contain no
    cold-masked row would report zero offenders having measured nothing, which is the shape of
    the defect rather than its absence. [M4.13, cs-01]
    """
    coords = await serve.coordinates(ctx.conn, ctx.backbone, bundle_version=ctx.version)
    # The install's OWNED set, not the list this script owns on purpose: the defect's subject is
    # every title a household can be shown, and the bundle flags `is_owned` of its OWN -- 839
    # titles on v20260828, 718 movies and 121 series -- which the 480 built above (movies, drawn
    # off the Backbone's rows) overlaps rather than extends. So the number read back here is the
    # UNION of the two, and the union is the 1,288 the run printed and `docs/TESTING.md`
    # publishes as the denominator of its zero -- not 839 + 480, and not 1,288 + 480. Read back
    # out of the database for the reason `ops/m411_exit_criterion.py` reads everything that way.
    # [M4.13 cycle 1, M413-EXIT-05]
    owned = [int(r["id"]) for r in await ctx.conn.fetch(
        "SELECT id FROM title WHERE is_owned ORDER BY id"
    )]
    cold_population = [t for t in owned if ctx.cold.get(t, False)]
    zero: list[int] = []
    absent: list[int] = []
    for title_id in owned:
        coordinate = coords.get(title_id)
        if coordinate is None:
            absent.append(title_id)
        elif not np.any(coordinate.e):
            zero.append(title_id)
    with_support = [t for t in cold_population if ctx.support.get(t, 0) >= bb.WARM_SUPPORT]
    sources: dict[str, int] = {}
    for title_id in cold_population:
        coordinate = coords.get(title_id)
        name = "none" if coordinate is None else coordinate.e_source
        sources[name] = sources.get(name, 0) + 1
    still_warm = await ctx.conn.fetchval(
        "SELECT count(*) FROM title WHERE is_owned AND placement = 'warm' AND id = ANY($1::int[])",
        cold_population,
    )
    ok = not zero and not absent and bool(with_support)
    return ok, (
        f"owned {len(owned)} titles, of which {len(cold_population)} carry a cold-masked "
        f"Backbone row and {len(with_support)} of those ship item_n >= {bb.WARM_SUPPORT:g}; "
        f"served at a zero coordinate: {len(zero)}; served with no coordinate at all: "
        f"{len(absent)}; the cold-masked rows are sourced {sources} and {still_warm} of them are "
        f"still stamped placement = 'warm'"
    ), ("the offenders: " + str(sorted(zero + absent)[:20]) if (zero or absent) else "")


# --- 4. REPORT ONLY: the two halves of the blend -----------------------------------------------


async def report_four(ctx: Install) -> None:
    """The plan's check 4, printed as numbers with no verdict. Decision 240, pending 236.

    §5.1's middle line reads `e(t) = gate*E[t] + (1-gate)*e_hat(t)`, which only means "a blend"
    while the two halves are comparable quantities. On v20260828 they are not -- median ||E||
    over warm rows 0.184 against median ||E_hat|| 27.05, and over the rows the middle line
    actually applies to the weighted ratio ran p10 83.3 / median 530.8 / p90 5,542.9, so the cold
    half decides the personal term of every thin title at every gate the spec's own k produces.

    WHY THERE IS NO VERDICT HERE. §4.1 says the Backbone is "carried over verbatim", so whether
    E is meant to be unit-scale item factors or support-weighted is the CORPUS's contract, and
    the two candidate rescalings are different models of what e(t) means rather than two
    spellings of one. Decision 236 sends the question upstream and lands the measurement only; a
    threshold printed beside these numbers would be this app answering a question it has just
    said is not its to answer. Both arms are printed, because a distribution over an empty set is
    not a small number: `BlendReport.quantile` returns None rather than nan for exactly that
    reason, and a report that printed nothing would read like a bundle whose blend is balanced.
    """
    # §5.3's nightly fold-in, run here because `user_score` is its output and not the Ledger
    # refit's: the import's own rebuild ran it before a single verdict existed, so without this the
    # second half of the report reads "NOT MEASURED" on an install that has a fitted member.
    # `only_stale = False` is the nightly pass's own argument, not a way round the debounce.
    folded = await foldin.run(
        ctx.conn, ctx.backbone, bundle_version=ctx.version, only_stale=False
    )
    placed = await serve.placements(ctx.conn, bundle_version=ctx.version)
    blend = bb.blend_ratios(ctx.backbone, placed)
    scores = await ctx.conn.fetchrow(
        "SELECT count(*) AS n, min(score) AS low, max(score) AS high, max(abs(score)) AS worst "
        "FROM user_score WHERE user_id = $1 AND kind = 'movie'",
        ctx.patrick,
    )
    note(
        f"offered {blend.n_offered} placed title(s): {blend.n_measured} are in the set the "
        f"middle line applies to, {blend.n_warm} sit at or above WARM_SUPPORT (first line), "
        f"{blend.n_no_row} have no usable Backbone row (third line), {blend.n_degenerate} are "
        "degenerate (gate 0 or a zero warm norm)"
    )
    if blend.n_measured:
        note(
            f"((1-g)*||e_hat||)/(g*||E||) over those {blend.n_measured} rows: "
            f"p10 {blend.p10:.1f}, median {blend.median:.1f}, p90 {blend.p90:.1f}"
        )
    else:
        note(
            "NOT MEASURED: no title on this install has both a Backbone row below WARM_SUPPORT "
            "and a Cold Tower placement, so the middle line applies to nothing here"
        )
    if scores is not None and scores["n"]:
        note(
            f"the fold-in fitted {len(folded.refit)} pair(s) and wrote "
            f"{folded.scores_written} score(s); user_score for the member: {scores['n']} row(s), "
            f"{scores['low']:.2f}..{scores['high']:.2f}, max abs {scores['worst']:.2f}"
        )
    else:
        note(
            "NOT MEASURED: the fitted member has no user_score row, so there is no owned-score "
            "range to read the scale against"
        )
    note(
        "no verdict attached: decision 236 sends the rescaling upstream as a corpus-contract "
        "question, and these numbers are what the answer has to arrive with"
    )


# --- 5. a tier edit keeps its meaning across a K change ----------------------------------------


def _meaning(level: int, k: int) -> float:
    """Where a rendered tier sits in the person's distribution, in percentile points.

    The cumulative prior mass below the level plus half of the level's own share -- the centre
    of the band the level names. `observations._tier_shares` is the app's own mass, reached
    through the module rather than restated, for the reason `pool._init_connection` is: a second
    spelling of the mapping would let this measurement agree with itself while disagreeing with
    the code it measures. Private and reached anyway; `rescale_level` is the public half and is
    what actually maps the level.
    """
    shares = observations._tier_shares(k)
    return 100.0 * (float(shares[:level].sum()) + 0.5 * float(shares[level]))


async def check_five(ctx: Install) -> tuple[bool, str, str]:
    """Defect 8: `tier_edit.tier` was a raw index with no record of the K it was chosen under.

    Simulated in the plan with 200 edits: growing 7 -> 12 left every S edit rendered at tier 6 of
    12, emptied the top five model tiers and put 36 of 60 subsequent drops into tension (meaning
    shift mean 20.1, max 41.8 percentile points); shrinking 7 -> 4 clamped B..S into the top tier,
    280 of 320 titles. A mass-preserving rescale measured 3.0/11 points and 0/60.

    TWO THINGS MEASURED, AND THE SECOND ONE'S DEFINITION IS THIS SCRIPT'S OWN. The meaning shift
    is the app's arithmetic read back (`rescale_level` against `_tier_shares`), and it reproduces
    the plan's own simulation to the decimal: 7 -> 4 measures max 11.0 percentile points, which is
    the 11 in the plan's "shift 3.0/11". "In tension" is an operationalisation and is stated here
    rather than implied: a subsequent drop at level L is in tension when L is more than one tier
    away from EVERY level the person's kept history can be rendered at. That is what "empties the
    top five model tiers" means -- under the shipped clamp a 7-level history can never be rendered
    above tier 6 of 12, so a drop at 8, 9, 10 or 11 lands in a tier the board shows as empty and
    the fit has to invent the placement; under a mass-preserving rescale the history reaches
    {0, 2, 4, 7, 10, 11} and every level of the twelve is within one tier of one of them. Measured
    that way the clamp reports 19/60 (32%) and the rescale 0/60, which is the band the plan
    reports. The clamp figure is the one number in this check nothing here computes -- the run
    measures the rescale -- so it is arithmetic over DROPS, K_FITTED and K_GROWN rather than a
    reading, and `test_static_contracts.py` recomputes it from those constants for that reason:
    the pair that shipped was 20/60 (33%), which is a drop stream over `range(1, DROPS + 1)` and
    not the one below. [review cycle 2: M413-C2-DIM5-05] The drop stream spans the whole new
    set, because a person who has just chosen twelve tiers uses twelve.

    WHAT WAS TRIED FIRST AND WAS WRONG, because the correction is not obvious from the number: the
    first definition asked whether the REFITTED BOARD renders any title at L, and on a corpus-scale
    board that is unsatisfiable by construction rather than by defect. A 7-level history maps to at
    most 7 of 12 levels however it is mapped, and the 1,288-title board is dominated by titles the
    person has never rated, whose `s` clusters at mu -- so the first run measured "6 of 12 tiers
    rendered" and called 52% of the drop stream tense on a tree where the rescale is working. The
    board's own histogram is printed below as a reading for that reason, and gated on nothing.

    `load_cache` is asked immediately after each save, before the refit: a fit whose cut-points
    index a set of another length does not mean something slightly out of date, it means
    something else. [M4.13, dd06, ml01; decision 11]
    """
    conn, member = ctx.conn, ctx.patrick
    await rank_tiers.save_tier_set(conn, user_id=member, tier_set=TIER_SETS[K_FITTED])
    subjects = [t for t in ctx.owned if t not in ctx.rated][:EDITS]
    require(
        len(subjects) >= EDITS,
        f"only {len(subjects)} owned movies are free to edit and this check needs {EDITS}; a "
        "shorter edit history measures a different simulation than the plan's",
    )
    for position, title_id in enumerate(subjects):
        await observations.record_tier_edit(
            conn, user_id=member, title_id=title_id, tier=(position * 3) % K_FITTED
        )
    edits = await conn.fetch(
        "SELECT title_id, tier, n_levels FROM tier_edit WHERE user_id = $1 ORDER BY id", member
    )
    require(
        len(edits) == EDITS and all(r["n_levels"] == K_FITTED for r in edits),
        f"{len(edits)} tier edit(s) were written and "
        f"{sum(1 for r in edits if r['n_levels'] != K_FITTED)} of them do not carry K = "
        f"{K_FITTED}: the column that records the set an index was chosen under is the whole of "
        "this check",
    )
    fit = await refit.refit_user(
        conn, user_id=member, kind="movie", hp=ctx.hp, embeddings=ctx.embeddings(),
        bundle_version=ctx.version,
    )
    require(
        fit.fitted,
        f"the K = {K_FITTED} fit did not converge ({fit.error!r}), so there is no board to read "
        "a meaning shift against",
    )

    lines: list[str] = []
    shifts_ok: list[bool] = []
    tension_ok: list[bool] = []
    caches_ok: list[bool] = []
    for k_to in (K_GROWN, K_SHRUNK):
        saved = await rank_tiers.save_tier_set(conn, user_id=member, tier_set=TIER_SETS[k_to])
        cache = await refit.load_cache(
            conn, user_id=member, kind="movie", hp=ctx.hp, lock=False
        )
        caches_ok.append(cache is None)
        after = await refit.refit_user(
            conn, user_id=member, kind="movie", hp=ctx.hp, embeddings=ctx.embeddings(),
            bundle_version=ctx.version,
        )
        shifts = [
            abs(
                _meaning(
                    observations.rescale_level(
                        int(r["tier"]), k_from=int(r["n_levels"]), k_to=k_to
                    ),
                    k_to,
                )
                - _meaning(int(r["tier"]), int(r["n_levels"]))
            )
            for r in edits
        ]
        rendered = sorted({
            int(r["tier"])
            for r in await conn.fetch(
                "SELECT DISTINCT tier FROM ledger_state WHERE user_id = $1 AND kind = 'movie' "
                "AND observed AND tier IS NOT NULL",
                member,
            )
        })
        reachable = sorted({
            observations.rescale_level(int(r["tier"]), k_from=int(r["n_levels"]), k_to=k_to)
            for r in edits
        })
        drops = [round(i * (k_to - 1) / (DROPS - 1)) for i in range(DROPS)]
        tense = [
            level for level in drops
            if min(abs(level - reached) for reached in reachable) > 1
        ]
        mean_shift = float(np.mean(shifts))
        rate = len(tense) / float(DROPS)
        shifts_ok.append(mean_shift < 5.0)
        tension_ok.append(rate < 0.10)
        lines.append(
            f"K {K_FITTED} -> {k_to}: {saved.tier_edits_kept} edit(s) kept, load_cache "
            f"{'None' if cache is None else 'a FitCache'} before the refit, refit "
            f"{'converged' if after.fitted else 'FAILED ' + str(after.error)}; meaning shift mean "
            f"{mean_shift:.1f} max {max(shifts):.1f} percentile points; the history is "
            f"renderable at {reachable}, so {len(tense)}/{DROPS} drops ({100 * rate:.0f}%) land "
            f"more than one tier from anything it can say; the board's own observed tiers are "
            f"{rendered} (printed, not gated)"
        )
    still = await conn.fetchval("SELECT count(*) FROM tier_edit WHERE user_id = $1", member)
    ok = all(shifts_ok) and all(tension_ok) and all(caches_ok) and still == EDITS
    return ok, f"{still} edit(s) survive both K changes; " + "; ".join(lines), ""


# --- 6. observations survive a title delete ----------------------------------------------------


async def check_six(ctx: Install) -> tuple[bool, str, str]:
    """§10: "Ledger observations always survive re-import". The schema said the opposite.

    Every FK from an observation table to `title` was `ON DELETE CASCADE` until 0022, so one
    `DELETE FROM title` took the household's verdicts, duels, tier edits and resolved evenings
    with it and reported nothing. §4.2 makes those rows append-only and decision 174 makes Undo
    the only thing that removes one; a cascade is neither. RESTRICT is the smallest statement of
    that: the delete is refused, the operator finds out, and the derived tables -- the ones a
    refit rewrites anyway -- keep cascading, which is the second half asserted here.

    The subjects are read out of the database rather than remembered from the seeding: a title
    this script believes carries no tier edit, on a run where check 5 has just written 200 of
    them, is exactly how a harness measures its own bookkeeping. [M4.13, decision 239 item 2]
    """
    conn = ctx.conn
    kept = await conn.fetchval(
        "SELECT v.title_id FROM verdict v JOIN title t ON t.id = v.title_id "
        "WHERE t.kind = 'movie' ORDER BY v.title_id LIMIT 1"
    )
    partner = await conn.fetchval(
        "SELECT v.title_id FROM verdict v JOIN title t ON t.id = v.title_id "
        "WHERE t.kind = 'movie' AND v.title_id <> $1 ORDER BY v.title_id LIMIT 1",
        kept,
    )
    require(
        kept is not None and partner is not None,
        "no two rated movies exist, so the delete this check refuses has nothing to refuse over",
    )
    await observations.record_duel(
        conn, user_id=ctx.patrick, title_a=kept, title_b=partner, outcome="A",
        context="tier_queue",
    )
    session_id = await conn.fetchval(
        "INSERT INTO session (room_code, host_user_id, kind, bundle_version, state, ended_at) "
        "VALUES ('MX-4130', $1, 'movie', $2, 'resolved', now()) RETURNING id",
        ctx.patrick, ctx.version,
    )
    await conn.execute(
        "INSERT INTO session_outcome (session_id, chosen_title_id, approval_share, participants) "
        "VALUES ($1, $2, 1.0, 1)",
        session_id, kept,
    )

    refused: Exception | None = None
    try:
        async with conn.transaction():
            await conn.execute("DELETE FROM title WHERE id = $1", kept)
    except asyncpg.ForeignKeyViolationError as exc:
        refused = exc
    survivors = await conn.fetchrow(
        "SELECT (SELECT count(*) FROM verdict WHERE title_id = $1) AS verdicts,"
        " (SELECT count(*) FROM duel WHERE title_a = $1 OR title_b = $1) AS duels,"
        " (SELECT count(*) FROM session_outcome WHERE chosen_title_id = $1) AS outcomes,"
        " (SELECT count(*) FROM title WHERE id = $1) AS titles",
        kept,
    )

    # A title carrying only the rows a refit rewrites. Chosen by the database, and required to
    # carry at least one of them: a title with nothing attached would delete for want of a
    # cascade rather than because the derived tables still have one.
    derived = await conn.fetchval(
        """
        SELECT t.id FROM title t
         WHERE t.is_owned AND t.kind = 'movie'
           AND EXISTS (SELECT 1 FROM ledger_state s WHERE s.title_id = t.id)
           AND NOT EXISTS (SELECT 1 FROM verdict v WHERE v.title_id = t.id)
           AND NOT EXISTS (SELECT 1 FROM duel d WHERE d.title_a = t.id OR d.title_b = t.id)
           AND NOT EXISTS (SELECT 1 FROM tier_edit e WHERE e.title_id = t.id)
           AND NOT EXISTS (SELECT 1 FROM user_title u WHERE u.title_id = t.id)
         ORDER BY t.id DESC LIMIT 1
        """
    )
    require(
        derived is not None,
        "no owned movie carries derived rows and no observation, so the second half of this "
        "check -- that the derived tables still cascade -- has no subject",
    )
    before = await conn.fetchrow(
        "SELECT (SELECT count(*) FROM ledger_state WHERE title_id = $1) AS state,"
        " (SELECT count(*) FROM user_score WHERE title_id = $1) AS scores,"
        " (SELECT count(*) FROM title_placement WHERE title_id = $1) AS placements",
        derived,
    )
    dropped: str | None = None
    try:
        await conn.execute("DELETE FROM title WHERE id = $1", derived)
    except Exception as exc:                       # noqa: BLE001 - reported, not swallowed
        dropped = f"{type(exc).__name__}: {exc}"
    gone = await conn.fetchval("SELECT count(*) FROM title WHERE id = $1", derived)

    ok = (
        refused is not None
        and survivors["titles"] == 1
        and survivors["verdicts"] >= 1
        and survivors["duels"] >= 1
        and survivors["outcomes"] == 1
        and dropped is None
        and gone == 0
    )
    return ok, (
        f"title {kept} carries {survivors['verdicts']} verdict(s), {survivors['duels']} duel(s) "
        f"and {survivors['outcomes']} outcome(s): the delete raised "
        f"{type(refused).__name__ if refused else None} and every one of them is still there; "
        f"title {derived} carries {before['state']} ledger_state, {before['scores']} user_score "
        f"and {before['placements']} title_placement row(s) and no observation: it deleted "
        f"({gone} row(s) left, error {dropped!r})"
    ), ""


# --- 7. the fitted coordinate is the served coordinate -----------------------------------------


async def check_seven(ctx: Install) -> tuple[bool, str, str]:
    """Defect 7: the Ledger's basis skipped §5.1's gate blend for every low-support title.

    `standard_embeddings` composed `chain(backbone_embeddings(...), placement_embeddings(...))`,
    and `chain` says the first source with a row wins -- while `Backbone.embedding` returns
    `E[row]` for ANY covered title regardless of `item_n`. So the placement source was consulted
    only for titles with no Backbone row at all, and the fit ran in a basis the serving path does
    not use: 4,807 of the real bundle's 14,397 rows sit below WARM_SUPPORT, and on the fixture
    the two differed by ||de|| 1.81 at item_n 6, 0.72 at 30 and 0.35 at 55. §6.1's held-out
    instrument was therefore measuring a v the serving path never reads.

    Equality is asserted EXACTLY rather than within a tolerance: both sides are now the same
    call, `scoring.backbone.coordinate`, so anything but a bit-for-bit match means a second
    expression has appeared somewhere. Read in chunks because `_placement_pairs` takes an id
    array, and over every title rather than a sample -- the disagreement is a property of the
    thin population, and a sample that misses it reports a repair nobody made. [M4.13, dd02]
    """
    coords = await serve.coordinates(ctx.conn, ctx.backbone, bundle_version=ctx.version)
    ids = [int(r["id"]) for r in await ctx.conn.fetch("SELECT id FROM title ORDER BY id")]
    source = ctx.embeddings()
    compared = 0
    worst = 0.0
    worst_title: int | None = None
    disagreed: list[int] = []
    for start in range(0, len(ids), 2048):
        chunk = ids[start:start + 2048]
        matrix, embedded = await observations.resolve_embeddings(source, chunk)
        for offset, title_id in enumerate(chunk):
            served = coords.get(title_id)
            if (served is not None) != bool(embedded[offset]):
                disagreed.append(title_id)
                continue
            if served is None:
                continue
            compared += 1
            gap = float(np.max(np.abs(matrix[offset] - served.e)))
            if gap > worst:
                worst, worst_title = gap, title_id
    thin = sum(
        1 for title_id in coords
        if 0 < ctx.support.get(title_id, 0) < bb.WARM_SUPPORT
    )
    ok = compared > 0 and worst == 0.0 and not disagreed and thin > 0
    return ok, (
        f"compared {compared} of {len(ids)} title(s), {thin} of them below WARM_SUPPORT and so "
        f"in the set the two used to disagree on; largest ||de||_inf {worst:g}"
        + (f" at title {worst_title}" if worst_title is not None else "")
        + f"; titles one side has and the other does not: {len(disagreed)}"
    ), (f"the disagreements: {disagreed[:20]}" if disagreed else "")


# --- the run -----------------------------------------------------------------------------------


CHECKS: tuple[tuple[int, str], ...] = (
    (1, "the rebuild's fit is stamped with the bundle it was computed in"),
    (2, "a stale basis refuses on the scoring and on the refit entrypoint"),
    (3, "no owned title is served at a zero coordinate"),
    (5, "a tier edit keeps its meaning across a K change"),
    (6, "observations survive a title delete"),
    (7, "the coordinate the fit sees is the coordinate the app serves"),
)


async def measure(number: int, runner: Any, ctx: Install) -> None:
    """Run one numbered check, reporting a crash inside it as that check's failure.

    One check's crash fails that check and no other, and the denominator stays the criterion's
    six: a run that stops at three and prints "3/3 checks passed" is the failure mode an exit
    criterion exists to rule out. A refused precondition is reported without its traceback --
    the sentence IS the diagnosis, and a stack trace over it is what
    `ops/m4_exit_criterion.py`'s seeding taught this project to stop printing.
    """
    label = next(f"{n}. {title}" for n, title in CHECKS if n == number)
    try:
        ok, measured, detail = await runner(ctx)
    except PreconditionFailed as exc:
        check(False, label, f"PRECONDITION FAILED: {exc}")
        return
    except Exception as exc:                       # noqa: BLE001 - reported, not propagated
        check(
            False, label, f"the check stopped on {type(exc).__name__}: {exc}",
            traceback.format_exc(),
        )
        return
    check(ok, label, measured, detail)


async def main() -> int:
    bundle_dir = os.environ.get("CORPUS_BUNDLE_DIR")
    if not bundle_dir:
        print("CORPUS_BUNDLE_DIR is unset. This script measures a REAL export bundle on purpose:")
        print("the fixture ships no cold-masked row, no four orders of magnitude of Backbone row")
        print("norm and no thin population, so it cannot falsify anything M4.13 exists to fix.")
        return 2
    root = Path(bundle_dir)
    if not (root / "BUNDLE.json").is_file() or not (root / "artifacts").is_dir():
        print(f"CORPUS_BUNDLE_DIR={root} is not an export bundle: it must carry BUNDLE.json and")
        print("an artifacts/ directory. This script refuses to run on the fixture on purpose.")
        return 2
    dsn = os.environ.get("TEST_DATABASE_URL") or _dsn_from_env_test()
    if not dsn:
        print("TEST_DATABASE_URL is unset and .env.test does not supply it.")
        return 2

    print(f"\nM4.13 exit criterion -- bundle {root.name}\n", flush=True)

    # A dedicated DATABASE, not a schema: 0003 creates `display` and `review_store`, which are
    # database-global, so a search_path could not isolate this run from a household's data. Named
    # with this run's pid so two concurrent runs cannot drop each other's database.
    admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
    scratch = f"spielplan_m413_exit_p{os.getpid()}"
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {scratch}")
    finally:
        await admin.close()

    # The block that creates the database is the block that drops it. Everything that can fail
    # after the CREATE -- the mkdtemp, the connect, the codec registration this project has been
    # bitten by once, the migration -- happens inside the `try`, because the scratch name carries
    # this run's pid: pid-suffixing it (which is what stops two concurrent runs dropping each
    # other's database) removed the accidental second chance the next run's `DROP DATABASE IF
    # EXISTS` used to be. An orphan nothing will ever name again is a leak on the household's own
    # server. [M4.8 dd22-m45-exit-script-harness-hygiene]
    conn: asyncpg.Connection | None = None
    work: Path | None = None
    ctx: Install | None = None
    try:
        # A temporary DATA_DIR, removed in the `finally`. It is the artifacts root as well:
        # `settings.artifacts_dir` is `data_dir / "artifacts"`, and check 2 goes through
        # `worker._active_store`, which reads it. Staging into `ROOT/data/` would leave 410 MB of
        # extracted artifacts inside the working tree per run.
        work = Path(tempfile.mkdtemp(prefix="spielplan-m413-exit-"))
        os.environ["DATA_DIR"] = str(work)
        settings.cache_clear()
        conn = await asyncpg.connect(dsn.rsplit("/", 1)[0] + f"/{scratch}")
        # The app's own connection setup, not a bare connect: `db/pool.py` registers the json/jsonb
        # codec every caller depends on, and a harness that skips it measures a database the app
        # never talks to. Without it `title_meta.payload` fails with "expected str, got dict".
        await pool._init_connection(conn)
        await migrate.apply_all(conn)

        print("\n0. The install this run measures (spec section 10; decision 162)", flush=True)
        ctx = await build_install(conn, root, work)

        print("\n1. The stamp names the basis the fit was computed in (plan check 1)", flush=True)
        await measure(1, check_one, ctx)

        print("\n2. A stale basis refuses rather than scoring (plan check 2)", flush=True)
        await measure(2, check_two, ctx)

        print("\n3. Owned titles served at a zero coordinate (plan check 3)", flush=True)
        await measure(3, check_three, ctx)

        print("\n4. REPORT ONLY, no verdict: the blend's two halves (plan check 4)", flush=True)
        await report_four(ctx)

        print("\n5. A tier edit across a K change (plan check 5)", flush=True)
        await measure(5, check_five, ctx)

        print("\n6. A title delete against an observation (plan check 6)", flush=True)
        await measure(6, check_six, ctx)

        print("\n7. The fitted coordinate against the served one (plan check 7)", flush=True)
        await measure(7, check_seven, ctx)
    except Exception as exc:                       # noqa: BLE001 - reported, not propagated
        # Everything outside a check: the connect, the migration, the import, the seeding.
        # Reported as the failures they are, so the exit code stays non-zero and the run still
        # ends in a score rather than in a traceback where the sentence naming the cause belongs.
        # The handler catches everything on purpose: the documented failure of the block above is
        # a TypeError over an import that landed no rows, and narrowing this to `asyncpg.
        # PostgresError` -- the narrowing a reviewer proposes for an async DB harness -- would
        # leave the guard green while the run reverted to the bare traceback that loses the
        # sections below it and the tally.
        # [M4.8 review cycle 2: m48-rev2-m45-raises-where-m4-was-taught-to-report]
        stopped = f"the run stopped on {type(exc).__name__}: {exc}"
        trace = traceback.format_exc()
        for index, (number, title) in enumerate(CHECKS[len(results):]):
            check(False, f"{number}. {title}", stopped, trace if index == 0 else "")
    finally:
        if conn is not None:
            await conn.close()
        discard_staging(work, ctx.stores if ctx is not None else [])
        admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        finally:
            await admin.close()

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed}/{len(CHECKS)} checks passed, plus one report with no verdict", flush=True)
    for ok, label in results:
        if not ok:
            print(f"  FAILED: {console(label)}", flush=True)
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
