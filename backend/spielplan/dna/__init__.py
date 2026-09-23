"""The DNA half of §8 -- the pack, the trust boundary, the projection. Spec v2.1 §8, §9, §4.1.

Three of the ten pipeline stages live here, and they are the three that stand around the one
place in this app where a language model's output becomes a fact:

  * stage 5, `dna pack` (`spec:381`) -- "ported packs.py (interleaving, caps, norm()) + craft
    supplement". The text an extraction pass is allowed to read, kept faithful to what its
    sources published.
  * stage 7, `verify` (`spec:389`) -- "ported trust boundary verbatim ... Failures drop, never
    repaired."
  * stage 8, `project` (`spec:393`) -- "per-title alias-map projection of its keywords
    (incremental - new code, same alias map)".

Stage 6, the extraction itself, is deliberately not here, and must not arrive here. §9 states the
principle the other three are built on -- "The schema is a cost-saving device, not the guarantee
- the guarantee is the validator" (`spec:418`) -- and a validator that shares a package with the
client it judges is one convenience import away from trusting it. The corpus project keeps that
boundary as a fact about its imports rather than as a convention: `mdc/dna/store.py`, which *is*
the trust boundary, imports `adjudication`, `packs`, `similarity` and `vocab`, and does not
import `mdc/llm/client.py` at all. So this package holds no provider call, no API key, no retry
and no prompt. What it offers the milestone that calls a model is a pack to send, a verdict on
what comes back, and a projection that never involved a model in the first place.

Nothing is re-exported from this file, on purpose. A package `__init__` that grows an `__all__`
is where callers stop naming the module they mean, and these are three separate jobs: the pack
builder, the verifier and the projection are imported from `dna.packs`, `dna.verify` and
`dna.project` by the code that wires them (decision 387 leaves that wiring to a later milestone).

**`themes.robots` is a real vocabulary-v1 term, and §8.4 does not say otherwise.**
`artifacts/dna_vocab/v1/vocab_v1_all.tsv:166` carries it -- 582 terms and a header -- split out
of `themes.artificial_intelligence` in the "gap-fill v1.1 2026-08-21" pass, with the split's
reasoning in its own note column: the alias map had been routing every hardware word into the
minds term, so Pacific Rim and Her counted as agreeing about a theme. §8.4's "breadth being the
measured binding constraint (zero owned titles carry `themes.robots`)" (`spec:408`) is therefore
a statement about COVERAGE -- no owned title has been tagged with a term the vocabulary has
carried all along -- and not about a term the vocabulary lacks.

That distinction is worth stating out loud because M5.4 is where somebody first opens the
vocabulary file, and the other reading makes the flywheel look impossible. Decision 163 makes a
vocabulary change a migration rather than an import, so a flywheel that had to *add terms* in
order to grow breadth would be arguing with the decision that froze the vocabulary. It does not
have to: breadth grows by covering terms that already exist across more titles, which is exactly
what §8.4's queue of naming failures feeds. The frozen vocabulary and the growing breadth are
about different things, and the circularity between them is only apparent.
"""
