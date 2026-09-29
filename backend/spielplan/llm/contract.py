"""The one extraction contract: its schema, its prompt, and the retry that names a violation.

Two prompt clauses exist because their absence caused measured failures (the prefix warning, the
anti-quota clause); do not "clean up" the prompt without re-running the A/B behind each.
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

# Salience bounds are a request only: the strict copies strip them, `verify_tags` enforces.
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

# Per-facet ceilings, keyed on this app's facet ids; each line names the corpus label it came from.
# Ceilings, never targets: treating them as targets inflated output 2x with no extra signal.
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

# The fixed opening of every retry; test doubles recognise a retry by it.
RETRY_MARKER = "Your previous answer was rejected:"

# Past this many, the rest are counted, not listed: a retry is paid input.
MAX_NAMED = 20

# Width of a shown value, matching `verify_tags`'s quote width.
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
    """One version's facets and terms as the prompt shows them; deliberately not `verify.Vocabulary`."""

    version: str
    facets: tuple[str, ...]
    terms: tuple[tuple[str, str, str | None], ...]


async def load_prompt_vocabulary(
    conn: asyncpg.Connection, version: str | None = None
) -> PromptVocabulary | None:
    """One version's vocabulary for the prompt, or None when this install has none."""
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

    A facet with no measured ceiling is said to have none (and logged), never handed an invented one.
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
    """The id prefix each facet's terms share, for a facet whose terms share exactly one."""
    heads: dict[str, set[str]] = defaultdict(set)
    for term, facet, _gloss in voc.terms:
        head, dot, _tail = term.partition(".")
        heads[facet].add(head if dot else "")
    return {facet: next(iter(found)) for facet, found in heads.items()
            if len(found) == 1 and "" not in found}


def vocabulary_block(voc: PromptVocabulary) -> str:
    """The closed vocabulary, one section per facet.

    The "prefix is not always the facet name" warning is kept only for facets where it is true.
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


def violation_prompt(rejects: Sequence[Rejection], *, version: str) -> str:
    """The retry message: every rule `verify_tags` said was broken, and by what.

    Deduplicated, bounded at `MAX_NAMED`; values are untrusted, so shown via `repr` and cut. Refuses
    to build a retry with nothing to name.
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
