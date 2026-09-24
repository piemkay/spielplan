"""Static guards over M5.6's Admin · Data surface: the review filters nothing, the board and the
queue cut no reason, and no file under `frontend/src` retypes §8's stage names.

Spec v2.1 §6.6 Data, §4.1 rule 2, §8 stage 7, §8; decisions 341, 345, 401, 444 and 446.

THE REVIEW'S OWN FILES, EVERY SHAPE OF THE CUT (decision 446). §4.1 rule 2 makes salience,
confidence and n_sources weights and never filters, and `test_landmine_guards.py` holds that over
the package's SQL and its Python comparisons. The review is where the cut is most tempting - its
subject is the weakest tags - and it is also the one surface where the cut can be made in a layer
that guard never reads: a Svelte `{#if}`, a JS `.filter`, a slider bound to a weight. Decision 401
records two more shapes the package-wide guard is blind to, a null test on a weight (`IS NULL`,
`?? `) and a truthiness test in Python, and chose to answer them over the review's own files rather
than widen that guard - whose one legitimate `salience IS NULL` in `importer/validate.py` would
need an allow-list. So this guard reads exactly four files, the domain read, the route module, the
component and its state module, and refuses every shape in every layer they are written in. It
reuses the landmine guard's SQL patterns and its literal reader rather than restating them, so the
two cannot disagree about what a SQL cut looks like.

THE BOARD'S AND THE QUEUE'S REASONS, WHOLE. `acquisition_job.reason` is "shown verbatim on the admin
board" (`0005_ledger.sql`) and `flywheel_item.reason` "shown verbatim in the admin queue"
(`0004_dna.sql`); both are sentences their writers composed for the operator (plan section 9). A
line clamp, an ellipsis, a clipped max-height or a `.slice` on the reason each turns that sentence
into a fragment the operator acts on without reading, and none of them fails a test that checks
the element exists - so the source is read for them.

ONE SPELLING OF §8'S TEN NAMES. The board draws its segments from the envelope's `stages`, which is
`pipeline.STAGES` and nothing else (plan A2), so a stage-name list typed into the frontend is a
second spelling nothing compares with the first. `reviews gate` is the one name no other word in
the app contains, so its absence from every source file under `frontend/src` - the colocated tests
included, whose fixtures invent their own legend - is the test that no copy exists.

Each rule has a self-test that feeds it a violating snippet: a guard that cannot fail reads as
coverage and is worse than none.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.test_landmine_guards import (
    _python_weight_comparisons,
    _sql_literals,
    _strip_sql_comments,
    _weight_filters,
)

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend" / "src"
LIB = FRONTEND / "lib"

REVIEW_PYTHON = (
    REPO / "backend" / "spielplan" / "dna" / "review.py",
    REPO / "backend" / "spielplan" / "api" / "curated.py",
)
REVIEW_FRONTEND = (
    LIB / "components" / "DnaRejects.svelte",
    LIB / "dnaReview.svelte.js",
)
REASON_CARRIERS = (
    LIB / "components" / "AcquisitionBoard.svelte",
    LIB / "components" / "FlywheelQueue.svelte",
    LIB / "acquisitionBoard.svelte.js",
    LIB / "flywheel.svelte.js",
)

WEIGHTS = ("confidence", "salience", "n_sources")
_W = r"\b(?:" + "|".join(WEIGHTS) + r")\b"
_WEIGHT = re.compile(_W)


# --- the review: SQL and Python ------------------------------------------------------------------

# Decision 401's first shape in SQL: a null test on a weight keeps or drops rows by whether they
# were measured, which is a cut on the column whatever the operator family says.
_SQL_NULL_TEST = re.compile(_W + r"(?:::\w+)?\s+is\s+(?:not\s+)?null\b", re.IGNORECASE)

# The top-N cut in every spelling that keeps the first N rows of a weight ordering, not only the
# one `TOP_N_PATTERN` reads (M56-DATA-05): FETCH FIRST and OFFSET cut the same rows LIMIT does, and a
# window rank over a weight exists to be cut by a predicate on the rank one level out. The reused
# pattern also stops 200 characters past the weight, a bound it needs because the package guard
# scans whole files and must not join two statements; each literal here is one statement, so a
# LIMIT written after a long ORDER BY is read however far away it lands.
_SQL_ROW_CUT = re.compile(
    r"\border\s+by\b[^;]*?" + _W + r"[^;]*?\b(?:limit|offset|fetch\s+(?:first|next))\b", re.IGNORECASE
)
_SQL_WINDOW_RANK = re.compile(
    r"(?:\b(?:row_number|rank|dense_rank|percent_rank|cume_dist|ntile)\s*\([^)]*\)\s*over"
    r"|\bwindow\s+\w+\s+as)\s*\([^)]*?" + _W,
    re.IGNORECASE,
)

# The same cut after the fetch. `low_evidence` returns its rows weakest first, so a slice of them
# keeps the least-evidenced N and hides the rest by rank - and names no weight, so neither the
# comparison nor the truthiness arm sees it. This guard cannot follow a list back to the ORDER BY
# that ranked it, and the review needs no slice anywhere (its one bound is `REJECT_LIMIT`, on
# recency, in SQL), so every slice in these files is refused, with the stdlib's three truncations.
_TRUNCATING_CALLS = ("islice", "nsmallest", "nlargest")


def _is_weight_ref(node: ast.AST, source: str) -> bool:
    """A reference to a weight: `confidence`, `row.confidence`, `row["confidence"]`,
    `row.get("confidence")`. A bare string constant is a column NAME and never counts, for the
    landmine guard's reason (`if "confidence" in row` asks whether the column was selected)."""
    if not isinstance(node, (ast.Name, ast.Attribute, ast.Subscript, ast.Call)):
        return False
    return bool(_WEIGHT.search(ast.get_source_segment(source, node) or ""))


def _python_truthiness(source: str) -> list[str]:
    """Decision 401's second shape: a weight tested for truth, which drops a 0.0 and a NULL alike.

    `if row["confidence"]:`, `x = a if tag.salience else b`, `[t for t in tags if t.n_sources]`,
    `ok = t.confidence and ...`, `not row.salience` - and `filter(...)` handed anything that names a
    weight, which is the same cut spelled as a call.
    """
    tree = ast.parse(source)
    tested: list[ast.AST] = []
    filtered: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            tested.append(node.test)
        elif isinstance(node, ast.BoolOp):
            tested.extend(node.values)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            tested.append(node.operand)
        elif isinstance(node, ast.comprehension):
            tested.extend(node.ifs)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "filter":
            filtered.append(node)
    hits = [ast.get_source_segment(source, node) or "" for node in tested if _is_weight_ref(node, source)]
    hits += [
        ast.get_source_segment(source, node) or ""
        for node in filtered
        if _WEIGHT.search(ast.get_source_segment(source, node) or "")
    ]
    return hits


def _python_truncations(source: str) -> list[str]:
    """A slice, `rows[:20]` or `rows[0:n]`, or a call that keeps the first N: `islice`, `nsmallest`,
    `nlargest`, bare or through their modules."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            hits.append(ast.get_source_segment(source, node) or "")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in _TRUNCATING_CALLS:
                hits.append(ast.get_source_segment(source, node) or "")
    return hits


def _python_cuts(source: str) -> list[str]:
    """Every cut on a weight a Python module can make: in the SQL it sends and in the rows after."""
    hits: list[str] = []
    for literal in _sql_literals(source):
        sql = _strip_sql_comments(literal)
        hits += _weight_filters(sql)
        hits += [m.group(0) for m in _SQL_NULL_TEST.finditer(sql)]
        hits += [m.group(0) for m in _SQL_ROW_CUT.finditer(sql)]
        hits += [m.group(0) for m in _SQL_WINDOW_RANK.finditer(sql)]
    hits += _python_weight_comparisons(source)
    hits += _python_truthiness(source)
    hits += _python_truncations(source)
    return hits


# --- the review: Svelte and JavaScript -----------------------------------------------------------

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# A line comment, and not the `//` inside a URL such as `https://`, which follows a colon.
_LINE_COMMENT = re.compile(r"(?<![:\w'\"`])//[^\n]*")
_STYLE = re.compile(r"<style\b[^>]*>.*?</style>", re.DOTALL)
_SCRIPT = re.compile(r"<script\b[^>]*>(.*?)</script>", re.DOTALL)


def _uncommented(text: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", _HTML_COMMENT.sub("", text)))


def _balanced(text: str, opening: int) -> str:
    """The text inside the brace or paren at `opening`, nesting counted; the rest if unclosed."""
    pair = {"{": "}", "(": ")"}[text[opening]]
    depth, cursor = 0, opening
    while cursor < len(text):
        if text[cursor] == text[opening]:
            depth += 1
        elif text[cursor] == pair:
            depth -= 1
            if depth == 0:
                return text[opening + 1 : cursor]
        cursor += 1
    return text[opening + 1 :]


def _code_regions(text: str, *, svelte: bool) -> tuple[list[str], str]:
    """The JavaScript a file runs - its script blocks and every `{...}` expression in its markup -
    and its markup with the style block removed. Prose in the markup is neither: a column heading
    reading "confidence" is a label, not a reference."""
    body = _uncommented(text)
    if not svelte:
        return [body], ""
    code = _SCRIPT.findall(body)
    markup = _SCRIPT.sub("", _STYLE.sub("", body))
    expressions = [_balanced(markup, m.start()) for m in re.finditer(r"\{", markup)]
    return code + expressions, markup


_JS_COMPARISON = re.compile(
    _W + r"\s*(?:===|!==|==|!=|<=|>=|<|>)"
    r"|(?:===|!==|==|!=|<=|>=|(?<![=\-])>|<)\s*[\w.$\]\['\"?]*" + _W
)
# Truthiness and the null test in JavaScript: `tag.confidence && ...`, `!tag.salience`,
# `tag.n_sources ? a : b`, `tag.confidence ?? 0`, `if (tag.confidence)`.
_JS_TRUTHINESS = re.compile(
    r"(?:&&|\|\||\?\?|!(?!=))\s*[\w.$\]\['\"?]*" + _W
    + r"|" + _W + r"[\w.$\]\['\"]*\s*(?:&&|\|\||\?\?|\?(?![.?]))"
    + r"|\bif\s*\(\s*[\w.$\]\['\"?]*" + _W + r"[\w.$\]\['\"]*\s*\)"
)
_JS_REORDER = re.compile(r"\.(?:sort|toSorted|reverse)\s*\(")
_BLOCK_CONDITION = re.compile(r"\{\s*(?:#if|:else\s+if)\b([^}]*)\}")
_BOUND = re.compile(r"\bbind:\w+\s*=\s*\{([^}]*)\}")
_CONTROL = re.compile(r"<(?:input|select|textarea)\b[^>]*>", re.DOTALL)

# Keeping the first N rows in the browser (M56-DATA-05). The component draws the rows in the order
# they came, weakest first, so a `.slice` or `.splice` of them, a `.length` assigned down, or a row
# rendered or hidden on its index hides the best-evidenced tags by rank - §4.1's top-N cut one layer
# up, and one that names no weight. The review slices nothing, so every slice in its files is
# refused. An index is an `{#each}` block's second name or an array callback's second parameter,
# and a comparison on one is a cut by position whatever it is compared with.
_JS_TRUNCATION = re.compile(r"\.(?:slice|splice|toSpliced)\s*\(|\.length\s*(?:-=|=(?!=))")
_EACH_INDEX = re.compile(r"^\s*#each\b.*\bas\b.*,\s*([\w$]+)\s*(?:\([^()]*\))?\s*$", re.DOTALL)
_CALLBACK_INDEX = re.compile(
    r"\.(?:filter|map|flatMap|forEach|some|every|find|findIndex)\s*\(\s*(?:async\s+)?"
    r"(?:function\b[^(]*)?\(\s*(?:\{[^}]*\}|\[[^\]]*\]|[\w$]+)\s*,\s*([\w$]+)"
)


def _index_comparisons(code: list[str]) -> list[str]:
    """Every comparison on a row's index, the `{#if i < 20}` and `hidden={i >= 20}` of a length cut."""
    names = {m.group(1) for region in code for m in _CALLBACK_INDEX.finditer(region)}
    names |= {m.group(1) for region in code if (m := _EACH_INDEX.match(region))}
    hits: list[str] = []
    for name in sorted(names):
        index = re.escape(name)
        compared = re.compile(
            rf"(?<![\w$.]){index}\s*(?:===|!==|==|!=|<=|>=|<|>)"
            rf"|(?:===|!==|==|!=|<=|>=|(?<![=\-])>|<)\s*{index}(?![\w$])"
        )
        hits += [f"index cut: {m.group(0)}" for region in code for m in compared.finditer(region)]
    return hits


def _frontend_cuts(text: str, *, svelte: bool) -> list[str]:
    """Every shape of the cut one layer up: a filter or a re-sort in JS, a comparison, a truthiness
    or null test on a weight, a block rendered on a weight's condition, a control bound to one, and
    the rows cut at a length by a slice or by their index."""
    code, markup = _code_regions(text, svelte=svelte)
    hits: list[str] = _index_comparisons(code)
    for region in code:
        hits += [f"truncated: {m.group(0)}" for m in _JS_TRUNCATION.finditer(region)]
        for match in re.finditer(r"\.filter\s*\(", region):
            argument = _balanced(region, match.end() - 1)
            if _WEIGHT.search(argument):
                hits.append(f".filter({argument.strip()[:60]})")
        hits += [f"re-sorted: {m.group(0)}" for m in _JS_REORDER.finditer(region)]
        hits += [m.group(0) for m in _JS_COMPARISON.finditer(region)]
        hits += [m.group(0) for m in _JS_TRUTHINESS.finditer(region)]
    if svelte:
        hits += [m.group(0) for m in _BLOCK_CONDITION.finditer(markup) if _WEIGHT.search(m.group(1))]
        hits += [m.group(0) for m in _BOUND.finditer(markup) if _WEIGHT.search(m.group(1))]
        hits += [m.group(0) for m in _CONTROL.finditer(markup) if _WEIGHT.search(m.group(0))]
    return hits


def test_the_review_cuts_on_no_weight_in_any_layer():
    """Decision 446: the review is two orderings and never a filter - in its SQL, in the rows after
    the fetch, in the route, in the state module and in the markup."""
    offenders = []
    for path in REVIEW_PYTHON:
        offenders += [f"{path.name}: {hit!r}" for hit in _python_cuts(path.read_text(encoding="utf-8"))]
    for path in REVIEW_FRONTEND:
        text = path.read_text(encoding="utf-8")
        offenders += [
            f"{path.name}: {hit!r}" for hit in _frontend_cuts(text, svelte=path.suffix == ".svelte")
        ]
    assert not offenders, (
        "section 4.1 rule 2: salience, confidence and n_sources are weights and never filters, and "
        "the review of low-evidence tags is where the cut is most tempting (decision 446):\n  "
        + "\n  ".join(offenders)
        + "\n\nOrder by the weight on the server and show every row; a reviewer reads the figure."
    )


def test_the_review_files_this_guard_reads_are_the_ones_that_exist():
    """A guard over a renamed file reads nothing and passes, so the four are asserted present and
    each is asserted to be the review: the domain read orders by confidence, the component shows it."""
    for path in (*REVIEW_PYTHON, *REVIEW_FRONTEND):
        assert path.is_file(), f"{path.relative_to(REPO)} is gone, so this guard reads nothing there"
    assert "ORDER BY t.confidence ASC" in REVIEW_PYTHON[0].read_text(encoding="utf-8")
    assert "figure(tag.confidence)" in REVIEW_FRONTEND[0].read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param('Q = "SELECT term FROM dna_tag WHERE confidence > $1"', id="sql-comparison"),
        pytest.param('Q = "SELECT term FROM dna_tag WHERE t.confidence IS NOT NULL"', id="sql-not-null"),
        pytest.param('Q = "SELECT term FROM dna_tag WHERE salience is null"', id="sql-null"),
        pytest.param('Q = "SELECT term FROM dna_tag ORDER BY confidence DESC LIMIT 20"', id="sql-top-n"),
        pytest.param("kept = [t for t in tags if t['confidence'] >= 0.5]", id="py-comparison"),
        pytest.param("kept = [t for t in tags if t['n_sources']]", id="py-truthy-comprehension"),
        pytest.param("if row.salience:\n    pass", id="py-truthy-if"),
        pytest.param("ok = row.get('confidence') and row", id="py-truthy-boolop"),
        pytest.param("gone = not tag.confidence", id="py-not"),
        pytest.param("kept = list(filter(lambda t: t['confidence'], tags))", id="py-filter-call"),
        pytest.param("return [dict(row) for row in rows][:20]", id="py-slice"),
        pytest.param("tags = tags[0:limit]", id="py-slice-bounds"),
        pytest.param("kept = list(itertools.islice(rows, 20))", id="py-islice"),
        pytest.param("kept = heapq.nlargest(20, rows)", id="py-nlargest"),
        pytest.param(
            'Q = "SELECT term FROM dna_tag ORDER BY confidence ASC FETCH FIRST 20 ROWS ONLY"',
            id="sql-fetch-first",
        ),
        pytest.param('Q = "SELECT term FROM dna_tag ORDER BY n_sources ASC OFFSET 5"', id="sql-offset"),
        pytest.param(
            'Q = "SELECT term FROM dna_tag ORDER BY confidence ASC NULLS LAST, '
            + "t.term, " * 40
            + 't.provider LIMIT 20"',
            id="sql-limit-far-from-the-weight",
        ),
        pytest.param(
            'Q = "SELECT term FROM (SELECT term, row_number() OVER (PARTITION BY title_id ORDER BY '
            'confidence) AS rn FROM dna_tag) s WHERE rn <= 20"',
            id="sql-row-number",
        ),
        pytest.param(
            'Q = "SELECT term, rank() OVER w AS rn FROM dna_tag WINDOW w AS (ORDER BY salience)"',
            id="sql-named-window",
        ),
    ],
)
def test_the_python_arm_refuses_every_shape(snippet):
    assert _python_cuts(snippet), snippet


def test_the_python_arm_passes_an_ordering_a_column_name_and_prose():
    clean = (
        '"""A low-evidence tag is one whose confidence is lowest; nothing is cut on it."""\n'
        'Q = "SELECT term, confidence FROM dna_tag WHERE title_id = $1 ORDER BY confidence ASC"\n'
        'R = "SELECT id FROM dna_reject ORDER BY at DESC LIMIT $1"\n'
        "shown = {'confidence': row['confidence']}\n"
        "if 'confidence' in row:\n    pass\n"
        "first = rows[0]\n"
        'P = "SELECT id FROM dna_reject ORDER BY at DESC, id DESC OFFSET $2 LIMIT $1"\n'
    )
    assert _python_cuts(clean) == []


@pytest.mark.parametrize(
    "snippet, svelte",
    [
        pytest.param("const kept = tags.filter((t) => t.confidence > 0.5);", False, id="js-filter"),
        pytest.param("const kept = tags.filter((t) => t.n_sources);", False, id="js-filter-truthy"),
        pytest.param("const ranked = tags.sort((a, b) => a.term < b.term);", False, id="js-resort"),
        pytest.param("const low = tag.salience < 2;", False, id="js-comparison"),
        pytest.param("const shown = tag.confidence ?? 0;", False, id="js-null-test"),
        pytest.param("const shown = tag.confidence ? 'y' : 'n';", False, id="js-ternary"),
        pytest.param("if (tag.confidence) show(tag);", False, id="js-if"),
        pytest.param("const hide = !row.n_sources;", False, id="js-not"),
        pytest.param("{#if tag.confidence}<li>{tag.term}</li>{/if}", True, id="svelte-if"),
        pytest.param("{#if a}x{:else if tag.salience}y{/if}", True, id="svelte-else-if"),
        pytest.param(
            '<input type="range" bind:value={minConfidence.confidence} />', True, id="svelte-bind"
        ),
        pytest.param('<select name="salience"><option>1</option></select>', True, id="svelte-control"),
        pytest.param(
            "<script>\n  const kept = $derived(tags.filter((t) => t.confidence >= floor));\n</script>",
            True,
            id="svelte-script-filter",
        ),
        pytest.param("return envelope.tags.slice(0, 20);", False, id="js-slice"),
        pytest.param("rows.splice(20);", False, id="js-splice"),
        pytest.param("const kept = rows.toSpliced(20);", False, id="js-to-spliced"),
        pytest.param("rows.length = 20;", False, id="js-length"),
        pytest.param("const kept = tags.filter((t, i) => i < 20);", False, id="js-filter-index"),
        pytest.param(
            "{#each tags.slice(0, 20) as tag (tag.term)}<li>{tag.term}</li>{/each}", True,
            id="svelte-each-slice",
        ),
        pytest.param(
            "{#each tags as tag, i (i)}{#if i < 20}<li>{tag.term}</li>{/if}{/each}", True,
            id="svelte-index-cut",
        ),
        pytest.param(
            "{#each tags as { term }, n}<li hidden={n >= 20}>{term}</li>{/each}", True,
            id="svelte-index-hidden",
        ),
    ],
)
def test_the_frontend_arm_refuses_every_shape(snippet, svelte):
    assert _frontend_cuts(snippet, svelte=svelte), snippet


def test_the_frontend_arm_passes_a_label_a_figure_and_a_comment():
    clean = (
        "<script>\n  // a confidence cut here would be section 4.1's 44%\n"
        "  const rows = $derived(evidenceRows(evidence));\n</script>\n"
        "<!-- confidence > 0.5 is the shape this guard refuses -->\n"
        "{#each rows as tag, i (i)}\n"
        "  <p class=\"data\">confidence {figure(tag.confidence)} - n_sources {figure(tag.n_sources)}</p>\n"
        "{/each}\n"
        "{#if rows.length === 0}<p>none</p>{/if}\n"
        "<style>\n  .data { color: var(--ink-4); }\n</style>\n"
    )
    assert _frontend_cuts(clean, svelte=True) == []


# The cuts the finding M56-DATA-05 wrote into the real files in memory, each in the file's own idiom
# and at the place a "show the 50 weakest" edit would put it. The snippet self-tests prove a pattern
# fires on a shape; these prove it fires on the files it guards, whose markup and literals a pattern
# written against a snippet can fail to reach.
_REAL_FILE_CUTS = [
    pytest.param(
        REVIEW_FRONTEND[0], "{#each tags as tag, i (i)}", "{#each tags.slice(0, 20) as tag, i (i)}",
        id="component-each-slice",
    ),
    pytest.param(
        REVIEW_FRONTEND[0], "{#each tags as tag, i (i)}", "{#each tags as tag, i (i)}{#if i < 20}",
        id="component-index-cut",
    ),
    pytest.param(
        REVIEW_FRONTEND[1], "envelope.tags : []", "envelope.tags.slice(0, 20) : []",
        id="state-module-slice",
    ),
    pytest.param(
        REVIEW_PYTHON[0],
        '"tags": [dict(row) for row in rows]}',
        '"tags": [dict(row) for row in rows][:20]}',
        id="domain-row-slice",
    ),
    pytest.param(
        REVIEW_PYTHON[0], "t.term, t.provider\n", "t.term, t.provider\n     FETCH FIRST 20 ROWS ONLY\n",
        id="domain-fetch-first",
    ),
]


def _cuts_in(path: Path, text: str) -> list[str]:
    if path.suffix == ".py":
        return _python_cuts(text)
    return _frontend_cuts(text, svelte=path.suffix == ".svelte")


@pytest.mark.parametrize("path, clean, cut", _REAL_FILE_CUTS)
def test_the_review_guard_sees_a_length_cut_written_into_the_real_files(path, clean, cut):
    """The rows arrive weakest first, so cutting the list at a length is §4.1 rule 2's top-N cut
    made after the fetch, and it hides the best-evidenced tags (decision 446)."""
    text = path.read_text(encoding="utf-8")
    assert clean in text, f"{path.name} no longer contains {clean!r}; re-anchor this mutation"
    assert _cuts_in(path, text) == []
    assert _cuts_in(path, text.replace(clean, cut, 1)), f"{path.name}: {cut!r} walks past the guard"


# --- the board and the queue: reasons whole ------------------------------------------------------

_CLAMP = re.compile(r"line-clamp|text-overflow|-webkit-box-orient")
_REASON_SLICE = re.compile(r"\breason\b[\w.?\]\[]*\s*\.\s*(?:slice|substring|substr)\s*\(")
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def _reason_cuts(text: str) -> list[str]:
    """A clamp, an ellipsis, a slice of a reason, or a box that clips what overflows it."""
    body = _uncommented(text)
    hits = [m.group(0) for m in _CLAMP.finditer(body)]
    hits += [m.group(0) for m in _REASON_SLICE.finditer(body)]
    for style in re.findall(r"<style\b[^>]*>(.*?)</style>", body, re.DOTALL):
        for selector, rule in _RULE.findall(style):
            if "max-height" in rule and re.search(r"overflow(?:-y)?\s*:\s*(?:hidden|clip)", rule):
                hits.append(f"{selector.strip()} {{ max-height ... overflow: hidden }}")
    return hits


def test_the_board_and_the_queue_show_every_reason_whole():
    """`acquisition_job.reason` and `flywheel_item.reason` are shown verbatim (plan A3, section 9)."""
    offenders = []
    for path in REASON_CARRIERS:
        offenders += [f"{path.name}: {hit}" for hit in _reason_cuts(path.read_text(encoding="utf-8"))]
    assert not offenders, (
        "a reason is the sentence its writer composed for the operator, shown verbatim; these cut "
        "it:\n  " + "\n  ".join(offenders)
    )
    board = REASON_CARRIERS[0].read_text(encoding="utf-8")
    queue = REASON_CARRIERS[1].read_text(encoding="utf-8")
    assert 'data-testid="board-reason">{job.reason}</p>' in board, "the board prints no reason"
    assert 'data-testid="flywheel-reason">{item.reason}</p>' in queue, "the queue prints no reason"
    for name, text in (("AcquisitionBoard", board), ("FlywheelQueue", queue)):
        assert "white-space: pre-wrap" in text and "overflow-wrap: anywhere" in text, (
            f"{name} does not wrap its reasons, so a long one widens the phone instead of wrapping"
        )


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param("<style>\n  .reason { -webkit-line-clamp: 2; }\n</style>", id="clamp"),
        pytest.param("<style>\n  .reason { text-overflow: ellipsis; }\n</style>", id="ellipsis"),
        pytest.param("<style>\n  .reason { max-height: 3em; overflow: hidden; }\n</style>", id="clip"),
        pytest.param("<p>{job.reason.slice(0, 80)}</p>", id="slice"),
        pytest.param("const short = item.reason?.substring(0, 40);", id="substring"),
    ],
)
def test_the_reason_guard_refuses_every_cut(snippet):
    assert _reason_cuts(snippet), snippet


def test_the_reason_guard_passes_a_wrapped_reason():
    clean = (
        "<p class=\"reason\">{job.reason}</p>\n"
        "<style>\n  .reason { white-space: pre-wrap; overflow-wrap: anywhere; }\n"
        "  .docs { max-height: none; }\n</style>\n"
    )
    assert _reason_cuts(clean) == []


# --- one spelling of section 8's names -----------------------------------------------------------

_RETYPED_STAGE = "reviews gate"


def _retyped(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in {".svelte", ".js"}
        and _RETYPED_STAGE in path.read_text(encoding="utf-8").lower()
    )


def test_no_frontend_file_retypes_the_stage_names():
    """Plan A2: the board's ten names come from `pipeline.STAGES` through the envelope, and only
    from there. A file under frontend/src naming `reviews gate` is a second copy of the list."""
    assert FRONTEND.is_dir(), "frontend/src is missing, so this guard reads nothing"
    retyped = _retyped(FRONTEND)
    assert not retyped, (
        f"these files spell section 8's stage names themselves: {retyped}. The board draws them "
        "from GET /api/admin/acquisition's `stages`, which is `acquire/pipeline.STAGES`; a copy here "
        "is a second spelling nothing compares with the first."
    )


def test_the_stage_name_guard_sees_a_copy(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "stages.js").write_text(
        "export const STAGES = ['identify', 'enrich', 'derive', 'Reviews Gate'];\n", encoding="utf-8"
    )
    (tmp_path / "lib" / "clean.svelte").write_text("<p>{stage.name}</p>\n", encoding="utf-8")
    assert _retyped(tmp_path) == ["lib/stages.js"]
