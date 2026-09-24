"""M5.1's exit criterion as something that has to run, and not only as something to read.

§12's M5.1 row states its criterion "with stages 2-8 declared no-ops", and `ops/m51_exit_criterion.py`
is the instrument it names. The script was written when those stages WERE declared no-ops, and it
drained the shipped pipeline with the real default fetcher. Then M5.3 gave stages 2, 3 and 4 bodies
and M5.5 gave stage 6 one, and nothing in this tree ran the script again: `test_static_contracts.py`
reads it as source text, which says nothing about what its drain walks. On a real bundle check 1
could no longer reach stage 10 -- stage 4 parked its invented film for want of reviews, after stage 2
had asked five keyless sources about it from the household's own address -- and the docstring's "THIS
SCRIPT SENDS NOTHING TO A THIRD PARTY" was false. [M5.5 review cycle 1, NBR-03, M55-DOC-06]

So check 1 is run here as the script runs it, through its own `build_install`, against the fixture
bundle. The script refuses the fixture in `main` on purpose -- its mint is only a measurement against
the corpus's 19,000 titles -- and nothing here says otherwise: what this measures is the pipeline the
script's install drains, which is the same pipeline on any bundle.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys

from spielplan.acquire import pipeline
from spielplan.core.config import settings
from tests import conftest
from tests.fixtures import make_bundle as fx

SCRIPT = conftest.ROOT.parent / "ops" / "m51_exit_criterion.py"


def _load(monkeypatch):
    """The script as a module of its own, with every process-wide thing it touches put back by
    `monkeypatch`: the three variables it sets at import, and its entry in `sys.modules`, which
    `dataclasses` needs while its `Install` is declared under `from __future__ import annotations`."""
    for name, placeholder in {
        "SESSION_SECRET": "a-session-secret-this-test-puts-back-afterwards",
        "SECRETS_KEY": "a-secrets-key-this-test-puts-back-afterwards",
        "PUBLIC_URL": "http://localhost:8080",
    }.items():
        monkeypatch.setenv(name, os.environ.get(name, placeholder))
    spec = importlib.util.spec_from_file_location("m51_exit_criterion_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


async def test_check_one_walks_the_ten_stages_with_stages_two_to_eight_declared_no_ops(
    db, tmp_path, monkeypatch
):
    """The script's own install and its own check 1: the injected item walks all ten stages to
    `stage = 10, status = 'ready'`, and no stage from 2 to 8 runs a body, fetches or can spend.

    THE SAFETY NET IS THIS TEST'S, AND IT IS SET FIRST. The shipped pipeline is recorded so that
    `monkeypatch` puts it back whatever the script replaces, and the default fetcher is one that
    refuses -- so a script that drained the shipped stages again fails here with stage 2's refusal in
    its detail, rather than crawling the open web from whatever machine runs the suite.
    """
    monkeypatch.setattr(pipeline, "STAGES", pipeline.STAGES)

    async def crawl(_conn):
        raise AssertionError("the script's drain asked for the real default fetcher")

    monkeypatch.setattr(pipeline, "_default_fetcher", crawl)
    work = tmp_path / "a-run-of-its-own"
    work.mkdir()
    monkeypatch.setenv("DATA_DIR", str(work))
    settings.cache_clear()
    module = _load(monkeypatch)
    root = fx.make_bundle(tmp_path / "bundle")
    try:
        ctx = await module.build_install(db, root, work)
        try:
            ok, detail = await module.check_one(ctx)
        finally:
            logging.getLogger("spielplan").removeHandler(ctx.log)
    finally:
        settings.cache_clear()

    assert ok, detail
    assert "stage 10 status ready" in detail, detail
    no_ops = [stage for stage in pipeline.STAGES if 2 <= stage.number <= 8]
    assert [stage.number for stage in no_ops] == [2, 3, 4, 5, 6, 7, 8]
    # `implemented` False is what the spend gate reads first: a declared no-op cannot spend.
    assert not [stage.number for stage in no_ops if stage.implemented or stage.fetches], no_ops
