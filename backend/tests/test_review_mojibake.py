"""The dangerous failure is repairing a correct string; most cases are text to leave alone."""

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
    """cp1252 has no code point at 0x9d; half-repairing is worse than not touching."""
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
    """The guard's promise is the inequality, asserted for the declining and the repairing case."""
    for text in ("Ãa va", "Â£20", "aÂ b", "Un film Ãƒ voir", "CafÃ© et thÃ©"):
        repaired, changed = repair_mojibake(text)
        if changed:
            assert _markers(repaired) < _markers(text), (
                f"{text!r} was called repaired without losing a marker"
            )
        else:
            assert repaired == text, f"{text!r} was rewritten while reporting no change"


def test_declines_the_string_the_mutant_half_repairs():
    """No shipped row exercises the marker-count guard; this string re-encodes cleanly with the
    SAME marker count, which a guardless repair would call changed."""
    text = "Un film Ãƒ voir"
    repaired, changed = repair_mojibake(text)

    assert (repaired, changed) == (text, False)
    assert text.encode("cp1252").decode("utf-8") == "Un film Ã voir"
    assert _markers("Un film Ã voir") == _markers(text)


def test_double_encoded_text_is_repaired_one_layer_at_a_time():
    once, changed = repair_mojibake("CafÃƒÂ© society")
    assert changed
    assert once == "CafÃ© society"
    twice, changed_again = repair_mojibake(once)
    assert changed_again
    assert twice == "Café society"
