"""`norm()` entry by entry (§8 stages 5 and 7).
A fold must reconcile markup and still keep a genuinely absent quote absent. No database."""

from __future__ import annotations

import pytest

from spielplan.dna.norm import _PUNCT_FOLD, norm

# Escapes, not the characters: the codepoint is under test and U+2018 and U+201B look alike.
FOLD_CASES = (
    ("‘", "'", "left single quotation mark"),
    ("’", "'", "right single quotation mark"),
    ("‚", "'", "single low-9 quotation mark"),
    ("‛", "'", "single high-reversed-9 quotation mark"),
    ("“", '"', "left double quotation mark"),
    ("”", '"', "right double quotation mark"),
    ("„", '"', "double low-9 quotation mark"),
    ("«", '"', "left-pointing double angle quotation mark"),
    ("»", '"', "right-pointing double angle quotation mark"),
    ("–", "-", "en dash"),
    ("—", "-", "em dash"),
    ("−", "-", "minus sign"),
    (" ", " ", "no-break space"),
    (" ", " ", "narrow no-break space"),
    (" ", " ", "thin space"),
    ("­", "", "soft hyphen"),
    ("…", "...", "horizontal ellipsis"),
    ("ʼ", "'", "modifier letter apostrophe"),
    ("`", "'", "grave accent"),
    ("´", "'", "acute accent"),
)


@pytest.mark.parametrize(
    "source, folded, name",
    FOLD_CASES,
    ids=[f"U+{ord(src):04X}" for src, _folded, _name in FOLD_CASES],
)
def test_every_fold_table_entry_folds(source, folded, name):
    assert norm(f"A{source}B") == f"a{folded}b", (
        f"U+{ord(source):04X} ({name}) did not fold to {folded!r}"
    )


def test_the_case_table_covers_every_fold_entry():
    """`_PUNCT_FOLD` is imported private on purpose, so a new entry nobody exercises fails here."""
    covered = {src: folded for src, folded, _name in FOLD_CASES}
    table = {chr(point): folded for point, folded in _PUNCT_FOLD.items()}
    assert covered == table, (
        "the fold table and this file's case list have drifted apart; every entry in "
        "_PUNCT_FOLD needs a row in FOLD_CASES and vice versa"
    )


# `\*{1,3}` covers markdown emphasis, `_{2,}` double underscores, and the BBCode spoiler tags.
MARKUP_CASES = (
    ("**the best film of the year**", "the best film of the year"),
    ("*a single asterisk pair*", "a single asterisk pair"),
    ("***bold italic***", "bold italic"),
    ("*** a scene break ***", "a scene break"),
    ("__doubly underscored__", "doubly underscored"),
    ("[spoiler]the son never comes home[/spoiler]", "the son never comes home"),
    ("[SPOILER]the son never comes home[/Spoiler]", "the son never comes home"),
    # `_{2,}` and not `_+`: a lone underscore is ordinary text.
    ("a snake_case identifier", "a snake_case identifier"),
)


@pytest.mark.parametrize("source, expected", MARKUP_CASES, ids=[c[0] for c in MARKUP_CASES])
def test_inline_markup_is_stripped(source, expected):
    assert norm(source) == expected


def test_whitespace_collapses_across_newlines_and_tabs():
    assert norm("  The\tstaging\n\nis   theatrical  ") == "the staging is theatrical"


def test_the_result_is_lowercased():
    assert norm("A Fixed Camera") == "a fixed camera"


# Decision 398: one case per family, since the rule is
# `str.isprintable()`; the soft hyphen is covered above.
PRINTS_NOTHING = (
    ("\u200b", "zero width space"),
    ("\u200c", "zero width non-joiner"),
    ("\u200d", "zero width joiner"),
    ("\u2060", "word joiner"),
    ("\ufeff", "zero width no-break space"),
    ("\u180e", "mongolian vowel separator"),
    ("\u200e", "left-to-right mark"),
    ("\u2066", "left-to-right isolate"),
    ("\x07", "bell"),
    ("\u0000", "nul"),
)


@pytest.mark.parametrize(
    "source, name", PRINTS_NOTHING, ids=[f"U+{ord(c):04X}" for c, _n in PRINTS_NOTHING]
)
def test_a_character_that_prints_nothing_folds_away(source, name):
    """U+200B is a routine line-break hint in scraped HTML; §4.1 rule 8 says the corpus contains ZWSP."""
    assert norm(f"A{source}B") == "ab", f"U+{ord(source):04X} ({name}) did not fold away"
    assert norm(source) == "", (
        "a quote made of nothing else has to reach the verifier's empty-fold refusal, which is "
        "where decision 392 put the question"
    )


def test_the_invisible_fold_does_not_eat_the_whitespace_that_separates_two_words():
    """Newline, tab and form feed are non-printable too, but dropping them would join two words."""
    assert norm("cat\ndog") == "cat dog"
    assert norm("cat\tdog") == "cat dog"
    assert norm("cat\x0cdog") == "cat dog"
    assert norm("cat\x0bdog") == "cat dog"
    assert norm("cat\u00a0dog") == "cat dog"


def test_a_non_string_is_coerced_rather_than_raising():
    """A provider's `"quote": null` must not take a whole batch down over one tag."""
    assert norm(None) == "none"
    assert norm(3) == "3"


# A pack fragment with its source's markup still in it: §8 stage 5 does not clean the pack.
PACK = (
    "The critic called it **the best film of the year**, and he was not alone: "
    "[spoiler]the son never comes home[/spoiler], and the last act doesn’t flinch from it. "
    "The staging is theatrical — long takes, a fixed camera — and the colour is "
    "deliberately drained."
)

TRANSCRIBED = (
    # bold markers dropped by the transcription
    "the best film of the year",
    # spoiler tags dropped
    "the son never comes home",
    # U+2019 typed as an ASCII apostrophe
    "the last act doesn't flinch from it",
    # em dashes typed as hyphens, and the sentence's own capital
    "The staging is theatrical - long takes, a fixed camera - and the colour is "
    "deliberately drained.",
)

FABRICATED = (
    # one word changed, which is what a plausible hallucination looks like
    "the daughter never comes home",
    "the best film of the decade",
    "the colour is deliberately saturated",
    # entirely invented, in the register of the pack
    "a masterpiece of controlled grief",
)

# Quotes of pure markup fold to "", which the fold CANNOT keep out; see the test below.
FOLDS_TO_NOTHING = ("**", "___", "[spoiler]", "[/spoiler]", "*", "\u00ad", "**[/spoiler]*")


@pytest.mark.parametrize("quote", TRANSCRIBED)
def test_a_quote_transcribed_across_markup_verifies_against_the_pack(quote):
    assert norm(quote) in norm(PACK), (
        "a quote that differs from the pack only by markup or punctuation must verify; "
        "this is the half of the fold that stops good tags being dropped"
    )


@pytest.mark.parametrize("quote", FABRICATED)
def test_a_quote_that_is_absent_stays_absent_under_the_fold(quote):
    assert norm(quote) not in norm(PACK), (
        "folding merges two strings only where they already differed by punctuation, so a "
        "quote with any content left must not be admitted by it; this is the trust "
        "boundary's half"
    )


@pytest.mark.parametrize("quote", FOLDS_TO_NOTHING)
def test_a_quote_with_nothing_left_after_the_fold_is_the_verifiers_business(quote):
    """`""` is a substring of every text, so the verifier refuses an empty fold (decision 392)."""
    assert norm(quote) == ""
    assert norm(quote) in norm(PACK), (
        "if this ever stops being true the boundary's refusal has become belt and braces "
        "rather than the only thing standing between a pack and a quote made of nothing"
    )
