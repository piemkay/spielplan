"""The one normalisation a quote and a pack are compared under (§8 stages 5 and 7).

Must stay the only one: two folds silently disagree and good tags get dropped.
"""

from __future__ import annotations

import re
from typing import Any

_WS_RE = re.compile(r"\s+")

# Markup that survives `clean` because it is not HTML: markdown emphasis and BBCode spoilers.
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

    Every verification path must use this exact function. Fold at comparison time, never strip at
    build time: packs keep what sources published. Characters that print nothing are dropped first
    (decision 398); a quote that folds to "" is refused by `verify_tags` (decision 392).
    """
    s = "".join(c for c in str(s) if c.isprintable() or c.isspace())
    s = _MARKUP_RE.sub("", s)
    s = s.translate(_PUNCT_FOLD)
    return _WS_RE.sub(" ", s).strip().lower()
