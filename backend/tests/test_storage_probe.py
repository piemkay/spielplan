""""Unwritable" is made by refusing the probe's `mkstemp`, not `chmod`: Windows ignores the mode
and CI runs as root."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spielplan import worker
from spielplan.api import admin as admin_api
from spielplan.core import storage
from spielplan.core.config import settings
from tests.fixtures import make_bundle as fx

ADMIN_PASSWORD = "an-admin-password"

# Captured before any patch, so nested patches wrap the real function.
_REAL_MKSTEMP = tempfile.mkstemp


def _refuse_mkstemp_in(monkeypatch, *refused: Path) -> list[Path]:
    """Make the probe's own write fail in `refused`, the way a root-owned bind mount does, and
    record every directory it tried to write into."""
    tried: list[Path] = []

    def mkstemp(*args, dir=None, **kwargs):  # noqa: A002 - tempfile's own keyword
        tried.append(Path(dir))
        if Path(dir).resolve() in {p.resolve() for p in refused}:
            raise PermissionError(13, "Permission denied")
        return _REAL_MKSTEMP(*args, dir=dir, **kwargs)

    monkeypatch.setattr(storage.tempfile, "mkstemp", mkstemp)
    return tried


def test_the_probe_names_an_unwritable_mount_with_the_chown_that_fixes_it(tmp_path, monkeypatch):
    """ASCII: it goes to a container log and into `job_run` as it is."""
    for name in storage.MOUNTS:
        (tmp_path / name).mkdir()
    _refuse_mkstemp_in(monkeypatch, tmp_path / "backups")

    result = storage.probe(tmp_path, storage.WORKER_MOUNTS)

    assert storage.unwritable(result) == ["backups"]
    sentence = storage.refusal(result)
    assert sentence is not None and sentence.isascii(), sentence
    assert str(tmp_path / "backups") in sentence and "PermissionError" in sentence
    assert "sudo chown -R 1000:1000 data/backups`" in sentence, sentence
    assert "data/artifacts" not in sentence, "the command names directories that are fine"
    assert not [p for p in tmp_path.rglob("*") if p.is_file()], (
        "the probe left a file behind in a mount it only meant to ask about"
    )


def test_the_import_mount_is_asked_by_permission_and_never_written_into(tmp_path, monkeypatch):
    """The bundle root IS `/data/import`, and validate fails any file BUNDLE.json does not list, so a
    probe file there would fail an import."""
    for name in storage.MOUNTS:
        (tmp_path / name).mkdir()
    tried = _refuse_mkstemp_in(monkeypatch)
    real_access = os.access
    monkeypatch.setattr(
        storage.os, "access",
        lambda path, mode: False if Path(path) == tmp_path / "import" else real_access(path, mode),
    )

    result = storage.probe(tmp_path, storage.MOUNTS)

    assert tmp_path / "import" not in tried, "the probe wrote a file into /data/import"
    assert set(tried) == {tmp_path / n for n in storage.MOUNTS if n != "import"}
    assert storage.unwritable(result) == ["import"], result


def test_a_mount_that_is_not_there_yet_is_not_a_failure(tmp_path):
    """The importer and the dump create their directories; only the parent is asked, without writing."""
    assert storage.writable(tmp_path / "artifacts") is None
    assert not (tmp_path / "artifacts").exists(), "asking about a directory created it"


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="a directory's mode stops nobody on Windows or as root",
)
def test_a_real_read_only_directory_is_reported(tmp_path):
    locked = tmp_path / "backups"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        assert storage.writable(locked) is not None
    finally:
        locked.chmod(0o755)


async def _admin(app):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    return client


async def _run_storage_check() -> None:
    """One tick of the real loop with the registry cut to this job, due now."""
    row = next(j for j in worker.JOBS if j.name == admin_api.STORAGE_JOB)
    jobs = worker.JOBS
    worker.JOBS = (row,)
    try:
        await worker._tick(time.monotonic(), datetime.now(UTC), {}, {})
    finally:
        worker.JOBS = jobs


async def _newest(db):
    return await db.fetchrow(
        "SELECT ok, detail FROM job_run WHERE name = $1 ORDER BY started_at DESC, id DESC LIMIT 1",
        admin_api.STORAGE_JOB,
    )


async def test_the_storage_check_job_fails_with_the_chown_and_passes_after_it(
    app, db, tmp_path, monkeypatch
):
    """Green on the next run after the chown, with nothing restarted."""
    admin = await _admin(app)
    for name in storage.MOUNTS:
        (tmp_path / name).mkdir(exist_ok=True)
    _refuse_mkstemp_in(monkeypatch, tmp_path / "backups")

    await _run_storage_check()
    failed = await _newest(db)
    assert failed["ok"] is False
    assert "sudo chown -R 1000:1000 data/backups" in failed["detail"]["error"], failed["detail"]

    _refuse_mkstemp_in(monkeypatch)  # the chown
    await _run_storage_check()
    passed = await _newest(db)
    assert passed["ok"] is True, passed["detail"]

    card = (await admin.get("/api/admin/system")).json()
    assert [j["name"] for j in card["jobs"]].count(admin_api.STORAGE_JOB) == 1


async def test_validate_refuses_an_unwritable_artifacts_root_before_anything_is_queued(
    app, db, tmp_path, monkeypatch
):
    """Every refusal an import can raise must be reachable at validate, before queueing."""
    admin = await _admin(app)
    root = fx.make_bundle(tmp_path / "import" / "test-v1", version="test-v1")
    artifacts = settings().artifacts_dir
    artifacts.mkdir(parents=True, exist_ok=True)
    _refuse_mkstemp_in(monkeypatch, artifacts)

    validated = await admin.post("/api/admin/bundle/validate", json={"path": str(root)})
    assert validated.status_code == 200, validated.text
    report = validated.json()["report"]
    assert report["ok"] is False
    stage = [f for f in report["findings"] if f["rule"] == "stage" and f["severity"] == "fail"]
    assert stage, report["findings"]
    assert "sudo chown -R 1000:1000 data/artifacts" in stage[0]["message"], stage[0]

    refused = await admin.post("/api/admin/bundle/import", json={"path": str(root)})
    assert refused.status_code == 422, refused.text
    assert await db.fetchval("SELECT count(*) FROM job_run WHERE name = 'bundle-import'") == 0
    assert not list(artifacts.iterdir()), "the refused import staged something anyway"


async def test_health_names_an_unwritable_backend_mount_and_keeps_its_status(app):
    """Names, never paths or errors; a read-only cache does not make a serving backend a 503."""
    client = app()
    watch = client._transport.app.state.storage
    watch.result = {"artifacts": None, "cache": "/data/cache is not writable", "import": None}

    answer = await client.get("/api/health")

    assert answer.status_code == 200
    assert answer.json()["storage"] == {"ok": False, "unwritable": ["cache"]}
