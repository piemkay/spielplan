"""Verification for extracted DNA -- the trust boundary. Spec v2.1 §8 stage 7, §9, §4.1 rule 1.

The pipeline's trust boundary lives here.  An extraction pass is an untrusted
producer — of tags, and (§6.7) of text it read from user-written reviews — so
nothing it says about its own correctness is used.  Every tag is re-checked
against the vocabulary and against the title's own pack before it can become a
row, and the checks are mechanical string tests rather than judgements:

    1. the term id exists in the vocabulary (after the deterministic
       prefix repair, which is free recall and cannot invent a term)
    2. the quote is a substring of THAT title's pack, whitespace-collapsed
       and lowercased
    3. salience is 1, 2 or 3

Failing 1 or 2 drops the tag.  It is never repaired into something plausible:
a fabricated quote is exactly what a hallucinating extractor produces, and the
substring test is what turns catching it from judgement into mechanics.  The
pilot's negative control - plausible tags with invented quotes - was caught at
100% by this test alone.

**Schema failure is not content failure.**  One observed run emitted `"id"`
instead of `"term"` and omitted `"source"`, while its own self-verification
reported zero errors because it checked its own field names.  A strict consumer
logged all 161 tags as "bad term", which is indistinguishable from a
catastrophic quality collapse.  So :func:`verify_payload` accepts the common key
aliases, and every rejection is recorded with a reason that separates
`schema` from `unknown_term` and `quote_unverified`.  ``mdc dna ingest``
reports those separately, and a spike in `schema` means fix the parser, not the
extractor.

Everything above this line is `mdc/dna/store.py`'s own module docstring, spliced from its lines
3-29 by script rather than re-typed, because a trust boundary whose argument was paraphrased is a
boundary somebody will later reason about from the paraphrase. Two things are deliberately absent.
The summary line is this port's, because that module's subject is "verification, merge and storage"
and merge and storage are M5.5's. And its fourth paragraph -- the union-never-intersection
arithmetic that turns two passes' agreement into `confidence` -- is absent for the same reason: it
is a fact about RUNS, and the runs belong to the milestone that calls a provider.

§9 IS WHY THE ARGUMENT ABOVE BINDS THIS APP AND NOT ONLY THE PROJECT THAT MEASURED IT: "The schema
is a cost-saving device, not the guarantee - the guarantee is the validator" (`spec:418`). A
provider that returns well-formed JSON has satisfied the schema and has said nothing at all about
whether the tags are true. §8 stage 7 (`spec:389`) names the three checks in the order this module
applies them -- term-in-vocabulary after alias repair and adjudication rename, quote-substring-of-
pack via `norm()`, salience within {1, 2, 3} -- and adds the rule that makes them a boundary
rather than a cleanup: "Failures drop, never repaired."

THE KEY-ALIAS TOLERANCE LOOKS LIKE SLOPPINESS AND IS A MEASURED DECISION. `TERM_KEYS` and the three
lists beside it are ported with the corpus's own comment, and the second paragraph above is the
scar that produced them: a run that emitted `"id"` for `"term"` and omitted `"source"` while
reporting zero errors about itself, and a strict consumer that logged all 161 of its tags as "bad
term" -- a reading indistinguishable from the extractor having collapsed. A reviewer who tightens
these lists is not making the guarantee stricter; they are making it more expensive, because the
guarantee is the three checks below and a field name is not one of them. Every alias must stay
unambiguous, which is the only rule the list has.

NO PROVIDER CLIENT REACHES THIS MODULE, and that is a fact about its imports rather than a
convention. `mdc/dna/store.py` -- which *is* the corpus's trust boundary -- imports its
adjudication ledger, its packs, its similarity layer and its vocabulary, and does not import its
LLM client at all. Neither does this: `verify_payload` takes a decoded payload mapping and knows
nothing about where it came from, so there is no import to follow from a validator to the thing it
judges.

WHAT IS PORTED AND WHAT IS NAMED. Seven changes, each forced and each argued where it is written:

  1. TWO LAYERS, because the vocabulary, the packs and the ledger are rows here and files there.
     `load_vocabulary`, `read_packs` and `record_rejects` do the I/O; `verify_payload` is a
     function of what they return, plus one optional connection for the ledger. The corpus reads
     all three from inside the one synchronous function, which nothing in this app could do.
  2. `Vocabulary.repair` is REWRITTEN rather than ported -- the deterministic prefix repair, whose
     own paragraph states the rule it keeps and the measurement behind it.
  3. THE ALIAS REPAIR IS NEW, and it is the spec's rather than the port's: §8 stage 7 says
     "term-in-vocabulary (after alias repair + adjudication rename)" and this app has an authored
     alias map where the corpus's extraction path deliberately has none.
  4. A SALIENCE OUTSIDE {1,2,3} IS REFUSED rather than clamped (decision 386).
  5. `Rejection` CARRIES THE FACET, THE SALIENCE AND THE QUOTE, because decision 341 gives it a
     table and not a log line: the corpus's rejections are counted and printed, and §6.6's reject
     review has to show a person the tag that was refused. On every arm that knows them, which is
     decision 400 and is not where this started: the level was read after the other refusals had
     already appended, so the one row recording a level was the row where the level was the rule
     that broke -- and `unknown_term` and `quote_unverified`, the two a reviewer triages, said
     nothing about what the provider had claimed.
  6. `VerifiedTag` REFUSES TO EXIST WITHOUT ITS QUOTE. §4.1 rule 1 and `0004_dna.sql:91-92` -- "a
     tag without its quote is unfalsifiable" -- are enforced in the corpus at the write, and the
     write is M5.5's file, so the rule would have no home in this milestone at all. A type that
     cannot be constructed without a quote puts the refusal in the only place M5.4 owns.
  7. `coverage()`, `merge_passes()` and `store_title()` ARE NOT PORTED. The first is decision
     390's and lives beside the facets it measures; the other two are about runs.

NOTHING HERE REPAIRS A TERM BODY. Two writes change a term and both are named: the resolve above
the vocabulary check, and the ledger rename after it. `test_dna_verify.py` reads this module with
`ast` and asserts there is no third, because "never repaired" is the rule a reader is most likely
to soften by accident -- one character off a real term looks like a typo to fix, and is a tag that
was never emitted.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from spielplan.acquire import rawstore
from spielplan.db.dna_terms import active_version
from spielplan.dna import adjudicate
from spielplan.dna.aliases import alias_key, load_alias_map
from spielplan.dna.norm import norm
from spielplan.dna.packs import sha as pack_sha

# Key aliases seen in the wild.  Extending this list is cheaper than losing a
# whole batch to a field name, but every alias must be unambiguous.
TERM_KEYS = ("term", "id", "term_id", "tag")
QUOTE_KEYS = ("quote", "evidence", "span", "text")
SOURCE_KEYS = ("source", "src", "marker")
SALIENCE_KEYS = ("salience", "weight", "level")

REASONS = ("schema", "unknown_term", "adjudicated", "quote_unverified", "unknown_title",
           "no_pack", "duplicate")

# The current extraction configuration: the 60/12 packs plus the prompt with
# the anti-quota clause.  Recorded per title so a run planner can tell tags
# produced by an earlier configuration from current ones - an older pipeline's
# output is stale evidence, not merely older.
PIPELINE = "library/v1"

# Decision 386, and the one rule this port changes rather than carries. The corpus writes
# `max(1, min(3, sal))` (`mdc/dna/store.py:214`), which promotes a 0 to a 1 and demotes a 4 to a 3
# -- and a promotion is a repair, which is the one thing §8 stage 7 says this boundary never does.
# `dna_tag` states the same domain as a constraint (`0004_dna.sql:78`'s CHECK on the column), so
# a clamp is also the only way to write a row the schema would otherwise have refused. A value
# outside the set is recorded under `schema`: REASONS is ported verbatim and gains no member for
# this, because what broke is the contract's declared domain and that is what `schema` means.
SALIENCE_LEVELS = (1, 2, 3)

# What a tag that states no level at all is worth. This IS ported -- it is the `or 2` in the
# corpus's expression -- and it is not the clamp wearing another hat: a field the extractor did not
# fill is a field it made no claim about, and the middle of a three-point scale is the only reading
# that invents nothing. A field it DID fill with 0 is a claim, and a claim outside the domain is
# refused above rather than replaced with this.
DEFAULT_SALIENCE = 2

# The widest value the reject store can hold, because `dna_reject.salience` is a `smallint` and a
# payload is untrusted about its numbers as well as about its strings. A provider that answers 1e30
# would otherwise take the whole `executemany` down with a range error and lose every OTHER refusal
# in the same run -- the "losing a whole batch" failure the key-alias comment above is written
# against, arriving by a different door. The value is dropped rather than clamped, for decision
# 386's reason, and the term, the quote and the rule still name the row.
_SMALLINT = 32767

# The widest title id the probe in `_titles_this_install_holds` can carry, for `_SMALLINT`'s
# reason one column over. A payload's own title key reaches `Rejection.title_id` through a bare
# `int()`, which has no width at all, and binding one wider than `bigint` would raise out of the
# very probe that exists to stop a raise. An id no `bigint` can hold is an id no `title` row has,
# so it is not held, and its refusal is written with the NULL `0027` made the column nullable for.
_INT8 = 2 ** 63 - 1


@dataclass
class Rejection:
    """One tag the boundary refused, and the rule it broke.

    `facet`, `salience` and `quote` are this port's addition (named change 5): decision 341 turns a
    rejection into a `dna_reject` row that §6.6's review renders, and a person reading "this term
    was not in the vocabulary" cannot judge it without the quote it claimed to rest on. The
    corpus's three fields are enough for a counter and not for a screen.

    `detail` reaches no column, which is worth stating rather than leaving to be discovered:
    `0027` gives `dna_reject` the seven-member `rule_violated` and no free-text column, so the
    detail is what a caller logs and the rule is what the install keeps. That is deliberate -- a
    free-text column beside a closed set is where the closed set stops being read -- and it is why
    the details below are written to be readable on their own.
    """

    title_id: int | None
    pass_id: str
    term: str | None
    reason: str
    detail: str = ""
    facet: str | None = None
    salience: int | None = None
    quote: str | None = None


@dataclass
class VerifiedTag:
    """One tag that passed all three checks, with the evidence it passed on.

    THE QUOTE IS REQUIRED AND THE TYPE ENFORCES IT (named change 6). §4.1 rule 1 makes
    `dna_evidence` ship with the extracted tier and `0004_dna.sql:91-92` says why in four words --
    "a tag without its quote is unfalsifiable" -- and the exit criterion this milestone is judged
    against demands that a passed tag with no evidence quote be refused by the writer. The writer
    of a `dna_tag` row is M5.5's, so the refusal is placed at the only boundary M5.4 owns: the
    constructor of the only object this module hands forward. A caller that builds one without a
    quote gets a `ValueError` at the point of the mistake rather than a row nothing can falsify.

    THE TEST IS `norm()` AND NOT `strip()`, WHICH IS DECISION 392. `norm()` is the only reading of
    a quote this boundary has, and `**`, `___`, `[spoiler]` and a soft hyphen all fold to nothing
    under it while every one of them survives `str.strip()`. A tag whose evidence is a pair of
    asterisks is unfalsifiable in exactly the sense §4.1 rule 1 means, so the type's promise and
    the substring test in `verify_payload` have to be one question asked of one function --
    otherwise the constructor admits what rule 2 then passes vacuously, `""` being a substring of
    every pack.

    AND A QUOTE THAT IS NOT TEXT IS NOT A QUOTE, WHICH IS DECISION 399. `norm()` takes `Any` and
    opens with `str(s)` so that no payload can make it raise, which means a JSON number reached
    this type as a `str()` of itself and satisfied the test above: `1995` folds to "1995", and the
    `[plot:1]` markers and the `# <name> (<year>)` header `render_pack` writes put those digits in
    essentially every pack this app builds. The annotation already said `str`; the constructor now
    asks. It is the same refusal `_level` makes one field over for a boolean (decision 394) -- a
    value whose JSON type is a category error is refused rather than coerced into a claim the
    provider did not make -- and it is not a minimum length, which the spec does not state and
    this type therefore does not invent.

    AND A QUOTE POSTGRES CANNOT STORE IS REFUSED HERE AS WELL AS AT THE BOUNDARY, WHICH IS DECISION
    397 APPLIED TO THE ONE OBJECT IT HAD MISSED. `norm()` drops what prints nothing (decision
    398), and U+0000 is Cc while an unpaired surrogate is Cs, so the question above reads
    `"a slow\\x00 burn"` as a clean sentence while the raw string this type carries forward is one
    `dna_evidence.quote` cannot hold. `verify_payload` already refuses both before it builds a
    tag, which is precisely what made the type's guarantee a property of one caller: M5.5 builds
    its own `VerifiedTag`s, and one NUL in one quote of an `executemany` would take every GOOD tag
    in the batch down with nothing recording that they existed. `record_rejects` was denied the
    producer-only guarantee in so many words, and this type is exported under the same argument.
    [M5.4 review cycle 3, M54-C3-DIM2-02]
    """

    term: str
    facet: str
    salience: int
    source: str
    quote: str
    repaired: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.quote, str):
            raise ValueError(
                f"a verified tag must carry the quote it was verified against; {self.term!r} "
                f"arrived with {type(self.quote).__name__} and not text, so it is not a span of "
                "any pack, and section 4.1 rule 1 makes a tag without its quote "
                "unfalsifiable"
            )
        if not norm(self.quote):
            raise ValueError(
                f"a verified tag must carry the quote it was verified against; {self.term!r} "
                f"arrived with {str(self.quote)[:40]!r}, which folds to nothing under norm(), "
                "and section 4.1 rule 1 makes a tag without its quote unfalsifiable"
            )
        if _storable_text(self.quote) is None:
            raise ValueError(
                f"a verified tag must carry a quote its evidence row can hold; {self.term!r} "
                "arrived with one carrying a NUL or an unpaired surrogate, which Postgres "
                "cannot store (decision 397)"
            )


@dataclass
class PassResult:
    """One extraction pass over one unit of titles, after verification.

    `n_seen` COUNTS THE TAGS THE BOUNDARY LOOKED AT, which is not the same number as the tags a
    payload contained, and two readings of one field is how an accounting identity stops meaning
    anything. A title refused whole -- `unknown_title`, `no_pack`, a non-numeric key, a `tags`
    that is not a list -- is ONE rejection for the title and its tags are never examined, so
    `n_seen` does not rise for them. Over the tags that WERE examined the identity holds for any
    payload and is asserted as one: `n_seen` equals `n_kept` plus the tag-level rejections
    (decision 392). Reading it as "every tag the payload contained" would make those title-level
    arms indistinguishable from the silent loss decision 341 exists to make impossible.
    """

    pass_id: str
    tags: dict[int, list[VerifiedTag]] = field(default_factory=dict)
    rejects: list[Rejection] = field(default_factory=list)
    n_seen: int = 0

    @property
    def n_kept(self) -> int:
        return sum(len(v) for v in self.tags.values())


def _first(d: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


# ---------------------------------------------------------------------------
# the vocabulary, as the boundary needs to see it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Vocabulary:
    """One version's terms, facets and alias map, resolved once for a whole pass.

    `mdc/dna/vocab.py`'s `Vocabulary` reads TSVs off disk and carries the idf table, the labels,
    the glosses and the extraction block. This carries the four things a boundary asks for, and it
    is a plain value so that `verify_payload` is a function of its arguments: what the corpus gets
    from a module-level cache over a file that cannot change under it, this gets from being handed
    the answer before the walk starts.

    `by_tail` is the index the prefix repair reads. It is built once here rather than scanned per
    term because a payload names a handful of terms and the vocabulary carries 582 of them, and
    because the repair's refusal rule is a question about the WHOLE vocabulary -- "is there exactly
    one term with this body" -- which a scan would have to answer from scratch every time.

    `facet_keys` is the same facets case-folded, and a second set rather than a folded `facets`,
    so that `facets` keeps meaning the ids `dna_facet` stores. It is what the repair's head guard
    reads, for the reason `repair` gives.
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
        """The type, with `by_tail` derived rather than supplied.

        `by_tail` is an invariant of the vocabulary and not of the loader, so it is derived in
        one place: a test that built the index itself would be a second derivation of the
        repair's uniqueness rule, and a second derivation is how the repair starts answering
        one thing in the suite and another on the install. `dna/norm.py` exists one module
        over for the same reason, stated about a different function.
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
        """The term an AUTHORED alias row gives this spelling, or None. Named change 3.

        §8 stage 7 puts "alias repair" before the vocabulary check and this app has a map to do it
        with: `dna_alias` ships 4,095 raw spellings and until M5.4 nothing read it. An extractor
        that answers "slow burn" where the vocabulary says `pacing.slow_burn` has named a term the
        owner already decided that phrase means, and rejecting it would be rejecting a tag on a
        spelling rather than on its content.

        AUTHORED, WHICH IS WHY IT RUNS BEFORE THE DERIVED REPAIR BELOW. A map row is a decision
        somebody took; the prefix repair is a mechanical guess about a mistake. Where both can
        answer, the decision wins.

        ONE KNOWN NARROWING, STATED RATHER THAN HIDDEN. `load_alias_map` drops `kind='lexicon'`
        rows because §8 stage 8 must not project them, and the corpus calls those rows the
        EXTRACTION lexicon -- so a stricter map is being used here than the name suggests. On this
        install the two readings are the same map: `dna_alias.kind` arrives with `0027` and no
        loader fills it yet (decision 383), so every shipped row is NULL and none is excluded. The
        day a loader writes the column, whether the boundary should read lexicon rows the
        projection may not is a question for the milestone that fills it, and the narrowing is in
        the conservative direction -- fewer repairs, never more.
        """
        mapped = self.aliases.get(alias_key(term_id))
        return mapped[1] if mapped is not None else None

    def repair(self, term_id: str) -> str | None:
        """Map a near-miss term id onto a real one, or return None. Named change 2.

        THE MEASUREMENT IS THE CORPUS'S AND SO IS THE PROPERTY. Extractors emit
        `plot_structure.cat_and_mouse` for what the file calls `structure.cat_and_mouse`; with the
        prompt's prefix warning this is rare, and without it Haiku 4.5 produced 59% invalid ids
        with almost every one being that mistake (`mdc/dna/vocab.py:238-263`, citing its own
        `reports/model-bakeoff.md`). Repair is free recall, so it runs before anything is counted
        invalid -- but it only ever rewrites the PREFIX, never the term body, so it cannot invent a
        tag the extractor did not mean.

        THE RULE IS REWRITTEN BECAUSE THE TABLE IT READ DOES NOT EXIST HERE. The corpus maps the
        offered head through `prefix_of_facet`, a table of its own extraction labels (`mood_tone`,
        `narrative_themes`, `character_dynamics`) onto this app's term prefixes (`mood`, `themes`,
        `characters`). Importing that table would import the other project's naming, which is the
        confusion M4.9 finding 1 measured at 29,188 of 31,540 `dna_tag` rows. So the rule is stated
        against this app's own data instead: a head `dna_facet` does not declare is not a facet,
        and the body is looked up among the active version's terms.

        AND IT REFUSES WHEN THE MATCH IS NOT UNIQUE, which is what keeps the corpus's property. A
        body two facets both carry is a tag whose facet the extractor was making a claim about, and
        picking one would be choosing a meaning rather than recalling a spelling. A head that IS a
        declared facet is held to it: a repair that would file the tag under any OTHER facet is
        refused, because the extractor named a real facet and a body that facet does not carry,
        which is a claim about the BODY, and the body is never rewritten. The corpus's second arm
        -- the same head spelled with the other separator -- needs no port, because `mood-tone` is
        not a declared facet either and takes the same path.

        "IS A DECLARED FACET" IS ASKED CASE-FOLDED AND STRIPPED, because every other comparison
        this package makes on a term spelling folds case -- `alias_key`, the lexicon test and
        `adjudicate._verdict_key` -- and an exact-case test here made one capital letter change
        what the same string meant: `mood.slow_burn` was refused while `Mood.slow_burn`, the
        head an LLM title-cases, was kept as `pacing.slow_burn`, a tag under a facet the
        extractor never named and M4.9 finding 1's shape. That is decision 392's hole one field
        over -- one form for the rule and another for the guard. `casefold` rather than `lower`
        because the head is the untrusted side and a wider fold refuses more, never less. A head
        that names the facet which really carries the body (`Mood.bleak`) still repairs: the
        only thing rewritten is the prefix's spelling, and the facet claim is honoured.
        [M5.4 review cycle 3, M54-DIM3-C3-01]
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
        """The vocabulary term this spelling names, or None. The first of the two named writes.

        Exact match, then the authored alias map, then the deterministic prefix repair -- §8 stage
        7's "after alias repair" in the order the two repairs earn. Everything this returns is a
        key of `terms` or None, which is the whole of what the vocabulary check downstream is
        entitled to assume.

        AND THE AUTHORED ARM TESTS THAT ITSELF, so the sentence above is a property of this type
        rather than of its loader. `verify_payload` spends the answer on `voc.terms[term]`, and
        the exact-match and repair arms are closed by construction while this one returns whatever
        the map says. On this install the map is joined to `dna_term` one module over, but
        `Vocabulary.build` takes it from its caller and validates nothing -- and `dna_alias` has
        no foreign key from `term` to `dna_term` at all (`0004_dna.sql:42-47` keys only
        `version`), so a row naming a term the vocabulary does not carry is a row the table
        STORES rather than refuses. Without this test such a row turned a dropped tag into a
        `KeyError` out of the boundary. The fall-through to the prefix repair is deliberate: an
        authored row pointing at nothing is not a decision that answered, so it does not get to
        suppress the arm below it.
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

    None is the M0 and pre-seed state rather than an error (§3.1: "a bundle-less app is a legal
    state"), and the caller must not run stage 7 on it. That is deliberate and it is the
    conservative reading: a boundary with no vocabulary would reject every tag as `unknown_term`,
    which is precisely the count that cannot be told apart from an extractor collapsing -- the
    confusion the second ported paragraph above is written about. An install with nothing to check
    against declines to check, rather than refusing everything in a way that reads as a verdict.

    §14 risk 7 (`spec:503`) makes every read scoped to one version and `db/dna_terms.py` holds this
    app's one derivation of which one is live, so no literal version string appears here.
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


# ---------------------------------------------------------------------------
# the pack a verdict is reached against
# ---------------------------------------------------------------------------

_PACK_ROW = "SELECT pack_sha, raw_document_id FROM dna_pack WHERE title_id = $1 AND version = $2"


async def read_pack(conn: asyncpg.Connection, title_id: int, version: str) -> str | None:
    """The text this title's tags must quote from, or None when it has no pack. Decision 382.

    The corpus reads a file; this reads `dna_pack`'s row and the bytes M5.1's raw store holds under
    it. None means there is no pack to verify against and the caller records `no_pack` -- never a
    silent pass, because a quote that cannot be checked is exactly what an invented one looks like.

    A SHA MISMATCH RAISES RATHER THAN ANSWERING None, and the corpus's scar is the argument. Its
    ingest derived the sha from whatever pack was on disk at the time, which marked 825 titles
    current against a pack no pass had ever seen and hid 652 (`mdc/dna/store.py:277-300`). A
    `dna_pack` row whose bytes are not the pack it names is that damage in this app's shape: not a
    title without a pack, but an install that would verify quotes against the wrong text and record
    the verdicts as if they meant something. `rawstore.read` already raises on the same question
    one layer down, for the same reason and in the same voice.
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


async def read_packs(
    conn: asyncpg.Connection, title_ids: Iterable[int], version: str
) -> dict[int, str | None]:
    """The packs for a payload's titles, deduplicated, in the order they were asked for."""
    return {
        int(title_id): await read_pack(conn, int(title_id), version)
        for title_id in dict.fromkeys(int(t) for t in title_ids)
    }


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


async def verify_payload(
    payload: Mapping[str, Any],
    *,
    pass_id: str,
    voc: Vocabulary,
    packs: Mapping[int, str | None],
    ledger: asyncpg.Connection | None = None,
    allowed: Iterable[int] | None = None,
) -> PassResult:
    """Check one extractor output object against the vocabulary and the packs.

    `payload` is `{"titles": {"<id>": [tag, ...]}}`; `pass` inside the payload overrides `pass_id`
    only if present, so a file that names itself keeps its identity through a rename.

    `packs` maps a title id to its pack TEXT, unfolded -- `read_packs` is how a caller with a
    database gets one. The fold happens here, once per title, and never at pack-build time: a pack
    that has been tidied verifies FEWER quotes, not more, and the damage is invisible until
    somebody counts rejections.

    `ledger` is the connection the curation ledger is read through, or None where there is none.
    None is honest rather than convenient: an install that cannot read `dna_adjudication` cannot
    tell a term the owner retired from one the extractor invented, so it says `unknown_term`, which
    drops the same tag under the less informative of the two labels. It never keeps one.

    AND THE LEDGER IS ONLY ASKED ABOUT A TERM THE VOCABULARY DOES NOT CARRY. That is the port's
    own arrangement and it bounds what a `drop` verdict does, which is worth stating because
    `dna/adjudicate.py`'s `is_retired` answers the same question without asking the vocabulary
    first: a retirement of a term the ACTIVE vocabulary still carries is recorded in
    `dna_adjudication` and is not acted on here, so the package answers one question two ways
    depending on which door a caller knocks on. Removing the term from the vocabulary is what
    retires it, and decision 163 makes that a migration. The alternative costs one ledger read per
    TAG rather than per unknown term, which is a bill a milestone takes deliberately under its own
    number rather than a quiet edit to this line.

    `allowed` IS THE UNIT'S TITLE IDS, AND OMITTING IT COSTS A LABEL rather than a tag. With it, a
    payload naming a title outside this unit is refused as `unknown_title`; without it the same
    payload falls through to `packs` and is refused as `no_pack`, which drops the same tag under
    the less informative of those two -- exactly the trade `ledger=None` makes one paragraph up.
    A caller that has the unit in hand passes it, or §6.6's screen reads a provider inventing a
    title id as this install never having packed one, and sends the operator to the pack builder.
    """
    res = PassResult(pass_id=str(payload.get("pass") or pass_id))
    allow = {int(a) for a in allowed} if allowed is not None else None

    titles = payload.get("titles")
    if not isinstance(titles, dict):
        res.rejects.append(Rejection(None, res.pass_id, None, "schema",
                                     "no 'titles' object in payload"))
        return res

    # The corpus's per-title pack cache, kept for the half of its job that survives the port. The
    # disk read is gone -- `packs` is already in hand -- but `norm()` over a sixty-review pack is
    # not free, and a payload names a title once and its tags a dozen times.
    pack_cache: dict[int, str | None] = {}
    seen_terms: dict[int, set[str]] = {}
    for raw_tid, tags in titles.items():
        try:
            tid = int(raw_tid)
        except (TypeError, ValueError):
            res.rejects.append(Rejection(None, res.pass_id, None, "schema",
                                         f"non-numeric title key {raw_tid!r}"))
            continue
        if allow is not None and tid not in allow:
            res.rejects.append(Rejection(tid, res.pass_id, None, "unknown_title",
                                         "title not in this unit"))
            continue
        if tid not in pack_cache:
            text = packs.get(tid)
            pack_cache[tid] = norm(text) if text is not None else None
        pack = pack_cache[tid]
        if pack is None:
            res.rejects.append(Rejection(tid, res.pass_id, None, "no_pack",
                                         "no pack for this title"))
            continue
        if not isinstance(tags, list):
            res.rejects.append(Rejection(tid, res.pass_id, None, "schema",
                                         f"tags for {tid} are {type(tags).__name__}"))
            continue

        # DECISION 392, AND THE REASON BOTH OF THESE ARE KEYED ON THE TITLE. `int()` reads "7"
        # and "07" as one title, and a payload is untrusted about its keys as well as about its
        # values, so a list bound per payload KEY and assigned at the end of this body dropped
        # the first spelling's verified tags on the floor -- a drop that left no `dna_reject`
        # row, which is the one failure decision 341 exists to make impossible, and invisible in
        # any rejection count because what was lost was GOOD. Carrying `seen` with them is what
        # keeps a term the second spelling repeats landing as `duplicate`, so every tag this
        # function was offered leaves it either kept or recorded.
        kept = res.tags.setdefault(tid, [])
        seen = seen_terms.setdefault(tid, set())
        for tg in tags:
            res.n_seen += 1
            if not isinstance(tg, dict):
                res.rejects.append(Rejection(tid, res.pass_id, None, "schema",
                                             f"tag is {type(tg).__name__}"))
                continue
            raw_term = _first(tg, TERM_KEYS)
            quote = _first(tg, QUOTE_KEYS)
            # DECISION 400, AND THE READ IS HOISTED WHILE THE TWO REFUSALS BELOW STAY WHERE THEY
            # ARE. `Rejection` carries the salience because decision 341 gives it a table and not
            # a log line, and the level was read after every other arm had already appended -- so
            # the ONE row that recorded a level was the row where the level is itself the rule
            # that broke, which is the row a reviewer needs it least on. On `unknown_term` and
            # `quote_unverified`, the two refusals §6.6's screen is mostly made of, the level the
            # provider claimed was discarded although the payload had stated it, and a reviewer
            # cannot tell a provider confidently asserting nonsense from one hedging. `facet` is
            # NULL on the arms above the vocabulary check because it is not knowable there; a
            # stated level always is. Reporting stays where it was: a coercion that failed is
            # still refused at its own arm below, so rule precedence is untouched.
            #
            # `OverflowError` BESIDE THE OTHER TWO IS DECISION 392, and this app has paid for the
            # lesson twice already: `acquire/stages.py:864-869` catches all three under a comment
            # naming the mechanism, and `importer/dna.py:796` records the import that died on it
            # after reading 1.04 GB. `float()` raises it for an integer too wide to convert and
            # `int()` raised it for an infinity, which Python's own `json` decoder produces from
            # the bare `Infinity` literal by default. A guard that names two of the three
            # exceptions its own conversion raises is what took the whole pass down instead of
            # dropping one tag -- and a raise is not a drop: it records nothing, never reaches
            # `record_rejects`, and loses every OTHER refusal in the same run.
            stated = _first(tg, SALIENCE_KEYS)
            try:
                level: int | float | None = DEFAULT_SALIENCE if stated is None else _level(stated)
            except (TypeError, ValueError, OverflowError):
                level = None
            if raw_term is None or quote is None:
                res.rejects.append(Rejection(
                    tid, res.pass_id, str(raw_term) if raw_term else None,
                    "schema",
                    f"missing {'term' if raw_term is None else 'quote'}; "
                    f"keys were {sorted(tg)}",
                    salience=_storable(level),
                    quote=str(quote) if quote is not None else None))
                continue
            # DECISION 399. `norm()` opens with `str(s)` so that nothing a payload contains can
            # make it raise, which is right for a fold and is not a reading of a type: a JSON
            # number came through it as the `str()` of itself, and `render_pack`'s own `[plot:1]`
            # markers and `# <name> (<year>)` header put those digits in essentially every pack
            # this app builds, so `{"evidence": 1}` from an adapter that rendered a structured
            # span as its offset verified against the title it was attached to. A quote that is
            # not text is a missing quote for §4.1 rule 1's purpose, so it is refused under the
            # same `schema` and the value is still recorded for §6.6 to show.
            if not isinstance(quote, str):
                res.rejects.append(Rejection(
                    tid, res.pass_id, str(raw_term), "schema",
                    f"quote {str(quote)[:40]!r} is {type(quote).__name__} and not text",
                    salience=_storable(level), quote=str(quote)))
                continue
            # AND THE SAME QUESTION OF THE TERM, which is decision 399's reason applied one field
            # over. `str()` of a one-element array is "['slow burn']", and `alias_key` strips
            # brackets and quotes as surrounding punctuation, so the authored map -- and on an
            # install the self-mapping `load_alias_map` adds for each non-register term -- answered
            # it: the tag was KEPT with `repaired=True`, a repair nobody made, while a two-element
            # array of the same malformation dropped as `unknown_term`. §8 stage 7 drops and never
            # repairs. `TERM_KEYS` says which KEY a term may arrive under; this asks whether the
            # VALUE is text. [M5.4 review cycle 3, M54-DIM3-C3-05]
            if not isinstance(raw_term, str):
                res.rejects.append(Rejection(
                    tid, res.pass_id, str(raw_term), "schema",
                    f"term {str(raw_term)[:40]!r} is {type(raw_term).__name__} and not text",
                    salience=_storable(level), quote=quote))
                continue
            # DECISION 397, AND IT IS THE LEDGER READ ONE LINE BELOW THAT MAKES IT THIS ARM'S JOB
            # RATHER THAN THE WRITER'S ALONE. A term the vocabulary cannot resolve is bound into
            # `dna_adjudication`'s query as a parameter, and `json.loads` accepts both a U+0000 escape
            # and a lone surrogate escape while `norm()` and `str.strip()` preserve both -- so one
            # NUL in one term raised `CharacterNotInRepertoireError` out of `verify_payload`
            # itself, and a raise is not a drop: the pass returned no verdict, no tag was kept and
            # no refusal was recorded, which is the failure decision 392's own `OverflowError` arm
            # was written to close, through a door one check above it. The same two spellings then
            # take the atomic `executemany` in `record_rejects` down and lose every OTHER refusal
            # in the pass (decision 341's named failure), which `_storable_text` guards there.
            # Refusing here is what keeps the row informative: the rule, the title and the facet
            # still name it, instead of a NULL column standing for a value nobody can see.
            term_text = _storable_text(str(raw_term))
            quote_text = _storable_text(quote)
            if term_text is None or quote_text is None:
                res.rejects.append(Rejection(
                    tid, res.pass_id, term_text, "schema",
                    f"the {'term' if term_text is None else 'quote'} carries text Postgres "
                    "cannot store: a NUL or an unpaired surrogate",
                    salience=_storable(level), quote=quote_text))
                continue
            # DECISION 392, AND THE ONE `norm()` THIS TAG COSTS. Bound here rather than at rule 2
            # below, because evidence that folds to nothing is not evidence: `**`, `___`,
            # `[spoiler]` and U+00AD all fold to the empty string, `""` is a substring of every
            # pack, and rule 2 passed every one of them vacuously -- a term the vocabulary really
            # carries attached to a title whose pack never supported it, kept, and (because it
            # was not a drop) with no `dna_reject` row to say it happened. It is refused beside
            # the missing-quote test above and under the same `schema`, because a quote the only
            # fold this boundary has reads as nothing IS a missing quote; REASONS gains no
            # member. `VerifiedTag.__post_init__` asks the same question of the same function, so
            # no path here can build one its own type would refuse.
            folded = norm(quote)
            if not folded:
                res.rejects.append(Rejection(
                    tid, res.pass_id, str(raw_term), "schema",
                    f"quote {str(quote)[:40]!r} folds to nothing under norm()",
                    salience=_storable(level), quote=str(quote)))
                continue

            # ONE SPELLING FOR ALL THREE READS OF THE TERM. `resolve` strips its argument, and the
            # ledger matches `term = $2` exactly over terms the importer stored stripped, so the
            # vocabulary and the ledger were reading one value two ways inside one arm: a padded
            # `"themes.satire\n"` passed the first and missed its re-point in the second, and a
            # padded retirement landed as `unknown_term` instead of `adjudicated`. `adjudicate`
            # folds nothing by design, so the strip is made once here. The raw value is still what
            # a `Rejection` records, because the row says what the payload offered. Not named
            # `term`: the two named repairs below are the only statements that write that name.
            # [M5.4 review cycle 3, M54-DIM3-C3-02]
            offered = str(raw_term).strip()
            term = voc.resolve(offered)
            if term is None:
                # A retired id the ledger re-points is not an unknown term -- it is a term the
                # vocabulary renamed after this file was written. Repairing it here, before the
                # vocabulary check, is what lets a result file outlive a merge; without it every
                # ingest after a merge rejects the row and silently reverts the curation
                # (measured 2026-08-25: 496 rows, twice).
                term = await _renamed(ledger, offered, tid, voc.version)
                if term is None or term not in voc:
                    # A term the ledger DROPS is not an unknown term either -- it is a term the
                    # owner retired on the evidence. Counting the two together makes a routine
                    # retirement look like an extractor emitting garbage, and hides the case that
                    # actually needs attention.
                    retired = await _is_retired(ledger, offered, tid, voc.version)
                    res.rejects.append(Rejection(
                        tid, res.pass_id, str(raw_term),
                        "adjudicated" if retired else "unknown_term",
                        "retired by the curation ledger" if retired
                        else "not in vocabulary",
                        salience=_storable(level), quote=str(quote)))
                    continue
            facet = voc.terms[term]
            if folded not in pack:
                res.rejects.append(Rejection(
                    tid, res.pass_id, term, "quote_unverified",
                    f"{str(quote)[:80]!r} is not in this title's pack",
                    facet=facet, salience=_storable(level), quote=str(quote)))
                continue
            if term in seen:
                res.rejects.append(Rejection(tid, res.pass_id, term, "duplicate",
                                             "term emitted twice for this title",
                                             facet=facet, salience=_storable(level),
                                             quote=str(quote)))
                continue

            # The coercion that produced `level` is hoisted above the arms that record it
            # (decision 400) and this is where its failure is REPORTED, unmoved: a level that is
            # not a number is refused after the term and the quote have been judged, so a tag
            # with a fabricated term and an unreadable level still lands as `unknown_term`. None
            # is the coercion's only failure spelling -- `_level` returns a number or raises, and
            # an absent field takes `DEFAULT_SALIENCE` -- so nothing else reaches this arm, and
            # the row carries no salience because there is no level to record.
            if level is None:
                res.rejects.append(Rejection(
                    tid, res.pass_id, term, "schema",
                    f"stated level {str(stated)[:40]!r} is not a number",
                    facet=facet, quote=str(quote)))
                continue
            # DECISION 386, AND THE LINE'S SHAPE IS NOT A STYLE CHOICE. The value is bound to a
            # local called `level` because `test_landmine_guards.py` reads every comparison in
            # this package for the bare word `salience` and cannot tell a domain check from a
            # weight cut -- §4.1 rule 2 forbids a WHERE clause that keeps only the rows whose
            # salience is 3, which deletes rows on a weight, and §8 stage 7 requires exactly
            # this, which is the declared domain of one field. The guard is right to be unable to
            # tell them apart and must not be taught to, so the distinction is carried here
            # instead: what follows is the three-level domain `0004_dna.sql:78` states as a CHECK
            # constraint, asked of one value out of an untrusted payload, and it selects no rows.
            if level not in SALIENCE_LEVELS:
                res.rejects.append(Rejection(
                    tid, res.pass_id, term, "schema",
                    f"stated level {level} is outside the declared domain {SALIENCE_LEVELS}",
                    facet=facet, salience=_storable(level), quote=str(quote)))
                continue

            seen.add(term)
            kept.append(VerifiedTag(
                term=term, facet=facet,
                salience=level,
                source=str(_first(tg, SOURCE_KEYS) or ""),
                quote=str(quote), repaired=(term != str(raw_term).strip())))
    return res


async def _renamed(
    ledger: asyncpg.Connection | None, term_id: str, title_id: int, version: str
) -> str | None:
    """`dna/adjudicate.py`'s `rename` where there is a ledger to read, None where there is not."""
    if ledger is None:
        return None
    return await adjudicate.rename(ledger, term_id, title_id, version=version)


async def _is_retired(
    ledger: asyncpg.Connection | None, term_id: str, title_id: int, version: str
) -> bool:
    """`dna/adjudicate.py`'s `is_retired` where there is a ledger, False where there is not.

    False is the conservative answer and not a guess: without the ledger the tag is rejected either
    way, and `unknown_term` claims less than `adjudicated` does. Naming a retirement that nobody
    read would be the boundary asserting a curation decision it never consulted.
    """
    if ledger is None:
        return False
    return await adjudicate.is_retired(ledger, term_id, title_id, version=version)


def _level(stated: Any) -> int | float:
    """The salience a payload states, read and never rounded. Decisions 386, 392 and 394.

    THE CORPUS'S `int(float(stated))` IS NOT PORTED, AND DECISION 386 IS THE REASON. That
    expression never tests the provider's claim against {1,2,3}; it tests `trunc(claim)`, so every
    non-integral claim in [1,4) was silently replaced with a value inside the domain. A stated 3.9
    became a stored 3, which is word for word the row 386 forbids -- "indistinguishable from a real
    3" -- and the only difference from the clamp it refuses is how wide the interval repaired over
    is. A stated 0.5 was worse: it dropped, and the `dna_reject` row recorded 0, so §6.6's reviewer
    read a number the provider never uttered on the screen decision 341 built to be reviewable.
    Reading an integral SPELLING is not repairing it -- "2" and 2.0 are claims the domain holds --
    so those still come back as `int`, and everything else comes back as what was said and is
    refused below by the domain check that was always meant to see it.

    A BOOLEAN STATES NO LEVEL. `float(True)` is 1.0, so a provider whose adapter renders a
    tri-state field as a boolean had every `true` read as the bottom of a three-point scale: a
    claim it never made, on the field §4.1 rule 2 makes a weight. It is refused rather than
    defaulted, for the reason `DEFAULT_SALIENCE`'s own comment gives -- that constant is for a
    field the extractor did NOT fill, and this one was filled with something that is not a number.

    A NON-FINITE VALUE IS NOT A NUMBER EITHER, and that is where decision 392 meets this function.
    `nan` already reached the not-a-number refusal through `ValueError`; `inf` reached
    `OverflowError` out of `int()` and left the boundary as a stack trace, taking every other tag
    and every other refusal in the pass with it. Both are the same claim about the same field, so
    both are refused here and arrive at one reason instead of one of them arriving as a crash.
    """
    if isinstance(stated, bool):
        raise TypeError("a boolean states no level")
    number = float(stated)
    if not math.isfinite(number):
        raise ValueError("a non-finite level is not a level")
    return int(number) if number.is_integer() else number


def _storable(level: int | float | None) -> int | None:
    """The stated level as `dna_reject.salience` can hold it, or None. See `_SMALLINT`.

    INTEGRALITY IS PART OF THE QUESTION AND NOT A TYPE CHECK. `_level` hands this whatever the
    provider stated, so a refused 3.9 arrives as a float -- and asyncpg truncates a float bound
    into a `smallint` client-side and silently, which would put a 3 in the column of the row that
    exists to record that 3.9 was said. A number a reviewer cannot see is better than a number
    nobody stated: that is `_SMALLINT`'s argument about 1e30 and decision 386's about the clamp,
    and the value itself still reaches the caller in the rejection's detail either way.

    None IS AN ANSWER AND NOT AN ERROR, because decision 400 hoists the coercion above the arms
    that record its result: a tag whose level could not be read at all still reaches an earlier
    refusal, and "there is no level" is exactly the NULL this returns.

    A BOOLEAN IS NOT A LEVEL EITHER, and it has to be refused HERE and not only in `_level`.
    `isinstance(True, int)` is true, so `True` passed the width test and asyncpg wrote a 1 --
    a level the provider never stated, in the column decision 386 exists to keep faithful. The
    boundary's own path cannot reach it (`_level` raises on a boolean first, decision 394), but
    this function is now the writer's guard as well as the producer's, and a guard that holds
    only for the caller that does not need it is not a guard.
    """
    if isinstance(level, int) and not isinstance(level, bool):
        return level if -_SMALLINT - 1 <= level <= _SMALLINT else None
    return None


def _storable_text(s: str | None) -> str | None:
    """One untrusted string as a Postgres `text` column can hold it, or None. Decision 397.

    TWO SPELLINGS, ONE CONSEQUENCE. Postgres refuses U+0000 in a `text` value (22021) and UTF-8
    cannot encode an unpaired surrogate at all, so asyncpg raises on either -- and `json.loads`
    accepts both escapes while `norm()` and `str.strip()` preserve both, so both arrive here as
    ordinary provider strings. This app has met the first already: `api/auth.py:37` and
    `api/admin.py:68` both exist because a NUL byte in a login name reached Postgres and answered
    500 (sec-04), and `asyncpg is right to refuse text Postgres cannot store`.

    None RATHER THAN A CLEANED STRING, for the reason `_storable` gives one column over: a value
    a reviewer cannot see beats a value nobody stated, and a repaired term in the row recording
    why a term was refused would be the one repair this module does not make (§8 stage 7). The
    rule, the title and the facet still name the row, which is what decision 341 asks of it.

    AND ANYTHING THAT IS NOT A STRING IS None, for the reason `_storable` answers None for a
    non-integer: this is the writer's guard as well as the producer's, and a guard that raised on
    a value it exists to disarm would be the defect wearing the fix's clothes (decision 396).
    """
    if not isinstance(s, str):
        return None
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return None if "\x00" in s else s


# ---------------------------------------------------------------------------
# what was refused, written down
# ---------------------------------------------------------------------------

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

    §6.6 Data promises "review of DNA rejects and low-evidence tags" and §8 stage 7 drops the tags,
    and both are only true together if the drop is written down: a refusal nobody stored is a
    refusal nobody can review, and before `0027` there was no table to store it in.

    `provider` and `run_id` come from the caller because they are facts about the call and not
    about the tag -- which LLM produced the payload (§6.6's parallel mode) and which job run asked
    for it. `Rejection.pass_id` reaches no column: a pass's own identity belongs with the call that
    made it, which is `llm_call` in `0028` and M5.5's to design.

    NOTHING HERE FILTERS AND NOTHING HERE ORDERS. §4.1 rule 2 makes `salience` a weight and never a
    filter, and decision 341 settles what "low-evidence" may mean on §6.6's screen: an ORDER BY on
    ascending confidence over the tags that PASSED, never a WHERE on any of these columns.
    `dna_reject` carries no `confidence` column at all, which is that decision written into the
    schema rather than trusted to a reviewer.

    AND A TITLE THIS INSTALL DOES NOT HOLD IS WRITTEN AS THE NULL `0027` MADE THE COLUMN NULLABLE
    FOR. `verify_payload` carries the payload's OWN title id into a rejection -- it has no other
    -- and the column keeps its foreign key, so an id a provider invented was unwritable. Because
    `executemany` is atomic in asyncpg, one transposed digit discarded every genuine refusal in
    the same pass and handed the caller an exception instead of a count: the "losing a whole
    batch" failure the key-alias comment above is written against, arriving at the write rather
    than at the read, and the same door `_SMALLINT` already guards one column over. So the ids are
    resolved once against `title` and the ones it does not carry are written NULL, which is
    exactly the state `0027` describes -- "a payload naming a title this install does not hold is
    refused, and the refusal is the row" (decision 396). The id itself is not kept:
    `Rejection.detail` reaches no column on purpose, and a free-text column beside a closed set is
    where the closed set stops being read.

    AND EVERY UNTRUSTED VALUE IS GUARDED AT THE BIND RATHER THAN AT ONE PRODUCER (decision 397).
    `Rejection` is a plain dataclass in `__all__` and this function takes any iterable of them, so
    an invariant that held only because `verify_payload` happened to call `_storable` was a
    property of one caller and not of the writer -- and the whole argument above is that ONE
    unwritable value discards a whole pass's refusals. `term` and `quote` are the payload's own
    strings and go through `_storable_text`, which answers None for the NUL and the unpaired
    surrogate Postgres cannot hold; `salience` goes through `_storable` here as well as there, so
    a `Rejection` M5.5 builds for itself cannot write a 1e30 or a `True`. `facet` is not guarded
    and that is the same rule rather than an exception to it: it is `voc.terms[term]`, this
    install's own vocabulary row, and nothing a payload says reaches it. `provider` and `run_id`
    are the caller's facts about the call, for the reason two paragraphs up.
    """
    refused = list(rejects)
    if not refused:
        return 0
    held = await _titles_this_install_holds(conn, refused)
    rows = [
        (r.title_id if r.title_id in held else None,
         run_id, _storable_text(r.term), r.facet, _storable(r.salience),
         _storable_text(r.quote), r.reason, provider)
        for r in refused
    ]
    await conn.executemany(_RECORD_REJECT, rows)
    return len(rows)


async def _titles_this_install_holds(
    conn: asyncpg.Connection, rejects: Sequence[Rejection]
) -> frozenset[int]:
    """Which of a pass's refused title ids `title` actually carries a row for.

    One query per call rather than per row, because the alternative shapes both cost something
    this milestone refuses: a per-row write gives up `executemany`'s all-or-nothing, and catching
    the foreign-key error after the fact would tell the caller which batch failed and not which
    row. `bigint` on the probe and a width test before it for `_INT8`'s reason -- the ids arrive
    from a bare `int()` over an untrusted key, and a guard that raised on the value it exists to
    disarm would be the defect wearing the fix's clothes.
    """
    named = {
        r.title_id for r in rejects
        if r.title_id is not None and -_INT8 - 1 <= r.title_id <= _INT8
    }
    if not named:
        return frozenset()
    rows = await conn.fetch("SELECT id FROM title WHERE id = ANY($1::bigint[])", sorted(named))
    return frozenset(row["id"] for row in rows)


__all__ = [
    "DEFAULT_SALIENCE",
    "PIPELINE",
    "QUOTE_KEYS",
    "REASONS",
    "SALIENCE_KEYS",
    "SALIENCE_LEVELS",
    "SOURCE_KEYS",
    "TERM_KEYS",
    "PassResult",
    "Rejection",
    "VerifiedTag",
    "Vocabulary",
    "load_vocabulary",
    "read_pack",
    "read_packs",
    "record_rejects",
    "verify_payload",
]
