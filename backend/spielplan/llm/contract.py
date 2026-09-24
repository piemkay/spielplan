"""The one extraction contract: its schema, its prompt, and the retry that names a violation.

Spec v2.1 §9, §8 stage 6, §4.1 rule 1; decisions 337, 431, 432.

§9: "The schema is a cost-saving device, not the guarantee - the guarantee is the validator ...
Two-attempt pattern: retry once with the specific contract violation named." This module is the
half of that sentence which talks to a model, and it deliberately holds no verdict:

  * ONE SCHEMA, `EXTRACTION_SCHEMA`, which each adapter projects into its own mechanism -
    Anthropic's tool `input_schema` as it stands, OpenAI's strict schema with the unsupported
    keywords stripped, Gemini's `responseSchema` dialect - so the three cannot diverge in what they
    ask for. What the model then puts in it is M5.4's `verify_payload`'s to judge.
  * THE PROMPT, generated from the install's own vocabulary and never transcribed.
  * THE RETRY MESSAGE, which formats `verify_payload`'s refusals - each violated rule and the value
    it was violated by - and re-derives none of them. Nothing here folds a quote, resolves a term or
    reads a salience domain: those are the validator's three checks, and a second copy of any of
    them beside the client is the convenience that ends with the client trusting itself.

Everything between the two rules below is `mdc/dna/prompt.py`'s own module docstring from its
ninth line, carried across verbatim at the corpus's own line width, because the clauses it
protects are ported into `INSTRUCTIONS` and `vocabulary_block` below and the warning has to travel
with them. The ceiling sum it names (44) is the corpus's at the time of measurement; the eleven
ceilings `FACET_MAX` carries today sum to 47.

--- `mdc/dna/prompt.py:9-25`, verbatim ------------------------------------------------------

**Two clauses here exist because their absence caused a measured failure.  Do
not "clean up" this prompt without re-running the A/B behind each one.**

1. *The prefix warning in the vocabulary header.*  Without it Haiku 4.5 emitted
   59% invalid term ids, almost all `plot_structure.cat_and_mouse` for what the
   file calls `structure.cat_and_mouse`.  With it: 4%.  This single sentence
   mattered more than the entire quality spread between models.
2. *The anti-quota clause.*  Without it the extractor fills toward the 44-tag
   ceiling sum regardless of evidence - per-title output collapsed to a 31-36
   range across ten very different films.  Adding one sentence saying the
   ceilings are upper bounds moved total output from 332 tags to 161.

A third clause was tested and **rejected**: allowing quotes that name a composer
or DP looked like a 3.4x sound_score win, but that run was inflated 1.8x
overall, and once the anti-quota clause controls inflation the gain vanishes.
Any future prompt edit has to be scored on total tags and per-title density
spread as well as its targeted metric, or it will report inflation as a win.

--- end of the ported text; everything below is this port's ---------------------------------

PORT VERDICT: **ported with named changes** from `mdc/dna/prompt.py` (180 lines), with the
ceilings from `mdc/dna/vocab.py:64-72` and the retry's opening from `mdc/aspects/prompt.py:671-679`.
Taken verbatim: `PROMPT_VERSION`, `INSTRUCTIONS`' six rules and the anti-quota clause in rule 5,
the vocabulary header's copy-exactly sentence and its prefix warning wherever that warning is
true, the per-facet section headers, `system_prompt`, `user_prompt` and `prompt_sha`. What
changed, each argued at the line it changes:

  1. **The answer is an object, `{"tags": [...]}`**, where the corpus asked for "ONLY a JSON
     array". Anthropic's tool `input_schema` and OpenAI's strict `json_schema` both require an
     object root, and a prompt that disagrees with the schema sent beside it asks the model to
     disobey one of the two.
  2. **The ceilings are keyed on this app's facet ids.** See `FACET_MAX`.
  3. **The vocabulary is read from `dna_term` and `dna_facet` for one version, in `ord` order,
     and the prefix clause says what is true of it.** See `vocabulary_block`.
  4. **`EXTRACTION_SCHEMA`, `as_verifier_payload` and `violation_prompt` are this port's.** The
     corpus's DNA passes ran as batch handoffs with no request schema, so the schema is written
     from rule text's own element shape; `as_verifier_payload` speaks `verify_payload`'s shape;
     `violation_prompt` keeps `mdc/aspects/prompt.py`'s opening and replaces its one error string
     with M5.4's verdicts (plan C4).
  5. **Not ported: `FOCUS` and `instructions(focus=...)`** - the gap-filling pass over a craft
     supplement, which has no caller at M5 (one extraction task, decision 432) - **and
     `render_readme`**, the batch handoff decision 338 does not ship.

`PROMPT_VERSION` names the lineage and is not the identity: named changes 1-3 make this wording
differ from the corpus's `dna-v1`, and `prompt_sha` over the rendered system prompt is what tells
two wordings apart - the corpus's own rule, "the manifest records :func:`prompt_sha` so ingest can
refuse a result produced against different wording" (`mdc/dna/prompt.py:4-5`).
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from spielplan.db.dna_terms import active_version

if TYPE_CHECKING:
    import asyncpg

    from spielplan.dna.verify import Rejection

log = logging.getLogger("spielplan.llm.contract")

PROMPT_VERSION = "dna-v1"

# Named change 4. The element shape rule text states ({"term", "salience", "source", "quote"}), all
# four required and nothing else, under the object root named change 1 requires. The salience
# bounds are the declared domain `0004_dna.sql:78` states as a CHECK; OpenAI's strict mode cannot
# carry them and they are stripped from its copy, which costs nothing, because the bounds here are
# a request and `verify_payload` refuses a level outside them from every provider alike.
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "salience": {"type": "integer", "minimum": 1, "maximum": 3},
                    "source": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["term", "salience", "source", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tags"],
    "additionalProperties": False,
}

INSTRUCTIONS = """\
You extract Movie DNA: a sparse, evidence-grounded set of tags drawn from a
fixed controlled vocabulary.

Rules, all strict:
1. CLOSED VOCABULARY. Use only term ids listed in the vocabulary, exactly as
   written. Never invent a term. If a quality has no term, omit it.
2. EVIDENCE REQUIRED. Every tag must quote a VERBATIM span from the source text
   (10-15 words is ideal). Copy it character for character. If you cannot quote
   support for a tag, do not emit the tag. Do not paraphrase, do not quote your
   own words, do not rely on your own knowledge of the film.
3. DESCRIBE THE EXPERIENCE, NOT THE ARTIFACT OR ITS RECEPTION. No quality
   judgements ("great acting"), no production facts, no viewer-relative claims
   ("rewatchable", "comfort watch").
4. SALIENCE: 3 = core defining trait, 2 = significant presence, 1 = minor but
   real. Cap salience-3 at 40% of a facet's tags, BUT a facet with 1-2 tags may
   carry one salience-3 — the percentage is an anti-inflation guard, not a ban
   on core traits in small facets.
5. Per-facet maxima (CEILINGS, not targets — a facet the sources never discuss
   should get no tags at all): {ceilings}.
   Ceilings are upper bounds only; do not work toward the ceiling; a
   thinly-discussed film should end up with noticeably fewer tags.
6. Packs marked `[type] series` are television. Describe the show AS A WHOLE
   (per-show DNA; reviews may discuss different seasons). If a show changes
   cast or country between seasons, tag only what is true of ALL of it — the
   invariants — and let structure.anthology carry the format itself. When a
   show pivots (survival story becomes revenge story), tag BOTH rather than
   picking a side.

Return ONLY a JSON object, no prose, no markdown fence, of the form
{{"tags": [<element>, ...]}}. Each element:
{{"term": "<id>", "salience": <1-3>, "source": "<marker like blog:1>",
 "quote": "<verbatim span>"}}\
"""

# Named change 2. `mdc/dna/vocab.py:64-72`'s ceilings, number for number, re-keyed from the
# corpus's extraction labels onto the facet ids this app's `dna_facet` stores - which are its term
# prefixes (`importer/dna.app_facet`, `0018_read_layer.sql`'s backfill). The corpus's labels name
# no facet here, and M4.9 finding 1 measured what importing that naming costs: 29,188 of 31,540
# `dna_tag` rows filed under a facet nothing joins. Each line names the label its number came
# from, so the map can be audited against the file it was read from. The corpus's comment:
#
# Spec §2: per-facet tag ceilings.  Ceilings, never targets - the measured
# effect of treating them as targets was a 2x inflation with no extra signal
# (reports/multipass-test.md).
FACET_MAX: dict[str, int] = {
    "themes": 8,        # narrative_themes
    "structure": 5,     # plot_structure
    "mood": 6,          # mood_tone
    "visual": 5,        # visual_style
    "sound": 4,         # sound_score
    "pacing": 3,        # pacing_energy
    "era": 3,           # setting_era
    "place": 3,         # setting_place
    "characters": 4,    # character_dynamics
    "sensibility": 3,   # sensibility
    "register": 3,      # register_audience
}

# The fixed opening of every retry, `mdc/aspects/prompt.py:674`'s words. Exported because the
# refusing double M5.5's plan phase F builds recognises a retry by it, reading the prompt as a
# provider would - and the corpus's loop sends it after the original user prompt, under the same
# system prompt (`mdc/sources/llm.py:86-87`).
RETRY_MARKER = "Your previous answer was rejected:"

# How many distinct violations one retry names. A retry is a second full input pass, so its
# message is worth a line per violation - and it is still a prompt, whose length is paid for in
# input tokens: past this many, the remainder is counted rather than listed, and the model has the
# rule it keeps breaking several times over already.
MAX_NAMED = 20

# How much of an offending value a line shows, and how long a line may run. Eighty characters is
# the width `verify_payload` already quotes a refused quote at; the line cap bounds a `schema`
# verdict whose detail lists a payload's keys, which the payload chose.
_SHOWN = 80
_LINE = 240

_CORRECTION = (
    "Return the corrected JSON only - the same object, with every term id copied exactly from "
    "the vocabulary, every quote copied verbatim from the source text, and every salience 1, 2 "
    "or 3. Leave out a tag you cannot support rather than repairing it. No prose, no markdown "
    "fence."
)


@dataclass(frozen=True)
class PromptVocabulary:
    """One version's facets and terms, as the prompt shows them. Named change 3.

    `verify.Vocabulary` is the boundary's view of the same rows and carries what a CHECK needs -
    the alias map, the repair index. This carries what a READER needs - the facet order and each
    term's gloss - and neither type borrows the other's, so the prompt cannot come to depend on
    the validator's internals or the reverse.
    """

    version: str
    facets: tuple[str, ...]
    terms: tuple[tuple[str, str, str | None], ...]


async def load_prompt_vocabulary(
    conn: asyncpg.Connection, version: str | None = None
) -> PromptVocabulary | None:
    """One version's vocabulary for the prompt, or None when this install has none.

    §14 risk 7 scopes every DNA read to one version, and `db/dna_terms.active_version` is this
    app's one derivation of which is live, so no literal version appears here. Facets come in
    `dna_facet.ord` order - the order §6.8's palette and every facet list in the app use - where the
    corpus used its own `FACETS` tuple.
    """
    if version is None:
        version = await active_version(conn)
    if version is None:
        return None
    facets = await conn.fetch(
        "SELECT facet FROM dna_facet WHERE version = $1 ORDER BY ord, facet", version
    )
    terms = await conn.fetch(
        "SELECT term, facet, gloss FROM dna_term WHERE version = $1 ORDER BY term", version
    )
    return PromptVocabulary(
        version=version,
        facets=tuple(row["facet"] for row in facets),
        terms=tuple((row["term"], row["facet"], row["gloss"]) for row in terms),
    )


def instructions(voc: PromptVocabulary) -> str:
    """The six rules, with rule 5's ceilings for the facets this vocabulary declares.

    A FACET THE MAP DOES NOT COVER IS SAID TO HAVE NO CEILING, and logged, rather than handed one:
    a number nobody measured, in the one clause whose measured effect was halving the output, is
    the invention decision 343 refuses for prices, in a place where it would be harder to see.
    """
    undeclared = [facet for facet in voc.facets if facet not in FACET_MAX]
    if undeclared:
        log.warning("vocabulary %s declares facets with no ceiling in llm.contract.FACET_MAX: %s; "
                    "the prompt names them without one", voc.version, ", ".join(undeclared))
    ceilings = ", ".join(
        f"{facet} {FACET_MAX[facet]}" if facet in FACET_MAX else f"{facet} (no ceiling declared)"
        for facet in voc.facets
    )
    return INSTRUCTIONS.format(ceilings=ceilings)


def _prefix_of_facet(voc: PromptVocabulary) -> dict[str, str]:
    """The id prefix each facet's terms share, for a facet whose terms share exactly one.

    Derived from the data, never hardcoded, which is the corpus's rule (`mdc/dna/vocab.py:147-148`).
    Where the corpus took the first term's prefix, this takes a prefix only when every term of the
    facet agrees on it: a facet whose ids begin two ways would otherwise have its header claim one
    of them for all.
    """
    heads: dict[str, set[str]] = defaultdict(set)
    for term, facet, _gloss in voc.terms:
        head, dot, _tail = term.partition(".")
        heads[facet].add(head if dot else "")
    return {facet: next(iter(found)) for facet, found in heads.items()
            if len(found) == 1 and "" not in found}


def vocabulary_block(voc: PromptVocabulary) -> str:
    """The closed vocabulary, one section per facet. Named change 3.

    The corpus's docstring calls this "a constant prefix across every call in a run" and advises
    caching it with each provider's prefix cache. No adapter sends a cache directive at M5; the
    advice stands for whoever measures whether it pays.

    THE PREFIX CLAUSE SAYS WHAT IS TRUE OF THIS VOCABULARY. The corpus's sentence - "The id prefix
    is NOT always the facet name" - was true of a file whose facet labels (`plot_structure`) were
    not its id prefixes (`structure.`), and it is kept verbatim for any facet of which it is still
    true. Every facet this app stores IS its prefix (`0018_read_layer.sql`), so here the sentence
    would tell the model something false about the list it is reading, which is the opposite of
    what the measured clause was for: its work was to point at the prefix and say copy it as
    written. So where no facet differs, the same pointer is made as the true statement. An
    absent gloss renders the id alone rather than the word "None".
    """
    by_facet: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    for term, facet, gloss in voc.terms:
        by_facet[facet].append((term, gloss))
    prefix = _prefix_of_facet(voc)
    differ = [f"{facet} uses '{prefix[facet]}.'" for facet in voc.facets
              if facet in prefix and prefix[facet] != facet]
    if differ:
        out = ["VOCABULARY. Copy term ids EXACTLY as written on each line. The id "
               f"prefix is NOT always the facet name (facet {', '.join(differ)}).\n"]
    else:
        out = ["VOCABULARY. Copy term ids EXACTLY as written on each line. The id "
               "prefix is the facet name as each heading below gives it, followed by a dot - "
               "never a longer or reworded facet label.\n"]
    for facet in voc.facets:
        rows = sorted(by_facet.get(facet, []), key=lambda row: row[0])
        if not rows:
            continue
        heading = f"\n## facet {facet}"
        if facet in prefix:
            heading += f" — ids begin '{prefix[facet]}.'"
        out.append(heading)
        out += [f"{tid} — {gloss}" if gloss else tid for tid, gloss in rows]
    return "\n".join(out)


def system_prompt(voc: PromptVocabulary) -> str:
    return instructions(voc) + "\n\n" + vocabulary_block(voc)


def user_prompt(pack_text: str) -> str:
    return "Extract the DNA for this film.\n\n" + pack_text


def prompt_sha(voc: PromptVocabulary) -> str:
    """Identity of the exact instructions + vocabulary a result was produced
    against.  Ingest compares this to the batch manifest.
    """
    return hashlib.sha256(system_prompt(voc).encode("utf-8")).hexdigest()[:16]


def as_verifier_payload(title_id: int, payload: Any) -> dict[str, Any]:
    """One title's answer in `verify_payload`'s shape, `{"titles": {"<id>": tags}}`. Named change 4.

    The object the schema asks for gives its `tags`; a bare array, from a model that ignored the
    root, is taken as the tags it plainly is. ANYTHING ELSE IS HANDED THROUGH AS THIS TITLE'S TAGS,
    unrepaired, so `verify_payload` refuses it under `schema` - the refusal is the validator's to
    make and to record (decision 341), not this reshaping's to pre-empt. And the title key is the
    CALLER'S: an answer shaped like a verifier payload of its own cannot name a title stage 6 did
    not ask about, because it arrives as the value under this one.
    """
    tags = payload["tags"] if isinstance(payload, dict) and "tags" in payload else payload
    return {"titles": {str(title_id): tags}}


def violation_prompt(rejects: Sequence[Rejection], *, version: str) -> str:
    """The retry message: every rule `verify_payload` said was broken, and by what. Plan C4.

    "unknown_term: 'themes.mecha' is not in vocabulary v1" is actionable; "try again" is a second
    full input pass for nothing. So each line is one `Rejection`'s rule and the value it names - the
    term for a vocabulary or duplicate refusal, the quote for an unverified one, verify's own detail
    for a `schema` refusal, which already names the value it refused. Deduplicated, because the
    same refusal twice is one thing to fix; bounded at `MAX_NAMED`, with the rest counted.

    THE VALUES ARE UNTRUSTED TEXT - the model's own output, handed back to it - so each is shown
    through `repr` and cut at a fixed width: a newline or a quote inside one cannot start a line of
    its own and read as a rule this module wrote.

    A retry with nothing to name is refused. Any rejection is a contract violation and no rejection
    is none, so the caller only asks with at least one; asking with none would be the bare "try
    again" this function exists to prevent.
    """
    named = list(dict.fromkeys(_named(reject, version) for reject in rejects))
    if not named:
        raise ValueError("violation_prompt has nothing to name: a retry needs a verdict to cite")
    lines = [f"- {line}" for line in named[:MAX_NAMED]]
    if len(named) > MAX_NAMED:
        lines.append(f"- ... and {len(named) - MAX_NAMED} more")
    return (f"{RETRY_MARKER} it broke the extraction contract, as follows:\n"
            + "\n".join(lines) + "\n\n" + _CORRECTION)


def _named(reject: Rejection, version: str) -> str:
    reason = reject.reason
    if reason == "unknown_term":
        line = f"unknown_term: {_shown(reject.term)} is not in vocabulary {version}"
    elif reason == "adjudicated":
        line = f"adjudicated: {_shown(reject.term)} is retired by this install's curation ledger"
    elif reason == "quote_unverified":
        line = f"quote_unverified: {_shown(reject.quote)} is not in this title's pack{_for(reject)}"
    elif reason == "duplicate":
        line = f"duplicate: {_shown(reject.term)} was emitted more than once for this title"
    else:
        detail = (reject.detail or "refused").replace("\r", "\\r").replace("\n", "\\n")
        line = f"{reason}: {detail}{_for(reject)}"
    return line[:_LINE]


def _for(reject: Rejection) -> str:
    return f" (for {_shown(reject.term)})" if reject.term else ""


def _shown(value: object) -> str:
    if value is None:
        return "(none)"
    text = str(value)
    return repr(text[:_SHOWN]) + ("..." if len(text) > _SHOWN else "")
