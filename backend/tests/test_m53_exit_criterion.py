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
