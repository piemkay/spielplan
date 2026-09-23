"""`norm()` entry by entry, and the guard that keeps it the only one. Spec v2.1 §8 stages 5 and 7.

Registered under `data-rules-one-quote-normalisation-for-the-pack-and-the-verifier`.

The function has no branches -- three substitutions applied in order to whatever it is handed --
so there are no paths to walk and the table below IS the coverage: one case per entry in the fold
table, one per markup pattern the ported docstring names, and a completeness check that fails if
`_PUNCT_FOLD` ever grows an entry this file does not exercise. An untested fold entry is exactly
the shape of the bug the fold exists to prevent: nothing crashes, one character simply stops
folding, and the only symptom is a quote-verification rate nobody is watching.

Underneath the table is the property the whole milestone rests on, and it has two halves that
must both hold. A quote transcribed across `**`, `[spoiler]` or a smart apostrophe has to verify
against a pack that carries that markup verbatim -- §8 stage 5 keeps the pack faithful to what
its sources published, so the folding can only happen here, at comparison time. And a quote that
is genuinely absent has to stay absent. The second half is the one §8 stage 7 stands on: a fold
that admitted a fabrication would not be a leniency, it would be a hole in the trust boundary,
which is why the ported docstring argues the point from the shape of the operation rather than
from a sample -- folding can only merge strings that already differed by punctuation.

Then the guard. Two normalisations in one tree is the failure the ported docstring is written to
prevent, and it fails SILENTLY: the pack writer folds one way, the verifier folds another, good
tags are dropped as unverified quotes and nothing raises. So this file reads every module under
`backend/spielplan/` and fails if a second `def norm` appears anywhere -- including as a method,
because a normalisation hidden on a class is the same second normalisation -- and it carries a
self-test over a synthetic tree, because a guard that cannot fail reads as coverage (the
convention `test_landmine_guards.py` states in its own opening).

No database anywhere in this file: `norm()` is a string function and the guard reads source.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from spielplan.dna.norm import _PUNCT_FOLD, norm

PACKAGE = Path(__file__).resolve().parents[1] / "spielplan"
NORM_MODULE = PACKAGE / "dna" / "norm.py"

# One row per entry in `_PUNCT_FOLD`, spelled as escapes rather than as the characters
# themselves: the codepoint is the thing under test, and a reviewer cannot tell U+2018 from
# U+201B by looking at a proportional font. The completeness test below ties this list back to
# the table, so the two cannot drift apart in either direction.
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
    """Each variant, folded in place, with the surrounding text left alone but lowercased."""
    assert norm(f"A{source}B") == f"a{folded}b", (
        f"U+{ord(source):04X} ({name}) did not fold to {folded!r}"
    )


def test_the_case_table_covers_every_fold_entry():
    """Exhaustive by assertion, not by inspection.

    `_PUNCT_FOLD` is imported private on purpose. The alternative is to test the twenty cases
    somebody remembered to list, which passes just as green on a table that has quietly gained a
    twenty-first entry -- and a fold entry nobody exercises is indistinguishable from one that
    does not work.
    """
    covered = {src: folded for src, folded, _name in FOLD_CASES}
    table = {chr(point): folded for point, folded in _PUNCT_FOLD.items()}
    assert covered == table, (
        "the fold table and this file's case list have drifted apart; every entry in "
        "_PUNCT_FOLD needs a row in FOLD_CASES and vice versa"
    )


# The markup arm. `\*{1,3}` covers markdown's italic, bold and bold-italic spellings, `_{2,}`
# covers the double-underscore emphasis while leaving a lone underscore alone, and the BBCode
# spoiler tags are what IMDb and Trakt reviewers wrap an ending in.
MARKUP_CASES = (
    ("**the best film of the year**", "the best film of the year"),
    ("*a single asterisk pair*", "a single asterisk pair"),
    ("***bold italic***", "bold italic"),
    ("*** a scene break ***", "a scene break"),
    ("__doubly underscored__", "doubly underscored"),
    ("[spoiler]the son never comes home[/spoiler]", "the son never comes home"),
    ("[SPOILER]the son never comes home[/Spoiler]", "the son never comes home"),
    # `_{2,}` and not `_+`: a lone underscore is ordinary text, and stripping it here would
    # quietly rewrite every identifier-shaped span a reviewer happens to quote.
    ("a snake_case identifier", "a snake_case identifier"),
)


@pytest.mark.parametrize("source, expected", MARKUP_CASES, ids=[c[0] for c in MARKUP_CASES])
def test_inline_markup_is_stripped(source, expected):
    assert norm(source) == expected


def test_whitespace_collapses_across_newlines_and_tabs():
    assert norm("  The\tstaging\n\nis   theatrical  ") == "the staging is theatrical"


def test_the_result_is_lowercased():
    assert norm("A Fixed Camera") == "a fixed camera"



# Decision 398's rule, one case per family rather than one per code point, because the rule is
# stated over what a character DOES: `str.isprintable()` is false for the categories that render
# nothing, so the fold covers the ones nobody has thought of yet as well as these. The soft hyphen
# is deliberately NOT here -- it is the ported table's entry and is covered above, and the point of
# the rule is the siblings that entry does not carry.
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
    """DECISION 398. Python's whitespace class matches none of these and the ported table folds the soft
    hyphen and none of its siblings, so each survived this function NON-EMPTY -- past the
    verifier's empty-fold refusal, and into a quote that renders as nothing against a pack whose
    own source published the same character. A scraped body carries one routinely: U+200B is a
    line-break hint in HTML and §4.1 rule 8 says the corpus "legitimately contains CJK, RTL
    scripts, ZWSP and emoji"."""
    assert norm(f"A{source}B") == "ab", f"U+{ord(source):04X} ({name}) did not fold away"
    assert norm(source) == "", (
        "a quote made of nothing else has to reach the verifier's empty-fold refusal, which is "
        "where decision 392 put the question"
    )


def test_the_invisible_fold_does_not_eat_the_whitespace_that_separates_two_words():
    """THE ONE WAY THE RULE ABOVE COULD HAVE BEEN WRONG, pinned rather than argued.

    A newline, a tab and a form feed are all non-printable by `str.isprintable()`, and dropping
    them instead of collapsing them would join two words into one -- silently, in both the pack
    and the quote, which is the failure mode this module exists to prevent. They are kept because
    they are whitespace, and the whitespace arm below then makes them one space.
    """
    assert norm("cat\ndog") == "cat dog"
    assert norm("cat\tdog") == "cat dog"
    assert norm("cat\x0cdog") == "cat dog"
    assert norm("cat\x0bdog") == "cat dog"
    assert norm("cat\u00a0dog") == "cat dog"


def test_a_non_string_is_coerced_rather_than_raising():
    """`str(s)` is part of the port, and the reason is a boundary that must not crash.

    A provider that returns `"quote": null` inside an otherwise well-formed payload hands this
    function `None`, and a `norm()` that raised would take a whole batch down over one tag.
    Refusing a non-string quote is the verifier's schema arm, which runs before the substring
    test; this function's job there is to not be the thing that fails.
    """
    assert norm(None) == "none"
    assert norm(3) == "3"


# --- the property the milestone rests on ----------------------------------------------------
#
# A pack fragment in the shape the pack builder will actually produce: review prose with the
# markup its source published still in it. §8 stage 5 is explicit that the pack is not cleaned,
# so every quote an extractor transcribes across that markup has to be reconciled here or not
# at all.
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

# The one class the fold CANNOT keep out of a pack, because there is nothing left of it to
# compare: a quote made of nothing but the markup and punctuation this table removes. The
# assertion below used to state its property universally over the four prose strings above,
# which never reach this class, and the boundary took the universal at its word.
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
    """THE EXCEPTION TO THE TEST ABOVE, PINNED HERE SO NOBODY HAS TO REDISCOVER IT.

    A quote of pure markup folds to the empty string, and `""` is a substring of every text --
    so the substring test admits it, and no fold table can prevent that without giving up the
    7,334 `**` markers this module was measured against. The class belongs to the boundary, which
    refuses a quote whose fold is empty rather than asking whether the pack contains it
    (decision 392, `test_dna_verify.py::test_a_quote_that_folds_to_nothing_is_a_missing_quote`).
    Stating it here is what stops the universal above being read back as covering it.
    """
    assert norm(quote) == ""
    assert norm(quote) in norm(PACK), (
        "if this ever stops being true the boundary's refusal has become belt and braces "
        "rather than the only thing standing between a pack and a quote made of nothing"
    )


# --- the guard: exactly one normalisation in the tree ---------------------------------------


def _functions_named(path: Path, name: str) -> list[int]:
    """Line numbers of every `def name` in one module, at any depth.

    `ast.walk` and not a module-level scan: a second normalisation is at least as likely to
    arrive as a method on a verifier class or a closure inside a loader as it is to arrive at
    the top of a file, and all three are the same bug.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]


def _modules_defining(root: Path, name: str) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if _functions_named(path, name)
    )


def test_exactly_one_module_in_the_package_defines_norm():
    """CLAUDE.md's domain-package rule says where code lives; this says how much of it there is.

    The failure this prevents does not raise. A second fold -- one that forgot the soft hyphen,
    or that also stripped the markup at pack-build time -- makes the pack writer and the quote
    checker disagree about what two strings are, and the whole symptom is tags dropped as
    `quote_unverified` at a rate that looks like a bad provider.
    """
    defining = _modules_defining(PACKAGE, "norm")
    assert defining == ["dna/norm.py"], (
        "exactly one module under backend/spielplan/ may define norm(), and it is "
        "dna/norm.py; found: " + ", ".join(defining) + ". Two normalisations do not crash - "
        "the pack writer and the verifier simply stop agreeing about what two strings are, "
        "and good tags are dropped as unverified quotes with nothing in the log"
    )


def test_the_norm_module_defines_that_one_function_and_nothing_else():
    """One module, one function, so the guard above can stay a name comparison.

    `clean()` and `sha()` are the pack builder's and live with it; the alias map's key
    normalisation is a different function with a different job and is called `alias_key()`.
    Neither belongs beside this one, because a module that accumulates string helpers is a
    module where the second fold eventually gets written.
    """
    tree = ast.parse(NORM_MODULE.read_text(encoding="utf-8"))
    names = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    assert names == ["norm"], (
        "dna/norm.py holds norm() and nothing else; found: " + ", ".join(names)
    )


def test_the_single_definition_guard_sees_a_second_definition(tmp_path):
    """A guard that cannot fail reads as coverage (`test_landmine_guards.py`'s own convention).

    The synthetic tree puts the second definition on a class, which is the spelling a
    module-level scan would have walked straight past.
    """
    (tmp_path / "dna").mkdir()
    (tmp_path / "dna" / "norm.py").write_text("def norm(s):\n    return s\n", encoding="utf-8")
    (tmp_path / "verify.py").write_text(
        "class Checker:\n    def norm(self, s):\n        return s.lower()\n", encoding="utf-8"
    )
    (tmp_path / "innocent.py").write_text("def clean(s):\n    return s\n", encoding="utf-8")
    assert _modules_defining(tmp_path, "norm") == ["dna/norm.py", "verify.py"]
