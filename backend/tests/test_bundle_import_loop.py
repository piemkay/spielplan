"""The Validate press must never take the event loop (§10 step 1). Needs TEST_DATABASE_URL.

Only the FIRST press on an archive pays the extraction, so it is the one measured."""

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

# `tarfile.extractall` costs ~0.6 ms per MB, so 192 MiB is ~120 ms: unmistakable beside a 5 ms
# heartbeat. The assertions calibrate against the extraction actually paid, not a constant.
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
    while not stop.is_set():
        answer = await client.get("/api/health")
        statuses.append(answer.status_code)
        await asyncio.sleep(HEALTH_EVERY_S)


async def _press(admin, watcher, path: Path) -> tuple[float, float, list[int]]:
    gaps: list[float] = []
    statuses: list[int] = []
    stop = asyncio.Event()
    watchers = [
        asyncio.create_task(_heartbeat(stop, gaps)),
        asyncio.create_task(_health(watcher, stop, statuses)),
    ]
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
    """A warm-up press pays the process's one-time costs; the extraction is then timed alone in a thread.
    The loop gap is the assertion with teeth: `/api/health` turns 503 only after 2 s."""
    admin = await _admin(app)
    watcher = app()
    root = fx.make_bundle(tmp_path / "import" / "test-v1", version="test-v1")
    pad = root / "pad.bin"
    # After `make_bundle`, so BUNDLE.json does not inventory it and it is no `bundle-integrity` finding.
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
