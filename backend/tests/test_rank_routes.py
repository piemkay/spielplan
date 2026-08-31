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

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import numpy as np
import pytest

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


async def test_the_answer_route_writes_the_arm_the_server_drew_it_under(db, ranked):
    """The mutation the review found: `selection="boundary"` hard-coded in the route puts every
    held-out pair into the adaptive stream, and nothing else in the suite notices.

    Drawn repeatedly so all three arms are exercised — §6.3's mix makes the held-out tenth rare,
    and a test that only ever saw a boundary pair would not be testing the interesting one.
    """
    client, user_id = ranked
    seen_arms = set()
    for _ in range(60):
        served = (await client.get("/api/rank/queue?kind=movie")).json()
        pair = served["pair"]
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
        assert row["selection"] == pair["arm"], (
            "the row's arm must be the arm the server drew, not a constant"
        )
        seen_arms.add(row["selection"])
        if len(seen_arms) == 3:
            break
    assert seen_arms == {
        queue.ARM_BOUNDARY, queue.ARM_EXPLORATION, queue.ARM_HOLDOUT
    }, f"all three arms have to reach the column; saw {sorted(seen_arms)}"


async def test_a_client_cannot_name_its_own_selection_arm(db, ranked):
    """§13's guard in one request. The body has no arm field, and a client that invents one is
    ignored rather than obeyed — a client that could choose would choose which stream its
    answers evaluate."""
    client, user_id = ranked
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
    assert row["selection"] == served["arm"]
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
    silently discard the pair in front of them."""
    client, _user_id = ranked
    served = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
    await client.post("/api/rank/drop?kind=movie", json={"title_id": 3, "tier": 5, "above": 1})
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


async def test_a_held_out_answer_moves_nothing_the_model_reads(db, ranked):
    """§13's guard, at the return path the review found nobody had looked at.

    The fit cannot see a held-out row — `load_observations` and `_load_local` both exclude it —
    so re-fitting after one is a no-op that can only perturb state. And it did: the incremental
    write stamps `last_observed_at = now` for every title with an observation, which resets
    §5.2's freshness inflation, which moves `sigma_eff`, which moves `straddle`, which is the
    predicate the *boundary* arm draws from. The evaluation stream would have been steering the
    selector it exists to audit, and the nightly path — which derives the stamp from the
    excluded set — would have quietly undone it every night.
    """
    client, user_id = ranked
    snapshot = (
        "SELECT title_id, s, sigma_eff, last_observed_at FROM ledger_state "
        "WHERE user_id = $1 ORDER BY title_id"
    )

    # Reaching a held-out draw means answering adaptive ones on the way — and those move the
    # clock legitimately, so the snapshot is taken immediately before the one that must not.
    before = None
    for _ in range(60):
        served = (await client.get("/api/rank/queue?kind=movie")).json()["pair"]
        if served["arm"] == queue.ARM_HOLDOUT:
            before = [dict(r) for r in await db.fetch(snapshot, user_id)]
        answered = await client.post(
            "/api/rank/queue/answer", json={"pair": served["token"], "outcome": "A"}
        )
        assert answered.status_code == 200
        if before is not None:
            break
    else:
        pytest.fail("no held-out pair was drawn in 60 attempts")

    stored = await db.fetchval(
        "SELECT count(*) FROM duel WHERE user_id = $1 AND selection = 'uniform_holdout'", user_id
    )
    assert stored >= 1, "the row is still written — it is the evaluation stream"

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
        await client.post("/api/setup/members", json={"name": "jenny", "role": "member"})
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
    }[served["arm"]]
    assert on["log"][0].endswith(phrase)


# --- §6.3's routes ---------------------------------------------------------------------------


async def test_the_drop_route_answers_with_the_board_under_the_filters_it_was_given(db, ranked):
    """A drop while a filter is on must not silently clear it. Without the filters on the
    route, the client would have to choose between rendering a board it did not ask for and
    making a second request for one it already had."""
    client, _user_id = ranked
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
