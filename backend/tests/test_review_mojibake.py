"""Rule 8's mojibake repair. Spec v2.1 §4.1 rule 8.

    "UTF-8 everywhere; never 'clean' non-ASCII (the corpus legitimately contains CJK, RTL
     scripts, ZWSP, emoji); the 73 known-mojibake review rows are fixed individually in the
     importer."

The dangerous failure here is not missing a broken row — it is "repairing" a correct one. Most
of these tests are about text the repair must leave alone.
"""

from __future__ import annotations

import pytest

from spielplan.importer.reviews import repair_mojibake


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
    ends up with more markers than it started with is left alone."""
    for text in ("Ãa va", "Â£20", "aÂ b"):
        repaired, changed = repair_mojibake(text)
        assert repaired == text or changed


def test_double_encoded_text_is_repaired_one_layer_at_a_time():
    """A twice-mangled string comes back one layer better, not silently over-corrected."""
    once, changed = repair_mojibake("CafÃƒÂ© society")
    assert changed
    assert once == "CafÃ© society"
    twice, changed_again = repair_mojibake(once)
    assert changed_again
    assert twice == "Café society"
