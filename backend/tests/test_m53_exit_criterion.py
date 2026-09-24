"""M5.3's exit criterion as something that has to run, and not only as something to read.

`ops/m53_exit_criterion.py` is decision 378's own deliverable, and §12's M5.3 row names it as the
instrument the criterion is measured by -- "eleven numbered checks, which refuses to run on the
fixture". Nothing in this tree executes it. `test_static_contracts.py` reads all ten exit scripts
as SOURCE TEXT -- ASCII console output, constant predicates, the figures they publish, the one
citation they make into the app -- which is a rule about what a file says and never about what a
database would answer it, so a statement naming a column the schema has never carried ships green
and is met for the first time on the run the milestone is closed by.

It fails there in the shape this family of scripts was built to refuse rather than as a crash:
`measure()` catches the exception, records that check as a failure with its traceback and lets the
run end in a score -- so the report reads as a criterion measured and come out no, over a question
that was never asked. [decision 378; M5.3 review cycle 1, M53-EXIT-01]
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import asyncpg

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "ops" / "m53_exit_criterion.py"

# The verbs this script sends, anchored and case-sensitive. Anchored because ` WITH (FORCE)` is
# the tail of a `DROP DATABASE` built by interpolation and ` with its evidence ` is prose;
# case-sensitive because "with the adjudication applier disabled" is one of the eleven check
# titles. Either would otherwise arrive here as a statement and fail on its own syntax, which is a
# guard reporting itself rather than the script.
_STATEMENT = re.compile(r"^(?:SELECT|INSERT|UPDATE|DELETE|WITH)\b")


def _statements(path: Path) -> list[tuple[int, str]]:
    """Every whole SQL statement the script holds as a literal, with the line it sits on.

    An f-string's pieces are skipped because a piece is not a statement: `derived_snapshot`'s
    per-table SELECT interpolates both the table and its column list, and preparing the fragment
    `SELECT ` would report a syntax error about this reader. Nothing is lost by the skip here,
    which is what makes it safe rather than convenient -- that one statement builds its identifiers
    out of `information_schema` at runtime, so it cannot name a column the database does not have.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    interpolated = {
        id(piece)
        for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
        for piece in ast.walk(node) if isinstance(piece, ast.Constant)
    }
    found = [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in interpolated and _STATEMENT.match(node.value)
    ]
    return sorted(found)


async def test_every_statement_the_exit_script_sends_prepares_against_the_schema(db):
    """The one defect class in a never-run script that reading its source cannot reach.

    `derived_snapshot` selected `value` from `display.platform_rating`, which has carried `score`
    since `0003_content.sql:177-184` and gained only `metric` and `scale` at `0015_seed.sql:208`.
    `value` is the corpus BUNDLE's name for that column -- `importer/load.py:283` maps
    `columns={"score": "value"}` on the way in -- so the statement read like the rest of the tree
    and could never execute. Checks 2, 5 and 6 call that helper, and they are decision 375's
    idempotence, the zero-outbound retry and the raw-store-before-parse check: three of the eleven
    reporting FAIL over a measurement none of them reached.

    PREPARE and not execute, for the reason the script makes a database of its own: the server
    parses the statement, resolves every name in it and infers every parameter type, which is the
    whole of what a wrong identifier can fail, while touching no row. [M5.3 review cycle 1,
    M53-EXIT-01]
    """
    statements = _statements(SCRIPT)
    assert len(statements) >= 20, (
        f"{SCRIPT.name} holds {len(statements)} SQL literals and this guard was written over "
        "twenty-seven. If the script now builds its statements some other way, this reads almost "
        "nothing and reports green: widen the reader rather than lower the floor."
    )
    refused = []
    for line, statement in statements:
        try:
            await db.prepare(statement)
        except asyncpg.PostgresError as refusal:
            flat = " ".join(statement.split())
            refused.append(
                f"ops/{SCRIPT.name}:{line}: {type(refusal).__name__}: {refusal}"
                f"\n      {flat[:96]}"
            )
    assert not refused, (
        "the exit criterion sends a statement this schema cannot answer, so the check that sends "
        "it reports a failure it never measured:\n  " + "\n  ".join(refused)
    )


# --- the negative control names the writer it stands in for ---------------------------------------

# The one statement in the app that puts a row into the extracted tier, and the loader it sits in.
# Read as text rather than imported, because what this guard is about is which BRANCH calls it.
_TIER_WRITE = "INSERT INTO dna_tag"
_TIER_LOADER = "load_tags"

# The other module allowed to hold that statement. Decision 432 gives the post-seed write to stage 6
# rather than stage 8 -- its runs are merged and written through `llm/consensus.store_title`
# (decision 337) -- so this is the second writer the docstring below already counts as "M5.4's stage
# 8 after it", arriving where the merge lives. It is admitted by path, and the reading this guard
# makes of the importer keeps its meaning only while no importer module can reach it, which the
# guard now asks as well. [M5.5, decisions 337 and 432]
_STAGE_WRITER = "backend/spielplan/llm/consensus.py"


def _function_source(path: Path, name: str) -> str:
    """One function's own lines, comments and all.

    Sliced rather than `inspect.getsource`d for the reason the file above reads the script as
    text: a rule about what a check SAYS must not be discharged by a sentence somewhere else in
    the module, and importing the script to reach `check_three` would run its argument parser.
    """
    text = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return "\n".join(text.splitlines()[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{path.as_posix()} holds no function named {name}")


def _calls(nodes: list[ast.stmt], name: str) -> list[ast.Call]:
    return [
        node for branch in nodes for node in ast.walk(branch)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    ]


def test_the_negative_control_does_not_say_a_re_import_puts_the_extracted_tier_back():
    """Decision 162 makes the seed import the only writer of the extracted tier, and the negative
    control said otherwise in both places it is explained.

    THE SENTENCE WAS FALSE AND THE CONTROL IS SOUND, which is why this is a guard over prose and
    not a change to either. `check_three` staged the revert with "put back exactly as a re-import
    would put it back - the import writes `dna_tag`", and
    `test_derive_ledgers.py`'s control said "something writes the tier again (a models-only
    re-import today, M5.4's stage 8 tomorrow)". `importer/bundle.py` forbids exactly that:
    `load_tags` sits inside the seed-only `if db is not None:` branch, whose `else:` names it in
    its own comment as one of "the content tiers decision 162 forbids re-importing", and
    `seed-once` refuses a second content bundle. So on a real install no re-import of any kind can
    put back a tag the ledger removed - the writers are the ONE seed import, before any derive,
    and M5.4's stage 8 after it.

    WHY IT IS WORTH A GUARD RATHER THAN AN EDIT. Check 3's detail line is printed into the
    milestone's close-out: an owner reads that the scar was reproduced against a sequence this
    install cannot perform, which is the same false reassurance the import report was cleaned of.
    And the file contradicted itself - check 10 uses "models-only re-import" with full precision
    four hundred lines further down - so a reader has no way to tell which of the two is the
    measurement. The rule is therefore two-sided: these two functions must NAME the writers the
    re-insert stands in for, so a rewrite that drops them reddens, and they may not reach for the
    word at all, so the sentence cannot come back in its old shape. The positive half is what the
    ban would otherwise leave open - "a models-only import" without the prefix is the same false
    claim - and the ban is what keeps the positive half from being satisfied beside it.

    The premise is re-derived here rather than restated, so the day a models-only import does
    write the tier this guard comes out with the sentence rather than outliving it.
    [decisions 162, 247, 376; plan section 7 check 3; M5.3 review cycle 1, M53-C1-SCAR-02]
    """
    package = REPO / "backend" / "spielplan"
    writers = sorted(
        path.relative_to(REPO).as_posix() for path in package.rglob("*.py")
        if _TIER_WRITE in path.read_text(encoding="utf-8")
    )
    assert writers == ["backend/spielplan/importer/dna.py", _STAGE_WRITER], (
        f"{writers} write the extracted tier; this guard reads the branch ONE loader is called "
        "from, and a writer beyond the seed import and stage 6's merge would make that reading say "
        "less than it appears to"
    )
    reaching = sorted(
        path.relative_to(REPO).as_posix() for path in (package / "importer").rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("spielplan.llm"))
        or (isinstance(node, ast.Import) and any(a.name.startswith("spielplan.llm") for a in node.names))
    )
    assert not reaching, (
        f"{reaching} import the LLM layer, whose merge writes the extracted tier; an importer that "
        "reached it could put the tier back through the second writer, which decision 162 forbids"
    )

    bundle = ast.parse((package / "importer" / "bundle.py").read_text(encoding="utf-8"))
    seeded, recurring = [], []
    for node in ast.walk(bundle):
        seeds_content = (
            isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name) and node.test.left.id == "db"
            and isinstance(node.test.ops[0], ast.IsNot)
        )
        if seeds_content:
            seeded += _calls(node.body, _TIER_LOADER)
            recurring += _calls(node.orelse, _TIER_LOADER)
    assert seeded and not recurring, (
        f"`{_TIER_LOADER}` is called from {len(seeded)} seed-only branch(es) and "
        f"{len(recurring)} recurring one(s). If a models-only import now writes the extracted "
        "tier, decision 162 has moved and the two texts below are owed the sentence back"
    )

    ledgers = REPO / "backend" / "tests" / "test_derive_ledgers.py"
    control = "test_a_curated_verdict_reverts_when_the_adjudicator_does_not_run"
    for path, name in ((SCRIPT, "check_three"), (ledgers, control)):
        where = f"{path.relative_to(REPO).as_posix()}::{name}"
        text = _function_source(path, name).lower()
        for owed in ("seed import", "stage 8"):
            assert owed in text, (
                f"{where} does not name the {owed!r} among the writers the re-insert stands in "
                "for. The extracted tier has exactly two: the one seed import, before any derive, "
                "and M5.4's stage 8 after it. A control that does not say whose write it is "
                "rehearsing is one a reader cannot check against the importer."
            )
        assert "re-import" not in text, (
            f"{where} explains the negative control with a re-import, and no re-import writes "
            "`dna_tag` on an install (decision 162). The word is refused here rather than the "
            "sentence parsed, because every use of it in these two functions was false and the "
            "true statement never needs it: say the seed import, M5.4's stage 8, or -- for the "
            "path that really is recurring -- a models-only IMPORT, which touches the two curated "
            "ledgers and neither content tier. Check 10 is where that path belongs and is exact "
            "about it."
        )
