"""Rule 8's mojibake repair. Spec v2.1 §4.1 rule 8.

    "UTF-8 everywhere; never 'clean' non-ASCII (the corpus legitimately contains CJK, RTL
     scripts, ZWSP, emoji); the 73 known-mojibake review rows are fixed individually in the
     importer."

The dangerous failure here is not missing a broken row — it is "repairing" a correct one. Most
of these tests are about text the repair must leave alone.
"""

from __future__ import annotations

import pytest

from spielplan.importer.reviews import _MOJIBAKE_MARKERS, repair_mojibake


def _markers(text: str) -> int:
    """How many mojibake markers a string carries, counted the way `repair_mojibake` counts."""
    return sum(text.count(m) for m in _MOJIBAKE_MARKERS)


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        ("Itâ€™s a masterpiece", "It’s a masterpiece"),
        ("CafÃ© society", "Café society"),
        ("naÃ¯ve and proud", "naïve and proud"),
        ("BjÃ¶rk sings", "Björk sings"),
        ("Ver Ã¥ret rundt", "Ver året rundt"),
        ("â€“ an em dash", "– an em dash"),
        ("â€œ an opening quote", "“ an opening quote"),
    ],
)
def test_repairs_cp1252_over_utf8(broken, expected):
    repaired, changed = repair_mojibake(broken)
    assert changed
    assert repaired == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "A plain ASCII review.",
        "重慶森林 is the original title",       # CJK
        "الفيلم رائع",                          # RTL
        "a zero​width space",              # ZWSP
        "🎬 a film about films",                # emoji
        "It's a masterpiece",                   # already correct
        "Amélie",                               # already correct
    ],
)
def test_leaves_legitimate_text_exactly_as_it_arrived(text):
    repaired, changed = repair_mojibake(text)
    assert not changed
    assert repaired == text


def test_unmappable_bytes_make_the_repair_decline():
    """cp1252 has no code point at 0x9d, so `â€` (a mangled right curly quote) cannot be
    re-encoded. The conservative choice is to leave it: half-repairing a string is worse than
    not touching it, and these rows are visible in the admin DNA-evidence view."""
    text = "â€\u009dclosing quote"
    repaired, changed = repair_mojibake(text)
    assert not changed
    assert repaired == text


def test_repair_is_idempotent():
    once, _ = repair_mojibake("CafÃ© society")
    twice, changed = repair_mojibake(once)
    assert not changed
    assert twice == once


def test_repair_never_increases_the_mojibake_markers():
    """The guard that stops a 'fix' making things worse — a string that re-encodes cleanly but
    does not strictly reduce its marker count is left alone.

    The body asserted `repaired == text or changed` until M4.9, which is true by construction of
    every return path in `repair_mojibake`: each one is `(text, False)` or `(repaired, True)`, so
    the disjunction cannot be false whatever the function does. With the marker-count guard
    deleted all nineteen invocations in this file still passed. What the guard actually promises
    is the inequality, so that is what is asserted — for the declining case AND for the repairing
    one, because a guard that only ever declines would satisfy the first half alone.
    [M4.9 finding 34]
    """
    for text in ("Ãa va", "Â£20", "aÂ b", "Un film Ãƒ voir", "CafÃ© et thÃ©"):
        repaired, changed = repair_mojibake(text)
        if changed:
            assert _markers(repaired) < _markers(text), (
                f"{text!r} was called repaired without losing a marker"
            )
        else:
            assert repaired == text, f"{text!r} was rewritten while reporting no change"


def test_declines_the_string_the_mutant_half_repairs():
    """The one input that separates the shipped guard from a mutant without it.

    Census over the real `reviews.sqlite`: 485,602 rows, **86 carry a marker, 0 repair** — 82
    fail the UTF-8 decode and 4 the cp1252 encode, because the damage is a truncated sequence
    (`clichÃ`, `dÃbut`) sitting beside legitimately accented text. So no shipped row exercises
    the marker-count guard, and brute force over the marker alphabet found the string that does:
    `'Un film \\u00c3\\u0192 voir'` re-encodes to valid UTF-8 and comes back with the SAME marker
    count, one `Ã` traded for another. A `repair_mojibake` without the guard returns
    `'Un film Ã voir'` and calls it changed — half a repair, which rule 8 calls worse than none
    because the row then looks fixed to every later reader.

    Not parametrised into `test_leaves_legitimate_text_exactly_as_it_arrived`: that test is a
    list of text a human can see is legitimate, and this is a string whose whole interest is that
    it is damaged and still must not be touched. [M4.9 finding 34]
    """
    text = "Un film Ãƒ voir"
    repaired, changed = repair_mojibake(text)

    assert (repaired, changed) == (text, False)
    # The mutant's answer, spelled out so a future reader can see what is being refused rather
    # than trusting the assertion above to have been about something.
    assert text.encode("cp1252").decode("utf-8") == "Un film Ã voir"
    assert _markers("Un film Ã voir") == _markers(text)


def test_double_encoded_text_is_repaired_one_layer_at_a_time():
    """A twice-mangled string comes back one layer better, not silently over-corrected."""
    once, changed = repair_mojibake("CafÃƒÂ© society")
    assert changed
    assert once == "CafÃ© society"
    twice, changed_again = repair_mojibake(once)
    assert changed_again
    assert twice == "Café society"
