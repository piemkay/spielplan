"""The press, and the event loop it is not allowed to take. Spec v2.1 §10 step 1, §6.6, §5.3.

The §7 exit criterion measures two things about the operator's press: that the request answers in
under 5 s, and that "/api/health answers every second from the press to the flip, each under 5 s".
The second clause is published on the worker-job row of `spec_coverage.toml` as "and /api/health
keeps answering throughout", and until this file nothing asserted it anywhere: the one instrument
that measures it (`ops/m414_exit_criterion.py`) tars the corpus and OPENS it before `create_app()`
is called, so every request it has ever recorded took `_unpack`'s reuse path and the extraction an
operator's first press pays was outside every measurement taken. Driven through the app instead,
that press was 5.88-6.27 s for a 1.04 GB archive with `/api/health` taking 5.2 s to answer - past
the image HEALTHCHECK's own 5 s timeout. [M4.14 cycle 4, m414-c4-waveE-02 and m414-c4-rec-01]

Decision 287 put the validation in a thread and `api/artifacts._open` now does the same for the
extraction, so what this file holds is the whole of the press: no part of it may own the loop.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tarfile
import time
from pathlib import Path

from spielplan.importer import bundle as bundle_import
from tests.fixtures import make_bundle as fx

ADMIN_PASSWORD = "an-admin-password"

# The pad is the instrument, and it is sized rather than guessed: `tarfile.extractall` costs about
# 0.6 ms per MB on the reference box, so 192 MiB is ~120 ms of extraction - long enough that a
# blocked loop is unmistakable beside a 5 ms heartbeat, short enough that the file is written,
# tarred, extracted and deleted inside one test. The assertions below calibrate against the
# extraction this press actually paid rather than against a constant, because the same 192 MiB is
# seconds on a household spinning disk and the property is the same there.
PAD_BYTES = 192 << 20
HEARTBEAT_S = 0.005
HEALTH_EVERY_S = 0.05


async def _admin(app):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    return client


async def _heartbeat(stop: asyncio.Event, gaps: list[float]) -> None:
    """How long the loop went between two 5 ms sleeps. A blocked loop is one long gap."""
    last = time.perf_counter()
    while not stop.is_set():
        await asyncio.sleep(HEARTBEAT_S)
        now = time.perf_counter()
        gaps.append(now - last)
        last = now


async def _health(client, stop: asyncio.Event, statuses: list[int]) -> None:
    """The published clause itself, sampled the way §7 samples it - every probe, its status."""
    while not stop.is_set():
        answer = await client.get("/api/health")
        statuses.append(answer.status_code)
        await asyncio.sleep(HEALTH_EVERY_S)


async def _press(admin, watcher, path: Path) -> tuple[float, float, list[int]]:
    """One press of Validate, with the loop watched from beside it."""
    gaps: list[float] = []
    statuses: list[int] = []
    stop = asyncio.Event()
    watchers = [
        asyncio.create_task(_heartbeat(stop, gaps)),
        asyncio.create_task(_health(watcher, stop, statuses)),
    ]
    # Let both watchers reach their first await, then drop what they measured getting there.
    await asyncio.sleep(0.05)
    gaps.clear()
    statuses.clear()
    started = time.perf_counter()
    try:
        answer = await admin.post("/api/admin/bundle/validate", json={"path": str(path)})
        wall = time.perf_counter() - started
    finally:
        # A press that raises must still stop its watchers, or the failure this test is about
        # arrives behind "Task was destroyed but it is pending".
        stop.set()
        await asyncio.gather(*watchers)
    assert answer.status_code == 200, answer.text
    assert gaps, "the heartbeat never ran, so this press measured nothing"
    return wall, max(gaps), statuses


async def test_the_first_press_on_an_archive_leaves_the_loop_free_for_api_health(app, tmp_path):
    """m414-c4-waveE-02: the extraction used to run on the request loop, and only the FIRST press
    pays it - which is why three review cycles and every recorded run of the exit criterion missed
    it.

    The measurement is calibrated on this box rather than against a constant, because the same
    192 MiB is a tenth of a second on an NVMe developer box and seconds on a household spinning
    disk while the property is the same on both. So: one press against the bundle DIRECTORY, whose
    numbers are thrown away and which is here to pay this process's one-time costs (torch's import
    inside `validate`, the pool's first statements); then the extraction timed on its own in a
    thread and removed again; then the press that is measured, which extracts it a second time.

    The loop-gap assertion is the one with teeth at fixture scale: a `/api/health` probe answers
    503 only after `app._HEALTH_TIMEOUT_S` (2 s) of not reaching the pool, and 192 MiB of tar does
    not block anything for two seconds on a developer's box. The statuses are asserted anyway
    because they are the published clause, and because the regression this guards against is
    unbounded in exactly the direction that trips them: the archive an operator imports is 1.04 GB.
    """
    admin = await _admin(app)
    watcher = app()
    root = fx.make_bundle(tmp_path / "import" / "test-v1", version="test-v1")
    pad = root / "pad.bin"
    # After `make_bundle`, so BUNDLE.json does not inventory it: this file is bytes for the
    # extraction to move and must not be a `bundle-integrity` finding as well.
    chunk = os.urandom(1 << 20)
    with pad.open("wb") as fh:
        for _ in range(PAD_BYTES >> 20):
            fh.write(chunk)
    archive = tmp_path / "import" / "test-v1.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(root, arcname=root.name)
    unpacked = archive.parent / f".unpacked-{archive.name}"

    try:
        await _press(admin, watcher, root)

        started = time.perf_counter()
        await asyncio.to_thread(bundle_import.Bundle.open, archive)
        extraction = time.perf_counter() - started
        shutil.rmtree(unpacked)
        assert extraction > 0.05, (
            f"this box extracted {PAD_BYTES >> 20} MiB in {extraction * 1000:.0f} ms, which is "
            "too fast for the comparison below to mean anything - raise PAD_BYTES rather than "
            "relaxing the assertion"
        )

        _, gap, health = await _press(admin, watcher, archive)

        assert unpacked.is_dir(), "the press against the archive did not extract it"
        assert gap < 0.5 * extraction, (
            f"the press held the event loop for {gap * 1000:.0f} ms of an extraction measured at "
            f"{extraction * 1000:.0f} ms on this box: the archive is being unpacked on the loop, "
            "so /api/health stops answering for as long as the operator's bundle takes to extract"
        )
        assert health, "no /api/health probe was answered during the press"
        assert set(health) == {200}, (
            f"/api/health answered {sorted(set(health))} during the press it is published as "
            f"answering throughout ({len(health)} probes)"
        )
    finally:
        # A test that leaves 400 MB in `tmp_path` leaves it three runs deep, which pytest keeps.
        shutil.rmtree(unpacked, ignore_errors=True)
        archive.unlink(missing_ok=True)
        pad.unlink(missing_ok=True)
