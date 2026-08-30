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
    assert at_90s == {"jellyfin-sessions-poll", "fold-in-tick"}

    at_1000s = {job.name for job in worker.due(now=1000.0, last_run=last)}
    assert at_1000s == {"jellyfin-sessions-poll", "fold-in-tick", "jellyfin-seen-sync"}


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


def test_the_placement_sweep_runs_before_the_fits_that_read_its_coordinates():
    """§5.3 lists the two nightly fits above the placement sweep, but both fits read the
    coordinates the sweep writes: §5.1's `e(t)` needs ê for a cold or low-support title, and
    §5.2's fit takes the same coordinates as its embeddings.

    Left in table order, the night a bundle arrives fits every user against a library whose
    newly-owned titles have no coordinate yet, and the sweep corrects it a day later — once per
    import, silently, and only ever in the direction that makes the first night's tiers worse.
    The registry stays in §5.3's order for reading; `due` sorts by `stage`.
    """
    order = [j.name for j in worker.due(1e9, {})]
    assert order.index("placement-reconciliation") < order.index("fold-in-user-vectors")
    assert order.index("placement-reconciliation") < order.index("ledger-map-refit")

    # …and the table itself is still §5.3's, so the registry has not been reordered to fake it.
    table = [j.name for j in worker.JOBS]
    assert table.index("ledger-map-refit") < table.index("placement-reconciliation")


def test_the_fold_in_runs_often_enough_to_answer_within_a_sitting():
    """§12's M2 exit criterion — "50-100 verdicts each produce visibly personal rankings" — is a
    claim about what a person sees during a sitting, and every §6.0 shelf orders by a table only
    the fold-in writes.

    A strictly nightly fold-in cannot meet it: the tier badges move on every tap (the
    interactive path writes `ledger_state`) while the shelves stay in the order they had that
    morning, for up to a day. §5.3's nightly pass stays exactly as §5.3 writes it; this asserts
    the tick exists alongside it and is measured in minutes, not hours.
    """
    tick = next(j for j in worker.JOBS if j.name == "fold-in-tick")
    nightly = next(j for j in worker.JOBS if j.name == "fold-in-user-vectors")
    assert tick.run is not None, "M2 owes this one an implementation"
    assert tick.every <= 300, "a sitting is minutes long; an hourly tick is a nightly job"
    assert nightly.every == 86400, "§5.3's nightly pass is not replaced by the tick"
