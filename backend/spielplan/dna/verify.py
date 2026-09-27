"""Verification for extracted DNA: the trust boundary (§8 stage 7, §9).

Term in vocabulary (after alias repair and ledger rename), quote a substring of that title's pack
via `norm()`, salience in {1,2,3}. Failures drop, never repaired; imports no provider client.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from spielplan.acquire import rawstore
from spielplan.db.dna_terms import active_version
from spielplan.dna import adjudicate
from spielplan.dna.aliases import alias_key, load_alias_map
from spielplan.dna.norm import norm
from spielplan.dna.packs import sha as pack_sha

REASONS = ("schema", "unknown_term", "adjudicated", "quote_unverified", "unknown_title",
           "no_pack", "duplicate")

# Refused, not clamped (decision 386): a clamp is a repair. Out-of-domain values fall under `schema`.
SALIENCE_LEVELS = (1, 2, 3)

# A tag stating no level claims nothing; the middle of the scale invents nothing.
DEFAULT_SALIENCE = 2

# `dna_reject.salience` is a smallint; a wider value is dropped so one bad number cannot fail the batch.
_SMALLINT = 32767


@dataclass
class Rejection:
    """One tag the boundary refused, and the rule it broke.

    `detail` reaches no column; `rule_violated` is the closed set the install keeps.
    """

    title_id: int
    term: str | None
    reason: str
    detail: str = ""
    facet: str | None = None
    salience: int | None = None
    quote: str | None = None


@dataclass
class VerifiedTag:
    """One tag that passed all three checks, with the evidence it passed on."""

    term: str
    facet: str
    salience: int
    source: str
    quote: str
    repaired: bool = False


@dataclass
class PassResult:
    """One extraction pass over one title, after verification. `n_seen` counts the tags examined."""

    tags: list[VerifiedTag] = field(default_factory=list)
    rejects: list[Rejection] = field(default_factory=list)
    n_seen: int = 0

    @property
    def n_kept(self) -> int:
        return len(self.tags)


def _stated(tag: Mapping[str, Any], key: str) -> Any:
    """What the tag states for `key`; an empty string states nothing."""
    value = tag.get(key)
    return None if value == "" else value


# the vocabulary, as the boundary needs to see it


@dataclass(frozen=True)
class Vocabulary:
    """One version's terms, facets and alias map, resolved once for a whole pass.

    `by_tail` indexes term bodies for the prefix repair; `facet_keys` is `facets` case-folded.
    """

    version: str
    terms: Mapping[str, str]
    facets: frozenset[str]
    aliases: Mapping[str, tuple[str, str]]
    by_tail: Mapping[str, tuple[str, ...]]
    facet_keys: frozenset[str]

    def __contains__(self, term_id: object) -> bool:
        return term_id in self.terms

    @classmethod
    def build(
        cls,
        version: str,
        terms: Mapping[str, str],
        facets: Iterable[str],
        aliases: Mapping[str, tuple[str, str]] | None = None,
    ) -> Vocabulary:
        """The type, with `by_tail` derived rather than supplied, so the uniqueness rule lives in one
        place.
        """
        by_tail: dict[str, list[str]] = {}
        for term_id in terms:
            tail = term_id.partition(".")[2]
            if tail:
                by_tail.setdefault(tail, []).append(term_id)
        declared = frozenset(facets)
        return cls(
            version=version,
            terms=dict(terms),
            facets=declared,
            aliases=dict(aliases or {}),
            by_tail={tail: tuple(sorted(ids)) for tail, ids in by_tail.items()},
            facet_keys=frozenset(facet.casefold() for facet in declared),
        )

    def alias_of(self, term_id: str) -> str | None:
        """The term an AUTHORED alias row gives this spelling, or None.

        Runs before the prefix repair: an authored row is a decision, the repair a guess.
        """
        mapped = self.aliases.get(alias_key(term_id))
        return mapped[1] if mapped is not None else None

    def repair(self, term_id: str) -> str | None:
        """Map a near-miss term id onto a real one, or return None.

        Only ever rewrites the prefix, never the body, and refuses when the body is not unique or when the
        head is a declared facet (case-folded) that does not carry it.
        """
        term_id = (term_id or "").strip()
        if not term_id:
            return None
        if term_id in self.terms:
            return term_id
        if "." not in term_id:
            return None
        head, tail = term_id.split(".", 1)
        candidates = self.by_tail.get(tail, ())
        if len(candidates) != 1:
            return None
        named = head.strip().casefold()
        if named in self.facet_keys and self.terms[candidates[0]].casefold() != named:
            return None
        return candidates[0]

    def resolve(self, term_id: str) -> str | None:
        """The vocabulary term this spelling names, or None: exact, then authored alias, then prefix
        repair.

        Checks the alias target is a real term: `dna_alias` has no FK to `dna_term`.
        """
        cleaned = (term_id or "").strip()
        if cleaned in self.terms:
            return cleaned
        authored = self.alias_of(cleaned)
        if authored is not None and authored in self.terms:
            return authored
        return self.repair(cleaned)


async def load_vocabulary(
    conn: asyncpg.Connection, version: str | None = None
) -> Vocabulary | None:
    """One version's vocabulary, or None when this install has no active one.

    Callers must not run stage 7 on None: it would reject everything as `unknown_term`.
    """
    if version is None:
        version = await active_version(conn)
    if version is None:
        return None

    facet_rows = await conn.fetch("SELECT facet FROM dna_facet WHERE version = $1", version)
    term_rows = await conn.fetch("SELECT term, facet FROM dna_term WHERE version = $1", version)

    return Vocabulary.build(
        version,
        {row["term"]: row["facet"] for row in term_rows},
        (row["facet"] for row in facet_rows),
        await load_alias_map(conn, version),
    )


# the pack a verdict is reached against

_PACK_ROW = "SELECT pack_sha, raw_document_id FROM dna_pack WHERE title_id = $1 AND version = $2"


async def read_pack(conn: asyncpg.Connection, title_id: int, version: str) -> str | None:
    """The text this title's tags must quote from, or None when it has no pack. Decision 382.

    A sha mismatch raises rather than answering None: verifying against the wrong text is worse.
    """
    row = await conn.fetchrow(_PACK_ROW, title_id, version)
    if row is None:
        return None
    text = (await rawstore.read(conn, row["raw_document_id"])).decode("utf-8")
    if pack_sha(text) != row["pack_sha"]:
        raise OSError(
            f"dna_pack row for title {title_id} under {version} names pack_sha "
            f"{row['pack_sha']} and raw_document {row['raw_document_id']} holds a pack whose sha "
            f"is {pack_sha(text)}: a quote verified against this text would be checked against "
            "the wrong evidence (spec section 8 stage 7)"
        )
    return text


# verification


async def verify_tags(
    title_id: int, tags: Any, *, pack: str, voc: Vocabulary, ledger: asyncpg.Connection
) -> PassResult:
    """Check one title's extracted tags against the vocabulary and that title's pack.

    `tags` is the provider's untrusted list; `pack` is unfolded text, folded here. The ledger is asked
    only about terms the vocabulary lacks.
    """
    res = PassResult()
    if not isinstance(tags, list):
        res.rejects.append(Rejection(title_id, None, "schema", f"tags are {type(tags).__name__}"))
        return res

    folded_pack = norm(pack)
    seen: set[str] = set()
    for tg in tags:
        res.n_seen += 1
        if not isinstance(tg, dict):
            res.rejects.append(Rejection(title_id, None, "schema", f"tag is {type(tg).__name__}"))
            continue
        raw_term = _stated(tg, "term")
        quote = _stated(tg, "quote")
        # Read before the other arms so every rejection records the stated level (decision 400).
        # `OverflowError` too: `int(inf)` raises it, and a raise would lose the whole pass (decision
        # 392).
        stated = _stated(tg, "salience")
        try:
            level: int | float | None = DEFAULT_SALIENCE if stated is None else _level(stated)
        except (TypeError, ValueError, OverflowError):
            level = None
        if raw_term is None or quote is None:
            res.rejects.append(Rejection(
                title_id, str(raw_term) if raw_term else None,
                "schema",
                f"missing {'term' if raw_term is None else 'quote'}; "
                f"keys were {sorted(tg)}",
                salience=_storable(level),
                quote=str(quote) if quote is not None else None))
            continue
        # Decision 399: a non-string quote is a missing quote, refused under `schema`.
        if not isinstance(quote, str):
            res.rejects.append(Rejection(
                title_id, str(raw_term), "schema",
                f"quote {str(quote)[:40]!r} is {type(quote).__name__} and not text",
                salience=_storable(level), quote=str(quote)))
            continue
        # Same for the term: `str()` of a list would otherwise alias-repair into a real term.
        if not isinstance(raw_term, str):
            res.rejects.append(Rejection(
                title_id, str(raw_term), "schema",
                f"term {str(raw_term)[:40]!r} is {type(raw_term).__name__} and not text",
                salience=_storable(level), quote=quote))
            continue
        # Decision 397: a NUL or lone surrogate would raise from the ledger query and lose the pass.
        term_text = _storable_text(raw_term)
        quote_text = _storable_text(quote)
        if term_text is None or quote_text is None:
            res.rejects.append(Rejection(
                title_id, term_text, "schema",
                f"the {'term' if term_text is None else 'quote'} carries text Postgres "
                "cannot store: a NUL or an unpaired surrogate",
                salience=_storable(level), quote=quote_text))
            continue
        # Decision 392: a quote that folds to "" matches every pack, so it is refused as missing.
        folded = norm(quote)
        if not folded:
            res.rejects.append(Rejection(
                title_id, raw_term, "schema",
                f"quote {quote[:40]!r} folds to nothing under norm()",
                salience=_storable(level), quote=quote))
            continue

        # Stripped once, so the vocabulary and the ledger read the same spelling.
        offered = raw_term.strip()
        term = voc.resolve(offered)
        if term is None:
            # A re-pointed id is a renamed term, not an unknown one; repairing it lets result files
            # outlive a merge.
            term = await adjudicate.rename(ledger, offered, title_id, version=voc.version)
            if term is None or term not in voc:
                # A dropped term is a curated retirement, not extractor garbage.
                retired = await adjudicate.is_retired(ledger, offered, title_id, version=voc.version)
                res.rejects.append(Rejection(
                    title_id, raw_term,
                    "adjudicated" if retired else "unknown_term",
                    "retired by the curation ledger" if retired
                    else "not in vocabulary",
                    salience=_storable(level), quote=quote))
                continue
        facet = voc.terms[term]
        if folded not in folded_pack:
            res.rejects.append(Rejection(
                title_id, term, "quote_unverified",
                f"{quote[:80]!r} is not in this title's pack",
                facet=facet, salience=_storable(level), quote=quote))
            continue
        if term in seen:
            res.rejects.append(Rejection(title_id, term, "duplicate",
                                         "term emitted twice for this title",
                                         facet=facet, salience=_storable(level), quote=quote))
            continue

        # Reported here, after the term and quote were judged, so rule precedence is unchanged.
        if level is None:
            res.rejects.append(Rejection(
                title_id, term, "schema",
                f"stated level {str(stated)[:40]!r} is not a number",
                facet=facet, quote=quote))
            continue
        # Decision 386. Bound to `level`, not `salience`, because `test_landmine_guards.py` flags any
        # comparison on the word `salience`; this is a domain check on one value and selects no rows.
        if level not in SALIENCE_LEVELS:
            res.rejects.append(Rejection(
                title_id, term, "schema",
                f"stated level {level} is outside the declared domain {SALIENCE_LEVELS}",
                facet=facet, salience=_storable(level), quote=quote))
            continue

        seen.add(term)
        res.tags.append(VerifiedTag(
            term=term, facet=facet,
            salience=level,
            source=str(_stated(tg, "source") or ""),
            quote=quote, repaired=(term != raw_term.strip())))
    return res


def _level(stated: Any) -> int | float:
    """The salience a payload states, read and never rounded. Decisions 386, 392 and 394.

    Integral spellings come back as `int`; anything else is returned as stated and refused by the
    domain check. Booleans and non-finite values raise.
    """
    if isinstance(stated, bool):
        raise TypeError("a boolean states no level")
    number = float(stated)
    if not math.isfinite(number):
        raise ValueError("a non-finite level is not a level")
    return int(number) if number.is_integer() else number


def _storable(level: int | float | None) -> int | None:
    """The stated level as `dna_reject.salience` can hold it, or None. See `_SMALLINT`.

    Non-integral and boolean values are None: asyncpg would silently truncate or write a 1.
    """
    if isinstance(level, int) and not isinstance(level, bool):
        return level if -_SMALLINT - 1 <= level <= _SMALLINT else None
    return None


def _storable_text(s: str | None) -> str | None:
    """One untrusted string as a Postgres `text` column can hold it, or None. Decision 397.

    NUL and unpaired surrogates cannot be stored; None, never a cleaned string.
    """
    if not isinstance(s, str):
        return None
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return None if "\x00" in s else s


# what was refused, written down

_RECORD_REJECT = """
    INSERT INTO dna_reject
      (title_id, run_id, term, facet, salience, quote, rule_violated, provider)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""


async def record_rejects(
    conn: asyncpg.Connection,
    rejects: Iterable[Rejection],
    *,
    run_id: int | None = None,
    provider: str | None = None,
) -> int:
    """Write a pass's refusals to `dna_reject`. Returns how many rows landed. Decision 341.

    Every untrusted value is guarded at the bind, because `executemany` is atomic and one bad value
    would discard the pass's refusals.
    """
    rows = [
        (r.title_id, run_id, _storable_text(r.term), r.facet, _storable(r.salience),
         _storable_text(r.quote), r.reason, provider)
        for r in rejects
    ]
    if not rows:
        return 0
    await conn.executemany(_RECORD_REJECT, rows)
    return len(rows)


__all__ = [
    "DEFAULT_SALIENCE",
    "REASONS",
    "SALIENCE_LEVELS",
    "PassResult",
    "Rejection",
    "VerifiedTag",
    "Vocabulary",
    "load_vocabulary",
    "read_pack",
    "record_rejects",
    "verify_tags",
]
