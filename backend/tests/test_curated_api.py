"""§6.6 Data's three ledger editors and its reject review, over HTTP. Spec v2.1 §6.6 Data, §8 stages 3,
6 and 7, §6.4, §4.1 rule 2; decisions 342, 445 and 446.

The rules are the domain's and `test_curated_editors.py` and `test_dna_review.py` hold them; what is
held here is what only the route layer can break:

* EACH LEDGER LISTS, WRITES, WITHDRAWS AND EXPORTS THROUGH ITS OWN ROUTES, with the bundle's rows
  read-only (409, the editor's sentence) and a withdrawn row gone (204, then 404).
* THE EXPORT IS A FILE THE IMPORTER READS. The bytes the route serves - not the domain function's
  return - are saved and read back by `validate._read_tsv`, `dna.parse_corrections` and
  `dna.load_axes`, under the name the Content-Disposition header gives, as a household saving the
  download would.
* NO HANDLER REACHES TWO EDITORS. Proposal 105's "never one merged screen", read out of
  `api/curated.py`'s own handlers: each ledger route calls exactly one `curated` module, and nothing
  the handlers share calls any.
* THE REVIEW IS HANDED ON AS READ: rejects newest first, a title's tags weakest first, a NULL and a
  0.01 confidence both present - no route parameter narrows either list.
* EVERY ROUTE IS AN ADMIN ROUTE, a member 403 and a stranger 401 on each.

Integration tests are skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from spielplan.api import curated as curated_api
from spielplan.curated import adjudications, axes, corrections
from spielplan.dna import review
from spielplan.importer import dna
from spielplan.importer import validate as validator
from spielplan.importer.report import ImportReport
from tests.fixtures import make_bundle as fx
from tests.test_acquisition_board import _bootstrap
from tests.test_curated_editors import AWKWARD, UNSHIPPED_FACET, _install, _saved
from tests.test_dna_review import TITLE as REVIEWED
from tests.test_dna_review import _reject
from tests.test_dna_review import _tag as _extracted

ROUTES = Path(__file__).resolve().parents[1] / "spielplan" / "api" / "curated.py"
EDITORS = {"adjudications", "corrections", "axes"}
TSV = "text/tab-separated-values; charset=utf-8"


@pytest.fixture
def bundle_dir(tmp_path) -> Path:
    """`test_curated_editors.py`'s fixture, restated rather than imported, for its own reason."""
    fx.make_bundle(tmp_path / "bundle")
    return tmp_path / "bundle"


def _attachment(response) -> str:
    """The file name the download is saved under, out of the header a browser reads it from."""
    assert response.headers["content-type"] == TSV, response.headers
    found = re.fullmatch(r'attachment; filename="([^"]+)"', response.headers["content-disposition"])
    assert found, response.headers["content-disposition"]
    return found.group(1)


# --- DNA verdicts ------------------------------------------------------------------------------


async def test_the_verdict_editor_lists_writes_exports_and_withdraws_over_http(
    db, app, bundle_dir, tmp_path
):
    """One household verdict through `POST`, listed first beside the bundle's two, exported under the
    importer's name and columns and read back by `validate._read_tsv` from the served bytes, then
    withdrawn - 204 and gone, 404 the second time - while a bundle row is 409 with the editor's own
    sentence and still there."""
    admin, _member = await _bootstrap(app)
    await _install(db, bundle_dir)

    written = await admin.post("/api/admin/curated/adjudications", json={
        "scope": "title", "title_id": 2, "term": "mood.dread", "action": "DROP_EVIDENCE",
        "quote": AWKWARD, "source": "trakt:comment", "note": "about the novel\tnot the film",
    })

    assert written.status_code == 201, written.text
    row = written.json()["row"]
    assert (row["origin"], row["action"], row["quote"]) == ("household", "DROP_EVIDENCE", AWKWARD)
    listed = (await admin.get("/api/admin/curated/adjudications")).json()
    assert listed["applies"] == curated_api.APPLIES_VERDICTS
    assert [r["origin"] for r in listed["rows"]] == ["household", "bundle", "bundle"]

    exported = await admin.get("/api/admin/curated/adjudications/export")
    assert exported.status_code == 200
    path = _saved(tmp_path, _attachment(exported), exported.content.decode("utf-8"))
    assert path.name == "adjudications_v1.tsv"
    report = ImportReport()
    parsed = validator._read_tsv(path, report, "adjudications", dna.ADJUDICATION_COLUMNS)
    assert report.ok, report.render()
    assert parsed == [{
        "scope": "title", "title_id": "2", "term": "mood.dread", "action": "DROP_EVIDENCE",
        "target": "", "quote": AWKWARD, "source": "trakt:comment", "note": "about the novel\tnot the film",
    }]

    bundle_row = next(r for r in listed["rows"] if r["origin"] == "bundle")
    refused = await admin.delete(f"/api/admin/curated/adjudications/{bundle_row['id']}")
    assert (refused.status_code, refused.json()["detail"]) == (409, adjudications.READ_ONLY)

    withdrawn = await admin.delete(f"/api/admin/curated/adjudications/{row['id']}")
    assert (withdrawn.status_code, withdrawn.content) == (204, b"")
    assert (await admin.delete(f"/api/admin/curated/adjudications/{row['id']}")).status_code == 404
    assert [r["origin"] for r in (await admin.get("/api/admin/curated/adjudications")).json()["rows"]] == [
        "bundle", "bundle"
    ]


async def test_a_verdict_the_applier_could_not_apply_is_a_409_with_the_editors_sentence(
    db, app, bundle_dir
):
    admin, _member = await _bootstrap(app)
    await _install(db, bundle_dir)

    refused = await admin.post("/api/admin/curated/adjudications", json={
        "scope": "global", "term": "mood.dread", "action": "DROP_EVIDENCE", "quote": "a line",
    })

    assert refused.status_code == 409
    assert refused.json()["detail"] == "DROP_EVIDENCE rules on one title's quote, so it names the title."


# --- credit facts ------------------------------------------------------------------------------


async def test_the_correction_editor_lists_writes_exports_and_withdraws_over_http(
    db, app, bundle_dir, tmp_path
):
    """The credit ledger's four routes, the export read back by `dna.parse_corrections` - the parser
    `load_corrections` uses - from the served bytes, under the bundle's own file name."""
    admin, _member = await _bootstrap(app)
    await _install(db, bundle_dir)

    written = await admin.post("/api/admin/curated/corrections", json={
        "title_id": 1, "kind": "composer", "value": "Elliot Goldenthal",
        "evidence": "https://example.invalid/heat\tend credits", "note": AWKWARD,
    })

    assert written.status_code == 201, written.text
    row = written.json()["row"]
    assert (row["origin"], row["kind"], row["value"]) == ("household", "composer", "Elliot Goldenthal")
    assert written.json()["applied"], "the save reports what reached the card"
    listed = (await admin.get("/api/admin/curated/corrections")).json()
    assert listed["applies"] == curated_api.APPLIES_CORRECTIONS
    assert [r["origin"] for r in listed["rows"]] == ["household", "bundle"]

    exported = await admin.get("/api/admin/curated/corrections/export")
    path = _saved(tmp_path, _attachment(exported), exported.content.decode("utf-8"))
    assert path.name == corrections.EXPORT_NAME
    report = ImportReport()
    assert dna.parse_corrections(path, report) == [dna.Correction(
        1, "composer", "Elliot Goldenthal", "https://example.invalid/heat\tend credits", AWKWARD
    )]
    assert report.ok, report.render()

    bundle_row = next(r for r in listed["rows"] if r["origin"] == "bundle")
    refused = await admin.delete(f"/api/admin/curated/corrections/{bundle_row['id']}")
    assert (refused.status_code, refused.json()["detail"]) == (409, corrections.READ_ONLY)
    assert (await admin.delete(f"/api/admin/curated/corrections/{row['id']}")).status_code == 204
    assert (await admin.delete(f"/api/admin/curated/corrections/{row['id']}")).status_code == 404

    opinion = await admin.post("/api/admin/curated/corrections", json={
        "title_id": 1, "kind": "composer", "value": "Somebody", "evidence": "  ",
    })
    assert opinion.status_code == 409
    assert opinion.json()["detail"].startswith("A correction carries the evidence that settles it")


# --- the axes ----------------------------------------------------------------------------------


async def test_the_axis_editor_lists_writes_exports_and_withdraws_over_http(db, app, bundle_dir, tmp_path):
    """Decision 342's editor over its four routes: the declared facets listed beside the bundle's
    axes, a household axis written whole, exported as `<facet>.tsv` and - once withdrawn, 204 - read
    back by `load_axes` from the served bytes as the same poles and terms; a bundle axis is 409."""
    admin, _member = await _bootstrap(app)
    await _install(db, bundle_dir)

    listed = (await admin.get("/api/admin/curated/axes")).json()
    assert listed["applies"] == curated_api.APPLIES_AXES
    assert UNSHIPPED_FACET in listed["facets"]
    assert {axis["facet"] for axis in listed["axes"]} == set(fx.AXES)

    written = await admin.post("/api/admin/curated/axes", json={
        "facet": UNSHIPPED_FACET, "left_pole": 'linear, "straight"', "right_pole": "fractured",
        "weights": [{"term": "structure.procedural", "weight": -0.75}, {"term": "pacing.patient",
                                                                         "weight": 1.0}],
    })
    assert written.status_code == 201, written.text
    axis = written.json()["axis"]
    assert (axis["origin"], axis["left_pole"]) == ("household", 'linear, "straight"')

    exported = await admin.get(f"/api/admin/curated/axes/{UNSHIPPED_FACET}/export")
    path = _saved(tmp_path, _attachment(exported), exported.content.decode("utf-8"))
    assert path.name == f"{UNSHIPPED_FACET}.tsv"
    withdrawn = await admin.delete(f"/api/admin/curated/axes/{UNSHIPPED_FACET}")
    assert withdrawn.status_code == 204
    assert (await admin.delete(f"/api/admin/curated/axes/{UNSHIPPED_FACET}")).status_code == 404
    report = ImportReport()
    await dna.load_axes(db, path.parent, "v1", report)
    assert report.ok, report.render()
    back = (await admin.get("/api/admin/curated/axes")).json()
    (reloaded,) = [a for a in back["axes"] if a["facet"] == UNSHIPPED_FACET]
    assert (reloaded["origin"], reloaded["left_pole"], reloaded["right_pole"]) == (
        "bundle", 'linear, "straight"', "fractured"
    )
    assert reloaded["weights"] == axis["weights"]

    refused = await admin.delete("/api/admin/curated/axes/mood")
    assert (refused.status_code, refused.json()["detail"]) == (409, axes.READ_ONLY)
    unknown = await admin.post("/api/admin/curated/axes", json={
        "facet": "nonsense", "left_pole": "a", "right_pole": "b",
        "weights": [{"term": "mood.dread", "weight": 0.5}],
    })
    assert unknown.status_code == 409 and "is not a facet of this vocabulary" in unknown.json()["detail"]


# --- the review --------------------------------------------------------------------------------


async def test_the_review_hands_on_both_orderings_and_filters_nothing(db, app, bundle_dir):
    """Decision 446 over the wire: the rejects newest first, and one title's extracted tags weakest
    first with a NULL confidence last and a 0.01 first - every tag the title carries at the version,
    none cut, whatever a screen of "low evidence" might have been tempted to hide."""
    admin, _member = await _bootstrap(app)
    await _install(db, bundle_dir)
    for n in (2, 3, 1):
        await _reject(db, n)
    await _extracted(db, REVIEWED, "mood.dread", 0.9, 3)
    await _extracted(db, REVIEWED, "mood.cosy", None, 1)
    await _extracted(db, REVIEWED, "pacing.patient", 0.01, 1)
    await _extracted(db, REVIEWED, "structure.procedural", 0.4, 2)

    rejects = (await admin.get("/api/admin/dna/rejects")).json()
    evidence = (await admin.get(f"/api/admin/dna/evidence/{REVIEWED}")).json()

    assert [r["term"] for r in rejects["rejects"]] == ["mood.term_3", "mood.term_2", "mood.term_1"]
    assert rejects["limit"] == review.REJECT_LIMIT
    assert {"quote", "salience", "rule_violated"} <= set(rejects["rejects"][0])
    assert (evidence["title_id"], evidence["version"]) == (REVIEWED, "v1")
    assert [t["term"] for t in evidence["tags"]] == [
        "pacing.patient", "structure.procedural", "mood.dread", "mood.cosy"
    ]
    assert [t["confidence"] for t in evidence["tags"]][-1] is None


# --- the gate ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/admin/curated/adjudications", None),
        ("POST", "/api/admin/curated/adjudications", {"scope": "global", "term": "x", "action": "DROP"}),
        ("DELETE", "/api/admin/curated/adjudications/1", None),
        ("GET", "/api/admin/curated/adjudications/export", None),
        ("GET", "/api/admin/curated/corrections", None),
        ("POST", "/api/admin/curated/corrections",
         {"title_id": 1, "kind": "composer", "value": "x", "evidence": "y"}),
        ("DELETE", "/api/admin/curated/corrections/1", None),
        ("GET", "/api/admin/curated/corrections/export", None),
        ("GET", "/api/admin/curated/axes", None),
        ("POST", "/api/admin/curated/axes",
         {"facet": "mood", "left_pole": "a", "right_pole": "b", "weights": []}),
        ("DELETE", "/api/admin/curated/axes/mood", None),
        ("GET", "/api/admin/curated/axes/mood/export", None),
        ("GET", "/api/admin/dna/rejects", None),
        ("GET", "/api/admin/dna/evidence/1", None),
    ],
)
async def test_every_editor_and_review_route_refuses_a_member_and_a_stranger(app, db, method, path, body):
    _admin, member = await _bootstrap(app)
    kwargs = {} if body is None else {"json": body}

    assert (await member.request(method, path, **kwargs)).status_code == 403
    assert (await app().request(method, path, **kwargs)).status_code == 401


# --- no merged write path (proposal 105, decision 445) -----------------------------------------


def _routes_of(tree: ast.Module) -> dict[str, tuple[str, ast.AsyncFunctionDef]]:
    """Each route handler in the module, keyed by its name, with the path its decorator declares."""
    found = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == "router"):
                found[node.name] = (decorator.args[0].value, node)
    return found


def _editors_called(node: ast.AST) -> set[str]:
    """The `curated` modules a function calls into, however the call is nested."""
    return {
        inner.func.value.id
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
        and isinstance(inner.func.value, ast.Name) and inner.func.value.id in EDITORS
    }


def test_each_editor_route_calls_exactly_one_editor_and_nothing_shared_calls_any():
    """"Three separate editors with separate semantics, never one merged screen" (§6.6 Data), one
    layer up from `test_curated_editors.py`'s reading of the three modules: every `/curated/` handler
    calls into exactly one of them - the one its path names - every review handler into none, and
    the module's own helpers into none, so no route can become the merged write path by dispatching
    on a name."""
    assert _editors_called(ast.parse("axes.author(conn)\nx = adjudications")) == {"axes"}, (
        "the reader sees a call and not a bare name"
    )
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    routes = _routes_of(tree)

    ledger = {name: path for name, (path, _node) in routes.items() if path.startswith("/curated/")}
    assert len(ledger) == 12 and len(routes) == 14, sorted(routes)
    for name, (path, node) in routes.items():
        called = _editors_called(node)
        if path.startswith("/curated/"):
            assert called == {path.split("/")[2]}, f"{name} ({path}) calls {sorted(called)}"
        else:
            assert called == set(), f"{name} ({path}) is a review route and calls {sorted(called)}"
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in routes:
            assert _editors_called(node) == set(), f"the shared helper {node.name} calls an editor"
