"""§6.5 (decision 549): a person's ladder read per DNA term, and two members' read side by side.

A term is read for a person once `MIN_CARRIERS` of their placed titles of the kind carry it in the
extracted tier. Its position is the mean step of those titles less the mean step of every title they
placed, scaled so their strongest term sits at `END`. Privacy is applied here, where the payload is
built: another member's steps, order and ladder size never leave this module, and a term's films go
only to the two members being compared.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

import asyncpg

from spielplan.db import dna_terms
from spielplan.ledger.observations import latest_tier_edit_sql, rescale_level, tier_set_of

MIN_CARRIERS = 4
PICKABLE_AT = 20
END = 0.9
SIDE = 5
SLOTS = 4
# Closer to the middle than this, a marker reads as "near the middle".
OFF_MIDDLE = 0.2

NOUN = {"movie": "films", "series": "series"}


class NotPickable(ValueError):
    """A seat holds someone who cannot be compared: absent, the other seat, or under `PICKABLE_AT`."""


@dataclass(frozen=True)
class _Film:
    id: int
    name: str
    poster_path: str | None
    step: int


@dataclass(frozen=True)
class _Row:
    term: str
    label: str
    facet: str
    carriers: tuple[_Film, ...]  # the person's own order, highest placed first
    d: float
    pos: float


@dataclass(frozen=True)
class _Ladder:
    films: tuple[_Film, ...]
    rows: dict[str, _Row]


async def _placed(conn: asyncpg.Connection, *, user_id: int, kind: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        f"""
        SELECT te.title_id, te.tier, te.n_levels, t.name, t.poster_path, ls.s
          FROM ({latest_tier_edit_sql()}) te
          JOIN title t ON t.id = te.title_id AND t.kind = $2
          LEFT JOIN ledger_state ls ON ls.user_id = $1 AND ls.title_id = te.title_id
        """,
        user_id,
        kind,
    )


async def _ladder(conn: asyncpg.Connection, *, user_id: int, kind: str) -> _Ladder:
    k = len(await tier_set_of(conn, user_id=user_id, kind=kind))
    placed = [
        (rescale_level(int(r["tier"]), k_from=r["n_levels"], k_to=k), r)
        for r in await _placed(conn, user_id=user_id, kind=kind)
    ]
    # Within a step the person's own Rank order: the fit's s, highest first.
    placed.sort(key=lambda p: (-p[0], p[1]["s"] is None, -(p[1]["s"] or 0.0), p[1]["title_id"]))
    films = tuple(
        _Film(id=int(r["title_id"]), name=r["name"], poster_path=r["poster_path"], step=step)
        for step, r in placed
    )
    if not films:
        return _Ladder(films=films, rows={})

    tags = await conn.fetch(
        f"""
        SELECT DISTINCT d.title_id, d.term, d.facet, dl.label
          FROM dna_tag d
          LEFT JOIN dna_term dl ON dl.version = d.version AND dl.term = d.term
         WHERE d.version = {dna_terms.ACTIVE_VERSION} AND d.title_id = ANY($1::int[])
        """,
        [f.id for f in films],
    )
    by_id = {f.id: f for f in films}
    order = {f.id: i for i, f in enumerate(films)}
    carriers: dict[str, list[_Film]] = {}
    named: dict[str, tuple[str, str]] = {}
    for t in tags:
        carriers.setdefault(t["term"], []).append(by_id[int(t["title_id"])])
        named[t["term"]] = (dna_terms.label_of(t["term"], t["label"]), t["facet"])

    mean = sum(f.step for f in films) / len(films)
    read = {term: fs for term, fs in carriers.items() if len(fs) >= MIN_CARRIERS}
    d = {term: sum(f.step for f in fs) / len(fs) - mean for term, fs in read.items()}
    strongest = max((abs(v) for v in d.values()), default=0.0)
    rows = {
        term: _Row(
            term=term,
            label=named[term][0],
            facet=named[term][1],
            carriers=tuple(sorted(fs, key=lambda f: order[f.id])),
            d=d[term],
            pos=END * d[term] / strongest if strongest else 0.0,
        )
        for term, fs in read.items()
    }
    return _Ladder(films=films, rows=rows)


def _high_first(row: _Row) -> tuple[float, int, str]:
    return (-row.pos, -len(row.carriers), row.label)


def _low_first(row: _Row) -> tuple[float, int, str]:
    return (row.pos, -len(row.carriers), row.label)


def _films(films: list[_Film] | tuple[_Film, ...]) -> dict[str, Any]:
    return {
        "films": [{"id": f.id, "name": f.name, "poster_path": f.poster_path} for f in films[:SLOTS]],
        "more": max(0, len(films) - SLOTS),
    }


def _own_row(row: _Row) -> dict[str, Any]:
    films = row.carriers if row.pos >= 0 else tuple(reversed(row.carriers))
    return {
        "term": row.term,
        "label": row.label,
        "facet": row.facet,
        "pos": round(row.pos, 4),
        **_films(films),
        "model": {"d": round(row.d, 3), "carriers": len(row.carriers)},
    }


async def chart(conn: asyncpg.Connection, *, user_id: int, kind: str) -> dict[str, Any]:
    """Your taste: the 5 terms that sit highest, the 5 that land lowest, and every term read."""
    ladder = await _ladder(conn, user_id=user_id, kind=kind)
    ordered = sorted(ladder.rows.values(), key=_high_first)
    low = sorted((r for r in ordered if r.pos < 0), key=_low_first)
    return {
        "kind": kind,
        "placed": len(ladder.films),
        "n_terms": len(ordered),
        "high": [_own_row(r) for r in ordered if r.pos > 0][:SIDE],
        "low": [_own_row(r) for r in low[:SIDE]],
        "all": [_own_row(r) for r in ordered],
    }


async def _people(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT id, name, role, colour FROM app_user "
        "WHERE is_active AND role IN ('admin', 'member') ORDER BY lower(name), id"
    )
    initials = _initials([r["name"] for r in rows])
    return [
        {
            "id": int(r["id"]),
            "name": r["name"],
            "role": r["role"],
            "colour": r["colour"],
            "initials": mark,
        }
        for r, mark in zip(rows, initials, strict=True)
    ]


def _initials(names: list[str]) -> list[str]:
    """One letter, or two where another name starts with the same one: the first letter and the
    earliest later one no namesake before it took, so no two markers read alike."""
    firsts = Counter(n.strip()[:1].upper() for n in names)
    taken: set[str] = set()
    marks = []
    for name in (n.strip() for n in names):
        first = name[:1].upper()
        if firsts[first] == 1:
            marks.append(first)
            continue
        tails = [c.lower() for c in name[1:] if c.isalnum()] + [str(i) for i in range(2, len(names) + 2)]
        mark = next(first + c for c in tails if first + c not in taken)
        taken.add(mark)
        marks.append(mark)
    return marks


def _seat(person: dict[str, Any], placed: int, kind: str) -> dict[str, Any]:
    ok = placed >= PICKABLE_AT
    return {**person, "pickable": ok, "reason": None if ok else f"Not enough {NOUN[kind]} placed yet"}


async def members(conn: asyncpg.Connection, *, viewer_id: int, kind: str) -> dict[str, Any]:
    """Every member, pickable or with the reason not, and the seats Compare opens on."""
    people = await _people(conn)
    placed = {
        p["id"]: {int(r["title_id"]) for r in await _placed(conn, user_id=p["id"], kind=kind)}
        for p in people
    }
    listed = [_seat(p, len(placed[p["id"]]), kind) for p in people]
    mine = placed.get(viewer_id, set())
    # `min` keeps the first of a tie, and `listed` is in name order.
    partner = min(
        (m for m in listed if m["id"] != viewer_id and m["pickable"]),
        key=lambda m: -len(mine & placed[m["id"]]),
        default=None,
    )
    return {"members": listed, "default": [viewer_id, partner["id"] if partner else None]}


def _gap(row: dict[str, Any]) -> float:
    return abs(row["pa"] - row["pb"])


def _off_middle(row: dict[str, Any]) -> bool:
    return row["pa"] * row["pb"] > 0 and min(abs(row["pa"]), abs(row["pb"])) >= OFF_MIDDLE


def _most_different(row: dict[str, Any]) -> tuple[float, str]:
    return (-_gap(row), row["label"])


def _most_alike(row: dict[str, Any]) -> tuple[bool, float, str]:
    return (not _off_middle(row), _gap(row), row["label"])


def _behind(term: str, mine: _Ladder, theirs: _Ladder) -> list[_Film]:
    """Films both placed first, then the viewer's own, each in the viewer's order; then the other's
    by name, so nothing here carries the other member's order."""
    own = mine.rows[term].carriers
    other = {f.id for f in theirs.rows[term].carriers}
    known = {f.id for f in own}
    rest = sorted(
        (f for f in theirs.rows[term].carriers if f.id not in known), key=lambda f: (f.name, f.id)
    )
    return [f for f in own if f.id in other] + [f for f in own if f.id not in other] + rest


async def compare(
    conn: asyncpg.Connection, *, viewer_id: int, kind: str, a: int, b: int
) -> dict[str, Any]:
    """Compare: the terms both members read, Most alike and Most different. Raises NotPickable."""
    people = {p["id"]: p for p in await _people(conn)}
    if a == b or a not in people or b not in people:
        raise NotPickable("Pick two different people to compare.")
    ladders = {x: await _ladder(conn, user_id=x, kind=kind) for x in (a, b)}
    if any(len(ladder.films) < PICKABLE_AT for ladder in ladders.values()):
        raise NotPickable(
            f"Comparing {NOUN[kind]} opens once two of you have each placed {PICKABLE_AT} {NOUN[kind]}."
        )

    seated = viewer_id in (a, b)
    mine, theirs = (ladders[a], ladders[b]) if viewer_id != b else (ladders[b], ladders[a])
    rows = []
    for term, ra in ladders[a].rows.items():
        rb = ladders[b].rows.get(term)
        if rb is None:
            continue
        films = _behind(term, mine, theirs) if seated else []
        rows.append(
            {
                "term": term,
                "label": ra.label,
                "facet": ra.facet,
                "pa": round(ra.pos, 4),
                "pb": round(rb.pos, 4),
                **_films(films),
            }
        )

    every = sorted(rows, key=_most_different)
    different = every[:SIDE]
    taken = {r["term"] for r in different}
    alike = sorted((r for r in rows if r["term"] not in taken), key=_most_alike)[:SIDE]
    note = None
    if alike and not any(_off_middle(r) for r in alike):
        note = (
            "Nothing sits clearly high or low for you both yet. These are the closest, near both "
            "your middles."
            if seated
            else "Nothing sits clearly high or low for them both yet. These are the closest, near "
            "both their middles."
        )
    return {
        "kind": kind,
        "a": {**people[a], "pickable": True, "reason": None},
        "b": {**people[b], "pickable": True, "reason": None},
        "alike": alike,
        "different": different,
        "all": every,
        "films_visible": seated,
        "note": note,
    }


__all__ = ["MIN_CARRIERS", "PICKABLE_AT", "NotPickable", "chart", "compare", "members"]
