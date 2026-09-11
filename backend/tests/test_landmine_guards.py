"""Static guards for the two §4.1 rules that no DDL can enforce.

Rule 2 — "salience, confidence, n_sources are WEIGHTS, NEVER FILTERS. No `WHERE confidence > x`
anywhere (a 0.5 cut would delete 44% of the extracted tier; union recalls 93%, intersection
67%)." Nothing in Postgres prevents someone writing that predicate, so these tests read the
package's SQL *and the migrations* and fail if one appears.

Rule 1 — "dna_tag … and dna_projected … NEVER MERGED, NEVER UNIONED". The one sanctioned union
is the `dna_tagged` view in 0004_dna.sql, which exists precisely so the `tier` discriminator
cannot be dropped. The guard below excises that one statement by name and then looks at what is
left, rather than exempting everything within N characters of it.

Each guard has a self-test that feeds it a synthetic violation, because a guard that cannot
fail is worse than no guard: it reads as coverage.

M4.8 findings 17 and 18: both guards were written as *keyword* scans, and a keyword scan is
the wrong shape for either rule. Rule 1 forbids merging the tiers, not the word UNION — of the
five merge shapes probed against the old `UNION [ALL] SELECT` regex, one was caught and four
were not (a FULL OUTER JOIN, two CTEs cross-joined, a comment between UNION and SELECT, and
rows concatenated in Python after the fetch). Rule 2 forbids cutting on a weight, not the four
inequality operators: `= 3`, `IN (2, 3)`, `BETWEEN`, `!=`, `<>`, `> :threshold`,
`HAVING avg(confidence) > 0.5` and `ORDER BY confidence DESC LIMIT 20` all walked past, and the
80-character `check (` lookback that excused domain constraints excused any predicate written
within 80 characters *after* one — in a migration file full of CHECK constraints that is a
bypass, not an exemption.

So the guards now enforce the rules rather than their most-expected spelling. Rule 1 reports
any SQL statement naming both tiers outside the sanctioned view, whatever the operator, and
carries a Python arm for rows merged after they leave the database. Rule 2 matches the whole
operator family, named parameters and aggregate wrappers, cuts balanced `CHECK ( ... )` spans
out structurally instead of guessing from a character distance, and carries a Python arm of its
own. Neither widening exempts a file by name: everything either passes the rule or is a finding
for the owner.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "spielplan"
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

WEIGHT_COLUMNS = ("confidence", "salience", "n_sources", "weight")

# `weight` is deliberately absent from the Python arm. In SQL it is `dna_projected.weight` and
# `dna_axis_weight.weight`, both §4.1 weight columns; in Python it is also the name torch gives
# every parameter tensor — `placement/tower.py:269,281` compare `f"{head}.weight"` state-dict
# keys while loading a checkpoint — and a rule-2 guard that flagged those would be arguing
# about a neural net rather than about the DNA tiers. The SQL arm still covers the column.
PYTHON_WEIGHT_COLUMNS = ("confidence", "salience", "n_sources")

# `WHERE confidence > 0.5`, `AND salience = 3`, `AND n_sources IN (2, 3)`,
# `HAVING avg(confidence) >= :floor`. Three widenings over the shape this pattern shipped with,
# each with its own reason:
#
#   * the operator family is the whole family. A cut is a cut whether it is spelled `> 0.5`,
#     `= 3`, `!= 1`, `<>`, `IN (2, 3)` or `BETWEEN 2 AND 3`; §4.1's "No `WHERE confidence > x`
#     anywhere" names the natural spelling of the thing forbidden, not the only one.
#   * the right-hand side may be a named parameter (`:floor`) or an opening paren (`IN (...)`),
#     not only a digit, a dot or a `$n` placeholder — and it may be `ANY(...)`/`ALL(...)`, which
#     is not an exotic spelling here but the only one available: this package talks to raw
#     asyncpg (`db/pool.py:15`), asyncpg expands no Python sequence into an `IN` list, and
#     Postgres rewrites a literal `IN (list)` into `= ANY(array)` itself. Fifty-eight sites in
#     the package already write set membership that way, so a cut spelled `salience = ANY($1)`
#     is the parameterised twin of the `IN (2, 3)` above and the form a real regression takes.
#   * a cast may sit between the column and the operator: `confidence::numeric >= $1` deletes
#     the same rows as `confidence >= $1`, and the annotation is not the predicate.
#   * `\)*` lets the column sit inside an aggregate wrapper: `HAVING avg(confidence) > 0.5` is
#     the spelling a group-by cut takes, and it deletes the same 44% by another route. That arm
#     shipped able to see only the wrappers that take one argument, because a second one puts a
#     comma where it expected the closing paren -- so `WHERE coalesce(confidence, 0) > 0.5` walked
#     past it. Which is the spelling most likely to be written here of all of them: `0004_dna.sql`
#     declares `confidence` and `n_sources` nullable, the sanctioned `dna_tagged` view emits
#     `NULL::real AS salience` for the projected tier, and `home/why.py:50` and `tonight/dna.py:32`
#     already write `COALESCE(d.salience, 1.0)` over these columns -- legitimately, in arithmetic.
#     Moving one of those into a WHERE is one line, so the comma form is not an exotic spelling of
#     the cut, it is the house's own idiom pointed at a predicate. `[^();]{0,60}` and a required
#     `\)`: the argument must be a plain one and the wrapper must actually close, or the clause
#     starts eating prose -- `rate/round.py`'s docstring is what an unbounded version matched.
#
# The leading `\b` is a repair, not a widening: without it the `or` branch matched the tail of
# `for`, `constructor` and every other word ending in those two letters. That was inert while
# the operator set was four inequalities and the right-hand side had to start with a digit; with
# `=` in the alternation the first `for row in rows: row.confidence = 0.0` in the package would
# have been reported as a filter. Nothing in the tree triggers it today either way, which is
# exactly why it had to be fixed in the same edit that made it reachable.
FILTER_PATTERN = re.compile(
    r"\b(?:where|and|or|having)\s[^;]{0,200}?\b("
    + "|".join(WEIGHT_COLUMNS)
    + r")\b(?:::\w+)?(?:\s*,[^();]{0,60}\))?\s*\)*\s*(?:>=|<=|<>|!=|=|>|<|\bin\b|\bbetween\b)"
    r"\s*(?:\b(?:any|all)\b\s*)?[\d.$:({]",
    re.IGNORECASE | re.DOTALL,
)

# The predicate that is not written after a keyword at all. `db/library.py:64-131` and
# `scoring/serve.py:238-275` are the two §6.0/§6.3 filter surfaces, and both build every
# predicate as a bare element appended to a `where` list -- `where.append(f"col op {arg(value)}")`
# -- so the clause the pattern above needs is a Python method name (`where` followed by `.`, not
# by `\s`) and the right-hand side opens with `{`. Either condition alone was enough: a real
# rule-2 cut added to either file was invisible to both arms, and `db/library.py:96-107` is the
# file that names this exact temptation in its own comment ("The obvious implementation of 'only
# good matches' is a confidence cut, and a 0.5 cut deletes 44% of the extracted tier"), which
# makes the bare `where.append` spelling the natural one rather than a contrived one. So `{` is
# added to the right-hand side above -- `{arg(value)}` is what this package writes where a
# literal `$n` would go -- and the arm below reads a predicate that opens its own literal.
#
# `=`, `IN` and `BETWEEN` are deliberately absent here, and their absence is forced rather than
# an oversight: with no clause keyword in front of it, a bare `confidence = 0.0` arm would report
# `for row in rows: row.confidence = 0.0`, which is a registered legitimate usage below and the
# false positive the leading `\b` on the keyword alternation exists to prevent. So
# `where.append(f"dt.salience = {arg(v)}")` and the IN/BETWEEN forms remain outside both arms;
# the operator family is complete only where a clause keyword makes the intent unambiguous.
# [M4.8 review cycle 3: m48-c3-landmine-02]
BARE_FILTER_PATTERN = re.compile(
    r"\b(?:\w+\.)?(" + "|".join(WEIGHT_COLUMNS) + r")\b(?:::\w+)?\s*"
    r"(?:>=|<=|<>|!=|>|<)\s*(?:\b(?:any|all)\b\s*)?[\d.${:]",
    re.IGNORECASE,
)

# `ORDER BY confidence DESC LIMIT 20` is a cut with the comparison left implicit: it deletes
# every row past the twentieth *by weight*, which is the deletion §4.1 measures at 44%. Ordering
# alone is not — the board is allowed to rank by salience, and `db/library.py` does — so the
# LIMIT is what turns a ranking into a filter, and the pattern requires both.
TOP_N_PATTERN = re.compile(
    r"\border\s+by\b[^;]{0,200}?\b("
    + "|".join(WEIGHT_COLUMNS)
    + r")\b[^;]{0,200}?\blimit\b",
    re.IGNORECASE | re.DOTALL,
)

SANCTIONED_VIEW = "dna_tagged"
TIERS = ("dna_tag", "dna_projected")

# `dna_tag` is a prefix of `dna_tagged`, so every tier reference is word-boundaried on both
# sides: a bare substring test reads the sanctioned view's own name as a reference to the
# extracted tier and flags the one statement §4.1 exists to permit.
_TIER_REFS = {tier: re.compile(rf"\b{tier}\b", re.IGNORECASE) for tier in TIERS}

# A statement is SQL when it carries a verb. The tier names appear in prose too — an error
# message ("bundle must ship dna_tag AND dna_projected as separate tables",
# `importer/validate.py:210`), a backup table list, a column-mapping dict — and naming two
# tables in a sentence merges nothing. This guard reads SQL; it does not read English.
_SQL_VERB = re.compile(r"\b(?:select|insert|update|delete|create|with)\b", re.IGNORECASE)

# INTERSECT and EXCEPT *compare* the tiers, they do not merge them: every row of
# `dna_tag INTERSECT dna_projected` is in both tiers by construction and every row of an EXCEPT
# is in exactly one, so neither output can lose the discriminator §4.1 demands — "14,181
# (title,term) pairs exist in both and must stay distinguishable", which
# `importer/validate.py:224` measures with precisely such a statement and
# `test_shared_dna_pairs_are_counted_not_deduped` requires stay measured. UNION and JOIN mix
# rows whose tier is afterwards unrecoverable, which is the thing the rule names. So the
# exemption turns on what the operator does to the tier label, not on how close it sits to
# something innocent, and a statement that set-compares *and* unions is still a merge.
#
# Which means the operator's own operands, not its presence: keyed on presence, one unrelated
# `EXCEPT` bolted onto a sub-select pardoned a comma cross join of the two tiers — the same
# exemption-by-co-occurrence as the 80-character `check (` lookback below, in the other guard.
# `_compares_the_tiers` therefore requires the tiers to be the two sides of the comparison and
# nothing outside it to reach either one.
_SET_COMPARISON = re.compile(r"\b(?:intersect|except)\b", re.IGNORECASE)
_MERGING_OPERATOR = re.compile(r"\b(?:union|join)\b", re.IGNORECASE)

# The keyword this rule used to be written as, kept as a floor under the .py arm rather than
# thrown away with it -- see `_raw_union_merges`.
_UNION_SELECT = re.compile(r"union\s+(?:all\s+)?select", re.IGNORECASE)

# A scalar subquery that counts one tier is the other half of the same argument: its output is
# a number under its own column alias, not rows, so no row's tier can be lost. That is how
# `placement/features.py:406-409` reads both tiers in one statement -- `(SELECT count(*) FROM
# dna_tag ...) AS n_dna_x` and `(SELECT count(*) FROM dna_projected ...) AS n_dna_p` -- and the
# column names *are* the discriminator §4.1 asks for, the same shape as `api/library.py:174`'s
# one dict key per tier. The span must name exactly one tier to be excused, so a UNION written
# inside such a subquery is still reported, and a derived table or CTE that selects rows rather
# than an aggregate is not excused at all: it can meet the other tier in the outer FROM.
_AGGREGATE_SUBQUERY = re.compile(r"^\s*select\s+(?:count|sum|avg|min|max)\s*\(", re.IGNORECASE)

# `--` to end of line and `/* ... */`, replaced by their own newlines so nothing joins across.
_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)

_CHECK_HEAD = re.compile(r"\bcheck\s*\(", re.IGNORECASE)
_SANCTIONED_VIEW_HEAD = re.compile(
    rf"\bcreate\s+(?:or\s+replace\s+)?view\s+{SANCTIONED_VIEW}\b", re.IGNORECASE
)

# The five spellings that put rows from two tiers in one container once they are out of the
# database. `chain` is matched on the call's tail so both `chain(a, b)` and
# `itertools.chain(a, b)` are seen; `update` covers the dict/set spelling of `extend`.
_MERGE_METHODS = ("extend", "update")

_COMPARISON_OPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn)
_PYTHON_WEIGHT_REFS = tuple(re.compile(rf"\b{column}\b") for column in PYTHON_WEIGHT_COLUMNS)


def _sql_sources() -> list[Path]:
    """Everything that can contain SQL: the package *and* the migrations."""
    return sorted(PACKAGE.rglob("*.py")) + sorted(MIGRATIONS.glob("*.sql"))


def _excerpt(text: str) -> str:
    return " ".join(text.split())[:120]


def _strip_sql_comments(sql: str) -> str:
    """Comments are prose, and in this schema the prose states the rule: `0004_dna.sql:71` reads
    "Tier 1: EXTRACTED. Quote-verified. Never unioned with dna_projected." and sits in a
    `;`-delimited span that also names `dna_tag`. A guard that read that as a violation would
    fire on the file documenting the rule, so the comments come out before the split."""
    return _SQL_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), sql)


def _strip_sanctioned_view(sql: str) -> str:
    """Remove every `CREATE [OR REPLACE] VIEW dna_tagged AS ... ;` statement, and nothing else.

    `CREATE OR REPLACE VIEW` is the spelling a later migration would use to *edit* the view,
    which makes it the statement most worth reading — and the old head matched `create view
    dna_tagged` alone, so a replacement that quietly dropped the `tier` column would have been
    excised from the guard's view by the very mechanism that pardons the original.
    """
    while True:
        head = _SANCTIONED_VIEW_HEAD.search(sql)
        if not head:
            return sql
        end = sql.find(";", head.end())
        sql = sql[: head.start()] + (sql[end + 1 :] if end != -1 else "")


def _strip_check_constraints(text: str) -> str:
    """Remove every balanced `CHECK ( ... )` span.

    A CHECK constraint on a weight column is a domain rule, not a filter: `weight real CHECK
    (weight >= -1.0 AND weight <= 1.0)` keeps the authored axis TSVs in range (§6.4) and deletes
    nothing. The exemption that shipped was a *lookback* — any hit within 80 characters after
    `check (` was forgiven — which in a migration file pardons the constraint and whatever
    follows it (`test_the_weight_guard_catches_a_predicate_beside_a_check_constraint` replays
    exactly that). Cutting the balanced span out instead forgives the constraint and only the
    constraint.

    A span that never closes is forgiven nothing, which is the half this got wrong first time:
    `cursor = len(text)` dropped everything below an unclosed `check (` before either pattern
    saw it, and the trigger is the English word plus a paren — a prose comment (`placement/
    reconcile.py:535` already writes "positive check (the row exists and is validated)") or an
    open paren inside a string literal in a real CHECK. So an unbalanced head is kept and the
    scan resumes after it. Parens inside string literals can still confuse the depth count in
    the other direction, and there a guard that mis-parses over-reports rather than
    under-reporting, which is the safe way round for a landmine guard.
    """
    out: list[str] = []
    cursor = 0
    while True:
        head = _CHECK_HEAD.search(text, cursor)
        if not head:
            out.append(text[cursor:])
            return "".join(out)
        out.append(text[cursor : head.start()])
        close = _matching_paren(text, head.end() - 1)
        if close == -1:
            out.append(text[head.start() : head.end()])
            cursor = head.end()
        else:
            cursor = close + 1


def _matching_paren(text: str, opening: int) -> int:
    """The index of the `)` closing the `(` at `opening`, or -1 if the span never closes."""
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _strip_scalar_tier_counts(sql: str) -> str:
    """Remove every balanced `(SELECT count(*) FROM <one tier> ...)` span.

    See `_AGGREGATE_SUBQUERY`: a per-tier count is a labelled number, not a row, and
    `placement/features.py`'s meta query takes one of each in a single statement. Left in, the
    guard would report the one module whose docstring at `:16-17` states rule 1 as its design
    ("`dna_tag` and `dna_projected` are two functions and two statements"), which is the same
    class of false positive as reading the schema's comments.
    """
    out: list[str] = []
    cursor = 0
    while True:
        opening = sql.find("(", cursor)
        if opening == -1:
            out.append(sql[cursor:])
            return "".join(out)
        close = _matching_paren(sql, opening)
        inner = sql[opening + 1 : close] if close != -1 else ""
        named = [tier for tier, pattern in _TIER_REFS.items() if pattern.search(inner)]
        if close != -1 and len(named) == 1 and _AGGREGATE_SUBQUERY.match(inner):
            out.append(sql[cursor:opening])
            cursor = close + 1
        else:
            out.append(sql[cursor : opening + 1])
            cursor = opening + 1


def _weight_filters(text: str) -> list[str]:
    """Every place `text` cuts on a weight column: a predicate, or a ranking that truncates."""
    body = _strip_check_constraints(text)
    return (
        [m.group(0) for m in FILTER_PATTERN.finditer(body)]
        + [m.group(0) for m in BARE_FILTER_PATTERN.finditer(body)]
        + [m.group(0) for m in TOP_N_PATTERN.finditer(body)]
    )


def _tier_unions(text: str) -> list[str]:
    """The SQL statements in `text` that name both tiers outside the sanctioned view.

    §4.1 rule 1 forbids merging the tiers, not the word UNION, so this looks for no operator at
    all: a statement reaching both `dna_tag` and `dna_projected` at once is either the
    tier-labelled view or a merge. That reframing is what catches the shapes the keyword scan
    missed. It returns the offending statements rather than line numbers on purpose — excising
    the sanctioned view shifts every line after it, and a guard that points at the wrong line
    is worse than one that quotes what it objects to.
    """
    body = _strip_scalar_tier_counts(_strip_sanctioned_view(_strip_sql_comments(text)))
    return [_excerpt(statement) for statement in body.split(";") if _is_tier_merge(statement)]


def _is_tier_merge(statement: str) -> bool:
    if not all(pattern.search(statement) for pattern in _TIER_REFS.values()):
        return False
    if not _SQL_VERB.search(statement):
        return False
    return not _compares_the_tiers(statement)


def _enclosing_span(statement: str, index: int) -> tuple[int, int]:
    """The innermost `( ... )` containing `index`, or the whole statement when there is none."""
    stack: list[int] = []
    for position in range(index):
        if statement[position] == "(":
            stack.append(position)
        elif statement[position] == ")" and stack:
            stack.pop()
    if not stack:
        return (0, len(statement))
    close = _matching_paren(statement, stack[-1])
    return (stack[-1], len(statement) if close == -1 else close + 1)


def _compares_the_tiers(statement: str) -> bool:
    """Are the two tiers the two operands of one INTERSECT/EXCEPT, and nothing else's?

    Presence of the operator is not the question. `SELECT a.term, b.term FROM (SELECT term FROM
    dna_tag) a, (SELECT term FROM dna_projected EXCEPT SELECT term FROM retired) b` set-compares
    `dna_projected` against a third table and cross-joins the tiers, and reading the exemption
    over the whole statement pardoned it — a one-token bypass of the shape the self-tests above
    pin. So: find the comparison's own span (its enclosing parens, or the statement when it has
    none), require that no tier is named outside it, and require that no single operand names
    both. What survives is the shape `importer/validate.py:224` writes and §4.1's 14,181 shared
    pairs are counted with, where every output row is in both tiers or in exactly one by
    construction and the discriminator cannot be lost.
    """
    if _MERGING_OPERATOR.search(statement):
        return False
    for match in _SET_COMPARISON.finditer(statement):
        start, end = _enclosing_span(statement, match.start())
        outside = statement[:start] + statement[end:]
        if any(pattern.search(outside) for pattern in _TIER_REFS.values()):
            continue
        operands = _SET_COMPARISON.split(statement[start:end])
        if all(
            sum(bool(pattern.search(operand)) for pattern in _TIER_REFS.values()) < 2
            for operand in operands
        ):
            return True
    return False


def _joined_literal(node: ast.JoinedStr) -> str:
    """An f-string as the single string it becomes, each interpolation a parameter placeholder.

    ` $0 ` rather than the interpolated expression's own source, because what reaches the driver
    there is a value and not SQL, and because the spaces stop the placeholder soldering its
    neighbours together: `f"dna_{tier}"` names no tier in the source and must not be read as
    naming one.
    """
    return "".join(
        part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else " $0 "
        for part in node.values
    )


def _sql_literals(source: str) -> list[str]:
    """Every string constant in a module, docstrings excluded and f-strings kept whole.

    A .py file has no `;` to split on, so the split has to be chosen rather than inherited, and
    the unit of SQL in Python is the string literal that reaches the driver. The alternatives
    were measured on this package: splitting on Python's own statement boundaries puts a query
    beside its neighbours and reports eight legitimate statements (a dict with one key per
    tier, a backup table list, a column-mapping table, three messages), and not splitting at all
    reports nine whole files. Docstrings are excluded for the reason `_strip_sql_comments`
    exists: `importer/load.py`'s module docstring quotes §4.1's shape note, which names both
    tiers. What a module does with the rows after the fetch is not SQL and is read by
    `_python_tier_merges` instead.

    "The literal that reaches the driver" is what an f-string is, and an `ast.Constant` walk is
    not it: `JoinedStr` holds one Constant per span between interpolations, so a query whose two
    tier names sit either side of a `{...}` arrived here as two half-statements naming one tier
    each and rule 1 reported nothing. The whole-file scan this widening replaced *did* report
    that shape, which made the split a net loss of reach in the edit that was meant to add some.
    Not an exotic spelling either: `api/library.py:146-176` builds DNA SQL as an f-string over an
    interpolated table name and calls it once per tier at `:175-176`, and 64 of the package's 508
    f-strings carry a SQL verb across 25 files. What this reader still cannot see is that
    interpolated *table* name: `{table}` is a value here, so a merge spelled with two of them
    names no tier in any literal it returns. That is not a price the split has to charge, and
    calling it one was wrong -- the module does name both tiers, as the arguments handed to the
    helper, and `_raw_union_merges` reads it whole to catch exactly that.
    [M4.8 review cycle 3: m48-c3-landmine-01; review cycle 4: m48-c4-landmine-01]
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        first = node.body[0] if node.body else None
        if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
            continue
        if isinstance(first.value.value, str):
            docstrings.add(id(first.value))
    interpolated = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
        if isinstance(part, ast.Constant)
    }
    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            literals.append(_joined_literal(node))
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings and id(node) not in interpolated
        ):
            literals.append(node.value)
    return literals


def _tier_of(node: ast.AST | None, source: str, bound: dict[str, str]) -> str | None:
    """The tier a value carries, or None when it is neither tier or both."""
    while isinstance(node, (ast.Await, ast.Starred)):
        node = node.value
    if isinstance(node, ast.Name) and node.id in bound:
        return bound[node.id]
    segment = ast.get_source_segment(source, node) if node is not None else None
    named = [tier for tier, pattern in _TIER_REFS.items() if pattern.search(segment or "")]
    return named[0] if len(named) == 1 else None


def _python_tier_merges(source: str) -> list[str]:
    """Rows from the two tiers combined after they leave the database.

    The SQL arm cannot see this one: `tagged_rows + projected_rows` names no table. So this
    records which locals were bound from an expression naming exactly one tier and then reports
    the spellings that put two such values in one container. §4.1's "never merged" is a claim
    about the rows; a merge performed in Python drops the `tier` discriminator exactly as a
    UNION does. The binding table is module-wide rather than per-scope on purpose: a name that
    means the extracted tier in one function and the projected tier in another is a merge
    waiting to be written, and the wider table costs nothing on a package that has no such name.
    """
    tree = ast.parse(source)
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        tier = _tier_of(node.value, source, {})
        if tier:
            bound[target.id] = tier

    hits: list[str] = []
    for node in ast.walk(tree):
        pair: tuple[ast.AST | None, ast.AST | None] | None = None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            pair = (node.left, node.right)
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
            pair = (node.target, node.value)
        elif isinstance(node, ast.Call) and node.args:
            func = ast.get_source_segment(source, node.func) or ""
            if isinstance(node.func, ast.Attribute) and node.func.attr in _MERGE_METHODS:
                pair = (node.func.value, node.args[0])
            elif func.split(".")[-1] == "chain" and len(node.args) >= 2:
                pair = (node.args[0], node.args[1])
        elif isinstance(node, (ast.List, ast.Set, ast.Tuple)):
            starred = [element for element in node.elts if isinstance(element, ast.Starred)]
            if len(starred) >= 2:
                pair = (starred[0], starred[1])
        if pair is None:
            continue
        left, right = _tier_of(pair[0], source, bound), _tier_of(pair[1], source, bound)
        if left and right and left != right:
            hits.append(_excerpt(ast.get_source_segment(source, node) or ""))
    return hits


def _raw_union_merges(source: str) -> list[str]:
    """The keyword scan step 2a replaced, kept underneath the .py arms as a floor.

    Reframing rule 1 from the word UNION to the rule itself bought four merge shapes and sold
    one, and the shape it sold is the one this package writes. `_sql_literals` splits a module
    into the literals that reach the driver and `_joined_literal` renders every interpolation as
    ` $0 `, so a merge whose table names are interpolated and whose tier names arrive as call
    arguments names no tier in anything either .py arm reads -- while `api/library.py:146-179` is
    precisely that shape already (one f-string over `{table}`, called once per tier at `:176-177`,
    under a docstring that states rule 1), and folding its two calls into one is the smallest edit
    "return one neighbour list instead of two" can take. Measured: the replaced scan reported that
    mutation and the arms that replaced it reported nothing, which made the widening a net loss of
    reach at the one site whose own docstring cites the rule.

    So this reads the module the way that scan did -- comments blanked, the sanctioned view
    excised, split on `;`, both tiers and a `UNION [ALL] SELECT` in one span. A .py file has
    almost no `;`, which makes the span most of a module and is exactly where the reach comes
    from; the arms above are the precise readers and this is the coarse one under them. No
    INTERSECT/EXCEPT exemption, and its absence is forced rather than an oversight:
    `_compares_the_tiers` returns False for any statement carrying `union` or `join`, so on a span
    this arm has already matched it could only ever answer one way, and a check whose answer is
    settled before it runs is the defect this milestone exists to remove rather than a safeguard
    against one. Measured green over `_sql_sources()`: nine package modules name both tiers, two
    carry a `UNION ... SELECT`, and the two sets do not meet -- so the floor costs no waiver and
    exempts no file. [M4.8 review cycle 4: m48-c4-landmine-01]
    """
    body = _strip_sanctioned_view(_strip_sql_comments(source))
    hits: list[str] = []
    for match in _UNION_SELECT.finditer(body):
        start = body.rfind(";", 0, match.start()) + 1
        end = body.find(";", match.end())
        statement = body[start : end if end != -1 else len(body)]
        if all(pattern.search(statement) for pattern in _TIER_REFS.values()):
            # Quoting the span whole would quote most of a module. The union's own neighbourhood
            # is what a reader has to see, and it is where the merge is.
            hits.append(_excerpt(body[max(0, match.start() - 60) : match.end() + 60]))
    return hits


def _merge_hits(name: str, text: str) -> list[str]:
    """Every merge of the two tiers in one source, dispatched on how that source splits."""
    if name.endswith(".py"):
        literal_hits = [hit for literal in _sql_literals(text) for hit in _tier_unions(literal)]
        # Deduplicated because the arms overlap: a merge written inline in one f-string is seen
        # by the literal reader and by the floor, and the same violation listed twice reads as
        # two of them.
        return list(dict.fromkeys(literal_hits + _raw_union_merges(text) + _python_tier_merges(text)))
    return _tier_unions(text)


def _python_weight_comparisons(source: str) -> list[str]:
    """Rule 2 applied to the rows after the fetch.

    §4.1's 44% is a claim about how many extracted tags a cut deletes, and the deletion is the
    same whether Postgres or a list comprehension performs it. Only a *reference* counts, never
    a bare string constant: `if "confidence" in row:` asks whether the column was selected, and
    reading that as a cut on the column would repeat the category error the `check (` lookback
    made in the other direction.
    """
    tree = ast.parse(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, _COMPARISON_OPS) for op in node.ops):
            continue
        for side in [node.left, *node.comparators]:
            if isinstance(side, ast.Constant):
                continue
            segment = ast.get_source_segment(source, side) or ""
            if any(pattern.search(segment) for pattern in _PYTHON_WEIGHT_REFS):
                hits.append(_excerpt(ast.get_source_segment(source, node) or ""))
                break
    return hits


# --- the guards ----------------------------------------------------------------------


def test_no_weight_column_is_used_as_a_filter():
    offenders: list[str] = []
    for path in _sql_sources():
        text = path.read_text(encoding="utf-8")
        for hit in _weight_filters(text):
            offenders.append(f"{path.name}: {hit[:90]!r}")
    assert not offenders, (
        "§4.1 rule 2: salience/confidence/n_sources are weights, never filters.\n"
        + "\n".join(offenders)
    )


def test_no_weight_column_is_compared_in_python():
    """Rule 2 does not stop at the database. §4.1's 44% counts deleted rows, and a list
    comprehension deletes them as thoroughly as a WHERE clause does."""
    offenders: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        offenders += [
            f"{path.relative_to(PACKAGE).as_posix()}: {hit}"
            for hit in _python_weight_comparisons(path.read_text(encoding="utf-8"))
        ]
    assert not offenders, (
        "§4.1 rule 2: a row's confidence, salience or n_sources compared to a threshold in "
        "Python is the same cut as `WHERE confidence > x`.\n" + "\n".join(offenders)
    )


def test_the_only_sanctioned_dna_union_is_the_tier_labelled_view():
    offenders: list[str] = []
    for path in _sql_sources():
        text = path.read_text(encoding="utf-8")
        offenders += [f"{path.name}: {hit!r}" for hit in _merge_hits(path.name, text)]
    assert not offenders, (
        "§4.1 rule 1: dna_tag and dna_projected are never merged and never unioned except in "
        "the tier-labelled `dna_tagged` view.\n" + "\n".join(offenders)
    )


# --- self-tests: a guard that cannot fail is not a guard -----------------------------


@pytest.mark.parametrize(
    "snippet",
    [
        "SELECT * FROM dna_tag WHERE confidence > 0.5",
        "SELECT * FROM dna_tag WHERE facet = 'mood' AND salience >= 2",
        "SELECT term FROM dna_projected GROUP BY term HAVING weight > $1",
        'await conn.fetch("SELECT * FROM dna_tag WHERE n_sources >= 2")',
        # The shapes the inequality-and-a-digit pattern walked past (M4.8 finding 18). Each is
        # the same 44% deletion under a different operator, right-hand side or wrapper.
        "SELECT * FROM dna_tag WHERE salience = 3",
        "SELECT * FROM dna_tag WHERE salience IN (2, 3)",
        "SELECT * FROM dna_tag WHERE confidence BETWEEN 0.5 AND 1.0",
        "SELECT * FROM dna_tag WHERE confidence != 0",
        "SELECT * FROM dna_tag WHERE confidence <> 0",
        "SELECT * FROM dna_projected WHERE weight > :threshold",
        "SELECT term FROM dna_tag GROUP BY term HAVING avg(confidence) > 0.5",
        "SELECT term FROM dna_tag ORDER BY confidence DESC LIMIT 20",
        'await conn.fetch("SELECT * FROM dna_tag WHERE n_sources < :floor")',
        # `= ANY($n)` is not an exotic spelling of `IN (2, 3)` here, it is the only one: asyncpg
        # expands no Python sequence into an IN list, so 58 sites across `db/library.py`,
        # `home/why.py`, `home/shelves.py` and `ledger/` write set membership this way, and
        # Postgres itself rewrites a literal `IN (list)` into `= ANY(array)`. The guard caught
        # the hand-typed form and walked past the one the driver forces.
        "SELECT * FROM dna_tag WHERE salience = ANY($1)",
        "SELECT * FROM dna_tag WHERE salience = ANY($1::int[])",
        "SELECT * FROM dna_projected WHERE weight <> ALL($1)",
        # And a cast between the column and the operator, which is the same cut with a type
        # annotation on it.
        "SELECT * FROM dna_tag dt WHERE dt.confidence::numeric >= $1",
        # The wrapper that takes a second argument. `confidence` and `n_sources` are nullable in
        # `0004_dna.sql` and the sanctioned view emits a NULL `salience` for the projected tier,
        # so COALESCE over these columns is the correct spelling and already the house's own
        # (`home/why.py:50`, `tonight/dna.py:32`) -- which makes this the cut most likely to be
        # written by accident, one edit after a left join. The single-argument `avg(...)` above
        # was caught and every one of these was not (M4.8 review cycle 2).
        "SELECT * FROM dna_tag WHERE coalesce(confidence, 0) > 0.5",
        "SELECT * FROM dna_tag WHERE greatest(confidence, 0) >= $1",
        "SELECT * FROM dna_tag WHERE round(confidence, 2) > 0.5",
        "SELECT * FROM dna_tag WHERE nullif(salience, 0) = 3",
        "SELECT * FROM dna_tag dt WHERE coalesce(dt.confidence, 0.5) >= $1",
        # And the two halves of the spelling the only two filter surfaces in the package
        # actually write: a bare predicate with no clause keyword in front of it, and a
        # right-hand side that opens with `{`. `db/library.py:64-131` and
        # `scoring/serve.py:238-275` append every predicate to a `where` list this way, so a
        # "only show confident DNA matches" cut added to either was the one shape neither the
        # keyword arm nor the Python arm could see (M4.8 review cycle 3).
        'where.append(f"dt.confidence >= {arg(dna_confidence_min)}")',
        'where.append("dt.confidence >= $5")',
        'sql = f"SELECT * FROM dna_tagged WHERE confidence >= {arg(floor)}"',
    ],
)
def test_weight_filter_guard_catches_a_real_violation(snippet):
    assert _weight_filters(snippet), f"the rule-2 guard would not catch: {snippet!r}"


def test_weight_filter_guard_does_not_flag_legitimate_usage():
    for ok in (
        "ORDER BY g.salience DESC, g.facet, g.term",
        "SELECT salience, confidence, n_sources FROM dna_tag WHERE title_id = $1",
        "salience smallint NOT NULL CHECK (salience IN (1, 2, 3))",
        "weight real NOT NULL CHECK (weight >= -1.0 AND weight <= 1.0)",
        # What the widened operator set must still let through. Writing a weight is not
        # filtering on one; ranking without truncating is §6.4's board, not a cut; and
        # `for row in rows: row.confidence = 0.0` is the false positive the leading `\b` on
        # the `(?:where|and|or|having)` alternation exists to prevent, because `for ` ends in
        # `or ` and `=` is now in the operator family.
        "UPDATE dna_tag SET salience = 2 WHERE title_id = $1",
        "SELECT title_id, salience FROM dna_tag ORDER BY t.name LIMIT 20",
        "for row in rows: row.confidence = 0.0",
        "confidence real NOT NULL CHECK (confidence > 0 AND confidence <= 1)",
        # The widening that made `= ANY($1)` a cut must not make every `= ANY($1)` one: this is
        # the package's own key lookup, with a weight column in the select list and none in the
        # predicate.
        "SELECT title_id, salience FROM dna_tag WHERE title_id = ANY($1::int[])",
        # And the comma-wrapper widening must not make `home/why.py`'s scoring arithmetic a cut:
        # the same COALESCE over the same columns, feeding a multiplication rather than a
        # predicate. §4.1 rule 2 is about deleting rows, not about reading a weight.
        "SELECT title_id, COALESCE(d.salience, 1.0) * COALESCE(d.confidence, 0.5) AS w "
        "FROM dna_tagged d WHERE d.title_id = ANY($1::int[])",
    ):
        assert not _weight_filters(ok), f"the rule-2 guard false-positives on: {ok!r}"
    for ok_python in (
        'if "confidence" in row:\n    keep(row)',
        "if confidence_floor > 0:\n    pass",
        "row['confidence'] = 0.0",
        "salience = max(salience, 1)",
    ):
        assert not _python_weight_comparisons(ok_python), (
            f"the rule-2 Python arm false-positives on: {ok_python!r}"
        )


def test_the_weight_guard_catches_a_predicate_beside_a_check_constraint():
    """The 80-character lookback pardoned the predicate along with the constraint.

    This is the shape that made the exemption a bypass: `0004_dna.sql` alone carries four CHECK
    constraints, and each of them cast an 80-character shadow in which a real cut could be
    written and read as a domain rule. The structural strip removes the constraint's own
    balanced span and nothing else, so the predicate on the next line is still a predicate.
    """
    beside = (
        "salience smallint NOT NULL CHECK (salience IN (1, 2, 3)),\n"
        "  SELECT * FROM dna_tag WHERE confidence > 0.5"
    )
    assert _weight_filters(beside), "a cut written just after a CHECK constraint is still a cut"
    assert not _weight_filters("salience smallint NOT NULL CHECK (salience IN (1, 2, 3))")

    # The structural strip had the same bypass in the other direction. A `check (` whose paren
    # never closes made the strip run to the end of the file, so every predicate below one was
    # dropped before the scan -- and the trigger is the English word plus a paren, which a prose
    # comment satisfies (`placement/reconcile.py:535` writes "positive check (the row exists and
    # is validated)" already). The tail is scanned again now, so the exemption forgives a
    # constraint and never a file.
    below = "-- a positive check (see the note below\nSELECT * FROM dna_tag WHERE confidence > 0.5"
    assert _weight_filters(below), "a cut below an unclosed `check (` is still a cut"
    literal = (
        "note text CHECK (note <> 'a smiley :-( here'),\n"
        "  SELECT * FROM dna_tag WHERE salience >= 2"
    )
    assert _weight_filters(literal), "an unbalanced paren inside a CHECK must not blind the rest"


@pytest.mark.parametrize(
    "snippet",
    [
        "rows = [r for r in rows if r['confidence'] > 0.5]",
        "if row.salience >= 2:\n    keep(row)",
        "kept = [r for r in rows if r['n_sources'] in (2, 3)]",
        "if tag['confidence'] != 0:\n    keep(tag)",
    ],
)
def test_the_python_weight_guard_catches_a_synthetic_violation(snippet):
    assert _python_weight_comparisons(snippet), f"the rule-2 Python arm would not catch: {snippet!r}"


def test_union_guard_catches_a_real_violation():
    violation = (
        "SELECT term FROM dna_tag WHERE title_id = 1 "
        "UNION ALL SELECT term FROM dna_projected WHERE title_id = 1;"
    )
    assert _tier_unions(violation), "the rule-1 guard would not catch an unlabelled tier union"
    # The scalar-count exemption pardons a count of one tier, not everything wearing a paren:
    # a union inside the subquery names both tiers, so the span is not excused, and a derived
    # table that selects rows can meet the other tier in the outer FROM.
    assert _tier_unions(
        "SELECT t.id, (SELECT count(*) FROM (SELECT term FROM dna_tag "
        "UNION ALL SELECT term FROM dna_projected) m) AS n_dna FROM title t;"
    )
    assert _tier_unions(
        "SELECT a.term, b.term FROM (SELECT term FROM dna_tag) a, "
        "(SELECT term FROM dna_projected) b;"
    )


@pytest.mark.parametrize(
    "name, snippet",
    [
        # A join merges without the word UNION appearing anywhere.
        (
            "probe.sql",
            "SELECT t.term, p.term FROM dna_tag t "
            "FULL OUTER JOIN dna_projected p ON p.title_id = t.title_id;",
        ),
        # Two CTEs, cross-joined in the final SELECT: neither table is named beside the other.
        (
            "probe.sql",
            "WITH tagged AS (SELECT term FROM dna_tag), "
            "projected AS (SELECT term FROM dna_projected) "
            "SELECT tagged.term, projected.term FROM tagged, projected;",
        ),
        # A comment between UNION and SELECT defeated `union\s+(?:all\s+)?select` outright.
        (
            "probe.sql",
            "SELECT term FROM dna_tag UNION -- merge\nSELECT term FROM dna_projected;",
        ),
        # An unrelated set comparison anywhere in the statement excused the merge outright: the
        # INTERSECT/EXCEPT exemption was read over the whole `;`-delimited text, so bolting one
        # onto a sub-select turned the two shapes above into a pardon. The comparison here is
        # between `dna_projected` and a third table; the merge is still the comma cross join.
        (
            "probe.sql",
            "SELECT a.term, b.term FROM (SELECT term FROM dna_tag) a, "
            "(SELECT term FROM dna_projected EXCEPT SELECT term FROM retired) b;",
        ),
        (
            "probe.sql",
            "WITH tagged AS (SELECT term FROM dna_tag EXCEPT SELECT term FROM retired), "
            "projected AS (SELECT term FROM dna_projected) "
            "SELECT tagged.term, projected.term FROM tagged, projected;",
        ),
        # And the statement that legitimately set-compares the tiers *and* then merges them: the
        # comparison in the CTE is the shape `importer/validate.py:224` writes, the cross join in
        # the final SELECT is the violation, and the pardon has to be the operator's own operands
        # rather than its presence.
        (
            "probe.sql",
            "WITH shared AS (SELECT term FROM dna_tag INTERSECT SELECT term FROM dna_projected) "
            "SELECT a.term, b.term FROM dna_tag a, dna_projected b;",
        ),
        # And the merge that happens after both queries have already returned.
        (
            "probe.py",
            "async def merge(conn):\n"
            '    tagged_rows = await conn.fetch("SELECT term FROM dna_tag")\n'
            '    projected_rows = await conn.fetch("SELECT term FROM dna_projected")\n'
            "    return tagged_rows + projected_rows\n",
        ),
        # The merge written in the spelling this package writes parameterised SQL in. HEAD's
        # whole-file scan caught this one and the literal split did not: `ast.Constant` cuts an
        # f-string at every interpolation, so the two tier names arrived as two half-statements
        # naming one tier each. `api/library.py:146-176` already builds DNA SQL this way.
        (
            "probe.py",
            "async def dna_for(conn, tid):\n"
            "    return await conn.fetch(\n"
            '        f"SELECT term FROM dna_tag WHERE title_id = {tid} "\n'
            '        f"UNION ALL SELECT term FROM dna_projected WHERE title_id = {tid}"\n'
            "    )\n",
        ),
        # And the same merge with the tier names moved out of the SQL and into the call. This is
        # `api/library.py:146-179` as it stands -- one f-string over an interpolated `{table}`,
        # called once per tier -- with the two calls folded into one, which is the smallest edit
        # that "return one neighbour list instead of two" asks for. Every arm added in cycle 3
        # is blind to it: the literal reader renders each interpolation as ` $0 `, so the query
        # names no tier, and the two tier names arrive as plain call arguments, which is not one
        # of the five container spellings the Python arm reads. The whole-file scan those arms
        # replaced reported it. [M4.8 review cycle 4: m48-c4-landmine-01]
        (
            "probe.py",
            "async def neighbours(conn, extracted, projected, tid):\n"
            "    return await conn.fetch(\n"
            '        f"WITH mine AS (SELECT term FROM {extracted} WHERE title_id = {tid} "\n'
            '        f"UNION ALL SELECT term FROM {projected} WHERE title_id = {tid})"\n'
            "    )\n"
            "\n"
            "async def similar_by_term(conn, tid):\n"
            '    return {"neighbours": await neighbours(conn, "dna_tag", "dna_projected", tid)}\n',
        ),
    ],
)
def test_the_union_guard_catches_every_merge_shape(name, snippet):
    assert _merge_hits(name, snippet), f"the rule-1 guard would not catch: {snippet!r}"


def test_the_union_guard_recognises_a_replaced_sanctioned_view():
    """`CREATE OR REPLACE VIEW` is how a later migration edits the view, and the head that
    shipped matched `CREATE VIEW` only: a replacement that dropped the `tier` column would have
    been excised from the guard's view by the same clause that pardons the original."""
    replaced = (
        "CREATE OR REPLACE VIEW dna_tagged AS\n"
        "    SELECT title_id, term, 'extracted'::text AS tier FROM dna_tag\n"
        "    UNION ALL\n"
        "    SELECT title_id, term, 'projected'::text AS tier FROM dna_projected;"
    )
    assert not _merge_hits("0099_probe.sql", replaced)
    assert _merge_hits(
        "0099_probe.sql",
        replaced + "\nSELECT term FROM dna_tag FULL OUTER JOIN dna_projected USING (term);",
    )


def test_union_guard_allows_the_sanctioned_view_and_nothing_after_it():
    sanctioned = (MIGRATIONS / "0004_dna.sql").read_text(encoding="utf-8")
    assert not _tier_unions(sanctioned)
    # …but a second union in the same file, outside the view, must still be caught.
    assert _tier_unions(
        sanctioned + "\nSELECT term FROM dna_tag UNION ALL SELECT term FROM dna_projected;"
    )
    # A set comparison is not a merge: `importer/validate.py:224` counts the shared pairs §4.1
    # requires stay distinguishable, and every row of an INTERSECT is in both tiers by
    # construction. The exemption is not the old lookback in another costume: a statement that
    # both set-compares and unions is still reported.
    assert not _tier_unions(
        "SELECT count(*) FROM (SELECT DISTINCT title_id, term FROM dna_tag "
        "INTERSECT SELECT DISTINCT title_id, term FROM dna_projected);"
    )
    assert _tier_unions(
        "SELECT term FROM dna_tag INTERSECT SELECT term FROM x "
        "UNION ALL SELECT term FROM dna_projected;"
    )
    # The same comparison without an enclosing paren is still a comparison: the tiers are the
    # operator's two operands and nothing outside them reaches either tier, which is the
    # property the exemption turns on now that its presence alone no longer pardons a statement.
    assert not _tier_unions("SELECT term FROM dna_tag INTERSECT SELECT term FROM dna_projected;")
    # Nor is a per-tier count: `placement/features.py`'s meta query reads both tiers in one
    # statement and hands back two numbers under two column aliases, which is §4.1's
    # discriminator spelled as a column name.
    assert not _tier_unions(
        "SELECT t.id, (SELECT count(*) FROM dna_tag d WHERE d.title_id = t.id) AS n_dna_x, "
        "(SELECT count(*) FROM dna_projected j WHERE j.title_id = t.id) AS n_dna_p "
        "FROM title t;"
    )
    # And `dna_tag` is a prefix of `dna_tagged`: a statement that reads the sanctioned view is
    # not a statement that reads the extracted tier. `home/why.py:18` states the convention
    # ("Both DNA tiers are read through the sanctioned `dna_tagged` view and nowhere else"), so
    # a substring test would report the modules that follow §4.1 most literally.
    assert not _tier_unions(
        "INSERT INTO dna_projected (title_id, term) "
        "SELECT title_id, term FROM dna_tagged WHERE tier = 'projected';"
    )


# --- structural rules ----------------------------------------------------------------


def test_display_schema_is_read_from_exactly_one_place():
    """§4.1 rule 3: platform_rating lives in a display-only schema the feature builder cannot
    import from. Keeping the reads in one function is what makes the boundary auditable."""
    readers = [
        p.relative_to(PACKAGE).as_posix()
        for p in sorted(PACKAGE.rglob("*.py"))
        if "display.platform_rating" in p.read_text(encoding="utf-8")
    ]
    assert sorted(readers) == ["db/library.py", "importer/load.py"], readers


def test_every_listing_query_partitions_by_kind():
    """§4.1 rule 5: 'every ranking surface partitions by it'.

    Owner decision 2026-08-29 makes kind a selection of one or both rather than a one-of-two
    switch, so the guard is that the argument is **required and cannot be empty** — an empty
    selection is the unpartitioned query the rule exists to prevent.
    """
    source = (PACKAGE / "db" / "library.py").read_text(encoding="utf-8")
    assert "kinds: Sequence[str]," in source
    assert 'where = ["t.kind = ANY($1)"]' in source
    assert "args: list[Any] = [normalise_kinds(kinds)]" in source

    from spielplan.db.library import normalise_kinds

    assert normalise_kinds(["movie"]) == ["movie"]
    assert normalise_kinds(["series", "movie"]) == ["movie", "series"]   # canonical order
    for empty in ([], None, ["nonsense"]):
        with pytest.raises(ValueError, match="at least one kind"):
            normalise_kinds(empty)


# The same rule, one layer up. `db/library.py`'s builder is not the only place a listing is
# written: `api/library.py` builds §6.4's wander neighbours and §6.0's filmography inline, and
# both selected `t.kind` into the SELECT list and the GROUP BY and into no predicate at all --
# so a wander from a film ranked films and series in one shared-term ordering, and a tap on a
# director's name answered one interleaved list. The guard above reads the builder, which is
# exactly why neither route was visible to it: they never call it. [M4.9 finding 13]
#
# The rule this reads is "a statement that OUTPUTS kind must CONSTRAIN it". That is the shape
# both defects had and the shape a third would take: a route means to be rendered per kind
# precisely when it sends the kind, and §4.1 rule 5's measured failure is a shared *ranking*
# rather than a shared screen. It also leaves alone the statements that are not catalogue
# listings at all -- `api/tonight.py`'s reveal reads one session's own `session_result` rows,
# chosen upstream by §6.2's already-partitioned pool, and joins `title` only for the names --
# without exempting any file or function by name.
#
# Dotted on purpose. Every statement in this layer aliases its tables, and a bare `kind` in an
# `api/` string is far more likely to be the query parameter's own name in a message than a
# column: the alternative pattern reports `normalise_kinds`'s own refusal text ("select at
# least one kind: 'movie', 'series', or both"), which carries a SQL verb and the word.
_KIND_COLUMN = re.compile(r"\b\w+\.kind\b", re.IGNORECASE)
_KIND_PREDICATE = re.compile(
    r"\b\w+\.kind\s*(?:=\s*(?:any\s*\(|\$\d+)|\bin\b\s*\()", re.IGNORECASE
)


def _unpartitioned_kind_statements(source: str) -> list[str]:
    """Every SQL literal in `source` that returns a title's kind without selecting on it."""
    return [
        _excerpt(literal)
        for literal in _sql_literals(source)
        if _SQL_VERB.search(literal)
        and _KIND_COLUMN.search(literal)
        and not _KIND_PREDICATE.search(literal)
    ]


def test_every_listing_route_in_the_api_layer_partitions_by_kind():
    """§4.1 rule 5: 'every ranking surface partitions by it'.

    Two arms, because the defect had two halves. The statements must constrain the kind they
    report, and the routes must take that selection from the caller through the app's own
    validator -- §6.4's wander is the caller's choice of kinds and never the anchor title's,
    which would be a different rule wearing rule 5's name.
    """
    offenders: list[str] = []
    for path in sorted((PACKAGE / "api").glob("*.py")):
        offenders += [
            f"{path.name}: {hit!r}"
            for hit in _unpartitioned_kind_statements(path.read_text(encoding="utf-8"))
        ]
    assert not offenders, (
        "§4.1 rule 5: an api/ statement reports a title's kind and selects on nothing.\n"
        + "\n".join(offenders)
    )

    source = (PACKAGE / "api" / "library.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    selectors = {
        node.name: (ast.get_source_segment(source, node) or "")
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and any(a.arg == "kind" for a in node.args.args + node.args.kwonlyargs)
    }
    # `facets` is here too and takes a default rather than being required -- it is the filter
    # vocabulary for the controls, not a listing -- but it goes through the same validator, so
    # `?kind=` is a refusal on all four rather than a silent "everything".
    assert {"list_titles", "similar_by_term", "person_detail", "facets"} <= set(selectors), (
        sorted(selectors)
    )
    for name, body in sorted(selectors.items()):
        assert "library.normalise_kinds(kind)" in body, f"{name} takes kind and never checks it"


def test_the_api_kind_guard_catches_a_real_violation():
    """The wander query as it shipped, which is the only proof the arm above can fail."""
    wander = (
        "async def neighbours(table):\n"
        '    return await conn.fetch(f"""\n'
        "        SELECT o.title_id, t.name, t.year, t.kind, count(*) AS shared\n"
        "          FROM {table} o JOIN title t ON t.id = o.title_id\n"
        "         WHERE o.title_id <> $1\n"
        '         GROUP BY o.title_id, t.name, t.year, t.kind""", title_id)\n'
    )
    assert _unpartitioned_kind_statements(wander)
    assert not _unpartitioned_kind_statements(
        wander.replace("WHERE o.title_id <> $1", "WHERE o.title_id <> $1 AND t.kind = ANY($2)")
    )

    # And the shape that must NOT be reported: a slate keyed to one session, joining `title`
    # for its names and reporting no kind at all.
    assert not _unpartitioned_kind_statements(
        'rows = await conn.fetch("""\n'
        "    SELECT r.title_id, r.rank, t.name, t.year\n"
        "      FROM session_result r JOIN title t ON t.id = r.title_id\n"
        '     WHERE r.session_id = $1 ORDER BY r.rank""", session_id)\n'
    )


def test_frozen_rating_source_ids_match_the_spec():
    from spielplan.importer.validate import FROZEN_RATING_SOURCE_IDS

    assert {1, 2, 3, 4, 7, 11, 21, 23, 26, 28, 31} == FROZEN_RATING_SOURCE_IDS
    ddl = (MIGRATIONS / "0003_content.sql").read_text(encoding="utf-8")
    assert "CHECK (id IN (1, 2, 3, 4, 7, 11, 21, 23, 26, 28, 31))" in ddl


def test_no_unique_constraint_on_tmdb_or_trakt_ids():
    """§4.1 rule 6: 315/171 duplicate values exist, mostly legitimate movie/series pairs."""
    ddl = (MIGRATIONS / "0003_content.sql").read_text(encoding="utf-8")
    for column in ("tmdb_id", "trakt_id"):
        short = column.split("_")[0]
        assert f"CREATE INDEX title_{short} ON title ({column})" in ddl
        assert f"UNIQUE INDEX title_{short}" not in ddl


def test_user_title_state_has_no_forgotten_value():
    """Owner decision 2026-08-29 (§4.2): there is no 'forgotten' state. A title you cannot
    remember is plain `unseen` — one control, one sync rule."""
    ddl = (MIGRATIONS / "0005_ledger.sql").read_text(encoding="utf-8")
    assert "CHECK (state IN ('unseen', 'seen'))" in ddl
    assert "forgotten" not in ddl.replace("no 'forgotten' state", "")


# --- §4.2: the v1.1 machinery that does not survive -------------------------------------

# §4.2's own comment on `session_participant`: "v1.1 §6's mu/sigma/tolerance/phase columns do
# NOT survive (8-axis posterior machinery deleted per §0); fairness_ledger omitted in v1".
# §0 row 4 is why: no aggregation rule dominates plain averaging, and dominance rules cost
# −0.012 against a 0.003–0.008 noise floor. So this is deleted, not merely unbuilt — the same
# standing `forgotten` has on `user_title`, and it is guarded the same way.
DELETED_POSTERIOR_COLUMNS = ("mu", "tolerance", "phase")
SESSION_TABLES = (
    "session", "session_participant", "session_answer",
    "session_ballot", "session_result", "session_outcome",
)

# `mu smallint`, `tolerance real NOT NULL`, `phase text` — a column definition, not the word
# appearing in a comment or inside a longer identifier like `phase_shift` or `mu_prior`.
_COLUMN_DEF = r"^\s*(?:{names})\s+(?:smallint|integer|bigint|real|double|numeric|text|jsonb|boolean|float)"


def _session_table_bodies(sql: str) -> dict[str, str]:
    """The text between `CREATE TABLE <name> (` and its closing `);`, per session table."""
    bodies = {}
    for table in SESSION_TABLES:
        m = re.search(rf"create\s+table\s+{table}\s*\((.*?)\n\);", sql, re.IGNORECASE | re.DOTALL)
        if m:
            bodies[table] = m.group(1)
    return bodies


def _deleted_posterior_columns(sql: str) -> list[str]:
    pattern = re.compile(
        _COLUMN_DEF.format(names="|".join(DELETED_POSTERIOR_COLUMNS)),
        re.IGNORECASE | re.MULTILINE,
    )
    hits = []
    for table, body in _session_table_bodies(sql).items():
        for m in pattern.finditer(body):
            hits.append(f"{table}: {m.group(0).strip()}")
    # `sigma` is checked separately: `ledger_state.sigma` is legitimate (§5.2's Laplace
    # diagonal), so the word may not simply be banned from the migrations — only from a
    # session table, which the per-table bodies above already scope.
    sigma = re.compile(_COLUMN_DEF.format(names="sigma"), re.IGNORECASE | re.MULTILINE)
    for table, body in _session_table_bodies(sql).items():
        for m in sigma.finditer(body):
            hits.append(f"{table}: {m.group(0).strip()}")
    return hits


def _fairness_ledger(sql: str) -> list[str]:
    return re.findall(r"create\s+(?:table|view)\s+\S*fairness_ledger\S*", sql, re.IGNORECASE)


def test_the_session_tables_carry_no_v11_posterior_machinery():
    """§4.2 deletes v1.1 §6's mu/sigma/tolerance/phase and omits `fairness_ledger`.

    The schema layer (`test_migrations.py`) reads the *applied* columns and catches a later
    ALTER; this reads the migration text and catches the intent at review time, when it is
    cheap to argue about. Neither subsumes the other.
    """
    offenders = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        offenders += [f"{path.name} {h}" for h in _deleted_posterior_columns(sql)]
        offenders += [f"{path.name} {h}" for h in _fairness_ledger(sql)]
    assert not offenders, (
        "§0 row 4 measured the 8-axis posterior and the fairness ledger out of the design; "
        f"these bring them back: {offenders}"
    )


@pytest.mark.parametrize(
    "snippet",
    [
        "CREATE TABLE session_participant (\n    id bigserial,\n    mu real,\n    x text\n);",
        "CREATE TABLE session_participant (\n    id bigserial,\n    sigma real NOT NULL\n);",
        "CREATE TABLE session_participant (\n    id bigserial,\n    tolerance double precision\n);",
        "CREATE TABLE session_answer (\n    id bigserial,\n    phase text NOT NULL\n);",
    ],
)
def test_the_posterior_guard_catches_a_real_violation(snippet):
    """A guard that cannot fail reads as coverage while providing none."""
    assert _deleted_posterior_columns(snippet), f"guard missed: {snippet!r}"


def test_the_posterior_guard_catches_a_fairness_ledger():
    assert _fairness_ledger("CREATE TABLE fairness_ledger (user_id bigint);")
    assert _fairness_ledger("CREATE VIEW session_fairness_ledger AS SELECT 1;")


def test_the_posterior_guard_does_not_flag_legitimate_usage():
    """`ledger_state.sigma` is §5.2's Laplace diagonal and must survive; so must a session
    column whose name merely contains one of the banned words, and any prose in a comment."""
    assert not _deleted_posterior_columns(
        "CREATE TABLE ledger_state (\n    user_id bigint,\n    sigma real,\n    sigma_eff real\n);"
    )
    assert not _deleted_posterior_columns(
        "CREATE TABLE session_participant (\n    id bigserial,\n"
        "    -- v1.1's mu/sigma/tolerance/phase columns do NOT survive\n"
        "    phase_locked boolean,\n    mu_unused text\n);"
    )
    assert not _fairness_ledger("-- fairness_ledger omitted in v1 (§4.2)")


# --- §10: observations survive a re-import, so nothing deletes a title ------------------

# `DELETE FROM title`, in the spellings Postgres accepts: any case, whitespace or a newline
# between the keywords, the optional `ONLY`, an optional schema qualifier and optional double
# quotes. The trailing lookahead is the point of the pattern rather than a detail of it --
# `title_placement`, `title_prior`, `title_alias`, `title_genre`, `title_meta` and
# `title_jellyfin_item` are DERIVED tables that §10's rebuild is supposed to clear, and a guard
# that flagged `DELETE FROM title_meta` would be deleted in a week and take the real rule with it.
#
# A comment naming the statement is flagged too, deliberately: a package file that needs to
# explain how it would delete a title is already making the argument this forbids, and rewording
# a comment is cheaper than the alternative failure.
TITLE_DELETE = re.compile(
    r"delete\s+from\s+(?:only\s+)?(?:\w+\s*\.\s*)?\"?title\"?(?![\w\"])",
    re.IGNORECASE,
)


def _title_deletes(text: str) -> list[str]:
    return [m.group(0) for m in TITLE_DELETE.finditer(text)]


def _functions_deleting_titles() -> set[tuple[str, str]]:
    """(module path, enclosing function) for every package line that deletes from `title`.

    The shape `test_ledger_observations.py::_functions_containing` establishes, with the line
    number taken from the match offset rather than from a per-line scan so that a statement
    wrapped across two lines -- which is how every long query in this package is written -- still
    reports the function it sits in. Naming the function is the whole value of a source guard:
    the failure has to tell the next person where the landmine is.
    """
    hits: set[tuple[str, str]] = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(PACKAGE.parent).as_posix()
        matches = list(TITLE_DELETE.finditer(source))
        if not matches:
            continue
        tree = ast.parse(source)
        for match in matches:
            line = source.count("\n", 0, match.start()) + 1
            enclosing = "<module>"
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
                    node.lineno <= line <= (node.end_lineno or node.lineno)
                ):
                    enclosing = node.name
            hits.add((rel, enclosing))
    return hits


def test_no_package_file_deletes_from_the_title_table():
    """§10: "Ledger observations always survive re-import". §4.2's append-only rule, §7.2.

    0022_model_basis.sql re-declares `verdict.title_id`, `duel.title_a/b`, `tier_edit.title_id`,
    `user_title.title_id` and the §13 session columns as ON DELETE RESTRICT, so the database now
    refuses the delete at runtime. That FK is the refusal; this is the guard that keeps the
    statement from being written in the first place, and the two are not redundant: the FK fails
    an admin action that has already shipped, while this fails the review that would have shipped
    it. Seven observation tables and the §13 outcome row -- eight tables, ten foreign-key columns
    -- carried ON DELETE CASCADE, so the survival guarantee rested on the convention that no code
    deletes a title, and taste data is the one thing this app cannot re-derive.

    The derived tables are a different matter and are not covered here: clearing `title_meta` or
    `title_jellyfin_item` is what a re-import is for.
    """
    found = _functions_deleting_titles()
    assert not found, (
        "§10 says Ledger observations always survive re-import, so no package file may delete a "
        f"title row; these do: {sorted(found)}. Retire the title through display state or a "
        "placement flag instead, and leave the observations that name it where they are."
    )


@pytest.mark.parametrize(
    "snippet",
    [
        'await conn.execute("DELETE FROM title WHERE id = $1", title_id)',
        "delete from title",
        'await conn.execute(\n    """\n    DELETE\n      FROM title\n     WHERE id = $1\n    """\n)',
        "DELETE FROM public.title AS t",
        'DELETE FROM "title" WHERE id = 1',
        "DELETE FROM ONLY title",
    ],
)
def test_the_title_delete_guard_catches_a_synthetic_violation(snippet):
    """A guard that cannot fail reads as coverage while providing none."""
    assert _title_deletes(snippet), f"guard missed: {snippet!r}"


def test_the_title_delete_guard_does_not_fire_on_a_derived_table():
    """The word boundary, proved: every `title_*` table in the schema is a legal DELETE target."""
    for legal in (
        'await conn.execute("DELETE FROM title_placement")',
        "DELETE FROM title_prior WHERE title_id = $1",
        "DELETE FROM title_meta",
        "DELETE FROM title_alias",
        "DELETE FROM title_genre",
        "DELETE FROM title_jellyfin_item WHERE title_id = $1",
        "DELETE FROM titles",
    ):
        assert not _title_deletes(legal), f"guard over-reached: {legal!r}"
