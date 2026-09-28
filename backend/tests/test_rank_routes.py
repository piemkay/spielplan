"""The Rank surface's HTTP seam. Races are gathered twice over one session, eight times over:
a race that passes once has not passed."""

from __future__ import annotations

import asyncio
import random

import httpx
import numpy as np
import pytest

from spielplan.api import rank as rank_api
from spielplan.home import rail
from spielplan.ledger import observations, refit
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.rank import queue, read


def _embedding(title_id: int) -> np.ndarray:
    rng = np.random.default_rng(1000 + title_id)
    vector = rng.normal(size=64)
    return vector / (np.linalg.norm(vector) * 8.0)


def fixture_embeddings(title_ids):
    ids = list(title_ids)
    if not ids:
        return np.zeros((0, 64)), np.zeros(0, dtype=bool)
    return np.stack([_embedding(t) for t in ids]), np.ones(len(ids), dtype=bool)


# The first number `queue.draw` reads, per arm: `SHARES` walks 0.70 / 0.90 / 1.00 cumulatively.
BOUNDARY_ROLL, EXPLORATION_ROLL, HOLDOUT_ROLL = 0.10, 0.80, 0.95


class _Armed(random.Random):
    """Forcing the first roll replaces sixty draws and a hope: P(miss) = 0.9^60, about one red CI run
    in 285."""

    def __init__(self, roll: float, seed: int = 7) -> None:
        super().__init__(seed)
        self._roll: float | None = roll

    def random(self) -> float:
        if self._roll is not None:
            roll, self._roll = self._roll, None
            return roll
        return super().random()


def _arm(monkeypatch, roll: float) -> None:
    """One armed generator for every draw, in place of the per-position derivation."""
    armed = _Armed(roll)
    monkeypatch.setattr(rank_api, "_queue_rng", lambda *_: armed)


def _second(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """Two clients: a race needs two connections. `raise_app_exceptions=False` so a 500 reads as a
    status rather than an exception out of `gather`."""
    transport = httpx.ASGITransport(app=client._transport.app, raise_app_exceptions=False)
    second = httpx.AsyncClient(transport=transport, base_url="http://test")
    second.cookies.update(client.cookies)
    return second


async def _warm(*clients: httpx.AsyncClient) -> None:
    """Without it the loser's connection setup, not the lock, orders the two requests."""
    await asyncio.gather(*(c.get("/api/health") for c in clients))


async def _after(delay: float, request):
    """The drop race needs the mover's write between the other drop's neighbour read and its write."""
    await asyncio.sleep(delay)
    return await request


def _walk(payload):
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk(item)


def _refuses_to_fit(monkeypatch) -> None:
    """Patched on the module: `update_incrementally_reporting` reaches it through the module global."""

    async def refused(*_args, **_kwargs):
        raise refit.RefitRefused("s is not finite for user 1/movie")

    monkeypatch.setattr(refit, "update_incrementally", refused)


@pytest.fixture
async def ranked(db, app):
    await db.execute(
        """
        INSERT INTO title (id, kind, name, year, runtime_min, is_owned)
        SELECT x.id, 'movie', x.name, 1995, x.runtime, true
        FROM unnest($1::int[], $2::text[], $3::int[]) AS x(id, name, runtime)
        """,
        list(range(1, 9)),
        [f"Title {i}" for i in range(1, 9)],
        [90 + (i * 13) % 80 for i in range(1, 9)],
    )
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201
    user_id = (await client.get("/api/auth/me")).json()["id"]

    for title_id, value in ((1, 2), (2, 2), (3, 1), (4, 1), (5, 0), (6, 0)):
        await observations.record_verdict(db, user_id=user_id, title_id=title_id, value=value)
    for a, b, outcome in ((1, 2, "A"), (3, 4, "TIE"), (5, 6, "B"), (1, 5, "A")):
        await observations.record_duel(
            db, user_id=user_id, title_a=a, title_b=b, outcome=outcome,
            context="profile_battle", decisive=False, hp=DEFAULTS,
        )
    report = await refit.refit_user(
        db, user_id=user_id, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert report.fitted, report.as_dict()
    return client, user_id


async def test_the_answer_route_writes_the_arm_the_server_drew_it_under(db, ranked, monkeypatch):
    """Each arm forced through the seam, and read through the gate (`pair.model.arm`, toggle on)."""
    client, user_id = ranked
    await client.post("/api/auth/preferences", json={"show_model": True})
    seen_arms = set()
    for roll in (BOUNDARY_ROLL, EXPLORATION_ROLL, HOLDOUT_ROLL):
        _arm(monkeypatch, roll)
        pair = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
        assert pair is not None
        answered = await client.post(
            "/api/rank/queue/answer", json={"pair": pair["token"], "outcome": "A"}
        )
        assert answered.status_code == 200
        row = await db.fetchrow(
            "SELECT title_a, title_b, context, selection FROM duel "
            "WHERE user_id = $1 ORDER BY id DESC LIMIT 1",
            user_id,
        )
        assert row["context"] == "tier_queue"
        assert (row["title_a"], row["title_b"]) == (pair["title_a"], pair["title_b"])
        assert row["selection"] == pair["model"]["arm"], (
            "the row's arm must be the arm the server drew, not a constant"
        )
        seen_arms.add(row["selection"])
    assert seen_arms == {
        queue.ARM_BOUNDARY, queue.ARM_EXPLORATION, queue.ARM_HOLDOUT
    }, f"all three arms have to reach the column; saw {sorted(seen_arms)}"


async def test_a_client_cannot_name_its_own_selection_arm(db, ranked):
    """A client that could choose the arm would choose which stream its answers evaluate."""
    client, user_id = ranked
    await client.post("/api/auth/preferences", json={"show_model": True})
    served = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
    answered = await client.post(
        "/api/rank/queue/answer",
        json={
            "pair": served["token"],
            "outcome": "A",
            "arm": "uniform_holdout",
            "selection": "uniform_holdout",
            "context": "profile_battle",
        },
    )
    assert answered.status_code == 200
    row = await db.fetchrow(
        "SELECT context, selection FROM duel WHERE user_id = $1 ORDER BY id DESC LIMIT 1",
        user_id,
    )
    assert row["selection"] == served["model"]["arm"]
    assert row["context"] == "tier_queue"


async def test_a_pair_can_only_be_answered_once(db, ranked):
    """The seal carries the answered-comparison count; answering moves it, so a replay is a stale
    card. §13 counts rows, so N replays would weight one held-out judgement N-fold."""
    client, user_id = ranked
    served = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]

    first = await client.post(
        "/api/rank/queue/answer", json={"pair": served["token"], "outcome": "A"}
    )
    assert first.status_code == 200
    after_one = await db.fetchval(
        "SELECT count(*) FROM duel WHERE user_id = $1 AND context = 'tier_queue'", user_id
    )
    assert after_one == 1

    for _ in range(3):
        replayed = await client.post(
            "/api/rank/queue/answer", json={"pair": served["token"], "outcome": "A"}
        )
        assert replayed.status_code == 409
        assert replayed.json()["detail"]["reason"] == "stale_pair"
    assert await db.fetchval(
        "SELECT count(*) FROM duel WHERE user_id = $1 AND context = 'tier_queue'", user_id
    ) == 1, "a replayed seal wrote another comparison"


async def test_a_drop_does_not_invalidate_a_pair_on_the_table(db, ranked):
    """The counter is over comparisons, so a drop mid-queue must not discard the pair. Title 1 is
    seated in tier 6 first, so the drop is the legal gesture (finding 18)."""
    client, _user_id = ranked
    seated = await client.post("/api/rank/drop?kind=movie", json={"title_id": 1, "tier": 6})
    assert seated.status_code == 200, seated.text
    served = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
    dropped = await client.post(
        "/api/rank/drop?kind=movie", json={"title_id": 3, "tier": 6, "above": 1}
    )
    assert dropped.status_code == 200, dropped.text
    answered = await client.post(
        "/api/rank/queue/answer", json={"pair": served["token"], "outcome": "A"}
    )
    assert answered.status_code == 200


async def test_a_tier_label_long_enough_to_break_the_rail_is_refused_at_the_save(db, ranked):
    """`rail.record` runs after the drop commits, so an over-long label is refused at the save."""
    client, user_id = ranked
    refused = await client.put(
        "/api/rank/tiers", json={"tier_set": ["A" * 400, "B", "C"]}
    )
    assert refused.status_code == 422
    assert "label" in refused.json()["detail"].lower()
    assert (await client.get("/api/rank/tiers")).json()["tier_set"] == list(
        observations.DEFAULT_TIER_SET
    )

    dropped = await client.post("/api/rank/drop?kind=movie", json={"title_id": 1, "tier": 6})
    assert dropped.status_code == 200
    assert await db.fetchval(
        "SELECT count(*) FROM tier_edit WHERE user_id = $1", user_id
    ) == 1


async def test_a_held_out_answer_moves_nothing_the_model_reads(db, ranked, monkeypatch):
    """The fit cannot see a held-out row, but the incremental write stamps `last_observed_at`, which
    moves `sigma_eff` and `straddle`: the evaluation stream would steer the selector."""
    client, user_id = ranked
    snapshot = (
        "SELECT title_id, s, sigma_eff, last_observed_at FROM ledger_state "
        "WHERE user_id = $1 ORDER BY title_id"
    )

    _arm(monkeypatch, HOLDOUT_ROLL)
    served = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
    assert served is not None
    before = [dict(r) for r in await db.fetch(snapshot, user_id)]
    answered = await client.post(
        "/api/rank/queue/answer", json={"pair": served["token"], "outcome": "A"}
    )
    assert answered.status_code == 200

    # The stored arm, not the served one: it is what the fit's exclusion reads.
    stored = await db.fetchval(
        "SELECT count(*) FROM duel WHERE user_id = $1 AND selection = 'uniform_holdout'", user_id
    )
    assert stored == 1, "the row is still written — it is the evaluation stream"

    after = [dict(r) for r in await db.fetch(snapshot, user_id)]
    moved = [
        (b["title_id"], b["last_observed_at"], a["last_observed_at"])
        for b, a in zip(before, after, strict=True)
        if b["last_observed_at"] != a["last_observed_at"] or b["sigma_eff"] != a["sigma_eff"]
    ]
    assert not moved, f"a held-out answer moved the freshness clock or sigma_eff: {moved}"


async def test_a_tampered_or_forged_pair_is_refused_and_writes_nothing(db, ranked):
    client, user_id = ranked
    before = await db.fetchval("SELECT count(*) FROM duel WHERE user_id = $1", user_id)
    served = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]

    for token in (
        "not-a-token",
        served["token"][:-3] + "aaa",
        served["token"].split(".")[0],
    ):
        refused = await client.post(
            "/api/rank/queue/answer", json={"pair": token, "outcome": "A"}
        )
        assert refused.status_code == 409, token
        assert refused.json()["detail"]["reason"] == "stale_pair"
    assert await db.fetchval("SELECT count(*) FROM duel WHERE user_id = $1", user_id) == before


async def test_one_persons_sealed_pair_cannot_be_answered_by_another(db, app, ranked):
    """§4.2's tables are append-only, so a duel in the wrong ledger is unrecoverable."""
    client, user_id = ranked
    served = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]

    otp = (
        await client.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    ).json()["one_time_password"]
    other = app()
    await other.post("/api/auth/login", json={"name": "jenny", "password": otp})
    await other.post(
        "/api/auth/password", json={"current_password": otp, "new_password": "jennys-password"}
    )

    refused = await other.post(
        "/api/rank/queue/answer", json={"pair": served["token"], "outcome": "A"}
    )
    assert refused.status_code == 403
    assert await db.fetchval(
        "SELECT count(*) FROM duel WHERE user_id <> $1", user_id
    ) == 0


async def test_two_gathered_answers_under_one_seal_write_one_comparison(db, ranked):
    """Check-then-write on an autocommit connection let two gathered answers both write. Outcomes
    differ so a double write shows as a contradiction; eight runs, as one green race proves nothing."""
    client, user_id = ranked
    counted = "SELECT count(*) FROM duel WHERE user_id = $1 AND context = 'tier_queue'"
    other = _second(client)
    try:
        for attempt in range(8):
            served = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
            assert served is not None, f"the queue had no pair left on attempt {attempt}"
            before = await db.fetchval(counted, user_id)
            await _warm(client, other)
            results = await asyncio.gather(
                client.post(
                    "/api/rank/queue/answer", json={"pair": served["token"], "outcome": "A"}
                ),
                other.post(
                    "/api/rank/queue/answer", json={"pair": served["token"], "outcome": "B"}
                ),
            )
            statuses = sorted(r.status_code for r in results)
            assert statuses == [200, 409], f"attempt {attempt}: {statuses}"
            refused = next(r for r in results if r.status_code == 409)
            assert refused.json()["detail"]["reason"] == "stale_pair"
            after = await db.fetchval(counted, user_id)
            assert after == before + 1, f"attempt {attempt}: {before} -> {after}"
    finally:
        await other.aclose()


async def test_two_gathered_answers_on_the_held_out_arm_write_one_evaluation_row(
    db, ranked, monkeypatch
):
    """§13 counts rows, so a duplicated held-out comparison weights one judgement twice."""
    client, user_id = ranked
    held = "SELECT count(*) FROM duel WHERE user_id = $1 AND selection = 'uniform_holdout'"
    snapshot = (
        "SELECT title_id, s, sigma_eff, last_observed_at FROM ledger_state "
        "WHERE user_id = $1 ORDER BY title_id"
    )
    before_fit = [dict(r) for r in await db.fetch(snapshot, user_id)]
    other = _second(client)
    try:
        for attempt in range(8):
            _arm(monkeypatch, HOLDOUT_ROLL)
            served = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
            assert served is not None
            before = await db.fetchval(held, user_id)
            await _warm(client, other)
            results = await asyncio.gather(
                client.post(
                    "/api/rank/queue/answer", json={"pair": served["token"], "outcome": "A"}
                ),
                other.post(
                    "/api/rank/queue/answer", json={"pair": served["token"], "outcome": "B"}
                ),
            )
            statuses = sorted(r.status_code for r in results)
            assert statuses == [200, 409], f"attempt {attempt}: {statuses}"
            after = await db.fetchval(held, user_id)
            assert after == before + 1, f"attempt {attempt}: the §13 stream went {before} -> {after}"
    finally:
        await other.aclose()

    assert [dict(r) for r in await db.fetch(snapshot, user_id)] == before_fit, (
        "a held-out answer moved the fit the held-out arm is held out of"
    )


async def test_two_gathered_drops_leave_the_board_consistent(db, ranked, monkeypatch):
    """Decision 202: a drop carries no seal, so identical drops are both legal. The second race moves
    the neighbour mid-drop, with the window widened so it lands every attempt; `tier_edit.id` order
    proves an accepted sandwich ran first."""
    client, user_id = ranked
    edits = "SELECT count(*) FROM tier_edit WHERE user_id = $1"
    inserts = "SELECT count(*) FROM duel WHERE user_id = $1 AND context = 'tier_insert'"
    high_water = "SELECT coalesce(max(id), 0) FROM tier_edit WHERE user_id = $1"
    seat = {"title_id": 1, "tier": 5}
    sandwich = {"title_id": 3, "tier": 5, "above": 1}
    away = {"title_id": 1, "tier": 0}

    seated = await client.post("/api/rank/drop?kind=movie", json=seat)
    assert seated.status_code == 200, seated.text

    one, two = _second(client), _second(client)
    try:
        edits_before = await db.fetchval(edits, user_id)
        inserts_before = await db.fetchval(inserts, user_id)
        await _warm(one, two)
        repeated = await asyncio.gather(
            one.post("/api/rank/drop?kind=movie", json=sandwich),
            two.post("/api/rank/drop?kind=movie", json=sandwich),
        )
        statuses = sorted(r.status_code for r in repeated)
        assert statuses == [200, 200], f"a repeated drop was refused or raised: {statuses}"
        assert await db.fetchval(edits, user_id) - edits_before == 2, "one tier_edit per drop"
        assert await db.fetchval(inserts, user_id) - inserts_before == 2, "one duel per drop"

        recorded = observations.record_tier_edit

        async def _slow_edit(conn, **kw):
            if kw.get("title_id") == sandwich["title_id"]:
                await asyncio.sleep(0.25)
            return await recorded(conn, **kw)

        monkeypatch.setattr(observations, "record_tier_edit", _slow_edit)

        accepted = 0
        for attempt in range(8):
            reseated = await client.post("/api/rank/drop?kind=movie", json=seat)
            assert reseated.status_code == 200, f"attempt {attempt}: {reseated.text}"
            mark = await db.fetchval(high_water, user_id)
            inserts_before = await db.fetchval(inserts, user_id)
            await _warm(one, two)
            dropped, moved = await asyncio.gather(
                one.post("/api/rank/drop?kind=movie", json=sandwich),
                _after(0.05, two.post("/api/rank/drop?kind=movie", json=away)),
            )
            assert moved.status_code == 200, f"attempt {attempt}: the mover got {moved.text}"
            assert dropped.status_code in (200, 422), f"attempt {attempt}: {dropped.status_code}"
            fresh = {
                int(row["title_id"]): int(row["id"])
                for row in await db.fetch(
                    "SELECT title_id, id FROM tier_edit WHERE user_id = $1 AND id > $2",
                    user_id,
                    mark,
                )
            }
            new_inserts = await db.fetchval(inserts, user_id) - inserts_before
            if dropped.status_code == 422:
                assert new_inserts == 0, f"attempt {attempt}: a refused drop wrote a duel"
                assert sorted(fresh) == [1], f"attempt {attempt}: a refused drop wrote an edit"
                continue
            accepted += 1
            assert new_inserts == 1, f"attempt {attempt}: {new_inserts} duels for one neighbour"
            assert sorted(fresh) == [1, 3], f"attempt {attempt}: edits written {sorted(fresh)}"
            assert fresh[3] < fresh[1], (
                f"attempt {attempt}: the drop stored a neighbour duel against title 1 after "
                "title 1 had been moved out of the tier - the neighbour check and the write are "
                "not one gesture, so the resolution and both writes belong inside one transaction "
                "whose first statement is a per-user advisory lock"
            )
        assert accepted, "every attempt was refused, so the write this race is about never ran"
    finally:
        await one.aclose()
        await two.aclose()


async def test_a_refused_refit_does_not_lose_the_drop(db, ranked, monkeypatch):
    """The property is the 200: a refusal after the commit invited a retry that wrote a second row."""
    client, user_id = ranked
    await client.post("/api/auth/preferences", json={"show_model": True})
    _refuses_to_fit(monkeypatch)

    dropped = await client.post("/api/rank/drop?kind=movie", json={"title_id": 1, "tier": 6})
    assert dropped.status_code == 200, dropped.text
    ledger = dropped.json()["ledger"]
    assert ledger["applied"] is False
    assert "not finite" in ledger["reason"], ledger
    assert await db.fetchval(
        "SELECT count(*) FROM tier_edit WHERE user_id = $1", user_id
    ) == 1, "the edit is durable exactly once"


async def test_a_refused_refit_does_not_lose_the_queue_answer(db, ranked, monkeypatch):
    """The seal makes the retry safe: a 409 on the replay and one row either way."""
    client, user_id = ranked
    await client.post("/api/auth/preferences", json={"show_model": True})
    # Not the held-out arm: that one skips the fit entirely, so it cannot exercise a refusal.
    _arm(monkeypatch, EXPLORATION_ROLL)
    served = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
    _refuses_to_fit(monkeypatch)

    answered = await client.post(
        "/api/rank/queue/answer", json={"pair": served["token"], "outcome": "A"}
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["ledger"]["applied"] is False
    counted = "SELECT count(*) FROM duel WHERE user_id = $1 AND context = 'tier_queue'"
    assert await db.fetchval(counted, user_id) == 1

    replayed = await client.post(
        "/api/rank/queue/answer", json={"pair": served["token"], "outcome": "A"}
    )
    assert replayed.status_code == 409
    assert await db.fetchval(counted, user_id) == 1, "the retry wrote a second comparison"


async def test_two_names_too_long_for_the_rail_do_not_lose_the_comparison(db, ranked, monkeypatch):
    """`title.name` is bundle data, so the rail elides long names instead of raising after the write."""
    client, user_id = ranked
    await db.execute("UPDATE title SET name = repeat('x', 300) || id::text WHERE id <= 8")
    await client.post("/api/auth/preferences", json={"show_model": True})
    _arm(monkeypatch, EXPLORATION_ROLL)

    served = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
    assert len(served["name_a"]) >= 300 and len(served["name_b"]) >= 300
    answered = await client.post(
        "/api/rank/queue/answer", json={"pair": served["token"], "outcome": "A"}
    )
    assert answered.status_code == 200, answered.text
    assert await db.fetchval(
        "SELECT count(*) FROM duel WHERE user_id = $1 AND context = 'tier_queue'", user_id
    ) == 1
    line = answered.json()["log"][0]
    assert len(line) <= rail.MAX_LINE, f"the recorded line is {len(line)} characters"
    assert len(line) < 2 * len(served["name_a"]), "both names reached the line whole"


async def test_a_ledger_cache_miss_answers_without_fitting_in_the_request(db, ranked, monkeypatch):
    """The response must not claim a refit that did not run: §6.7 exists to make the model legible."""
    client, user_id = ranked
    await client.post("/api/auth/preferences", json={"show_model": True})
    _arm(monkeypatch, EXPLORATION_ROLL)
    served = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]

    await db.execute("DELETE FROM ledger_fit WHERE user_id = $1", user_id)
    await db.execute(
        "UPDATE ledger_cutpoints SET refit_requested_at = NULL WHERE user_id = $1", user_id
    )

    def never_inline(*_args, **_kwargs):
        raise AssertionError("a full MAP fit ran inside the request (finding 9)")

    monkeypatch.setattr(refit, "refit_user", never_inline)
    answered = await client.post(
        "/api/rank/queue/answer", json={"pair": served["token"], "outcome": "A"}
    )
    assert answered.status_code == 200, answered.text
    ledger = answered.json()["ledger"]
    assert ledger["applied"] is False, (
        f"the response claims the ledger was updated by a fit that did not run: {ledger}"
    )
    assert "queued" in ledger["reason"], ledger
    assert "ms" not in ledger and "refit" not in ledger, (
        f"a miss still reports a millisecond count for work nobody did: {ledger}"
    )
    assert await db.fetchval(
        "SELECT refit_requested_at FROM ledger_cutpoints WHERE user_id = $1 AND kind = 'movie'",
        user_id,
    ) is not None, "the sweep was never asked for the fit the request declined to run"


async def test_a_board_whose_first_fit_is_owed_says_so_instead_of_reading_zero_rated(db, ranked):
    """Between a first verdict and the sweep's fit the board is empty; it must read "owed", not 0 rated."""
    client, user_id = ranked
    await db.execute("DELETE FROM ledger_state WHERE user_id = $1", user_id)
    await db.execute("DELETE FROM ledger_fit WHERE user_id = $1", user_id)
    await db.execute("DELETE FROM ledger_cutpoints WHERE user_id = $1", user_id)

    unstarted = (await client.get("/api/rank?kind=movie")).json()
    assert unstarted["rated_total"] == 0 and unstarted["fitting"] is False, unstarted

    dropped = await client.post("/api/rank/drop?kind=movie", json={"title_id": 1, "tier": 4})
    assert dropped.status_code == 200, dropped.text
    # Read from the column: decision 117 gates `ledger`, but an owed fit is owed to every member.
    assert await db.fetchval(
        "SELECT refit_requested_at FROM ledger_cutpoints WHERE user_id = $1 AND kind = 'movie'",
        user_id,
    ) is not None, "the drop's cache miss queued no fit, so this is not finding 9's window"

    owed = (await client.get("/api/rank?kind=movie")).json()
    assert owed["rated_total"] == 0, "the fixture gained a fit this test needs it not to have"
    assert owed["fitting"] is True, (
        f"the board reads the same as one nobody has started: rated={owed['rated']}"
    )

    # Serviced the way `worker.py`'s `tier-set-refit` services it, and not by waiting.
    report = await refit.refit_user(
        db, user_id=user_id, kind="movie", hp=DEFAULTS, embeddings=fixture_embeddings
    )
    assert report.fitted, report.as_dict()
    await db.execute(
        "UPDATE ledger_cutpoints SET refit_requested_at = NULL WHERE user_id = $1", user_id
    )
    fitted = (await client.get("/api/rank?kind=movie")).json()
    assert fitted["rated_total"] > 0 and fitted["fitting"] is False, fitted


async def test_a_constant_the_fit_cannot_use_is_a_503_on_the_board_route(db, ranked, monkeypatch):
    """Not defaulted: the defaults carry a different `hp_digest` and would invalidate every cached fit."""
    client, _user_id = ranked
    monkeypatch.setattr(client._transport.app.state, "hyperparams", None)
    refused = await client.get("/api/rank?kind=movie")
    assert refused.status_code == 503, refused.text
    assert "ledger constants" in refused.json()["detail"]


async def test_the_board_ships_no_model_numbers_until_the_toggle_is_on(db, ranked):
    """`rail.redact` deletes the keys; a promise kept in CSS is still on the wire."""
    client, user_id = ranked

    off = (await client.get("/api/rank?kind=movie")).json()
    assert "model" not in off
    for tier in off["tiers"]:
        for entry in tier["entries"]:
            assert not {"s", "sigma", "cdf"} & set(entry)

    await client.post("/api/auth/preferences", json={"show_model": True})
    on = (await client.get("/api/rank?kind=movie")).json()
    assert "model" in on
    assert on["model"]["straddle_z"] == DEFAULTS.straddle_z
    assert on["model"]["held_out"]["stream"] == "uniform_holdout"
    assert user_id


async def test_the_queue_answers_log_line_is_gated_too(db, ranked):
    client, _user_id = ranked
    served = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
    off = (
        await client.post("/api/rank/queue/answer", json={"pair": served["token"], "outcome": "A"})
    ).json()
    assert "log" not in off

    await client.post("/api/auth/preferences", json={"show_model": True})
    served = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
    on = (
        await client.post("/api/rank/queue/answer", json={"pair": served["token"], "outcome": "A"})
    ).json()
    assert on["log"] and "Davidson arm, tier_queue" in on["log"][0]
    phrase = {
        queue.ARM_BOUNDARY: "boundary-targeted",
        queue.ARM_EXPLORATION: "exploration",
        queue.ARM_HOLDOUT: "uniform-random, held out",
    }[served["model"]["arm"]]
    assert on["log"][0].endswith(phrase)


async def test_the_queue_ships_no_arm_to_a_member_who_cannot_see_the_model(
    db, ranked, monkeypatch
):
    """A member told a pair does not count may answer it carelessly. Walked recursively: a key added
    later must inherit the gate."""
    client, _user_id = ranked
    for roll in (BOUNDARY_ROLL, EXPLORATION_ROLL, HOLDOUT_ROLL):
        _arm(monkeypatch, roll)
        off = (await client.get("/api/rank/queue?kind=movie")).json()
        assert "arm" not in {key for key, _ in _walk(off)}, off
        for _key, value in _walk(off):
            if isinstance(value, str):
                assert "held out" not in value and "tunes the model" not in value, value
        assert off["pair"]["reason"] == rank_api._QUEUE_WHY

    await client.post("/api/auth/preferences", json={"show_model": True})
    _arm(monkeypatch, HOLDOUT_ROLL)
    on = (await client.get("/api/rank/queue?kind=movie")).json()
    assert on["pair"]["model"]["arm"] == queue.ARM_HOLDOUT
    assert "held out" in on["pair"]["model"]["reason"]
    assert on["pair"]["reason"] == rank_api._QUEUE_WHY, (
        "the user-facing why-line is the same sentence on every arm"
    )


async def test_thirty_draws_return_one_pair_until_it_is_answered(db, ranked):
    """The draw derives from `(user, kind, answered)` under `SESSION_SECRET`, so reloading cannot
    select the arm. `_queue_rng` is left alone: the derivation is the subject."""
    client, _user_id = ranked
    first = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
    assert first is not None
    for draw in range(2, 31):
        again = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
        assert again == first, f"draw {draw} differed from the first"

    answered = await client.post(
        "/api/rank/queue/answer", json={"pair": first["token"], "outcome": "A"}
    )
    assert answered.status_code == 200
    after = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
    assert after != first, "answering is what turns the queue over, and it did not"


async def test_the_queue_never_re_serves_a_pair_this_person_has_answered(db, ranked, monkeypatch):
    """Dropping `asked=asked` from `draw`'s `_exploration` calls is silent, so the route is driven.
    Forced onto exploration, which gets only a fifth of the draws."""
    client, user_id = ranked
    # The fixture's four duels, so a route ignoring the set trips immediately.
    asked = {frozenset(pair) for pair in ((1, 2), (3, 4), (5, 6), (1, 5))}
    for draw in range(6):
        _arm(monkeypatch, EXPLORATION_ROLL)
        pair = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
        assert pair is not None, f"draw {draw}: the queue ran dry on an eight-title board"
        key = frozenset((pair["title_a"], pair["title_b"]))
        assert key not in asked, (
            f"draw {draw}: the route served {sorted(key)}, a pair this person has already "
            f"answered -- `read.asked_pairs` did not reach the exploration arm"
        )
        asked.add(key)
        answered = await client.post(
            "/api/rank/queue/answer", json={"pair": pair["token"], "outcome": "A"}
        )
        assert answered.status_code == 200, answered.text
    assert await db.fetchval(
        "SELECT count(*) FROM duel WHERE user_id = $1 AND context = 'tier_queue'", user_id
    ) == 6


async def _every_title_straddles(db, user_id: int) -> None:
    """The fixture's σ is too small to straddle, so boundary rolls would fall through. Re-applied per
    draw: each answer rewrites two titles' σ."""
    await db.execute(
        "UPDATE ledger_state SET sigma_eff = 20.0 WHERE user_id = $1 AND kind = 'movie'", user_id
    )


async def test_the_queue_route_never_re_serves_an_answered_boundary_pair(db, ranked, monkeypatch):
    """Decision 494: the boundary arm reads `read.asked_pairs` too."""
    client, user_id = ranked
    await client.post("/api/auth/preferences", json={"show_model": True})
    asked = {frozenset(pair) for pair in ((1, 2), (3, 4), (5, 6), (1, 5))}
    arms = []
    for draw in range(6):
        await _every_title_straddles(db, user_id)
        _arm(monkeypatch, BOUNDARY_ROLL)
        pair = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
        assert pair is not None, f"draw {draw}: the queue ran dry on a board that all straddles"
        arms.append(pair["model"]["arm"])
        key = frozenset((pair["title_a"], pair["title_b"]))
        assert key not in asked, f"draw {draw}: the route re-served {sorted(key)}"
        asked.add(key)
        answered = await client.post(
            "/api/rank/queue/answer", json={"pair": pair["token"], "outcome": "A"}
        )
        assert answered.status_code == 200, answered.text
    assert arms.count(queue.ARM_BOUNDARY) >= 3, (
        f"the boundary arm was hardly reached, so this proved little about it: {arms}"
    )


async def test_an_exhausted_queue_says_nothing_is_left_to_settle_not_rate_more(
    db, ranked, monkeypatch
):
    """Decision 495. The held-out tenth still draws: it reads no answered set (§13)."""
    client, user_id = ranked
    rated = [1, 2, 3, 4, 5, 6]
    done = {frozenset(pair) for pair in ((1, 2), (3, 4), (5, 6), (1, 5))}
    for a in rated:
        for b in rated:
            if a < b and frozenset((a, b)) not in done:
                await observations.record_duel(
                    db, user_id=user_id, title_a=a, title_b=b, outcome="A",
                    context="profile_battle", decisive=False, hp=DEFAULTS,
                )
    for roll in (BOUNDARY_ROLL, EXPLORATION_ROLL):
        _arm(monkeypatch, roll)
        settled = (await client.get("/api/rank/queue?kind=movie")).json()
        assert settled["pair"] is None, settled
        assert settled["reason"] == rank_api._QUEUE_SETTLED
        assert "fills up" not in settled["reason"]
    _arm(monkeypatch, HOLDOUT_ROLL)
    assert (await client.get("/api/rank/queue?kind=movie")).json()["pair"] is not None

    await db.execute(
        "DELETE FROM ledger_state WHERE user_id = $1 AND title_id <> 1", user_id
    )
    _arm(monkeypatch, EXPLORATION_ROLL)
    thin = (await client.get("/api/rank/queue?kind=movie")).json()
    assert thin["pair"] is None and thin["reason"] == rank_api._QUEUE_THIN, thin


async def test_a_queue_answer_returns_both_titles_placement_on_every_arm(db, ranked, monkeypatch):
    """Same shape on every arm, with no "moved": a held-out answer is never refitted, and saying so
    would name it. Toggle off: placement carries no model number (decision 117)."""
    client, _user_id = ranked
    shapes = []
    for roll in (BOUNDARY_ROLL, EXPLORATION_ROLL, HOLDOUT_ROLL):
        _arm(monkeypatch, roll)
        pair = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
        answered = (
            await client.post(
                "/api/rank/queue/answer", json={"pair": pair["token"], "outcome": "B"}
            )
        ).json()
        placed = answered["placed"]
        assert {p["title_id"] for p in placed} == {pair["title_a"], pair["title_b"]}, placed
        for spot in placed:
            assert spot["badge"].startswith(answered_tier_label(spot)), spot
            assert not {"s", "sigma", "moved", "changed"} & set(spot), spot
        shapes.append(tuple(sorted(placed[0])))
    assert len(set(shapes)) == 1, f"the reply's shape differs by arm: {shapes}"


def answered_tier_label(spot) -> str:
    """The badge leads with the tier the row renders in (§6.3, "A — between Heat and Prisoners")."""
    return list(observations.DEFAULT_TIER_SET)[spot["tier"]]


async def test_the_recent_window_reads_neither_battles_nor_the_held_out_stream(db, ranked):
    """Decision 494's window is a selector input, so it excludes the held-out stream and Rate battles."""
    _client, user_id = ranked

    async def queued(a, b, selection):
        await observations.record_duel(
            db, user_id=user_id, title_a=a, title_b=b, outcome="A",
            context="tier_queue", selection=selection, decisive=False, hp=DEFAULTS,
        )

    assert await read.recent_titles(db, user_id=user_id, kind="movie") == set()
    await queued(1, 2, queue.ARM_BOUNDARY)
    await queued(3, 4, queue.ARM_HOLDOUT)
    assert await read.recent_titles(db, user_id=user_id, kind="movie") == {1, 2}
    await queued(5, 6, queue.ARM_EXPLORATION)
    await queued(2, 3, queue.ARM_BOUNDARY)
    await queued(4, 5, queue.ARM_BOUNDARY)
    # The last three adaptive answers: (4, 5), (2, 3), (5, 6).
    assert await read.recent_titles(db, user_id=user_id, kind="movie") == {2, 3, 4, 5, 6}


async def test_the_drop_route_answers_with_the_board_under_the_filters_it_was_given(db, ranked):
    """Decision 204 at the route: `filtered=bool(filters.active())` is the one joining line."""
    client, user_id = ranked
    filtered = (await client.get("/api/rank?kind=movie&runtime_max=120")).json()
    assert filtered["rated"] < filtered["rated_total"]

    dropped = (
        await client.post(
            "/api/rank/drop?kind=movie&runtime_max=120", json={"title_id": 1, "tier": 6}
        )
    ).json()
    assert dropped["filters"] == {"runtime_max": 120}
    assert dropped["rated"] == filtered["rated"]
    assert dropped["rated_total"] == filtered["rated_total"]

    # `q=Title` matches every fixture title, so only "was a filter on?" differs between the drops.
    duels = "SELECT count(*) FROM duel WHERE user_id = $1 AND context = 'tier_insert'"
    edits = "SELECT count(*) FROM tier_edit WHERE user_id = $1"
    seated = await client.post("/api/rank/drop?kind=movie", json={"title_id": 2, "tier": 6})
    assert seated.status_code == 200, seated.text
    before_duels = await db.fetchval(duels, user_id)
    before_edits = await db.fetchval(edits, user_id)

    sandwiched = {"title_id": 3, "tier": 6, "above": 1, "below": 2}
    under_filter = await client.post("/api/rank/drop?kind=movie&q=Title", json=sandwiched)
    assert under_filter.status_code == 200, under_filter.text
    assert await db.fetchval(duels, user_id) == before_duels, (
        "a drop under an active filter stored neighbour duels, so the route did not tell "
        "`drop.drop` that the board had a filter on it (decision 204)"
    )
    assert await db.fetchval(edits, user_id) == before_edits + 1, (
        "the tier edit is the gesture the person made and is written either way"
    )

    # The unfiltered control, so a route that never writes neighbour duels cannot pass.
    plain = await client.post("/api/rank/drop?kind=movie", json=sandwiched)
    assert plain.status_code == 200, plain.text
    assert await db.fetchval(duels, user_id) == before_duels + 2
    assert await db.fetchval(edits, user_id) == before_edits + 2


async def test_the_drop_route_refuses_a_tier_outside_the_set_and_writes_nothing(db, ranked):
    client, user_id = ranked
    before = await db.fetchval("SELECT count(*) FROM tier_edit WHERE user_id = $1", user_id)
    refused = await client.post("/api/rank/drop?kind=movie", json={"title_id": 1, "tier": 99})
    assert refused.status_code == 422
    assert await db.fetchval("SELECT count(*) FROM tier_edit WHERE user_id = $1", user_id) == before


async def test_the_board_route_refuses_an_absent_kind(db, ranked):
    client, _user_id = ranked
    assert (await client.get("/api/rank")).status_code == 422
    assert (await client.get("/api/rank?kind=episode")).status_code == 422


async def test_an_unknown_genre_is_an_empty_board_and_not_an_error(db, ranked):
    """Decision 473: Rank answers an unknown genre with no titles, so the no-match state names it."""
    client, _user_id = ranked
    await db.executemany(
        "INSERT INTO title_genre (title_id, genre, source) VALUES ($1, $2, 'tmdb')",
        [(1, "Science Fiction"), (2, "Drama")],
    )

    unknown = await client.get("/api/rank", params={"kind": "movie", "genre": "heist film"})
    assert unknown.status_code == 200, unknown.text
    assert [e for t in unknown.json()["tiers"] for e in t["entries"]] == []
    assert unknown.json()["filters"] == {"genre": "heist film"}

    known = await client.get("/api/rank", params={"kind": "movie", "genre": "science FICTION"})
    assert known.status_code == 200, known.text
    assert [e["title_id"] for t in known.json()["tiers"] for e in t["entries"]] == [1]
    assert known.json()["filters"] == {"genre": "Science Fiction"}


async def test_the_tier_set_route_round_trips_and_warns(db, ranked):
    """Member register (decision 486): decision 11's two facts, without the model's nouns."""
    client, user_id = ranked
    current = (await client.get("/api/rank/tiers")).json()
    assert current["tier_set"] == list(observations.DEFAULT_TIER_SET)
    assert "throws away where your tier lines were learned to fall" in current["warning"]
    assert "works them out again" in current["warning"]
    for noun in ("cutpoint", "refit", "ledger"):
        assert noun not in current["warning"].lower(), current["warning"]

    saved = (
        await client.put("/api/rank/tiers", json={"tier_set": ["bad", "ok", "good"]})
    ).json()
    assert saved["k_changed"] and saved["refit_queued"]
    assert (await client.get("/api/rank?kind=movie")).json()["tier_set"] == ["bad", "ok", "good"]

    refused = await client.put("/api/rank/tiers", json={"tier_set": ["only"]})
    assert refused.status_code == 422
    assert user_id


async def test_the_whole_rank_surface_is_behind_a_session(db, app):
    """Every route: a new one without `ActiveUser` is how this stops being true."""
    anonymous = app()
    for method, path, body in (
        ("get", "/api/rank?kind=movie", None),
        ("get", "/api/rank/queue?kind=movie", None),
        ("get", "/api/rank/tiers", None),
        ("post", "/api/rank/drop?kind=movie", {"title_id": 1, "tier": 0}),
        ("post", "/api/rank/queue/answer", {"pair": "x", "outcome": "A"}),
        ("put", "/api/rank/tiers", {"tier_set": ["a", "b"]}),
    ):
        response = await getattr(anonymous, method)(path, **({"json": body} if body else {}))
        assert response.status_code == 401, f"{method.upper()} {path} is reachable signed out"
