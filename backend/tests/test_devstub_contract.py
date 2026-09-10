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
