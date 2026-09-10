"""The dev harness must not drift from the real API.

`ops/devstub.py` exists so the front end can be developed on a machine with no Postgres. That is
only safe while it answers the same paths as `spielplan.app`; a harness that quietly diverges
teaches the UI a contract the real backend does not honour.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]


def _paths(app) -> set[str]:
    return set(app.openapi()["paths"])


@pytest.fixture(scope="module")
def apps():
    sys.path.insert(0, str(ROOT))
    import importlib.util

    from spielplan.app import app as real
    from spielplan.core.config import settings

    spec = importlib.util.spec_from_file_location("devstub", ROOT / "ops" / "devstub.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed, which is what `import` itself does. Without it the
    # harness's Pydantic models cannot resolve their own annotations: `ops/devstub.py` carries
    # `from __future__ import annotations`, so every annotation is a string that Pydantic
    # resolves through `sys.modules[cls.__module__]` when FastAPI builds the schema — and this
    # module was not there. The symptom is a `class-not-fully-defined` error from `openapi()`
    # naming whichever model happens to be built first, which says nothing about the real
    # problem.
    sys.modules[spec.name] = module
    # Executing the harness is not running it, and one line of it is written for the process it
    # normally is: `ops/devstub.py` sets SPIELPLAN_INSECURE_DEV at import, which decision 181
    # makes the one documented way past §2's config refusals — and it also re-opens `/api/docs`
    # and `/openapi.json` (`app.py:213`). Left in `os.environ` it disarmed both for every test
    # that ran after this file, silently, from the first `settings.cache_clear()` onwards: the
    # `lru_cache` is what hid it until some later fixture dropped it. It surfaced four files away
    # as `test_http_seam.py`'s anonymous-schema assertion, which is the wrong place to debug it.
    # Put back what the harness changed, and drop the cache so nothing it constructed under the
    # flag survives into the next test. [M4.7 sec-12, spec-04; decision 181]
    flag = os.environ.get("SPIELPLAN_INSECURE_DEV")
    try:
        spec.loader.exec_module(module)
    finally:
        if flag is None:
            os.environ.pop("SPIELPLAN_INSECURE_DEV", None)
        else:
            os.environ["SPIELPLAN_INSECURE_DEV"] = flag
        settings.cache_clear()
    return real, module.app


def test_harness_covers_every_path_the_front_end_uses(apps):
    real, stub = apps
    real_paths = _paths(real)
    stub_paths = _paths(stub)

    # Paths the front end never calls; the harness may skip them.
    #
    # It used to carry four more. `/api/auth/password`, `/api/auth/pin` and `/api/auth/switch`
    # were exempted as "never called in M0 scope" and are called by four shipped components —
    # `routes/account/password/+page.svelte`, `routes/account/+page.svelte` and
    # `lib/components/AccountChip.svelte` — so §3.1's forced first-login change, the screen
    # every member meets first, had no local harness while this set said it needed none. An
    # exemption is a claim about the front end, and nothing re-checked it after M0. `/api/docs`
    # was the fourth, and it exempted nothing at any point: FastAPI registers both renderers and
    # the schema route with `include_in_schema=False`, so they have never been in
    # `openapi()["paths"]` — the set this test subtracts from — and the entry was inert from the
    # day it was written. M4.7 also stopped serving `/api/docs` over HTTP (sec-12), but that is a
    # different surface, and dropping the entry is right either way: an exemption naming a path
    # the comparison cannot see reads as a real carve-out to whoever inherits the set.
    # `test_the_docs_renderers_are_not_schema_paths_even_when_served` is why that is stated
    # rather than believed. [M4.7 test-14, sec-12]
    exempt = {
        "/api/titles/{title_id}/similar-by-term",
    }
    missing = real_paths - stub_paths - exempt
    assert not missing, f"dev harness is missing real routes: {sorted(missing)}"


def test_the_docs_renderers_are_not_schema_paths_even_when_served(monkeypatch):
    """The exempt set above dropped `/api/docs` on a reason that had to be checked to be stated.

    Built under `SPIELPLAN_INSECURE_DEV`, which is the one configuration where the renderers and
    the schema route are actually mounted (`app.py`'s `docs_url`/`redoc_url`/`openapi_url`), so
    this asks the question in the only place it can be answered wrongly. FastAPI adds all three
    with `include_in_schema=False` and `get_openapi` walks `APIRoute`s alone, which is why the
    two comparisons in this file never saw them and why the exemption was carrying no weight.
    Should a FastAPI upgrade put them in the document, the coverage test above would start
    demanding that the dev harness answer `/api/docs`; that is a wrong instruction to receive
    from a diff about routes, and this is where it is caught instead. [M4.7 test-14]
    """
    from spielplan.app import create_app
    from spielplan.core.config import settings

    monkeypatch.setenv("SPIELPLAN_INSECURE_DEV", "1")
    settings.cache_clear()
    try:
        served = create_app()
    finally:
        # Same discipline as the fixture above: the cache is what carries the flag into the next
        # test, and monkeypatch's own teardown runs after this.
        settings.cache_clear()

    renderers = {"/api/docs", "/redoc", "/openapi.json"}
    # `getattr` because an included router is one opaque `_IncludedRouter` with no `.path` under
    # FastAPI 0.141 (see `test_static_contracts.py`'s router-mount guard, which is the file that
    # learned it). Lossy in the safe direction here: a renderer whose route stops carrying a path
    # fails this assertion rather than satisfying it.
    routed = {getattr(route, "path", None) for route in served.routes}
    assert renderers <= routed, "the dev flag no longer mounts the renderers, so this asks nothing"
    assert not renderers & set(served.openapi()["paths"])


def test_harness_invents_no_routes(apps):
    real, stub = apps
    invented = _paths(stub) - _paths(real)
    assert not invented, (
        "the dev harness answers paths the real app does not have — the UI would be built "
        f"against a contract that does not exist: {sorted(invented)}"
    )


def test_harness_requires_the_query_parameters_the_app_requires(apps):
    """A path in common is not a contract in common.

    M4.9 made `kind` required on `/api/people/{person_id}` (§4.1 rule 5, finding 13) and the
    coverage test above would have stayed green with the harness still answering it without
    one — the front end would have been built against a filmography route that needs no
    selection, and the first run against the real backend would be a 422 from a screen that
    worked all week. So the comparison is the *required query parameters*, both ways: a harness
    that demands less teaches a contract the app refuses, and one that demands more teaches the
    UI to send a parameter nothing reads.

    Read out of both OpenAPI documents rather than out of either source, because that is the
    document a client is written against.
    """
    real, stub = apps

    def required(app) -> dict[tuple[str, str], set[str]]:
        out: dict[tuple[str, str], set[str]] = {}
        for path, operations in app.openapi()["paths"].items():
            for method, operation in operations.items():
                out[(path, method)] = {
                    parameter["name"]
                    for parameter in operation.get("parameters", [])
                    if parameter.get("in") == "query" and parameter.get("required")
                }
        return out

    app_side, stub_side = required(real), required(stub)
    shared = sorted(set(app_side) & set(stub_side))
    assert shared, "no operation is answered by both, so this comparison asks nothing"
    drift = {
        key: (sorted(app_side[key]), sorted(stub_side[key]))
        for key in shared
        if app_side[key] != stub_side[key]
    }
    assert not drift, f"harness and app disagree about required query parameters: {drift}"


def test_the_harness_derives_the_dna_facet_the_way_the_importer_does(apps):
    """§6.8's palette is keyed on the vocabulary facet id, and the bundle's `facet` column
    carries the extraction label instead — `mood_tone` where the app stores `mood` (M4.9
    finding 1). The harness reads the bundle's sqlite directly, so it is the one place that can
    still serve the shipped label after the importer stopped storing it, and a card whose chips
    were all neutral in the harness and coloured against the real backend is a day spent on the
    palette rather than on the bug.

    Asserted on the import and on every read, rather than on a payload: `app_facet` is the rule,
    and a harness that re-derived it locally would be the second copy the rule was extracted to
    prevent.
    """
    source = (ROOT / "ops" / "devstub.py").read_text(encoding="utf-8")
    assert "from spielplan.importer.dna import app_facet" in source
    for row in ("r", "g"):
        raw, wrapped = f'{row}["facet"]', f'app_facet({row}["term"], {row}["facet"])'
        assert source.count(raw) == source.count(wrapped) > 0, (
            f'{raw} is read somewhere other than through app_facet'
        )


def test_harness_uses_the_real_validator(apps):
    """The import report the harness renders must be produced by the real code, not mocked —
    otherwise the one screen that shows §4.1 enforcement would be theatre."""
    source = (ROOT / "ops" / "devstub.py").read_text(encoding="utf-8")
    assert "from spielplan.importer import bundle as bundle_import" in source
    assert "bundle_import.validate(" in source


def test_reading_the_harness_leaves_the_config_refusals_armed(apps):
    """The one thing this file must not do to the rest of the suite.

    Both fields are passed explicitly, so nothing in the environment can make this pass by
    accident — the only way §2's refusal does not fire is `SPIELPLAN_INSECURE_DEV` still being
    set, which is the leak itself. Asserted here rather than trusted to the `finally` above,
    because the symptom of the leak is a failure in another file entirely and the cost of
    catching it there was an hour of bisecting. [M4.7 spec-04; decision 181]
    """
    from spielplan.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(public_url="", session_secret="")


# --- M4.9 review cycle 1: two divergences the path comparison above cannot see -------------
#
# Both tests below read the executed harness rather than its OpenAPI document, because both
# defects were inside a route that answers the right path with the right parameters. A contract
# is what a client is written against, and a client is written against the numbers and the
# sentences in the body as much as against the shape of the URL.


@pytest.fixture(scope="module")
def harness(apps):
    """The executed harness module. `apps` is what loads and registers it; this is the same
    object under the name `import` gave it, for the tests that read its internals."""
    return sys.modules["devstub"]


def test_the_harness_weighs_a_projected_term_once(harness):
    """Decision 188 bounded the projected tier and the harness applied it to one of its two
    projected-weight sites.

    `_dna()` -- the why-line, the chip and the catalogue's DNA predicate -- saturated;
    `_tonight_dna()` still ran the unbounded `0.30 * n_sources` the app had just been repaired
    for. Tonight is the half decision 188's own thesis says the change moves: the tilt vectors,
    the authored-axis positions, `terms_carried_by` and §6.2 step 7's match lines all read the
    number, so the harness taught the defect on the surface it was removed from, while its own
    other door served the repair.

    One number and one identity, not a substring: the fixture's `structure.procedural` carries
    `n_sources` 2, which `db/dna_terms.TERM_WEIGHT` weighs `0.30 * (2 / 3)`. The unrepaired site
    answered 0.60 -- three times the app's, and above the extracted floor for any corpus row
    with `n_sources >= 3`. [M4.9 review cycle 1: M49-D188-01; decision 188]
    """
    tonight = harness._tonight_dna()
    assert tonight[2]["structure.procedural"] == pytest.approx(0.30 * (2 / 3)), (
        "the harness weighs a projection by the app's saturating form, not by `0.30 * n_sources`"
    )
    elsewhere = {
        title_id: {term.term: weight for term, weight in ranked}
        for title_id, ranked in harness._dna().items()
    }
    assert tonight == elsewhere, (
        "the harness holds two weightings for one row again: Tonight and the why-line read the "
        "same `dna_projected` rows and must not disagree about how loudly a term speaks"
    )


async def test_the_harness_echoes_the_verdict_line_the_app_sends(harness):
    """§6.1's per-response echo, which `RateModelLog` renders on the Rate surface.

    The app puts `rail.verdict_line`'s sentence in `log[0]` (`rate/session.py:934`) because §6.8
    forbids a bare model number and `verdict(title 3) = liked` is exactly one: nothing on the
    client can resolve that integer back into a film. The harness recorded the repaired line on
    its own rail and shipped the bare-id string on the wire in the same request, so the stub
    contradicted itself one drawer apart -- and a front end built against it would have been
    built against the defect this milestone shipped to remove.

    Driven through the ASGI app rather than compared as source, because the divergence is in a
    response body and this file's other comparisons are all documents. [M49-HOME-01; finding 23]
    """
    import httpx

    from spielplan.home import rail

    transport = httpx.ASGITransport(app=harness.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://harness") as client:
        me = (await client.post(
            "/api/setup/admin", json={"name": "vera", "password": "correct horse battery"}
        )).json()
        started = (await client.post("/api/rate/session", json={"mode": "sweep"})).json()
        card = started["card"]
        assert card and card["type"] == "sweep", started
        answered = (await client.post(
            "/api/rate/verdict", json={"card_token": card["token"], "value": 2}
        )).json()

    line = answered["log"][0]
    assert f"title {card['title']['id']}" not in line, (
        f"§6.8 forbids a bare model number and the response log printed one: {line!r}"
    )
    # The app's own builder, called with no `refit_ms`, is the sentence up to the millisecond
    # count the harness's fake ledger invents; comparing the whole string would compare that.
    head, sep, tail = line.partition(", incremental refit ")
    assert head == rail.verdict_line(me["name"], card["title"]["name"], "liked"), (
        f"the response log is not §6.7's line: {line!r}"
    )
    assert sep and tail.endswith(" ms"), f"the refit millisecond count is missing: {line!r}"
    assert any(event["text"] == line for event in rail.recent(user_id=me["id"])), (
        "the drawer and the response echo one sentence about one write, not two formats of it"
    )


# --- M4.10: three payload shapes the app moved, and the block machine under them ------------
#
# Every assertion below is made against a response body rather than against the OpenAPI
# document, for the reason the M4.9 block above gives: all four divergences are inside a route
# that answers the right path with the right parameters, and a client is written against the
# keys and the sentences in the body. Where the app decides something the harness is supposed to
# borrow, the assertion is made against the real module's own object rather than against a
# literal, so a harness holding a second spelling of a sentence fails here rather than on the
# day somebody reads both.


@pytest.fixture
def fresh(apps):
    """A harness with nobody in it and nothing rated, one per test.

    `STATE` is module-global and the tests below each need a different starting position - a
    member with no verdicts at all, and a member who has rated the whole fixture - so they
    cannot share one. Re-executing the file is 40 ms after the first load and is the only reset
    that cannot leave a key behind, which a hand-written teardown of six `STATE` entries could.

    The environment dance is the `apps` fixture's, for the reason its own comment gives at
    length: `ops/devstub.py` sets SPIELPLAN_INSECURE_DEV at import, and a copy of it left in
    `os.environ` disarms §2's config refusals for every test that runs afterwards - four files
    away, silently. [M4.7 spec-04; decision 181]
    """
    import importlib.util

    from spielplan.core.config import settings

    name = "devstub_fresh"
    spec = importlib.util.spec_from_file_location(name, ROOT / "ops" / "devstub.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    flag = os.environ.get("SPIELPLAN_INSECURE_DEV")
    try:
        spec.loader.exec_module(module)
    finally:
        if flag is None:
            os.environ.pop("SPIELPLAN_INSECURE_DEV", None)
        else:
            os.environ["SPIELPLAN_INSECURE_DEV"] = flag
        settings.cache_clear()
    try:
        yield module
    finally:
        sys.modules.pop(name, None)


def _client(module):
    """The harness over ASGI, as `test_the_harness_echoes_the_verdict_line_the_app_sends` drives
    it. Returned unentered, so each test can say `async with`."""
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=module.app), base_url="http://harness"
    )


async def _admin(client):
    """§3.1's first-boot admin, which is also the harness's only door to a session cookie."""
    res = await client.post(
        "/api/setup/admin", json={"name": "vera", "password": "correct horse battery"}
    )
    assert res.is_success, res.text
    return res.json()


async def _answer(client, *, value: int = 2):
    """Answer whatever card is on the table, however the harness chose to fill the slot.

    Returns `(type, payload)`, or `(None, payload)` when there is no card - which is the drained
    state two of the tests below are about, so it is a result rather than an assertion failure.
    """
    envelope = (await client.get("/api/rate")).json()
    card = envelope["card"]
    if card is None:
        return None, envelope
    if card["type"] == "sweep":
        res = await client.post(
            "/api/rate/verdict", json={"card_token": card["token"], "value": value}
        )
    else:
        res = await client.post(
            "/api/rate/duel", json={"card_token": card["token"], "outcome": "A"}
        )
    assert res.status_code == 200, res.text
    return card["type"], res.json()


def _every_key(value, into=None):
    """Every key anywhere inside a payload, at any depth - the shape `11-rate.spec.js` reads a
    card with. A gated key that moved one level deeper is still on the wire."""
    into = set() if into is None else into
    if isinstance(value, dict):
        for key, nested in value.items():
            into.add(key)
            _every_key(nested, into)
    elif isinstance(value, list):
        for item in value:
            _every_key(item, into)
    return into


async def test_the_harness_gates_the_queue_arm_exactly_as_the_route_does(fresh):
    """§13's guard, which `GET /api/rank/queue` was the one route on its surface not to apply.

    `queue.Pair.public()` ships the arm together with its sentence - on the held-out tenth,
    "uniform-random, held out - this pair never tunes the model" - and §13 makes that stream the
    *only* data the tier model is evaluated against. A person told which of their answers does
    not count has been given a reason to answer it carelessly, so the app moved both under the
    gated `model` key and gave the pair one arm-independent why-line (§6.8 still owes a line).
    The harness went on shipping `arm` and the arm's sentence plainly to every member, which is
    the front end being built against a key the real backend redacts.

    The key SETS are pinned, both ways. `arm` absent is not enough on its own: the defect was a
    flat payload, and a payload that grew a second copy of the pair's reason, or kept the arm
    one level deeper, would satisfy a `not in` and still be the wrong contract.
    [M4.10 finding 16; decision 117]
    """
    from spielplan.api.rank import _QUEUE_WHY
    from spielplan.home import rail
    from spielplan.rank import queue as rank_queue

    async with _client(fresh) as client:
        await _admin(client)
        for _ in range(6):
            kind, _served = await _answer(client)
            assert kind is not None, "the harness must be able to seed a board"

        plain = (await client.get("/api/rank/queue?kind=movie")).json()
        assert plain["pair"] is not None, plain
        assert set(plain["pair"]) == {
            "title_a", "title_b", "name_a", "name_b", "token", "reason"
        }, plain["pair"]
        assert "arm" not in _every_key(plain), plain
        assert plain["pair"]["reason"] == _QUEUE_WHY
        # The sentence, not only the key: the arm's own line is what names the held-out stream.
        assert "never tunes the model" not in repr(plain), plain

        await client.post("/api/auth/preferences", json={"show_model": True})
        gated = (await client.get("/api/rank/queue?kind=movie")).json()
        assert set(gated["pair"]) == {
            "title_a", "title_b", "name_a", "name_b", "token", "reason", "model"
        }, gated["pair"]
        assert gated["pair"]["reason"] == _QUEUE_WHY, "the why-line says the same on every arm"
        arm = gated["pair"]["model"]
        assert arm["arm"] in (
            rank_queue.ARM_BOUNDARY, rank_queue.ARM_EXPLORATION, rank_queue.ARM_HOLDOUT
        ), arm
        assert arm["arm"] in rail.ARM_PHRASES, arm
        assert arm["reason"] != _QUEUE_WHY, "the arm's own sentence is what the rail opens onto"


async def test_the_harness_names_which_pool_is_empty_rather_than_always_the_queue(fresh):
    """§6.1's empty state has three causes and the harness answered one sentence for all of them.

    A Battle-mode session on an account with no verdicts is not a drained queue: there is
    nothing to compare because no two ratings sit in the same band yet. Telling that person
    "You've rated everything we can queue right now" sends them to Rank when one sweep would fix
    it, which §6.8 makes a matter of honesty rather than of wording. Both reachable causes are
    compared with `DRAINED_CAUSES` itself, so the harness cannot hold a second copy of either
    sentence; the third (`both`) needs a drained queue AND no band with two members,
    which this fixture cannot reach because draining it rates eight titles. [M4.10 finding 20]
    """
    from spielplan.rate import session as rate_session

    async with _client(fresh) as client:
        await _admin(client)
        battle = (await client.post("/api/rate/session", json={"mode": "battle"})).json()
        assert battle["card"] is None, battle["card"]
        assert battle["drained"] == rate_session.DRAINED_CAUSES["pool"], battle["drained"]
        assert battle["drained"]["cause"] == "pool"

        swept = (await client.post(
            "/api/rate/session", json={"restart": True, "mode": "sweep"}
        )).json()
        assert swept["card"] is not None, "a fresh sweep queue has the whole fixture in it"
        for _ in range(20):
            kind, payload = await _answer(client)
            if kind is None:
                break
        else:
            raise AssertionError("the sweep queue did not drain in twenty answers")
        assert payload["drained"] == rate_session.DRAINED_CAUSES["queue"], payload["drained"]
        assert payload["drained"]["cause"] == "queue"


async def test_the_harness_runs_the_block_machine_on_the_counter_the_app_counts(fresh):
    """Decisions 200 and 199, which meet at the same place: the roll.

    Decision 200 derives the card type from the session's monotone observation index rather than
    from the slot, because fifteen is odd - slot 15 was a sweep and `advance` rolls slot 15 to
    slot 1, which was a sweep too, so every block ended and the next began with the same type.
    The harness passed the slot to `card_type_for` either way, so after the app's repair it was
    not merely teaching the old alternation: the same positional argument now means the monotone
    index, and the harness's slot 1 asked for a BATTLE from a member with nothing rated.

    Decision 199 is the other end of the same block. `advance` rolls the counter ON the
    fifteenth observation, so comparing block indexes alone disabled Undo on the same round trip
    that answered card 15 - the tap still on screen, unretractable, at the end of a run where
    fatigue mis-taps live. The fifteenth stays reachable until the sixteenth lands, and the
    sixteenth commits the block it ended: retracting it does not re-open block 0, which is the
    clause the arithmetic alone does not give.
    """
    async with _client(fresh) as client:
        await _admin(client)
        opened = (await client.get("/api/rate")).json()
        assert opened["session"]["block"]["serving"] == "sweep", (
            "index 0 is a sweep, so a new labeller opens on the one card they can answer"
        )
        assert opened["card"]["type"] == "sweep"

        payload = opened
        for i in range(15):
            kind, payload = await _answer(client)
            assert kind is not None, f"the fixture must outlast the block (observation {i + 1})"
        block = payload["session"]["block"]
        assert (block["index"], block["slot"]) == (1, 1), block
        assert block["serving"] == "battle", (
            "the roll alternates too: index 14 called for a sweep, index 15 calls for a battle"
        )
        # Decision 199: the fifteenth is still on screen, so it is still retractable.
        assert payload["undo"]["available"] is True, payload["undo"]

        kind, _sixteenth = await _answer(client)
        assert kind is not None
        undone = (await client.post("/api/rate/undo")).json()
        assert undone["session"]["block"]["slot"] == 1, undone["session"]["block"]
        assert undone["undo"] == {
            "available": False, "kind": None, "reason": "block_boundary"
        }, "the sixteenth committed the block it ended; retracting it does not un-commit it"
        refused = await client.post("/api/rate/undo")
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["reason"] == "block_boundary"


async def test_the_harness_marks_a_repaired_pair_that_fell_through_to_a_sweep(fresh):
    """§6.1's correction repairs the question, and sometimes it cannot.

    `_redraw_pair` keeps the half the person did not correct; `both` leaves nothing to keep, and
    a band with no other member leaves nothing to draw, so the slot honestly falls through to a
    sweep. The counter still says `battle` - `serving` is what the counter CALLS FOR, and a
    correction does not advance it - so the flip has to be reported on the card, and at this
    site it was not. `RateSweepCard.svelte` renders `substituted_for` as the sentence explaining
    why a battle slot is showing a sweep, so an unmarked substitution is a surface that changed
    the question and said nothing about it. [M4.10 finding 21]

    Arranged so that the fall-through is the only possible answer: two titles liked and one
    disliked, which is one band of two and one of one, and correcting `both` empties the only
    band that could have offered a pair.
    """
    async with _client(fresh) as client:
        await _admin(client)
        for value in (2, 2, 0):
            kind, payload = await _answer(client, value=value)
            assert kind == "sweep", f"the arrangement needs three sweep cards, got {kind}"
        card = payload["card"]
        assert card["type"] == "battle", payload["session"]["block"]
        repaired = (await client.post(
            "/api/rate/correction", json={"card_token": card["token"], "side": "both"}
        )).json()

        assert repaired["session"]["block"]["serving"] == "battle", (
            "a correction repairs the question rather than answering it, so the counter stands"
        )
        assert repaired["card"]["type"] == "sweep", repaired["card"]
        assert repaired["card"]["substituted_for"] == "battle", repaired["card"]


async def test_the_harness_serves_the_banner_population_and_link_the_app_serves(fresh):
    """§6.0's banner, both halves of finding 23, in the file Home is developed against.

    The app filters the banner's population by the live session's kinds and builds its CTA as
    `head=` alone (`shelves.pending_verdicts`; finding 23 and decision 203). The harness kept the
    old spelling on both counts, and neither generic guard in this file can see that:
    `test_harness_invents_no_routes` compares OpenAPI paths and
    `test_harness_requires_the_query_parameters_the_app_requires` compares required query
    parameters, while a dead parameter inside a link in a response BODY is in neither document. So
    the only in-repo example of the banner link was `/rate?mode=sweep&head=...`, naming a control
    `GET /api/rate` does not declare, and a films-only session was still offered a series the
    §6.1 queue will not serve - proposal 150's own failure mode, reached through the harness.

    The link is parsed back rather than compared with a literal, the way
    `test_home.py::test_the_banner_cta_carries_exactly_the_named_titles_as_the_queue_head`
    falsifies the app's: string equality would also pass on a link that carried the right
    parameters in a shape `GET /api/rate` answers 422.
    """
    from urllib.parse import parse_qs, urlsplit

    catalog = fresh._catalog()
    movie = next(t for t in catalog if t["kind"] == "movie")
    series = next(t for t in catalog if t["kind"] == "series")

    async with _client(fresh) as client:
        await _admin(client)
        for title in (movie, series):
            marked = await client.post(
                f"/api/titles/{title['id']}/state", json={"state": "seen"}
            )
            assert marked.status_code == 200, marked.text

        # No session has been opened yet, so the queue has not been narrowed and both kinds are
        # named - the app's own fallback when `rate_session` holds no live row.
        both = (await client.get("/api/home/pending-verdicts")).json()
        assert {n["kind"] for n in both["named"]} == {"movie", "series"}, both["named"]
        assert both["head_title_ids"] == [n["title_id"] for n in both["named"]], both
        for link in (both["cta"]["route"], both["cta"]["api"]):
            query = parse_qs(urlsplit(link).query)
            assert list(query) == ["head"], f"{link}: {query}"
            assert [int(i) for i in query["head"]] == both["head_title_ids"], link

        narrowed = await client.post("/api/rate/session", json={"kinds": ["movie"]})
        assert narrowed.status_code == 200, narrowed.text
        films = (await client.get("/api/home/pending-verdicts")).json()
        assert [n["title_id"] for n in films["named"]] == [movie["id"]], films["named"]
        assert films["count"] == 1, films
        assert series["name"] not in repr(films), (
            "the copy names a series the films-only queue cannot serve"
        )
