"""The one normalisation a quote and a pack are compared under. Spec v2.1 §8 stages 5 and 7.

§8 stage 5 lists `norm()` among what is ported into the pack builder (`spec:381`) and §8 stage 7
spends it on "quote-substring-of-pack via norm()" (`spec:389`). The same function on both sides
of the model is the whole reason that substring test means anything: the pack writer decides what
two strings *are*, the verifier decides whether one is inside the other, and if the two answer
that question differently the trust boundary rejects good tags for a reason nobody can see.

So this module exists to hold exactly one function, and it holds nothing else. `clean()` and
`sha()` are the pack builder's and live with it in `packs.py`. The alias map's key normalisation
is a DIFFERENT function with a different job -- it keys a lookup table, it does not compare a
quote to a text -- and it lives in `aliases.py` as `alias_key()`; the two must never be folded
into one, and nothing here is named `normalise` or `_norm`, so that a search for a second
normalisation finds either one module or a bug. `test_dna_norm.py` asserts that over the whole
package rather than leaving it to convention, because the failure is silent: two folds do not
crash, they just stop agreeing, and the symptom is a drop rate that looks like a bad provider.

Ported from `mdc/dna/packs.py:72-99`, verbatim -- the markup pattern, the twenty-entry fold
table, the three-line body and the docstring below, whose measurements are the argument for the
rule rather than decoration on it. A port that keeps the rule and drops the measurement reads as
an assertion where the original was an argument, and the next reader tidying the fold table would
have nothing to argue with. The closing paragraphs of that docstring are this port's additions and
are marked as such; everything above them is the corpus's own text.

The body is one line longer than the ported three, and that line is decision 398's: the ported
table folds a soft hyphen and none of its zero-width siblings, so the fourth line drops what
prints nothing before the three below it read the text. The table itself is untouched -- a fold
entry this app added would be a fold the corpus's measurements do not cover, and the rule belongs
where it can be stated rather than enumerated.
"""

from __future__ import annotations

import re
from typing import Any

_WS_RE = re.compile(r"\s+")

# Inline markup that survives `clean` -- the pack builder's HTML-tag strip -- because it is not
# an HTML tag: markdown emphasis, and the BBCode spoiler tags IMDb and Trakt reviewers use.
_MARKUP_RE = re.compile(r"\*{1,3}|_{2,}|\[/?spoiler\]", re.I)

# Punctuation whose shape varies between a source page and a transcription.
_PUNCT_FOLD = {ord(a): b for a, b in (
    ("‘", "'"), ("’", "'"), ("‚", "'"), ("‛", "'"),
    ("“", '"'), ("”", '"'), ("„", '"'), ("«", '"'),
    ("»", '"'), ("–", "-"), ("—", "-"), ("−", "-"),
    (" ", " "), (" ", " "), (" ", " "), ("­", ""),
    ("…", "..."), ("ʼ", "'"), ("`", "'"), ("´", "'"),
)}


def norm(s: Any) -> str:
    """The quote-verification normalisation: collapse whitespace, lowercase,
    fold the punctuation and inline markup that sources disagree about.

    Every verification path must use this exact function.  A verifier that
    normalises differently from the pack writer rejects good tags.

    **Fold at comparison time, do not strip at pack-build time.**  The packs
    carry what their sources published — markdown bold, BBCode spoiler tags,
    smart quotes — and an extractor transcribing a span across that markup
    produces a quote that is right about the film and wrong about the
    characters.  Measured over the 2026-08-25 runs, this was the single most
    common mechanical failure: 7,334 ``**`` markers and 1,336 ``[spoiler]``
    tags across the library, plus U+2019 in ordinary possessives, and the
    retrieved score-review blocks are worse because those sites are WordPress
    installs with smart quotes on.

    Doing it here rather than in :func:`clean` means the stored text stays
    faithful to the source, the packs do not need rebuilding, and a quote
    verifies whether or not the extractor reproduced the markup — because both
    sides pass through this function.  Folding can only ever merge strings that
    already differed by punctuation, so it cannot admit a quote that is
    genuinely absent.

    This last paragraph is the port's and not the corpus's.  §8 stage 5 names
    ``norm()`` among what is ported into the pack builder and §8 stage 7 spends it on
    "quote-substring-of-pack via norm()", so the requirement three paragraphs up -- every
    verification path must use this exact function -- binds this app and not only the
    corpus that measured it.  A second fold anywhere in the tree would make the pack
    writer and the quote checker disagree about what two strings are, which raises
    nothing; ``test_dna_norm.py`` asserts over the whole package that there is one.

    AND THE PARAGRAPH ABOVE IT HAS ONE DEGENERATE CLASS, WHICH IS THE VERIFIER'S AND NOT
    THIS FUNCTION'S.  "Folding can only ever merge strings that already differed by
    punctuation" is true of every string that has any content left; a string made of
    NOTHING but markup and folded punctuation -- ``**``, ``___``, ``[spoiler]``, a soft
    hyphen -- folds to ``""``, which is a substring of every text there has ever been.
    That is this function working: those characters are exactly what it exists to remove.
    It is not a licence for the substring test, so ``verify_payload`` refuses a quote whose
    fold is empty before it asks whether the pack contains it (decision 392).  Widening the
    fold table cannot make that class smaller and narrowing it would cost the measured
    7,334 ``**`` markers; the refusal is the only place the question belongs.

    AND A CHARACTER THAT PRINTS NOTHING IS NOT PART OF A QUOTE, WHICH IS DECISION 398 AND
    THIS PORT'S SECOND ADDITION.  The paragraph above assumed the two classes were "has
    content left" and "folds to nothing", and a zero-width space sits in neither: Python's
    ``\\s`` does not match U+200B, U+200C, U+200D, U+2060 or U+FEFF, and the ported table
    folds the soft hyphen but none of its siblings -- so a quote made of one ZWSP survived
    this function NON-EMPTY, walked past decision 392's refusal, and was admitted by rule 2
    against any pack carrying the same character.  A scraped review body routinely carries
    one, because U+200B is a line-break hint in HTML and §4.1 rule 8 says the corpus
    "legitimately contains CJK, RTL scripts, ZWSP and emoji".  The rule is stated over what
    a character DOES rather than over a list of the ones somebody thought of, because the
    list was the defect: ``str.isprintable()`` is false for exactly the categories that
    render nothing (Cc, Cf, Cs, Co, Cn and the separators), and the whitespace it also
    covers is kept so that a newline still collapses to a space instead of joining two
    words.  It runs FIRST so every fold below reads the text a reader sees.  This is the
    admitting half as much as the refusing one: a quote transcribed without a source's
    invisible hint now verifies, where before it was dropped as ``quote_unverified``.
    What it does not reach is a character that IS a character and happens to render blank
    -- U+2800, U+3164, a variation selector -- which is the pack's own content and not
    formatting it disagrees with its transcriber about.
    """
    s = "".join(c for c in str(s) if c.isprintable() or c.isspace())
    s = _MARKUP_RE.sub("", s)
    s = s.translate(_PUNCT_FOLD)
    return _WS_RE.sub(" ", s).strip().lower()
