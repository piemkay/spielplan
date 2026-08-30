"""The materialised §5.1 serving stack and the ranked read. Spec v2.1 §5.1, §4.1 rule 5, §6.0, §10.

Everything the ranked surfaces sort on is computed once, by the nightly job, into two tables:

  `title_prior`  the crowd half — b(t), b_i, item_n, gate, e_source. User-independent.
  `user_score`   the per-user half — score_u(t) and its ⟨v_u, e(t)⟩ component, `kind` in the row.

So the number the §6.0 title card prints and the number the ranked list sorts on are the same
arithmetic rather than two implementations of it.

§4.1 RULE 5, STRUCTURALLY. "Every ranking surface partitions by kind (measured: the
unpartitioned crowd top-10 is 8/10 TV series)." Owner decision 18 makes kind two independent
toggles — either or both, never neither. This module therefore contains exactly one ranked
statement, it binds `us.kind = $2` as a **scalar**, and `ranked_sections()` calls it once per
selected kind and concatenates the SECTIONS. There is no statement here that binds kind as a
set, so there is no merged ordering anywhere to accidentally return: `ranked_sections` hands
back a list of kind-headed sections and never a list of titles. A person filter is a predicate
inside each section's WHERE and never touches the kind loop — decision 18's "a filmography is
complete across two sections", not "a filter suspends the partition".

§10, THREE DEEP. "Everything expressed in the old Backbone's basis is garbage against a new
one." `bundle_version` is on the prior row, on the score row, and bound in every read here. A
row from a superseded basis is not returned as a stale number; it is not returned at all, and
the section's `total` says so.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from spielplan.db.library import KINDS, Kind, SeenFilter, normalise_kinds
from spielplan.scoring.backbone import Backbone, Coordinate, coordinate, unpack_vec

log = logging.getLogger("spielplan.scoring.serve")

# §6.0 names the surfaces "film/series"; the two ranked sections are headed with the plural the
# library controls already use, so the toggle and the heading say the same word.
HEADINGS: dict[str, str] = {"movie": "Films", "series": "Series"}


@dataclass
class PriorReport:
    """What one `materialise_priors` pass wrote, in the terms §12's exit criterion is stated in."""

    written: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    # §12 (M2): "every owned title has a coordinate (warm Backbone row or Cold Tower placement)".
    # Named, not counted: a report that says "3" cannot be acted on.
    uncoordinated_owned: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "by_source": dict(self.by_source),
            "uncoordinated_owned": list(self.uncoordinated_owned),
        }


# --- coordinates ------------------------------------------------------------------------------


async def placements(conn, *, bundle_version: str) -> dict[int, tuple[np.ndarray, float]]:
    """The Cold Tower's (ê, b̂) per title, in this basis. §5.3 placement reconciliation writes it.

    Bound to `bundle_version` because a coordinate computed in the old basis is garbage against
    a new one (§10) — the rows survive a rollback precisely because they are not overwritten.
    """
    rows = await conn.fetch(
        "SELECT title_id, e_hat, b_hat FROM title_placement WHERE bundle_version = $1",
        bundle_version,
    )
    return {r["title_id"]: (unpack_vec(r["e_hat"]), float(r["b_hat"])) for r in rows}


async def coordinates(
    conn, backbone: Backbone, *, bundle_version: str, kind: Kind | None = None
) -> dict[int, Coordinate]:
    """Every title of `kind` that has a coordinate at all, keyed by title_id.

    Titles without one are simply absent — `uncoordinated_owned` is where they are named.
    """
    placed = await placements(conn, bundle_version=bundle_version)
    if kind is None:
        rows = await conn.fetch("SELECT id FROM title ORDER BY id")
    else:
        rows = await conn.fetch("SELECT id FROM title WHERE kind = $1 ORDER BY id", kind)

    coords: dict[int, Coordinate] = {}
    for row in rows:
        title_id = int(row["id"])
        c = coordinate(title_id, backbone, placed.get(title_id))
        if c is not None:
            coords[title_id] = c
    return coords


async def materialise_priors(conn, backbone: Backbone, *, bundle_version: str) -> PriorReport:
    """Write `title_prior` for every title. The crowd half of §5.1, computed once.

    An uncoordinated title keeps a row with `b` NULL and `e_source = 'none'` rather than being
    omitted: the row is what lets the reconciliation report name the offenders instead of
    reporting a difference between two counts.
    """
    placed = await placements(conn, bundle_version=bundle_version)
    titles = await conn.fetch("SELECT id, is_owned FROM title ORDER BY id")

    ids: list[int] = []
    b_values: list[float | None] = []
    b_i_values: list[float | None] = []
    item_n_values: list[int] = []
    gates: list[float] = []
    sources: list[str] = []
    report = PriorReport()

    for row in titles:
        title_id = int(row["id"])
        c = coordinate(title_id, backbone, placed.get(title_id))
        ids.append(title_id)
        b_i_values.append(backbone.raw_prior(title_id))
        if c is None:
            b_values.append(None)
            item_n_values.append(backbone.support(title_id))
            gates.append(0.0)
            sources.append("none")
            if row["is_owned"]:
                report.uncoordinated_owned.append(title_id)
        else:
            b_values.append(c.b)
            item_n_values.append(c.item_n)
            gates.append(c.gate)
            sources.append(c.e_source)
        report.by_source[sources[-1]] = report.by_source.get(sources[-1], 0) + 1

    await conn.execute(
        """
        INSERT INTO title_prior (title_id, bundle_version, b, b_i, item_n, gate, e_source, computed_at)
        SELECT u.title_id, $1, u.b, u.b_i, u.item_n, u.gate, u.e_source, now()
          FROM unnest($2::integer[], $3::real[], $4::real[], $5::integer[], $6::real[], $7::text[])
               AS u(title_id, b, b_i, item_n, gate, e_source)
        ON CONFLICT (title_id) DO UPDATE
           SET bundle_version = EXCLUDED.bundle_version, b = EXCLUDED.b, b_i = EXCLUDED.b_i,
               item_n = EXCLUDED.item_n, gate = EXCLUDED.gate, e_source = EXCLUDED.e_source,
               computed_at = now()
        """,
        bundle_version, ids, b_values, b_i_values, item_n_values, gates, sources,
    )
    report.written = len(ids)
    if report.uncoordinated_owned:
        log.warning(
            "§12 M2 exit criterion: %d owned titles have no coordinate: %s",
            len(report.uncoordinated_owned), report.uncoordinated_owned[:20],
        )
    return report


async def uncoordinated_owned(conn, *, kind: Kind, bundle_version: str) -> list[int]:
    """§12's M2 exit criterion as a list a test can read: owned titles of this kind with no
    coordinate. Must be empty."""
    rows = await conn.fetch(
        """
        SELECT tp.title_id FROM title_prior tp JOIN title t ON t.id = tp.title_id
         WHERE tp.e_source = 'none' AND tp.bundle_version = $1 AND t.kind = $2 AND t.is_owned
         ORDER BY tp.title_id
        """,
        bundle_version, kind,
    )
    return [r["title_id"] for r in rows]


# --- the per-user half -------------------------------------------------------------------------


async def replace_scores(
    conn, *, user_id: int, kind: Kind, bundle_version: str,
    rows: Sequence[tuple[int, float, float]],
) -> int:
    """Rewrite one (user, kind)'s `user_score` rows. `kind` is written into every row.

    A refit replaces rather than updates: a title that lost its coordinate must lose its score,
    and an UPDATE would leave it ranked on a number from a basis that no longer exists.
    """
    async with conn.transaction():
        await conn.execute("DELETE FROM user_score WHERE user_id = $1 AND kind = $2", user_id, kind)
        if rows:
            await conn.execute(
                """
                INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf, computed_at)
                SELECT $1, u.title_id, $2, $3, u.score, u.cf, now()
                  FROM unnest($4::integer[], $5::real[], $6::real[]) AS u(title_id, score, cf)
                """,
                user_id, kind, bundle_version,
                [int(r[0]) for r in rows], [float(r[1]) for r in rows], [float(r[2]) for r in rows],
            )
    return len(rows)


async def fit_row(conn, *, user_id: int, kind: Kind) -> dict[str, Any] | None:
    """The stored fold-in for one (user, kind), or None when the user has never been fitted.

    "Fitted to zero labels" and "never fitted" are different states and the section copy says so
    (§6.0's zero-verdict fallback), which is why the zero-label case still writes a row.
    """
    row = await conn.fetchrow(
        """
        SELECT vec, blend_beta, label_count, mu, prior_mean, prior_sd, cf_sd,
               foldin_lambda, cv_rho, bundle_version, updated_at
          FROM user_vector WHERE user_id = $1 AND kind = $2 AND purpose = 'foldin'
        """,
        user_id, kind,
    )
    return dict(row) if row else None


# --- the ranked read ---------------------------------------------------------------------------


def _filters(
    args: list[Any],
    *,
    q: str | None,
    genre: str | None,
    decade: int | None,
    person_id: int | None,
    seen: SeenFilter,
    owned_only: bool,
    user_id: int,
) -> str:
    """The predicates shared by a section and by the count of what a *deselected* kind hides.

    Deliberately identical for both: a "2 series hidden" line computed with different filters
    from the section it describes is a lie that nobody would notice.
    """
    where: list[str] = []

    def arg(value: Any) -> str:
        args.append(value)
        return f"${len(args)}"

    if q:
        needle = f"%{q.lower()}%"
        where.append(
            f"(lower(t.name) LIKE {arg(needle)} OR EXISTS ("
            f"  SELECT 1 FROM title_alias a WHERE a.title_id = t.id AND lower(a.alias) LIKE {arg(needle)}"
            f"))"
        )
    if genre:
        where.append(
            f"EXISTS (SELECT 1 FROM title_genre g WHERE g.title_id = t.id AND g.genre = {arg(genre)})"
        )
    if decade is not None:
        where.append(f"t.year >= {arg(decade)} AND t.year < {arg(decade + 10)}")
    if person_id is not None:
        # §6.0: "credits, each person tappable → filters the library to their filmography".
        # A predicate on titles. It cannot reach the kind loop, which is decision 18's rule.
        where.append(
            f"EXISTS (SELECT 1 FROM credit c WHERE c.title_id = t.id AND c.person_id = {arg(person_id)})"
        )
    if owned_only:
        where.append("t.is_owned")
    if seen != "any":
        # An absent user_title row is the default, not an assertion (§7.3), so `unseen` must
        # include it rather than matching only explicit rows.
        uid = arg(user_id)
        predicate = (
            f"SELECT 1 FROM user_title s WHERE s.title_id = t.id "
            f"AND s.user_id = {uid} AND s.state = 'seen'"
        )
        where.append(f"EXISTS ({predicate})" if seen == "seen" else f"NOT EXISTS ({predicate})")

    return (" AND " + " AND ".join(where)) if where else ""


async def ranked_section(
    conn,
    *,
    user_id: int,
    kind: Kind,
    bundle_version: str,
    q: str | None = None,
    genre: str | None = None,
    decade: int | None = None,
    person_id: int | None = None,
    seen: SeenFilter = "any",
    owned_only: bool = True,
    limit: int = 24,
    offset: int = 0,
) -> dict[str, Any]:
    """ONE kind's ranked section. `kind` is bound as a scalar — there is no set-valued variant.

    Ordering is `score DESC, title.id ASC`. The tie-break is not cosmetic: paging over equal
    scores is otherwise nondeterministic across pages, and a household's cold catalogue has
    plenty of equal scores.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")

    args: list[Any] = [user_id, kind, bundle_version]
    clause = _filters(
        args, q=q, genre=genre, decade=decade, person_id=person_id, seen=seen,
        owned_only=owned_only, user_id=user_id,
    )
    # The prior joins on the score's OWN bundle_version, not on the bound one: a score and the
    # prior it was computed against must come from one basis or the card and the sort disagree.
    joins = """
          FROM user_score us
          JOIN title t ON t.id = us.title_id
          JOIN title_prior tp ON tp.title_id = us.title_id AND tp.bundle_version = us.bundle_version
          LEFT JOIN user_title ut ON ut.user_id = us.user_id AND ut.title_id = us.title_id
          LEFT JOIN ledger_state ls ON ls.user_id = us.user_id AND ls.title_id = us.title_id
    """
    where = f"WHERE us.user_id = $1 AND us.kind = $2 AND us.bundle_version = $3{clause}"
    total = await conn.fetchval(f"SELECT count(*) {joins} {where}", *args)

    args.append(limit)
    lim = f"${len(args)}"
    args.append(offset)
    off = f"${len(args)}"
    rows = await conn.fetch(
        f"""
        SELECT t.id, t.kind, t.name, t.year, t.runtime_min, t.poster_path, t.placement,
               COALESCE(ut.state, 'unseen') AS seen_state,
               us.score, us.cf, tp.b, tp.gate, tp.item_n, tp.e_source,
               ls.s, ls.sigma, ls.cdf, ls.tier
        {joins} {where}
         ORDER BY us.score DESC, t.id
         LIMIT {lim} OFFSET {off}
        """,
        *args,
    )

    fit = await fit_row(conn, user_id=user_id, kind=kind)
    beta = float(fit["blend_beta"]) if fit and fit["blend_beta"] is not None else 0.0
    labels = int(fit["label_count"]) if fit and fit["label_count"] is not None else 0
    return {
        "kind": kind,
        "heading": HEADINGS[kind],
        "total": int(total or 0),
        # §6.0's zero-verdict fallback needs to know the difference between "ranked by the crowd
        # prior alone" and "ranked by this person", without a second query.
        "personalised": beta > 0.0,
        "beta": beta,
        "label_count": labels,
        "fitted": fit is not None,
        "uncoordinated": await uncoordinated_owned(conn, kind=kind, bundle_version=bundle_version),
        "items": [dict(r) for r in rows],
    }


async def ranked_sections(
    conn,
    *,
    user_id: int,
    kinds: Sequence[str],
    bundle_version: str,
    **filters: Any,
) -> list[dict[str, Any]]:
    """One section per selected kind, in canonical order. Never one merged ordering.

    §4.1 rule 5 + decision 18. `normalise_kinds` refuses the empty selection (neither toggle is
    not a state), and the return type is a list of SECTIONS — there is no top-level ordering
    for a caller to render, because there is none to render.

    `limit`/`offset` apply PER SECTION, which is what makes a merged-then-split implementation
    detectable by shape rather than by reading the code: with both kinds on and limit=5, this
    returns up to 5 films AND up to 5 series, never 5 rows in total.
    """
    return [
        await ranked_section(
            conn, user_id=user_id, kind=kind, bundle_version=bundle_version, **filters
        )
        for kind in normalise_kinds(kinds)
    ]


async def hidden_by_kind(
    conn, *, user_id: int, kinds: Sequence[str], bundle_version: str, **filters: Any
) -> dict[str, int]:
    """How many ranked rows each *unselected* kind holds, under the same filters.

    §6.0's count line has to be able to say "2 series hidden": a toggle that hides things
    without saying how many is the silent truncation the control exists to fix. Computed with
    the section's own predicates, so the number describes what the toggle would reveal.
    """
    chosen = set(normalise_kinds(kinds))
    hidden: dict[str, int] = {}
    for kind in KINDS:
        if kind in chosen:
            continue
        args: list[Any] = [user_id, kind, bundle_version]
        clause = _filters(
            args,
            q=filters.get("q"), genre=filters.get("genre"), decade=filters.get("decade"),
            person_id=filters.get("person_id"), seen=filters.get("seen", "any"),
            owned_only=filters.get("owned_only", True), user_id=user_id,
        )
        count = await conn.fetchval(
            f"""
            SELECT count(*)
              FROM user_score us
              JOIN title t ON t.id = us.title_id
             WHERE us.user_id = $1 AND us.kind = $2 AND us.bundle_version = $3{clause}
            """,
            *args,
        )
        if count:
            hidden[kind] = int(count)
    return hidden


# --- §6.0's model line -------------------------------------------------------------------------


def _format_line(b: float, beta: float, gate_value: float) -> str:
    """§6.0: "the model line in the data voice (`b(t) 0.52 · β 0.8 · gate 0.93`)".

    Two decimals everywhere, including β where the spec's own example prints one: one formatter
    with one precision is how the card and the rail stop drifting apart. The number printed is
    the number the ranking uses — no display rescaling, which is the whole of the transparency
    promise.
    """
    return f"b(t) {b:.2f} · β {beta:.2f} · gate {gate_value:.2f}"


def _format_support(sigma: float | None, item_n: int) -> str:
    # σ renders as an em dash before the Ledger has ever fitted this title, never as 0.00 —
    # which would read as certainty about a title nobody has rated.
    shown = f"±{sigma:.2f}" if sigma is not None else "—"
    return f"σ {shown} · support n={item_n}"


async def model_line(conn, *, user_id: int, title_id: int, bundle_version: str) -> dict[str, Any]:
    """The §6.0 title-card model line, with the real b(t), β and gate.

    Ungated by the show-the-model preference (proposal 19, decision 117): this is crowd-level
    provenance and §6.0's M0 transparency promise, not an annotation about this viewer.
    """
    row = await conn.fetchrow(
        """
        SELECT t.kind, tp.b, tp.gate, tp.item_n, tp.e_source, tp.bundle_version, ls.sigma
          FROM title t
          LEFT JOIN title_prior tp ON tp.title_id = t.id
          LEFT JOIN ledger_state ls ON ls.title_id = t.id AND ls.user_id = $2
         WHERE t.id = $1
        """,
        title_id, user_id,
    )
    if row is None:
        return {"available": False, "reason": "no such title"}
    if row["bundle_version"] is None or row["bundle_version"] != bundle_version:
        # Either nothing has been materialised yet, or what was materialised belongs to a basis
        # that is no longer active. Both are "we cannot say", and neither is a number.
        return {
            "available": False,
            "reason": "no prior computed for the active bundle",
            "bundle": bundle_version,
        }
    if row["e_source"] == "none":
        return {
            "available": False,
            "reason": "no Backbone row and no Cold Tower placement",
            "bundle": bundle_version,
            "e_source": "none",
        }

    fit = await fit_row(conn, user_id=user_id, kind=row["kind"])
    beta = float(fit["blend_beta"]) if fit and fit["blend_beta"] is not None else 0.0
    sigma = float(row["sigma"]) if row["sigma"] is not None else None
    b, gate_value, item_n = float(row["b"]), float(row["gate"]), int(row["item_n"])
    return {
        "available": True,
        "bundle": bundle_version,
        "b": b,
        "beta": beta,
        "gate": gate_value,
        "item_n": item_n,
        "sigma": sigma,
        "e_source": row["e_source"],
        "text": _format_line(b, beta, gate_value),
        "second_line": _format_support(sigma, item_n),
    }
