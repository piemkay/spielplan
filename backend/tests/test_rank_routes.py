"""The Rank surface's HTTP seam. Spec v2.1 §6.3, §6.7, §13; decisions 11, 117.

Written after the M3 review, which found that `api/rank.py` — the file that decides which §13
stream a comparison joins, seals the pair, and applies decision 117's gate — had no backend
test of any kind. The review mutation-proved the hole: hard-coding `selection="boundary"` in
the answer route re-creates proposal 120's exact bug (a held-out pair recorded as
boundary-targeted) and the entire pytest suite stayed green, because the row's named test
passed the arm in on both sides and checked it came back.

So these are route tests, and they exist for the things only the route can get wrong:

  * **the arm comes out of the seal and out of nothing else.** A client cannot name it, a
    forged or tampered token is refused, and another account's token is refused.
  * **decision 117's gate is applied at the exit**, not merely respected by the assembler.
  * **the drop route's filters travel**, so a drop under a filter answers with that board.
  * **one durable comparison per sealed pair, under concurrency** (M4.10 finding 1). The
    single-use guard was a check and a write two round trips apart on an autocommit connection,
    and every race here is gathered twice over one session because that is the only shape that
    can see it. Repeated eight times: a race that passes once has not passed.
  * **no route raises after its observation is durable** (finding 8). A refused refit, a rail
    line built from 300-character names, a cache miss - each of them used to be a 500 over a row
    the person cannot take back, with the client's retry writing another.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

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
from spielplan.rank import queue


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
    """A seeded generator whose FIRST roll lands in one arm's band.

    `queue.draw` rolls once for the arm and then draws the pair from the same generator, so
    forcing an arm is forcing exactly that first number and letting the rest run from the seed.
    What it replaces is sixty draws and a hope: §6.3's mix makes the held-out tenth rare, so the
    two §13 tests below each looped sixty times and then required all three arms to have turned
    up, P(miss) = 0.9^60. Replaying that roll 200,000 times against `SystemRandom` missed 352
    times - roughly one CI run in 285 red on the only route-level proof of the guard, and red
    for nothing. [M4.10 finding 30]
    """

    def __init__(self, roll: float, seed: int = 7) -> None:
        super().__init__(seed)
        self._roll: float | None = roll

    def random(self) -> float:
        if self._roll is not None:
            roll, self._roll = self._roll, None
            return roll
        return super().random()


def _arm(monkeypatch, roll: float) -> None:
    """Point the route's draw seam at a generator whose first roll picks a known arm.

    `api/rank.py._rng` is None in production - the draw is derived from the queue position - and
    this is the one thing that overrides it, in `_queue_rng`, which is also the function the
    derivation goes through. A seam the production path does not use tests something else.
    """
    monkeypatch.setattr(rank_api, "_rng", _Armed(roll))


def _second(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """A second client over the same app and the same session, tolerant of an unhandled raise.

    Two clients because a race needs two connections and one client's jar is one device: the
    shape being tested is a phone and a laptop, or two tabs, on one account.
    `raise_app_exceptions=False` because the failure being measured IS a 500 - a route that
    raises after its write has to be readable as a status code here rather than as a traceback
    out of `gather`, which would take the other half of the race with it.
    """
    transport = httpx.ASGITransport(app=client._transport.app, raise_app_exceptions=False)
    second = httpx.AsyncClient(transport=transport, base_url="http://test")
    second.cookies.update(client.cookies)
    return second


async def _warm(*clients: httpx.AsyncClient) -> None:
    """Both pool connections open before either write starts.

    Without it the loser's first connection cost is what orders the two requests rather than the
    lock, and warming them one after the other is itself that serialisation - which is exactly
    how `test_account_security.py`'s first-boot race used to pass with the lock removed.
    """
    await asyncio.gather(*(c.get("/api/health") for c in clients))


async def _after(delay: float, request):
    """One half of a gathered pair, started late on purpose.

    A window a test has to be lucky to hit is a window the suite reports green on. The drop race
    below needs the mover's write to land between the other drop's neighbour read and its own
    write, and with nothing said those two are microseconds apart - so the interleaving that
    matters turns up on the attempts where the scheduler helped and nowhere else.
    """
    await asyncio.sleep(delay)
    return await request


def _walk(payload):
    """Every (key, value) in a nested payload, so a gate can be asserted at any depth."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk(item)


def _refuses_to_fit(monkeypatch) -> None:
    """`refit.update_incrementally` raising what a non-finite fit raises.

    Patched on the module rather than on the router, because `update_incrementally_reporting`
    is what the routes call and it reaches its sibling through the module global - which is the
    seam the fix put there.
    """

    async def refused(*_args, **_kwargs):
        raise refit.RefitRefused("s is not finite for user 1/movie")

    monkeypatch.setattr(refit, "update_incrementally", refused)


@pytest.fixture
async def ranked(db, app):
    """An admin with a fitted movie board, and a client holding their session."""
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


# --- §13: the arm comes out of the seal ------------------------------------------------------


async def test_the_answer_route_writes_the_arm_the_server_drew_it_under(db, ranked, monkeypatch):
    """The mutation the review found: `selection="boundary"` hard-coded in the route puts every
    held-out pair into the adaptive stream, and nothing else in the suite notices.

    All three arms, forced one at a time through the seam rather than drawn sixty times and
    hoped for (`_Armed`). And read through the gate, because that is where the arm travels now:
    `pair.model.arm`, with decision 117's toggle on. An ungated member is told which comparison
    is which by neither the payload nor the copy. [M4.10 findings 16, 30]
    """
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
    """§13's guard in one request. The body has no arm field, and a client that invents one is
    ignored rather than obeyed — a client that could choose would choose which stream its
    answers evaluate."""
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
    """§6.1's `card_token` is server-held and single-use, and the M3 review found this surface
    claiming the same property without having it: the seal is bound to a user but replaying it
    wrote another `duel` every time.

    That matters most for the held-out arm — §13's agreement figure counts rows, so N replays
    of one uniform-random pair weight one judgement N-fold in the only number §13 admits — and
    it matters generally because §4.2's tables are append-only, so a duplicate cannot be taken
    back. The seal carries the count of comparisons the person had answered when it was drawn;
    answering moves that count, and a second answer under the old one is a stale card.
    """
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
    """The counter is over *comparisons*, not over everything the person does. A drag-drop
    writes `tier_insert` duels; if those moved the counter, picking a title up mid-queue would
    silently discard the pair in front of them.

    The drop's own status is asserted, which it was not: `drop` now refuses a neighbour that is
    not in the tier being dropped into (finding 18), so this body used to 422 and the test went
    on passing because it only read the *answer*. A drop that never happened cannot fail to move
    a counter. Title 1 is seated in tier 6 first, so the gesture is the legal one the client
    sends.
    """
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
    """The review's finding, at the boundary that should have caught it.

    §6.7's rail refuses a line over 400 characters, and `rail.record` runs *after* the drop's
    transaction has committed — so a 400-character tier label turned every subsequent drop into
    that tier into a 500 with the `tier_edit` and its neighbour duels already durable, and each
    retry wrote another. Refused at the save, in `tiers.validate`, which is where the other
    refusals live.
    """
    client, user_id = ranked
    refused = await client.put(
        "/api/rank/tiers", json={"tier_set": ["A" * 400, "B", "C"]}
    )
    assert refused.status_code == 422
    assert "label" in refused.json()["detail"].lower()
    assert (await client.get("/api/rank/tiers")).json()["tier_set"] == list(
        observations.DEFAULT_TIER_SET
    )

    # And the drop path still works, with the tier_edit count as the proof that nothing was
    # half-written on the way.
    dropped = await client.post("/api/rank/drop?kind=movie", json={"title_id": 1, "tier": 6})
    assert dropped.status_code == 200
    assert await db.fetchval(
        "SELECT count(*) FROM tier_edit WHERE user_id = $1", user_id
    ) == 1


async def test_a_held_out_answer_moves_nothing_the_model_reads(db, ranked, monkeypatch):
    """§13's guard, at the return path the review found nobody had looked at.

    The fit cannot see a held-out row — `load_observations` and `_load_local` both exclude it —
    so re-fitting after one is a no-op that can only perturb state. And it did: the incremental
    write stamps `last_observed_at = now` for every title with an observation, which resets
    §5.2's freshness inflation, which moves `sigma_eff`, which moves `straddle`, which is the
    predicate the *boundary* arm draws from. The evaluation stream would have been steering the
    selector it exists to audit, and the nightly path — which derives the stamp from the
    excluded set — would have quietly undone it every night.

    One forced draw rather than up to sixty real ones. The old shape answered adaptive pairs
    until a held-out one turned up — which is why the snapshot had to be taken inside the loop,
    and why it was red once in every 285 runs for no reason at all. [M4.10 finding 30]
    """
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

    # The stored arm, not the served one: it is what the fit's exclusion reads, and asserting it
    # here is what makes "a held-out answer" the subject of the rest of this test.
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
    """The seal is what makes the arm the server's. Without a signature check, a client could
    mint `{"a": 1, "b": 2, "arm": "boundary"}` and put an adaptively chosen pair anywhere."""
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
    """A duel written into the wrong ledger is unrecoverable — §4.2's tables are append-only."""
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


# --- M4.10 finding 1: one durable comparison per sealed pair -----------------------------------


async def test_two_gathered_answers_under_one_seal_write_one_comparison(db, ranked):
    """The seal was single-use against a replay and not against a race.

    `read.answered_comparisons` — a bare `SELECT count(*) FROM duel` — compared with the sealed
    count, then `record_duel` two round trips later, on a connection that autocommits every
    statement, with no unique index anywhere on `duel` to backstop it. Four independent probes
    agree on what that does with no injected latency at all: two gathered answers under one seal
    returned 200/200 and wrote two rows in most races, sometimes carrying contradictory outcomes
    A and B for the same pair, and one doubled race drew `uniform_holdout` — §13's only
    admissible evaluation data, weighted twice, in a §4.2 append-only table on a surface with no
    undo.

    The outcomes posted here differ on purpose, so a doubled write shows up as a contradiction
    and not only as a count. Eight repetitions because the original reproductions failed in four
    of six and five of six attempts: one green run of a race proves nothing.
    """
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
    """The same race on the arm that makes it worth a transaction.

    §13's agreement figure counts *rows*, so a duplicated held-out comparison weights one
    judgement twice in the only data admitted to evaluate the tier model — and one of the
    original doubled races was exactly this. The fit must also stay untouched: the held-out arm
    skips the incremental update on purpose (the write stamps `last_observed_at`, which moves
    `sigma_eff`, which moves `straddle`, which is what the boundary arm draws from), so a race
    that let the loser through would have had the evaluation stream steering the selector it
    exists to audit.
    """
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
    """Decision 202, narrowed deliberately: the drop route ships no new refusal.

    A drop carries no seal and two identical drops a minute apart are two legitimate gestures -
    §6.3's edit is a statement about where the title belongs, not an answer to a question the
    server asked - so there is nothing here to refuse with a 409. The first race is that claim
    and the whole of it: the same body twice, answered twice, one `tier_edit` and one neighbour
    duel each.

    THE SECOND RACE DISAGREES ON PURPOSE, and the reason is that the first one cannot fail. Two
    identical drops cannot move the neighbour either of them names, so "no duel names a
    neighbour outside the target tier" holds under every interleaving - it held with finding
    18's resolution deleted, and it held at the M4.9 head, which is a clause of the exit
    criterion discharged by a test with no reachable red. The pair below puts title 3 under
    title 1 in tier 5 while pulling title 1 out to tier 0, which is the one shape in which the
    board can end up carrying a comparison nobody made: `above` is accepted against a read, and
    a duel written after that read went stale stores a placement the person never saw.
    [M4.10 cycle 2, M410-C2-D202-02]

    The window is widened rather than hoped for. `record_tier_edit` waits a quarter of a second
    for the sandwiching drop alone - after its neighbour has been resolved, before anything of
    the gesture is written - and the mover starts a twentieth of a second in, so the mover's
    edit lands inside that window on every attempt instead of on the attempts where the
    scheduler happens to help.

    What holds is then ORDER, not the final board. The mover legitimately leaves title 1 in
    tier 0, so `placed[1] == 5` is false at the end of every attempt whatever the route did -
    which is why the identical-body version was the only one that could assert it. `tier_edit.
    id` is a bigserial and records the order the inserts really ran in, so an accepted sandwich
    has to be the EARLIER of the two: nothing moved the neighbour between the check and the
    write. Moving the resolution inside the transaction does not buy that on its own - under
    read committed the mover still commits into the gap - which is why the guard is the lock.

    Both halves go through `_second`, because either of them could be the one that raises and a
    500 has to be a status code here rather than an exception out of `gather` that takes the
    other half with it.
    """
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


# --- M4.10 finding 8: nothing raises after the observation is durable --------------------------


async def test_a_refused_refit_does_not_lose_the_drop(db, ranked, monkeypatch):
    """The drop committed its edit and then called the fit bare, so a refusal was a 500.

    Reproduced before the repair: with `update_incrementally` raising `RefitRefused` the route
    answered 500 with the `tier_edit` already durable and the client's retry wrote a second
    (1 -> 2), while the *same* refusal on a Rate verdict answered 200 — one surface losing the
    tap and the other not, over identical model trouble. `rank/tiers.py`'s `MAX_LABEL` comment
    records the same failure mode for tier labels, which is how it was found.

    The retry clause belongs to the answer route below, where the seal makes a repeat idempotent.
    Here the property is the 200: it removes the client's reason to repeat at all, and decision
    202 is explicit that a *deliberate* second drop is a second gesture and a second row.
    """
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
    """The same repair at the second raise site, with the retry the old 500 invited.

    A 409 on the replay and one row either way: the seal is what makes the retry safe, and it is
    the reason the drop route gets no equivalent clause.
    """
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
    """Finding 8's third raise site: the §6.7 line is composed after the duel is durable.

    `rail.record` refuses a line over `MAX_LINE`, and `title.name` is bundle data — there is no
    person to refuse. Measured with two 260-390 character names: the answer route 500'd after the
    row had committed, 4 -> 5 duels on the retry. The renderers elide a display name now
    (`rail.MAX_NAME_IN_LINE`) and keep `RailError` for the two lines that are programming errors,
    which is the only way a call made after a write can be made safe.
    """
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
    """Finding 9 at the route: a miss queues the fit instead of running it on the event loop.

    `load_cache` returning None used to call `refit_user` inline — every observation loaded,
    `model.fit`, and a dense `(p+n)x(p+n)` inverse — inside §5.3's "<50 ms" row. Measured on
    synthetic boards: 0.39 s at n=300, 6.96 s at n=2000, 33.4 s at n=4000, and it is hit on the
    first tap ever per (user, kind), on every tap after a bundle import and on an `hp_digest`
    change. On this route the same call was uncaught, so a slow fit was a client timeout over a
    durable row, and the retry wrote another one. `refit_requested_at` and the 60 s job are
    0012's and decision 11's, and nothing in that column says only a tier-set change may ask.

    What the response SAYS about the fit is asserted here too, and not only that it carried no
    rows: the repair kept `refit: true` and the millisecond count the inline fit used to earn, so
    `RateModelLog.svelte` rendered "ledger movie - refit 8.9 ms" and §6.7's rail recorded
    ", incremental refit 9 ms" over a fit that had not run — `ledger_fit` empty, `ledger_state`
    unmoved, nothing anywhere saying a fit was owed. §6.7 is the surface that exists to make the
    model legible, so a number it prints for work that did not happen is the defect, not a
    cosmetic. [M4.10 cycle 2, m410-c2-cache-miss-reports-a-refit-that-did-not-run]
    """
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
    """Decision 209: the other end of finding 9, on the surface §6.3 makes a promise about.

    §6.3's board is "every rated title" and it is `ledger_state` that holds them, so between a
    member's first verdict and the sweep that fits it the route answers `rated: 0`, `rated_total:
    0` and seven empty tiers, over this fixture's six verdicts and four duels. §5.3 is why: the
    full MAP fit is its "seconds" row and the request is its "<50 ms" one, so the fit is owed
    rather than run and nothing is lost. What is wrong is the reading. "0 rated" is what a
    member who has never opened Rate sees, so the healthy value of this instrument equals its
    broken one, and the person who has just rated their evening is told they have rated nothing.

    Three answers over one board that never gains a row, which is the whole of the claim: nothing
    owed reads 0, the drop's queued fit turns the same emptiness into "owed", and servicing that
    fit exactly as `tier-set-refit` does clears it. The `why` line moves with it, because a count
    is only §6.8's provenance while it is a count of something.
    """
    client, user_id = ranked
    # The first sitting, reproduced: the observations are durable and no fit has ever landed.
    await db.execute("DELETE FROM ledger_state WHERE user_id = $1", user_id)
    await db.execute("DELETE FROM ledger_fit WHERE user_id = $1", user_id)
    await db.execute("DELETE FROM ledger_cutpoints WHERE user_id = $1", user_id)

    unstarted = (await client.get("/api/rank?kind=movie")).json()
    assert unstarted["rated_total"] == 0 and unstarted["fitting"] is False, unstarted
    assert "0 rated" in unstarted["why"], unstarted["why"]

    dropped = await client.post("/api/rank/drop?kind=movie", json={"title_id": 1, "tier": 4})
    assert dropped.status_code == 200, dropped.text
    # The stamp, read from the column rather than from the response: decision 117 gates `ledger`,
    # and what the board says about an owed fit is owed to every member, gate or no gate.
    assert await db.fetchval(
        "SELECT refit_requested_at FROM ledger_cutpoints WHERE user_id = $1 AND kind = 'movie'",
        user_id,
    ) is not None, "the drop's cache miss queued no fit, so this is not finding 9's window"

    owed = (await client.get("/api/rank?kind=movie")).json()
    assert owed["rated_total"] == 0, "the fixture gained a fit this test needs it not to have"
    assert owed["fitting"] is True, (
        f"the board reads the same as one nobody has started: rated={owed['rated']}, "
        f"why={owed['why']!r}"
    )
    assert "still being fitted" in owed["why"], owed["why"]
    assert "0 rated" not in owed["why"], owed["why"]

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
    assert f"{fitted['rated_total']} rated" in fitted["why"], fitted["why"]


async def test_a_constant_the_fit_cannot_use_is_a_503_on_the_board_route(db, ranked, monkeypatch):
    """Finding 10's Rank half. `hyperparams.load` raises `ValueError` on a bad constant and
    nothing caught it, so one hand-edited or badly restored number turned `GET /api/rank` into a
    500 for everyone with nothing on screen saying why. Not defaulted, deliberately: the defaults
    carry a different `hp_digest`, so serving them would invalidate every cached fit in the
    install. The Rate copy of this clause, and the bundle validation that stops such a file being
    swapped in at all, are `test_ledger_hyperparams.py`'s and `test_bundle_lifecycle.py`'s."""
    client, _user_id = ranked
    monkeypatch.setattr(client._transport.app.state, "hyperparams", None, raising=False)

    def unusable(*_args, **_kwargs):
        raise ValueError("straddle_z must be a positive number, got 0.0")

    monkeypatch.setattr(rank_api.hyperparams, "load", unusable)
    refused = await client.get("/api/rank?kind=movie")
    assert refused.status_code == 503, refused.text
    assert "ledger constants" in refused.json()["detail"]

    # The same clause against the real reader on a file that is not a JSON object, which is the
    # malformation this test could not see: patching `load` to raise `ValueError` can only ever
    # prove the class it raises, and `from_mapping` met a top-level array with an `AttributeError`
    # that this `except` does not catch — so the board answered 500. [M4.10 cycle 1, M410-R1-03]
    def shapeless(*_args, **_kwargs):
        return rank_api.hyperparams.from_mapping([])

    monkeypatch.setattr(rank_api.hyperparams, "load", shapeless)
    refused = await client.get("/api/rank?kind=movie")
    assert refused.status_code == 503, refused.text
    assert "ledger constants" in refused.json()["detail"]


# --- decision 117: the gate is applied at the exit --------------------------------------------


async def test_the_board_ships_no_model_numbers_until_the_toggle_is_on(db, ranked):
    """Decision 117 governs "the rail and every inline annotation", and `rail.redact` deletes
    the keys rather than marking them hidden — a promise kept in CSS is still on the wire."""
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
    """§6.7's rail is decision 117's, so the line the answer returns is gated with everything
    else — and it names the arm rather than asserting one (proposal 120)."""
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
    """`GET /api/rank/queue` was the one Rank route calling neither `visible_to` nor `redact`.

    `Pair.public()` ships the arm *and* its sentence, and the held-out one reads "uniform-random,
    held out - this pair never tunes the model", rendered unconditionally by the surface. A
    person told a comparison does not count has been given a reason to answer it carelessly, and
    those are the 10% §13 calls the only admissible evaluation data. Walked recursively rather
    than checked at the top level, because the point of `redact` is that a key added later
    inherits the gate: a test that named one path would pass over a second copy nested anywhere.
    [M4.10 finding 16]
    """
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
    """Finding 15: the arm was re-rolled per GET, so the §13 stream was reload-selectable.

    `_rng` was a `SystemRandom` and `queue.draw` picks the arm before the pair, so a client could
    refresh until the pair was held out — or until it was not — and only then answer. That is
    worse than a pair-level door: the arm's *rate* is the thing §13 needs independent of the
    model, and a rate a person can steer by pressing reload is independent of nothing. Derived
    from `(user, kind, answered)` under `SESSION_SECRET`, the draw is a function of the queue
    position instead, and looking is not an event. Deliberately does not touch the `_rng` seam:
    the derivation is the subject.
    """
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
    """Finding 12's other half at the seam that makes it true for a person.

    `test_rank_queue.py` asserts it of `queue._exploration` and of `queue.draw`; neither can see
    the route, where the set is a `read.asked_pairs` query forwarded twice before it reaches the
    arm. `_draw` declares `asked` keyword-only with no default, so deleting the route's read is a
    loud TypeError — but dropping `asked=asked` from `draw`'s two `_exploration` calls is silent,
    and with that one hop removed this loop re-serves a pair the fixture had already answered at
    the fourth draw. Each repeat is an independent Davidson row, so ten repeats shrink that
    pair's posterior by the root of ten on the strength of one judgement: §13's reliability
    inflation, reached through the selector. [M4.10 finding 12; cycle 1, M410-REV3]

    The arm is forced because §6.3's mix gives exploration a fifth of the draws and it is the
    only arm that consults the set — the held-out arm must not (§13), and the boundary arm's own
    repetition is M3-open-points §3.1's remaining half.
    """
    client, user_id = ranked
    # The four duels the fixture already wrote, so the set the route reads is non-empty before
    # the first GET and a route ignoring it has something to trip over immediately.
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


# --- §6.3's routes ---------------------------------------------------------------------------


async def test_the_drop_route_answers_with_the_board_under_the_filters_it_was_given(db, ranked):
    """A drop while a filter is on must not silently clear it. Without the filters on the
    route, the client would have to choose between rendering a board it did not ask for and
    making a second request for one it already had.

    And decision 204's half, which until now was asserted only against `drop.drop` itself: the
    domain test hands it `filtered=True`, so `api/rank.py`'s `filtered=bool(filters.active())` —
    the one line joining the decision to the surface a person uses — could be deleted with this
    row still green, after which a filtered two-neighbour gesture would store two append-only
    comparisons the person never made. [decision 204; M4.10 cycle 1, M410-REV1-D204-01]
    """
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

    # One body, posted twice, with the filter the only difference — because the flag's whole
    # effect IS the difference, and `q=Title` matches every title in this fixture, so the board
    # the two drops were made on is the same board and only "was a filter on?" changes. That is
    # also the case decision 204 turns on: the route cannot know that this particular filter
    # happens to hide nothing, and the client reads `above`/`below` off whatever it can see.
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

    # The same body with no filter, so the A/B cannot be satisfied by a route that never writes
    # neighbour duels at all.
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
    """§4.1 rule 5: there is no unpartitioned ranked read, so the URL cannot express one."""
    client, _user_id = ranked
    assert (await client.get("/api/rank")).status_code == 422
    assert (await client.get("/api/rank?kind=episode")).status_code == 422


async def test_the_tier_set_route_round_trips_and_warns(db, ranked):
    """Decision 11: the control is per-user, and the save "discards that user's learned
    cutpoints and queues a refit" — a warning the surface can only show if the route sends it."""
    client, user_id = ranked
    current = (await client.get("/api/rank/tiers")).json()
    assert current["tier_set"] == list(observations.DEFAULT_TIER_SET)
    assert "queues a refit" in current["warning"]

    saved = (
        await client.put("/api/rank/tiers", json={"tier_set": ["bad", "ok", "good"]})
    ).json()
    assert saved["k_changed"] and saved["refit_queued"]
    assert (await client.get("/api/rank?kind=movie")).json()["tier_set"] == ["bad", "ok", "good"]

    refused = await client.put("/api/rank/tiers", json={"tier_set": ["only"]})
    assert refused.status_code == 422
    assert user_id


async def test_the_whole_rank_surface_is_behind_a_session(db, app):
    """Every route, not a sample: a new one added without `ActiveUser` is the way this stops
    being true."""
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
