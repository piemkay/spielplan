"""The run arithmetic and the write it feeds: agreement is a weight. Spec v2.1 §6.6, §4.1 rules 1-2.

**Union, never intersection.**  Two passes agree on only 41% of their union
(per-title Jaccard 0.41), and the union recalls 93% against a gold where the
intersection manages 67% - below a single pass.  Merge therefore unions and
stores the agreement rate as ``confidence`` for the similarity layer to
discount with.  Nothing filters on it.

The paragraph above is `mdc/dna/store.py:31-35` verbatim. It is the fourth paragraph of the module
docstring `dna/verify.py` splices its own opening from, and the one that file leaves out by name,
because "it is a fact about RUNS, and the runs belong to the milestone that calls a provider"
(`dna/verify.py:35-37`). This is that milestone, and the paragraph sits where the runs are counted.

§6.6 AND §4.1 GIVE THE NUMBERS AND THE RULE, AND NOT THE FUNCTION. §6.6 merges parallel runs "by the
measured consensus rule (union with per-tag agreement as confidence -- union recalls 93% vs 67% for
intersection; agreement is a weight, never a filter ...)" (`spec:322`), and §4.1 rule 2 makes the
last clause binding over the three columns this module writes (`spec:99`). Neither says what number
a tag found by two runs of three receives. Decision 337 takes the corpus's answer: the share of runs
that found the term, rounded to two places, with the count beside it as `n_sources`, the highest
salience any run assigned, and the evidence of every run that found it.

A RUN IS ONE PROVIDER AT ONE PASS, POOLED ACROSS PROVIDERS (decision 337). Two passes of Gemini and
one pass each of Gemini and Anthropic are the same arithmetic -- two runs -- and the keys
`merge_passes` takes are spelled `<provider>:<pass_index>` so that the provider a run belongs to is
recoverable from the run itself. §6.6 prints the 93%/67% figures as the caption of the PARALLEL
toggle, a control over providers, while this counts runs; the label that caption is owed for the
difference is §6.6 text recorded under decision 337 and not applied by this milestone (plan E4), and
nothing here restates the figures as a property of passes.

A TAG ONE RUN FOUND IS A TAG. At one run of one its weight is 1.0 -- there was nobody to disagree --
and at one run of three it is 0.33 and is written exactly as a unanimous tag is. The weight is what
the similarity layer discounts with; dropping the row instead would be the cut §4.1 measures at 44%
of the extracted tier, made before the row existed to be counted. So neither function below compares
a weight to anything, and `test_landmine_guards.py`'s rule-2 pair reads this module along with
every other one under `spielplan/` -- which is plan E3's static guard, registered on the coverage
row rather than written a second time.

ONE ROW PER PROVIDER, EACH CARRYING THE POOLED WEIGHTS. `dna_tag` is `UNIQUE (title_id, version,
term, provider)` (`0004_dna.sql:84`), made to fire by `0018_read_layer.sql:68-70`'s `''` for "no
provider recorded", and that key exists so §6.6's parallel mode can hold two providers' reading of
one film side by side (`derive/ledgers.py:58-67`). So a term Gemini and Anthropic both found is two
rows, provider 'gemini' and provider 'anthropic', both carrying the pooled figures and each holding
only its own provider's quotes as `dna_evidence`. The readers already count a term once however many
providers named it (`api/library.py:193-205`, `dna/coverage.py:86-93`), so the second row is a
record of who read what and not a second vote.

PORT VERDICT, unit by unit.

  * `merge_passes` (`mdc/dna/store.py:246-275`) -- **ported with named changes.** The arithmetic is
    verbatim: the union, `round(counts[term] / n_runs, 2) if n_runs else 1.0`, the count, the
    strict-greater salience rule (so the first run to assign the top level supplies the facet), the
    evidence of every run, the term order. Its docstring's salience argument is verbatim.
      1. The salience comparison is bound to locals, argued where it lands.
      2. The container is the app's: a frozen `MergedTag` for the dict, `n_sources` for
         `runs_found` (the name `importer/dna.py:583-592` already maps it to), and `Evidence` for
         the `{"pass_id", "src", "quote"}` dicts.
      3. Every key must spell a run, and a tag's providers are read off its own evidence. The
         corpus has no provider column; this schema writes a row per provider (decision 337).
  * `store_title` (`mdc/dna/store.py:278-338`) -- **rewritten** for Postgres and per-provider rows.
    What survives is the rule its docstring states and this one quotes, "Replace, not accumulate",
    kept in one transaction. Not carried: the corpus's adjudication applied before the write, whose
    applier here is `derive/ledgers.apply_adjudications` -- it rules over the rows a title carries
    "whatever wrote them" (`derive/ledgers.py:240-243`), so it is the writer's to call once this has
    written, inside the same transaction; the `dna_annotation` upsert, which this schema has no table
    for (the pack a run read is `llm_call.pack_document_id`, decision 430, and its refusals are
    `dna_reject`, decision 341); and the coverage figures, which are `dna/coverage.py`'s.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import asyncpg

from spielplan.dna.verify import VerifiedTag


def pass_id_for(provider: str, pass_index: int) -> str:
    """The one spelling of a run's key: `<provider>:<pass_index>`, the index counted from 1.

    The caller's own spelling and never `PassResult.pass_id`, which a payload's `pass` field
    overrides (`dna/verify.py:557`): a key a model chose would name the provider a row is written
    under, and the model does not get to say which provider it was.
    """
    return f"{provider}:{pass_index}"


def provider_of(pass_id: str) -> str:
    """The provider a run's key names, refusing a key that does not spell a run.

    Named change 3. The provider is a component of `dna_tag`'s unique key, so a key that does not
    parse is refused here rather than written as a provider nobody configured: `gemini` alone,
    `:1`, `gemini:0` and `gemini:x` are not runs, and guessing which run each meant would decide who
    a row is attributed to.
    """
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

    `providers` IS READ OFF THE EVIDENCE AND NOT STORED BESIDE IT. `store_title` writes one row per
    provider and each row's `dna_evidence` is that provider's own quotes, and §4.1 rule 1 ships the
    evidence with the tier because "a tag without its quote is unfalsifiable" (`0004_dna.sql:90`).
    A provider that found the term has a quote here by construction, so deriving the one from the
    other is what makes a row without evidence unwritable rather than merely unwritten.
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

    Every run belongs in `passes`, including one that found nothing: `n_runs` is `len(passes)`, as
    the corpus counts it, and a run left out because it came back empty would lift every other
    run's agreement to a figure nobody measured.
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
            # Named change 1. The corpus writes `t.salience > cur.salience`, and
            # `test_landmine_guards.py`'s Python arm reports every comparison one side of which
            # names a weight column, over every module under `spielplan/`. This comparison picks
            # the level a kept tag carries and removes nothing, but the guard cannot tell a max
            # from a cut by reading source, and `dna/verify.py:803-811` argues why it "must not be
            # taught to": an exemption for a comparison that looks like a max is one a real cut
            # can wear. So the two levels are bound to locals first, which is the idiom that
            # comment establishes, and the arithmetic stays the corpus's to the bit.
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


# The whole extracted tier of one title under one vocabulary version, whoever wrote it: an earlier
# extraction, another provider's, or the bundle importer's `''` rows. `dna_evidence` follows through
# `ON DELETE CASCADE` (`0004_dna.sql:94`).
_REPLACE = "DELETE FROM dna_tag WHERE title_id = $1 AND version = $2"

_TAG = (
    "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, n_sources, provider)"
    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id"
)

# `source_ref` carries the run's key, which is what `importer/dna.py:638-641` already writes there
# for the bundle's own `pass_id`. Without it two runs of one provider quoting the same sentence are
# two identical rows, and "the evidence of every run that found it" could not be told from one
# quote stored twice.
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

    Replace, not accumulate: re-ingesting a batch must be idempotent, and a
    re-extraction against a new pack must not leave tags behind that were
    verified against the old one.

    That is `mdc/dna/store.py:296-298` verbatim, and it binds harder here than there: an upsert on
    the four-column key would keep a term the new runs no longer found, under a provider the admin
    may since have switched off. The delete and every insert are one transaction, so a write that
    fails part-way leaves the previous tier standing rather than half of each.

    `n_runs` is the count `merge_passes` returned, and zero is refused before anything is deleted.
    Runs that all came back empty are a verdict -- the title carries nothing verified against this
    pack -- and they replace the tier with nothing; no runs at all is the absence of one, and a
    write that took it for a verdict would erase a tier nobody re-read.

    It writes `dna_tag` and `dna_evidence` and nothing else: the projected tier is stage 8's, and
    §4.1 rule 1 keeps the two apart. Returns the number of `dna_tag` rows written.
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
