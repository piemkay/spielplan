"""M5's umbrella as something CI runs: each clause of `ops/m5_exit_criterion.py`, one test apiece.

§12's M5 row is measured in-process on the fixture by `ops/m5_exit_criterion.py`, whose checks CI
runs as the tests below (decision 465). The script's `main()` builds one install on a scratch
database and runs the nine checks in the plan's order; each test here builds the same install on
this test's own database, composes only the steps its clause needs - the add and its walk for
clauses two to five, seven and nine - and asserts the check's own verdict, with its detail as the
message. What the script prints and what these assert are therefore one reading, and a check that
went vacuous would go red here on the next push rather than on the next hand-run.

SHARE NO INSTALL BETWEEN TESTS. Under `-n 16` xdist's default `load` distribution spreads one
module's tests over workers, so a module-scoped install would be built once per worker and a clause
test could land where the check it depends on never ran. So nothing here is module-scoped and no
test reads another's state; the whole-run order is `main()`'s alone.

THE SAFETY NET IS SET FIRST, as `test_m51_exit_criterion.py` sets it: the shipped stage table and
the app's Jellyfin client factory are recorded so `monkeypatch` puts back whatever the script
replaces, and the default fetcher refuses until the script points it at its routing transport, as
it must for the worker's drain to reach the doubles - so a script that stopped doing so fails here
on that refusal rather than crawling the open web from whatever machine runs the suite.

NO TEST HERE SKIPS beyond conftest's no-database skip, because a registered test that skipped fails
the release workflow's coverage gate (`ops/coverage_gate.py`).

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import os
import re
import sys
from pathlib import Path

import asyncpg
import pytest

from spielplan import worker
from spielplan.acquire import pipeline
from spielplan.connectors import registry
from spielplan.core.config import settings
from spielplan.dna import verify
from spielplan.llm import consensus, extract
from tests import conftest
from tests.fixtures import make_bundle as fx

SCRIPT = conftest.ROOT.parent / "ops" / "m5_exit_criterion.py"


def _load(monkeypatch):
    """The script as a module of its own, with every process-wide thing it touches put back by
    `monkeypatch`: the variables it and `ops/m53_exit_criterion.py` set outright at import, the
    `sys.path` entries both insert, and its entry in `sys.modules`, which `dataclasses` needs while
    its `Install` is declared under `from __future__ import annotations`."""
    for name, placeholder in {
        "SESSION_SECRET": "a-session-secret-this-test-puts-back-afterwards",
        "SECRETS_KEY": "a-secrets-key-this-test-puts-back-afterwards",
        "PUBLIC_URL": "http://localhost:8080",
    }.items():
        monkeypatch.setenv(name, os.environ.get(name, placeholder))
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("m5_exit_criterion_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def install(db, pg_url, tmp_path, monkeypatch):
    """The script's own install on this test's database, closed again afterwards.

    What `main()` does to the process before it builds one is done here through `monkeypatch`: a
    DATA_DIR of this test's own (the raw store's root and, under it, the staged artifacts), the
    app's pool pointed at this process's database, every connector variable an operator exported
    taken away and a working directory with no `.env` in it - conftest's `app` fixture's two halves,
    for its reason.
    """
    monkeypatch.setattr(pipeline, "STAGES", pipeline.STAGES)

    async def crawl(_conn):
        raise AssertionError("the script's drain asked for the real default fetcher")

    monkeypatch.setattr(pipeline, "_default_fetcher", crawl)
    monkeypatch.setattr(registry, "make_client", registry.make_client)
    work = tmp_path / "a-run-of-its-own"
    work.mkdir()
    monkeypatch.setenv("DATA_DIR", str(work))
    monkeypatch.setenv("DATABASE_URL", pg_url)
    for name in conftest._connector_seed_variables_present():
        monkeypatch.delenv(name, raising=False)
    neutral = work / "no-dot-env"
    neutral.mkdir()
    monkeypatch.chdir(neutral)
    module = _load(monkeypatch)
    settings.cache_clear()
    ctx = None
    try:
        ctx = await module.build_install(db, fx.make_bundle(tmp_path / "bundle"), work)
        yield module, ctx
    finally:
        if ctx is not None:
            await module.close_install(ctx)
        settings.cache_clear()


async def test_check_one_a_new_add_reaches_ready_unattended(install):
    """Clause one: an `ItemAdded` for a film the bundle lacks, delivered by the Jellyfin double's
    emitter, swept once its window closes and walked by one drain, is `ready` at stage 10 as an
    acquired title in the app's half of the id space - with the plugin's delivery the only request
    the app received between the add and `ready`, and no admin action's sentence anywhere on the
    board or the task."""
    module, ctx = install

    ok, detail = await module.check_one(ctx)

    assert ok, detail
    assert ctx.walk.report.stages_run == [stage.name for stage in pipeline.STAGES], detail
    assert ctx.walk.app_requests == [module.DELIVERY], detail


async def test_check_two_the_dna_stages_run_in_the_walk_and_an_upgraded_park_re_enters_at_five(
    install,
):
    """Clause two: in that walk stage 5 stored the augmented pack under the walk's task key and
    run, every extracted tag carries its evidence, stage 7's detail is exactly the refusals stage 6
    filed under the run and is not empty, and stage 8 projected the film's keyword through the
    fixture's alias map (decisions 461, 462, 463) - and a fixture title parked at stage 6 on a
    missing pack, its deadline passed, re-enters at stage 5 on the next drain and stores one
    (decision 467)."""
    module, ctx = install
    await module.walk_the_add(ctx)

    ok, detail = await module.check_two(ctx)

    assert ok, detail


async def test_check_two_goes_red_when_the_workers_own_drain_walks_with_no_run(install, monkeypatch):
    """Clause two reads the run the worker's tick opened, so a drain job that withholds it fails it.

    M5's first review found check 2 green through a seam only the script had: its drain opened a
    `job_run` row and handed it to `pipeline.drain` itself, while `worker._acquisition_drain` - the
    one production caller - passed none, so on every real install stage 6 filed its refusals under no
    run and stage 7 recorded `rejected: {}` while the certificate printed PASS. Decision 468 gave the
    worker's drain its tick's run; this holds the instrument to measuring THAT drain. The shipped
    drain row is put back to the shape the review found - the same job, walking with the tick's run
    withheld - and clause two has to say that stage 7 recorded nothing. [M5 review cycle 1,
    M5-EXIT-C1-01, M5-C1-GATE-01]
    """
    module, ctx = install

    def withholding_the_run(job):
        shipped = job.run

        async def run():
            withheld = worker._JOB_RUN.set(None)
            try:
                return await shipped()
            finally:
                worker._JOB_RUN.reset(withheld)

        return dataclasses.replace(job, run=run)

    monkeypatch.setattr(worker, "JOBS", tuple(
        withholding_the_run(job) if job.name == module.DRAIN_JOB else job for job in worker.JOBS
    ))
    await module.walk_the_add(ctx)

    ok, detail = await module.check_two(ctx)

    assert not ok, detail
    assert "stage 7: detail {} beside dna_reject for the run {}" in detail, detail


async def test_check_three_the_walk_is_retried_once_with_the_violation_named(install):
    """Clause three: the walk made two `llm_call` rows, and the double's own record of the second
    request carries the violated rule and the offending term - one no `dna_term` row holds - as the
    provider received it."""
    module, ctx = install
    await module.walk_the_add(ctx)

    ok, detail = await module.check_three(ctx)

    assert ok, detail


async def test_check_four_a_second_violation_fails_for_good_and_writes_nothing(install):
    """Clause four: the walked title retried from stage 6 on the board, with the double repeating
    its violation, fails stage 6 for good after two requests - the board saying no retry is coming,
    the task closed, the tier exactly as the walk wrote it - and the next tick asks nobody."""
    module, ctx = install
    await module.walk_the_add(ctx)

    ok, detail = await module.check_four(ctx)

    assert ok, detail


async def test_check_four_goes_red_when_the_failing_retry_rewrites_the_tier(install, monkeypatch):
    """Clause four's "writes nothing" sees a tier deleted and re-inserted with the same values.

    `consensus.store_title` replaces a title's tier (`_REPLACE`), and the double's tags are fixed by
    the pack and the vocabulary, so a stage 6 that wrote the violating second answer's verified tags
    and then failed leaves rows equal to the walk's in every value column: only `id` and
    `created_at` move. A check that compared values alone printed PASS over that write. This is the
    review's mutant, run in-process: the stubborn run's last verdict is stored as the tier before
    the run reports `violated`. [M5 review cycle 1, M5-EXIT-C1-02]
    """
    module, ctx = install
    await module.walk_the_add(ctx)
    verdicts, rewritten = [], []
    shipped_verify, shipped_run = verify.verify_payload, extract._run

    async def remembering(*args, **kwargs):
        verdicts.append(await shipped_verify(*args, **kwargs))
        return verdicts[-1]

    async def writes_then_fails(conn, fetcher, asked, planned, pass_index, **kwargs):
        run = await shipped_run(conn, fetcher, asked, planned, pass_index, **kwargs)
        if run.status == extract.VIOLATED:
            tags = verdicts[-1].tags.get(asked.title_id, [])
            merged, n_runs = consensus.merge_passes(
                {consensus.pass_id_for(planned.provider, pass_index): tags}
            )
            rewritten.append(await consensus.store_title(conn, asked.title_id, asked.voc.version,
                                                         merged, n_runs=n_runs))
        return run

    monkeypatch.setattr(verify, "verify_payload", remembering)
    monkeypatch.setattr(extract, "_run", writes_then_fails)

    ok, detail = await module.check_four(ctx)

    assert rewritten and rewritten[0] > 0, f"the mutant never wrote the tier: {rewritten}\n{detail}"
    assert not ok, detail


async def test_check_five_the_thin_title_is_in_the_flywheel_when_drain_returns(install):
    """Clause five: `GET /api/admin/flywheel`, read the moment the walk's drain returned, holds the
    walked title's open thin-facet row naming exactly the facets its tier left unnamed."""
    module, ctx = install
    await module.walk_the_add(ctx)

    ok, detail = await module.check_five(ctx)

    assert ok, detail


async def test_check_six_a_launch_takes_exactly_its_rows_and_leaves_bundle_projection_alone(install):
    """Clause six: of three open rows on fixture bundle titles, the two launched through the route
    are the only rows running and their titles the only ones due; the next drain re-extracts both
    from stage 5 with the batch's provider, the third row and its title are untouched, and both
    launched titles' projected rows are byte-identical afterwards (decision 463)."""
    module, ctx = install

    ok, detail = await module.check_six(ctx)

    assert ok, detail


async def test_check_seven_a_re_derive_keeps_the_shipped_and_the_household_correction(install):
    """Clause seven: the fixture's shipped composer correction and a household one written through
    the correction editor's route each stand after a board retry from stage 3 - over a derive that
    wrote the source's wrong composer and had the ledger replace it."""
    module, ctx = install
    await module.walk_the_add(ctx)

    ok, detail = await module.check_seven(ctx)

    assert ok, detail


async def test_check_eight_a_burst_of_episode_adds_files_one_task_for_the_show(install):
    """Clause eight: twelve episode `ItemAdded`s for one series inside one window add exactly one
    task to the queue, the show's, and none keyed on an episode."""
    module, ctx = install

    ok, detail = await module.check_eight(ctx)

    assert ok, detail


async def test_check_nine_the_cap_parks_and_never_auto_retries(install):
    """Clause nine: with the cap set to the month's own spend - the walk's two calls - a fresh add
    walks to stage 6 and parks `over spend cap` with no request to the double, and on the next tick,
    made due with its deadline untouched, it parks there again and asks nobody."""
    module, ctx = install
    await module.walk_the_add(ctx)

    ok, detail = await module.check_nine(ctx)

    assert ok, detail


# --- the statements the script sends --------------------------------------------------------------

# `test_m53_exit_criterion.py`'s reader, restated rather than imported for this suite's convention on
# helpers shared across files: the verbs the script sends, anchored and case-sensitive, so a check's
# title or a sentence of prose is not read as a statement.
_STATEMENT = re.compile(r"^(?:SELECT|INSERT|UPDATE|DELETE|WITH)\b")

# The floor this guard was written with. If the script comes to build its statements another way,
# this reads almost nothing and reports green: widen the reader rather than lower the floor.
_FLOOR = 30


def _statements(path: Path) -> list[tuple[int, str]]:
    """Every whole SQL statement the script holds as a literal, with the line it sits on.

    An f-string's pieces are skipped because a piece is not a statement; the script interpolates
    only its scratch database's name, into the two statements `main()` sends before the schema
    exists.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    interpolated = {
        id(piece)
        for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
        for piece in ast.walk(node) if isinstance(piece, ast.Constant)
    }
    found = [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in interpolated and _STATEMENT.match(node.value)
    ]
    return sorted(found)


async def test_every_statement_the_exit_script_sends_prepares_against_the_schema(db):
    """The one defect class a script can carry that reading its source cannot reach.

    M5.3's script shipped a statement naming a column the schema never carried, and its check
    reported a failure it never measured (M53-EXIT-01). PREPARE and not execute, for that test's
    reason: the server parses the statement, resolves every name and infers every parameter type,
    which is the whole of what a wrong identifier can fail, while touching no row.
    """
    statements = _statements(SCRIPT)
    assert len(statements) >= _FLOOR, (
        f"{SCRIPT.name} holds {len(statements)} SQL literals, under this guard's floor of {_FLOOR}"
    )
    refused = []
    for line, statement in statements:
        try:
            await db.prepare(statement)
        except asyncpg.PostgresError as refusal:
            flat = " ".join(statement.split())
            refused.append(
                f"ops/{SCRIPT.name}:{line}: {type(refusal).__name__}: {refusal}\n      {flat[:96]}"
            )
    assert not refused, (
        "the exit criterion sends a statement this schema cannot answer, so the check that sends "
        "it reports a failure it never measured:\n  " + "\n  ".join(refused)
    )
