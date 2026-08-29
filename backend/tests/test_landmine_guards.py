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
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "spielplan"
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

WEIGHT_COLUMNS = ("confidence", "salience", "n_sources", "weight")

# `WHERE ... confidence > 0.5`, `AND salience >= 2`, `HAVING confidence < $1`.
FILTER_PATTERN = re.compile(
    r"(?:where|and|or|having)\s[^;]{0,200}?\b("
    + "|".join(WEIGHT_COLUMNS)
    + r")\b\s*(?:>=|<=|>|<)\s*[\d.$]",
    re.IGNORECASE | re.DOTALL,
)

SANCTIONED_VIEW = "dna_tagged"
UNION_PATTERN = re.compile(r"union\s+(?:all\s+)?select", re.IGNORECASE)


def _sql_sources() -> list[Path]:
    """Everything that can contain SQL: the package *and* the migrations."""
    return sorted(PACKAGE.rglob("*.py")) + sorted(MIGRATIONS.glob("*.sql"))


def _strip_sanctioned_view(sql: str) -> str:
    """Remove the whole `CREATE VIEW dna_tagged AS … ;` statement, and nothing else."""
    start = sql.lower().find(f"create view {SANCTIONED_VIEW}")
    if start == -1:
        return sql
    end = sql.find(";", start)
    return sql[:start] + sql[end + 1 :] if end != -1 else sql[:start]


def _weight_filters(text: str) -> list[str]:
    """Matches that are query predicates. A CHECK constraint on a weight column is a domain
    rule, not a filter — `weight real CHECK (weight >= -1.0 AND weight <= 1.0)` keeps the
    authored axis TSVs in range (§6.4) and deletes nothing."""
    hits = []
    for m in FILTER_PATTERN.finditer(text):
        preceding = text[max(0, m.start() - 80) : m.start()].lower()
        if "check (" in preceding:
            continue
        hits.append(m.group(0))
    return hits


def _tier_unions(text: str) -> list[int]:
    body = _strip_sanctioned_view(text)
    hits = []
    for match in UNION_PATTERN.finditer(body):
        # Look at the statement the union sits in, delimited by `;`.
        start = body.rfind(";", 0, match.start()) + 1
        end = body.find(";", match.end())
        statement = body[start : end if end != -1 else len(body)].lower()
        if "dna_tag" in statement and "dna_projected" in statement:
            hits.append(body[: match.start()].count("\n") + 1)
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


def test_the_only_sanctioned_dna_union_is_the_tier_labelled_view():
    offenders: list[str] = []
    for path in _sql_sources():
        offenders += [f"{path.name}:{line}" for line in _tier_unions(path.read_text(encoding="utf-8"))]
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
    ):
        assert not _weight_filters(ok), f"the rule-2 guard false-positives on: {ok!r}"


def test_union_guard_catches_a_real_violation():
    violation = (
        "SELECT term FROM dna_tag WHERE title_id = 1 "
        "UNION ALL SELECT term FROM dna_projected WHERE title_id = 1;"
    )
    assert _tier_unions(violation), "the rule-1 guard would not catch an unlabelled tier union"


def test_union_guard_allows_the_sanctioned_view_and_nothing_after_it():
    sanctioned = (MIGRATIONS / "0004_dna.sql").read_text(encoding="utf-8")
    assert not _tier_unions(sanctioned)
    # …but a second union in the same file, outside the view, must still be caught.
    assert _tier_unions(
        sanctioned + "\nSELECT term FROM dna_tag UNION ALL SELECT term FROM dna_projected;"
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
