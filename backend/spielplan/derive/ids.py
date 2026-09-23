"""Who a credit is about, which credits survive, and the loose key that recognises a scraped page.

Spec v2.1 §3.1 (the seven roles that become graph nodes), §8 stage 3, §4.1's id partition;
decisions 162, 372, 375.

PORT VERDICT: **ported with named changes** from `mdc/ids.py` (347 lines). Taken: `loose_name`
(`:65-76`) with its `_FOLD` table (`:49-62`), `clean_name` (`:200-209`), `upsert_person`
(`:213-253`), `classify_role` (`:318-327`) with `_JOB_MAP` (`:264-305`) and `_SUPPORT`
(`:309-316`), and `keep_credit` (`:330-335`). Not taken, because they are the corpus's title
spine rather than this app's: `norm_title`, `slugify`, `valid_imdb`, `find_title`, `upsert_title`,
`ID_FIELDS`, `META_FIELDS` and `chunks`. §8 stage 1 already mints this app's titles
(`acquire/stages._mint`) under decision 162's partition, and a second title upsert here would be
a second answer to "which title is this", which `connectors/resolve.py:15-17` settles once.

THE FOUR NAMED CHANGES.

  1. **sqlite3 -> asyncpg.** `conn.execute(sql, (a, b))` becomes `await conn.fetchrow(sql, a, b)`
     and `cur.lastrowid` becomes `RETURNING id`, which is the only way to learn a Postgres
     sequence's value.
  2. **`upsert_person` asserts the minted id, as `_mint` does for a title.** The corpus has one
     id space and needs no such rule; this app has two (`0015_seed.sql:20-23`: "A disjoint range
     makes the collision arithmetically impossible instead of contingent on the corpus standing
     still"). `person_id_seq` is `MINVALUE 1000000000` (`0015_seed.sql:25`), so a person minted
     here lands above every person the bundle shipped -- unless a sequence someone reset by hand
     or a column default someone dropped in a repair puts it below, in which case the next
     models-only import overwrites a bundle person with a household one and nothing says so. The
     assertion is what makes that loud. It fires ONLY on an INSERT: a bundle person legitimately
     has an id below the floor, and matching one is the whole point of the lookup above it.
  3. **`gender` is not written, because `person` has no such column.**
     `0003_content.sql:135-142` carries `(id, name, imdb_id, tmdb_id, birth_year, profile_path)`
     and the corpus carries `death_year`, `gender` and `known_for` besides. So the corpus's
     `_gender` map (`mdc/parse/titles.py:191-193`) is not ported either: a parser that produced a
     field with no target would read as data this app keeps and does not. A later migration that
     adds the column is where that map belongs, and TMDB's `gender` code is still in the raw
     store to re-parse it out of (`spec:398`).
  4. **`known_people` moves here from `mdc/sources/metacritic.py:46-51`.** The corpus files it
     with the source that fetches the page, because there the refusal happens at fetch time; here
     the refusal happens at PARSE time -- the coverage row says "refused rather than parsed" --
     and `derive/parse.page_belongs_to_title` is what needs it. It is the corpus's own query,
     scoped to one title, and it belongs beside `loose_name`, which is the key it is built from:
     two implementations of "is this the same human" is how a page starts being refused for one
     source and accepted for the other.

WHY `loose_name` IS NOT `norm_title` AND NOT `lower()`. The corpus measured it: "Andrzej Sekula"
loses its `l` to an `[^a-z0-9]` filter after NFKD leaves the Polish `l-stroke` intact, because
that letter has no decomposition, and the two spellings then never compare equal. The `_FOLD`
table is that measurement. It matters here for exactly one thing -- whether a scraped page's
billed cast overlaps this title's -- and a fold that splits one human into two turns a page that
IS about this film into a page refused for having a disjoint cast.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import asyncpg

from spielplan.sources._htmlutil import fix_mojibake, unescape

# `0015_seed.sql:24-25`, and the same constant `acquire/stages.py:125` and
# `importer/bundle.py:71` already spell for themselves. A third local copy rather than an import:
# `derive/` must not depend on `acquire/`, which is the layer that drives it, and `importer/`
# holds it as a validation floor for a bundle rather than as a mint rule. The number is the
# sequence's own MINVALUE and a migration is the only thing that can move it.
APP_ID_MIN = 1_000_000_000

# Latin letters with no NFKD decomposition. They are base characters, not accented ones, so
# stripping combining marks leaves them intact and the `[^a-z0-9]` filter then DELETES them
# outright: "Andrzej Sekula" became "andrzejseku" and never matched "Andrzej Sekula". Folding
# them keeps Polish and Turkish names from splitting into two people.
_FOLD = str.maketrans({
    "\u0142": "l", "\u0141": "l", "\u0131": "i", "\u0130": "i",
    "\u00f8": "o", "\u00d8": "o", "\u0111": "d", "\u0110": "d",
    "\u00f0": "d", "\u00d0": "d", "\u0127": "h", "\u0167": "t",
    "\u00fe": "th", "\u00de": "th", "\u00e6": "ae", "\u00c6": "ae",
    "\u0153": "oe", "\u0152": "oe", "\u00df": "ss",
})


def loose_name(s: str | None) -> str:
    """Accent- and punctuation-insensitive key for matching one human.

    "Micheal MacLiammoir" / "Micheal Mac Liammoir" and "A.J. Langer" / "A. J. Langer" are one
    person written two ways; this is what makes them compare equal.
    """
    s = (s or "").translate(_FOLD)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def clean_name(name: str | None) -> str:
    """One spelling of a human's name, whatever the source escaped it as.

    TMDB serves `Lupita Nyong&apos;o` and `Gladys Knight &amp; The Pips` inside JSON, where
    nothing else decodes entities - so without this the graph grows a second, mangled node for
    anyone with an apostrophe in their name, and it never merges with the real one.
    """
    return re.sub(r"\s+", " ", fix_mojibake(unescape(name or ""))).strip()


# ---------------------------------------------------------------------------
# role classification (§3.1: only these roles become graph nodes)
# ---------------------------------------------------------------------------

# Exact job titles that make someone the PRINCIPAL holder of a role.
#
# Matching on "does the job contain 'director'" is what this replaces, and it was badly wrong: it
# promoted 18,500 assistant and second-unit directors to director, 3,312 still photographers to
# cinematographer, and 4,001 assistant editors to editor. Those people then carry the same weight
# in the graph as the actual author of the film's look - §3.1 exists precisely to keep the second
# unit gaffer out. The vocabulary is also `credit.role_class`, which `0015_seed.sql:50-56` names
# as what "the feature contract's `p:<role_class>:<name>` grammar is built from", so a value
# invented here is a coordinate the Cold Tower was never trained on.
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

# Qualifiers that demote a job to support work even when the base title matches - "Second Unit
# Director of Photography" is not the film's DP.
_SUPPORT = (
    "assistant", "asst", "second unit", "2nd unit", "trainee", "additional",
    "associate", "apprentice", "crowd", "aerial", "underwater", "still",
    "stills", "set photographer", "bts", "epk", "online", "offline",
    "digital intermediate", "dailies", "action director", "stage director",
    "casting", "script supervisor", "second second", "third", "temp",
    "main title", "end title", "theme", "consultant", "shadowing",
)

# §3.1's billed-cast cut, spelled once. `keep_credit` is called from six parsers and the number
# is the same rule in all six.
CAST_BILLING_LIMIT = 6


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


def keep_credit(role_class: str | None, billing_order: int | None) -> bool:
    """§3.1: the crew roles above, plus the top 6 billed cast."""
    if role_class is None:
        return False
    if role_class == "cast":
        return billing_order is not None and billing_order < CAST_BILLING_LIMIT
    return True


# ---------------------------------------------------------------------------
# the two reads that need a connection
# ---------------------------------------------------------------------------

# The corpus's extras, intersected with the columns this app's `person` actually has
# (`0003_content.sql:135-142`). Named rather than inlined so change note 3 in the header has
# something to point at: the day a migration adds `gender`, this set is where it arrives.
_PERSON_EXTRAS = frozenset({"birth_year", "profile_path"})

_PERSON_BY_IMDB = "SELECT id, imdb_id, tmdb_id, birth_year, profile_path FROM person WHERE imdb_id = $1"
_PERSON_BY_TMDB = "SELECT id, imdb_id, tmdb_id, birth_year, profile_path FROM person WHERE tmdb_id = $1"
_PERSON_BY_NAME = (
    "SELECT id, imdb_id, tmdb_id, birth_year, profile_path FROM person"
    " WHERE name = $1 AND imdb_id IS NULL AND tmdb_id IS NULL"
)


async def upsert_person(
    conn: asyncpg.Connection,
    *,
    name: str,
    imdb_id: str | None = None,
    tmdb_id: int | None = None,
    **extra: Any,
) -> int:
    """Find this human's `person` row or create one, filling blanks and clobbering nothing.

    NO `id` IN THE COLUMN LIST, for `acquire/stages._mint`'s reason and under the same decision:
    `0015_seed.sql:28` sets `nextval('person_id_seq')` as the column default and the sequence is
    `MINVALUE 1000000000`, so the row lands in this app's half of the id space without this code
    knowing a number. The assertion below is the backstop, and it is worth more here than for a
    title: a title the bundle also ships collides visibly at import (`importer/bundle.py:539`
    refuses an id in the app's range), while a PERSON silently acquires the credits of whoever
    held that id in the corpus, and `credit.person_id` is a foreign key that will happily point
    at them. Inside a transaction, so a refusal leaves no row (decision 162). When the derive
    already holds one this is a savepoint, which is what makes the refusal local to the person.

    FILL, NEVER CLOBBER, which is decision 372's rule for identity one table over: an existing
    value is kept and only a NULL is filled. A source that is wrong about an imdb id must not be
    able to repoint a person the bundle curated, and the corpus's own upsert is written the same
    way ("only FILLS IN missing fields, so a low-quality seed source can never clobber good data
    from TMDB", `mdc/ids.py:115-116`).

    The lookup order is the corpus's and is not an accident: `imdb_id`, then `tmdb_id`, then name
    among the people who carry neither. Falling back to a name match for a person who DOES carry
    an id would merge two humans who share a name, which is the failure `loose_name` exists to
    avoid on the other side.
    """
    name = clean_name(name) or "?"
    imdb_id = imdb_id if (imdb_id and re.fullmatch(r"nm\d{5,10}", str(imdb_id))) else None
    row = None
    if imdb_id:
        row = await conn.fetchrow(_PERSON_BY_IMDB, imdb_id)
    if row is None and tmdb_id:
        row = await conn.fetchrow(_PERSON_BY_TMDB, int(tmdb_id))
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
                    # "spec 4.1" and not the section mark: this string reaches a console, and
                    # CLAUDE.md's ASCII rule is about a Windows cp1252 terminal crashing on the
                    # glyph rather than about taste. Every comment in this file still cites the
                    # clause the house way; a message a person reads under a failure does not.
                )
        return int(minted)

    updates = {k: v for k, v in fields.items() if not row[k]}
    if imdb_id and not row["imdb_id"]:
        updates["imdb_id"] = imdb_id
    if tmdb_id and not row["tmdb_id"]:
        updates["tmdb_id"] = int(tmdb_id)
    if updates:
        assignments = ", ".join(f"{k} = ${i}" for i, k in enumerate(updates, start=2))
        # The corpus wraps this UPDATE in `except sqlite3.IntegrityError: pass`
        # (`mdc/ids.py:249-251`) because its own schema declares `person.imdb_id` and
        # `person.tmdb_id` UNIQUE and two sources can disagree about which human an id belongs
        # to. This app's `person` declares neither (`0003_content.sql:135-143`: one PRIMARY KEY
        # and one plain index on `lower(name)`), so there is no violation to swallow -- and a
        # swallow here would be worse than absent. Postgres poisons a transaction on any error,
        # so under decision 375's one-transaction derive an `except ... : pass` would leave every
        # later statement failing with "current transaction is aborted" and the derive reporting
        # the wrong cause. If a UNIQUE ever arrives on those columns, the repair is a SAVEPOINT
        # around this statement, not a bare except.
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

    `mdc/sources/metacritic.py:46-51` and `mdc/parse/rebuild.py:559-565` build the same set two
    ways for the same purpose; this is the per-title one. `role_class` rather than `job` because
    the vocabulary is closed (`0015_seed.sql:50-53`) and a free-text match on "Director" would
    pick up "Assistant Director" -- someone who worked on this film, credited on neither page.

    An empty set is a real answer and not a failure: a title acquired minutes ago has no credits
    yet, and `page_belongs_to_title` is written for that case -- with nothing of ours to compare,
    the page's year decides, and absence of evidence is not evidence.
    """
    rows = await conn.fetch(_KNOWN_PEOPLE, title_id)
    return {key for key in (loose_name(r["name"]) for r in rows) if key}
