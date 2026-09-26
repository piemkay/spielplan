"""Who a credit is about, which credits survive, and the loose key that recognises a scraped page.

`upsert_person` asserts a minted id lands in the app's half of the id space (>= `APP_ID_MIN`);
`known_people` lives here so stages 2 and 3 share one "is this the same human" rule.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import asyncpg

from spielplan.sources._htmlutil import fix_mojibake, unescape

# `person_id_seq`'s MINVALUE; copied, not imported, because `derive/` must not depend on `acquire/`.
APP_ID_MIN = 1_000_000_000

# Latin letters with no NFKD decomposition; without folding, the `[^a-z0-9]` filter deletes them.
_FOLD = str.maketrans({
    "\u0142": "l", "\u0141": "l", "\u0131": "i", "\u0130": "i",
    "\u00f8": "o", "\u00d8": "o", "\u0111": "d", "\u0110": "d",
    "\u00f0": "d", "\u00d0": "d", "\u0127": "h", "\u0167": "t",
    "\u00fe": "th", "\u00de": "th", "\u00e6": "ae", "\u00c6": "ae",
    "\u0153": "oe", "\u0152": "oe", "\u00df": "ss",
})


def loose_name(s: str | None) -> str:
    """Accent- and punctuation-insensitive key for matching one human.

    "A.J. Langer" / "A. J. Langer" are one person written two ways.
    """
    s = (s or "").translate(_FOLD)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def clean_name(name: str | None) -> str:
    """One spelling of a human's name, whatever the source escaped it as.

    TMDB serves `Lupita Nyong&apos;o` inside JSON; undecoded, the graph would grow a second node.
    """
    return re.sub(r"\s+", " ", fix_mojibake(unescape(name or ""))).strip()


# role classification (§3.1: only these roles become graph nodes)

# Exact job titles that make someone the PRINCIPAL holder of a role. Substring matching promoted
# 18,500 assistant directors to director. Values are `credit.role_class`, a closed vocabulary.
_JOB_MAP = {
    # directing
    "director": "director",
    "co-director": "director",
    "series director": "director",
    # writing
    "writer": "writer",
    "screenplay": "writer",
    "story": "writer",
    "author": "writer",
    "novel": "writer",
    "teleplay": "writer",
    "characters": "writer",
    "original story": "writer",
    "co-writer": "writer",
    "adaptation": "writer",
    "screenstory": "writer",
    # camera
    "director of photography": "dp",
    "cinematography": "dp",
    "cinematographer": "dp",
    "lighting camera": "dp",
    # music
    "original music composer": "composer",
    "composer": "composer",
    "music": "composer",
    "original score composer": "composer",
    # editing
    "editor": "editor",
    "film editor": "editor",
    "supervising editor": "editor",
    # art
    "production design": "prod_designer",
    "production designer": "prod_designer",
}

# Qualifiers that demote a job to support work even when the base title matches.
_SUPPORT = (
    "assistant", "asst", "second unit", "2nd unit", "trainee", "additional",
    "associate", "apprentice", "crowd", "aerial", "underwater", "still",
    "stills", "set photographer", "bts", "epk", "online", "offline",
    "digital intermediate", "dailies", "action director", "stage director",
    "casting", "script supervisor", "second second", "third", "temp",
    "main title", "end title", "theme", "consultant", "shadowing",
)

# §3.1's billed-cast cut.
CAST_BILLING_LIMIT = 6

# `credit.role_class`'s closed vocabulary; stored values outside it are classed via `class_of`.
ROLE_CLASSES = frozenset({*_JOB_MAP.values(), "cast"})


def classify_role(department: str | None, job: str | None, is_cast: bool = False) -> str | None:
    """The §3.1 role this job holds, or None for everything that is not one of the seven."""
    if is_cast:
        return "cast"
    j = (job or "").strip().lower()
    if not j:
        return None
    if any(q in j for q in _SUPPORT):
        return None
    return _JOB_MAP.get(j)


def class_of(role_class: str | None, department: str | None, job: str | None) -> str | None:
    """A stored credit's §3.1 class: the stored one when valid, else `classify_role` of the job, else
    None.
    """
    if role_class in ROLE_CLASSES:
        return role_class
    return classify_role(department, job)


def keep_credit(role_class: str | None, billing_order: int | None) -> bool:
    """§3.1: the crew roles above, plus the top 6 billed cast."""
    if role_class is None:
        return False
    if role_class == "cast":
        return billing_order is not None and billing_order < CAST_BILLING_LIMIT
    return True


# The corpus's person extras that this app's `person` table actually has.
_PERSON_EXTRAS = frozenset({"birth_year", "profile_path"})

_PERSON_BY_IMDB = "SELECT id, imdb_id, tmdb_id, birth_year, profile_path FROM person WHERE imdb_id = $1"
_PERSON_BY_TMDB = "SELECT id, imdb_id, tmdb_id, birth_year, profile_path FROM person WHERE tmdb_id = $1"
_PERSON_BY_ID = "SELECT id, imdb_id, tmdb_id, birth_year, profile_path FROM person WHERE id = $1"
_PERSON_BY_NAME = (
    "SELECT id, imdb_id, tmdb_id, birth_year, profile_path FROM person"
    " WHERE name = $1 AND imdb_id IS NULL AND tmdb_id IS NULL"
)

_CREDITED_ON = """
    SELECT DISTINCT p.id, p.name, (p.imdb_id IS NOT NULL OR p.tmdb_id IS NOT NULL) AS has_ids,
           c.role_class, c.department, c.job
      FROM credit c
      JOIN person p ON p.id = c.person_id
     WHERE c.title_id = $1
     ORDER BY p.id
"""

# (loose name, §3.1 class) -> the person credited on ONE title, or None when two id'd people collide.
Credited = dict[tuple[str, str | None], tuple[int | None, bool]]


def note_credited(
    credited: Credited, name: str | None, role_class: str | None, person_id: int, has_ids: bool
) -> None:
    """Record that `person_id` holds `role_class` on the title `credited` describes.

    An id'd person beats one without; two id'd people are ambiguous; ties go to the lower id.
    """
    key = (loose_name(name), role_class)
    if not key[0]:
        return
    held = credited.get(key)
    if held is None:
        credited[key] = (person_id, has_ids)
        return
    held_id, held_ids = held
    if held_id == person_id or held_id is None:
        return
    if has_ids and held_ids:
        credited[key] = (None, True)
    elif has_ids or (not held_ids and person_id < held_id):
        credited[key] = (person_id, has_ids)


async def credited_on(conn: asyncpg.Connection, title_id: int) -> Credited:
    """Who is already credited on this title, keyed for `upsert_person`'s same-title match."""
    credited: Credited = {}
    for row in await conn.fetch(_CREDITED_ON, title_id):
        note_credited(
            credited, row["name"], class_of(row["role_class"], row["department"], row["job"]),
            row["id"], row["has_ids"],
        )
    return credited


async def upsert_person(
    conn: asyncpg.Connection,
    *,
    name: str,
    imdb_id: str | None = None,
    tmdb_id: int | None = None,
    role_class: str | None = None,
    credited: Credited | None = None,
    **extra: Any,
) -> int:
    """Find this human's `person` row or create one, filling blanks and clobbering nothing.

    No `id` in the INSERT: the sequence default mints above `APP_ID_MIN`, asserted below. Lookup order:
    `imdb_id`, `tmdb_id`, then a name match among this title's credited people in the same class, then
    a name match among people with neither id.
    """
    name = clean_name(name) or "?"
    imdb_id = imdb_id if (imdb_id and re.fullmatch(r"nm\d{5,10}", str(imdb_id))) else None
    row = None
    if imdb_id:
        row = await conn.fetchrow(_PERSON_BY_IMDB, imdb_id)
    if row is None and tmdb_id:
        row = await conn.fetchrow(_PERSON_BY_TMDB, int(tmdb_id))
    if row is None and not imdb_id and not tmdb_id and credited is not None:
        same_title, _has_ids = credited.get((loose_name(name), role_class), (None, False))
        if same_title is not None:
            row = await conn.fetchrow(_PERSON_BY_ID, same_title)
    if row is None and not imdb_id and not tmdb_id:
        row = await conn.fetchrow(_PERSON_BY_NAME, name)

    fields = {k: v for k, v in extra.items() if k in _PERSON_EXTRAS and v not in (None, "")}

    if row is None:
        columns = ["name", *fields]
        values: list[Any] = [name, *fields.values()]
        if imdb_id:
            columns.append("imdb_id")
            values.append(imdb_id)
        if tmdb_id:
            columns.append("tmdb_id")
            values.append(int(tmdb_id))
        placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
        async with conn.transaction():
            minted = await conn.fetchval(
                f"INSERT INTO person ({', '.join(columns)}) VALUES ({placeholders}) RETURNING id",
                *values,
            )
            if int(minted) < APP_ID_MIN:
                raise RuntimeError(
                    f"person {minted} was minted below the app's id range ({APP_ID_MIN}): "
                    "person_id_seq has been repositioned or the column default is gone, and "
                    "spec 4.1's id partition is the only thing keeping an acquired person from "
                    "colliding with a corpus one (0015_seed.sql:20-25, decision 162)"
                    # ASCII in the message, not the section sign: it reaches a cp1252 console.
                )
        return int(minted)

    updates = {k: v for k, v in fields.items() if not row[k]}
    if imdb_id and not row["imdb_id"]:
        updates["imdb_id"] = imdb_id
    if tmdb_id and not row["tmdb_id"]:
        updates["tmdb_id"] = int(tmdb_id)
    if updates:
        assignments = ", ".join(f"{k} = ${i}" for i, k in enumerate(updates, start=2))
        # No `except IntegrityError` here: `person` has no UNIQUE on these ids, and a swallowed error
        # would poison the derive's transaction. Use a SAVEPOINT if a UNIQUE ever arrives.
        await conn.execute(
            f"UPDATE person SET {assignments} WHERE id = $1", row["id"], *updates.values()
        )
    return int(row["id"])


_KNOWN_PEOPLE = """
    SELECT p.name
      FROM credit c
      JOIN person p ON p.id = c.person_id
     WHERE c.title_id = $1 AND c.role_class IN ('director', 'cast')
"""


async def known_people(conn: asyncpg.Connection, title_id: int) -> set[str]:
    """This title's directors and billed cast as loose-match keys, for the scraped-page refusal.

    By `role_class`, never a free-text "Director" match. An empty set is a real answer.
    """
    rows = await conn.fetch(_KNOWN_PEOPLE, title_id)
    return {key for key in (loose_name(r["name"]) for r in rows) if key}
