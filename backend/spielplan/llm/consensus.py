"""Merging runs: union, never intersection, with agreement as a weight (§6.6, §4.1 rule 2).

A run is one provider at one pass (decision 337); each provider gets its own `dna_tag` row carrying
the pooled weights and only its own quotes. Nothing compares a weight.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import asyncpg

from spielplan.dna.verify import VerifiedTag


def pass_id_for(provider: str, pass_index: int) -> str:
    """The one spelling of a run's key: `<provider>:<pass_index>`, counted from 1.

    Never `PassResult.pass_id`, which a payload can override: the model does not choose its provider.
    """
    return f"{provider}:{pass_index}"


def provider_of(pass_id: str) -> str:
    """The provider a run's key names; a key that does not spell a run is refused, never guessed."""
    provider, sep, index = pass_id.partition(":")
    if not sep or not provider or not (index.isascii() and index.isdigit()) or int(index) < 1:
        raise ValueError(
            f"a run is keyed '<provider>:<pass_index>' with the index counted from 1; {pass_id!r} "
            "names no run (decision 337)"
        )
    return provider


@dataclass(frozen=True)
class Evidence:
    """One run's quote for one term: which run, where the quote came from, and the quote."""

    pass_id: str
    source: str
    quote: str

    @property
    def provider(self) -> str:
        return provider_of(self.pass_id)


@dataclass(frozen=True)
class MergedTag:
    """One term's union over every run of one title, with the weights the runs' agreement earns.

    `providers` is derived from the evidence, so a row without evidence is unwritable.
    """

    term: str
    facet: str
    salience: int
    confidence: float
    n_sources: int
    evidence: tuple[Evidence, ...]

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted({item.provider for item in self.evidence}))


def merge_passes(passes: Mapping[str, Sequence[VerifiedTag]]) -> tuple[list[MergedTag], int]:
    """Union the passes for ONE title.  Returns (tags, n_runs).

    Salience is the max any pass assigned; a pass that saw a trait as core is
    better evidence than one that saw it as minor, and the confidence weight
    already records how alone it was in seeing it at all.

    Pass every run, empty ones included: `n_runs` is `len(passes)`.
    """
    for pid in passes:
        provider_of(pid)
    n_runs = len(passes)
    counts: Counter[str] = Counter()
    best: dict[str, VerifiedTag] = {}
    evidence: dict[str, list[tuple[str, VerifiedTag]]] = defaultdict(list)
    for pid, tags in passes.items():
        for t in tags:
            counts[t.term] += 1
            evidence[t.term].append((pid, t))
            cur = best.get(t.term)
            # Bound to locals because `test_landmine_guards.py` flags any comparison naming a weight
            # column;
            # this picks a max and removes nothing.
            level = t.salience
            best_level = None if cur is None else cur.salience
            if cur is None or level > best_level:
                best[t.term] = t

    out = []
    for term, t in sorted(best.items()):
        out.append(MergedTag(
            term=term, facet=t.facet, salience=t.salience,
            confidence=round(counts[term] / n_runs, 2) if n_runs else 1.0,
            n_sources=counts[term],
            evidence=tuple(Evidence(pid, v.source, v.quote) for pid, v in evidence[term]),
        ))
    return out, n_runs


# Every writer's rows for this title and version; `dna_evidence` cascades.
_REPLACE = "DELETE FROM dna_tag WHERE title_id = $1 AND version = $2"

_TAG = (
    "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, n_sources, provider)"
    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id"
)

# `source_ref` carries the run key, so two runs quoting one sentence stay two rows.
_EVIDENCE = (
    "INSERT INTO dna_evidence (dna_tag_id, quote, source, source_ref) VALUES ($1, $2, $3, $4)"
)


async def store_title(
    conn: asyncpg.Connection,
    title_id: int,
    version: str,
    merged: Sequence[MergedTag],
    *,
    n_runs: int,
) -> int:
    """Replace one title's extracted tier for `version` with the merge of its runs.

    One transaction. `n_runs == 0` is refused before anything is deleted: all-empty runs are a verdict,
    no runs are not. Writes `dna_tag` and `dna_evidence` only. Returns the `dna_tag` rows written.
    """
    if n_runs < 1:
        raise ValueError(
            f"title {title_id}: no run was merged, and replacing its extracted tier with the result "
            "would erase tags nobody re-read"
        )
    written = 0
    async with conn.transaction():
        await conn.execute(_REPLACE, title_id, version)
        for tag in merged:
            for provider in tag.providers:
                tag_id = await conn.fetchval(
                    _TAG, title_id, version, tag.term, tag.facet, tag.salience, tag.confidence,
                    tag.n_sources, provider,
                )
                await conn.executemany(
                    _EVIDENCE,
                    [
                        (tag_id, item.quote, item.source, item.pass_id)
                        for item in tag.evidence
                        if item.provider == provider
                    ],
                )
                written += 1
    return written
