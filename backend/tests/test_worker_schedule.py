"""The worker's schedule. Spec v2.1 §5.3.

§5.3's table gives each job a trigger, and the registry is meant to be a readable copy of it.
Before M1 the loop ran everything once an hour, which was fine while the only jobs were
hourly and daily prunes — and would have quietly turned §7.3's 1-minute `/Sessions` poll into
a 1-hour one, so a finish prompt would arrive long after the television was off.

`due` is pure, so the schedule is testable without waiting or mocking a clock.
"""

from __future__ import annotations

from spielplan import worker


def test_every_registered_job_matches_its_spec_trigger():
    """The `trigger` column is prose from §5.3; `every` is what the loop obeys. They have to
    agree, or the registry is documentation that lies."""
    by_name = {job.name: job for job in worker.JOBS}
    assert by_name["jellyfin-sessions-poll"].every == 60
    assert by_name["jellyfin-seen-sync"].every == 900        # "15 min + webhook"
    assert by_name["session-prune"].every == 3600            # "hourly"
    assert by_name["push-subscription-prune"].every == 86400  # "daily"


def test_the_tick_is_shorter_than_the_shortest_job():
    """Otherwise the shortest interval is a fiction: a job can never run more often than the
    loop wakes."""
    live = [job.every for job in worker.JOBS if job.run is not None]
    assert min(live) > worker.TICK_SECONDS


def test_a_job_that_has_never_run_is_due_immediately():
    """A worker restart should reconcile, not wait out a fifteen-minute interval it has no
    memory of."""
    names = {job.name for job in worker.due(now=0.0, last_run={})}
    assert "jellyfin-seen-sync" in names
    assert names == {job.name for job in worker.JOBS if job.run is not None}


def test_only_the_elapsed_jobs_are_due():
    last = {job.name: 0.0 for job in worker.JOBS}
    at_90s = {job.name for job in worker.due(now=90.0, last_run=last)}
    assert at_90s == {"jellyfin-sessions-poll"}

    at_1000s = {job.name for job in worker.due(now=1000.0, last_run=last)}
    assert at_1000s == {"jellyfin-sessions-poll", "jellyfin-seen-sync"}


def test_a_job_awaiting_its_milestone_is_never_due():
    """§5.3's later rows are declared so the registry reads like the spec, and `run=None` is
    what says "not yet" out loud instead of a silent omission."""
    pending = [job for job in worker.JOBS if job.run is None]
    assert pending, "the registry should still name the jobs later milestones own"
    due_names = {job.name for job in worker.due(now=1e9, last_run={})}
    assert not due_names & {job.name for job in pending}


def test_the_registry_covers_the_milestones_it_claims():
    milestones = {job.milestone for job in worker.JOBS}
    assert {"M0", "M1", "M2", "M5", "M6"} <= milestones
    live_m1 = {job.name for job in worker.JOBS if job.milestone == "M1" and job.run is not None}
    assert live_m1 == {
        "jellyfin-seen-sync", "jellyfin-sessions-poll", "webauthn-challenge-prune"
    }
