"""Guards over things that are not Python and have no runtime to assert them.

The design language, the compose stack and the CPU-only constraint are all rules the spec
states and nothing enforces. Each of these reads the artifact and fails if the rule slipped.
"""

from __future__ import annotations

import ast
import dataclasses
import io
import operator
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DESIGN = REPO / "frontend" / "src" / "lib" / "design.css"
COMPOSE = REPO / "docker-compose.yml"
DOCKERFILE = REPO / "ops" / "backend.Dockerfile"
PYPROJECT = REPO / "backend" / "pyproject.toml"

# §4.3 / §6.4: vocabulary v1's eleven facets. §6.4's binding note is explicit that the
# prototype's set is NOT this one — it invented dialogue/tone/setting/craft and lacked
# place/era/sensibility/register — and that "everything facet-shaped binds to the real 11".
VOCAB_V1_FACETS = {
    "mood", "themes", "pacing", "structure", "visual", "sound",
    "characters", "place", "era", "sensibility", "register",
}
PROTOTYPE_ONLY_FACETS = {"dialogue", "tone", "setting", "craft"}


def _css() -> str:
    return DESIGN.read_text(encoding="utf-8")


# --- §6.8: the design language ---------------------------------------------------------


def test_facet_colours_are_exactly_vocabulary_v1():
    """§6.8: "A fixed colour per vocabulary facet (11)" — and §6.4 says which eleven."""
    declared = set(re.findall(r"--facet-([a-z-]+):", _css()))
    assert declared == VOCAB_V1_FACETS, (
        f"missing: {sorted(VOCAB_V1_FACETS - declared)}; "
        f"unexpected: {sorted(declared - VOCAB_V1_FACETS)}"
    )


def test_no_prototype_facet_survives():
    """The prototype's invented facets are the specific trap §6.4 warns about."""
    declared = set(re.findall(r"--facet-([a-z-]+):", _css()))
    assert not (declared & PROTOTYPE_ONLY_FACETS)


def test_the_facet_colours_are_distinguishable():
    """Identity colours: the same facet is the same colour on every surface, so two facets
    sharing one makes the map and the silhouette lie."""
    colours = re.findall(r"--facet-[a-z-]+:\s*(#[0-9a-fA-F]{6})", _css())
    assert len(colours) == len(set(c.lower() for c in colours)), "two facets share a colour"


def test_the_ember_accent_is_not_reused_as_a_neutral():
    """§6.8: "one ember accent #c8613a spent on selection and primary actions". It is also
    facet-mood's colour, which is deliberate and the only exception.

    Read through `_ACCENT` from review cycle 2 on, rather than through the lowercase hex literal
    this shipped with since M0. This is the half of the sentence that reads NAMES, and the half
    that reads spends skips every `--` property by design -- so a second token holding the accent
    was answerable to this guard alone, and this guard knew one of its four spellings. Both
    `--badge-bg: rgb(200, 97, 58)` and the plain alias `--badge-bg: var(--ember)` minted the colour
    under a second name with both guards green, and `.badge { background: var(--badge-bg) }` in
    the sheet every surface inherits from is the widening the pair exists to refuse.
    `test_the_design_css_accent_guard_tells_a_spend_from_a_definition` shows all four refused and
    the two alpha derivatives still admitted.
    [§6.8; decision 276 as amended; review cycle 2: acc-c2-02]
    """
    css = _css()
    assert "--ember: #c8613a;" in css
    ember_uses = _design_css_accent_names(_stylesheet(DESIGN))
    assert set(ember_uses) <= {"--ember", "--facet-mood"}, (
        f"these design.css tokens hold the ember accent: {ember_uses}. §6.8 rations the colour to "
        "one name -- `--facet-mood` is #c8613a by §6.4's own binding and is the single exception "
        "(decision 276) -- and a second name for it is a second accent whatever it is called, "
        "because every consumer of the new token spends the one colour the app reserves"
    )


def test_the_data_voice_is_monospace_and_the_display_face_is_not():
    """§6.8: JetBrains Mono for every model number, ID and data annotation; Space Grotesk for
    everything else. A data voice that is not visually distinct is not a voice.

    Which is the half that had never been read here. The declarations were checked and the classes
    were not, so `.why` -- the quiet-reason register, the sentence §6.8 makes the reason a household
    trusts a recommendation at all -- was set in the mono face on 129 elements across 27 files,
    half a pixel from the numbers it was explaining, and this guard called the design language
    intact. §6.8 gives the mono face to "every model number, ID and data annotation" and to nothing
    else; prose is not an annotation. Decision 275 moved `.why` to the display face, so both
    classes are now read -- and read for the declaration that renders rather than for one that is
    merely present, because a face is taken back by a second line under the first. [§6.8; decision 275]

    Registered twice on purpose -- by M0's `map-taste-admin-palette-facet-binding-and-accent` and
    by M4.15's `platform-quiet-reasons-are-prose-and-legible`. Widen it; renaming it takes two
    rows down at once.
    """
    css = _css()
    assert "--mono: 'JetBrains Mono'" in css
    assert "--display: 'Space Grotesk'" in css
    data_rule = re.search(r"\.data\s*\{([^}]+)\}", css)
    assert data_rule and "var(--mono)" in data_rule.group(1)
    why_rule = re.search(r"\.why\s*\{([^}]+)\}", css)
    assert why_rule, "design.css declares no `.why`, so the quiet-reason register has no face"
    # The LAST font-family in each rule, not whether the right one appears somewhere in it. A
    # second declaration under the first is how a face is taken back inside its own rule -- the
    # cascade renders the later one and `in` would read the one that lost.
    faces = {
        name: re.findall(r"font-family\s*:\s*([^;}]+)", body.group(1))[-1].strip()
        for name, body in ((".data", data_rule), (".why", why_rule))
    }
    assert faces == {".data": "var(--mono)", ".why": "var(--display)"}, (
        f"the two voices render as {faces}, and §6.8 gives the mono face to model numbers, ids and "
        "data annotations only -- a quiet reason is prose. Where one quotes a model number, that "
        "fragment wears `.data`, which is two voices inside one sentence rather than one of them "
        "becoming the other"
    )


def test_the_touch_target_rule_applies_beyond_the_nav():
    """§6 preamble: 48 px targets. A rule that lives in one component is not a rule — this is
    the finding that produced the coarse-pointer block."""
    css = _css()
    assert "--touch: 48px;" in css
    coarse = re.search(r"@media \(pointer: coarse\)\s*\{(.+?)\n\}", css, re.S)
    assert coarse, "no coarse-pointer block"
    body = coarse.group(1)
    for primitive in (".pill", ".btn-primary", ".btn-ghost", "select", "input"):
        assert primitive in body, f"{primitive} is not covered by the touch-target rule"


# --- §6.8: a primitive ships its whole box, or every consumer finishes it differently ----

# The three sizes the surfaces converged on once the accidents were told apart from the
# decisions. A fourth is not forbidden — it is a design decision, made in design.css where the
# next reader can see it, rather than a number chosen inside one component's scoped block.
CARD_PAD_TOKENS = ("--card-pad", "--card-pad-tight", "--card-pad-roomy")

# The primitives whose box model design.css owns. Both are checked for shipping one; only
# `card` has its consumers checked against a scale, because only `card` has a scale. `.pill` has
# a single size and one deliberate, commented widening (Tonight's Play CTA, for §6's touch
# floor) — a second one would be the moment to give pills a scale of their own too.
GUARDED_PRIMITIVES = ("card", "pill")
SCALED_PRIMITIVES = ("card",)

# Every way to spell "this element's box": the shorthand and the longhands. `padding-inline`
# alone is enough to move a card off the scale.
_PADDING = re.compile(r"\bpadding(?:-(?:inline|block|top|right|bottom|left))?[a-z-]*\s*:\s*([^;}]+)")


def _style_blocks(source: str) -> str:
    """Every `<style>` in a component, comments stripped.

    `<style>` with no attributes is not the only spelling — `lang=` and a second block both
    exist in the wild, and a guard that captured the first bare one would drop whole files
    silently. The comments go because a rule preceded by one carries it into the selector
    capture below, which is how the first draft of this guard stopped seeing `Onboarding.svelte`.
    """
    blocks = re.findall(r"<style[^>]*>(.*?)</style>", source, re.S)
    return re.sub(r"/\*.*?\*/", "", "\n".join(blocks), flags=re.S)


def _classes(attrs: str) -> set[str]:
    """The class tokens on an element, compared whole.

    `card-wrap` and `wildcard` both contain the word `card`, and a hyphen is a word boundary —
    so `\\bcard\\b` calls `PosterCard.svelte` a card surface when it holds no card at all, and
    then excuses its button reset as if it were a card's padding.
    """
    # Either quote. HTML accepts both and nothing in this repo picks one: `frontend/package.json`
    # has no prettier and no eslint, and `npm run check` is svelte-check alone -- so every class
    # attribute in the tree being double-quoted is a habit and not a constraint, and a single
    # quote took a whole file out of both consumers of this function at once, the card-padding
    # guard and the why-register guard. [review cycle 2: why-c2-01]
    static = re.search(r"class=(\"|')(.*?)\1", attrs)
    names = set(static.group(2).split()) if static else set()
    # `class:card={expr}` is the same claim written as a directive.
    names |= set(re.findall(r"class:([A-Za-z0-9_-]+)", attrs))
    return names


def _selectors_that_style(source: str, primitive: str) -> set[str]:
    """Every selector in this file that can reach an element carrying `primitive`.

    Not a search for `.card {`. Two of the fourteen consumers never write that: the sign-in card
    is a `<form class="card">` styled by the bare `form` selector, and `Onboarding.svelte` styles
    its `<section class="card">` as `section`. Svelte scopes both — `form.s-xyz`, `section.s-xyz`
    — so both outrank design.css, and a guard that looked only for the class would have passed
    the two files most able to drift without anyone seeing it.
    """
    selectors: set[str] = set()
    for tag, attrs in re.findall(r"<([A-Za-z][A-Za-z0-9]*)((?:[^<>\"]|\"[^\"]*\")*)>", source):
        names = _classes(attrs)
        if primitive not in names:
            continue
        selectors.add(tag)
        selectors |= {"." + name for name in names if name != primitive}
    return selectors


def _invented_paddings(root: Path) -> list[str]:
    """Every rule that gives a guarded primitive a box the scale does not name."""
    offenders = []
    for path in sorted(root.rglob("*.svelte")):
        source = path.read_text(encoding="utf-8")
        style = _style_blocks(source)
        for primitive in SCALED_PRIMITIVES:
            reachable = _selectors_that_style(source, primitive)
            if not reachable:
                continue
            reachable.add("." + primitive)
            for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", style):
                # The whole selector part, not its last word: `.rooms li` styles a row INSIDE a
                # card, and Tonight's solo picks are `<li class="card">`, so matching on `li`
                # alone would call every list row a card.
                if not ({part.strip() for part in selector.split(",")} & reachable):
                    continue
                for value in _PADDING.findall(body):
                    if any(token in value for token in CARD_PAD_TOKENS):
                        continue
                    offenders.append(
                        f"{path.name}: {selector.strip()} {{ padding: {value.strip()} }}"
                    )
    return offenders


def _unsigned_exceptions(root: Path) -> list[str]:
    """Uses of a non-default size with no comment saying why.

    The scale stops the vocabulary growing; it does not stop a dense card being quietly promoted
    to the roomy size, which is the same drift one indirection later. An exception has to be
    signed, and the signature is a comment on the line above — the same thing every other
    deliberate deviation in this repository carries.
    """
    unsigned = []
    for path in sorted(root.rglob("*.svelte")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not any(t in line for t in ("--card-pad-tight", "--card-pad-roomy")):
                continue
            above = lines[i - 1].strip() if i else ""
            if not (above.startswith("/*") or above.startswith("*") or above.endswith("*/")):
                unsigned.append(f"{path.name}:{i + 1}: {line.strip()}")
    return unsigned


def test_the_card_primitive_ships_its_whole_box_model():
    """§6.8 is a claim about one surface reading like the next, and a primitive that hands out
    half of itself cannot keep it.

    `.card` shipped background, border and radius and no padding. Fourteen surfaces finished it
    seven ways — 30, 26, 18/20, 16/18, 14, 12/14, 12/13 — and the fifteenth finished it not at
    all, drawing its text on its own border for a whole milestone before anybody looked. `.pill`
    is the control group in the same stylesheet: it ships its padding and is redefined nowhere.
    """
    css = _css()
    for primitive in GUARDED_PRIMITIVES:
        rule = re.search(r"\." + primitive + r" \{(.*?)\}", css, re.S)
        assert rule, f"no `.{primitive}` rule in design.css"
        assert "padding" in rule.group(1), (
            f"`.{primitive}` hands out a skin with no box model, so every consumer has to finish it"
        )
    for token in CARD_PAD_TOKENS:
        assert f"{token}:" in css, f"the card scale is missing {token}"


def test_no_surface_invents_its_own_card_padding():
    """A surface may still give a card a different box — a sign-in form that IS the screen wants
    more room than one of five stacked rows. What it may not do is invent the number.

    This is the failure mode `e2e/specs/13-rank.spec.js` was written for, one property over: nine
    controls shipped at 32-36 px because a scoped rule outranks design.css's coarse-pointer
    floor, invisible to a suite that never measured one. Scoped CSS means the local rule wins
    silently — no conflict, no warning — so nothing but a reader ever notices.
    """
    offenders = _invented_paddings(REPO / "frontend" / "src")
    assert not offenders, (
        "a card's padding is a decision with three names, not a number to pick:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse var(--card-pad), var(--card-pad-tight) or var(--card-pad-roomy). A size none of"
        " them fits is a change to the scale in design.css, where the next reader can see it."
    )


def test_every_card_size_that_is_not_the_default_says_why():
    """The scale keeps the vocabulary three wide; this keeps it honest.

    Nothing stops a dense card being quietly promoted to the roomy size, which is the same drift
    the scale exists to end, one indirection later. So a deviation is signed.
    """
    unsigned = _unsigned_exceptions(REPO / "frontend" / "src")
    assert not unsigned, (
        "a card that is not the default size says why, in a comment above the line:\n  "
        + "\n  ".join(unsigned)
    )


@pytest.mark.parametrize(
    ("name", "markup", "rule"),
    [
        ("by its class", '<div class="card">x</div>', ".card { padding: 19px; }"),
        ("by its tag", '<form class="card">x</form>', "form { padding: 21px; }"),
        ("by a companion class", '<div class="drained card">x</div>', ".drained { padding: 23px; }"),
        ("with no trailing semicolon", '<div class="card">x</div>', ".card { padding: 19px }"),
        ("by a longhand", '<div class="card">x</div>', ".card { padding-inline: 26px; }"),
        ("padded to nothing", '<div class="card">x</div>', ".card { padding: 0; }"),
    ],
)
def test_the_card_padding_guard_catches_a_real_violation(tmp_path, name, markup, rule):
    """docs/TESTING.md: "A guard needs a self-test … a guard that cannot fail reads as coverage
    while providing none."

    Seven shapes, and five of them are ones an earlier draft of this guard passed. `padding: 0`
    is in the list because a card padded to nothing is not an exemption, it is the original bug:
    Tonight shipped a whole milestone drawing its text on its own border.
    """
    (tmp_path / "Surface.svelte").write_text(
        markup + "\n<style>\n  " + rule + "\n</style>\n", encoding="utf-8"
    )
    assert _invented_paddings(tmp_path), f"a card padded {name} went unnoticed"


def test_the_card_padding_guard_leaves_innocent_files_alone(tmp_path):
    """The other half of a guard's self-test: what it must NOT say.

    `card-wrap` and `wildcard` both contain the word, and a hyphen is a word boundary — so a
    guard matching `\\bcard\\b` calls `PosterCard.svelte` a card surface when it holds no card,
    and then reads its button reset as a card's padding.
    """
    (tmp_path / "Wrap.svelte").write_text(
        '<button class="card-wrap">x</button>\n<style>\n  .card-wrap { padding: 0; }\n</style>\n',
        encoding="utf-8",
    )
    (tmp_path / "Rows.svelte").write_text(
        '<ul><li class="card">x</li></ul>\n<style>\n  .card { padding: var(--card-pad); }\n'
        "  .rooms li { padding: 8px 0; }\n</style>\n",
        encoding="utf-8",
    )
    (tmp_path / "Attributed.svelte").write_text(
        '<div class="card">x</div>\n<style lang="css">\n  .card { padding: var(--card-pad-roomy); }\n'
        "</style>\n",
        encoding="utf-8",
    )
    assert _invented_paddings(tmp_path) == []


def test_the_signed_exception_guard_catches_an_unsigned_one(tmp_path):
    """And the signature guard needs its own violation, for the same reason."""
    (tmp_path / "Unsigned.svelte").write_text(
        "<style>\n  .drained { padding: var(--card-pad-roomy); }\n</style>\n", encoding="utf-8"
    )
    (tmp_path / "Signed.svelte").write_text(
        "<style>\n  .drained {\n    /* An empty state: it gets room. */\n"
        "    padding: var(--card-pad-roomy);\n  }\n</style>\n",
        encoding="utf-8",
    )
    caught = _unsigned_exceptions(tmp_path)
    assert len(caught) == 1 and "Unsigned.svelte" in caught[0], caught


# --- §6.8, §6.0: the facet palette and the card that renders it -------------------------

# The five places vocabulary v1's facet names are written down outside the corpus. They are one
# vocabulary spelled five times, which is why they are guarded together: for the whole life of
# the app before M4.9 they agreed with each other and disagreed with the data, so every
# consistency check passed while ten of the eleven facets rendered neutral. A guard over one
# site would have said the same thing. [M4.9 findings 3 and 4]
FRONTEND = REPO / "frontend" / "src"
HOME_MODULE = FRONTEND / "lib" / "home.svelte.js"
RATE_MODULE = FRONTEND / "lib" / "rate.svelte.js"
TITLE_DETAIL = FRONTEND / "lib" / "components" / "TitleDetail.svelte"
SHELF_ROW = FRONTEND / "lib" / "components" / "ShelfRow.svelte"
POSTER_CARD = FRONTEND / "lib" / "components" / "PosterCard.svelte"
MODEL_RAIL = FRONTEND / "lib" / "components" / "ModelRail.svelte"
DNA_IMPORTER = REPO / "backend" / "spielplan" / "importer" / "dna.py"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _frontend_sources() -> list[Path]:
    """The shipped frontend. Colocated `*.test.js` are excluded on purpose: they are falsifiers,
    not palette sites, and the one that names `character` names it to assert that the singular is
    NOT a facet and resolves to the neutral. Vitest is what reads those files."""
    return [
        p for p in sorted(FRONTEND.rglob("*"))
        if p.is_file() and p.suffix in {".css", ".js", ".svelte"} and not p.name.endswith(".test.js")
    ]


def _declared_facets() -> set[str]:
    return set(re.findall(r"--facet-([a-z]+)\s*:", _css()))


def _facets_used_as_vars(source: str) -> set[str]:
    """Every `var(--facet-x)` a component spends. A component names the facets its own subject
    has, not all eleven, so this is read as a subset rather than as a second palette."""
    return set(re.findall(r"var\(--facet-([a-z]+)\)", source))


def _js_string_set(source: str, name: str) -> set[str]:
    """The single-quoted members of a `const NAME = new Set([...])` literal."""
    block = re.search(re.escape(name) + r"\s*=\s*new Set\(\[(.*?)\]\)", source, re.S)
    assert block, f"no `{name} = new Set([...])` to read"
    return set(re.findall(r"'([a-z_]+)'", block.group(1)))


def _default_facet_colours() -> set[str]:
    """`DEFAULT_FACET_COLOURS`'s keys, read from the AST rather than from the text: the dict is
    written several pairs to a line and a regex over it would silently read half of it."""
    tree = ast.parse(_src(DNA_IMPORTER))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "DEFAULT_FACET_COLOURS" for t in node.targets):
            continue
        assert isinstance(node.value, ast.Dict)
        return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError("importer/dna.py no longer defines DEFAULT_FACET_COLOURS")


def _spelt_singular(source: str) -> list[int]:
    """Line numbers where a facet is still named `character`. The lookahead matters: the correct
    spelling has the wrong one as a prefix, so a substring test can never be satisfied."""
    return [
        source[: m.start()].count("\n") + 1
        for m in re.finditer(r"--facet-character(?![a-z])|'character'", source)
    ]


def test_every_facet_palette_site_names_the_shipped_eleven():
    """§6.8: "a fixed colour per vocabulary facet (11)" — and the eleven are the shipped ones.

    The vocabulary directory ships `vocab_characters_v1.tsv` and every shipped term prefix is
    `characters`; seven source sites spelled it `character`, including the set the guard above
    compares design.css against. So the palette was internally consistent, externally wrong, and
    the guard was the thing that would have rejected the repair. This reads all five sites at
    once because the failure mode is not "a site is wrong" but "the sites agree and the data
    does not". [M4.9 finding 4; the editorial licence in M4.9-plan.md §1]

    The three complete palettes must BE the eleven. `ModelRail` is read as a subset on purpose:
    its rules colour event kinds, and it has thirteen kinds to spend nine facets on. Requiring
    equality there would be a rule the file cannot satisfy, which is the kind of guard that gets
    weakened rather than obeyed.
    """
    complete = {
        "design.css": _declared_facets(),
        "home.svelte.js FACETS": _js_string_set(_src(HOME_MODULE), "FACETS"),
        "importer/dna.py DEFAULT_FACET_COLOURS": _default_facet_colours(),
    }
    for label, names in complete.items():
        assert names == VOCAB_V1_FACETS, (
            f"{label} does not name vocabulary v1: missing {sorted(VOCAB_V1_FACETS - names)}, "
            f"unexpected {sorted(names - VOCAB_V1_FACETS)}"
        )

    rail = _facets_used_as_vars(_src(MODEL_RAIL))
    assert rail and rail <= VOCAB_V1_FACETS, (
        "ModelRail colours an event kind with a facet that is not one of the eleven: "
        f"{sorted(rail - VOCAB_V1_FACETS)}"
    )

    # TitleDetail held a second copy of the set and a second `facetColour`. Two spellings of one
    # palette is how one of them rots -- and the one that rotted was the copy, not the shared
    # module. There is one palette function, and every surface imports it.
    detail = _src(TITLE_DETAIL)
    assert not re.search(r"\bconst\s+FACETS\b", detail), (
        "TitleDetail declares its own facet set again; import facetColour from $lib/home.svelte.js"
    )
    assert not re.search(r"\bconst\s+facetColour\s*=", detail), (
        "TitleDetail defines a second facetColour; there is one, in $lib/home.svelte.js"
    )
    assert re.search(
        r"import\s*\{[^}]*\bfacetColour\b[^}]*\}\s*from\s*'\$lib/home\.svelte\.js'", detail, re.S
    ), "TitleDetail must import facetColour from $lib/home.svelte.js"

    # The singular is not a spelling variant, it is a facet no row can ever carry.
    singular = [
        f"{path.relative_to(REPO).as_posix()}:{line}"
        for path in _frontend_sources()
        for line in _spelt_singular(_src(path))
    ]
    assert not singular, (
        "the shipped facet id is `characters` (vocab_characters_v1.tsv); still singular at:\n  "
        + "\n  ".join(singular)
    )


# §4.1 rule 1 and §6.6: two tiers, never merged, and a parallel extraction mode that writes a
# second row for one term. `dna_tag`'s uniqueness is (title_id, version, term, provider), so the
# term alone was never a key -- and Svelte 5 raises `each_key_duplicate` in the production build
# too, which took the whole panel down mid-render rather than dropping a chip. [M4.9 finding 8]
#
# And the facet is not what separates those two rows. Since 0018 section 1 `facet` IS
# `split_part(term, '.', 1)`, so `facet + ':' + term` discriminates exactly as well as the term
# alone did -- the provider is the column that varies, which is what 0018 section 2 armed the
# unique index for. Only `dna_tag` has one: `dna_projected` is UNIQUE (title_id, version, term)
# and the shelf's `shared_terms` arrive already grouped by term (`home/why.py`), so those two
# blocks are safe for a reason that is theirs and not the key's.
# [M4.9 review cycle 1: M49-REV1-01]
_EACH_HEAD = re.compile(r"\{#each\s+(?P<head>[^{}]+?)\s*\}")
_DNA_COLLECTION = re.compile(r"\.dna\.|shared_terms")
_TAG_COLLECTION = re.compile(r"\.dna\.extracted")


def _dna_each_keys(source: str) -> list[str]:
    """Every DNA each-block in `source` whose key cannot tell its own rows apart, as complaints.

    A block counts as DNA either by its collection or by its key naming `.term` -- the second
    clause is what makes a regression to `(tag.term)` fail rather than merely go unclassified.
    """
    offenders = []
    for m in _EACH_HEAD.finditer(source):
        head = " ".join(m.group("head").split())
        collection, _, binding = head.partition(" as ")
        keyed = binding.endswith(")") and "(" in binding
        key = binding[binding.index("(") + 1: -1] if keyed else ""
        if not (_DNA_COLLECTION.search(collection) or ".term" in key):
            continue
        line = source[: m.start()].count("\n") + 1
        if ".facet" not in key or ".term" not in key:
            offenders.append(f"line {line}: {{#each {head}}} keys on {key or 'nothing'}")
        elif _TAG_COLLECTION.search(collection) and ".provider" not in key:
            offenders.append(f"line {line}: {{#each {head}}} keys a dna_tag row without .provider")
        elif not re.search(r"""['"][^'"]+['"]""", key):
            offenders.append(f"line {line}: {{#each {head}}} concatenates without a delimiter")
    return offenders


def test_every_dna_each_block_keys_on_facet_and_term():
    """The DNA blocks key on facet AND term, delimited -- the shape the platform-scores block
    twenty lines away already uses, and whose comment records that this exact crash shipped once.
    The extracted tier keys on its provider as well, because that is the component two rows of
    one term actually differ in and the facet, since 0018, is the term's own prefix.

    Not de-duplicated in the client: §4.1 rule 1 and §6.6 both want both rows visible. The key is
    what makes two rows two rows.
    """
    offenders = []
    for path in (TITLE_DETAIL, SHELF_ROW):
        offenders += [f"{path.name} {complaint}" for complaint in _dna_each_keys(_src(path))]
    assert not offenders, (
        "a DNA each-block is keyed on something that is not unique per row:\n  "
        + "\n  ".join(offenders)
        + "\n\ndna_tag is unique on (title_id, version, term, provider) and 0018 derives the"
        " facet from the term, so the provider is what separates two opinions of one term. Key"
        " the extracted tier on `tag.facet + ':' + tag.term + ':' + tag.provider`."
    )


@pytest.mark.parametrize(
    ("name", "markup"),
    [
        ("on the term alone", "{#each data.dna.extracted as tag (tag.term)}"),
        ("on the facet alone", "{#each section.shared_terms as t (t.facet)}"),
        ("on nothing at all", "{#each data.dna.projected as p}"),
        ("on both, undelimited", "{#each section.shared_terms as t (t.facet + t.term)}"),
        ("on the index", "{#each data.dna.projected as p, i (i)}"),
        ("across a line break", "{#each data.dna.extracted as tag\n  (tag.term)}"),
        (
            "on a facet the term already carries",
            "{#each data.dna.extracted as tag (tag.facet + ':' + tag.term)}",
        ),
    ],
)
def test_the_dna_key_guard_catches_a_real_violation(name, markup):
    """docs/TESTING.md: "a guard that cannot fail reads as coverage while providing none."

    The term alone is what all three blocks keyed on before M4.9; the last case is what M4.9 put
    in their place, and it belongs here because 0018 section 1 derives the facet from the term --
    so on the one block a second provider can duplicate, that key discriminates no better than
    the one it replaced. `(i)` is here because an index key is the tempting fix for a
    duplicate-key crash and is the worse one: it makes Svelte keep the wrong node when a row is
    inserted, which is the failure the credits block's own comment records.
    [M4.9 finding 8; review cycle 1: M49-REV1-01]
    """
    assert _dna_each_keys(markup), f"a DNA each keyed {name} went unnoticed"


def test_the_dna_key_guard_leaves_the_other_each_blocks_alone():
    """The other half of a guard's self-test: what it must NOT say. The platform-scores block is
    already correct and must not be reported; the shelf's poster row keys on a title id, which is
    not a DNA row and has no facet to name. The projected tier and the shelf's shared terms are
    not asked for a provider: neither table has one, and `dna_projected` is unique on the term."""
    innocent = (
        "{#each data.platform_ratings.items as p (p.platform + ':' + p.metric)}\n"
        "{#each section.items as item (item.title_id)}\n"
        "{#each tag.evidence as e}\n"
        "{#each data.dna.extracted as tag (tag.facet + ':' + tag.term + ':' + tag.provider)}\n"
        "{#each data.dna.projected as p (p.facet + ':' + p.term)}\n"
        "{#each section.shared_terms as t (t.facet + ':' + t.term)}\n"
    )
    assert _dna_each_keys(innocent) == []


# §8 stage 10's badge. The expression is pinned rather than described because the fallback it
# replaced is the natural thing to write: `title.placement === 'cold_tower'` reads correctly and
# is false 111 times out of 130 on the reference library, since 0008_placement.sql stamps
# cold_tower on any title with a Backbone row and item_n < 90. [M4.9 finding 18]
_COLD_BADGE = (
    "title.e_source ? title.e_source === 'cold_tower' "
    ": title.item_n === 0 || (title.item_n == null && title.placement === 'cold_tower')"
)


def test_the_cold_badge_expression_reads_e_source_not_placement():
    """PosterCard's `noCrowdData` was already right, and this is what keeps it right.

    Home's payload is what changed under it: `e_source` and `item_n` used to sit inside
    `card["model"]`, which decision 117 strips wholesale, so `placement` was the only branch the
    shelf could reach. With the payload repaired, an edit that "simplifies" the expression back
    to the placement test would be invisible again -- the badge would simply be wrong, on a
    surface with no assertion that reads it. The comment beneath it IS the specification and is
    pinned with it.

    The expression moved in M4.15 and this guard followed it rather than being relaxed. It was
    inline in `$derived(...)` while the card was its only reader; the shelf now asks the same
    question, to carry the badge's one-line why once for the row instead of in a `title=` tooltip
    no phone can open (finding 10, decision 278). A second copy of the test is exactly what the
    comment beneath it forbids, so there is one `isColdPlaced` and both readers call it -- which
    is what keeps this pin over both of them.
    """
    poster = _src(POSTER_CARD)
    stated = re.search(
        r"export function isColdPlaced\(title\) \{\s*return (?P<expr>.*?);\s*\}", poster, re.S
    )
    assert stated, "PosterCard no longer states the cold-placement test as one exported expression"
    assert "$derived(isColdPlaced(title))" in poster, (
        "the badge no longer reads the expression this guard pins, so the pin holds nothing"
    )
    assert " ".join(stated.group("expr").split()) == _COLD_BADGE, (
        "the no-crowd-data badge is off `e_source`/`item_n`, NOT off `title.placement`:\n"
        f"  found:  {' '.join(stated.group('expr').split())}\n  wanted: {_COLD_BADGE}"
    )
    assert "Off `e_source`/`item_n`, NOT off `title.placement`." in poster, (
        "the comment stating the rule is the specification and travels with the expression"
    )


# The sentence decision 278 moved out of the tooltip, matched on the half that carries no em dash
# so a failure message stays ASCII on a cp1252 console.
COLD_REASON = 'Cards marked "new" are placed by the Cold Tower'


def test_every_surface_that_draws_the_cold_badge_states_its_reason():
    """§6.8's quiet reasons, on the form factor a `title=` attribute does not exist on.

    Decision 278 took the cold badge's explanation out of the hover and gave it to the SHELF, once
    for the row rather than once per card -- twelve identical sentences down a row is the noise the
    quiet-reason register exists to avoid. `PosterCard` renders on TWO surfaces, though, and the
    second is Home's catalog grid: reached by the search box, a filter chip or a tapped credit, and
    drawing no shelf header at all. There the chip went back to being an unexplained word on a
    poster, which is finding 10's own defect surviving its repair, on the form factor §6's
    preamble makes primary.

    So the rule is per SURFACE rather than per component, and this asks every file that draws the
    card whether it also draws the sentence. The tooltip is not an answer and is not counted: a
    hover is a pointer affordance, and this is the rule for fingers.
    [§6.8; decision 278; M4.15 review cycle 2: M415-C2-COMP-06]
    """
    silent = []
    for path in sorted(FRONTEND.rglob("*.svelte")):
        source = _src(path)
        if "<PosterCard" in source and COLD_REASON not in source:
            silent.append(path.relative_to(REPO).as_posix())
    assert not silent, (
        "these surfaces draw the cold badge and carry no reachable reason for it: "
        + ", ".join(silent)
        + ". The chip's sentence belongs to the surface (decision 278) -- carry it once above the "
        "cards, the way `ShelfRow` carries it in the row header. A `title=` tooltip is not the "
        "answer: it does not exist on a phone, which is the whole of finding 10."
    )


# §6.0's metadata line and proposal 27's data voice. One label, every surface. [M4.9 finding 37]
_RUNTIME_BRANCH = "return h ? `${h}h ${m}m` : `${m}m`;"

# A title's runtime, read by a surface. The sweep below was shaped like the expression that had
# been removed -- `runtime_min / 60` -- rather than like the rule its own message states, so it
# was green on the two ways of formatting a runtime that do not divide: through a local alias,
# and not at all. Tonight's reveal rendered `{winner?.runtime_min} min` under it for the whole
# milestone, which is finding 37's own defect (a series printed as flat minutes, a NULL runtime
# printed as a bare unit) on a fourth surface. [M4.9 review cycle 1: M49-CARD-2]
_RUNTIME_READ = re.compile(r"(?:\?\.|\.)runtime_min\b")
# The one legal read outside the label: carrying the field forward under its own name, which
# neither formats it nor prints it. `home.svelte.js`'s `toPosterTitle` hands it to `PosterCard`,
# which calls `runtimeLabel`. A filter key spelled `'runtime_min'` (`rank.svelte.js`) is not a
# read of a title at all and never matches.
_RUNTIME_CARRIED = re.compile(r"\bruntime_min\s*:\s*[\w?.\[\]]*\.runtime_min\b")


# Commentary, in the three forms the frontend writes it. Blanked rather than deleted, because
# the sweep below reports line numbers and a stripper that shortened the file would report the
# wrong ones. This codebase quotes the expression a surface stopped using in the sentence saying
# why it stopped -- `ShelfRow.svelte` does, and so does the import comment this rule was written
# for -- so a sweep over the raw file reports the repair as the defect.
_COMMENTARY = re.compile(r"<!--.*?-->|/\*.*?\*/|//[^\n]*", re.S)


def _runtime_reads(source: str) -> list[int]:
    """The 1-based lines on which `source` reads a title's runtime other than to pass it on."""
    code = _COMMENTARY.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), source)
    carried = [m.span() for m in _RUNTIME_CARRIED.finditer(code)]
    return [
        code[: m.start()].count("\n") + 1
        for m in _RUNTIME_READ.finditer(code)
        if not any(start <= m.start() and m.end() <= end for start, end in carried)
    ]


def test_one_runtime_label_serves_every_surface():
    """A series read `2017 - 0h 24m - series` two taps after a poster that said `24m/ep`.

    Three open-coded copies, one of which had never grown the kind branch: the title card's. The
    zero-hour branch is here rather than in a formatting module because there is one function,
    and a `format.js` holding it would be an abstraction invented for a single caller.

    The sweep is over every read of the field rather than over the shape of the division,
    because the defect is a surface holding a runtime and deciding for itself what it says --
    `${title.runtime_min} min` is that defect with no arithmetic in it at all.
    """
    rate = _src(RATE_MODULE)
    assert len(re.findall(r"export function runtimeLabel\b", rate)) == 1, (
        "there is exactly one runtimeLabel and rate.svelte.js exports it"
    )
    body = re.search(r"export function runtimeLabel\b.*?\n\}", rate, re.S)
    assert body and _RUNTIME_BRANCH in body.group(0), (
        "runtimeLabel must not print a leading `0h`: 240 of 13,324 corpus movies run under an "
        f"hour. Wanted: {_RUNTIME_BRANCH}"
    )

    open_coded = [
        f"{path.relative_to(REPO).as_posix()}:{line}"
        for path in _frontend_sources()
        if path != RATE_MODULE
        for line in _runtime_reads(_src(path))
    ]
    assert not open_coded, (
        "a surface holds a title's runtime and decides for itself what it says; hand the title "
        "to runtimeLabel or metaLine from $lib/rate.svelte.js, or carry the field on under its "
        "own name (`runtime_min: title.runtime_min`):\n  " + "\n  ".join(open_coded)
    )
    for path in (TITLE_DETAIL, POSTER_CARD):
        assert re.search(
            r"import\s*\{[^}]*\bruntimeLabel\b[^}]*\}\s*from\s*'\$lib/rate\.svelte\.js'",
            _src(path),
            re.S,
        ), f"{path.name} must read the shared runtimeLabel"


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        # The copy that shipped, and the two the old `runtime_min / 60` sweep was green on.
        ("the division that shipped", "const h = Math.floor(data.title.runtime_min / 60);", 1),
        ("the same division through an alias", "const m = t.runtime_min;\nMath.floor(m / 60);", 1),
        ("a runtime rendered with no arithmetic", "<p>{winner?.runtime_min} min</p>", 1),
        # And the reads that are not a surface formatting anything.
        ("the field carried forward", "runtime_min: item.runtime_min,", 0),
        ("carried forward from an optional", "runtime_min: item?.title.runtime_min,", 0),
        ("a filter key of the same name", "if (key === 'runtime_min') return `${v} min`;", 0),
        ("the title handed to the label", "const minutes = $derived(runtimeLabel(title));", 0),
        # The sentence saying why a surface stopped open-coding one is not a surface doing it.
        ("the repair quoted in a comment", "// it spelled `{w?.runtime_min} min` itself", 0),
        ("the same, in markup", "<!-- was {winner.runtime_min} min -->", 0),
    ],
)
def test_the_runtime_sweep_catches_the_copies_it_is_written_for(name, source, expected):
    assert len(_runtime_reads(source + "\n")) == expected, name


# --- §1, §2: the stack -----------------------------------------------------------------

# Enough YAML to see a rule, and no more. Three of the guards below read *one part* of
# docker-compose.yml, and reading the whole file as text is exactly how two of them stopped
# working: `assert "/data/backups" in compose` passes on a file whose mount is commented out and
# on a file that is nothing but comments, and a `ports` sweep over the whole text cannot tell the
# backend's one published port from a second one added beside it. PyYAML is not a test dependency
# and this repository will not add one to read six lines; what these need is the block structure
# and the anchors, which indentation gives for free. Comments are stripped on the way through,
# which is the entire point — a mount that was commented out is a mount that is not there.
# [M4.7 ddocs-08, tq3-one-published-port]

# §1 names /data/pg, /data/raw, /data/artifacts and /data/cache; §2's nightly pg_dump adds
# /data/backups. `db` mounts the same host directory at /backups, which is the read side of the
# restore, so the property is over the stack rather than over any one service.
SPEC_VOLUMES = {
    "/var/lib/postgresql/data", "/data/raw", "/data/artifacts", "/data/cache", "/data/backups",
}

# The mode is an option list and it is not lower-case: `:Z` is the unshared half of the SELinux
# relabel pair, and a mode group that could not spell it made the whole line fail to match — so a
# mount carrying one was not a read-only mount, it was no mount at all. [M4.7 cycle 3 f15]
_MOUNT = re.compile(
    r"^\s*-\s*(?P<host>[^\s:]+):(?P<container>/[^\s:]*)(?::(?P<mode>[A-Za-z,]+))?\s*$", re.M
)


def _compose() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def _nested(text: str, key: str) -> str:
    """The lines nested under `key:`, by indentation, with comments removed.

    Returns the header line too, so `volumes: *worker-volumes` is readable off the result.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        head = line.split("#", 1)[0].rstrip()
        if head.strip() != f"{key}:" and not head.strip().startswith(f"{key}: "):
            continue
        indent = len(head) - len(head.lstrip())
        body = [head]
        for follow in lines[i + 1:]:
            if follow.strip() and len(follow) - len(follow.lstrip()) <= indent:
                break
            body.append(follow.split("#", 1)[0].rstrip())
        return "\n".join(body)
    return ""


def _service_names(compose: str) -> list[str]:
    assert "\nservices:\n" in compose, "docker-compose.yml declares no services at all"
    body = compose[compose.index("\nservices:\n") + 1:]
    return re.findall(r"^  ([A-Za-z][\w-]*):\s*$", body, re.M)


def _service(compose: str, name: str) -> str:
    """One service's block, from its own key to the next service's."""
    body = compose[compose.index("\nservices:\n") + 1:]
    blocks = list(re.finditer(r"^  (?P<name>[A-Za-z][\w-]*):\s*$", body, re.M))
    for i, match in enumerate(blocks):
        if match.group("name") != name:
            continue
        end = blocks[i + 1].start() if i + 1 < len(blocks) else len(body)
        return body[match.start():end]
    raise AssertionError(f"docker-compose.yml declares no `{name}` service")


def _anchor(compose: str, name: str) -> str:
    """The block a `&name` anchor holds. Both volume lists and the env map are read this way."""
    match = re.search(rf"^(?P<key>[A-Za-z][\w-]*): &{re.escape(name)}\s*$", compose, re.M)
    assert match, f"docker-compose.yml defines no `&{name}` anchor"
    return _nested(compose[match.start():], match.group("key"))


def _bind_mounts(compose: str, service: str) -> set[tuple[str, str, str]]:
    """(host path, container path, access mode) for every bind mount `service` receives."""
    block = _nested(_service(compose, service), "volumes")
    alias = re.search(r"volumes:\s*\*([\w-]+)", block)
    if alias:
        block = _anchor(compose, alias.group(1))
    return {
        (m.group("host"), m.group("container"), m.group("mode") or "rw")
        for m in _MOUNT.finditer(block)
    }


def _mounts(compose: str, service: str) -> set[tuple[str, str]]:
    """(container path, access mode) for every bind mount `service` actually receives.

    The projection, kept because "does the worker still get /data/cache" is a question about the
    container path and `test_worker_registry.py` imports it to ask exactly that. The dumps are
    not that kind of question — a host directory delivered under any container path is the same
    directory — so the guard below reads `_bind_mounts` instead. [M4.7 sec-08]
    """
    return {(container, mode) for _, container, mode in _bind_mounts(compose, service)}


def _unmounted_spec_volumes(compose: str) -> set[str]:
    mounted = {path for name in _service_names(compose) for path, _ in _mounts(compose, name)}
    return SPEC_VOLUMES - mounted


def _published_ports(compose: str) -> list[str]:
    """Every port this stack publishes to the host, across every service.

    Both spellings YAML gives a sequence, because the guard below is the only thing standing
    between this stack and a second door and `ports: ["5432:5432"]` — the one an operator adds
    to reach Postgres with psql — is one keystroke fewer than the block form. Read as `- ` items
    alone, that line contributed nothing and the count stayed at one. [M4.7 tq3-one-published-port]
    """
    entries: list[str] = []
    for name in _service_names(compose):
        body = _nested(_service(compose, name), "ports").partition("ports:")[2]
        if "[" in body:
            assert "]" in body, f"the `{name}` service's ports sequence is never closed"
            inner = body[body.index("[") + 1:body.rindex("]")]
            entries += [item.strip().strip("\"'") for item in inner.split(",") if item.strip()]
            continue
        for line in body.splitlines():
            item = line.strip()
            if item.startswith("- "):
                entries.append(item[2:].strip().strip("\"'"))
    return entries


def test_the_app_publishes_one_plain_http_port_and_terminates_no_tls():
    """§2: "the app itself serves plain HTTP on one internal port; the operator's existing
    Traefik + Cloudflare terminates TLS".

    Written at M0 as a count of lines matching `${BIND_ADDR...}`, which is one spelling of a
    published port and not the property: adding `- "0.0.0.0:9090:9090"` to the backend left the
    count at one and the guard green, and `assert "443" not in compose` was the only thing
    standing between this stack and a second door. The entries are read off the `ports` block of
    every service now, however they are written. [M4.7 tq3-one-published-port]
    """
    compose = _compose()
    published = _published_ports(compose)
    assert len(published) == 1, f"expected exactly one published app port, found {published}"
    assert published[0].endswith(":8080"), (
        f"the one published port must reach the app's internal 8080, not {published[0]}"
    )
    assert "443" not in compose
    for tls in ("letsencrypt", "certresolver", "ssl_certificate", "traefik.http.routers"):
        assert tls not in compose.lower(), f"the app must not configure TLS itself ({tls})"


def test_every_data_volume_the_spec_names_is_mounted():
    """§1 names /data/pg, /data/raw, /data/artifacts, /data/cache; §2's nightly pg_dump adds
    /data/backups. A missing mount is data that does not survive a container replacement.

    Written at M0 as `assert path in compose` over the raw file text, which is true of a mount
    that has been commented out and true of a compose file consisting of nothing but comments.
    The paths are extracted from the mount lists the services actually resolve now — through
    their anchors, which M4.7 split in two. [M4.7 ddocs-08]
    """
    missing = _unmounted_spec_volumes(_compose())
    assert not missing, f"not mounted anywhere in the stack: {sorted(missing)}"


def _dumps_host_dir(compose: str) -> str:
    """The host directory §2's nightly dump lands in, as this file spells it."""
    hosts = [host for host, container, _ in _bind_mounts(compose, "worker")
             if container == "/data/backups"]
    assert hosts, "no worker mount delivers /data/backups, so the dumps have no host directory"
    return hosts[0]


def _delivers(host: str, root: str) -> bool:
    """Would mounting `host` put everything in `root` inside the container?"""
    host = host.rstrip("/") or "/"
    return root == host or root.startswith(f"{host}/")


def _backend_mounts_holding_the_dumps(compose: str) -> list[str]:
    """The backend mounts that deliver §2's dumps writable.

    Read-only is the property, so read-only is what is looked for. Written as `mode == "rw"`,
    which is a whole-string comparison against an option *list*: `:z`, `:rw,z`, `:cached` and
    `:delegated` are every one of them read-write and every one of them was read as not writable,
    and `:Z` did not reach this at all because the mount pattern above could not spell it. This is
    the only place in this file where a mode it cannot parse fails open — `_mounts`'s identical
    idiom fails the assertion it feeds instead, which is why that one can stay as it is.
    [M4.7 sec-08, cycle 3 f15]
    """
    dumps = _dumps_host_dir(compose)
    return sorted(
        f"{host}:{container}"
        for host, container, mode in _bind_mounts(compose, "backend")
        if "ro" not in mode.split(",")
        and (_delivers(host, dumps) or container.startswith("/data/backups"))
    )


def test_the_http_container_does_not_carry_the_nightly_dumps():
    """§14.3 makes the stored connector credential admin-equivalent, and the dumps hold every
    member's Ledger and the wrapped DEK.

    The backend serves §6's unauthenticated SPA fallback out of the same process;
    `e2e/specs/07-boundaries.spec.js` records the traversal bug that once served
    `data/backups/dump.sql` from exactly that handler. A file that is not in the container
    cannot be served out of it, so the mount is dropped rather than made read-only — but `:ro`
    would satisfy the rule this asserts, which is that the HTTP process has no writable copy.

    Asked of the host side, which is where the dumps are. Written first as a filter on the
    *container* path, so `- ./data/backups:/backups` — the exact line the `db` service already
    carries — put every night's dump back inside the HTTP container with this green, and so did
    `- ./data:/data`, which is the shape a simplification of the volume list actually takes. The
    dumps directory is read off the worker's own mount rather than named here, because the rule
    is about that directory and not about a path literal; an ancestor of it delivers it too. The
    container-path clause stays as the second net: it is what catches the host directory being
    renamed while `/data/backups` — the path `backup/nightly.py` writes and a traversal would
    ask for — stays where it is. [M4.7 sec-08]
    """
    writable = _backend_mounts_holding_the_dumps(_compose())
    assert not writable, f"the backend mounts the nightly dumps read-write: {writable}"


def _without_grace_period(compose: str) -> list[str]:
    return [
        service
        for service in ("backend", "worker")
        if not re.search(r"^\s*stop_grace_period:\s*\S+\s*$", _service(compose, service), re.M)
    ]


def test_both_app_services_declare_a_stop_grace_period():
    """§2 budgets the nightly dump in minutes and §10 makes "restart backend + worker" routine,
    so Docker's 10 s default SIGKILLs mid-`pg_dump`: a `.partial` left behind, the pool never
    closed, the "worker stopped" line never written and the child `pg_dump` orphaned.
    [M4.7 ops-08]"""
    missing = _without_grace_period(_compose())
    assert not missing, f"Docker SIGKILLs these after its 10s default: {missing}"


# The `Settings` field prefixes of every connector §2 lets the environment seed, and ONE tuple for
# the compose guard and the two `.env.example` guards below, because three copies of it is how all
# three went stale together. Each was written for the four M0 connectors, so when M5.5 gave
# `Settings` the three LLM providers' keys (plan A3) the guards went on reading jellyfin_, tmdb_,
# omdb_ and trakt_ and would have passed a compose file and a `.env.example` that carried no
# GEMINI_API_KEY at all. The plan's risk note -- this guard "fails the moment you add a config field
# without forwarding it", so the new fields "will otherwise fail, by design" -- was therefore false
# until this widening: a field the tuple does not name is a field no guard reads. What holds it to
# the code from now on is `test_every_variable_env_seeds_reads_is_one_the_seed_guards_read`, which
# derives the fields from `registry.env_seeds` itself. [M5.5 plan A3, §9's risks]
_SEED_PREFIXES = ("jellyfin_", "tmdb_", "omdb_", "trakt_", "gemini_", "anthropic_", "openai_")


def _seed_fields() -> list[str]:
    from spielplan.core.config import Settings

    return [
        name.upper()
        for name in Settings.model_fields
        if name.startswith(_SEED_PREFIXES)
    ]


def _unforwarded_seeds(compose: str) -> list[str]:
    anchor = _anchor(compose, "app-env")
    fields = _seed_fields()
    assert fields, "Settings should carry the connector seed fields"
    return [name for name in fields if not re.search(rf"^\s*{name}:", anchor, re.M)]


def test_compose_forwards_every_connector_seed_variable():
    """§2: "env vars may *seed* connector config on first boot for automated installs."

    `Settings` declared all six, `.env.example` documented all six, `app.py` called
    `seed_from_env` at every boot and a coverage row certified the behaviour over ten tests —
    and not one of the six ever reached a container, because the env anchor named six variables
    and these were not among them. None of those ten tests asked whether compose delivers them,
    which is what makes this a static rule rather than a runtime one. The field list is
    `Settings`'s own, so a renamed field fails here instead of seeding nothing.
    [M4.7 ds01, dd-compose]
    """
    missing = _unforwarded_seeds(_compose())
    assert not missing, f"docker-compose.yml's env anchor does not forward: {missing}"


# --- and the self-tests those five owe ---------------------------------------------------
#
# docs/TESTING.md: "a guard that cannot fail reads as coverage while providing none." Two of
# the guards above shipped for four milestones unable to see the violation they were written
# for, so each one here is fed the synthetic violation before it is trusted with the real file.


def test_the_volume_guard_sees_a_mount_that_was_commented_out():
    """The M0 form's exact blind spot: `- ./data/backups:/data/backups` behind a `#`."""
    commented = _compose().replace(
        "  - ./data/backups:/data/backups", "  # - ./data/backups:/data/backups"
    )
    assert _unmounted_spec_volumes(commented) == {"/data/backups"}


def test_the_volume_guard_sees_a_compose_file_of_pure_comments():
    """ddocs-08's own reduction: the M0 form passed on this file."""
    pure = "\n".join("# " + line for line in _compose().splitlines())
    with pytest.raises(AssertionError, match="no services"):
        _unmounted_spec_volumes(pure)


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        # tq3's: a port that is not written with `${BIND_ADDR}` was invisible, and `443` was the
        # only backstop — so `0.0.0.0:9090:9090` opened a second door with the guard still green.
        (
            "a second app port in block form",
            "    ports:\n",
            '    ports:\n      - "0.0.0.0:9090:9090"\n',
        ),
        # And the spelling the rewritten guard was still blind to: a flow sequence carries no
        # `- ` item at all, so `db` published 5432 to the host and `_published_ports` returned
        # the one entry it had before.
        (
            "the database in flow style",
            "    image: postgres:16\n",
            '    image: postgres:16\n    ports: ["0.0.0.0:5432:5432"]\n',
        ),
    ],
)
def test_the_port_guard_sees_a_second_published_port(name, old, new):
    compose = _compose()
    second = compose.replace(old, new)
    assert second != compose, f"the line {name} is written after is no longer in the file"
    assert len(_published_ports(second)) == 2, f"{name} went unnoticed"


def _backend_volumes(compose: str, *items: str) -> str:
    """The same compose file with the backend's volume list replaced by `items`."""
    anchor = _anchor(compose, "backend-volumes")
    body = "".join(f"  - {item}\n" for item in items)
    mutated = compose.replace(anchor, f"x-backend-volumes: &backend-volumes\n{body}")
    assert mutated != compose, "the backend's volume anchor is no longer where this reads it"
    return mutated


@pytest.mark.parametrize(
    ("name", "items"),
    [
        # The state this stack was in before M4.7 split the anchor: one list, both services.
        ("the worker's whole list", ("./data/raw:/data/raw", "./data/artifacts:/data/artifacts",
                                     "./data/cache:/data/cache", "./data/import:/data/import",
                                     "./data/backups:/data/backups")),
        # The `db` service's own line, copied one service up. Same host directory, a container
        # path the old filter could not see, and every dump inside the HTTP process.
        ("the dumps under another container path",
         ("./data/artifacts:/data/artifacts", "./data/backups:/backups")),
        # What a simplification of the list actually looks like, and the one shape the old
        # self-test could not even express: one line, five mounts, dumps included.
        ("the whole data directory in one line", ("./data:/data",)),
        # And the three spellings a whole-string comparison against "rw" could not see, all of
        # them writable. `:z` is what every SELinux guide tells an operator to append when a bind
        # mount answers Permission denied on Fedora, RHEL or Podman; `:Z` is the unshared form of
        # the same advice, which the mount pattern dropped on the floor rather than reading as a
        # mode; `:rw,z` is that advice applied to a line that already carried one. `:cached` and
        # `:delegated` are the same shape again. Every case above is written bare, so the guard's
        # own proof only ever exercised the one spelling it could match. [M4.7 cycle 3 f15]
        ("the dumps relabelled for SELinux",
         ("./data/artifacts:/data/artifacts", "./data/backups:/data/backups:z")),
        ("the dumps relabelled unshared",
         ("./data/artifacts:/data/artifacts", "./data/backups:/data/backups:Z")),
        ("the dumps with a mode beside the relabel",
         ("./data/artifacts:/data/artifacts", "./data/backups:/data/backups:rw,z")),
    ],
)
def test_the_backups_guard_sees_the_dumps_reach_the_http_container(name, items):
    """docs/TESTING.md's rule for a guard that was reading the wrong side of the colon.

    All six of these hand the backend a writable copy of every night's `pg_dump`; the first is
    the only one the container-path filter could see, and the last three are the ones a mode read
    as a word rather than as an option list called harmless. [M4.7 sec-08, cycle 3 f15]
    """
    assert _backend_mounts_holding_the_dumps(_backend_volumes(_compose(), *items)), (
        f"the backend receiving {name} went unnoticed"
    )


@pytest.mark.parametrize("mode", ["ro", "ro,z"])
def test_the_backups_guard_leaves_a_read_only_mount_alone(mode):
    """The other half of reading the option list rather than the word.

    The rule is that the HTTP process has no *writable* copy, and the guard's own docstring says
    `:ro` would satisfy it — so a mode read as a list must not turn the mount this stack could
    legitimately grow into a violation, relabelled or not. [M4.7 sec-08, cycle 3 f15]
    """
    read_only = _backend_volumes(
        _compose(), "./data/artifacts:/data/artifacts", f"./data/backups:/data/backups:{mode}"
    )
    assert _backend_mounts_holding_the_dumps(read_only) == []


def test_the_grace_period_guard_sees_a_service_without_one():
    """The mutation removes the worker's own line, whatever number it currently carries.

    It read the literal `stop_grace_period: 5m` until decision 300 moved the worker to 10m to
    cover `BUNDLE_IMPORT_TIMEOUT`, after which the replace matched nothing, `_without_grace_period`
    was handed an unmutated file and this self-test asserted `[] == ["worker"]` -- red, but only
    by luck: had the strip removed some *other* service's line the assertion would have gone
    quietly green over a guard that had stopped being exercised. A self-test whose violation is
    spelled as a literal stops being a violation the day the file moves, which is the M4.16 defect
    class in miniature. The line is read off the worker's own block and its removal asserted, so a
    later bump cannot make this vacuous again. [decision 300]
    """
    worker = _service(_compose(), "worker")
    line = re.search(r"^ +stop_grace_period:.*\n", worker, re.M)
    assert line, "the worker declares no stop_grace_period for this mutation to take away"
    stripped = _compose().replace(worker, worker.replace(line.group(0), "", 1), 1)
    assert stripped != _compose(), "the mutation removed nothing, so the guard is untested"
    assert _without_grace_period(stripped) == ["worker"]


def test_the_seed_guard_sees_a_variable_that_stopped_being_forwarded():
    dropped = _compose().replace("  TMDB_API_KEY: ${TMDB_API_KEY:-}\n", "")
    assert _unforwarded_seeds(dropped) == ["TMDB_API_KEY"]


def test_the_seed_guard_sees_a_provider_key_that_stopped_being_forwarded():
    """The same mutation on the family M5.5 added, which the guard could not see until the tuple
    above was widened: with the M0 prefixes it read the compose file without GEMINI_API_KEY as
    complete, so an automated install's Gemini key would have seeded nothing and said nothing."""
    compose = _compose()
    dropped = compose.replace("  GEMINI_API_KEY: ${GEMINI_API_KEY:-}\n", "")
    assert dropped != compose, "compose no longer forwards GEMINI_API_KEY in the form this removes"
    assert _unforwarded_seeds(dropped) == ["GEMINI_API_KEY"]


def _fields_env_seeds_reads() -> set[str]:
    """Every `Settings` field `registry.env_seeds` reads, off its source: each `cfg.<field>`.

    Read from the function rather than listed, because the list is the thing that went stale: the
    seed guards were a copy of which connectors seed, and the copy stopped at four.
    """
    tree = ast.parse(_src(REPO / "backend" / "spielplan" / "connectors" / "registry.py"))
    body = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "env_seeds"),
        None,
    )
    assert body is not None, "connectors/registry.py no longer defines env_seeds"
    return {
        node.attr for node in ast.walk(body)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id == "cfg"
    }


def test_every_variable_env_seeds_reads_is_one_the_seed_guards_read():
    """The seed guards' subject, derived: a field `env_seeds` seeds from is a field they read.

    The compose guard, the `.env.example` guards and the operator's first boot all turn on the same
    set, and until M5.5 the guards' set was a literal that named four connector families while
    `env_seeds` read seven -- so GEMINI_API_KEY, ANTHROPIC_API_KEY and OPENAI_API_KEY could have
    been missing from compose and from the template with every one of those guards green. Held here
    so the next connector family `env_seeds` learns is a red line in this file rather than a quiet
    gap in three. [M5.5 plan A3]
    """
    read = _fields_env_seeds_reads()
    # Not vacuous: the reader has to find the M0 family and the M5.5 one, or it is reading nothing.
    assert {"jellyfin_url", "tmdb_api_key", "gemini_api_key"} <= read, read
    unguarded = sorted(name.upper() for name in read if not name.startswith(_SEED_PREFIXES))
    assert not unguarded, (
        "registry.env_seeds seeds from these and no seed guard reads them, so compose and "
        f".env.example could drop them with this file green: {unguarded}"
    )


def test_postgres_is_pinned_to_16():
    """§1: Postgres 16. The schema is written against it and PGlite only approximates it.

    Read off the `db` service's own `image` key, because the raw-text form was satisfied by any
    `image: postgres:16` anywhere in the file — including the one an upgrade leaves behind, the
    old line kept behind a `#` above the new one. [M4.7 ddocs-08]
    """
    assert re.search(r"image:\s*postgres:16\b", _nested(_service(_compose(), "db"), "image"))


def test_the_postgres_pin_guard_ignores_a_version_left_behind_in_a_comment():
    """The shape a bump actually takes, and the one the raw-text form could not see."""
    bumped = _compose().replace(
        "    image: postgres:16\n", "    # image: postgres:16\n    image: postgres:17\n"
    )
    assert re.search(r"image:\s*postgres:16\b", bumped), "the M0 form reads this file as pinned"
    assert not re.search(r"image:\s*postgres:16\b", _nested(_service(bumped, "db"), "image"))


def test_the_image_pulls_torch_from_the_cpu_index_only():
    """§1's hard constraint: "every in-app model update runs on CPU… The image must build and
    run on a GPU-less VM." A CUDA wheel is 2 GB of accident.

    Written at M0 as a grep for the CPU index URL in the Dockerfile, which was true while the
    build was CPU-only *by PEP 440 ordering luck*: `--index-strategy unsafe-best-match` lets the
    highest version across PyPI and the CPU index win, and `2.14.0+cpu` sorts above `2.14.0`
    only until PyPI publishes first. The day it does, the build takes the CUDA wheel and fifteen
    `nvidia-*` packages with the URL still sitting in the file. So what is asserted is the
    mechanism that cannot slip: torch pinned to an `explicit` CPU index in `pyproject.toml`,
    which every consumer honours — the image, CI's three jobs and a developer's `-e ".[dev]"`
    alike — and no index strategy in the Dockerfile picking a winner by version. [M4.7 dd28]
    """
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    pyproject_text = PYPROJECT.read_text(encoding="utf-8")
    uv = tomllib.loads(pyproject_text).get("tool", {}).get("uv", {})

    cpu = [
        entry.get("name")
        for entry in uv.get("index", [])
        if entry.get("url", "").rstrip("/") == "https://download.pytorch.org/whl/cpu"
    ]
    assert cpu, "backend/pyproject.toml declares no CPU-only pytorch index"
    named = {entry.get("name"): entry for entry in uv.get("index", [])}
    assert named[cpu[0]].get("explicit") is True, (
        f"index `{cpu[0]}` must be `explicit`, or it competes with PyPI for every package it "
        "carries and the image gets certifi==2022.12.7"
    )
    assert uv.get("sources", {}).get("torch", {}).get("index") == cpu[0], (
        f"torch must be sourced from `{cpu[0]}`, not merely offered it as an extra index"
    )
    assert "unsafe-best-match" not in dockerfile, (
        "an index strategy picks the highest version across indexes, which is the CPU wheel only "
        "while the CPU index is ahead of PyPI"
    )
    for cuda in ("cu118", "cu121", "cu124", "nvidia", "--gpus"):
        assert cuda not in dockerfile.lower(), f"the image must stay CPU-only ({cuda})"
        assert cuda not in pyproject_text.lower(), f"the dependency spec must stay CPU-only ({cuda})"


def _final_stage(dockerfile: str) -> str:
    """The instructions that build the image this compose file actually runs.

    A `FROM` starts a new stage with its own configuration: the `User`, `Cmd` and `Entrypoint`
    a container is started from come from the last stage alone, and a build stage's `USER` is
    spent when that stage ends. `ops/backend.Dockerfile` has been two stages since M0 — the node
    build and the python runtime — so a guard that reads the whole file credits the shipped image
    with directives it never receives: a `USER node` added to the frontend stage while the
    runtime stage lost its own left `_runs_as_root` returning None on an image that starts
    uvicorn, the SPA fallback and the worker as uid 0. [M4.7 sec-08]
    """
    stages = list(re.finditer(r"^FROM\s", dockerfile, re.M))
    return dockerfile[stages[-1].start():] if stages else dockerfile


def _runs_as_root(dockerfile: str) -> str | None:
    """Why this image would run its processes as root, or None.

    The order of `USER` and `CMD` within that stage is deliberately not a rule here. This guard
    used to return a violation when the last `USER` came after the first `CMD`, saying it "does
    not apply to the process that runs" — but Docker records the stage's last `USER` in the image
    config's `User` field and its last `CMD` in `Cmd`, and the runtime reads `User` whichever
    order the two were written in. The branch could only ever fire on a file that runs
    unprivileged, with a message explaining a mechanism that does not exist and a natural repair
    (move the line back up) that is cargo cult. The two branches left are the whole property.
    """
    users = list(re.finditer(r"^USER\s+(\S+)", _final_stage(dockerfile), re.M))
    if not users:
        return "no USER directive in the stage that ships"
    if users[-1].group(1) in ("root", "0"):
        return f"the last USER directive is `{users[-1].group(1)}`"
    return None


def test_the_image_does_not_run_its_processes_as_root():
    """§14.3's threat model is that the stored connector credential is admin-equivalent.

    uvicorn, §6's unauthenticated SPA fallback and the worker all ran as uid 0, in a container
    that until this milestone also held every night's `pg_dump`. The bind mounts under ./data
    are owned by whoever created them on the host, so this directive is what README's
    `chown -R 1000:1000 data` line exists for. [M4.7 sec-08]
    """
    reason = _runs_as_root(DOCKERFILE.read_text(encoding="utf-8"))
    assert reason is None, f"ops/backend.Dockerfile runs as root: {reason}"


def _unpinned_uv(dockerfile: str) -> str | None:
    """Why this image's `uv` is whatever was published most recently, or None."""
    specs = re.findall(r"^RUN\s+pip install\s+(?:--\S+\s+)*(uv\S*)", dockerfile, re.M)
    if not specs:
        return "nothing installs uv"
    loose = [spec for spec in specs if not re.fullmatch(r"uv==\d+(?:\.\d+)*", spec)]
    return f"not pinned to an exact version: {loose}" if loose else None


def test_the_image_pins_the_tool_that_installs_everything_else():
    """The `pip install uv` line runs as root and resolves every other package in the image.

    Unpinned, each rebuild is an unreviewed supply-chain decision taken by whoever published
    most recently — on the one line that then chooses the versions of all 47 others. Same
    §14.3 reasoning as the `USER` directive above; it is the other half of `sec-08`'s image
    hardening and the `what` of its coverage row says "pins uv". [M4.7 sec-08]
    """
    reason = _unpinned_uv(DOCKERFILE.read_text(encoding="utf-8"))
    assert reason is None, f"ops/backend.Dockerfile does not pin uv: {reason}"


@pytest.mark.parametrize(
    ("name", "dockerfile"),
    [
        ("unpinned", "RUN pip install uv\n"),
        ("a floor, not a pin", "RUN pip install uv>=0.11\n"),
        ("a compatible-release clause", "RUN pip install uv~=0.11.0\n"),
        ("nothing installs it", "RUN apt-get install -y curl\n"),
    ],
)
def test_the_uv_pin_guard_catches_a_real_violation(name, dockerfile):
    assert _unpinned_uv(dockerfile), f"an unpinned installer ({name}) went unnoticed"


def test_the_uv_pin_guard_accepts_a_flagged_pin():
    """The other half: the guard reads the requirement, not the whole line."""
    assert _unpinned_uv("RUN pip install --no-cache-dir uv==0.11.21\n") is None


# `RUN uv pip install ...`, with the shell's backslash continuations folded back in.
_UV_INSTALL = re.compile(r"^RUN\s+uv pip install\b(?P<argv>(?:[^\n\\]|\\\n)*)", re.M)


def _project_install_reason(dockerfile: str) -> str | None:
    """Why `[project.scripts]` would not exist inside this image, or None."""
    # Anchored at the start of a line, not searched for anywhere in the text: a COPY behind a
    # `#` is a line that copies nothing, and this guard's whole subject is what is really in
    # the image. Same reduction as ddocs-08, in the artifact next door. [M4.7 ddocs-08]
    copy = re.search(r"^COPY\s+backend/spielplan\b", dockerfile, re.M)
    if copy is None:
        return "the package is never copied into the image"
    copied = copy.start()
    for match in _UV_INSTALL.finditer(dockerfile):
        if "." not in match.group("argv").replace("\\\n", " ").split():
            continue
        if match.start() < copied:
            return "the project is installed before its package is copied in, so nothing ships"
        return None
    return (
        "no `uv pip install .`: reading pyproject with -r installs the dependencies and never "
        "the project, so no declared console script is created"
    )


def test_the_image_installs_the_project_and_not_only_its_dependencies():
    """§2's recovery gestures are commands, not routes, and the runbook names two of them.

    The image installed `-r pyproject.toml` — pyproject as a *requirements source*, which
    resolves 46 packages and none of them this one — so `spielplan-migrate` was a declared
    entry point that did not exist inside the container, and `spielplan-secrets` (the way back
    from a lost `SECRETS_KEY`, §2) and `spielplan-movie-data` (decision 162's only copy of the
    household's movie data) would have shipped the same way. The three scripts must also name
    modules that are really there, or the entry point exists and dies on import.
    [M4.7 dd-deploy-image, spec-08, spec-07]
    """
    reason = _project_install_reason(DOCKERFILE.read_text(encoding="utf-8"))
    assert reason is None, f"ops/backend.Dockerfile does not install the project: {reason}"

    scripts = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts, "the row's `what` is about declared console scripts and there are none"
    for name, target in scripts.items():
        module, _, function = target.partition(":")
        source = REPO / "backend" / Path(*module.split(".")).with_suffix(".py")
        assert source.is_file(), f"{name} points at {module}, which is not a module"
        assert re.search(rf"^def {re.escape(function)}\b", source.read_text(encoding="utf-8"), re.M), (
            f"{name} points at {target}, and {module} defines no {function}"
        )


@pytest.mark.parametrize(
    ("name", "dockerfile"),
    [
        (
            "dependencies only",
            "COPY backend/spielplan ./spielplan\nRUN uv pip install --system -r pyproject.toml\n",
        ),
        (
            "installed before the package arrives",
            "RUN uv pip install --system --no-deps -e .\nCOPY backend/spielplan ./spielplan\n",
        ),
        ("the package never arrives", "RUN uv pip install --system --no-deps -e .\n"),
        (
            "the copy is commented out",
            "# COPY backend/spielplan ./spielplan\nRUN uv pip install --system --no-deps -e .\n",
        ),
    ],
)
def test_the_project_install_guard_catches_a_real_violation(name, dockerfile):
    assert _project_install_reason(dockerfile), f"an image with no console scripts ({name}) passed"


def _multi_worker_reason(dockerfile: str) -> str | None:
    """Why this image would start more than one application process, or None.

    The `CMD` is read off the final stage, for the reason `_final_stage` gives: it is the only
    one the runtime receives. The two greps above it stay whole-file deliberately — a gunicorn
    installed in a build stage is a package that can still be copied forward, and neither name
    has any business anywhere in an image that must start exactly one process. [M4.7 arch-09]
    """
    if re.search(r"\bgunicorn\b", dockerfile):
        return "a gunicorn wrapper forks workers"
    if "WEB_CONCURRENCY" in dockerfile:
        return "WEB_CONCURRENCY is uvicorn's own multi-worker setting"
    command = re.search(r"^CMD\s+(?P<argv>.+)$", _final_stage(dockerfile), re.M)
    if command is None:
        return "no CMD at all"
    if re.search(r"--workers\b|(?<![\w-])-w(?![\w-])", command.group("argv")):
        return f"the CMD asks for workers: {command.group('argv')}"
    return None


def test_the_image_starts_exactly_one_application_process():
    """§6.7's rail buffers, Tonight's lobby hub and the push in-flight set are process-global.

    Correct for the shipped single-process CMD and enforced nowhere: `--workers 2` silently
    splits the household into two lobbies and two rails, with no log line and nothing failing.
    This is the static half; `app.py` refuses at boot for the settings this file cannot see (a
    compose `command:` override, a systemd unit, a hand-run uvicorn). [M4.7 arch-09]
    """
    reason = _multi_worker_reason(DOCKERFILE.read_text(encoding="utf-8"))
    assert reason is None, f"ops/backend.Dockerfile starts more than one process: {reason}"


_TWO_STAGE = (
    "FROM node:22-slim AS frontend\nWORKDIR /app\n{build}RUN npm run build\n\n"
    "FROM python:3.12-slim AS runtime\nWORKDIR /app\n{runtime}"
    'EXPOSE 8080\nCMD ["uvicorn", "spielplan.app:app"]\n'
)


@pytest.mark.parametrize(
    ("name", "dockerfile"),
    [
        ("no USER at all", 'CMD ["uvicorn", "spielplan.app:app"]\n'),
        ("USER root", 'USER root\nCMD ["uvicorn", "spielplan.app:app"]\n'),
        ("root reinstated", 'USER spielplan\nUSER root\nCMD ["uvicorn"]\n'),
        # The shape of the file this actually guards, and the one all four cases above miss:
        # two stages, the build stage hardened, the runtime stage's own USER gone. Reading the
        # whole file, `USER node` is the last one and the image ships as uid 0 unnoticed.
        ("USER only in the build stage", _TWO_STAGE.format(build="USER node\n", runtime="")),
        (
            "root reinstated in the stage that ships",
            _TWO_STAGE.format(build="USER node\n", runtime="USER spielplan\nUSER root\n"),
        ),
    ],
)
def test_the_root_guard_catches_a_real_violation(name, dockerfile):
    assert _runs_as_root(dockerfile), f"an image running as root ({name}) went unnoticed"


def test_the_root_guard_reads_the_stage_that_ships_rather_than_the_order_within_it():
    """The other half, and the two things the guard used to get wrong stated as passes.

    A build stage with no `USER` says nothing about the image: its instructions run as root, are
    thrown away with the stage, and only `COPY --from` survives. And a `USER` written after `CMD`
    is the image's `User` all the same — Docker reads the config field, not the file order — so
    it must not be reported as a violation, however unusual it looks. [M4.7 sec-08]
    """
    assert _runs_as_root(_TWO_STAGE.format(build="", runtime="USER spielplan\n")) is None
    trailing = 'FROM python:3.12-slim\nCMD ["uvicorn", "spielplan.app:app"]\nUSER spielplan\n'
    assert _runs_as_root(trailing) is None, (
        "a trailing USER still sets the image's User field; failing here teaches a repair that "
        "changes nothing about the process that runs"
    )


@pytest.mark.parametrize(
    ("name", "dockerfile"),
    [
        ("--workers", 'CMD ["uvicorn", "spielplan.app:app", "--workers", "4"]\n'),
        ("-w", 'CMD ["uvicorn", "spielplan.app:app", "-w", "4"]\n'),
        ("gunicorn", 'CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "spielplan.app:app"]\n'),
        ("WEB_CONCURRENCY", 'ENV WEB_CONCURRENCY=4\nCMD ["uvicorn", "spielplan.app:app"]\n'),
        # Same stage-blindness as the root guard next door, in the other direction: the first
        # `CMD` in the file belongs to a build stage that is thrown away, and reading it left
        # the shipped command's `--workers 4` unexamined.
        (
            "--workers in the stage that ships, behind a build stage's own CMD",
            'FROM node:22-slim AS frontend\nCMD ["npm", "run", "dev"]\n\n'
            "FROM python:3.12-slim AS runtime\nUSER spielplan\n"
            'CMD ["uvicorn", "spielplan.app:app", "--workers", "4"]\n',
        ),
    ],
)
def test_the_single_process_guard_catches_a_real_violation(name, dockerfile):
    assert _multi_worker_reason(dockerfile), f"a multi-worker image ({name}) went unnoticed"


def test_the_single_process_guard_leaves_the_shipped_cmd_alone():
    """The other half: `--host` and `--port` are not `-w`, and neither is `spielplan.app:app`."""
    shipped = 'CMD ["uvicorn", "spielplan.app:app", "--host", "0.0.0.0", "--port", "8080"]\n'
    assert _multi_worker_reason("USER spielplan\n" + shipped) is None


def _inherited_http_healthcheck(compose: str) -> str | None:
    """Why `docker compose ps` would stop answering "is the loop alive", or None."""
    block = _nested(_service(compose, "worker"), "healthcheck")
    if not block.strip():
        return "the worker declares none, so it inherits the image's HTTP check"
    if "disable: true" in block:
        return "the check is disabled, so the column says Up for a worker whose loop is stuck"
    if "/api/health" in block:
        return "the worker serves no HTTP; that check can only fail"
    if "worker.heartbeat" not in _nested(block, "test"):
        return "the check does not read the heartbeat `worker._tick` touches every pass"
    return None


def test_the_worker_does_not_inherit_the_backend_http_healthcheck():
    """They share an image; the worker serves no HTTP, so the inherited check can only fail,
    and a permanently unhealthy service teaches the operator to ignore the column.

    Overriding it with `disable: true` made the column say "Up" for a worker whose loop was
    stuck, which is the one thing an operator would ask it. M4.7 replaced the disable with the
    age of the file `worker._tick` touches every pass, so what is asserted here is that the
    override is still not the HTTP check *and* still not nothing.

    Written — in this milestone, in this file, beside two guards it was repairing for exactly
    this — as raw substrings over the worker's slice of the file text. A `#` in front of each
    line of the block leaves every one of those substrings exactly where it was, so all four
    assertions stayed true of a compose file whose worker healthcheck was entirely commented
    out: measured green on that file, with the worker back on the image's `/api/health` check
    and the column back to saying nothing about the loop. That is `ddocs-08`'s reduction, one
    service over, and this was the only guard in this file with no self-test. Read through
    `_nested`/`_service` now, which drop comments on the way through. [M4.7 ops-07, ddocs-08]
    """
    reason = _inherited_http_healthcheck(_compose())
    assert reason is None, f"docker-compose.yml's worker healthcheck: {reason}"


# The shell a synthetic worker service needs to be readable at all: `_service` slices between
# service keys, so the violations below are whole (tiny) compose files rather than fragments.
_WORKER = "\nservices:\n  worker:\n    command: [\"python\", \"-m\", \"spielplan.worker\"]\n{block}"

_HEARTBEAT_CHECK = (
    '    healthcheck:\n      test: ["CMD", "python", "-c", '
    "\"import pathlib; pathlib.Path('/data/cache/worker.heartbeat')\"]\n"
)


@pytest.mark.parametrize(
    ("name", "block"),
    [
        # The exact state the substring form could not see: every line of the block behind a
        # `#`, which moves `healthcheck:` and `worker.heartbeat` into prose and leaves both
        # exactly where the old assertions looked for them.
        (
            "commented out under the comment that explains it",
            "    # `worker._tick` touches the heartbeat file at the top of every tick.\n"
            "    # healthcheck:\n"
            "    #   test: [\"CMD\", \"python\", \"-c\", \"... worker.heartbeat ...\"]\n",
        ),
        ("no healthcheck at all", "    restart: unless-stopped\n"),
        ("disabled again", "    healthcheck:\n      disable: true\n"),
        (
            "the backend's HTTP check",
            '    healthcheck:\n      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8080/api/health"]\n',
        ),
        (
            "reading a file the loop no longer touches",
            '    healthcheck:\n      test: ["CMD", "python", "-c", '
            "\"import pathlib; pathlib.Path('/data/cache/alive')\"]\n",
        ),
    ],
)
def test_the_worker_healthcheck_guard_catches_a_real_violation(name, block):
    """docs/TESTING.md's rule, owed by the one guard in this file that had none.

    The first case is the finding: a commented-out block that the guard called present, in the
    milestone whose whole thesis is that guards must be able to fail. [M4.7 ops-07, ddocs-08]
    """
    assert _inherited_http_healthcheck(_WORKER.format(block=block)), (
        f"a worker with {name} went unnoticed"
    )


def test_the_worker_healthcheck_guard_accepts_the_shipped_shape():
    """The other half: a heartbeat check the loop really writes is not a violation."""
    assert _inherited_http_healthcheck(_WORKER.format(block=_HEARTBEAT_CHECK)) is None


def test_the_tick_does_not_argue_its_budget_against_a_healthcheck_the_compose_file_denies():
    """CLAUDE.md makes a comment the ARGUMENT for a change, so an argument stated against the
    wrong failure is a defect in the change.

    `asyncio.wait_for` around `job.run()` was justified by "with the process alive, every
    healthcheck green and no log line ever written again". The second and third clauses are
    right; the first is the opposite of what this repo ships. `_touch_heartbeat()` has one call
    site and it is the first statement of `_tick`, above the job loop, so a job that never
    returns stops the file's mtime moving -- and `docker-compose.yml`'s worker check reads
    exactly that age. The column goes unhealthy; it is the one thing that DOES notice. A reader
    who believed the comment would conclude that nothing detects a wedged worker and that
    `Job.timeout` is the only defence, which is how a budget gets sized against nothing.

    Three assertions, because the prose is only wrong relative to a mechanism: the check reads
    the heartbeat, the heartbeat is touched once per tick outside the loop, and the module makes
    no claim to the contrary. [review cycle 1: m411-rev1-tick-comment-claims-a-green-healthcheck]
    """
    source = (REPO / "backend" / "spielplan" / "worker.py").read_text(encoding="utf-8")

    assert _inherited_http_healthcheck(_compose()) is None, (
        "the worker's compose check no longer reads the heartbeat, so this rule is about nothing"
    )

    touches = [
        line
        for line in source.splitlines()
        if "_touch_heartbeat()" in line and not line.startswith("def ")
    ]
    assert touches == ["    _touch_heartbeat()"], (
        "the heartbeat is no longer touched exactly once, at the head of `_tick` and outside the "
        f"job loop -- a wedged job may now leave it moving, and the sentence below changes: {touches}"
    )

    denied = [
        phrase
        for phrase in ("healthcheck green", "healthchecks green", "every healthcheck")
        if phrase in source
    ]
    assert not denied, (
        "worker.py claims a wedged loop leaves the healthcheck green, which docker-compose.yml "
        f"denies -- it reads the heartbeat's age and reports the worker unhealthy: {denied}"
    )


def _ignored(dockerignore: str) -> set[str]:
    """The patterns `.dockerignore` actually applies: whole lines, comments and blanks dropped."""
    return {
        line.strip()
        for line in dockerignore.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def test_the_build_context_excludes_host_state_and_secrets():
    """`context: .` ships whatever is not ignored — including .env and three node_modules
    trees, and a host node_modules copied over the one installed in the image.

    Compared as whole patterns rather than as substrings of the file: `.env` is a substring of
    the `.env.local` line below it, so the M0 form went on passing after the entry that keeps
    the operator's real secrets out of the image was deleted. [M4.7 sec-08, ddocs-08]
    """
    ignore = _ignored((REPO / ".dockerignore").read_text(encoding="utf-8"))
    missing = sorted(
        entry for entry in (".env", "data/", "**/node_modules", "**/.venv") if entry not in ignore
    )
    assert not missing, f"these must not enter the build context: {missing}"


def test_the_build_context_guard_sees_the_env_file_stop_being_ignored():
    """The synthetic violation, and the one the substring form was blind to by construction:
    with `.env` gone the neighbouring `.env.local` still carries the characters it matched, and
    §14.3's admin-equivalent credential rides into the image on the next build."""
    text = (REPO / ".dockerignore").read_text(encoding="utf-8")
    dropped = re.sub(r"(?m)^\.env$\n", "", text)
    assert ".env" in dropped, "`.env.local` still carries the substring the M0 form matched"
    assert ".env" not in _ignored(dropped)
    assert ".env.local" in _ignored(dropped), "only the one line went"


@pytest.mark.parametrize("required", ["PUBLIC_URL", "SESSION_SECRET", "SECRETS_KEY"])
def test_the_stack_refuses_to_start_without_its_required_config(required):
    """§2 makes these three required, and `${VAR:?}` is what makes that true rather than
    aspirational — a missing SECRETS_KEY must stop the stack, not silently weaken it.

    Asked of the env anchor the services actually resolve rather than of the file text: a
    `${VAR:?}` inside a comment is a sentence about a rule and not the rule, and this file's
    own restore runbook and `.env` prose quote variables by name. Same reduction as `ddocs-08`.
    """
    anchor = _anchor(_compose(), "app-env")
    assert re.search(rf"\$\{{{required}:\?", anchor), f"{required} is not a required variable"


def test_the_required_config_guard_sees_a_variable_only_a_comment_still_requires():
    """The state the raw-text form could not tell from the real one."""
    commented = _compose().replace("  SECRETS_KEY: ${SECRETS_KEY", "  # SECRETS_KEY: ${SECRETS_KEY")
    assert "${SECRETS_KEY:?" in commented, "the string is still in the file, which is the point"
    assert not re.search(r"\$\{SECRETS_KEY:\?", _anchor(commented, "app-env"))


def _routes_the_dev_flag_could_take(compose: str) -> list[str]:
    """Every way `SPIELPLAN_INSECURE_DEV` could reach an app container from the host's `.env`.

    Three of them, because the shared anchor is only the obvious one: a service can name the
    variable in its own `environment` map beside `SPIELPLAN_ROLE`, and `env_file: .env` forwards
    whatever the file happens to hold. Read through `_anchor`/`_nested`, which strip comments, for
    the same reason the guard above is: this stack's env anchor argues *against* `env_file: .env`
    in prose two lines above the six variables it forwards one at a time, so a search of the file
    text finds that sentence and calls the rule broken.
    """
    routes = []
    if re.search(r"^\s*SPIELPLAN_INSECURE_DEV:", _anchor(compose, "app-env"), re.M):
        routes.append("the app-env anchor forwards SPIELPLAN_INSECURE_DEV")
    for service in ("backend", "worker"):
        block = _service(compose, service)
        if _nested(block, "env_file").strip():
            routes.append(f"the {service} service declares env_file")
        if re.search(r"^\s*SPIELPLAN_INSECURE_DEV:", _nested(block, "environment"), re.M):
            routes.append(f"the {service} service sets SPIELPLAN_INSECURE_DEV")
    return routes


def test_the_shipped_stack_withholds_the_switch_that_turns_the_refusals_off():
    """README's "Developing" paragraph: "`docker-compose.yml` deliberately does not forward it,
    so it cannot be switched on in the shipped stack by editing `.env`."

    Decision 181 gave §2's three refusals one global off-switch, and it lifts more than those:
    with it set the same boot signs cookies with an empty SESSION_SECRET, binds every passkey to
    `localhost`, and remounts `/api/docs`, `/redoc` and `/openapi.json` for anonymous callers
    (`app.py:213`, asserted from the other side by `test_http_seam.py`). Four security properties
    behind one variable, and what keeps an operator from reaching it is a file — not a password,
    not a role — so the sentence README makes about that file is only as true as this.

    It was the one compose claim in README with nothing reading compose. Every other file that
    names the flag — `core/config.py`, `ops/devstub.py`, `conftest.py`, `test_config.py`,
    `test_devstub_contract.py`, `test_http_seam.py` — asserts what it *does*, and not one of them
    opens `docker-compose.yml`. So adding the variable to the env anchor made README's sentence
    false and no test red. [M4.7 sec-03, spec-04; decision 181]
    """
    routes = _routes_the_dev_flag_could_take(_compose())
    assert not routes, f"the dev flag becomes reachable by editing `.env`: {routes}"


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        # A default of "0" is still a violation and is the shape this would actually arrive in:
        # the rule is about the route, not the value, because the operator supplies the value.
        (
            "the anchor forwarding it",
            "  TZ: ${TZ:-Europe/Berlin}\n",
            "  TZ: ${TZ:-Europe/Berlin}\n  SPIELPLAN_INSECURE_DEV: ${SPIELPLAN_INSECURE_DEV:-0}\n",
        ),
        # The gesture the anchor's own comment argues against, and the obvious fix the next time
        # a variable needs forwarding: it hands the container the whole file at once.
        (
            "a service reading the whole .env",
            "    volumes: *backend-volumes\n",
            "    env_file: .env\n    volumes: *backend-volumes\n",
        ),
        # And the route that goes round the anchor entirely, in the one map each service already
        # has of its own.
        (
            "a service setting it beside its role",
            "      SPIELPLAN_ROLE: worker\n",
            "      SPIELPLAN_ROLE: worker\n      SPIELPLAN_INSECURE_DEV: ${SPIELPLAN_INSECURE_DEV:-0}\n",
        ),
    ],
)
def test_the_dev_flag_guard_sees_each_way_the_variable_could_arrive(name, old, new):
    """docs/TESTING.md's rule, owed by a guard whose whole subject is a variable that is absent —
    the one shape where "green" and "not looking" are hardest to tell apart."""
    compose = _compose().replace(old, new)
    assert compose != _compose(), f"the line {name} is written against is no longer in the file"
    assert _routes_the_dev_flag_could_take(compose), f"{name} went unnoticed"


def test_the_suite_itself_runs_with_the_config_refusals_armed():
    """The guard above asks the stack; this asks the process that is asking.

    Decision 181 gave §2's refusals one global off-switch, and README's "Developing" paragraph
    hands it to the developer two lines after saying a hand-run backend reads `.env` from its own
    working directory — which is also the directory pytest runs from. With
    `SPIELPLAN_INSECURE_DEV` set there or exported, the fourteen tests this row names stop being
    able to fail: eight in `test_config.py`, the anonymous-schema assertion in `test_http_seam.py`
    four files away, and `test_devstub_contract.py`'s own leak detector. A green suite would then
    mean nothing about the one property §2 calls required, and nothing would say so.

    `conftest.py` is where that is neutralised, because the flag has to be gone before collection
    imports anything (`settings()` is `lru_cache`d and modules construct `Settings` while pytest
    is still collecting). This is the assertion that the neutralisation is still there and still
    reaches both routes into the process — an exported variable and a `.env` line. Written as a
    construction rather than as a grep over `conftest.py` for the same reason every other guard in
    this file was rewritten this milestone: the property is what the suite's environment does, not
    what a line of it looks like. [M4.7 spec-04; decision 181]
    """
    from pydantic import ValidationError

    from spielplan.core.config import Settings

    with pytest.raises(ValidationError):
        # Both fields passed explicitly, so only the flag can make this pass — which is the
        # whole subject. Same construction as `test_devstub_contract.py`'s leak detector, one
        # source further out: that one catches the flag this suite sets, this one the flag it
        # was handed.
        Settings(public_url="", session_secret="")


# --- §2: the env-seed variables are documented exactly ----------------------------------


def test_env_example_names_every_connector_seed_variable():
    """§2 lets env vars seed connector config on first boot. An operator writing an automated
    install reads `.env.example`, so a name that drifts from what `Settings` actually reads
    produces a connector that silently never seeds — which looks exactly like a working file.
    """
    from spielplan.core.config import Settings

    example = (REPO / ".env.example").read_text(encoding="utf-8")
    seeds = [
        name.upper()
        for name in Settings.model_fields
        if name.startswith(_SEED_PREFIXES)
    ]
    assert seeds, "Settings should carry the connector seed fields"
    missing = [name for name in seeds if f"{name}=" not in example]
    assert not missing, f".env.example does not document: {missing}"


def test_env_example_marks_the_seed_variables_optional():
    """They are commented out on purpose: an uncommented empty `JELLYFIN_URL=` is not the same
    as absent, and "configured empty" is a state §2 never wants."""
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        if line.startswith(tuple(prefix.upper() for prefix in _SEED_PREFIXES)):
            raise AssertionError(f"connector seed left uncommented in .env.example: {line}")


def test_the_required_variables_are_not_commented_out():
    """The counterpart: PUBLIC_URL, SESSION_SECRET and SECRETS_KEY are required, and compose
    refuses to start without them, so the template has to actually ask for them."""
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    for required in ("PUBLIC_URL", "SESSION_SECRET", "SECRETS_KEY"):
        assert re.search(rf"^{required}=", example, re.M), f"{required} is not in .env.example"


def _password_note(example: str) -> str:
    """The comment block directly above `POSTGRES_PASSWORD=`, lower-cased, and nothing else.

    Directly above, because that is where the operator is looking when they type the value. The
    block above `POSTGRES_USER` describes the three variables together and would make this rule
    true from four lines further away, which is exactly how it went missing.
    """
    lines = example.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("POSTGRES_PASSWORD="))
    note = []
    for line in reversed(lines[:index]):
        if not line.startswith("#"):
            break
        note.append(line)
    return "\n".join(reversed(note)).lower()


def test_env_example_says_the_password_it_asks_for_is_interpolated_into_a_url():
    """ops-12 moved the password into one field; this is the rule that came with it.

    `docker-compose.yml`'s env anchor now builds DATABASE_URL out of POSTGRES_USER/PASSWORD/DB, so
    the value typed here is parsed as a URL by the backend while the `db` service receives it
    verbatim — a Postgres that holds the password beside a backend that cannot send it, at first
    boot, which is finding 31 reproduced through the field the fix made authoritative. Compose
    cannot percent-encode and the plan's step 8 deliberately refuses a second field, so the note
    is the mitigation and its absence is the defect. Measured against asyncpg's own DSN parser:
    `p@ss` splits at the first `@` (password `p`, host `ss@db`), `/` `#` `?` end the netloc and
    the boot dies in urllib's `invalid literal for int()`, and `%20` is percent-decoded on the way
    through. [M4.7 ops-12]
    """
    note = _password_note((REPO / ".env.example").read_text(encoding="utf-8"))
    for phrase in ("url-safe", "docker-compose.yml", "database_url"):
        assert phrase in note, (
            f"the note above POSTGRES_PASSWORD does not mention `{phrase}`; an operator typing a "
            "password with a URL meaning in it gets a backend that cannot authenticate"
        )


def test_the_password_note_guard_sees_the_note_go_missing():
    """The state `.env.example` was in when the two fields became one: the rule attached to the
    variable that no longer holds the password."""
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    stripped = re.sub(r"(?m)^#.*\n(?=(?:#.*\n)*POSTGRES_PASSWORD=)", "", example)
    assert "POSTGRES_PASSWORD=" in stripped
    assert _password_note(stripped) == ""


# --- §1: the image installs from pyproject, so imports must be declared there -------------


def _dist(spec: str) -> str:
    """The distribution name a PEP 508 requirement names."""
    return re.split(r"[><=\[!~;\s]", spec.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _declared_dependencies(pyproject_text: str) -> tuple[set[str], set[str]]:
    """(what the image installs, what only the extras carry).

    Parsed with `tomllib` rather than scraped line by line, because the scrape merged
    `[project].dependencies`, the `[dev]` extra and every other quoted string in the file —
    `packages = ["spielplan"]` included — into one set. The two halves have to stay apart:
    `ops/backend.Dockerfile` installs the first and not the second, so a production module
    importing `pytest` or `cbor2` builds a healthy image and dies on its first import.
    [M4.7 ti-dependency-guard]
    """
    project = tomllib.loads(pyproject_text).get("project", {})
    runtime = {_dist(spec) for spec in project.get("dependencies", [])}
    extras = {
        _dist(spec)
        for group in project.get("optional-dependencies", {}).values()
        for spec in group
    }
    return runtime, extras - runtime


def _undeclared_imports(pyproject_text: str, modules: set[str]) -> tuple[list[str], list[str]]:
    """(imports the image will not have, imports this environment could not attribute)."""
    from importlib.metadata import packages_distributions

    runtime, extras_only = _declared_dependencies(pyproject_text)
    dist_of = packages_distributions()
    undeclared: list[str] = []
    unresolved: list[str] = []
    for module in sorted(modules):
        dists = {d.lower().replace("_", "-") for d in dist_of.get(module, ())}
        if not dists:
            # Not installed here, so this environment cannot say which distribution owns it.
            # Named in the failure message rather than silently dropped: the escape is what
            # keeps the guard runnable on a partial virtualenv, and a blind spot nobody can
            # see is how the previous version of this guard came to certify nothing.
            unresolved.append(module)
            continue
        if dists & runtime:
            continue
        extra = " — declared only in an extra, which the image does not install"
        undeclared.append(f"{module} (provided by {sorted(dists)}){extra if dists & extras_only else ''}")
    return undeclared, unresolved


def test_every_third_party_import_is_a_declared_dependency():
    """The container installs `[project].dependencies` from `pyproject.toml` and nothing else.

    A package that is in a developer's virtualenv but not in that list builds a perfectly
    healthy image which then dies on its first import — the failure is at *runtime*, in the
    container, after everything green. This walks the actual imports instead.
    """
    import ast
    import sys

    modules: set[str] = set()
    for path in (REPO / "backend" / "spielplan").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])

    third_party = modules - set(sys.stdlib_module_names) - {"spielplan", "__future__"}
    undeclared, unresolved = _undeclared_imports(PYPROJECT.read_text(encoding="utf-8"), third_party)
    assert not undeclared, (
        f"imported but not in pyproject dependencies: {undeclared}"
        f"\n(not installed here, so unchecked: {unresolved})"
    )


def test_the_dependency_guard_does_not_merge_the_dev_extra_into_the_image():
    """The scrape's own output: `pytest`, `ruff` and the wheel's `packages = ["spielplan"]`
    all landed in `declared`, which is why a production `import pytest` passed it."""
    runtime, extras_only = _declared_dependencies(PYPROJECT.read_text(encoding="utf-8"))
    assert "fastapi" in runtime and "asyncpg" in runtime
    assert "pytest" in extras_only and "pytest" not in runtime
    assert "spielplan" not in runtime, "a hatch build target is not a dependency"


def test_the_dependency_guard_catches_a_production_import_of_a_dev_only_package():
    """The synthetic violation the docstring above describes, fed to the guard itself: a
    production module doing `import pytest` must fail here, and say why."""
    undeclared, _ = _undeclared_imports(PYPROJECT.read_text(encoding="utf-8"), {"pytest"})
    assert undeclared and "the image does not install" in undeclared[0], undeclared


# --- §9: no vendor SDK reaches the LLM layer ----------------------------------------------------
#
# §9's constraint is "no vendor SDKs; one POST per provider through the rate-limited fetcher", and
# until M5.5 it held only by absence: the dependency spec carried httpx and nothing that speaks to a
# provider, so the first person to reach for `openai` to write a shorter adapter would have broken a
# spec clause nothing was watching. It is two guards in the torch guard's shape, each read off an
# artifact rather than a runtime - the dependency spec every consumer installs from, and the modules
# under `spielplan/llm/` - because the second is what the first cannot see: a module can import a
# package the spec never names, and one that opens its own HTTP client has left the fetcher without
# importing any SDK at all. The fetcher is where the per-host politeness, the breaker and the retry
# budget live (`acquire/fetch.py`), so a provider client that bypasses it has none of the three.
# [M5.5 plan B6, §6]
#
# A DENYLIST AND NOT AN ALLOWLIST, and the difference is what keeps this a rule about §9 rather than
# about taste. The allowlist of this project's dependencies IS `pyproject.toml`; a second copy of it
# here would go red on every unrelated addition - a guard about LLM SDKs failing because someone
# added a compression library - and would be updated by rote, which is how an allowlist stops being
# read. What §9 forbids is a class, and the class is enumerable: each provider's own SDK under every
# distribution name it has shipped (Google alone has published four), and the wrappers that make the
# call through one of those or through a client of their own. The list's blind spot is a wrapper
# nobody has named yet, and that is the import guard's job: whatever the distribution is called, a
# module under `llm/` that imports it, or that imports an HTTP client instead of `acquire.fetch`,
# fails there.
LLM_SDK_DISTRIBUTIONS = frozenset({
    # The three providers M5.5 speaks to, under every name each has published a Python SDK as.
    "anthropic", "openai",
    "google-generativeai", "google-genai", "google-ai-generativelanguage",
    "google-cloud-aiplatform", "vertexai",
    # Wrappers that route a provider call through one of those or through a client of their own.
    "litellm", "instructor", "openai-agents",
})
# Families published as one distribution per integration (`langchain-openai`, `llama-index-llms-
# gemini`, `pydantic-ai-slim`): the family's name and any `<name>-...` distribution.
LLM_SDK_FAMILIES = ("langchain", "llama-index", "pydantic-ai")

# The same class as import paths, plus the HTTP clients a module would reach for instead of the
# fetcher. `urllib.request` and `http.client` and not `urllib` or `http`: `gemini.py` quotes its model
# name with `urllib.parse`, which opens nothing.
LLM_SDK_MODULES = (
    "anthropic", "openai", "google.generativeai", "google.genai", "google.ai.generativelanguage",
    "google.cloud.aiplatform", "vertexai", "litellm", "instructor",
)
LLM_SDK_MODULE_FAMILIES = ("langchain", "llama_index", "pydantic_ai")
HTTP_CLIENT_MODULES = ("httpx", "requests", "aiohttp", "urllib3", "urllib.request", "http.client")

LLM_PACKAGE = REPO / "backend" / "spielplan" / "llm"


def _is_llm_sdk(dist: str) -> bool:
    """A normalised distribution name (`_dist`'s) that §9 forbids: listed, or of a listed family."""
    return dist in LLM_SDK_DISTRIBUTIONS or any(
        dist == family or dist.startswith(family + "-") for family in LLM_SDK_FAMILIES
    )


def _declared_llm_sdks(pyproject_text: str) -> list[str]:
    """Every LLM SDK the dependency spec declares, in the image's list or in any extra.

    The extras are read as well as the image's list, and on purpose: `[dev]` is what the tests and
    `ops/` run under, and an SDK there is the one a test double or an exit script would be written
    against instead of the fetcher - a second door to the provider that the image then lacks.
    """
    runtime, extras_only = _declared_dependencies(pyproject_text)
    return sorted(dist for dist in runtime | extras_only if _is_llm_sdk(dist))


def _imports_around_the_fetcher(source: str, label: str) -> list[str]:
    """Every import in `source` of an LLM SDK or of an HTTP client, as `label:line: module`.

    Judged on the MODULE PATH and never on the name bound: `from spielplan.llm import openai` is
    this package's own adapter and names nothing third-party, while `from openai import OpenAI` is
    the SDK. A relative import is the package's own by definition. For `from X import Y` both `X`
    and `X.Y` are candidates, because `from google import genai` and `from urllib import request`
    spell the forbidden path across the two halves of the statement.
    """
    banned = LLM_SDK_MODULES + HTTP_CLIENT_MODULES
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            candidates = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            candidates = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
        else:
            continue
        for path in candidates:
            if any(path == name or path.startswith(name + ".") for name in banned) or any(
                path.split(".")[0].startswith(family) for family in LLM_SDK_MODULE_FAMILIES
            ):
                hits.append(f"{label}:{node.lineno}: {path}")
                break
    return hits


def test_no_llm_provider_sdk_is_declared_in_the_dependency_spec():
    """§9: "no vendor SDKs; one POST per provider through the rate-limited fetcher".

    Read off `pyproject.toml` because that is what every consumer installs from - the image, CI's
    jobs and a developer's `-e ".[dev]"` alike, which is the torch guard's argument above for the
    same file. The premise is asserted beside the rule: the spec carries the one HTTP library the
    fetcher is built on, so a reader that parsed nothing would fail here rather than pass.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    runtime, _ = _declared_dependencies(text)
    assert "httpx" in runtime, "the dependency reader no longer finds httpx, so it is reading nothing"
    declared = _declared_llm_sdks(text)
    assert not declared, (
        "backend/pyproject.toml declares an LLM provider SDK or wrapper, and section 9 allows none: "
        f"every provider call is one POST through acquire.fetch.Fetcher: {declared}"
    )


def test_the_sdk_dependency_guard_sees_a_declared_sdk():
    """docs/TESTING.md: "a guard that cannot fail reads as coverage while providing none".

    Each shape a real addition takes - a provider's own SDK in the image's list with an extra of its
    own, a Google name, a wrapper family's integration package in `[dev]` - and the near misses
    that must stay silent, `openapi-*` above all: a substring rule would read the OpenAPI tooling a
    FastAPI project plausibly adds as the OpenAI SDK.
    """
    def toml_list(items: list[str]) -> str:
        return "[" + ", ".join(f'"{item}"' for item in items) + "]"

    def spec(runtime: list[str], dev: list[str]) -> str:
        return (f"[project]\nname = \"probe\"\ndependencies = {toml_list(runtime)}\n"
                f"[project.optional-dependencies]\ndev = {toml_list(dev)}\n")

    caught = _declared_llm_sdks(spec(
        ["httpx>=0.27", "openai>=1.40", "anthropic[vertex]>=0.34", "google-genai==1.2"],
        ["pytest>=8.3", "langchain-openai>=0.2", "llama-index-llms-gemini", "LiteLLM>=1.0"],
    ))
    assert caught == [
        "anthropic", "google-genai", "langchain-openai", "litellm", "llama-index-llms-gemini",
        "openai",
    ], caught
    assert _declared_llm_sdks(spec(
        ["httpx>=0.27", "openapi-core>=0.19", "openapi-spec-validator", "langchainish"], ["pytest"],
    )) == []


def test_no_llm_module_imports_a_vendor_sdk_or_an_http_client_of_its_own():
    """The modules half: nothing under `spielplan/llm/` imports a provider SDK, a wrapper, or an
    HTTP client - httpx included, which the fetcher itself is built on and which is exactly the
    import a "simpler" adapter would add to post once without the fetcher's pacing, breaker and
    retry budget. [§9; M5.5 plan B6]

    Both halves of the reading are asserted. The walk has to reach the three adapters and the
    client, or a moved package would pass it by being empty; and each adapter has to import
    `acquire.fetch`, which is the positive form of the rule - the door every provider call goes
    through is the fetcher, and a module that stopped importing it has found another one.
    """
    modules = sorted(LLM_PACKAGE.rglob("*.py"))
    names = {path.name for path in modules}
    assert {"client.py", "anthropic.py", "openai.py", "gemini.py"} <= names, (
        f"the walk read {sorted(names)} under {LLM_PACKAGE}; it is looking in the wrong place"
    )
    offenders = [
        hit
        for path in modules
        for hit in _imports_around_the_fetcher(_src(path), path.relative_to(REPO).as_posix())
    ]
    assert not offenders, (
        "a module under spielplan/llm/ imports an LLM SDK or an HTTP client of its own; section 9 "
        "puts every provider call through acquire.fetch as one POST:\n  " + "\n  ".join(offenders)
    )
    through_the_fetcher = {
        path.name for path in modules
        if any(
            isinstance(node, ast.ImportFrom) and node.module == "spielplan.acquire"
            and any(alias.name == "fetch" for alias in node.names)
            for node in ast.walk(ast.parse(_src(path)))
        )
    }
    assert {"client.py", "anthropic.py", "openai.py", "gemini.py"} <= through_the_fetcher, (
        f"only {sorted(through_the_fetcher)} import acquire.fetch; an adapter that stopped has found "
        "another way to the provider"
    )


@pytest.mark.parametrize(
    ("source", "caught"),
    [
        # Every spelling of the SDK import, including the two that split the path across `from`
        # and `import`, and the wrapper families by their integration packages.
        ("import openai", True),
        ("import anthropic as vendor", True),
        ("from openai import AsyncOpenAI", True),
        ("from google import genai", True),
        ("import google.generativeai as genai", True),
        ("from google.cloud import aiplatform", True),
        ("from langchain_openai import ChatOpenAI", True),
        ("import litellm", True),
        # A client of its own, which is the route around the fetcher that imports no SDK at all.
        ("import httpx", True),
        ("from httpx import AsyncClient", True),
        ("from requests import Session", True),
        ("import aiohttp", True),
        ("from urllib import request", True),
        ("import urllib.request", True),
        ("from http import client", True),
        # And the imports this package really makes, which must stay silent: its own adapters by
        # absolute and by relative path, the fetcher, and the quoting `gemini.py` does.
        ("from spielplan.llm import anthropic, gemini, openai", False),
        ("from . import openai", False),
        ("from spielplan.acquire import fetch", False),
        ("from urllib.parse import quote", False),
        ("from http import HTTPStatus", False),
    ],
)
def test_the_llm_import_guard_sees_every_spelling_of_an_sdk_or_a_client(source, caught):
    assert bool(_imports_around_the_fetcher(source + "\n", "probe.py")) is caught, source


# --- CLAUDE.md: `api/` decides only HTTP shapes; app.py is the only thing that mounts it ---


def _router_paths() -> dict[str, set[str]]:
    """Every module under `spielplan/api/` that exports a `router`, and the paths it declares.

    Read off the router object rather than off `app.routes`, because the two are exactly what
    must be compared. WebSocket routes and anything carrying `include_in_schema=False` are left
    out: the comparison below is against the OpenAPI document, which does not carry them either.
    A Starlette converter is dropped for the same reason — two routes here are declared
    `{credential_id:path}` (a passkey's credential id is base64url and carries `/`), and OpenAPI
    names the parameter without it.
    """
    import importlib

    from fastapi.routing import APIRoute

    declared: dict[str, set[str]] = {}
    for path in sorted((REPO / "backend" / "spielplan" / "api").glob("*.py")):
        if path.stem.startswith("_"):
            continue
        router = getattr(importlib.import_module(f"spielplan.api.{path.stem}"), "router", None)
        if router is None:
            continue
        declared[path.stem] = {
            re.sub(r"\{(\w+):\w+\}", r"{\1}", route.path)
            for route in router.routes
            if isinstance(route, APIRoute) and route.include_in_schema
        }
    return declared


def _unmounted_routers(served: set[str]) -> dict[str, set[str]]:
    return {name: paths - served for name, paths in _router_paths().items() if paths - served}


def test_every_router_under_api_is_mounted_on_the_application():
    """The one guard in this file that asserts something is *present*.

    Every other rule here is "the artifact must not do X"; this one exists because the pytest
    layer could not see `app.py` dropping an `include_router`. Five test files mounted their own
    router on the way in, guarded by `getattr(route, "path", None) == ...` over `app.routes` —
    and under FastAPI 0.141 an included router is one opaque `_IncludedRouter` with no `.path`,
    so the guard was always true and the mount always happened. With `home_api` and `rate_api`
    stripped from `create_app`, `test_home.py` and `test_rate_session.py` still passed 84/84
    while `/api/home` and the whole Rate surface answered 404. The scaffolds are gone; this is
    what stands in their place. [M4.7 tq2-router-mount]
    """
    from spielplan.app import create_app

    missing = _unmounted_routers(set(create_app().openapi()["paths"]))
    assert not missing, (
        "a router under spielplan/api/ declares paths the application does not serve — "
        f"`create_app` is missing an `include_router`: {missing}"
    )


def test_the_router_mount_guard_notices_a_dropped_include():
    """Fed the state the scaffolds hid: an application with Home not mounted."""
    from spielplan.app import create_app

    served = set(create_app().openapi()["paths"])
    assert "/api/home" in served, "the fixture for this self-test is a mounted Home"
    assert _unmounted_routers(served - {"/api/home"}) == {"home": {"/api/home"}}


def test_a_router_that_declares_no_paths_is_still_asserted_to_be_mounted():
    """The guard above compares PATH SETS, so a router with no paths is invisible to it.

    `api/events.py` is mounted with no routes in it (decision 332): M5.2 adds the webhook by
    adding a route to that file rather than by also remembering an `include_router` in `app.py`,
    which is the "remembering" the module exists to remove. But `_unmounted_routers` filters on
    `paths - served` being non-empty, and an empty set is never non-empty - so deleting
    `app.include_router(events_api.router)` changed nothing any test in this tree could observe,
    and the mount M5.2 is promised would be discovered missing by M5.2.

    That is the M4.7 tq2-router-mount blind spot in its second form, and it is asserted here
    rather than through a path because there is no path to assert. FastAPI 0.141 keeps the
    included router on the opaque `_IncludedRouter` as `original_router`, which states the mount
    directly: this is True today and False the moment that line is dropped.
    [M5.1 review cycle 1, M51-REV-EVENTS-04]
    """
    from spielplan.api import events as events_api
    from spielplan.app import create_app

    mounted = [
        route for route in create_app().routes
        if getattr(route, "original_router", None) is events_api.router
    ]
    assert len(mounted) == 1, (
        "`create_app` does not include `api/events.py`'s router - the /events namespace declares "
        "no paths yet, so nothing else in this suite can see the include go missing"
    )


def _self_mounted_routers(root: Path) -> list[str]:
    """Test files that mount a router THE APPLICATION OWNS, which is the rule's own wording.

    Any `.include_router(` at all was the first reading, and it convicts a router the test file
    builds in the same function out of `APIRouter()` -- which the application has never heard of
    and `create_app` therefore cannot drop. `test_layering_guards.py` mounts exactly such a probe,
    on purpose: FastAPI 0.141 stops flattening an included router, so a probe assembled any other
    way would not reproduce the nesting that hid the Tonight channel from the route walk, and the
    rule meant to stop a suite testing a router instead of the application was refusing the one
    construction that tests the real nesting. A router the file constructs itself is exonerated by
    name; anything else -- including a bare `home_api.router` the scanner cannot resolve -- stays
    an offence, because the scaffolds this was written against all mounted an imported one.
    [M4.7 tq2-router-mount; M4.16]
    """
    offenders = []
    for path in sorted(root.rglob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        built_here = set(re.findall(r"^\s*(\w+)\s*=\s*APIRouter\(", source, re.M))
        for number, line in enumerate(source.splitlines(), 1):
            mounted = re.match(r"^\s*[\w.]+\.include_router\(\s*([\w.]+)", line)
            if mounted and mounted.group(1).split(".")[0] not in built_here:
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    return offenders


def test_no_test_file_mounts_a_router_the_application_owns():
    """The other half, and the reason the guard above can be trusted.

    A suite that mounts the router it is testing asserts the router and never the application,
    so `create_app` can drop the include with four CI jobs green. Mounting one here is not a
    convenience to be re-added later; it is the blindness this milestone removed.
    """
    offenders = _self_mounted_routers(REPO / "backend" / "tests")
    assert not offenders, (
        "a test mounts a router `spielplan/app.py` is responsible for, which makes the mount "
        "untestable:\n  " + "\n  ".join(offenders)
    )


def test_the_scaffold_guard_catches_a_re_mounted_router(tmp_path):
    (tmp_path / "test_scaffold.py").write_text(
        "def test_x(app):\n    application = app()\n"
        "    application.include_router(home_api.router)\n",
        encoding="utf-8",
    )
    caught = _self_mounted_routers(tmp_path)
    assert len(caught) == 1 and "test_scaffold.py:3" in caught[0], caught


def test_the_scaffold_guard_leaves_a_probe_router_the_test_built_itself_alone(tmp_path):
    """The other direction, and the reason the narrowing is not a hole.

    A router assembled in the same file is not one `create_app` can drop an include for, so
    convicting it bought no coverage and cost the route walk the only probe that reproduces
    FastAPI's real nesting. The second file is what keeps the exoneration from swallowing the
    rule: the same mount, of a router that came from somewhere this scanner cannot see, is still
    an offence -- so the allowance is "the file built it", never "the line looks like a probe".
    """
    (tmp_path / "test_probe.py").write_text(
        "def test_x():\n    router = APIRouter(prefix='/api')\n"
        "    probe = FastAPI()\n    probe.include_router(router)\n",
        encoding="utf-8",
    )
    assert _self_mounted_routers(tmp_path) == []

    (tmp_path / "test_imported.py").write_text(
        "def test_y():\n    router = APIRouter(prefix='/api')\n"
        "    probe = FastAPI()\n    probe.include_router(rate_api.router)\n",
        encoding="utf-8",
    )
    caught = _self_mounted_routers(tmp_path)
    assert len(caught) == 1 and "test_imported.py:4" in caught[0], caught


# --- §12: the eight exit scripts, and the console they print to ---------------------------
#
# §12's M2, M3, M4, M4.9, M4.11 and M4.12 rows are measured by hand, by `ops/m*_exit_criterion.py`,
# and a milestone is closed on what they print and the code they exit with. A verdict that
# cannot come out `no` is a certificate rather than a measurement, so these read the scripts as
# source: no `check()` whose answer is settled before the run, no dereference of a result the
# verdict has not been computed from yet, no `main()` ending in a literal, and nothing printed
# that a Windows console can crash on.
#
# The count each guard asserts is a tripwire rather than a fact worth keeping current for its
# own sake: it fires when a milestone adds a script these four rules have never been read
# against, which is the only way a new exit criterion could inherit the defects M4.8 found in
# the three that existed. `ops/m49_exit_criterion.py` is the fourth, and the number moved only
# after it was measured against all four -- no printed literal outside cp850, no predicate
# settled before the run, no terminal constant, and no `rate()` seeding path for the last of
# them to read, the same exemption `ops/m45_exit_criterion.py` has for writing through the
# importer rather than through §6.1's routes. [M4.9]
#
# `ops/m411_exit_criterion.py` is the fifth, and the number moved to 5 only after every rule
# here had been read against it and come back empty: no printed literal outside cp850, no
# `check()` predicate settled before the run, a computed terminal verdict, and no component
# read with its comments in. It has no `rate()` seeding path for the last rule to exempt --
# §12's M1 claim is about seen states rather than verdicts, so it seeds through `sync.seen`
# and `sync.playback`, the two modules whose two-way flow it exists to falsify. The one shape
# the rules did force on it is its `check()` signature: the verdict is the first positional
# argument, because a number there is exactly what `_constant_check_predicates` reads as a
# predicate that cannot fail. [M4.11]
#
# `ops/m412_exit_criterion.py` is the sixth, and the number moved to 6 only after every rule here
# had been read against it: no printed literal outside cp850, no `check()` predicate settled
# before the run, a computed terminal verdict, and no component read at all -- §12's M4.12 row is
# about an evening that resolves on the real pool, so what it reads is the app over ASGI, the
# database it wrote to, and the loop it shares. It has no `rate()` seeding path for the last rule
# to exempt: its household is fitted through `foldin.run`, the app's own §5.3 pass, because
# §6.2 step 3's candidate pool IS `user_score` and a harness that seeded that table would be
# measuring its own idea of a score. Three of its twelve checks are source reads (decision 165's
# TV retirement, the one lifecycle owner, the coverage map's close) and the docstring says so
# rather than dressing them as runtime facts. [M4.12 decision 224]
#
# `ops/m413_exit_criterion.py` is the SEVENTH, and 6 became 7 in the five places below only after
# every rule here had been read against it: no printed literal outside cp850 (and it escapes what
# it did not author twice over -- the importer's em-dashed finding messages AND the 104 corpus
# title names of v20260828 that leave the code page, because three of its checks name a title), no
# `check()` predicate settled before the run, a computed terminal verdict, no component read at
# all, nothing that can fail between its CREATE DATABASE and the block whose finally drops it, an
# `except Exception` around the measurement that reports rather than propagates, and both arms on
# every numbered heading it prints. It has no `rate()` seeding path for that rule to exempt: it
# seeds through `observations.record_verdict` and the importer, which is
# `ops/m45_exit_criterion.py`'s own exemption and for the same reason -- what it measures is the
# basis a fit is computed in, not the route that records a tap. THE HAND-MERGE LANDS ON 7: M4.12
# is adding `ops/m412_exit_criterion.py` in a parallel worktree and bumps these same five
# assertions to 6 for its own script, so whichever of the two merges second resolves them to 7
# rather than taking one side. [M4.13]
#
# `ops/m414_exit_criterion.py` is the EIGHTH, and 7 became 8 in the five places below only after
# every rule here had been read against it: no printed literal outside cp850 (it escapes what it
# did not author twice over -- the importer's em-dashed finding messages, which it prints on every
# refused probe, and the corpus's own file names and paths, which reach the console from a bundle
# built on another machine), no `check()` predicate settled before the run, a computed terminal
# verdict, no component read at all, nothing that can fail between its CREATE DATABASE and the
# block whose finally drops it, an `except Exception` around the measurement that reports rather
# than propagates, and both arms on every numbered heading it prints -- thirteen of them, each
# naming the plan's own check number, because it runs them in the order the install state allows
# rather than in the order the plan tabulates them. It has no `rate()` seeding path for the last
# rule to exempt: it seeds through `POST /api/admin/bundle/import` and the worker's own tick,
# which is `ops/m45_exit_criterion.py`'s exemption one layer up -- what it measures IS the import
# route, so a harness that wrote rows another way would be measuring itself. It is also the first
# of the eight to start a CHILD PROCESS: §5.3 gives the import to the worker and the criterion's
# health check is that the BACKEND's loop is not the one it blocks, which a single-process harness
# cannot measure at all. [M4.14, decision 255]
#
# THE HAND-MERGE LANDED ON 8: M4.15 merged without bringing a script of its own, so the note
# above resolved on the number it named rather than on its alternative. M5.1 is what moved it.
#
# `ops/m51_exit_criterion.py` is the NINTH, and 8 became 9 in the five places below only after
# every rule here had been read against it: no printed literal outside cp850 (it escapes what
# it did not author twice over -- the park reasons `acquire/stages.py` writes for an operator
# and Home's own why-line, both em-dashed and both reaching the console), no `check()`
# predicate settled before the run, a computed terminal verdict, no component read at all --
# check 4 RESTATES `PosterCard.svelte`'s `isColdPlaced` rather than grepping it, which is that
# rule in force rather than a gap in its reach -- nothing that can fail between its CREATE
# DATABASE and the block whose finally drops it, an `except Exception` around the measurement
# that reports rather than propagates, and both arms on every numbered heading it prints. It
# has no `rate()` seeding path for the last rule to exempt: it seeds through
# `bundle_import.import_bundle` and `pipeline.drain`, which is `ops/m45_exit_criterion.py`'s
# exemption for the same reason one layer along -- what it measures IS the driver, so a harness
# that walked a title through the stages itself would be measuring its own idea of the walk.
#
# It is the first of the nine whose tally has a THIRD column. Two of its twelve checks need a
# container and a port -- `/data/raw`'s absence from the backend image, and `/events` answering
# 404 through the app that ships -- and a lane with neither prints "NOT MEASURED HERE" and
# exits 3, which is neither a pass nor a failure. That third exit code is the same instinct as
# `_constant_check_predicates` one level up: a check that cannot fail is worthless, and a check
# reported as passing because nothing asked it is worse than worthless. [M5.1, decision 184]
#
# M5.2 THROUGH M5.7 EACH OWE A SCRIPT under decision 321, so these five assertions move again
# per sub-milestone. M5.1 runs alone and there is no sibling to hand-merge against; the next
# wave resolves the number to what `ls ops/m*_exit_criterion.py` reports, having first read its
# own script against every rule below rather than bumping a constant to buy a green run.
#
# `ops/m52_exit_criterion.py` is the TENTH, and 9 became 10 in the five places below only after
# the same reading. No literal outside cp850 -- it prints `acquire/intake.py`'s refusal reasons,
# the importer's findings and the double's own 409 text, and every one of those goes through
# `console()` because none of them is its own. No `check()` predicate settled before the run:
# the eleven verdicts are conjunctions over counts, key sets and intake states the run read back
# out of the database, and each one is bound once so the return arm above can settle it. A
# computed terminal verdict. No component read at all, which is a fact about the milestone
# rather than about the script -- M5.2 ships no surface, so `05-milestones.spec.js` keeps every
# placeholder it has. Nothing that can fail between its CREATE DATABASE and the block whose
# finally drops it, an `except Exception` around the measurement that reports rather than
# propagates, and both arms on every numbered heading it prints. No `rate()` seeding path for
# the last rule to exempt: it seeds through `bundle_import.import_bundle` and through §6.6's own
# PUT and §7.3's own link route, and every refusal on that path raises `PreconditionFailed` with
# the sentence the rule is really about.
#
# It is the first of the ten with NO third column, and that is worth a line beside M5.1's. Every
# claim §7.2 makes is an HTTP fact, so the app is driven over `ASGITransport` and Jellyfin is
# `ops/fake_jellyfin.py` mounted in-process: there is no container to inspect and no port to
# probe, so no check can be one a lane could not measure, and the exit codes are 0, 1 and the 2
# it refuses the fixture with. A script whose checks all measure needs no way to say that one
# did not. [M5.2, decision 371]
#
# M5.3 IS THE FIRST WAVE WITH SIBLINGS, and the paragraph above is what it did: the number is
# `ls ops/m*_exit_criterion.py` in THIS lane's tree, which holds M5.3's script and not the two
# its siblings are writing in parallel. So the assertion is right on the branch and wrong on the
# merge, deliberately - five conflicts a person resolves by counting the directory again is the
# shape decision 184 asks for, and a lane that had left it at nine to avoid them would have
# published a figure nobody re-derived. Every rule below was read against
# `ops/m53_exit_criterion.py` before the number moved. [M5.3, decision 378]
#
# THE HAND-MERGE LANDED ON 11 -- and then the count came out altogether. M5.2 and M5.3 each moved
# it from 9 to 10 in their own tree, git took the identical edit once and silently, and five
# assertions read 10 over a directory holding eleven. A constant every new script has to bump, and
# every parallel merge has to re-derive, buys no rule this block does not already hold: each sweep
# below reads EVERY script the glob finds, so a new script is held to all of them the moment it
# exists. The five assertions now require only that the glob finds something. [decision 460]
#
# `ops/m55_exit_criterion.py` was read against every rule here before it landed, and came back
# empty. No printed literal outside cp850:
# every literal it holds bar its docstrings is ASCII, and what it did not author goes through
# `console()` on the way out - decision 348's park sentence with its section sign, the over-cap
# reason, the retry prompts as the double received them with the model's own quotes inside, the
# app's warnings it summarises and the httpx line it quotes - and where a failure has to show where
# a key landed it prints `[redacted]` in the key's place, the double's own log's rule. No `check()`
# predicate settled before the run: its fourteen verdicts are conjunctions over what the double
# recorded and what the run read back out of `llm_call`, `dna_tag`, `dna_reject`, `raw_document`,
# the board and the queue, each bound once. A computed terminal verdict. No component read at all,
# because the milestone ships no surface. Nothing that can fail between its CREATE DATABASE and the
# block whose finally drops it - its log captures are built before the CREATE for exactly that
# reason - an `except Exception` around the measurement that reports rather than propagates, and
# both arms on every numbered heading it prints. It has no `rate()` seeding path for the last rule
# to exempt: it seeds through `registry.save_connector`, the importer's `load_vocabulary`,
# `dna.packs.store_pack` and `pipeline.drain`, which is `ops/m45_exit_criterion.py`'s exemption for
# the same reason - what it measures IS stage 6 walked by the driver, so a harness that wrote the
# rows it reads back would be measuring itself. And like M5.2's it has NO THIRD COLUMN:
# `ops/fake_llm.py` is mounted in-process as the transport of the real Fetcher, so every check
# measures in any lane with a Postgres and the exit codes are 0, 1 and the 2 it refuses a missing
# database or double with. [M5.5, decision 435]

EXIT_SCRIPTS =tuple(sorted((REPO / "ops").glob("m*_exit_criterion.py")))
COVERAGE_REPORT = REPO / "backend" / "tests" / "test_spec_coverage.py"


def _docstrings(tree: ast.AST) -> set[int]:
    """`id()` of every docstring constant: an `ast.Expr` heading a module/class/function body.

    Excluded from the console guard deliberately. `ops/m3_exit_criterion.py`'s module docstring
    quotes §5.1 with a beta and a rho in it, and `ops/m4_exit_criterion.py`'s quotes §12's M4
    row with an en dash; Python never writes a docstring to stdout, so flagging those would
    make this a rule about source aesthetics rather than about what the console has to render.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)):
            continue
        if isinstance(first.value.value, str):
            out.add(id(first.value))
    return out


def _literals(node: ast.AST, skip: set[int]) -> list[tuple[int, str]]:
    """Every string literal under `node`, docstrings excluded.

    f-strings need no special case: `ast.JoinedStr` holds its literal halves as `ast.Constant`
    children, so walking constants collects them, while the interpolated values -- runtime
    data, not source -- stay correctly out of reach.
    """
    return [
        (sub.lineno, sub.value)
        for sub in ast.walk(node)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and id(sub) not in skip
    ]


def _printed_literals(source: str) -> list[tuple[int, str]]:
    """Only the string literals that reach a `print(...)` call."""
    tree = ast.parse(source)
    skip = _docstrings(tree)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "print":
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            out.extend(_literals(arg, skip))
    return out


def _all_literals(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    return _literals(tree, _docstrings(tree))


def _not_encodable(pairs: list[tuple[int, str]], label: str) -> list[str]:
    """The offenders, named by codepoint so this message survives its own subject."""
    out: list[str] = []
    for lineno, value in pairs:
        try:
            value.encode("cp850")
        except UnicodeEncodeError as exc:
            point = f"U+{ord(value[exc.start]):04X}"
            shown = value[:70].encode("ascii", "backslashreplace").decode()
            out.append(f"{label}:{lineno}: {point} in {shown!r}")
    return out


def _non_cp850_console_strings() -> list[str]:
    """Two arms, and the width of each one is measured rather than assumed.

    For the three exit scripts and the coverage report -- files whose entire output *is* a
    console report -- the unit is every string literal bar docstrings. Applying that rule to
    those four files flagged exactly four sites, every one of them printed.

    Widening that same rule to the rest of `ops/` flags thirty-one `ops/devstub.py` strings,
    and every one is part of an HTTP response -- an `HTTPException` detail, a `why` or `note`
    field the SPA renders, a shelf suppression reason, an audit-log entry -- travelling to a
    browser over a UTF-8 wire and reaching no console at all. So for every other `ops/*.py`
    the unit narrows to the literals that reach a `print(...)`. That arm flags nothing today,
    which is exactly its job: the next non-ASCII console line anywhere in `ops/` is caught,
    and a response body is not asked to be ASCII for a reason that does not apply to it.
    """
    offenders: list[str] = []
    report_files = EXIT_SCRIPTS + (COVERAGE_REPORT,)
    for path in report_files:
        offenders += _not_encodable(_all_literals(path.read_text(encoding="utf-8")), path.name)
    for path in sorted((REPO / "ops").glob("*.py")):
        if path in report_files:
            continue
        offenders += _not_encodable(_printed_literals(path.read_text(encoding="utf-8")), path.name)
    return offenders


def test_no_console_output_leaves_the_oem_code_page():
    """CLAUDE.md: "Keep console/test output ASCII -- Windows cp1252 consoles crash".

    Not a style rule. A `print` of an em dash on a cp850 console raises UnicodeEncodeError from
    inside the print, so the script dies on the line it was reporting from and the operator
    gets a traceback where the measurement should have been -- which is how a run of
    `test_spec_coverage.py` under `PYTHONIOENCODING=cp850` lost its own milestone ledger.
    """
    assert EXIT_SCRIPTS, "no ops/m*_exit_criterion.py found, so every rule below reads nothing"
    offenders = _non_cp850_console_strings()
    assert not offenders, (
        "a string a milestone script prints cannot be encoded on a Windows console:\n  "
        + "\n  ".join(offenders)
    )


def test_the_console_encoding_guard_catches_a_non_ascii_print():
    """Both halves: the print arm sees the violation, and it does not see a response body.

    The second half is the measurement the two arms exist for. `ops/devstub.py` carries
    thirty-one non-ASCII strings today and every one travels out over HTTP; a guard that
    flagged those would have needed a waiver on its first run, and a waived guard is the
    shape `docs/TESTING.md` calls coverage that provides none.
    """
    caught = _not_encodable(_printed_literals('print("a run \u2014 x")\n'), "probe.py")
    assert len(caught) == 1 and caught[0].startswith("probe.py:1: U+2014 "), caught
    # And the complaint is readable on the console it is complaining about: the offender is
    # named by codepoint and its context escaped, so this guard cannot crash on its own
    # subject the way the code it guards did.
    assert caught[0].isascii(), caught

    innocent = (
        '"""A module docstring with \u03b2 and \u03c1 in it."""\n'
        'raise HTTPException(status_code=404, detail="no bundle \u2014 import one first")\n'
        'print("plain ASCII, printed")\n'
    )
    assert _not_encodable(_printed_literals(innocent), "probe.py") == []
    # ... and the wide arm would have taken the response body with it, which is the whole
    # reason the wide arm is not the one pointed at `devstub.py`.
    assert len(_not_encodable(_all_literals(innocent), "probe.py")) == 1


# What "a constant predicate" means in this project's own words, which is wider than `ast.Constant`
# and is the reading every record of it takes: decision 240 glosses it "a check whose predicate
# cannot be evaluated", `docs/RELEASE.md` "a check that cannot fail", and the comment heading this
# section "no `check()` whose answer is settled before the run". The rule held two AST SHAPES --
# a truthy literal, and a BoolOp with a literal operand -- so `check(1 == 1, ...)`,
# `check(not False, ...)`, `check(True and True, ...)` and `check(placed == 0 or [1], ...)` all
# passed, the last because a truthy list is not an `ast.Constant`. Measured against the real
# scripts: the two shapes M4.8 recorded as repaired are caught, and those four are not.
#
# A CLOSED NODE SET, and the closure is the point rather than a limitation to apologise for. What
# is decidable here is a predicate built out of literals, `not`, `and`/`or` and a comparison
# between two of those; the moment a name, a call, an attribute or a subscript appears, the run
# decides the answer and this rule must say nothing, because that is what a check is. So
# `check(placed >= 0, ...)` -- a live predicate widened until it cannot fail -- is outside what any
# static reader can reach, and the row's `what` says so rather than promising it.
# [decision 240; M4.16 cycle 4, M416-C4-REL-05]
_UNSETTLED = object()
_COMPARISONS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
    ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.Is: operator.is_, ast.IsNot: operator.is_not,
}


def _settled(node: ast.expr):
    """The value a reader can work out from the source alone, or `_UNSETTLED`."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
        try:
            return ast.literal_eval(node)
        except ValueError:
            return _UNSETTLED
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = _settled(node.operand)
        return _UNSETTLED if value is _UNSETTLED else (not value)
    if isinstance(node, ast.BoolOp):
        values = [_settled(operand) for operand in node.values]
        if any(value is _UNSETTLED for value in values):
            return _UNSETTLED
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Compare):
        left = _settled(node.left)
        for op, node_right in zip(node.ops, node.comparators, strict=True):
            compare = _COMPARISONS.get(type(op))
            right = _settled(node_right)
            if left is _UNSETTLED or right is _UNSETTLED or compare is None:
                return _UNSETTLED
            if not compare(left, right):
                return False
            left = right
        return True
    return _UNSETTLED


def _fixed_predicate(node: ast.expr) -> str | None:
    """Why this predicate's answer is settled before the run, or None if it can vary.

    Rejected: a predicate that settles TRUE -- a truthy literal, which records a PASS whatever
    happened, and every expression over literals that comes out the same way -- and a boolean
    expression with an operand that pins the result (`x or True`, `x and False`), which is a
    predicate that was written to vary and no longer does.

    Allowed: a predicate that settles FALSE. `ops/m45_exit_criterion.py`'s block loop reports one
    per block that trains on counts and arrives as a presence bit; the surrounding `if` has
    already established that failure and the literal is only how it gets recorded. A deliberate
    FAIL is a report, an unconditional PASS is a certificate, and only the second one is a lie.
    """
    if isinstance(node, ast.Constant):
        return f"the literal {node.value!r}" if node.value else None
    settled = _settled(node)
    if settled is not _UNSETTLED:
        return (f"`{ast.unparse(node)}` is settled before the run and comes out true"
                if settled else None)
    if isinstance(node, ast.BoolOp):
        pin = isinstance(node.op, ast.Or)
        for operand in node.values:
            value = _settled(operand)
            if value is not _UNSETTLED and bool(value) is pin:
                joiner = "or" if pin else "and"
                return f"`{joiner} {ast.unparse(operand)}` pins the answer to {pin}"
    return None


def _check_verdict(node: ast.Call) -> ast.expr | None:
    """The expression a `check()` call hands in as its verdict, positionally or by keyword.

    `not node.args` was a `continue`, so `check(ok=True, label='x')` was skipped outright -- and
    `ok` is the name every one of these scripts declares that parameter with, so it is a legal
    call this reader simply did not read. The premise is asserted beside the rule.
    [M4.16 cycle 4, M416-C4-REL-05]
    """
    if node.args:
        return node.args[0]
    return next((kw.value for kw in node.keywords if kw.arg == "ok"), None)


# A CALL SITE IS NOT WHERE THESE SCRIPTS PUBLISH THEIR VERDICTS, and reading only call sites left
# the rule settling nothing about five of the nine. `ops/m51_exit_criterion.py` holds its twelve
# numbered verdicts in `check_one`..`check_twelve` and has four `check(` call sites, all inside one
# dispatcher -- two passing the literal `False` this rule deliberately allows, one passing the local
# name `verdict`. So `ok = True` inside `check_one` is M4.5's "a summary recorded as a pass" written
# one call frame out of the reader's reach, and the script still prints `12/12 checks passed`. The
# same shape counts 11 numbered checks against 3 call sites in M4.11, 12 against 3 in M4.12, 6
# against 4 in M4.13 and 13 against 4 in M4.14 -- and not one of those 54 functions contains a
# `check(` call at all. M4.5 and M4.9, the two the rule was measured against, declare no numbered
# check and write their verdicts straight into 14 and 22 calls, which is why it read as sound.
# [decision 184; M5.1 cycle 4, M51-C4-REL-01]
def _is_a_numbered_check(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A function that publishes one of the script's numbered verdicts, by name and by signature.

    Both spellings, because both ship: `-> tuple[bool | None, str]` in the M5.1 script and
    `-> tuple[bool, str, str]` in the four before it. The annotation is the gate rather than the
    name alone, because it is what makes "this returns a verdict" decidable from the source; a
    sibling test asserts every `check_*` in the tree still carries it, so the day a script drops
    the annotation is reported rather than quietly unread.
    """
    return (
        fn.name.startswith("check_")
        and fn.returns is not None
        and ast.unparse(fn.returns).startswith("tuple[")
    )


def _within(fn: ast.AST):
    """Every node inside `fn` except the bodies of the functions nested inside it.

    `ast.walk` would hand a closure's `return` to the check that encloses it --
    `ops/m412_exit_criterion.py`'s check 5 installs a patched `finish` that returns
    `await real_finish(...)` -- and a verdict is a claim about the numbered check, not about
    whatever it monkeypatched on the way in.
    """
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _sole_binding(fn: ast.AST, name: str) -> ast.expr | None:
    """The one expression this function binds `name` to, or None when a reader cannot say which.

    ONE binding or nothing, which is the same closure the predicate reader draws one level down:
    a name rebound in two branches is a value the run picks, and `for`, `with ... as` and
    `except ... as` are bindings whose value no reader of the source holds. Every one of those
    answers None, so the rule says nothing about that check rather than guessing at it.
    """
    taken = {arg.arg for arg in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
    taken |= {arg.arg for arg in (fn.args.vararg, fn.args.kwarg) if arg is not None}
    if name in taken:
        return None
    body = list(_within(fn))
    for node in body:
        if isinstance(node, ast.ExceptHandler) and node.name == name:
            return None
        if isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            return None
    stores = [
        node for node in body
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == name
    ]
    if len(stores) != 1:
        return None
    for node in body:
        if isinstance(node, ast.Assign) and node.targets == [stores[0]]:
            return node.value
        if isinstance(node, ast.AnnAssign) and node.target is stores[0]:
            return node.value
    return None


def _published_verdicts(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
    """The verdict expression of every `return <verdict>, ...` this numbered check publishes.

    A verdict returned as a bare name is followed to its binding, because `ok = True` and
    `return True, detail` are the same certificate and only the second is written at the return.
    A name the binding reader cannot settle is dropped rather than reported: this rule speaks
    about what it can read.
    """
    out: list[ast.expr] = []
    for node in _within(fn):
        if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)):
            continue
        if not node.value.elts:
            continue
        verdict = node.value.elts[0]
        if isinstance(verdict, ast.Name):
            bound = _sole_binding(fn, verdict.id)
            if bound is None:
                continue
            verdict = bound
        out.append(verdict)
    return out


def _constant_check_predicates(source: str, label: str) -> list[str]:
    """Read with `ast`, not a regex: a predicate wrapped across three lines is the same defect."""
    out: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "check":
            continue
        verdict = _check_verdict(node)
        if verdict is None:
            continue
        fixed = _fixed_predicate(verdict)
        if fixed:
            out.append(f"{label}:{node.lineno}: check({ast.unparse(verdict)}) -- {fixed}")
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _is_a_numbered_check(fn):
            continue
        for verdict in _published_verdicts(fn):
            fixed = _fixed_predicate(verdict)
            if fixed:
                out.append(
                    f"{label}:{verdict.lineno}: {fn.name} returns "
                    f"{ast.unparse(verdict)} -- {fixed}"
                )
    return out


def test_no_milestone_exit_check_has_a_constant_predicate():
    """Two of M4.5's eighteen checks could not fail, and both counted toward its published score.

    One stood in for §12's M2 exit criterion -- the query
    `backend/migrations/0008_placement.sql:63-64` builds a partial index for -- widened with a
    literal so it always passed; the other recorded a summary of the loop above it as a pass.
    The number behind the first was genuinely 0 on v20260828, so nothing was concealed on the
    day it was written; what was lost was the ability to notice the day it stops being 0.
    """
    assert EXIT_SCRIPTS, "no ops/m*_exit_criterion.py found, so every rule below reads nothing"
    offenders = [
        line
        for path in EXIT_SCRIPTS
        for line in _constant_check_predicates(path.read_text(encoding="utf-8"), path.name)
    ]
    assert not offenders, (
        "a milestone exit check reports a verdict it settled before the run:\n  "
        + "\n  ".join(offenders)
    )


def _a_verdict_function(body: str) -> str:
    """One numbered check in the shape five of the nine scripts write, around the body under test.

    The signature is `ops/m51_exit_criterion.py`'s verbatim; the M4.11-M4.14 scripts spell the
    same thing `-> tuple[bool, str, str]`, and one case below carries that spelling so the two
    are held together. [M5.1 cycle 4, M51-C4-REL-01]
    """
    return "async def check_one(ctx: Install) -> tuple[bool | None, str]:\n" + body


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        # The two shapes that shipped, verbatim.
        ("a live predicate widened by a literal", "check(placed == 0 or True, 'unplaced')", 1),
        ("a summary recorded as a pass", "check(True, 'the blocks carry counts')", 1),
        # The same defect a line-oriented regex would have walked past.
        ("split across lines", "check(\n    placed == 0 or True,\n    'unplaced',\n)", 1),
        ("an `and` pinned to false", "check(ok and False, 'never')", 1),
        # And the two this guard must NOT take: the loop's own deliberate failure, and the
        # repaired predicate.
        ("the loop's deliberate failure", "check(False, 'a count arrived as a bit')", 0),
        ("a predicate that can vary", "check(placed == 0, 'unplaced')", 0),
        # Review cycle 4, and every one of these passed while the rule read two AST shapes. The
        # first four are the same certificate written without a truthy `ast.Constant` at the
        # position the rule looked; the fifth is the call spelled with the parameter name the
        # scripts themselves declare. [M4.16 cycle 4, M416-C4-REL-05]
        ("a comparison that settles true", "check(1 == 1, 'x')", 1),
        ("the literal negated", "check(not False, 'x')", 1),
        ("two literals joined", "check(True and True, 'x')", 1),
        ("a truthy literal that is not a Constant", "check(placed == 0 or [1], 'x')", 1),
        ("the verdict passed by keyword", "check(ok=True, label='x')", 1),
        # And the other direction, which is what keeps the widening from becoming a rule about
        # taste: a comparison that settles FALSE is the deliberate report the loop writes, and a
        # comparison over anything the run decides is exactly what a check is.
        ("a comparison that settles false", "check(1 == 2, 'a count arrived as a bit')", 0),
        ("a comparison against a name", "check(placed >= floor, 'unplaced')", 0),
        ("a call the run answers", "check(bool(rows), 'rows')", 0),
        ("membership in a literal the run indexes", "check(state in ('ok', 'warm'), 'state')", 0),
        # M5.1 cycle 4. Five of the nine scripts publish their verdicts from a numbered check and
        # hand `check()` a local name inside one shared dispatcher, so a certificate written in
        # one of those twelve function bodies never passes the reader above at all -- which is
        # M4.5's "a summary recorded as a pass" one call frame out of reach.
        # [M5.1 cycle 4, M51-C4-REL-01]
        ("a verdict published as a literal",
         _a_verdict_function("    return True, 'the board carried counts'"), 1),
        ("a verdict assigned as a literal",
         _a_verdict_function("    ok = True\n    return ok, detail"), 1),
        ("a verdict assigned and then pinned",
         _a_verdict_function("    ok = rows == 0 or True\n    return ok, detail"), 1),
        ("the three-value verdict the M4 scripts return",
         "async def check_two(h: Household) -> tuple[bool, str, str]:\n    return True, 'x', ''", 1),
        # And the same four directions as above, from inside a check: a verdict the run decides,
        # the `None` arm that reports itself as not measured, the deliberate failure, and a name
        # bound in two branches, which is a value the run picks and not one a reader can settle.
        ("a verdict the run decides",
         _a_verdict_function("    ok = rows == 0\n    return ok, detail"), 0),
        ("the not-measured arm",
         _a_verdict_function("    return None, 'no container was inspected'"), 0),
        ("a deliberate failure published",
         _a_verdict_function("    return False, 'no placement row for the title'"), 0),
        ("a verdict bound in two branches",
         _a_verdict_function("    if rows:\n        ok = True\n    else:\n        ok = rows == 0\n"
                             "    return ok, detail"), 0),
        ("a helper that publishes no numbered verdict",
         "def tally(rows) -> tuple[bool, str]:\n    return True, 'rows'", 0),
    ],
)
def test_the_constant_predicate_guard_catches_a_real_violation(name, source, expected):
    assert len(_constant_check_predicates(source + "\n", "probe.py")) == expected, name


def test_the_constant_predicate_guard_reads_the_argument_the_scripts_declare():
    """The keyword arm's premise, asserted rather than assumed.

    `_check_verdict` falls back to the keyword named `ok`, and that name is not this file's
    choice: it is what every `check` these scripts declare calls its first parameter. A script
    that renamed it would leave the fallback reading a keyword nobody passes, which is a rule
    quietly holding nothing. [M4.16 cycle 4, M416-C4-REL-05]
    """
    declared = [
        (path.name, node)
        for path in EXIT_SCRIPTS
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "check"
    ]
    assert declared, "no exit script declares check() any more, so this rule reads nothing"
    wrong = [f"{name}:{node.lineno}: {node.args.args[0].arg}" for name, node in declared
             if not node.args.args or node.args.args[0].arg != "ok"]
    assert not wrong, (
        "an exit script's check() no longer takes its verdict as `ok`, so the keyword arm of "
        "_check_verdict reads a name nobody passes:\n  " + "\n  ".join(wrong)
    )


def test_the_constant_predicate_guard_reaches_every_verdict_the_scripts_publish():
    """The other arm's premise, and the measurement that says the rule is not reading past them.

    A guard that flags nothing is indistinguishable from a guard that reads nothing, and for five
    of the nine scripts this one WAS reading nothing: not one of the 54 numbered checks they
    declare contains a `check(` call, so the verdicts they publish reached the call-site reader
    only as the local name `verdict` inside a shared dispatcher. `ok = True` in a check body is
    M4.5's "a summary recorded as a pass" one call frame out of reach, and the script goes on
    printing `N/N checks passed`. So this asserts both halves the widened arm stands on -- that
    every `check_*` still carries the tuple annotation the gate keys on, and that every one of
    them publishes at least one verdict the reader can actually take hold of. A check whose
    verdict the binding reader cannot settle is legal (the closure is the point, as one level
    down), but a check with NO readable verdict at all is this rule blind again, silently.
    [decision 184; M5.1 cycle 4, M51-C4-REL-01]
    """
    functions = [
        (path.name, node)
        for path in EXIT_SCRIPTS
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("check_")
    ]
    assert functions, (
        "no exit script publishes its verdicts from a check_* function any more, so the return "
        "arm of _constant_check_predicates reads nothing. If the idiom has gone, it comes out "
        "together with this guard."
    )
    unread = [
        f"{name}:{node.lineno}: {node.name} -> "
        f"{ast.unparse(node.returns) if node.returns else '<unannotated>'}"
        for name, node in functions if not _is_a_numbered_check(node)
    ]
    assert not unread, (
        "an exit script's numbered check no longer declares the tuple return the verdict reader "
        "gates on, so its verdict is settled by nothing:\n  " + "\n  ".join(unread)
    )
    blind = [
        f"{name}:{node.lineno}: {node.name}"
        for name, node in functions if not _published_verdicts(node)
    ]
    assert not blind, (
        "a numbered check publishes no verdict this reader can follow, so a constant one in it "
        "would go unreported:\n  " + "\n  ".join(blind)
    )


def _main(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main":
            return node
    raise AssertionError("no main() in this script")


def _is_the_winner(node: ast.expr) -> bool:
    """The two spellings of the value: the local `winner`, and `result['winner']`."""
    if isinstance(node, ast.Name):
        return node.id == "winner"
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "result"
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "winner"
    )


def _winner_dereferences(fn: ast.AST) -> list[int]:
    """Linenos that index *into* the winner: `result['winner'][...]`, or `winner[...]`.

    Reading `result['winner']` is not a dereference and is not counted -- that is the test
    the verdict itself makes. What raises on an unresolved evening is the *second* index.
    """
    return sorted(
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Subscript) and _is_the_winner(node.value)
    )


def test_the_m4_script_computes_its_verdict_before_it_dereferences_the_winner():
    """The one failure that script exists to detect used to be a TypeError.

    §12's M4 row is "a real Friday night resolved by the app", so the outcome that matters is
    the evening that does *not* resolve. The report block printed `result['winner']['name']`
    while `ok` was computed twenty-eight lines below it, so an unresolved evening raised before
    "SOMETHING DID NOT RESOLVE" could be reached: the script could say yes, and could crash,
    and had no way at all to say no.
    """
    fn = _main(ast.parse((REPO / "ops" / "m4_exit_criterion.py").read_text(encoding="utf-8")))
    verdicts = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "ok" for t in node.targets)
    ]
    assert len(verdicts) == 1, f"main() assigns `ok` {len(verdicts)} times: {verdicts}"
    dereferences = _winner_dereferences(fn)
    # Not vacuous: the report still has to print the winner's name, or this guard would pass on
    # a script that had simply stopped reporting it.
    assert dereferences, "main() no longer reads the winner at all"
    assert verdicts[0] < dereferences[0], (
        f"the verdict is computed at line {verdicts[0]}, after the winner is dereferenced at "
        f"{dereferences}: an unresolved evening raises TypeError before it can be reported"
    )


def _constant_terminal_return(source: str, label: str) -> str | None:
    """Why `main()`'s last statement is not a verdict, or None if it is one."""
    fn = _main(ast.parse(source))
    last = fn.body[-1]
    if not isinstance(last, ast.Return) or last.value is None:
        return f"{label}: main() does not end in a return"
    if isinstance(last.value, ast.Constant):
        return f"{label}:{last.lineno}: main() ends in `return {ast.unparse(last.value)}`"
    assigned = {
        target.id
        for node in ast.walk(fn)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        for target in (getattr(node, "targets", None) or [node.target])
        if isinstance(target, ast.Name)
    }
    read = {n.id for n in ast.walk(last.value) if isinstance(n, ast.Name)}
    if not assigned & read:
        return (
            f"{label}:{last.lineno}: `return {ast.unparse(last.value)}` reads nothing the run "
            "computed"
        )
    return None


def test_the_m3_script_returns_a_verdict_rather_than_a_constant():
    """`return 0` was the last line of the M3 script, so every run of it succeeded.

    Including the runs where nobody was rated: `rate()` ignored every response status, so a
    stack refusing every verdict produced two empty Ledgers, two empty boards, four sections of
    numbers printed off empty dicts, and an exit code of 0. It now returns over the
    preconditions it can actually check -- two boards exist, they differ, a re-read moves
    nothing, and both people reached VERDICTS_EACH -- while "endorse", which no script can
    check, stays in the paragraph that says so. Its two siblings already ended in a computed
    verdict; they are held to the same rule here so that it stays true of all three.
    """
    assert EXIT_SCRIPTS, "no ops/m*_exit_criterion.py found, so every rule below reads nothing"
    offenders = [
        problem
        for path in EXIT_SCRIPTS
        if (problem := _constant_terminal_return(path.read_text(encoding="utf-8"), path.name))
    ]
    assert not offenders, (
        "a milestone exit script's exit code is settled before the run:\n  "
        + "\n  ".join(offenders)
    )


# A heading, not a detail line: `1. PERSONAL`, `3. BLIND`, `2. Import (§10...)`. The number is
# what makes it one -- a report whose numbering jumps from 1 to 3 is the shape being guarded
# against, and only a numbered line can produce it.
#
# `\S` and not `[A-Z]`, because the letter never was the discriminator: `\d+\.\s+` already
# excludes a decimal, which has no whitespace after its dot. What the letter did exclude was four
# of the nineteen numbered headings these three scripts print -- `4. §13's INSTRUMENT` (m3:249),
# `4. §13's FIGURE` (m4:268, :273) and `5. §14 RISK 6` (m4:276) -- and those four are the §13/§14
# instrument readings, the sections whose data is optional at runtime and therefore the ones most
# likely to acquire the `if` with no `else` this guard exists to report. Measured over the
# directory the guard reads: the widened class matches the same 19 lines and no others, so it
# reports nothing new today. [M4.8 review cycle 4: M48-C4-EXIT-01]
_SECTION_HEADING = re.compile(r"^\s*\d+\.\s+\S")


def _printed_text(node: ast.expr) -> str:
    """A print's first argument as text, f-string interpolations dropped."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return ""


def _prints(stmts: list[ast.stmt]) -> list[ast.Call]:
    return [
        call
        for stmt in stmts
        for call in ast.walk(stmt)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "print"
    ]


def _droppable_sections(source: str, label: str) -> list[str]:
    """Numbered sections a run can omit without printing a word about the omission."""
    problems = []
    for node in ast.walk(_main(ast.parse(source))):
        if not isinstance(node, ast.If):
            continue
        headings = [
            text.strip()
            for call in _prints(node.body)
            if call.args and _SECTION_HEADING.match(text := _printed_text(call.args[0]).lstrip("\n"))
        ]
        if headings and not _prints(node.orelse):
            problems.append(
                f"{label}:{node.lineno}: section {headings[0].split(':')[0]!r} is printed only "
                f"when `{ast.unparse(node.test)}` holds, and the branch that fails prints nothing"
            )
    return problems


def test_the_m3_script_names_the_section_it_could_not_measure():
    """A section that vanishes leaves a report that reads like a complete run.

    §12's M3 row rests on the personal one, and it was printed under `if len(shared) >= 2`
    with nothing on the other side: two people who happen to rate disjoint sets produced a
    report numbered 2, 3, 4, with no line saying that 1 had not been measured -- the same
    defect as a check whose predicate is a constant, one layer up, because what cannot be
    read off the output cannot be falsified by it. The rule is stated over all three scripts
    rather than over the branch that was wrong: the M4 script already answers an unresolved
    evening with a `1. UNRESOLVED` line, and that is the shape being held.
    [M4.8 review cycle 3: m48-c3-exit-03]
    """
    offenders = [
        problem
        for path in EXIT_SCRIPTS
        for problem in _droppable_sections(path.read_text(encoding="utf-8"), path.name)
    ]
    assert not offenders, "a milestone exit script can drop a section in silence:\n  " + "\n  ".join(
        offenders
    )

    # Shown failing, because a guard over a branch that is there is otherwise indistinguishable
    # from no guard at all: this is the m3 section with its `else` taken away.
    dropped = "\n".join(
        [
            "def main():",
            "    if len(shared) >= 2:",
            '        print(f"\\n  1. PERSONAL: rho = {rho}")',
            '    print("\\n  2. STABLE UNDER RE-READ: same request twice")',
            "    return ok",
        ]
    )
    reported = _droppable_sections(dropped, "dropped.py")
    assert reported and "1. PERSONAL" in reported[0], reported

    # And again in the spelling four of these scripts' own nineteen numbered headings take:
    # `4. §13's INSTRUMENT` at m3:249, `4. §13's FIGURE` twice at m4:268 and :273, `5. §14 RISK 6`
    # at m4:276. Those four are exactly the sections whose data is optional at runtime -- m3:252
    # reads a `model` that can be None and m4:275 subscripts `report['shortlist_agreement']` bare
    # -- so the obvious next repair to either is an `if` with no `else`, and the heading class
    # this guard shipped with could not see it. The section sign is written as itself here rather
    # than as an escape: escaped, this case would pass against the very regex that misses the
    # real print, which makes it a strawman instead of the shape the scripts take.
    sectioned = "\n".join(
        [
            "def main():",
            "    if model:",
            '        print("\\n  4. §13 INSTRUMENT: held-out pairs")',
            '    print("\\n  5. NEXT SECTION")',
            "    return ok",
        ]
    )
    reported = _droppable_sections(sectioned, "sectioned.py")
    assert reported and "§13 INSTRUMENT" in reported[0], reported


# --- the same scripts, given a runtime -------------------------------------------------
#
# Everything above reads these three as source, because their runtime is a live stack and a
# real bundle. Three of their defects were invisible to that reading and are answered here by
# importing the module and calling the helper: a cleanup that silently does not clean up, a
# `print` that raises on text it did not author, and a seeding loop that counted refusals.
# [M4.8 review cycle 1: findings m45-rmtree-is-a-no-op-on-windows,
# m45-prints-non-cp850-strings-it-did-not-author, m4-rate-still-ignores-every-response-status]


def _exit_script(name: str):
    """Import an exit script without running it, under a name of its own."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(f"exit_script_{name}", REPO / "ops" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution, the way `import` itself does it: `test_devstub_contract.py`
    # records why a module that resolves its own annotations needs to be findable in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- M4.9 review cycle 1: two claims about `title_company`, retired ------------------------

# Each phrase is a sentence this milestone wrote in several places and measured wrong afterwards.
# Kept as literals rather than described, because the point is that the exact wording does not
# come back: prose is how this codebase argues, and the two acts a reader takes from these two
# sentences are both expensive.
RETIRED_COMPANY_CLAIMS = {
    # Decision 194. `n_companies_log` is a column of no feature contract this app has loaded --
    # v20260828's `meta` block is 57 one-hots -- so the count was produced and discarded, and no
    # checkpoint was ever trained on it. A reader who believes otherwise either re-runs placement
    # over 19,071 titles that cannot move, or "repairs" the meta block by writing values into
    # columns §4.3 calls the exhaustive definition of the tower's input.
    "trained on the corpus's real counts": "decision 194",
    "trained on the real counts": "decision 194",
    # Decision 195. 8,594 is the number of duplicate GROUPS under (title_id, company, role);
    # the rows discarded are 11,654 and the rows involved in a collision 20,248. Neither reading
    # of "rows that collide" is 8,594, and the file that states it is about per-source row
    # multiplicity.
    "shipped rows collide": "decision 195",
}


def test_no_file_repeats_a_retired_claim_about_the_company_table():
    """The permanent record says what was measured. [M4.9 review cycle 1: M49-MIG-01, M49-MIG-05]

    `backend/migrations/0018_read_layer.sql` is the artefact that outlives the plan and is
    sha256-checksummed from its first apply, so a sentence that is wrong there is wrong for
    good; the same two sentences were copied into `load.py`, three test docstrings, the coverage
    row and decision 193. `docs/milestones/*.md` is not scanned: it is the plan, the workflow
    forbids editing it, and the corrections owed there go to the owner by hand.
    """
    offenders = []
    scanned = 0
    for path in sorted(
        [*(REPO / "backend" / "spielplan").rglob("*.py"),
         *(REPO / "backend" / "tests").rglob("*.py"),
         *(REPO / "backend" / "migrations").glob("*.sql"),
         *(REPO / "ops").glob("*.py"),
         REPO / "backend" / "tests" / "spec_coverage.toml",
         REPO / "docs" / "spec-v2.2-proposals.md"]
    ):
        if path == Path(__file__).resolve():
            continue        # this file states them in order to forbid them
        scanned += 1
        body = path.read_text(encoding="utf-8")
        for phrase, decision in RETIRED_COMPANY_CLAIMS.items():
            if phrase in body:
                offenders.append(f"{path.relative_to(REPO).as_posix()}: {phrase!r} ({decision})")
    assert scanned > 100, f"the sweep read {scanned} files and is not covering the tree"
    assert not offenders, (
        "§4.1's spine list is why `title_company` loads; the Cold Tower is not, and 8,594 is a "
        "group count. See decisions 194 and 195:\n  " + "\n  ".join(offenders)
    )


def test_no_m49_measure_heading_names_a_finding_it_does_not_measure():
    """An instrument that prints a heading for a measurement it did not take.

    Measure 2 shipped as "Key injectivity over the dumped payloads (finding 6)" over a body that
    builds `person_id + ':' + job` and reads nothing else -- which is finding 5's client half,
    the same finding measure 1 is labelled with. Finding 6 is the ordered
    `array_agg(character)`; §7's twelve measures assign it none, and none is invented here,
    because §6.0's card list does not name the field and no surface renders it. An operator
    reading the heading believed finding 6 had been measured on the corpus. It had not, and
    from this script it cannot be.

    The module docstring is dropped before the search rather than the file being grepped whole:
    the docstring explains all of this, and it must be able to say the words. [M49-CARD-4]
    """
    source = (REPO / "ops" / "m49_exit_criterion.py").read_text(encoding="utf-8")
    headings = re.findall(r'print\("\\n([^"]+)"', source)
    assert len(headings) == 10, f"the heading sweep found {len(headings)}: {headings}"

    module = ast.parse(source)
    code = "\n".join(ast.unparse(node) for node in module.body[1:])
    assert "character" not in code, (
        "this script now reads `character`, so it may be measuring finding 6 after all - "
        "rewrite this guard against whatever it measures rather than deleting it"
    )
    offenders = [h for h in headings if "finding 6" in h]
    assert not offenders, (
        "a numbered measure cites finding 6 (the ordered `array_agg(character)`) while nothing "
        f"in the script reads the field: {offenders}. Measures 1 and 2 are finding 5's SQL half "
        "and its client half; finding 6 is closed by "
        "`test_a_credit_is_one_row_per_person_and_job_across_department_spellings`"
    )


# --- M4.9 review cycle 1: measure 3, which measured its own population ---------------------
#
# `check(has_count and has_fold and with_credits == len(crossing), "the card names what it
# hides, on {with_credits}/{len(crossing)} payloads")`. Every member of `crossing` has at least
# one `credit` row by construction and `credit.person_id` is `NOT NULL REFERENCES person(id)`,
# so `credits_for`'s join cannot drop one: the printed `1,216/1,216` compared `crossing` with
# itself, and a bundle on which no title carries more than twelve credits -- the population the
# disclosure exists for -- still PASSED. The two source halves were whole-file `in` tests over
# a component this repository already shipped a guard against once, in the compose landmine
# that passed on a file of pure comments. [M49-CARD-1]

TITLE_CARD_RELATIVE = "frontend/src/lib/components/TitleDetail.svelte"


def _components_read_whole(source: str, label: str) -> list[str]:
    """Every `source("....svelte")` an exit script reads: a component read commentary and all.

    `ast`, not a regex, for the reason `_constant_check_predicates` is: the call wraps across
    lines in the file it was written for. `home.svelte.js` and `design.css` are deliberately not
    matched -- a module's constants and a stylesheet's custom properties are not markup, and
    this rule is about a control that a person can comment out and still satisfy.
    """
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "source" or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and str(arg.value).endswith(".svelte"):
            out.append(f"{label}:{node.lineno}: source({arg.value!r})")
    return out


def test_no_exit_measure_decides_on_a_component_it_read_with_the_comments_in():
    """A control commented out of the markup and left standing as prose passes a whole-file grep.

    This codebase argues its decisions in comments beside the code that carries them, so the
    file and the component are different documents; `ops/m49_exit_criterion.py:140`'s `markup()`
    exists for exactly that and measure 6 already reads through it. Measure 3 did not, so
    `'data-testid="credit-count"' in card_source` was satisfied by a card whose count line had
    been commented out -- the same shape as the compose guard that passed on a file of pure
    comments, which is why the rule is over the scripts rather than over the one measure.
    """
    assert EXIT_SCRIPTS, "no ops/m*_exit_criterion.py found, so every rule below reads nothing"
    offenders = [
        line
        for path in EXIT_SCRIPTS
        for line in _components_read_whole(path.read_text(encoding="utf-8"), path.name)
    ]
    assert not offenders, (
        "an exit measure reads a component's comments as if they were its markup; read it "
        "through markup():\n  " + "\n  ".join(offenders)
    )

    # And markup() is doing the work the rule assumes: it drops the commentary and keeps the
    # three controls measure 3 asks about. A stripper that stopped stripping would leave this
    # rule true and toothless.
    m49 = _exit_script("m49_exit_criterion")
    whole, rendered = m49.source(TITLE_CARD_RELATIVE), m49.markup(TITLE_CARD_RELATIVE)
    assert len(rendered) < len(whole), "markup() strips nothing, so reading through it asks nothing"
    for control in ('data-testid="credit-count"', "CREDIT_FOLD", 'data-testid="credits-disclosure"'):
        assert control in rendered, f"{control} is commentary, not markup, on the title card"


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        ("the read that shipped", 'card = source("frontend/src/lib/components/X.svelte")', 1),
        ("the same read, wrapped", 'card = source(\n    "a/X.svelte",\n)', 1),
        ("read through the stripper", 'card = markup("frontend/src/lib/components/X.svelte")', 0),
        # `home.svelte.js` ends in `.js` and is not a component; a Python module never is.
        ("a module beside a component", 'js = source("frontend/src/lib/home.svelte.js")', 0),
        ("a stylesheet", 'css = source("frontend/src/lib/design.css")', 0),
    ],
)
def test_the_component_read_guard_catches_a_real_violation(name, source, expected):
    assert len(_components_read_whole(source + "\n", "probe.py")) == expected, name


def test_the_count_line_measure_reports_the_population_the_disclosure_exists_for():
    """Measure 3's verdict has to turn on a number that can be zero on a real bundle.

    `with_credits == len(crossing)` is not one: `crossing` is built from `credit` rows, so every
    member has one, and the inner join to `person` is total under the FK. `folded_titles` is the
    honest subject -- the payloads that carry more than `CREDIT_FOLD` credits and therefore have
    something to hide -- and a bundle on which nothing folds leaves the disclosure unmeasured
    and must FAIL rather than print 1,216/1,216 and pass. [M49-CARD-1]
    """
    tree = ast.parse((REPO / "ops" / "m49_exit_criterion.py").read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "check" and len(node.args) > 1
    ]
    hides = [node for node in calls if "hides" in ast.unparse(node.args[1])]
    assert len(hides) == 1, f"measure 3's check is not identifiable by its label: {len(hides)}"
    predicate = ast.unparse(hides[0].args[0])
    assert "folded_titles" in predicate, (
        "measure 3 passes on a bundle whose disclosure it never exercised; the predicate is "
        f"{predicate}"
    )


# --- M4.9 review cycle 1: §6.6's sources-and-terms list, which nothing read ----------------

DATA_CARD = FRONTEND / "routes" / "admin" / "data" / "+page.svelte"


def _markup(source: str) -> str:
    """A Svelte source's template: script block, style block and HTML comments removed."""
    body = re.sub(r"<script\b.*?</script>", "", source, flags=re.S)
    body = re.sub(r"<style\b.*?</style>", "", body, flags=re.S)
    return re.sub(r"<!--.*?-->", "", body, flags=re.S)


def _rendered_markup(path: Path) -> str:
    """A Svelte file's template: script block, style block and HTML comments removed.

    Read this way because a sweep over the whole file passes on markup that has been commented
    out -- the failure a compose guard in this repository already shipped once, green against a
    file of nothing but comments (M4.7 ddocs-08). The claim below is that the card RENDERS these
    fields, so what is searched has to be what renders.
    """
    return _markup(path.read_text(encoding="utf-8"))


def test_the_data_card_renders_the_per_dataset_terms():
    """The surface half of `data-rules-rating-source-terms-survive-import`.

    The row promises the terms "are rendered on the §6.6 Data card as a sources-and-terms list"
    and nothing in the repository read that page: `routes/admin/data/` has no colocated vitest
    (jsdom arrived with finding 27, for one drawer; no route has been mounted under it), and the
    three e2e specs that visit `/admin/data` assert the tab row, the bundle heading and the
    rebuild set. So the fetch,
    the `{#if sources}` block or the four columns could each be deleted with ruff, pytest,
    vitest, `npm run build` and `M4.9 23/23 covered` all green, and the operator's question --
    which of the eleven sources bars redistribution of a movie-data archive -- would have no
    answer again. The route's own half is
    `test_import_integration.py::test_the_data_card_reads_the_terms_the_import_carried`.

    `version` is read with `||` rather than `??` because the corpus states "no version" with the
    empty string on four of the eleven frozen ids (v20260828: 1, 2, 3 and 4), which `??` lets
    through as a blank cell -- indistinguishable, to the operator reading the table, from a
    column the importer dropped. [M4.9 review cycle 1: M49-MIG-02, M49-MIG-04]
    """
    source = _src(DATA_CARD)
    assert "get('/admin/data/sources')" in source, (
        "the Data card renders a list it no longer fetches"
    )
    markup = _rendered_markup(DATA_CARD)
    assert "SOURCES AND TERMS" in markup and "{#each sources.sources as s" in markup, (
        "the sources-and-terms list is not in the rendered markup of "
        f"{DATA_CARD.relative_to(REPO).as_posix()}"
    )
    dropped = [field for field in ("s.name", "s.license", "s.version", "s.notes")
               if field not in markup]
    assert not dropped, (
        f"the terms list stopped printing {dropped}; §4.1 rule 4's eleven ids ship all four and "
        "the licence text is the one an operator has to read before sharing an archive"
    )
    assert "s.version ??" not in markup, (
        "`??` renders the empty string four of the eleven ids ship as a blank VERSION cell; "
        "`||` is what makes an unversioned dataset read as absent"
    )


# --- M4.9 review cycle 1: the title card's counts, and a note over a payload that moved ----

# A rendered credit count. §6.8's data voice separates one everywhere else the app prints one --
# `home.svelte.js:countLabel`, the catalogue's `Show more · N left` -- and the corpus's longest
# credit list is 1,535 rows against a median of 24, so the four-digit case is a page a person
# opens rather than a hypothetical.
_BARE_COUNT = re.compile(r"\.length(?!\s*\.toLocaleString\(\))")


def test_the_credit_disclosure_derives_its_labels_from_the_fold():
    """`CREDIT_FOLD = 12` and the button said `Show twelve`: one number, spelled twice.

    The file's own import comment fifteen lines above the constant argues that two spellings of
    one palette is how one of them rots, and English prose is the spelling no edit to the
    constant can reach: change the fold to twenty and the button keeps saying twelve while the
    count line says `20 of 47`. Nothing else can see it: the title card has no colocated vitest
    (finding 27 brought jsdom in for §6.7's drawer, and mounting this card is a bigger decision
    than one drawer -- §7's own measure 2 wants 1,216 payloads through it) and
    `e2e/specs/04-title-card.spec.js` restates `FOLD = 12` as its own literal.
    [M4.9 finding 7; review cycle 1: M49-CARD-5]
    """
    markup = _rendered_markup(TITLE_DETAIL)
    label = re.search(r'data-testid="credits-disclosure"(?P<label>.*?)</button>', markup, re.S)
    assert label, "the credit disclosure is not in the rendered markup of the title card"
    assert "CREDIT_FOLD" in label.group("label"), (
        "the disclosure states the fold in its own words; derive both labels from the constant. "
        f"It reads: {' '.join(label.group('label').split())}"
    )

    count = re.search(r'data-testid="credit-count"\s*>(?P<line>.*?)</span', markup, re.S)
    assert count, "the credit count line is not in the rendered markup of the title card"
    bare = [
        f"the {name} prints `{' '.join(text.split())}`"
        for name, text in (("count line", count.group("line")), ("disclosure", label.group("label")))
        if _BARE_COUNT.search(text)
    ]
    assert not bare, (
        "a credit count is rendered without its thousands separator, two screens from a "
        "catalogue that writes `1,535`:\n  " + "\n  ".join(bare)
    )


MODEL_NOTE = FRONTEND / "lib" / "components" / "ModelNote.svelte"
SHELVES = REPO / "backend" / "spielplan" / "home" / "shelves.py"
_MODEL_READ = re.compile(r"\bmodel\.(?P<field>[a-z_]+)")


def _shelf_card_model_keys() -> set[str]:
    """Every key a shelf card's `model` block can carry: the literal `_shelf_card` builds, plus
    the `extra=` dicts it merges in for §6.2's shared-sweet-spot numbers.

    `ast`, not a regex: the block is written one pair to a line and the merge happens at a call
    site three hundred lines away, so a text sweep would read one of the two.
    """
    keys: set[str] = set()
    for node in ast.walk(ast.parse(_src(SHELVES))):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "model"
                    and isinstance(value, ast.Dict)
                ):
                    keys |= {k.value for k in value.keys if isinstance(k, ast.Constant)}
        elif isinstance(node, ast.keyword) and node.arg == "extra" and isinstance(node.value, ast.Dict):
            keys |= {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    assert keys, "home/shelves.py no longer builds a card['model'] block"
    return keys


def test_every_field_the_model_note_reads_is_one_the_shelf_card_carries():
    """§6.7's annotation renders `_shelf_card`'s `model` block, and only that block.

    M4.9 lifted `item_n` and `e_source` out of it -- §8 stage 10's badge is decided on those two
    and decision 117 strips `model` wholesale, so a badge computed from inside it vanished for
    everyone with the toggle off -- and the two branches reading them here stayed. They cannot
    fire: the hover line stopped naming the crowd support behind the prior, and the compact line
    never showed those two (`all.slice(0, 2)`), which is why nothing went red. A branch over a
    key the payload cannot carry reads as a feature and is not one.
    [M4.9 finding 18; review cycle 1: M49-HOME-05]
    """
    callers = [
        path.relative_to(REPO).as_posix()
        for path in _frontend_sources()
        if path != MODEL_NOTE and "ModelNote" in _src(path)
    ]
    assert callers == ["frontend/src/lib/components/ShelfRow.svelte"], (
        "ModelNote is rendered somewhere else now, so the payload it prints is no longer the "
        f"shelf card's alone and this guard has the wrong subject: {callers}"
    )

    reads = set(_MODEL_READ.findall(_COMMENTARY.sub("", _src(MODEL_NOTE))))
    assert reads, "ModelNote no longer reads a field off `model`"
    carried = _shelf_card_model_keys()
    assert reads <= carried, (
        "ModelNote branches on a field the shelf card's model block does not carry: "
        f"{sorted(reads - carried)}. home/shelves.py builds {sorted(carried)}; either the "
        "server sends the field or the branch goes."
    )


def test_the_m45_script_removes_a_staging_tree_it_still_holds_open():
    """`shutil.rmtree(..., ignore_errors=True)` over an open file is a no-op that says nothing.

    The M4.5 script stages the bundle's artifacts into a tempdir and removes it in a `finally`
    -- but `placement/features.py:190` reads `review_text_emb.npz` through `ArtifactStore.npz`,
    which caches the NpzFile for the store's lifetime, and section 5 opened `content_X.npz`
    itself. On Windows the unlink of an open file raises PermissionError, `ignore_errors`
    swallows it, and 11.4 MB survived every run of the script forever with no message. The
    tree-removal half of this assertion is only sharp on Windows -- which is the platform that
    matters, because the only copy of the corpus is on a Windows workstation -- so the cache
    assertion carries it on POSIX.
    """
    import numpy as np

    from spielplan.models.artifacts import ArtifactStore

    m45 = _exit_script("m45_exit_criterion")
    root = Path(tempfile.mkdtemp(prefix="spielplan-m45-guard-"))
    store = None
    try:
        staged = root / "v1"
        staged.mkdir()
        np.savez(staged / "content_X.npz", data=np.arange(4.0), indices=np.arange(4))
        np.savez(staged / "review_text_emb.npz", ids=np.arange(2), vecs=np.zeros((2, 3)))
        store = ArtifactStore.open(staged, "v1")
        store.npz("review_text_emb.npz")

        m45.discard_staged_artifacts(root, store)
        assert not store._cache, "the store still holds a handle into a tree it just removed"
        assert not root.exists(), (
            "the staging tree survived its own cleanup: "
            f"{sorted(str(p.relative_to(root)) for p in root.rglob('*'))}"
        )
    finally:
        if store is not None:
            for handle in store._cache.values():
                closer = getattr(handle, "close", None)
                if closer is not None:
                    closer()
        shutil.rmtree(root, ignore_errors=True)


def test_the_m45_script_prints_a_finding_message_the_console_cannot_encode():
    """The guard above reads this file's own literals; `check()`'s detail is written elsewhere.

    Three sites -- `:156`, `:200` and `:319` -- pass the importer's own finding messages as
    `check()`'s `detail`, and 51 `report.fail`/`warn`/`note` literals in
    `backend/spielplan/importer/` carry an em dash -- most of them inside the 150-character
    slice. Under cp850 the print raised UnicodeEncodeError, so a bundle that fails validation
    printed "[FAIL] ... validates clean" and then a traceback where the reason belongs: the
    failure path of the one script whose job is diagnosis. It is `detail` and not those three
    linenos that this test stands over; the numbers move whenever the script is edited, and the
    pair the docstring shipped with (`:106` and `:138`) resolved to a docstring line and an early
    return in the tree it was written against. [M4.8 review cycle 3: m48-c3-exit-01]
    """
    import contextlib
    import io

    m45 = _exit_script("m45_exit_criterion")
    # backend/spielplan/importer/validate.py:120's own message, verbatim. The section sign
    # encodes in cp850 and the em dash does not, which is why the escape has to be selective.
    message = (
        "FAIL title-id: `title` has no `id` column — §4.1: the canonical key is title.id"
    )
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp850", newline="")
    with contextlib.redirect_stdout(console):
        m45.check(False, "the bundle the corpus built validates clean (2.1s)", message)
    console.flush()
    printed = console.buffer.getvalue().decode("cp850")

    assert "[FAIL] the bundle the corpus built validates clean" in printed, printed
    assert "title-id" in printed and "§4.1" in printed, printed
    assert "\\u2014" in printed, (
        "the em dash was dropped rather than named; backslashreplace keeps the codepoint "
        f"readable on the console that cannot render it: {printed!r}"
    )


def _unescaped_winner_prints(source: str) -> list[int]:
    """Linenos where a `print` interpolates the winner without passing it through `console()`.

    The literal guard above cannot reach this by construction: an f-string's interpolated value
    is runtime data rather than source, so `{winner['name']!r}` is invisible to a rule about
    literals -- and that name is the corpus's text, not the script's.
    """
    fn = _main(ast.parse(source))
    out: list[int] = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "print":
            continue
        for value in ast.walk(node):
            if not isinstance(value, ast.FormattedValue):
                continue
            if not any(
                isinstance(sub, ast.Subscript) and _is_the_winner(sub.value)
                for sub in ast.walk(value)
            ):
                continue
            if not any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "console"
                for call in ast.walk(value)
            ):
                out.append(value.lineno)
    return sorted(out)


def test_the_m4_script_escapes_the_winner_name_it_did_not_author():
    """The RESOLVED line prints the corpus's text, and the corpus is not ASCII.

    `api/tonight.py` selects `title.name` into the slate card and the result returns that card
    as `result['winner']`, so the name on §12's M4 line was written by the export, not by this
    project. `!r` does not save it -- Python 3's repr leaves a printable codepoint raw -- so
    under `PYTHONIOENCODING=cp850`, the code page CLAUDE.md's rule is about and the way this
    script's output is captured, an evening won by a title whose name leaves that code page
    raised UnicodeEncodeError from inside `print` on the *success* branch: a traceback where
    "1. RESOLVED" belongs, and sections 2 to 6 and the RESULT line never printed. 104 of
    v20260828's 19,071 names leave it; none is an owned movie today, which is a fact about this
    month's library rather than a guard.
    [M4.8 review cycle 2: m48-rev2-m4-prints-foreign-text-unescaped]
    """
    import contextlib
    import io

    path = REPO / "ops" / "m4_exit_criterion.py"
    unescaped = _unescaped_winner_prints(path.read_text(encoding="utf-8"))
    assert not unescaped, (
        f"{path.name} prints the winner's name straight at the console at line(s) {unescaped}: "
        "on a console that cannot carry it, the report raises from inside `print` on the one "
        "branch that says the evening resolved"
    )

    # Not vacuous, and the same measurement the M4.5 sibling above makes: the escape has to
    # survive its own subject on the console that cannot render it.
    m4 = _exit_script("m4_exit_criterion")
    # Written as an escape, not as the glyph: the household owns this title, and U+014D is one
    # of the codepoints cp1252 cannot carry either, so a failure report quoting this line would
    # crash the console it was being read on.
    name = "Sh\u014dgun"
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp850", newline="")
    with contextlib.redirect_stdout(console):
        print(f"  1. RESOLVED     room ABCD -> winner {m4.console(repr(name))}")
    console.flush()
    printed = console.buffer.getvalue().decode("cp850")
    assert "1. RESOLVED" in printed and "\\u014d" in printed, (
        "the winner's name was dropped rather than named; backslashreplace keeps the codepoint "
        f"readable on the console that cannot render it: {printed!r}"
    )


def test_the_m4_script_stops_seeding_when_a_write_is_refused():
    """`rate()` returned its target having written nothing, and the script printed that number.

    Against a stack refusing every verdict it counted 24 refusals as 24 verdicts, printed
    "seeded 24 and 24 verdicts", then spent three minutes in `wait_for_pool` and aborted
    naming the pool -- a true exit code with the wrong diagnosis, in the script whose output
    is the diagnosis. Its sibling `ops/m3_exit_criterion.py` took this repair in the same
    milestone; this is the same defect one file over.
    """
    import httpx

    m4 = _exit_script("m4_exit_criterion")
    refused: list[str] = []

    def stack(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/rate":
            return httpx.Response(200, json={"card": {"type": "sweep", "token": "card-token"}})
        if request.url.path == "/api/rate/verdict":
            refused.append(request.url.path)
            return httpx.Response(500, json={"detail": "the fold-in raised"})
        return httpx.Response(200, json={})

    client = httpx.Client(base_url="http://stack", transport=httpx.MockTransport(stack))
    with pytest.raises(httpx.HTTPStatusError):
        m4.rate(client, [2, 2, 1, 0])
    assert len(refused) == 1, (
        f"seeding continued past the first refused write: {len(refused)} verdicts posted"
    )


def test_the_m3_script_counts_a_refused_draw_as_well_as_a_refused_answer():
    """`sharpen()` read the answer's status and not the draw's, so half the loop was silent.

    Section 3 reads tier movement *per ten comparisons*, and the pair has to be drawn before it
    can be answered. `app.py`'s handlers answer a database fault with `{"detail": ...}`, so a
    refused `GET /api/rank/queue` parsed, carried no `pair`, and broke the loop on its first
    iteration -- byte-identical to an exhausted queue: "first 0 comparisons moved 0 titles",
    no refusal note, and a run that exits 0 having measured nothing. The draw fails
    independently of the answer, because it runs the candidate read, the draw and the seal that
    `GET /api/rank` never touches.
    [M4.8 review cycle 2: m48-rev2-sharpen-counts-only-half-its-refusals]
    """
    import httpx

    m3 = _exit_script("m3_exit_criterion")
    drawn: list[str] = []

    def stack(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/rank/queue":
            drawn.append(request.url.path)
            return httpx.Response(500, json={"detail": "the draw raised"})
        return httpx.Response(200, json={})

    client = httpx.Client(base_url="http://stack", transport=httpx.MockTransport(stack))
    assert m3.sharpen(client, 10) == (0, 1), (
        "a refused draw is reported as an exhausted queue: the run prints a board that moved "
        "nothing over comparisons it never made, with no refusal count"
    )
    # And it stops on the refusal rather than spending the loop's whole budget on a stack that
    # is already answering 500 to every draw.
    assert len(drawn) == 1, drawn


def _statements_between_create_and_drop(source: str) -> list[str]:
    """Statements running after the scratch database exists and before the block that drops it.

    A statement that can raise there leaves a database nothing will ever name again: the
    scratch name carries the run's pid (which is what stops two concurrent runs dropping each
    other's database), so the next run's `DROP DATABASE IF EXISTS` names a different one. The
    fixed name it replaced used to clean up its predecessor's orphan by accident.
    """
    fn = _main(ast.parse(source))
    creates = [i for i, stmt in enumerate(fn.body) if "CREATE DATABASE" in ast.unparse(stmt)]
    drops = [
        i
        for i, stmt in enumerate(fn.body)
        if isinstance(stmt, ast.Try)
        and "DROP DATABASE" in "".join(ast.unparse(node) for node in stmt.finalbody)
    ]
    assert len(creates) == 1 and len(drops) == 1, f"creates {creates}, drops {drops}"
    return [
        ast.unparse(stmt).splitlines()[0]
        for stmt in fn.body[creates[0] + 1:drops[0]]
        # A binding to a literal is the `x = None` the `finally` needs to guard itself with,
        # and it cannot raise. Anything else in this window can.
        if not (
            isinstance(stmt, (ast.Assign, ast.AnnAssign)) and isinstance(stmt.value, ast.Constant)
        )
    ]


def test_nothing_that_can_fail_runs_between_creating_the_scratch_database_and_dropping_it():
    """The block that creates the database has to be the block that drops it.

    Three statements sat outside it: the connect, the `pool._init_connection` whose two codec
    round trips this script has already been bitten by once, and the `tempfile.mkdtemp` M4.8
    itself moved there from inside the `try`.
    """
    stranded = _statements_between_create_and_drop(
        (REPO / "ops" / "m45_exit_criterion.py").read_text(encoding="utf-8")
    )
    assert not stranded, (
        "these run after CREATE DATABASE and outside the block whose finally drops it, so a "
        "failure in one orphans a database on the server:\n  " + "\n  ".join(stranded)
    )


def _catches_everything(handler: ast.ExceptHandler) -> bool:
    """A bare `except`, or one that names `Exception`/`BaseException` and nothing narrower.

    Which exception the handler catches is half the rule and was not read at all: any handler
    carrying a `check(False, ...)` satisfied the search, so narrowing `except Exception` to
    `except asyncpg.PostgresError` -- the narrowing a reviewer proposes for an async DB harness,
    or to `ValueError` for a parse -- left the guard green while the documented failure went back
    to a bare traceback. That failure is a TypeError (`None < APP_ID_FLOOR` once an import lands
    no rows), which none of those narrowings names. The sibling guard in this same row already
    reads `handler.type` (`_unguarded_seeding` below), so this is the one that omitted the check
    its neighbour makes. Compared on the last dotted segment rather than a suffix test, because
    `MyImportException` ends in the word and catches nothing broad.
    [M4.8 review cycle 3: m48-c3-exit-02]
    """
    if handler.type is None:
        return True
    return ast.unparse(handler.type).split(".")[-1] in {"Exception", "BaseException"}


def _propagating_measurement_block(source: str, label: str) -> str | None:
    """Why the block that measures raises instead of reporting, or None if it reports."""
    fn = _main(ast.parse(source))
    blocks = [
        stmt
        for stmt in fn.body
        if isinstance(stmt, ast.Try)
        and "DROP DATABASE" in "".join(ast.unparse(node) for node in stmt.finalbody)
    ]
    if len(blocks) != 1:
        return f"{label}: {len(blocks)} blocks whose finally drops the scratch database"
    reports = any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "check"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value is False
        for handler in blocks[0].handlers
        if _catches_everything(handler)
        for call in ast.walk(handler)
    )
    if not reports:
        return (
            f"{label}:{blocks[0].lineno}: no handler that catches everything turns an exception "
            "into a failed check, so a failure the handlers do not name ends the run in a "
            "traceback instead of a report"
        )
    return None


def test_the_m45_script_reports_a_failure_inside_its_measurement_block():
    """An import that lands no rows does not raise, and everything after section 2 needs rows.

    `importer/bundle.py` returns its report when `validate_for_install` fails and catches its
    own `_Rollback` when the load does, so `[FAIL] title loaded 0 rows` printed and the run
    walked on into section 3, where `max(id)` over an empty table is None and
    `None < APP_ID_FLOOR` raises TypeError -- and a guard on that one comparison only moves the
    traceback three sections down, to `ArtifactStore.open` over a tree the early-returning
    import never staged and `np.concatenate([])` over an empty sample. The operator got a
    traceback about a comparison where the sentence naming the cause belonged, and lost the
    remaining sections and the `passed/total` tally: the failure path of the one script whose
    job is diagnosis, which is where every other repair in this row was found.
    [M4.8 review cycle 2: m48-rev2-m45-raises-where-m4-was-taught-to-report]
    """
    problem = _propagating_measurement_block(
        (REPO / "ops" / "m45_exit_criterion.py").read_text(encoding="utf-8"),
        "m45_exit_criterion.py",
    )
    assert problem is None, problem
    # Not vacuous: the shape that shipped is the same block with no handler at all.
    assert _propagating_measurement_block(
        "async def main():\n"
        "    try:\n"
        "        check(max_title < FLOOR, 'the ids are ours')\n"
        "    finally:\n"
        "        await admin.execute('DROP DATABASE x')\n",
        "probe.py",
    )
    # And the handler kept but narrowed, which is what a review of an async DB harness proposes
    # and what a search over handler bodies alone could not tell from the one that shipped: the
    # documented failure here is a TypeError, so this run reverts to the bare traceback while the
    # `check(False, ...)` sits in a handler it never reaches.
    assert _propagating_measurement_block(
        "async def main():\n"
        "    try:\n"
        "        check(max_title < FLOOR, 'the ids are ours')\n"
        "    except asyncpg.PostgresError as exc:\n"
        "        check(False, 'the measurement block ran', str(exc))\n"
        "    finally:\n"
        "        await admin.execute('DROP DATABASE x')\n",
        "probe.py",
    )


def test_the_scratch_window_guard_sees_a_statement_left_outside_the_block():
    """The shape that shipped, reduced: a connect between the create and the try that drops."""
    stranded = _statements_between_create_and_drop(
        "async def main():\n"
        "    await admin.execute('CREATE DATABASE x')\n"
        "    conn = await asyncpg.connect(dsn)\n"
        "    root = None\n"
        "    try:\n"
        "        pass\n"
        "    finally:\n"
        "        await admin.execute('DROP DATABASE x')\n"
    )
    assert stranded == ["conn = await asyncpg.connect(dsn)"], stranded


def _unguarded_seeding(source: str, label: str) -> list[str]:
    """Calls to `rate()` in `main()` that are not inside a `try` handling `httpx.HTTPError`."""
    tree = ast.parse(source)
    if not any(
        isinstance(node, ast.FunctionDef) and node.name == "rate" for node in ast.walk(tree)
    ):
        return []
    fn = _main(tree)
    guarded: set[int] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        if not any(
            handler.type is not None and "HTTPError" in ast.unparse(handler.type)
            for handler in node.handlers
        ):
            continue
        guarded.update(
            call.lineno
            for stmt in node.body
            for call in ast.walk(stmt)
            if isinstance(call, ast.Call)
        )
    return [
        f"{label}:{node.lineno}"
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "rate"
        and node.lineno not in guarded
    ]


def test_the_seeding_scripts_name_the_precondition_a_refused_write_broke():
    """Raising is half the repair; the other half is a sentence instead of a traceback.

    `rate()` now raises on the first refused write in both scripts that have one. Letting that
    escape would exit non-zero too, but with a stack trace where the name of the failed
    precondition should be -- and the precondition is what the exit code is for.
    """
    assert EXIT_SCRIPTS, "no ops/m*_exit_criterion.py found, so every rule below reads nothing"
    offenders = [
        line
        for path in EXIT_SCRIPTS
        for line in _unguarded_seeding(path.read_text(encoding="utf-8"), path.name)
    ]
    assert not offenders, (
        "a refused write during seeding escapes as a traceback rather than a diagnosis:\n  "
        + "\n  ".join(offenders)
    )


def test_the_seeding_guard_sees_an_unguarded_call():
    """Both halves, on the two shapes that actually shipped one file apart."""
    unguarded = (
        "def rate(client, pattern):\n"
        "    return 0\n"
        "def main():\n"
        "    print(f'seeded {rate(a, [2])} and {rate(b, [0])} verdicts')\n"
    )
    assert len(_unguarded_seeding(unguarded, "probe.py")) == 2

    guarded = (
        "def rate(client, pattern):\n"
        "    return 0\n"
        "def main():\n"
        "    try:\n"
        "        seeded = rate(a, [2])\n"
        "    except httpx.HTTPError as exc:\n"
        "        print('PRECONDITION FAILED')\n"
        "        return 1\n"
    )
    assert _unguarded_seeding(guarded, "probe.py") == []
    # And a script with no seeding path is not asked for one: the M4.5 script writes through
    # the importer, not through §6.1's routes.
    assert _unguarded_seeding("def main():\n    return 0\n", "probe.py") == []


# --- M4.10 finding 3: the Rate journal's two statements are one unit ----------------------------

RATE_SESSION = REPO / "backend" / "spielplan" / "rate" / "session.py"


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    """Child -> parent for the whole tree, so a call can be read in its context."""
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _inside_a_transaction(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """Whether any ancestor of `node` is an `async with ....transaction()`.

    `.transaction(` is matched on the unparsed context expression so `conn.transaction()` and
    `other.transaction()` both count and `contextlib.AsyncExitStack()` does not -- the stack
    inside `_append` itself is the fallback, and a fallback is not what these rules are about.

    Shared by the two rules below because they ask one question in opposite directions: the
    journal append has to be inside a transaction and the Jellyfin socket has to be outside one.
    Two copies of this walk is how the two halves would come to disagree about what a
    transaction is. [M4.10 cycle 1, f11-01]
    """
    walker: ast.AST | None = parents.get(id(node))
    while walker is not None:
        if isinstance(walker, ast.AsyncWith) and any(
            ".transaction(" in ast.unparse(item.context_expr) for item in walker.items
        ):
            return True
        walker = parents.get(id(walker))
    return False


def _bare_append_calls(source: str, label: str) -> list[str]:
    """Every `_append(` call that no enclosing `conn.transaction()` covers.

    Read with `ast` and by walking *ancestors*, not with a regex over indentation: the call sites
    sit three and four levels in, behind `for` loops and `if`s, and the one that shipped bare sat
    at exactly the same indentation as one that did not.
    """
    tree = ast.parse(source)
    parents = _parents(tree)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "_append":
            continue
        if not _inside_a_transaction(node, parents):
            offenders.append(f"{label}:{node.lineno}: {ast.unparse(node).splitlines()[0][:72]}")
    return offenders


def test_every_rate_journal_append_is_inside_a_transaction():
    """Decision 35's journal: the INSERT at `seq + 1` and the UPDATE that moves the session to
    it are one unit, at every call site.

    For a year they were one unit at four of the five. `record_skip` called `_append` bare, and
    a process death, a dropped pool connection or a cancelled task between the two statements
    left the journal at N+1 with the session at N -- after which `rate_observation_seq` refused
    every later append in that session for ever. Reproduced from that state: verdict, skip and
    not-seen all 500, `GET /api/rate` went on serving a card that could not be answered, undo
    tombstoned the phantom row and the next verdict still 500'd, and only
    `DELETE /api/rate/session` recovered. Skip is the most frequent tap in a sweep.

    A static rule because the failure is a crash between two statements: the integration test
    beside it injects one at the cursor move, which proves the unit holds, while this proves
    there is no sixth call site that forgot. [M4.10 finding 3]
    """
    offenders = _bare_append_calls(RATE_SESSION.read_text(encoding="utf-8"), "rate/session.py")
    assert not offenders, (
        "a journal row is written outside any transaction, so a crash before the cursor moves "
        "wedges that session until it is deleted:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        # The shape that shipped, verbatim down to the two-line call.
        (
            "the bare call record_skip made",
            "async def record_skip(conn, s):\n"
            "    s = await _append(conn, s, kind_of='skip', card=card,\n"
            "                      title_ids=titles)\n",
            1,
        ),
        # And its four siblings' shape.
        (
            "wrapped like its siblings",
            "async def record_verdict(conn, s):\n"
            "    async with conn.transaction():\n"
            "        s = await _append(conn, s, kind_of='verdict')\n",
            0,
        ),
        # Depth must not fool it: the correction's call is four levels in.
        (
            "four levels inside the transaction",
            "async def f(conn, s):\n"
            "    async with conn.transaction():\n"
            "        for t in titles:\n"
            "            if t:\n"
            "                s = await _append(conn, s, kind_of='correction')\n",
            0,
        ),
        # A context manager that is not a transaction must not count as one -- including the
        # exit stack `_append` itself uses, which is how the fallback is built.
        (
            "an exit stack is not a transaction",
            "async def f(conn, s):\n"
            "    async with contextlib.AsyncExitStack() as stack:\n"
            "        s = await _append(conn, s, kind_of='skip')\n",
            1,
        ),
        (
            "a cursor is not a transaction",
            "async def f(conn, s):\n"
            "    async with conn.cursor('SELECT 1') as cur:\n"
            "        s = await _append(conn, s, kind_of='skip')\n",
            1,
        ),
        # The definition itself is not a call site, and neither is a mention of the name.
        (
            "the definition and a reference are not calls",
            "async def _append(conn, s, **kw):\n"
            "    return s\n"
            "handler = _append\n",
            0,
        ),
    ],
)
def test_the_append_transaction_guard_catches_a_real_violation(name, source, expected):
    """A guard that cannot see its own violation is the M4.7 lesson, so each shape is named."""
    assert len(_bare_append_calls(source, "probe.py")) == expected, name


# --- M4.10 cycle 1 (f11-01): the Jellyfin socket stays outside the write's transaction ----------

# The three spellings the §7.3 round trip reaches `rate/session.py` under. `_push_state` wraps
# `seen.push_owed` for the three forward taps, and `seen.retract` is Undo's compensation; all four
# call sites are an HTTP request against a foreign server on a 15 s budget, so the rule is the same
# for each. Written as unparsed names rather than as `ast.Name` ids because two of the three are
# attribute calls on the `seen` module.
PUSH_CALLS = frozenset({"_push_state", "seen.push_owed", "seen.retract"})


def _transacted_push_calls(source: str, label: str) -> list[str]:
    """Every §7.3 network call that an enclosing `conn.transaction()` *does* cover.

    The rule above with its predicate flipped, on the same ancestor walk: a journal append
    outside a transaction is the offence there, a socket inside one is the offence here.
    """
    tree = ast.parse(source)
    parents = _parents(tree)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if ast.unparse(node.func) not in PUSH_CALLS:
            continue
        if _inside_a_transaction(node, parents):
            offenders.append(f"{label}:{node.lineno}: {ast.unparse(node).splitlines()[0][:72]}")
    return offenders


def test_no_jellyfin_push_happens_inside_a_rate_transaction():
    """§3.3 and §7.3: the app-side write commits, and *then* the media server is told. At all
    four call sites, not only at the verdict's.

    Measured before the repair with a transport sleeping 1.5 s: the tap took 1.66 s, the verdict
    row was invisible to a second connection for 1.67 s of it, one backend sat
    `idle in transaction` for the whole wait against a pool of ten, and a second connection's
    UPDATE of the same row blocked 0.97 s behind it. Section 6's preamble gives a sweep card 2 s
    in total, and §3.3 says the app works when Jellyfin is down -- which a transaction awaiting
    its answer does not.

    A static rule because the integration test beside it drives `record_verdict` alone, and the
    verdict is the mildest of the four sites rather than the worst: a correction with
    `side='both'` awaits two 15 s-budget sockets in series, so a Jellyfin answering in 1.5 s
    would hold two `user_title` rows, the `rate_session` row `_claim_card` has locked and the
    journal row for 3 s. Returning `record_not_seen` and `record_correction` to the pre-M4.10
    shape left every test this row names green, which is the gap this closes.
    [M4.10 finding 11; cycle 1, f11-01]
    """
    source = RATE_SESSION.read_text(encoding="utf-8")
    # A rule that matches on a name has to say the name is still there, or a rename turns it into
    # a rule about nothing -- which is the M4.8 lesson about a widened instrument that narrowed.
    for spelling in sorted(PUSH_CALLS):
        assert f"{spelling}(" in source, (
            f"{spelling} is not called in rate/session.py any more, so this rule no longer "
            f"covers the call site it was written for -- rename it here too"
        )
    offenders = _transacted_push_calls(source, "rate/session.py")
    assert not offenders, (
        "a Jellyfin round trip is awaited inside a database transaction, so a foreign server's "
        "latency holds row locks against a pool of ten and widens the double-tap window from "
        "milliseconds to seconds:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        # The shape that shipped, verbatim: the push between the observation and the journal row.
        (
            "the push inside the verdict transaction",
            "async def record_verdict(conn, s, jf):\n"
            "    async with conn.transaction():\n"
            "        write = await observations.record_verdict(conn)\n"
            "        pushed, reason = await _push_state(conn, jf, user_id=1, title_id=2)\n",
            1,
        ),
        # The correction's worst case: the loop is three levels in and awaits one socket per leg.
        (
            "the correction's two pushes, three levels in",
            "async def record_correction(conn, s, jf):\n"
            "    async with conn.transaction():\n"
            "        for title_id in corrected:\n"
            "            pushed, reason = await _push_state(conn, jf, title_id=title_id)\n",
            1,
        ),
        # Undo's compensation is the fourth site and reaches `sync/seen.py` directly.
        (
            "undo's retraction inside its own transaction",
            "async def undo(conn, s, jf):\n"
            "    async with conn.transaction():\n"
            "        if pushed:\n"
            "            await seen.retract(conn, jf.client, jf.cfg, user_id=1, title_id=2)\n",
            1,
        ),
        # And the shape that is correct: the block closes, then the socket.
        (
            "after the transaction closes",
            "async def record_not_seen(conn, s, jf):\n"
            "    async with conn.transaction():\n"
            "        s = await _append(conn, s, kind_of='not_seen')\n"
            "    pushed, reason = await _push_state(conn, jf, user_id=1, title_id=2)\n",
            0,
        ),
        # An exit stack holds no locks, so a push inside one is not this rule's offence -- the
        # inverse of what the `_append` rule says about the same context manager.
        (
            "an exit stack is not a transaction",
            "async def f(conn, jf):\n"
            "    async with contextlib.AsyncExitStack() as stack:\n"
            "        await _push_state(conn, jf, user_id=1, title_id=2)\n",
            0,
        ),
        # `_push_state`'s own body calls `seen.push_owed`, and that is a definition, not a site
        # that a later edit could move into a transaction.
        (
            "the wrapper's own call and a bare reference are not offences",
            "async def _push_state(conn, jf, **kw):\n"
            "    return await seen.push_owed(conn, jf.client, jf.cfg, **kw)\n"
            "handler = _push_state\n",
            0,
        ),
    ],
)
def test_the_push_transaction_guard_catches_a_real_violation(name, source, expected):
    """A guard that cannot see its own violation is the M4.7 lesson, so each shape is named."""
    assert len(_transacted_push_calls(source, "probe.py")) == expected, name


# --- M4.10 cycle 1 (m410-rev-01): every Rate write claims its card first ------------------------

# The five §6.1 taps that write an observation. `record_correction` is in the list because it
# supersedes two labels and redraws the pair, which is as much a write as a verdict is; the two
# card-stash helpers are not, because their guard is the `AND card_token IS NULL` predicate in the
# UPDATE itself rather than a row lock.
CLAIMED_WRITES = (
    "record_verdict",
    "record_not_seen",
    "record_skip",
    "record_duel",
    "record_correction",
)


def _unclaimed_write_transactions(source: str, label: str, names=CLAIMED_WRITES) -> list[str]:
    """Every named write whose transaction does not *open* with `await _claim_card(...)`.

    "Opens with" and not "contains": the lock has to be the first statement inside the
    transaction or the loser has already done work under it -- and it has to be inside, because
    a `FOR UPDATE` taken on an autocommit connection is released before the write it guards.
    The function's first `.transaction(` block in source order is the one checked, so a helper
    transaction later in the body cannot stand in for the one the observation is written under.

    A missing name is an offence of its own: a rule that silently checks nothing is what M4.8
    was about, and renaming one of the five taps is an ordinary thing for a later milestone to do.
    """
    tree = ast.parse(source)
    offenders: list[str] = []
    seen_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if node.name not in names:
            continue
        seen_names.add(node.name)
        opened: ast.AsyncWith | None = None
        for inner in ast.walk(node):
            if not isinstance(inner, ast.AsyncWith):
                continue
            if not any(
                ".transaction(" in ast.unparse(item.context_expr) for item in inner.items
            ):
                continue
            if opened is None or inner.lineno < opened.lineno:
                opened = inner
        if opened is None:
            offenders.append(f"{label}:{node.lineno}: {node.name} writes outside any transaction")
            continue
        first = ast.unparse(opened.body[0]).splitlines()[0]
        if not first.startswith("await _claim_card("):
            offenders.append(f"{label}:{opened.body[0].lineno}: {node.name} opens with {first[:48]}")
    for missing in sorted(set(names) - seen_names):
        offenders.append(f"{label}: {missing} is not defined here any more")
    return offenders


def test_every_rate_write_claims_its_card_before_it_writes():
    """§6.1 makes the card the server's, and `_claim_card` is what makes that true when two
    answers arrive together.

    Nothing behavioural distinguishes the lock from its backstop at the surface: a loser stopped
    by `_claim_card`'s `FOR UPDATE` and a loser stopped by `rate_observation_seq` both leave as
    `StaleCard("stale_card")`, so the route answers the same 409 either way. What differs is how
    far the loser got first -- with the lock it stops before `observations.record_verdict`, and
    without it it writes the ledger row, the `user_title` state and, before finding 11 moved the
    socket out, the household's Played flag, and is then rolled back by an index. The race test
    beside this one counts that call; this one says there is no sixth tap that forgot, and that
    the lock is the transaction's *first* statement rather than merely somewhere inside it.

    The backstop is also not load-bearing for ever: `_append`'s own docstring records that
    `rate_observation_seq` is not partial on `undone_at`, which is exactly the shape a later undo
    milestone might change -- after which two gathered taps would both append and the 409 would
    be gone with nothing red. [M4.10 finding 2; cycle 1, m410-rev-01]
    """
    offenders = _unclaimed_write_transactions(
        RATE_SESSION.read_text(encoding="utf-8"), "rate/session.py"
    )
    assert not offenders, (
        "a Rate write does not take §6.1's card under a row lock as the first statement of its "
        "transaction, so two taps on one token both reach the observation:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        (
            "the shape that ships",
            "async def record_verdict(conn, s, card_token):\n"
            "    async with conn.transaction():\n"
            "        await _claim_card(conn, s, card_token)\n"
            "        write = await observations.record_verdict(conn)\n",
            0,
        ),
        # The mutation that matters most: the lock is there, one statement too late, so the loser
        # has already written the ledger row it will be rolled back out of.
        (
            "claimed after the observation",
            "async def record_verdict(conn, s, card_token):\n"
            "    async with conn.transaction():\n"
            "        write = await observations.record_verdict(conn)\n"
            "        await _claim_card(conn, s, card_token)\n",
            1,
        ),
        (
            "not claimed at all",
            "async def record_verdict(conn, s, card_token):\n"
            "    async with conn.transaction():\n"
            "        write = await observations.record_verdict(conn)\n",
            1,
        ),
        # Outside the transaction the `FOR UPDATE` is released the moment its own implicit
        # transaction commits, which is before the write it was taken for.
        (
            "claimed before the transaction opens",
            "async def record_verdict(conn, s, card_token):\n"
            "    await _claim_card(conn, s, card_token)\n"
            "    async with conn.transaction():\n"
            "        write = await observations.record_verdict(conn)\n",
            1,
        ),
        (
            "no transaction at all",
            "async def record_verdict(conn, s, card_token):\n"
            "    await _claim_card(conn, s, card_token)\n"
            "    write = await observations.record_verdict(conn)\n",
            1,
        ),
        # A later transaction in the same function cannot stand in for the observation's.
        (
            "a second transaction carries the claim",
            "async def record_verdict(conn, s, card_token):\n"
            "    async with conn.transaction():\n"
            "        write = await observations.record_verdict(conn)\n"
            "    async with conn.transaction():\n"
            "        await _claim_card(conn, s, card_token)\n",
            1,
        ),
        (
            "a tap that no longer exists under that name",
            "async def record_something_else(conn, s, card_token):\n"
            "    async with conn.transaction():\n"
            "        await _claim_card(conn, s, card_token)\n",
            1,
        ),
    ],
)
def test_the_card_claim_guard_catches_a_real_violation(name, source, expected):
    """Each shape named, including the rename that would leave the rule measuring nothing."""
    found = _unclaimed_write_transactions(source, "probe.py", ("record_verdict",))
    assert len(found) == expected, name


# --- M4.10 review cycle 1: two decisions that named code they do not describe ----------------
#
# The register is cited by number from commit bodies, from comments and from coverage rows, which
# makes it the text a later milestone reads BEFORE the code it is about. Two of M4.10's own
# decisions named a counter and a reader the shipped code rejects, so each guard below ties one
# decision's sentence to the symbol it rules on: if the code ever moves, the guard fails and the
# record gets rewritten with it, which is the only order that keeps the two honest.
# `docs/milestones/*.md` is not read here for the reason the company-claim sweep above gives --
# the plan is the plan, the workflow forbids editing it, and a correction owed there goes to the
# owner by hand.

REGISTER = REPO / "docs" / "spec-v2.2-proposals.md"
COVERAGE = REPO / "backend" / "tests" / "spec_coverage.toml"


def _decision(number: int) -> str:
    """One decision's own text: its row in the summary table plus the argued section under it.

    Both halves, because the misnaming this guards was in both and a reader cites whichever they
    opened: the row is one cell in a table of ten, the section is six paragraphs below it.
    """
    body = REGISTER.read_text(encoding="utf-8")
    rows = [ln for ln in body.splitlines() if ln.startswith(f"| {number} |")]
    assert len(rows) == 1, f"decision {number} has {len(rows)} summary rows in the register"
    heads = list(re.finditer(r"^### (\d+)\. ", body, re.M))
    start = next(m for m in heads if m.group(1) == str(number))
    after = [m.start() for m in heads if m.start() > start.start()]
    return rows[0] + "\n" + body[start.start(): after[0] if after else len(body)]


def _decision_section(number: int) -> str:
    """One decision's argued section, for a decision the summary tables do not carry a row for.

    `_decision` above wants both halves and asserts exactly one `| N |` row; the M4.14 block is
    written as sections without a summary table, so this reads the section alone rather than
    making the guard below depend on a table that does not exist.
    """
    body = REGISTER.read_text(encoding="utf-8")
    heads = list(re.finditer(r"^### (\d+)\. ", body, re.M))
    start = next(m for m in heads if m.group(1) == str(number))
    after = [m.start() for m in heads if m.start() > start.start()]
    return body[start.start(): after[0] if after else len(body)]


def test_decision_252_does_not_assert_a_subprocess_this_tree_does_not_have():
    """A decision is normative from the day it is taken (decision 177), so it may not describe a
    construction that is not there.

    252's ruling is `ProcessPoolExecutor(max_workers=1)` with two intra-op threads in the child,
    awaited from the validate route through `loop.run_in_executor`, and its Cost paragraph
    describes that child in the present tense. No executor of any kind exists in
    `backend/spielplan`: `validate._validate_model_artifacts` imports and calls `load_tower`
    inline, on the API process's event-loop thread, reached from an `async def` route. The gap is
    stated in `docs/TESTING.md`, in `ops/m414_exit_criterion.py`'s check 9 and in
    `api/artifacts.py`'s docstring - and it was stated in every place except the one a reader of
    the decision meets, which is how the owner's own review brief came to ask what happens when
    that child process dies.

    So the two are tied together here: while the construction is absent the section says so, and
    the day someone builds it this guard is what makes them delete the sentence rather than leave
    a record that has gone stale in the other direction. Named symbols rather than prose on the
    code side, because the ruling names them. [M4.14 cycle 1, M414-C1-REC-01]
    """
    built = [
        path.relative_to(REPO).as_posix()
        for path in (REPO / "backend" / "spielplan").rglob("*.py")
        if "ProcessPoolExecutor" in path.read_text(encoding="utf-8")
    ]
    section = _decision_section(252)
    disclosed = "NOT BUILT IN M4.14" in section

    if built:
        assert not disclosed, (
            f"decision 252's subprocess is built ({', '.join(built)}) and its section still says "
            "NOT BUILT IN M4.14 - delete that paragraph"
        )
    else:
        assert disclosed, (
            "no ProcessPoolExecutor exists under backend/spielplan, so decision 252 rules on a "
            "construction this tree does not have and its section has to say so"
        )


def _derives_the_card_type_from_a_counter_called_seq(text: str) -> list[str]:
    """Every clause in `text` that derives the card type from a counter named `seq`.

    Clause-scoped rather than file-scoped: `rate_session.seq` is a real column and the argument
    for NOT using it has to be allowed to name it. The stop class is `.` and `|` -- the sentence
    and the table cell -- so "derived from X. `seq` is the journal's row count" does not match and
    "derived from the session's monotone `seq`" does.
    """
    flat = " ".join(text.split())
    return [
        m.group(0).strip()
        for m in re.finditer(r"[^.|]*\bderiv\w*[^.|]{0,90}?\bseq\b[^.|]*", flat, re.I)
    ]


def test_decision_200_names_the_counter_the_card_type_is_derived_from():
    """The register said `seq`; the code says that column is specifically the wrong number.

    `observation_index`'s docstring gives the argument in full and the register recorded its
    opposite -- in the summary row, in the heading, in the decision paragraph and in
    `library-rate-mix-alternates-blocks`'s comment. The two counters are not one number twice:
    `_append` writes `seq = s.seq + 1` for every kind while its `advances` flag leaves the block
    and the slot alone on a correction, and `undo`'s restoring UPDATE rewinds block, slot, card
    and token and never `seq`. A later milestone "restoring" the decision's literal wording would
    flip the card type on the first correction of a sitting and serve a card of the other type
    than the one Undo had just put back -- re-creating finding 24's double sweep one undo later.
    [M4.10 review cycle 1: M410-D8-04, M410-REV1-D200-02; decision 200]
    """
    offenders = [
        f"{name}: {clause}"
        for name, text in (
            ("decision 200", _decision(200)),
            ("spec_coverage.toml", COVERAGE.read_text(encoding="utf-8")),
        )
        for clause in _derives_the_card_type_from_a_counter_called_seq(text)
    ]
    assert not offenders, (
        "decision 200's counter is `observation_index(block_index, slot)`, not the journal's row "
        "count -- `seq` moves on a correction and Undo does not rewind it:\n  "
        + "\n  ".join(offenders)
    )
    assert "observation_index" in _decision(200), (
        "decision 200 has to name the counter it took, or the next reader guesses again"
    )

    # The other direction: the record is only right while the code still reads this way.
    flat = " ".join(RATE_SESSION.read_text(encoding="utf-8").split())
    assert flat.count("card_type_for(s.mode, observation_index(s.block_index, s.slot))") == 2, (
        "the two call sites `ensure_card` and `payload` are what decision 200 describes; if the "
        "counter really moved, amend the decision and this guard together"
    )
    assert "card_type_for(s.mode, s.seq" not in flat, (
        "the card type is being read off the journal's row count, which a correction advances and "
        "Undo does not rewind -- see `observation_index`'s docstring and decision 200"
    )


def test_decision_207_names_the_one_file_that_reads_the_journals_pushed_flag():
    """The register sent an auditor of `prior_state.pushed` to the file that cannot read it.

    Decision 207's Cost paragraph said "`sync/seen.py`'s retract path keeps reading
    `prior_state.pushed`, and the integration test asserts the field for all three outcomes
    (success, refusal, no connector)". Neither half held: the `prior_state` `sync/seen.py` takes
    is a plain state string -- deliberately a string so `sync` does not import from `ledger` --
    and the sole reader of the journal's flag is `undo`, which gates the compensating Played write
    on it. The test asserts two outcomes; a refusal and a household with no connector are asserted
    nowhere, so a regression on either was believed to be caught.
    [M4.10 review cycle 1: M410-REV1-D207-03; decision 207]
    """
    readers = sorted(
        path.relative_to(REPO).as_posix()
        for path in (REPO / "backend" / "spielplan").rglob("*.py")
        if any(
            "prior_state" in line and '"pushed"' in line
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    )
    assert readers == ["backend/spielplan/rate/session.py"], (
        f"the journal's `pushed` flag is read by {readers}; decision 207 names its reader, so the "
        "decision has to be amended in the same commit that moves it"
    )

    text = _decision(207)
    assert "`rate/session.py`" in text, "the decision must name the file that reads the field"
    retired = {
        "retract path keeps reading": "`sync/seen.py` reads nothing from the journal",
        "all three outcomes": "two outcomes are asserted: a push that succeeded, and an owed one",
    }
    offenders = [f"{phrase!r} -- {why}" for phrase, why in retired.items() if phrase in text]
    assert not offenders, "decision 207 states what was measured:\n  " + "\n  ".join(offenders)


@pytest.mark.parametrize(
    "name,text,expected",
    [
        # The four places the register and the coverage row said it, in the four shapes they
        # said it in: a table cell, a heading, a sentence, and a comment wrapped over two lines.
        ("the summary row", "| 200 | `card_type_for` returns sweep on odd slots. Derive the card "
                            "type from the session's monotone `seq`, or amend 6.1 to "
                            '"alternates within a block"? | **Derive it from `seq`** (option a). '
                            "Slot 1 of the *first* block stays a sweep |", 2),
        ("the heading", "### 200. Mix alternation is derived from the session's monotone seq; "
                        "6.1 stands", 1),
        ("the decision paragraph", "**The decision.** Option (a). The card type is derived from "
                                   "the session's monotone `seq` rather than from `slot`, so "
                                   "alternation survives the roll.", 1),
        ("the coverage comment", "# the arm 5.2 credits with within-liked resolution. Decision "
                                 "200 derives the type from the\n# session's monotone `seq` "
                                 "rather than amending 6.1, keeping slot 1 a sweep.", 1),
        # The argument FOR the counter taken has to be sayable, or the record cannot explain
        # itself and the guard is a ban on a word rather than on a claim.
        ("the correction", "The card type is derived from `observation_index(block_index, slot)`. "
                           "`rate_session.seq` is the journal's row count and is deliberately not "
                           "that counter: a correction advances it without moving the slot.", 0),
    ],
)
def test_the_decision_200_counter_guard_catches_each_way_it_was_said(name, text, expected):
    """docs/TESTING.md: "a guard that cannot fail reads as coverage while providing none."

    The five cases are the four wordings that shipped plus the one that must pass: a register
    forbidden from naming the column it rejected cannot argue the decision, so the detector is
    scoped to the clause that DERIVES the card type rather than to the word.
    """
    assert len(_derives_the_card_type_from_a_counter_called_seq(text)) == expected, name


# The files that state where a block of 15 commits. Not every file that mentions Undo: the rule
# has one statement in the domain (`advance`'s docstring and `_undo_reaches`), one in the register,
# one in the coverage row's `what` and one in the browser spec's title, and those four are what a
# reader compares. `rate.svelte.js` renders whatever `undo_availability` answers -- decision 199
# says so in as many words -- so it states nothing and scanning it would ban a word rather than a
# claim.
COMMIT_POINT_FILES = (
    RATE_SESSION,
    REPO / "backend" / "tests" / "test_rate_session.py",
    COVERAGE,
    REPO / "e2e" / "specs" / "11-rate.spec.js",
)


def _commits_the_block_when_the_counter_rolls(text: str) -> list[str]:
    """Every clause in `text` that puts the commit on the roll instead of one observation later.

    Clause-scoped for the reason the decision-200 detector above is clause-scoped: decision 199's
    argument has to be able to describe the reading it replaced, and decision 35's own words --
    "starting a new block commits the previous one" -- are the NEW rule stated in the old
    vocabulary, so a ban on "fifteenth" near "commit" would forbid the register from explaining
    itself. A clause offends only when it puts the two together AND names none of the four ways
    the moved point is written. Stop class `.` and `|`: the sentence and the table cell.
    """
    flat = " ".join(text.split())
    rolls = r"(?:the roll\b|rolls into|fifteenth|fifteen taps|slot 15|card 15)"
    commits = r"(?:commit\w*|stops? being undoable|no longer undoable)"
    moved = re.compile(
        r"sixteenth|next block|new block's first|one tap later|decision 199"
        r"|starting a new block commits",
        re.I,
    )
    return [
        m.group(0).strip()
        for m in re.finditer(rf"[^.|]*\b{rolls}\b[^.|]*?\b{commits}\b[^.|]*", flat, re.I)
        if not moved.search(m.group(0))
    ]


def test_nothing_still_commits_the_block_of_fifteen_on_the_roll():
    """Decision 199 moved the commit point, and one of the four statements of it stayed behind.

    `test_the_counter_runs_to_fifteen_and_rolls_into_a_new_block`'s docstring went on reading "the
    roll is also decision 35's commit: everything in the old block stops being undoable at that
    instant" for the whole of the milestone that made it false -- 870 lines above
    `test_the_fifteenth_tap_stays_undoable_until_the_sixteenth_lands`, in the same file, so the
    next reader of the block machine found two in-repo answers to one question and no way to tell
    which was current. The register, `advance`'s docstring and the coverage row were all amended;
    a sentence is not amended by the diff that contradicts it, which is why this is a rule and not
    a review note. [M4.10 cycle 2, M410-C2-D19-04; decisions 35, 174, 199]
    """
    offenders = [
        f"{path.relative_to(REPO).as_posix()}: {clause}"
        for path in (*COMMIT_POINT_FILES, REGISTER)
        for clause in _commits_the_block_when_the_counter_rolls(
            _decision(199) if path is REGISTER else path.read_text(encoding="utf-8")
        )
    ]
    assert not offenders, (
        "a block of 15 is committed when the FIRST observation of the next block lands, not when "
        "the fifteenth of this one does (decision 199):\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "name,text,expected",
    [
        # The docstring that shipped, whole, because the §6.1 quote in front of it is the half
        # that must survive: a detector that fired on the quote would take the counter claim with
        # the Undo claim.
        ("the docstring that stayed behind",
         '"""§6.1: "the counter runs 1..15 and rolls into a new block." The roll is also decision '
         "35's commit: everything in the old block stops being undoable at that instant.\"\"\"", 1),
        ("a table cell", "| 199 | The fifteenth tap is the commit | **Change it** |", 1),
        ("the shape `advance` used to have",
         "The fifteenth observation rolls the counter and commits the block it ended.", 1),
        # The four that must pass, one per place the rule is stated now.
        ("advance's docstring", "A block is committed when the FIRST observation of the NEXT "
                                "block lands, not when the fifteenth of this one does.", 0),
        ("the coverage row", "the previous block stays reachable until the first observation of "
                             "the new block lands - so the fifteenth tap is undoable and the "
                             "sixteenth commits the block it ended", 0),
        ("decision 35's own words", "Decision 35: the depth matches the counter the person is "
                                    "reading, and starting a new block commits the previous one.",
         0),
        ("the counter claim alone", '§6.1: "the counter runs 1..15 and rolls into a new block."',
         0),
    ],
)
def test_the_commit_point_guard_catches_each_way_the_old_reading_was_said(name, text, expected):
    """docs/TESTING.md: "a guard that cannot fail reads as coverage while providing none."

    Three offending shapes and four true ones, because this detector's whole difficulty is that
    the true sentences and the false ones are built from the same words -- the rule moved by one
    observation, not by a vocabulary.
    """
    assert len(_commits_the_block_when_the_counter_rolls(text)) == expected, name


# --- M4.11 finding 20 (sec-15): a credential a dataclass prints ---------------------------------

# The packages a §7.3 or §14.3 credential is carried through: the connector that holds the admin
# key and the token map, the sweep that carries one member's decrypted token across their whole
# reconciliation, and the sender that holds the VAPID signing key. Scoped to `connectors/` at
# first, which made the rule about a DIRECTORY rather than about the credential -- and the one
# field in the whole codebase actually named `token`, `sync/seen.py`'s `LinkedUser.token`, sat
# outside it at default repr while the guard read green. [review cycle 1: m411-rev-jf-02]
CREDENTIAL_PACKAGES = tuple(
    REPO / "backend" / "spielplan" / part for part in ("connectors", "sync", "push")
)

# §14.3: Jellyfin "API keys are unscoped and admin-equivalent (no read-only variant exists), so the
# stored connector secret can administer the whole media server"; §7.3's per-user access tokens are
# one named person's credentials. These are the four names that custody is spelled as in those
# packages. `base_url` and `url` stay printable on purpose: the server a line is about is what makes
# the line useful, and hiding it would buy nothing (§7.1 ships it to the browser as a deep link).
#
# THE FOURTH IS §7.2's INTAKE TOKEN, and it is the one credential in this set that this app MINTS
# rather than one it was handed: `registry.JellyfinConfig.webhook_token` is sealed beside the
# api_key, shown to an admin exactly once (decision 332) because there is no rotation route, and
# whoever holds it can file acquisition work in the household's name. M5.2 declared it
# `field(repr=False)` and pinned the one site that exists with
# `test_connector_registry.py::test_the_webhook_token_does_not_survive_a_repr` -- and then did not
# add the name here, so the SOURCE sweep, whose whole job is the NEXT dataclass added to these
# packages, could not see a fourth credential at all. `stmt.target.id not in SECRET_FIELD_NAMES`
# matches exactly and not by substring, so a fourth credential needs a fourth name; the guard
# meanwhile read green through `api_key`, which is how `LinkedUser.token` sat outside it once
# before. [M5.2 review cycle 2: m52-c2-secretnames-01]
SECRET_FIELD_NAMES = frozenset({"api_key", "token", "user_tokens", "webhook_token"})


def _dataclass_decorated(node: ast.ClassDef) -> bool:
    """True when a `@dataclass` decorator generates this class's `__repr__`.

    Matched on the last dotted segment so `@dataclasses.dataclass`, `@dataclass` and
    `@dataclass(frozen=True)` are one rule. A plain class is not an offence here: it has no
    generated repr to leak through.
    """
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if ast.unparse(target).split(".")[-1] == "dataclass":
            return True
    return False


def _secret_fields_at_default_repr(source: str, label: str) -> list[str]:
    """Every credential-named dataclass field in `source` that the generated repr will print.

    An annotated assignment in a dataclass body is a field, and a field prints unless its default
    is a `field(..., repr=False)`. Read with `ast` rather than with a regex for the reason
    `_constant_check_predicates` is: `registry.py` already declares one of these across three
    lines, and a regex over a package is how a guard ends up asserting only the cases its author
    had in front of them. `ClassVar` is skipped because it is not a field at all -- flagging it
    would demand `repr=False` where the syntax does not allow it.
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef) or not _dataclass_decorated(node):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                continue
            if stmt.target.id not in SECRET_FIELD_NAMES:
                continue
            if ast.unparse(stmt.annotation).split("[")[0].split(".")[-1] == "ClassVar":
                continue
            keywords: dict[str, ast.expr] = {}
            if isinstance(stmt.value, ast.Call) and ast.unparse(stmt.value.func).endswith("field"):
                keywords = {kw.arg: kw.value for kw in stmt.value.keywords if kw.arg}
            hidden = keywords.get("repr")
            if isinstance(hidden, ast.Constant) and hidden.value is False:
                continue
            offenders.append(f"{label}:{stmt.lineno}: {node.name}.{stmt.target.id}")
    return offenders


def test_no_connector_credential_is_printed_by_a_generated_repr():
    """§14.3 + §2: the connector secret "can administer the whole media server", and a dataclass
    prints every field it is not told to hide.

    Observed rather than imagined. Before `field(repr=False)` landed, a pytest failure header in
    this very suite read
    `JellyfinClient(base_url='http://jellyfin.test', api_key='fake-admin-key', timeout=15.0)`,
    so one assertion about an unrelated behaviour copied an admin-equivalent key into the terminal,
    the CI log and whatever an operator pasted into a bug report. `JellyfinConfig` was worse: it
    carried the key and the whole `user_tokens` map, which §7.3 obtains one person at a time.

    Static because no runtime can hold it. `repr()` is called by the machinery that runs when the
    code is already failing -- a traceback frame, `logging`'s `%r`, pytest's assertion rewriting --
    so a behavioural test would have to enumerate every future leak site, while
    `push/keys.py:50-61` shows the codebase already knows the answer belongs at the declaration
    ("no accessor and no repr"). Both halves are here on purpose: `dataclasses.fields` reads what
    the interpreter actually generated for the two shipped classes, and the source sweep is what
    catches the next dataclass added to the packages.

    The sweep reads three packages and not one. Scoped to `connectors/` it was a rule about a
    directory, and the review that scoped it that way had already put `token` in the set for "the
    NEXT credential field" -- while the only field in the codebase that carries that name,
    `sync/seen.py`'s `LinkedUser.token`, sat one directory over at default repr, holding one
    member's decrypted §7.3 access token for the whole of their sweep. A credential does not
    become printable by moving out of `connectors/`. [M4.11 finding 20, sec-15; review cycle 1:
    m411-rev-jf-02]
    """
    from spielplan.connectors.jellyfin import JellyfinClient
    from spielplan.connectors.registry import JellyfinConfig

    printed: list[str] = []
    for cls in (JellyfinClient, JellyfinConfig):
        fields = dataclasses.fields(cls)
        # A rule that matches on a name has to say the name is still there, or a rename turns it
        # into a rule about nothing -- the M4.8 lesson about an instrument that widened by
        # narrowing.
        assert {f.name for f in fields} & SECRET_FIELD_NAMES, (
            f"{cls.__name__} declares no field named in {sorted(SECRET_FIELD_NAMES)} any more, so "
            f"this rule no longer covers the credential it was written for -- rename it here too"
        )
        printed += [
            f"{cls.__name__}.{f.name}"
            for f in fields
            if f.name in SECRET_FIELD_NAMES and f.repr
        ]

    swept = [path for package in CREDENTIAL_PACKAGES for path in sorted(package.glob("*.py"))]
    for package in CREDENTIAL_PACKAGES:
        assert sorted(package.glob("*.py")), (
            f"no module under {package} to sweep: this rule is reading nothing there"
        )
    offenders = printed + [
        site
        for path in swept
        for site in _secret_fields_at_default_repr(
            path.read_text(encoding="utf-8"), f"{path.parent.name}/{path.name}"
        )
    ]
    assert not offenders, (
        "a connector credential is printed by its own repr, so the next traceback, log line or "
        "assertion header copies an admin-equivalent key (§14.3) into plain text -- declare it "
        "`field(repr=False)`:\n  " + "\n  ".join(offenders)
    )


def test_the_connector_repr_guard_sees_a_repr_enabled_field_on_a_real_dataclass():
    """docs/TESTING.md: "a guard that cannot fail reads as coverage while providing none."

    The runtime half's self-test, built the way the defect was: a credential field declared with
    nothing said about its repr. Asserted through `dataclasses.fields` so the thing proved is the
    predicate the guard above runs, and then through `repr()` as well, because the predicate's
    answer only matters if it tracks what actually gets printed.
    """
    @dataclasses.dataclass
    class Probe:
        base_url: str
        api_key: str
        user_tokens: dict[str, str] = dataclasses.field(default_factory=dict, repr=False)

    leaking = [f.name for f in dataclasses.fields(Probe) if f.name in SECRET_FIELD_NAMES and f.repr]
    assert leaking == ["api_key"], leaking
    assert "s3cret" in repr(Probe("http://jellyfin.test", "s3cret")), (
        "the predicate and the generated repr disagree, so this guard is measuring the wrong thing"
    )


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        # The two declarations that shipped, verbatim: `jellyfin.py:97` and `registry.py:49`.
        ("the annotation with no default", "@dataclass\nclass C:\n    api_key: str\n", 1),
        ("a plain default", '@dataclass(frozen=True)\nclass C:\n    api_key: str = ""\n', 1),
        # A `field()` that says everything except the one thing that matters.
        ("a field call without repr",
         "@dataclass\nclass C:\n    user_tokens: dict = field(default_factory=dict)\n", 1),
        ("repr spelled True", "@dataclass\nclass C:\n    token: str = field(repr=True)\n", 1),
        # §7.2's intake token, the fourth credential these packages carry and the one the
        # sweep was blind to: `registry.JellyfinConfig.webhook_token` is declared
        # `field(repr=False)`, so nothing in the tree failed -- and nothing would have failed
        # for the next dataclass to carry it either, which is the sweep's whole job.
        # [M5.2 review cycle 2: m52-c2-secretnames-01]
        ("the intake token", "@dataclass\nclass C:\n    webhook_token: str = ''\n", 1),
        # Declared across three lines, which is how `registry.py` writes it and why this reads the
        # tree rather than the line.
        ("a declaration that wraps",
         "@dataclasses.dataclass\nclass C:\n    user_tokens: dict[str, str] = field(\n"
         "        default_factory=dict,\n    )\n", 1),
        # The repaired shapes, one per spelling in the package.
        ("the repair", "@dataclass\nclass C:\n    api_key: str = field(repr=False)\n", 0),
        ("the repair with a default",
         "@dataclass(frozen=True)\nclass C:\n"
         "    user_tokens: dict = field(default_factory=dict, repr=False)\n", 0),
        # What the rule must NOT say. A plain class generates no repr; `base_url` is printable on
        # purpose; a local variable is not a field; and a `ClassVar` cannot take `repr=False`.
        ("a class that is not a dataclass", "class C:\n    api_key: str\n", 0),
        ("the server a log line is about", "@dataclass\nclass C:\n    base_url: str\n", 0),
        ("a local of the same name", "def f():\n    api_key = 'k'\n    return api_key\n", 0),
        ("a class variable", "@dataclass\nclass C:\n    api_key: ClassVar[str] = ''\n", 0),
    ],
)
def test_the_connector_repr_guard_catches_each_way_a_field_is_declared(name, source, expected):
    """And the source half's self-test, offences and negative controls in one list, because this
    predicate's whole difficulty is that the offending declaration and the repaired one differ by
    a single keyword argument."""
    assert len(_secret_fields_at_default_repr(source, "probe.py")) == expected, name


# --- M4.11 finding 24 (arch-02): the domain never imports the API layer -------------------------

# CLAUDE.md, Conventions: "Rules live in the domain packages under `backend/spielplan/` (ledger,
# rate, scoring, placement, home, sync, connectors, importer); `api/` decides only HTTP shapes."
# Scoped to the three packages this milestone owns rather than to every package under
# `spielplan/`, so it cannot collide with a wider layering guard another workstream may add -- and
# because a wider one is a different argument, about modules this milestone has not read.
DOMAIN_PACKAGES = ("sync", "push", "connectors")

# The chain the inversion was reachable through, pinned at all three links. Exactly one of them is
# discriminating, and it is not the one the plan names: measured on 2026-09-10 against a tree holding
# `push/send.py:43`'s `from spielplan.api.push import device_handle`, `spielplan.push.send` answered
# True while `spielplan.sync.playback` and `spielplan.worker` both answered False -- because
# `playback.py:167` imports `push.send` inside `_notify` and `worker.py:115` imports `playback`
# inside the `playback-poll` job body, so the deferred links hid the cost from the process that paid
# it. The plan's verification line is `spielplan.worker` alone, which would have been a rule that
# could not fail. The other two stay pinned because the rule is about the worker process, and one
# module-level import added to either link restores the whole chain in a diff that touches neither
# `push/send.py` nor this file.
FASTAPI_FREE_MODULES = ("spielplan.push.send", "spielplan.sync.playback", "spielplan.worker")


def _names_the_api_package(dotted: str) -> bool:
    """Compared on dotted segments, not as a string prefix: `spielplan.api_keys` is not `api`."""
    return dotted == "spielplan.api" or dotted.startswith("spielplan.api.")


def _api_imports(source: str, label: str) -> list[str]:
    """Every import in `source` that reaches `spielplan.api`, in any of its spellings.

    `test_tonight_combine.py:367-394`'s rule -- only import statements, never prose -- with its
    parsing done by `ast` instead of by a line prefix. The reason is in the subject: `push/send.py`
    now quotes `from spielplan.api.push import device_handle` inside `device_handle`'s own
    docstring to explain why it is gone, and a prefix filter survives that only by the luck of
    where the line happens to wrap. An `ast` walk cannot see a docstring as an import at all.
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            offenders += [
                f"{label}:{node.lineno}: import {a.name}"
                for a in node.names
                if _names_the_api_package(a.name)
            ]
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        base = module
        if node.level:
            # Each of the three packages sits exactly one level below `spielplan`, so a relative
            # `from ..api import push` names the same module as the absolute spelling: drop the dots
            # and put the package back. Not a general resolver -- it is exact for this scope, and it
            # errs towards refusing a module called `api` at any relative depth, which a domain
            # package has no business with either.
            base = f"spielplan.{module}" if module else "spielplan"
        # `from spielplan import api` names the package in the alias rather than in the module,
        # which is why the aliases are joined on and not only the module itself.
        dotted = [base, *(f"{base}.{a.name}" for a in node.names)]
        if any(_names_the_api_package(d) for d in dotted):
            names = ", ".join(a.name for a in node.names)
            offenders.append(
                f"{label}:{node.lineno}: from {'.' * node.level}{module} import {names}"
            )
    return offenders


def _imports_fastapi(module: str, extra_path: Path | None = None) -> bool:
    """Does importing `module` in a fresh interpreter pull FastAPI in with it?

    A subprocess because this interpreter cannot answer: the suite's own fixtures build the ASGI
    app, so `fastapi` is in `sys.modules` long before any test runs. `PYTHONPATH` is set from
    `REPO` rather than inherited alone, so the answer is about this worktree however pytest was
    invoked.
    """
    env = dict(os.environ)
    roots = [str(REPO / "backend"), *([str(extra_path)] if extra_path else [])]
    inherited = [env["PYTHONPATH"]] if env.get("PYTHONPATH") else []
    env["PYTHONPATH"] = os.pathsep.join([*roots, *inherited])
    out = subprocess.run(
        [
            sys.executable, "-c",
            f"import sys, importlib; importlib.import_module({module!r}); "
            "print('fastapi' in sys.modules)",
        ],
        capture_output=True, text=True, timeout=300, env=env,
    )
    assert out.returncode == 0, f"importing {module} failed:\n{out.stdout}{out.stderr}"
    return out.stdout.strip().splitlines()[-1] == "True"


def test_no_domain_package_imports_the_api_layer():
    """CLAUDE.md, Conventions: the rules live in the domain packages and `api/` decides only HTTP
    shapes. `push/send.py:43` read `from spielplan.api.push import device_handle` -- the one import
    in the codebase pointing the wrong way.

    Two concrete costs, not a tidiness preference. The worker process imported FastAPI and every
    router side effect along `worker -> sync.playback -> push.send`, for one twelve-character hash
    of an endpoint; and a cycle was one refactor away, because `api/push.py` is exactly the module
    that will want `push/` helpers next -- at which point the import already pointing backwards
    closes the loop, and the failure is an ImportError at boot rather than a review note.

    Static, and with a runtime half, because neither alone is enough. The source scan states the
    direction for every module in the three packages, including ones nothing imports yet; the
    subprocess states the consequence, and is the only half that would notice the direction being
    re-inverted through a module this scan does not read. [M4.11 finding 24, arch-02]
    """
    modules: list[Path] = []
    for name in DOMAIN_PACKAGES:
        found = sorted((REPO / "backend" / "spielplan" / name).rglob("*.py"))
        # Per package, not over the total: a rule scoped by directory name goes quiet when one of
        # those directories is renamed, and seven files out of ten still reads as a healthy sweep.
        assert found, (
            f"no module under spielplan/{name}/: this rule is scoped by directory name, so a "
            f"renamed package silently stops being covered -- name it in DOMAIN_PACKAGES"
        )
        modules += found
    offenders = [
        site
        for path in modules
        for site in _api_imports(
            path.read_text(encoding="utf-8"),
            path.relative_to(REPO / "backend" / "spielplan").as_posix(),
        )
    ]
    assert not offenders, (
        "a domain package imports the API layer, which inverts CLAUDE.md's direction, pulls "
        "FastAPI and every router side effect into the worker process, and puts a circular import "
        "one refactor away:\n  " + "\n  ".join(offenders)
    )
    pulled = [module for module in FASTAPI_FREE_MODULES if _imports_fastapi(module)]
    assert not pulled, (
        "importing these domain modules loads FastAPI, so the worker process pays for the whole "
        f"HTTP layer it never serves: {pulled}"
    )


def test_the_fastapi_probe_sees_a_module_that_does_reach_the_api_layer(tmp_path):
    """docs/TESTING.md: "a guard that cannot fail reads as coverage while providing none."

    The subprocess half's self-test. It has to exist here because the shipped tree answers False for
    all three modules, and because the plan's verification line -- `import spielplan.worker` alone
    -- answered False before the repair too, which is the one way this rule could have shipped
    looking green while measuring nothing.
    """
    (tmp_path / "probe_api_layer.py").write_text(
        "from spielplan.api.push import device_handle\n\n__all__ = ['device_handle']\n",
        encoding="utf-8",
    )
    assert _imports_fastapi("probe_api_layer", extra_path=tmp_path), (
        "a module that imports `spielplan.api.push` does not register as pulling FastAPI, so the "
        "runtime half of this rule cannot fail and proves nothing"
    )


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        # The import that shipped, verbatim.
        ("the one that shipped", "from spielplan.api.push import device_handle\n", 1),
        ("the package itself", "from spielplan.api import push\n", 1),
        ("the alias spelling", "from spielplan import api\n", 1),
        ("a plain import", "import spielplan.api.push\n", 1),
        ("an aliased plain import", "import spielplan.api.deps as deps\n", 1),
        ("relative, through the parent", "from ..api.push import device_handle\n", 1),
        ("relative, naming the package", "from .. import api\n", 1),
        # Inside a function body, which is how the same dependency is usually reintroduced once a
        # guard exists -- `worker.py` already imports its jobs' modules exactly that way.
        ("deferred into a job body",
         "def job():\n    from spielplan.api.push import device_handle\n    return device_handle\n",
         1),
        # What the rule must NOT say. The first is the repair; the second is a sibling package whose
        # name merely starts with the same three letters; the third is the docstring `push/send.py`
        # now carries, which is why this reads the tree and not the line.
        ("the repair", "from spielplan.push.send import device_handle\n", 0),
        ("a package that is not api", "from spielplan.api_keys import mint\n", 0),
        ("the docstring that explains the repair",
         '"""It replaces `from spielplan.api.push import device_handle`, which pointed the wrong\n'
         'way: import spielplan.api.push pulled FastAPI into the worker."""\n', 0),
        ("a sibling domain import", "from spielplan.core.config import settings\n", 0),
        ("a relative sibling", "from . import keys\n", 0),
    ],
)
def test_the_api_import_guard_catches_each_spelling(name, source, expected):
    """Eight offending spellings and five true ones. The aliased, relative and deferred forms are in
    the list because a rule that only sees `from spielplan.api.x import y` is a rule about the one
    line of the diff that caused it."""
    assert len(_api_imports(source, "probe.py")) == expected, name


# --- M4.11 finding 25 / tq3: the sweep boundary belongs to the database -------------------------

SEEN = REPO / "backend" / "spielplan" / "sync" / "seen.py"


def _datetime_imports(source: str, label: str) -> list[str]:
    """Every import of the standard-library clock in `source`, at module level or in a function.

    By name and not by use: `datetime` has no business in this module at all, so the import is the
    earliest and least ambiguous place to catch it. Comments and docstrings are invisible to an
    `ast` walk, which matters here more than anywhere else in this file -- `seen.py` mentions
    `datetime` three times in prose, twice to argue why it holds none, so a `"datetime" in source`
    assertion would fire on the comment explaining the rule it is enforcing.
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            offenders += [
                f"{label}:{node.lineno}: import {a.name}"
                for a in node.names
                if a.name == "datetime" or a.name.startswith("datetime.")
            ]
            continue
        if not isinstance(node, ast.ImportFrom) or node.level:
            continue
        module = node.module or ""
        if module == "datetime" or module.startswith("datetime."):
            names = ", ".join(a.name for a in node.names)
            offenders.append(f"{label}:{node.lineno}: from {module} import {names}")
    return offenders


def test_the_sweep_module_reads_its_boundary_from_the_database_and_holds_no_clock():
    """§7.3's conflict rule compares `user_title.jf_synced_at` against the moment the sweep read
    Jellyfin, and Postgres writes both of those timestamps. A `datetime.now(UTC)` here compares two
    clocks that agree only by luck -- a container whose time drifts from the database server's then
    decides, for every linked member, whether an action taken during the sweep is pushed or
    adopted, which is the direction a person's own tap gets reverted in.

    The companion to `test_seen_sync.py::test_the_sweep_boundary_ignores_this_processs_clock`, which
    is the behavioural half and was inert for a milestone: it patched `seen.datetime` with
    `raising=False` on a module that imports no `datetime`, so running its body without the patch
    gave the identical answer. That test now injects the skew at the real seam (a connection whose
    `fetchval` answers `'SELECT now()'` ten seconds early); this states the same rule one level up,
    where a reintroduced import is caught by name before any behaviour depends on it -- and where a
    future `_note_unreachable` computing its minutes from a wall clock rather than from
    `time.monotonic` is caught as well. [M4.11 finding 25, tq3]
    """
    source = SEEN.read_text(encoding="utf-8")
    # The anti-vacuity half: the rule is "the boundary comes from the database", which deleting the
    # read would also satisfy. `SELECT now()` being gone means this rule needs rewriting, not that
    # the module got cleaner.
    assert "SELECT now()" in source, (
        "sync/seen.py no longer reads its sweep boundary with `SELECT now()`, so this rule is "
        "about a clock nothing consults -- find where the boundary comes from now"
    )
    offenders = _datetime_imports(source, "sync/seen.py")
    assert not offenders, (
        "sync/seen.py imports a process clock, and §7.3's conflict rule compares its answer "
        "against timestamps Postgres wrote -- read the boundary with `SELECT now()` and measure "
        "elapsed time with `time.monotonic`:\n  " + "\n  ".join(offenders)
    )


def test_the_clock_import_guard_reads_the_imports_and_not_the_prose_about_them():
    """The negative half, measured against the shipped file rather than a fixture, because the naive
    form of this rule fails on it today: `seen.py` argues in three comments about the `datetime` it
    does not import, so the one-line substring assertion the plan asked for would have had to strip
    the comments first. Reading imports is the same rule with nothing to strip."""
    source = SEEN.read_text(encoding="utf-8")
    assert "datetime" in source, (
        "sync/seen.py no longer explains why it holds no datetime, so this negative control is "
        "measuring nothing -- it exists because the word is in the prose and not in the imports"
    )
    assert not _datetime_imports(source, "sync/seen.py")


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        ("the module", "import datetime\n", 1),
        ("the names", "from datetime import UTC, datetime\n", 1),
        ("aliased", "import datetime as dt\n", 1),
        ("a submodule", "from datetime import timezone\n", 1),
        # Deferred into the function that reads the clock, which is how a module-level ban is
        # usually worked around.
        ("deferred into a helper",
         "async def _boundary(conn):\n    from datetime import UTC, datetime\n"
         "    return datetime.now(UTC)\n", 1),
        # What the rule must NOT say. `time.monotonic` is the elapsed-time clock step 3g was written
        # against on purpose; the prose is the reason the import is absent; and a module whose name
        # merely contains the word is not the clock.
        ("the elapsed-time clock", "import time\n\n_since = time.monotonic()\n", 0),
        ("the comment that argues the rule",
         "# `time.monotonic`, not a datetime: a module that imports `datetime` invites the next\n"
         "# reader to take the boundary from this process.\nimport time\n", 0),
        ("a docstring quoting the banned line",
         '"""The boundary is `SELECT now()`, never `from datetime import UTC, datetime`."""\n', 0),
        ("a name that contains the word", "from spielplan.core.datetimes import floor\n", 0),
    ],
)
def test_the_clock_import_guard_catches_each_way_the_clock_returns(name, source, expected):
    """Five offending spellings and four true ones, the last four being the shapes that made the
    behavioural test inert in the first place: prose about a clock is not a clock."""
    assert len(_datetime_imports(source, "probe.py")) == expected, name


# --- M4.11 review cycle 2: two §6.6 clauses the map published and no registered suite held -----
#
# `jellyfin-sync-a-failed-played-write-is-counted-not-swallowed` ends its `what` with "the admin
# card refuses to call that sweep healthy", and `jellyfin-sync-the-server-version-gates-the-played-
# write` ends its with "The admin copy names the write, not the reads." Both sentences are about
# markup: the health verdict is a ternary over the report and the below-the-pin line is component
# copy, neither of which the backend strings those rows name can drift with. The vitest beside
# `+page.svelte` does assert them and cannot be registered -- `docs/milestones/M4.11-plan.md`'s
# risks section forbids putting a `frontend/src/...` path in a `tests` list, and decision 206
# refused to register vitest ids even after M4.9 made them resolvable -- so the third layer the
# house already uses for a Svelte-source clause carries them instead: a static guard, the same
# choice `test_the_cold_badge_expression_reads_e_source_not_placement` makes for §8's badge.
# [M4.11 review cycle 2: m411-c2-cov-01]

CONNECTORS_PAGE = FRONTEND / "routes" / "admin" / "connectors" / "+page.svelte"

# The rule, pinned rather than described, because the two ways of weakening it are both the
# natural thing to write. Keyed on `push_failed` alone it was green for a sweep that never read
# the library (finding 3); keyed additionally on an empty `users` it was green for a sweep whose
# library read worked and whose member read 404'd for ever (cycle 1, seen-02). `skipped_no_link`
# is the negative control inside the expression: §3.1 makes a household with no connector and no
# link a legal state, and an outage is not what that is.
_SYNC_HEALTH = (
    "syncResult.push_failed ? 'failing' "
    ": !syncResult.skipped_no_link && "
    "(!syncResult.users?.length || syncResult.failed_users?.length) ? 'unreachable' : 'ok'"
)


def test_the_sync_health_verdict_refuses_a_sweep_that_reconciled_nobody():
    """§6.6's card is the only place a household ever sees whether §7.3 ran, and `ok` is a claim.

    The backend report has no health field -- `sync_all` returns counters -- so this ternary IS
    the verdict, and every counter in it reads zero for three different sweeps: the quiet healthy
    household, the one whose Jellyfin never answered, and the one whose member account was
    deleted. The first must print green and the other two must not, which is a distinction no
    test named on the two coverage rows can make: they assert `failed_users` and `push_failed` in
    the report, one layer below the sentence those rows publish.
    [M4.11 review cycle 2: m411-c2-cov-01; §6.6, §7.3, §3.3]
    """
    page = _src(CONNECTORS_PAGE)
    found = re.search(r"data-sync-health=\{(?P<expr>.*?)\}\n", page, re.S)
    assert found, "the sync card no longer publishes a health verdict for anyone to read"
    assert " ".join(found.group("expr").split()) == _SYNC_HEALTH, (
        "the sweep health verdict must deny `ok` to a sweep that read nobody -- an empty `users` "
        "AND a non-empty `failed_users` -- and must still allow it to a household with no link:\n"
        f"  found:  {' '.join(found.group('expr').split())}\n  wanted: {_SYNC_HEALTH}"
    )


def test_the_below_pin_copy_names_the_played_write_and_not_the_reads():
    """§7.1's pin is a gate on one route, and the card used to name the wrong half of it.

    The copy said reads "may miss fields", which is the degradation a 10.8 server does NOT have:
    what it lacks is `POST /UserPlayedItems`, so the app -> Jellyfin direction is dead while every
    read keeps working. An admin who acted on the old sentence would have gone looking at their
    metadata. `test_a_server_below_the_pin_refuses_the_played_write_by_name` holds the connector's
    own refusal string, which is a different string in a different file and drifts from this one
    freely -- and this sentence is printed twice, once off the stored verdict and once off a fresh
    probe, so a repair to one of them leaves the other saying whatever it said.
    [M4.11 review cycle 2: m411-c2-cov-01; §7.1, §6.6]
    """
    # Comments stripped for the reason the runtime sweep strips them: this codebase quotes the
    # wording a surface stopped using in the sentence explaining why it stopped, and both of the
    # comments above these lines quote "reads may miss fields" to bury it.
    code = _COMMENTARY.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), _src(CONNECTORS_PAGE))
    flat = " ".join(code.split())
    sentences = [flat[m.start():].partition("{")[0] for m in re.finditer(r"below the pinned", flat)]
    assert len(sentences) == 2, (
        f"the card prints {len(sentences)} below-the-pin sentences; §7.1's verdict reaches the "
        "admin twice -- from the stored probe and from a fresh one -- and both are the copy"
    )
    silent = [s for s in sentences if "POST /UserPlayedItems" not in s]
    assert not silent, (
        "a below-the-pin line has to name the route that is missing, or it accuses the whole "
        "connector:\n  " + "\n  ".join(silent)
    )
    accuses = [s for s in sentences if re.search(r"reads? (?:may|might|can)\b", s, re.I)]
    assert not accuses, (
        "the pin gates the Played WRITE; reads degrade nowhere and saying they do sends the "
        "admin after their metadata:\n  " + "\n  ".join(accuses)
    )


# --- M4.11 review cycle 2: the decision 213 narrowed, left saying what it said ----------------
#
# The same shape as the two M4.10 guards above, one milestone on: decision 213 was written
# because 210(4)'s adopt clause -- inherited verbatim from 172(4), which predates 210(a) and so
# predates the existence of a direction in which the app never writes -- composed into a loop
# that reverted every app-side series `unseen` within one sweep. 213 says so and 210 does not.
# Eight source sites cite a bare "decision 210" (`sync/seen.py`, `connectors/resolve.py`,
# `connectors/jellyfin.py`, `sync/playback.py`, `spec_coverage.toml`), so a reader following any
# of them lands on a numbered owner decision arguing for the loop 213 removed -- and the register
# is the text a later milestone reads BEFORE the code. The file already back-annotates the
# superseded side everywhere else (proposal 148's "Superseded by decision 166", proposal 54's
# heading, decision 193's in-place correction); this holds 210 to that practice by name.
# [M4.11 review cycle 2: m411-c2-dec-03]

# 172's fourth answer, in the two spellings decision 210 uses to inherit it.
_INHERITS_172_FOUR = r"172(?:'s)?\s*(?:\(\s*4\s*\)|fourth)"


def _inherits_172s_adoption_rule_unnarrowed(text: str) -> list[str]:
    """Every clause in `text` that takes 172(4)'s adoption rule without naming what narrowed it.

    Clause-scoped for the reason the two detectors above are clause-scoped: 213's argument has to
    be able to quote the rule it narrowed, and 210's own Why paragraph has to be able to describe
    the adopt direction as the defect it found there -- a ban on the word "adopt" would forbid the
    register from explaining itself. A clause offends only when it INHERITS the rule, which both
    shipped statements do by citing 172's fourth answer, and names neither decision 213 nor the
    narrowing. Stop class `.` and `|`: the sentence and the summary-table cell.
    """
    flat = " ".join(text.split())
    return [
        m.group(0).strip()
        for m in re.finditer(rf"[^.|]*{_INHERITS_172_FOUR}[^.|]*", flat)
        if not re.search(r"\b213\b|narrow", m.group(0), re.I)
    ]


def test_decision_210s_adoption_clause_carries_the_narrowing_213_imposed():
    """The register kept publishing the adopt loop that decision 213 exists to remove.

    210 states its adoption rule three times -- the summary row, the opening of "The decision",
    and the clause that spells it out -- and all three inherit 172(4) verbatim: "the sweep adopts
    Jellyfin-played over an app `unseen` or an absent row". For `kind='series'` the shipped sweep
    adopts over no existing row at all, in either direction, and §7.3's conflict rule was rewritten
    to match and cites both numbers. Only the superseded side went unmarked, which is the reader
    this file already misled once with decision 172's stale "record the whole as decision 167".
    [M4.11 review cycle 2: m411-c2-dec-03; decisions 210, 213]
    """
    offenders = _inherits_172s_adoption_rule_unnarrowed(_decision(210))
    assert not offenders, (
        "decision 210 inherits 172(4)'s adopt clause unnarrowed; decision 213 refuses adoption "
        "over an existing series row in BOTH directions, and a reader arriving by one of the "
        "code citations reads the loop instead:\n  " + "\n  ".join(offenders)
    )

    # The other direction: the record is only right while the code still reads this way. 213's
    # own Cost paragraph names this branch -- `if kind == "series" and not jf_seen` became
    # `if kind == "series"` -- so if the guard ever comes back, both texts are wrong together.
    flat = " ".join(SEEN.read_text(encoding="utf-8").split())
    assert 'if kind == "series": ' in flat, (
        "the adopt path no longer refuses a series unconditionally; decision 213 is the record of "
        "that refusal and has to be amended in the same commit that moves it"
    )
    assert 'if kind == "series" and not jf_seen' not in flat, (
        "the adopt path is back to un-marking only, which is 210(4) before decision 213 narrowed "
        "it -- an app-side series `unseen` is re-adopted from the computed folder flag"
    )


@pytest.mark.parametrize(
    "name,text,expected",
    [
        # The two shipped statements, in the two shapes they shipped in: a table cell and a
        # sentence inside the argued section.
        ("the summary row", "| 210 | ... | **(c) On the last known episode**, from a cached "
                            "`GET /Shows/{SeriesId}/Episodes?userId=`. Adoption follows 172(4): "
                            "played may mark, not-played may never un-mark, and a folder "
                            "reporting zero children marks nothing. |", 1),
        ("the decision paragraph", "And adoption, per 172(4): the sweep adopts Jellyfin-played "
                                   "over an app `unseen` or an absent row, never adopts "
                                   "Jellyfin-not-played over an app `seen`", 1),
        # The argument FOR the rule 210 took has to stay sayable, or the guard bans a citation
        # rather than a claim -- and 210's Why paragraph names the adopt direction as the defect.
        ("the correction", "Adoption follows 172(4) as decision 213 narrows it: over a "
                           "`user_title` row that already exists a series adopts in neither "
                           "direction", 0),
        ("the why paragraph", "The same fact has a second face in the adopt direction, because "
                              "Jellyfin computes a folder's `Played` as `playedCount >= "
                              "totalCount`", 0),
    ],
)
def test_the_decision_210_adoption_guard_catches_each_way_it_was_said(name, text, expected):
    """docs/TESTING.md: "a guard that cannot fail reads as coverage while providing none."

    Four cases: the two wordings that shipped, the correction that must pass, and the sentence in
    210's own Why paragraph that describes the adopt direction without inheriting its rule.
    """
    assert len(_inherits_172s_adoption_rule_unnarrowed(text)) == expected, name


# --- M4.12 review cycle 1: three records the tree had no way to hold ---------------------------
#
# CLAUDE.md makes the argued comment the record, which is worth exactly as much as the record's
# accuracy. Three of M4.12's failed differently and none of them could go red: a causal claim
# about a seam the app contradicts, a coordinate into a file the same diff rewrote, and a line of
# copy decision 222 ships with no assertion at any layer. Each guard below weighs a claim against
# the artifact that settles it -- `app.py`'s own handler, the client module, the Tonight page --
# which is the only reason a comment or a rendered string can be held at all.
# [M412-CONC-02, M412-CONC-03, M412-FE-2]

TONIGHT_PACKAGE = REPO / "backend" / "spielplan" / "tonight"
TONIGHT_CLIENT = FRONTEND / "lib" / "tonight.svelte.js"
TONIGHT_PAGE = FRONTEND / "routes" / "tonight" / "+page.svelte"
APP_MODULE = REPO / "backend" / "spielplan" / "app.py"

# What a paragraph has to be about before its "500" is a claim about a lost write race. Naming one
# of the three constraints, or the index itself, is the whole of it.
_A_LOST_UNIQUENESS_RACE = re.compile(
    r"unique index|UniqueViolationError|session_answer_seq|session_ballot_one_per_title|"
    r"session_result_pkey"
)
_A_FIVE_HUNDRED = re.compile(r"\b500\b|500ing")


def _paragraphs(source: str) -> list[str]:
    """Prose blocks, as a reader meets them: blank lines and bare `#` lines both end one."""
    flattened = re.sub(r"(?m)^[ \t]*#[ \t]*$", "", source)
    return [p for p in re.split(r"\n[ \t]*\n", flattened) if p.strip()]


def _answers_a_lost_race_with_a_500(source: str) -> list[str]:
    return [
        p for p in _paragraphs(source)
        if _A_LOST_UNIQUENESS_RACE.search(p) and _A_FIVE_HUNDRED.search(p)
    ]


def test_the_uniqueness_seam_the_tonight_comments_point_at_answers_409():
    """The premise the guard below rests on, read off `app.py` rather than assumed.

    M4.7's `_conflict` is registered for `asyncpg.UniqueViolationError` and answers 409 with the
    constraint's name; the general `PostgresError` handler answering 500 is a different exception.
    If that ever stops being true the guard below becomes the wrong rule, so it is asserted here
    rather than carried in a reviewer's head. `test_http_seam.py` asserts the behaviour over ASGI;
    this asserts that the two handlers are still two.
    """
    source = APP_MODULE.read_text(encoding="utf-8")

    assert "@app.exception_handler(asyncpg.UniqueViolationError)" in source
    conflict = source[source.index("(asyncpg.UniqueViolationError)"):]
    conflict = conflict[:conflict.index("@app.exception_handler(asyncpg.PostgresError)")]
    assert "status_code=409" in conflict, "the seam stopped answering 409"
    assert "exc.constraint_name" in conflict, "and stopped naming the constraint"


def test_no_tonight_paragraph_answers_a_lost_uniqueness_race_with_a_500():
    """M4.12's three write-race repairs were argued from a status the app does not return.

    Nine comments and two coverage rows said the loser of a race to `session_answer_seq`,
    `session_ballot_one_per_title` or `session_result_pkey` got a 500. It got 409 with the
    constraint's name, from a handler M4.7 shipped in the same repository with a test whose
    docstring reads "the seam M4.10 and M4.12 plug into, asserted here so they can point at it".
    The repairs were right for other reasons -- a reused seq, a wedged seat, a whole ballot rolled
    back -- but a maintainer weighing an unguarded write against a lock reads these paragraphs to
    learn what this app does with a lost race, and plan step 6 forbids the redundant except
    branch the false record invites. [M412-CONC-02]
    """
    offenders = {
        path.name: _answers_a_lost_race_with_a_500(path.read_text(encoding="utf-8"))
        for path in sorted(TONIGHT_PACKAGE.glob("*.py"))
    }
    named = {name: found for name, found in offenders.items() if found}

    assert not named, "\n\n".join(
        f"{name}: {block.strip()[:400]}" for name, blocks in named.items() for block in blocks
    )


@pytest.mark.parametrize(
    "name, text, expected",
    [
        ("record_answer", "the loser's INSERT collided with 0014's partial unique index -- "
                          "`app.py` turns that into a 500, and the client re-reads on a 409.", 1),
        ("ballot.submit", "its INSERTs collided with `session_ballot_one_per_title`, and `app.py` "
                          "turns every `asyncpg.PostgresError` into a 500.", 1),
        ("finish", "it deletes nothing, inserts, and is refused by `session_result_pkey` -- a 500 "
                   "on the last answer of somebody's round.", 1),
        ("the correction", "refused by `session_result_pkey` -- 409 conflict: session_result_pkey "
                           "out of `app.py`'s handler, on a room that is by then fine.", 0),
        ("settle", "a combine that raised once left the answer standing, the request 500ing, and "
                   "the room in `voting` for the rest of the evening.", 0),
    ],
)
def test_the_lost_race_guard_catches_each_way_it_was_said(name, text, expected):
    """docs/TESTING.md: "a guard that cannot fail reads as coverage while providing none."

    Three shipped wordings, the correction that has to stay sayable, and a 500 about something
    else entirely -- a combine that RAISED is a real 500 and settle's docstring says so.
    """
    assert len(_answers_a_lost_race_with_a_500(text)) == expected, name


_CLIENT_BY_LINE = re.compile(r"tonight\.svelte\.js:\d+")
_CLIENT_BY_SYMBOL = re.compile(r"tonight\.svelte\.js`'s `(\w+)`")


def test_no_backend_comment_cites_the_tonight_client_by_line_number():
    """A coordinate into another file is a citation nothing can keep true.

    Four backend comments and one coverage-registered test docstring cited
    `tonight.svelte.js:183-196`, `:259`, `:273` and `:275` -- taken from the plan, which took them
    from a commit two before the one that shipped -- and the same diff rewrote that module from 490
    lines to 774. All five pointed at a closing brace, a comment fragment or an unrelated
    statement, and one of them also described refresh as having three branches in a diff that gave
    it five. A function name survives the edit that moves it, and is checkable, which is the whole
    difference. channel.py already cited the module this way. [M412-CONC-03]
    """
    cited = {
        str(path.relative_to(REPO)): _CLIENT_BY_LINE.findall(path.read_text(encoding="utf-8"))
        for path in sorted((REPO / "backend").rglob("*.py"))
        # This file quotes the coordinates it forbids, which is how the docstring above says what
        # went wrong; a guard may name its own subject.
        if path != Path(__file__)
    }
    named = {path: hits for path, hits in cited.items() if hits}

    assert not named, f"cite the function, not the line: {named}"


def test_every_tonight_client_function_the_backend_cites_exists():
    """The other half: a name is only better than a number if something checks the name.

    Read off the module's own declarations rather than by importing it -- this is a Svelte runes
    module, and the guard is about what a reader will find when they open the file.
    """
    declared = set(re.findall(r"function (\w+)\(", TONIGHT_CLIENT.read_text(encoding="utf-8")))
    assert declared, "the client module declares no functions, so this guard is reading nothing"

    cited: dict[str, set[str]] = {}
    for path in sorted((REPO / "backend").rglob("*.py")):
        found = set(_CLIENT_BY_SYMBOL.findall(path.read_text(encoding="utf-8")))
        if found:
            cited[str(path.relative_to(REPO))] = found

    assert cited, "nothing cites the client by name, so this guard is reading nothing"
    unknown = {path: sorted(names - declared) for path, names in cited.items() if names - declared}
    assert not unknown, f"named a client function that does not exist: {unknown}"


def test_the_reshuffle_wrapped_line_is_rendered_where_decision_222_says_it_is():
    """Decision 222 ships one quiet line beside Reshuffle, and nothing asserted that it renders.

    A grep for tonight-wrapped and WRAPPED_LINE over the tree found exactly three hits -- the
    constant, the import and the paragraph element -- so the rendered half of the decision rested
    on nobody deleting it. The flag behind it is the backend's and is tested there; what this layer
    holds is that the copy is still wired to the flag and spelled once. The browser holds the rest
    -- `14-tonight.spec.js`'s reshuffle case presses until the line appears, which on a bundle of
    six owned films is the second press. [M412-FE-2; M412-SOLO-03; decision 222]
    """
    page = TONIGHT_PAGE.read_text(encoding="utf-8")
    client = TONIGHT_CLIENT.read_text(encoding="utf-8")

    copy = re.search(r"export const WRAPPED_LINE = .([^']+).;", client)
    assert copy, "the line decision 222 renders is no longer a constant of the client module"
    assert "{#if tonight.solo.wrapped}" in page, "the line is rendered unconditionally, or not"
    assert 'data-testid="tonight-wrapped"' in page, "and has no handle for an e2e assertion"
    assert "{WRAPPED_LINE}" in page, "the page spells the copy itself"
    assert copy.group(1) not in page, (
        "the copy is written twice, and the second spelling is the one that will drift"
    )


# --- M4.12 review cycle 1: the map's account of the socket's own history ------------------------
#
# A fourth record the tree had no way to hold, and the same shape as the three above: a claim
# whose truth is settled by an artifact in the repository, written down as its opposite. This one
# is in `spec_coverage.toml`, which CLAUDE.md calls the contract -- so the artifact that settles
# it is the map itself, where a shipped M0 row has named the refusal as its own evidence since
# before the Tonight channel had a second gate to argue about. [M412-API-03]

LOCKED_SOCKET_TEST = (
    "backend/tests/test_api_gating.py::test_the_tonight_channel_refuses_a_locked_account"
)

# Absolute denials only. A `why` may say the gate was undeclared, written out by hand, or
# invisible to a dependency sweep -- all three are true of this route before M4.12, and the route
# docstring says exactly that. What no row may say is that the refusal was not there.
_A_DENIAL = re.compile(r"never enforced|never refused|never closed|did not enforce|was not enforced")


def test_no_row_calls_decision_179s_socket_refusal_m4_12s_own_defect():
    """M4.12's channel row told the contract file that the lock had never been enforced.

    It had. Decision 179 wrote `must_change_password` out by hand inside the route in M4.6 -- one
    branch closing the anonymous and the locked caller together with `WS_1008_POLICY_VIOLATION`
    before `accept` -- and M0's `platform-forced-first-login-change` has named the gating test as
    ITS evidence ever since, its `what` saying in as many words that the Tonight WebSocket closes
    a locked session. So the two `== 1008` assertions M4.12 added to that test are green against
    the route as it was. What this milestone changed is that the gate is DECLARED, through
    `deps.active_user_ws` where a dependency sweep can see it, and that the route holds none of
    the pool's connections across its sends.

    The row said the opposite ten lines above its own trailing comment, which says decision 179
    "already wrote the `must_change_password` check out by hand inside the route". That matters
    because a later milestone reads this file to learn what was established and when: a reader who
    believes the `why` deletes
    `test_the_channel_is_behind_the_dependency_graph_and_never_behind_deps_db` as duplicated by
    the gating test, takes decision 225's only assertion with it, and the row stays green.

    Premise first, as `test_the_uniqueness_seam_the_tonight_comments_point_at_answers_409` does
    above: if M0 ever stops claiming the refusal, the rule enforced below is the wrong one and
    should fail loudly rather than pass quietly. [M412-API-03; decisions 179, 225]
    """
    rows = tomllib.loads(COVERAGE.read_text(encoding="utf-8"))["requirement"]
    naming = [r for r in rows if LOCKED_SOCKET_TEST in r.get("tests", [])]
    origin = [r for r in naming if r["id"] == "platform-forced-first-login-change"]
    assert origin, (
        "M0's platform-forced-first-login-change no longer names the locked-socket test, so the "
        "premise this guard weighs later rows against has moved out of the map"
    )
    assert "Tonight WebSocket closes it" in origin[0]["what"], (
        "M0's row stopped claiming that the Tonight WebSocket closes a locked session; this "
        "guard's premise moved, so the rule below is no longer the right one"
    )

    for row in naming:
        if row["id"] == origin[0]["id"]:
            continue
        denial = _A_DENIAL.search(row["why"])
        assert not denial, (
            f"{row['id']} ({row['milestone']}) says the socket {denial.group(0)!r} section 3.1's "
            f"first-login lock, while {origin[0]['id']} (M0) names the same test as evidence that "
            "it did. The gate was undeclared, not absent - say that instead"
        )
        assert "179" in row["why"], (
            f"{row['id']} narrates this socket's pre-M4.12 auth without crediting decision 179, "
            "which is what put the refusal in the route body by hand and is the whole reason the "
            "1008 assertions are a regression guard rather than this milestone's evidence"
        )


# --- M4.12 review cycle 2 (M412-GATE-8): the instrument reads what it grades --------------------
#
# The guards at the top of this file ask whether an exit check CAN report a failure. This one asks
# a narrower question of one check, and it is the same question one layer in: check 8 of
# `ops/m412_exit_criterion.py` grades the clause "a pool of two or three candidates reaches the
# ballot rather than waiting silently", and the only thing that can move such a room is the round
# read -- `boundary` returns None at or below the shortlist size, so there is no pair to answer,
# and the escape is refused below pair six (decision 215). The check issued one more GET of the
# session AFTER its loop of seat reads, and every read of the session, the ballot and the result
# calls `play.settle`: so the trailing GET moved the room itself, the verdict came back 'ballot'
# whichever way the round read behaved, and the detail line the check prints -- "'voting' is the
# silent wait this milestone closed" -- named the one thing it could not see.
#
# Held as source rather than by running the script, for the reason §12 rows are: its runtime is a
# live stack and the shipped 696-title bundle. What is being held is an ORDER, and an order is
# visible in the source.


_SMALL_POOL_CHECK = "check_eight"
_SETTLING_ROUTE = "/api/tonight/sessions"


def _settling_reads_after_the_seat_loop(source: str, label: str) -> list[str]:
    """Every settling route check 8 reads after the seat reads whose effect it is grading.

    `/api/tonight/sessions` covers all three of them -- the session, its `/ballot` and its
    `/result` -- because `api/tonight.py` settles on each one; the round read the loop makes is
    `/api/tonight/seats/{id}/round` and is deliberately not a prefix of it.

    The loop is found by what it ITERATES rather than by position or by what it contains: check 8
    runs its whole sequence twice, once per pool size, so the enclosing `for size in (3, 2)` holds
    the seat read too, and a reader keyed on that read would find two loops and be unable to say
    which one a trailing GET followed. Keyed on `e.seats(...)`, a statement inserted above the
    loop cannot quietly move the line this measures from either.
    """
    tree = ast.parse(source)
    checks = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == _SMALL_POOL_CHECK
    ]
    assert len(checks) == 1, f"{label}: {_SMALL_POOL_CHECK} is check 8 and there is exactly one"
    loops = [
        node for node in ast.walk(checks[0])
        if isinstance(node, ast.For) and "e.seats(" in ast.unparse(node.iter)
    ]
    assert len(loops) == 1, f"{label}: check 8 reads its seats in exactly one loop"
    ends_at = loops[0].end_lineno
    return [
        f"{label}:{node.lineno}: {node.value!r} is read after the seat loop ends at line {ends_at}"
        for node in ast.walk(checks[0])
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and _SETTLING_ROUTE in node.value and node.lineno > ends_at
    ]


def test_the_small_pool_check_grades_the_state_the_seat_reads_left():
    """Check 8's verdict is what the seat reads left behind, or it is not a measurement.

    `tonight.svelte.js`'s `refresh()` GETs the session and only then calls `loadRound`, so a
    session read after the last round read is an order no client produces -- and it is the order
    that made the check green over the defect the clause is about. The session read stays in the
    sequence, before each seat's round read, because that is where the phone puts it and because
    on a pool this size it settles nothing when it runs. [M412-GATE-8; decision 215]
    """
    script = REPO / "ops" / "m412_exit_criterion.py"
    offenders = _settling_reads_after_the_seat_loop(script.read_text(encoding="utf-8"), script.name)
    assert not offenders, (
        "check 8 reads a settling route after the seat reads it grades, so it reports 'ballot' "
        "whether or not the round read moved the room:\n  " + "\n  ".join(offenders)
    )


def test_the_settling_read_guard_catches_a_trailing_session_get():
    """The arrangement that shipped, and the one that replaced it."""
    body = [
        "async def check_eight(e):",
        "    for size in (3, 2):",
        "        for seat in await e.seats(session_id):",
        "            await e.card(e.client_for(seat['user_id']), seat['id'])",
    ]
    trailing = "\n".join([*body, "    await e.host.get(f'/api/tonight/sessions/{session_id}')"])
    caught = _settling_reads_after_the_seat_loop(trailing + "\n", "probe.py")
    assert len(caught) == 1 and caught[0].startswith("probe.py:5: "), caught

    leading = "\n".join(
        [
            *body[:3],
            "            await e.host.get(f'/api/tonight/sessions/{session_id}')",
            body[3],
        ]
    )
    assert _settling_reads_after_the_seat_loop(leading + "\n", "probe.py") == []


# --- M4.13 review cycle 2: two records that outlive the run they describe ----------------------
#
# Both guards below hold a sentence rather than a symbol, which is the shape the decision guards
# above already have: the register and `docs/TESTING.md` are what a later milestone reads BEFORE
# the code, and CLAUDE.md sends the next reader to the ledger "rather than assuming status". One
# holds decision 244's enumeration of the harness lane against the files that lane actually
# touched; the other holds the one figure `ops/m413_exit_criterion.py` publishes as measured and
# does not compute.

TESTING_LEDGER = REPO / "docs" / "TESTING.md"
M413_SCRIPT = REPO / "ops" / "m413_exit_criterion.py"

# The two symbols the parallel-lane harness repair introduced, and the only mechanical handle on
# a lane whose definition is a cause rather than a directory. `env.mjs` is the module e2e's
# origin and its two published ports are resolved through; `_inside_the_container` is the DSN
# rewrite a container-run `pg_dump` needs the moment a worktree publishes something other than
# 5432. Neither existed before the roadmap put a stack per worktree on one box, so a file naming
# one is a file that lane touched -- which is what decision 244's enumeration has to keep up
# with, because the enumeration is what the commit split is cut along.
LANE_MARKERS = ("env.mjs", "_inside_the_container")


def _decision_section(number: int) -> str:
    """One decision's argued section, for a decision the register gives no summary row.

    `_decision` reads the table cell as well, because the misnaming it was written for was in
    both halves and a reader cites whichever they opened. That table answers proposals; 244-246
    rule on how a milestone is CLOSED and answer none, so asserting a row here would be asserting
    a row nobody owes.
    """
    body = REGISTER.read_text(encoding="utf-8")
    heads = list(re.finditer(r"^### (\d+)\. ", body, re.M))
    start = next(m for m in heads if m.group(1) == str(number))
    after = [m.start() for m in heads if m.start() > start.start()]
    return body[start.start(): after[0] if after else len(body)]


def _carries_the_lane(path: Path) -> bool:
    """Whether a file CARRIES the lane repair, rather than citing its lesson in passing.

    The two markers are a proxy for the repair, and read off raw text the proxy convicts a comment.
    M4.16's `test_release_gate.py` argues decision 299's interpreter lookup by pointing at
    `env.mjs`'s lesson "one value over" -- provenance, in a single `#` line, resolving nothing
    per-checkout. Decision 244 cannot name it without claiming an M4.16 file for M4.13's
    `chore(e2e):` commit, so the only ways to a green build were to make the register false or to
    delete a true citation: a guard forcing the record to lie in order to stay quiet, which is the
    inversion this milestone exists to remove. Python is tokenized because that is where the
    ambiguity arose and where a comment can be told from a string exactly; every other file in the
    lane names a marker in code, so the distinction takes nothing away from them. A repair lives in
    code by definition -- a file that only ever mentions the lane in prose is not in it.
    [decision 244; M4.16]
    """
    source = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        commented = "".join(t.string for t in tokens if t.type == tokenize.COMMENT)
        return any(source.count(m) > commented.count(m) for m in LANE_MARKERS)
    return any(marker in source for marker in LANE_MARKERS)


def _lane_files() -> list[str]:
    """Every file in the tree carrying one of the lane's two symbols."""
    roots = [
        *(REPO / "e2e").glob("*.mjs"),
        *(REPO / "e2e").glob("*.js"),
        *(REPO / "backend" / "tests").glob("*.py"),
        *(REPO / "backend" / "spielplan").rglob("*.py"),
        *(REPO / "ops").glob("*.py"),
        *(REPO / "ops").glob("*.yml"),
    ]
    return sorted(
        path.relative_to(REPO).as_posix()
        for path in roots
        # This file names both markers in order to look for them, exactly as the retired-claim
        # sweep above states the phrases it forbids.
        if path != Path(__file__).resolve() and _carries_the_lane(path)
    )


def _unnamed_in(text: str, files: list[str]) -> list[str]:
    """The files `text` does not name in backticks -- the register's own citation form."""
    return [name for name in files if f"`{name}`" not in text]


def test_decision_244_names_every_file_the_parallel_lane_harness_repair_touched():
    """The commit split is cut along a list, so a file the list forgets lands in the other commit.

    Decision 244 partitions this worktree's diff in two: a `chore(e2e):` commit for the harness
    the lane ran on and `feat(M4.13):` for the milestone, with one named frontend exception ruled
    INSIDE. It enumerated six files under `e2e/` and `ops/`, and three more carry the same repair:
    `test_harness_contracts.py`, whose guards READ `e2e/env.mjs` and would leave the chore commit
    red on its own if they stayed behind, and `test_backup.py` with `test_restore_drill.py`, which
    pick the postgres:16 container by the port TEST_DATABASE_URL names. Under the literal list
    those three ship inside `feat(M4.13):` -- with no plan step, no coverage row and no decision
    behind them, over two files that hold M0's and M4.7's backup and restore exit criteria, whose
    failure mode when they break is a SILENT SKIP that a summary line reads as a pass.
    [M4.13 review cycle 2: M413-C2-DIM7-01; decision 244]
    """
    missing = _unnamed_in(_decision_section(244), _lane_files())
    assert not missing, (
        "decision 244 enumerates the harness lane and these files carry it without being named, "
        "so the commit split puts them in feat(M4.13):\n  " + "\n  ".join(missing)
    )


def test_the_lane_guard_sees_a_file_the_register_does_not_name():
    """docs/TESTING.md: "a guard that cannot fail reads as coverage while providing none."

    The synthetic violation is the state that shipped: a list naming the e2e half and neither
    backend file. The second case is the direction that keeps the guard from reading as coverage
    -- a complete list reports nothing.
    """
    lane = ["backend/tests/test_backup.py", "e2e/helpers.js"]
    six = "the harness the lane ran on -- `e2e/helpers.js`, `e2e/run.mjs`"
    assert _unnamed_in(six, lane) == ["backend/tests/test_backup.py"]
    assert _unnamed_in(six + " and `backend/tests/test_backup.py`", lane) == []


def test_the_lane_sweep_reads_a_citation_apart_from_the_repair(tmp_path):
    """The narrowing, held in both directions so it cannot quietly become an exemption.

    A file resolving a per-checkout value IS in the lane however the marker is spelled, and a file
    whose only mention of it is a `#` line is not -- otherwise every future comment citing the
    lane's lesson would owe decision 244 a name it cannot truthfully give. The third case is the
    one that matters most: a comment does not launder the repair when the code is there too.
    """
    carrier = tmp_path / "test_carrier.py"
    carrier.write_text("PATH = 'e2e/env.mjs'\n", encoding="utf-8")
    assert _carries_the_lane(carrier)

    citer = tmp_path / "test_citer.py"
    citer.write_text("X = 1  # the lesson `env.mjs` taught, one value over\n", encoding="utf-8")
    assert not _carries_the_lane(citer)

    both = tmp_path / "test_both.py"
    both.write_text("# env.mjs\ndef _inside_the_container():\n    return True\n", encoding="utf-8")
    assert _carries_the_lane(both)


def _tense_under_the_clamp(drops: int, k_fitted: int, k_to: int) -> int:
    """Check 5's control: drops landing more than one tier from anything the clamp can render.

    The script measures the RESCALE and prints it; the clamp is what that number is compared
    against, and nothing computes it -- so this is the only place it exists as arithmetic rather
    than as a remembered figure inside a docstring that says "Measured". Under the bare clamp a
    K-level history keeps its raw indices, so it reaches 0..k_fitted-1 and nothing above.
    """
    stream = [round(i * (k_to - 1) / (drops - 1)) for i in range(drops)]
    return sum(
        1 for level in stream if min(abs(level - reached) for reached in range(k_fitted)) > 1
    )


def test_the_m413_scripts_clamp_control_is_the_number_its_own_constants_give():
    """A figure published as measured that the instrument producing it cannot produce.

    Check 5's docstring and the ledger's M4.13 paragraph both carry the clamp's tension rate as
    the control the rescale's 0/60 is read against. Recomputed from the script's own `DROPS`,
    `K_FITTED` and `K_GROWN` it is 19/60, not the 20/60 that shipped -- the signature of a
    hand-run probe over `range(1, DROPS + 1)` that was never re-derived from the stream the
    script builds. Decision 246 makes the ledger's paragraph the record of what the script
    measured, so a figure in it that nobody can reproduce leaves the next agent restating a
    number it cannot derive or reporting a discrepancy that is not one. Both publications are
    held here, because correcting one of the two is how they came apart.
    [M4.13 review cycle 2: M413-C2-DIM5-05]
    """
    source = M413_SCRIPT.read_text(encoding="utf-8")
    # The two expressions the control models, asserted present rather than lifted: a second
    # spelling of either is what `_meaning`'s docstring refuses, so this fails loudly if the
    # stream or the predicate moves instead of quietly measuring a simulation nobody runs.
    assert "drops = [round(i * (k_to - 1) / (DROPS - 1)) for i in range(DROPS)]" in source
    assert "if min(abs(level - reached) for reached in reachable) > 1" in source
    drops = int(re.search(r"^DROPS = (\d+)$", source, re.M).group(1))
    k_fitted, k_grown = (
        int(v)
        for v in re.search(
            r"^K_FITTED, K_GROWN, K_SHRUNK = (\d+), (\d+), \d+$", source, re.M
        ).groups()
    )
    tense = _tense_under_the_clamp(drops, k_fitted, k_grown)
    # The script's own formatting, so the figures held here are the ones a run would print.
    published = f"{tense}/{drops} ({100 * tense / drops:.0f}%)"
    assert f"the clamp reports {published}" in source, (
        "ops/m413_exit_criterion.py's check 5 publishes a clamp control its own constants do "
        f"not give: DROPS = {drops}, K {k_fitted} -> {k_grown} is {published}"
    )
    ledger = TESTING_LEDGER.read_text(encoding="utf-8")
    assert f"the clamp fails at {100 * tense / drops:.0f}%" in ledger, (
        "docs/TESTING.md's M4.13 measurement paragraph publishes a clamp control the script's "
        f"own constants do not give: it is {published}"
    )


def test_the_clamp_control_arithmetic_moves_when_the_simulation_does():
    """docs/TESTING.md: "a guard that cannot fail reads as coverage while providing none."

    A control that returns one number whatever it is asked is not a control. Three streams: the
    one check 5 runs, a history that already spans the new set, and the shrink direction, where
    nothing the drop stream can produce is out of the clamp's reach.
    """
    assert _tense_under_the_clamp(60, 7, 12) == 19
    assert _tense_under_the_clamp(60, 12, 12) == 0
    assert _tense_under_the_clamp(60, 7, 4) == 0


# --- M4.14 review cycle 3: this lane's own decision range, published in two documents ---------
#
# The pair below holds one fact -- which decision numbers this lane has spent -- because the two
# documents that state it disagreed and nothing in the tree re-derived either. They hold sentences
# rather than symbols for the reason the M4.13 block above gives: the register and
# `docs/TESTING.md` are what a later milestone reads BEFORE the code, and a number handed out
# twice is two normative rules under one heading, over source files that already cite the first.


# The register spells its own counts. Both spellings are read so the guard below rules on the
# count rather than on how a preamble chose to write it.
_COUNT_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen", "twenty",
)


# --- M4.15: the weights the stylesheet declares are the weights the repository ships -------

FONTS_DIR = REPO / "frontend" / "static" / "fonts"
FONTS_CSS = FONTS_DIR / "fonts.css"
FETCH_FONTS = REPO / "ops" / "fetch-fonts.py"

_FONT_FACE = re.compile(r"@font-face\s*\{(.*?)\}", re.S)
_FONT_FILE = re.compile(r"([a-z-]+?)-(\d{3})-(latin|latin-ext)\.woff2")


def _fetch_fonts():
    """Import `ops/fetch-fonts.py`, whose name is not an identifier, under one that is."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("fetch_fonts", FETCH_FONTS)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution, the way `_exit_script` above does it and for the same reason.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_declared_font_face_ships_its_own_file():
    """§6.8 gives the app two faces, and a face the repository cannot serve is a face that lies.

    Self-hosting is the constraint, not a preference. §6's preamble makes this an installable PWA
    a household opens over the LAN and over Tailscale, and `app.html:13-15` says the same thing
    over its own `<link>` -- so a weight that cannot be fetched at *runtime* cannot be declared at
    *build time*. The rule, therefore: every `src` resolves to a file in the repository, and every
    declared weight is one that file can really render -- its own, or an instance of its `wght`
    axis if the file is variable.

    M4.15's finding 12 read the three heavier faces pointing at a file named `-400-` and concluded
    the app had never rendered bold. Measured here, that is not what those files are: all four
    carry `fvar`, `gvar`, `avar` and `STAT`, Space Grotesk's axis is 300-700 and JetBrains Mono's
    400-800, so the 500 and 700 faces are real instances of the axis. Which is exactly why this
    guards the rule and not a file list -- both repairs the finding proposed would have made the
    app worse. Deleting the faces hands bold back to the browser's synthesiser; copying one file
    under three names makes `service-worker.js`'s all-or-nothing `addAll` push the same bytes to
    the phone three times before the shell will open offline.

    The axis *bound* is the one clause this cannot reach: `fvar` is inside the brotli stream and
    the repo has no brotli. `ops/fetch-fonts.py` checks it over the wire instead, where Google's
    css2 endpoint answers a weight outside the axis with HTTP 400. [M4.15 finding 12, decision 268]
    """
    css = FONTS_CSS.read_text(encoding="utf-8")
    module = _fetch_fonts()

    faces = []
    for block in _FONT_FACE.findall(css):
        weight = re.search(r"font-weight:\s*(\d+)", block)
        src = re.search(r"src:\s*url\(/fonts/([^)]+)\)", block)
        assert weight and src, f"a @font-face in fonts.css declares no weight or no src: {block!r}"
        faces.append((int(weight.group(1)), src.group(1)))
    assert faces, "frontend/static/fonts/fonts.css declares no @font-face at all"

    referenced = set()
    for weight, name in faces:
        named = _FONT_FILE.fullmatch(name)
        assert named, (
            f"/fonts/{name} does not follow the <family>-<weight>-<subset>.woff2 convention the "
            f"committed files use and ops/fetch-fonts.py writes"
        )
        path = FONTS_DIR / name
        assert path.is_file(), (
            f"fonts.css declares font-weight {weight} against /fonts/{name}, which is not in the "
            f"repository: the app self-hosts, so nothing fetches it at runtime"
        )
        data = path.read_bytes()
        assert data[:4] == b"wOF2", f"/fonts/{name} is not a woff2 file"
        referenced.add(name)
        if int(named.group(2)) == weight:
            continue
        # A file serving a weight that is not the one in its name is only honest if it is
        # variable: the woff2 table directory is in the clear, so `fvar` is readable without
        # decompressing anything.
        assert "fvar" in module.woff2_tables(data), (
            f"fonts.css declares font-weight {weight} against /fonts/{name}, a static font whose "
            f"own weight is {named.group(2)}: a declared face suppresses synthesis, so that text "
            f"renders at {named.group(2)} and never bold"
        )

    orphans = sorted(p.name for p in FONTS_DIR.glob("*.woff2") if p.name not in referenced)
    assert not orphans, (
        f"frontend/static/fonts ships {orphans}, which no @font-face names: service-worker.js "
        f"precaches the directory with an all-or-nothing addAll, so an unused subset is bytes "
        f"every phone downloads before the shell will open offline"
    )


def test_the_font_stylesheet_is_what_its_generator_emits():
    """`fonts.css:4` has named a generator since M0 that did not exist until M4.15.

    A generated file nobody can regenerate is a hand-edited file with a misleading header, and
    this one wore the evidence: every block carried a blank line where the `/* latin */` comment
    it was pasted from had been deleted. `ops/fetch-fonts.py` is that script, and `render_css()`
    is a pure function of the families it declares and the files on disk -- no network, because
    which weights share a file is a fact about the files -- so the committed stylesheet can be
    held to it on every run. [M4.15 finding 12]
    """
    assert FETCH_FONTS.is_file(), (
        "frontend/static/fonts/fonts.css tells its reader to regenerate with "
        "ops/fetch-fonts.py, which does not exist"
    )
    module = _fetch_fonts()
    assert module.render_css() == FONTS_CSS.read_text(encoding="utf-8"), (
        "frontend/static/fonts/fonts.css is not what ops/fetch-fonts.py renders: either the "
        "stylesheet was hand-edited, or the script changed and nobody regenerated it"
    )


def test_decision_268_records_the_run_that_falsified_its_premise():
    """The record, not the code, and the one document that can overrule the code.

    Decision 268 ruled "write `ops/fetch-fonts.py` and run it, so the four missing subsets land
    beside the four that are there", and stated the guard's rule as a filename weight EQUAL to its
    rule's `font-weight`, with no face declared for a weight the repository does not ship. The run
    it authorised falsified both halves: the css2 endpoint serves one variable woff2 per (family,
    subset), all four committed files carry `fvar`, and the guard two tests above permits the
    mismatch exactly where the axis is. Entries 162 onward are normative from the day they are
    taken (decision 177), so a reader following `[M4.15 finding 12, decision 268]` out of that
    guard's docstring meets a sentence saying the shipped tree is non-compliant -- and the obvious
    repair from that reading, deleting the heavier faces or committing six files, is the repair
    this milestone measured and rejected.

    Conditional on the code, the way `test_the_logout_row_states_the_branch_on_which_it_holds`
    is: while `fonts.css` declares a face against a file that does not carry that weight in its
    name, decision 268 has to say so. Delete the heavier faces and this goes quiet, because then
    the decision's own sentence is true again.
    [decision 177; decision 268; row `platform-shipped-font-weights-are-real`;
     review cycle 2: M415-C2-DEC-01]
    """
    mismatched = []
    for block in _FONT_FACE.findall(FONTS_CSS.read_text(encoding="utf-8")):
        weight = re.search(r"font-weight:\s*(\d+)", block)
        src = re.search(r"src:\s*url\(/fonts/([^)]+)\)", block)
        named = _FONT_FILE.fullmatch(src.group(1)) if weight and src else None
        if named and int(named.group(2)) != int(weight.group(1)):
            mismatched.append(f"{src.group(1)} declared at {weight.group(1)}")
    if not mismatched:
        return

    _, marker, amendment = _decision_section(268).partition("**Amended")
    assert marker, (
        "fonts.css declares " + ", ".join(mismatched) + ", which decision 268 says is the state "
        "the repair exists to end -- and the register carries no amendment saying otherwise. The "
        "run that decision authorised proved its premise false: the faces are real instances of a "
        "wght axis. Add the amendment paragraph in the shape decisions 272, 275, 276 and 281 use, "
        "or delete the faces and make the decision's own sentence true again."
    )
    assert re.search(r"(?i)wght|fvar|variable", amendment), (
        "decision 268's amendment does not name the axis, which is the whole of why the shipped "
        "faces are honest. A paragraph that discloses a correction without stating it leaves the "
        "superseded rule as the only reading a reader can act on."
    )


# --- M4.15: the frontend type check is a gate, not a line in a command list ---------------

# `.github/workflows/ci.yml` read as TEXT, like the compose files above and for the reason
# `test_harness_contracts.py` argues at length: a reader of the instrument has to run wherever
# the suite runs, including on the partial virtualenv this file's own escape hatches exist for,
# and a check that needs a parser installed before it can speak is one more thing that stops
# speaking without saying so.
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
TESTING_LEDGER = REPO / "docs" / "TESTING.md"

FRONTEND_TYPE_CHECK = "npm --prefix frontend run check"

# The frontend job as M4.8 left it -- install, unit tests, build, and no type check. Kept here so
# the guard below can be shown refusing the source this milestone actually found, rather than
# being a reader nobody has watched say no to anything.
_FRONTEND_JOB_BEFORE_M415 = """  frontend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm --prefix frontend ci
      - run: npm --prefix frontend test
      - run: npm --prefix frontend run build

  e2e:
"""


def _job_block(workflow: str, name: str) -> str:
    """One job's lines, from its key under `jobs:` to the next key at the same indentation."""
    start = re.search(rf"^  {re.escape(name)}:[ \t]*$", workflow, re.M)
    if start is None:
        return ""
    rest = workflow[start.end() :]
    end = re.search(r"^  \S", rest, re.M)
    return rest[: end.start()] if end else rest


def _runs_and_can_fail(job: str, command: str) -> bool:
    """True when a step in `job` runs `command` in a way that can still fail the job."""
    if "continue-on-error" in job:
        return False
    for line in job.splitlines():
        step = line.strip()
        if not step.startswith(("- run:", "run:")) or command not in step:
            continue
        return "||" not in step
    return False


def test_ci_runs_the_frontend_type_check():
    """§12's gates are read off CI, and svelte-check was the one layer no job ran.

    `npm --prefix frontend run check` is in CLAUDE.md's command list and among the layers
    docs/TESTING.md publishes, and it exited 1 on a clean tree -- 25 errors on one day and 27 on
    another -- for this project's whole life. A documented command that always fails is worse
    than a missing one: it teaches every reader to skip the only static signal this frontend has,
    and the defects it does catch (a renamed envelope key, a dropped export, a rune and a local
    colliding) sit unread in that noise. So the command becomes a job step, on the branch where
    the count first reached its floor.

    Two things this is deliberately strict about. The step must be able to FAIL the job:
    `ruff format --check . || true` two jobs up is this same workflow's own precedent for a step
    that reports instead of gating, and a row discharged by that shape asserts nothing. And the
    job is located by name rather than by a substring over the whole file, because the command
    quoted in a comment is not a gate.

    What this cannot assert is the count. Decision 273 lands the step with one error still
    standing at `src/routes/admin/data/+page.svelte:14`, which M4.14 owns in the sibling worktree
    of this wave and which M4.15 must not open, so the frontend job is RED on this branch until
    the two merge. That is the honest state, and it is not hidden by narrowing the check, by
    excluding a file, or by `|| true`.
    [M4.15 finding 26, decision 273, row `platform-the-frontend-type-check-runs-in-ci`]
    """
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert _runs_and_can_fail(_job_block(workflow, "frontend"), FRONTEND_TYPE_CHECK), (
        "ci.yml's frontend job installs, tests and builds but does not run "
        f"`{FRONTEND_TYPE_CHECK}`: svelte-check is the only static signal the frontend has, and "
        "a command no job runs is a command that exits 1 for a year with nobody reading it"
    )

    # The guard has to be able to refuse. Two sources it must not read as covered: the job as
    # M4.8 left it, and the same step with its exit code thrown away.
    before_m415 = _job_block(_FRONTEND_JOB_BEFORE_M415, "frontend")
    assert not _runs_and_can_fail(before_m415, FRONTEND_TYPE_CHECK)
    discarded_exit_code = "      - run: " + FRONTEND_TYPE_CHECK + " || true"
    assert not _runs_and_can_fail(discarded_exit_code, FRONTEND_TYPE_CHECK)


# "1 error and 1 warning across 275 files", which is what `npm --prefix frontend run check` last
# printed on this branch. Two documents publish it -- the map's comment on the CI row and the
# ledger's paragraph on exit clause 10 -- and one run produced one number, so either they agree or
# one of them was typed rather than read.
_SVELTE_CHECK_RESULT = re.compile(r"(\d+) errors? and (\d+) warnings? across (\d+) files")


def test_the_two_records_publish_one_svelte_check_measurement():
    """Decision 184, applied to the one figure clause 10 of the exit criterion is argued from.

    The figure is load-bearing rather than decorative: it is the evidence that EXACTLY one error
    survives and that the surviving one is the sibling lane's, which is the whole basis for landing
    the CI step red on this branch (decision 273). The map said 274 files and the ledger 275, for
    one run of one command on one tree, and nothing read either -- so an auditor reconciling clause
    10 meets two numbers, cannot tell which run produced which, and either re-runs `svelte-check`
    to break the tie or restates a figure they did not derive. Both are what decision 184 exists to
    prevent.

    A cross-check and not a re-derivation, deliberately. `svelte-check` is a node command over a
    tree this suite does not build, and a pytest guard that shelled out to it would make every
    backend run depend on `frontend/node_modules`; decision 184 owes that figure to a run, not to a
    guard. What a guard CAN hold is that the record publishes one of them.
    [decision 184; decision 273; row `platform-the-frontend-type-check-runs-in-ci`;
     review cycle 2: M415-C2-COV-02]
    """
    published = []
    for path in (COVERAGE, TESTING_LEDGER):
        for found in _SVELTE_CHECK_RESULT.findall(path.read_text(encoding="utf-8")):
            published.append((path.relative_to(REPO).as_posix(), found))
    assert len(published) >= 2, (
        f"only {len(published)} document publishes the svelte-check result, so this guard holds "
        "nothing. Both the coverage map's comment on the frontend type-check row and the ledger's "
        "clause 10 paragraph state it, and clause 10 is argued from the fact that the single "
        "surviving error is the sibling lane's."
    )
    readings = {found for _, found in published}
    assert len(readings) == 1, (
        "the record publishes more than one svelte-check result for one run of one command: "
        + "; ".join(
            f"{where} says {errors} error(s) and {warnings} warning(s) across {files} files"
            for where, (errors, warnings, files) in published
        )
        + ". One run produced one number. Restate the stale one from the run rather than picking "
        "the reading that looks right -- a count nobody re-derived is decision 184's defect."
    )


# --- M4.15: the register the quiet reasons are set in, and the one control exempt from 48 -------

# §6.8's two voices are two FACES, not two sizes: `.why` is the prose register in Space Grotesk
# and `.data` is the data voice in JetBrains Mono, which §6.8 gives to "every model number, ID and
# data annotation" and to nothing else. design.css says so in its own header and decision 275
# moved `.why` to the display face to mean it -- but a rule decided once in design.css is decided
# nowhere if a scoped block can quietly take it back. Scoped CSS wins silently: no conflict, no
# warning, nothing but a reader ever notices, which is the failure mode this whole file exists to
# read. So the register is held at the source, over every component, and not in design.css alone.

# design.css's ink ramp, loudest first. The order is the rule: a why-line may be made louder by
# the surface it sits on and may not be made quieter, so the ladder is what "quieter" means.
INK_LADDER = ("--ink", "--ink-2", "--ink-3", "--ink-4", "--ink-5")

# Every OPAQUE ground text is drawn on. Opaque is what makes an alpha ink measurable at all: the
# drawn colour is the ink composited over what is behind it, and a surface carrying an alpha of
# its own is not a colour until something is behind it either.
SURFACE_TOKENS = ("--ground", "--ground-raised", "--card", "--card-raised")

# The fifth surface class, and the reason the four above are not the whole list. `--ember-wash` is
# a 10% tint laid over one of them at sixteen sites -- the sweep card's reveal, the re-auth banner,
# the no-bundle badge -- and it carries `--ink-*` text on every one of them. "An alpha over an
# alpha has no single answer" was the premise for leaving it out, and it is false for a wash over a
# KNOWN ground: there are exactly four answers, all computable from design.css alone. It mattered,
# because at 0.50 `--ink-5` measured 4.43:1 on the wash over `--card` -- the composite
# `RateSweepCard.svelte`'s `.next` hint actually ships on -- while measuring 4.58 on the bare
# `--card` this guard was reading. design.css:31 claims the floor holds "against each ground it
# lands on"; a guard reading only the opaque four is narrower than the sentence it enforces, and it
# would have stayed green for every future ink placed on the tint.
# [§6.8; decision 275 as amended; M4.15 review cycle 1: m415-rev1-css-01]
WASH_TOKEN = "--ember-wash"

# The sixth surface class, and the reason a fifth `SURFACE_TOKENS` entry would have been the wrong
# repair for it. `--identity` is an alpha too -- a 16% bone disc on the account chip -- so like the
# wash it is not a colour until something is behind it, and the two things behind it are known:
# `+layout.svelte`'s header at `--ground-raised` and `AccountChip`'s menu at `--card-raised`.
#
# What makes it a different KIND of ground from the wash is what lands on it, and that is why it is
# a PAIR here rather than a fifth ground in the cross product above. The wash is a tint under COPY:
# any rung of the ladder may be set on it, so every rung is measured against it. The disc carries
# one glyph in one colour -- `AccountChip.svelte` is `background: var(--identity); color: var(--ink)`
# -- and running it through the ladder would measure `--ink-5` at 3.85:1 on a 22px disc nothing has
# ever drawn `--ink-5` on, reddening the build over a composite the app does not contain. A guard
# asserting something false is worse than the gap it closed, because the reader it sends to
# design.css has nothing there to fix.
#
# It is held at all because the token is this milestone's own and nothing held it. design.css
# publishes 10.58 and 9.56 for it, and the guard that reads those two sentences asks that the figure
# MATCH the measurement, not that the measurement clear the floor -- so the one move the record
# leaves open is the unguarded one. Take the alpha to 0.55, restate the two figures as 2.96 and
# 2.82, and all three contrast guards stay green with the household's own initial at 2.82:1 on every
# signed-in screen: 2.82 is the exact figure design.css's header cites as the unreadable value
# decision 275 was minted to raise.
# [§6.8; decision 276; design.css:31 "each ground it lands on";
#  M4.15 review cycle 3: M415-C3-CSS-02]
TINTED_GROUNDS = (
    ("--ink", "--identity", "--ground-raised", "the account chip's disc, on the header's ground"),
    ("--ink", "--identity", "--card-raised", "the same disc in the menu, the worse of the two"),
)

# WCAG 2.x AA for body text. The 3:1 large-text relaxation is not available to either voice here:
# the quiet reasons are 13px and the data voice 10-11px, both well under the 18.66px threshold.
CONTRAST_FLOOR = 4.5

# The colours a component may give a why-line. Tonight sets its own at --ink-3 because the copy
# there IS the surface's content rather than a footnote under it, and that is allowed -- louder is
# a surface's business. --ink-5 is the rung below the register and is where ShelfRow's caption
# was: the darkest text in the app, 2.22:1 before decision 275 raised the token under it. A raw
# colour is refused whatever its value, because a hex here is a fourth voice nobody declared. So
# is an `opacity` under 1 on a rule that reaches a why-line: --ink-4 measures 5.35:1 on --ground,
# so a dimmed ancestor takes the register under the floor at about 0.90 -- 0.9028 over
# --card-raised, the worst of the eight -- AFTER the token was chosen, which is exactly where
# `test_every_text_ink_token_clears_the_contrast_floor` stops looking: it measures tokens, and an
# opacity composites what they drew. Any dimming is refused rather than composited, because a
# scoped rule does not say which ancestor stack it will be drawn under. The figure published here
# was 4.60, which is --ink-5 at the alpha decision 275's amendment superseded -- the rule survived
# the arithmetic, but a reader re-tuning this ramp would have read a margin of 0.1 where there is
# 0.85. [review cycle 1: reach-01; review cycle 2: M415-C2-CSS-06]
#
# The one entry that is not a rung of that ladder is `--ember-lift`, admitted at review cycle 2
# with the matching that made the rule reachable in the first place. §6.8's derivatives are the
# accent QUOTED rather than spent -- `--ember-lift` IS error copy, which is why `_ACCENT` refuses
# it and why the three derivative tokens exist at all -- and `rate/+page.svelte` ships
# `<p class="banner error why" role="alert">`, so on that surface the error and the reason are one
# element. The guard already licenses that element's frame on purpose: `background-color` and
# `border-color` are read past three rules below so that "a why-line inside a tinted callout keeps
# its frame", and licensing the frame while refusing the copy inside it is half a rule. The
# direction that decides is untouched, which is the whole test of the widening: over the wash the
# error copy is BRIGHTER than --ink-4, not dimmer, and nothing admitted here is quieter than the
# register. The alternative was recolouring the shipped banner to an ink rung, which takes §6.8's
# one word for "this failed" off the one sentence on the screen that says so.
# [§6.8; decision 275 as amended; review cycle 2: why-c2-01]
WHY_REGISTER_COLOURS = (
    frozenset(f"var({token})" for token in INK_LADDER[:-1]) | {"var(--ember-lift)"}
)

SETUP_PAGE = FRONTEND / "routes" / "setup" / "+page.svelte"


def _channel(value: float) -> float:
    """One 8-bit sRGB channel, linearised -- WCAG 2.x's own formula, constants included."""
    v = value / 255
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = (_channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(one: tuple[float, ...], two: tuple[float, ...]) -> float:
    a, b = _luminance(one), _luminance(two)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def _composite(rgb: tuple[float, ...], alpha: float, ground: tuple[float, ...]) -> tuple[float, ...]:
    """What an alpha colour actually draws as, once the thing behind it is known.

    The one operation this whole section turns on, named because it is now applied twice: once to
    put an ink on its ground, and once more to put a tinted SURFACE on its own ground first.
    """
    return tuple(rgb[i] * alpha + ground[i] * (1 - alpha) for i in range(3))


def _token_colour(css: str, token: str) -> tuple[tuple[int, int, int], float]:
    """A design.css colour token as (rgb, alpha).

    Both spellings the file uses: `#ece9e4` for the opaque ones and `rgba(236, 233, 228, 0.55)`
    for the ramp, which is one colour at five volumes rather than five colours.
    """
    opaque = re.search(rf"{token}:\s*(#[0-9a-fA-F]{{6}})\s*;", css)
    if opaque:
        return _hex_rgb(opaque.group(1)), 1.0
    ramp = re.search(rf"{token}:\s*rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*([0-9.]+)\s*\)\s*;", css)
    assert ramp, f"design.css declares no {token}"
    r, g, b, alpha = ramp.groups()
    return (int(r), int(g), int(b)), float(alpha)


def test_every_text_ink_token_clears_the_contrast_floor():
    """§6.8's copy register is load-bearing, so it has to be readable.

    Every `--ink-*` is drawn as text -- there is no decorative one -- and two of them were not
    readable: --ink-4 at 0.35 measured 2.82:1 against --ground and 2.87:1 against --card, --ink-5
    at 0.28 measured 2.22 / 2.28, under even the 3:1 floor a large-text relaxation would have
    given them, on the quiet reasons §6.8 makes the reason a household trusts a recommendation.

    Measured rather than declared: the ramp is one bone colour at five alphas over eight grounds,
    so the drawn colour is a composite and no token's own value says what it reads at. That is
    also why 0.52 and not a rounder number -- 0.51 measures 4.49 on the tinted surface and fails
    this guard, and finding fe-06's proposed 0.45 measures 3.93, which would have contradicted the
    4.5:1 the same finding asked for.

    Eight grounds and not four: `--ember-wash` is a surface too, and the reason it was left out --
    that an alpha over an alpha has no single answer -- is false for a tint over a KNOWN ground.
    At 0.50 the bottom rung cleared every opaque ground this guard read and measured 4.43:1 on the
    wash over `--card`, which is the composite the sweep card's reveal hint ships on.

    And eight grounds plus two PAIRS, which is the same widening finished rather than a second one.
    The eight are cross-producted with the whole ladder because they are surfaces any rung may land
    on; `--identity` is a 22px disc carrying one glyph in `--ink`, so the ground and the ink that
    lands on it are one claim and `TINTED_GROUNDS` states them together. Putting it in the cross
    product instead would have measured four rungs the app never draws there and reddened over
    them, which is why the gap this closes survived a review cycle looking straight at it.
    [§6.8; decisions 275 as amended, 276; row `platform-quiet-reasons-are-prose-and-legible`;
     review cycle 3: M415-C3-CSS-02]
    """
    css = _css()
    grounds = {token: _token_colour(css, token)[0] for token in SURFACE_TOKENS}
    wash, wash_alpha = _token_colour(css, WASH_TOKEN)
    for token in SURFACE_TOKENS:
        grounds[f"{WASH_TOKEN} on {token}"] = _composite(wash, wash_alpha, grounds[token])
    failures = []
    for token in INK_LADDER:
        rgb, alpha = _token_colour(css, token)
        for name, ground in grounds.items():
            drawn = _composite(rgb, alpha, ground)
            ratio = _contrast(drawn, ground)
            if ratio < CONTRAST_FLOOR:
                failures.append(f"{token} on {name}: {ratio:.2f}:1")
    for ink, tint, ground, where in TINTED_GROUNDS:
        rgb, alpha = _token_colour(css, tint)
        assert alpha < 1.0, f"{tint} is opaque, so it is a SURFACE_TOKENS entry and not a pair"
        behind = _composite(rgb, alpha, _token_colour(css, ground)[0])
        rgb, alpha = _token_colour(css, ink)
        ratio = _contrast(_composite(rgb, alpha, behind), behind)
        if ratio < CONTRAST_FLOOR:
            failures.append(f"{ink} on {tint} on {ground}: {ratio:.2f}:1 -- {where}")
    assert not failures, (
        f"every --ink-* is text and clears {CONTRAST_FLOOR}:1 on every ground it lands on:\n  "
        + "\n  ".join(failures)
        + "\n\nRaise the alpha in design.css. A token that fails here is unreadable on the phone"
        " §6's preamble makes the primary form factor, whatever it looks like on a desktop panel."
    )


def test_the_contrast_guard_composites_a_tinted_surface_before_measuring():
    """The step above that the guard passed for a whole milestone without.

    Written as a claim about the ARITHMETIC rather than about today's token values, so it keeps
    saying something once `--ink-5` moves again: a tint over a ground is a lighter ground, and a
    ladder measured against the ground under the tint reads a number nothing is ever drawn at.
    0.50 is the value this is shown with because 0.50 is the value that shipped -- it cleared
    every opaque ground and missed the floor on the composite, which is precisely the shape of
    hole a guard enumerating only opaque tokens cannot see.
    [§6.8; decision 275 as amended; M4.15 review cycle 1: m415-rev1-css-01]
    """
    css = _css()
    bone, _ = _token_colour(css, "--ink-5")
    wash, wash_alpha = _token_colour(css, WASH_TOKEN)
    assert wash_alpha < 1.0, "--ember-wash is opaque, so it is a SURFACE_TOKENS entry and not this"

    card = _token_colour(css, "--card-raised")[0]
    tinted = _composite(wash, wash_alpha, card)
    assert _luminance(tinted) > _luminance(card), (
        "the wash is meant to LIFT the surface it tints; if it ever darkens one, the opaque "
        "ground stops being the optimistic reading and this guard is measuring the wrong way"
    )

    shipped = 0.50
    assert _contrast(_composite(bone, shipped, card), card) >= CONTRAST_FLOOR
    assert _contrast(_composite(bone, shipped, tinted), tinted) < CONTRAST_FLOOR, (
        "the tinted composite no longer costs the bottom rung its floor, so this test proves "
        "nothing -- re-derive it against whatever surface is now the worst case"
    )


# Every contrast figure the record publishes about a token, in the one form it publishes them in:
# a colour, optionally over a second colour, measured against a ground. Read rather than trusted,
# because this milestone's contribution IS a set of measured numbers and one of them was a token's
# superseded value -- `--ink-4` was published at 4.60:1, which is `--ink-5` at the alpha decision
# 275's amendment replaced, sixteen lines under the right figure in the same comment block.
_PUBLISHED_RATIO = re.compile(
    r"(--[a-z0-9-]+)(?: on (--[a-z0-9-]+))? measures ([0-9]+\.[0-9]+):1 on (--[a-z0-9-]+)"
)

# How many figures stand in that form today: two in the map and this file's twin of the same
# sentence, two in design.css's `--identity` comment. The floor is here because the one way to
# satisfy this guard without obeying it is to rephrase a figure out of the form rather than
# correct it -- the same dodge `test_the_why_register_guard_leaves_innocent_files_alone` exists
# for one guard over.
_PUBLISHED_RATIO_FLOOR = 4


def test_every_contrast_figure_the_record_publishes_is_the_one_it_measures():
    """A measurement in a comment is a measurement, and decision 184 governs it.

    The two guards above hold the TOKENS; nothing held the sentences written about them, and the
    record drifted in the direction that costs a reader most: `--ink-4` was published at 4.60:1 on
    --ground in the map and in this file, in the argument for refusing any `opacity` on a rule
    that reaches a why-line. The rule survived the arithmetic -- the token really is one dimming
    away from the floor -- but the margin is 0.85 and not 0.1, and the figure quoted was the value
    review cycle 1 superseded. design.css's `--identity` comment had the same shape of error twice:
    the header's ground is --ground-raised and the figure published was the one on --ground, and
    "no worse than 9.6" is 9.56, which rounds the wrong way.

    So the figures are re-derived from design.css rather than compared with a copy of themselves,
    the way the register's range guard once re-derived a range from the register (retired by
    decision 460 with the other counts of the tree). Rounding is read off the figure's own
    precision: a record that publishes two decimals is held to two, and one that publishes one is
    held to one.
    [§6.8; decisions 275 as amended, 276; decision 184;
     row `platform-quiet-reasons-are-prose-and-legible`; review cycle 2: M415-C2-CSS-06]
    """
    css = _css()
    wrong, found = [], 0
    for path in (DESIGN, COVERAGE, Path(__file__).resolve()):
        text = path.read_text(encoding="utf-8")
        for fg, mid, published, ground in _PUBLISHED_RATIO.findall(text):
            found += 1
            base, base_alpha = _token_colour(css, ground)
            assert base_alpha == 1.0, (
                f"{path.name} measures {fg} against {ground}, which is not opaque: an alpha over "
                "an alpha is not a colour until something is behind it, so name the composite"
            )
            behind = base
            if mid:
                rgb, alpha = _token_colour(css, mid)
                behind = _composite(rgb, alpha, behind)
            rgb, alpha = _token_colour(css, fg)
            measured = _contrast(_composite(rgb, alpha, behind), behind)
            places = len(published.partition(".")[2])
            if f"{measured:.{places}f}" != published:
                over = f"{fg} on {mid} " if mid else f"{fg} "
                wrong.append(
                    f"{path.relative_to(REPO).as_posix()}: publishes {over}at {published}:1 on "
                    f"{ground}, which measures {measured:.{places}f}:1"
                )
    assert not wrong, (
        "the record publishes a contrast figure that is not the one design.css draws:\n  "
        + "\n  ".join(wrong)
        + "\n\nRestate it from the measurement. A figure a later token value overtook is the "
        "defect decision 184 refuses from the other side, and this ramp is the one place in the "
        "app where a reader re-tuning a number has nothing but these sentences to go on."
    )
    assert found >= _PUBLISHED_RATIO_FLOOR, (
        f"the record publishes {found} contrast figures in the form this guard reads and "
        f"{_PUBLISHED_RATIO_FLOOR} stood here when it was written. A figure moved out of the form "
        "is a figure nothing re-derives; keep the form, or lower this floor in the same change "
        "that removes the sentence."
    )


# A compound's own punctuation, taken off before its class tokens are read. Both of these NARROW a
# selector -- `:not(.loud)` names a class the element does not carry and `[data-testid='w']` is a
# test hook -- so neither can put a rule outside the set of elements the compound's classes reach.
_PSEUDO_PART = re.compile(r"::?[a-z-]+(?:\([^()]*\))?")
_ATTR_PART = re.compile(r"\[[^\]]*\]")


def _reaches_a_why_line(part: str, reachable: set[str]) -> bool:
    """Whether one comma-separated selector part lands on a why-line in this file.

    The LAST compound and not the whole part -- the opposite trade from `_invented_paddings`, and
    right here for the opposite reason. That guard compares whole selectors because `.rooms li`
    styles a row INSIDE a card and matching `li` alone would call every list row a card; here the
    descendant IS the target. `.empty .why` and `.drained .why` are two live rules that reach a
    why-line and that a whole-selector comparison could not see, and `.wrap p` is how a component
    restyles the paragraph it wrapped. The over-match this buys is real and is measured rather
    than assumed: eight rules in the shipped tree are reached only by the widening, none of them
    touches the register, and `test_the_why_register_guard_leaves_innocent_files_alone` holds the
    side of the trade that costs.

    `:global(...)` is unwrapped first because Svelte's escape hatch is exactly how a component
    reaches a why-line it did not declare, and the wrapper is punctuation around a selector rather
    than part of one. It helps only where the same file also carries a why-element: `reachable` is
    built from this file's markup, so a component styling ANOTHER component's why-line is still
    outside what a per-file scan can see, and saying so is cheaper than implying otherwise.

    The under-match was neither measured nor stated, and the paragraph above accounting for the
    over-match alone read as a claim that there was not one. The last compound was compared as ONE
    string, so every ordinary way of NARROWING a selector fell outside `reachable` while staying
    inside the cascade: `.why.loud`, `.why:not(.loud)`, `.why:first-child` and `p[data-testid='w']`
    all reach a why-line the file carries and none of them matched. That is not hypothetical --
    `rate/+page.svelte` ships `<p class="banner error why" role="alert">` styled as `.banner.error`,
    so the guard's verdict on a rule in the tree was decided by the selector's punctuation and not
    by the property, and simplifying that rule to `.error` would have reddened a build without
    moving a pixel. So a compound is read as its tag and the SET of its classes, pseudo-classes and
    attribute selectors removed first because both narrow rather than widen.

    All of the compound's classes, never any of them, which is what keeps the trade the paragraph
    above measures: the Data tab's why-line is a `<td class="note why">`, so `td` is in reach, and
    an any-token reading would call every other cell in that sheet a why-line. Measured again on
    this tree: fifteen rules are reached only by this widening, fourteen of them borders, paddings
    and margins the guard does not read, and the fifteenth is the error banner above. What stays
    out is a compound whose classes no single why-line carries -- two why-lines in one file with a
    class each still admit `.a.b`, which is the over-match this direction costs and is the same
    direction the paragraph above already accepts.
    [§6.8; decision 275 as amended; review cycle 1: reach-01; review cycle 2: why-c2-01]
    """
    part = re.sub(r":global\(\s*([^()]*?)\s*\)", r"\1", part.strip())
    if part in reachable:
        return True
    # Attribute selectors are put aside before the combinator split and put back after it, because
    # `~` is both the general-sibling combinator and the `~=` of a class-list match: splitting the
    # raw string tore `[class~="why"]` into `[class` and `="why"]` and left the compound reader a
    # fragment. Two bugs in one line -- the split destroyed the selector and the strip below then
    # dropped what survived -- which is why the shape read as narrowing when it is exactly as wide
    # as `.why`. [review cycle 3: M415-C3-WHY-05]
    stash: list[str] = []
    part = _ATTR_PART.sub(lambda m: stash.append(m.group(0)) or f"\x00{len(stash) - 1}\x00", part)
    last = re.split(r"[\s>+~]+", part)[-1]
    last = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], last)
    if last in reachable:
        return True
    bare = _PSEUDO_PART.sub("", _ATTR_PART.sub("", last))
    tag = re.match(r"[A-Za-z][A-Za-z0-9]*", bare)
    classes = {"." + name for name in re.findall(r"\.([A-Za-z0-9_-]+)", bare)}
    # An attribute selector ON `class` is a class selector in the one spelling the strip above
    # takes off: `[class~="why"]` matches exactly what `.why` matches, so dropping it as a
    # NARROWING left a rule reaching the register with nothing left to compare and `bool(tag)`
    # answering False. Only `class` -- `[data-testid='w']` really does narrow and stays dropped.
    # [review cycle 3: M415-C3-WHY-05]
    classes |= {
        "." + token
        for attr in _ATTR_PART.findall(last)
        for value in re.findall(r"^\[\s*class\s*[~*^$|]?=\s*[\"']?([^\"'\]]*)", attr)
        for token in value.split()
    }
    if tag and tag.group(0) not in reachable:
        return False
    if classes:
        return classes <= reachable
    return bool(tag)


def _register_escapes(name: str, source: str) -> list[str]:
    """Every rule in one component that takes a why-line out of the register.

    Reached the way `_selectors_that_style` reaches a card: by what the markup actually carries,
    not by searching for `.why {`. RateClassBalance's warning is `<p class="warn why">` and styled
    as `.warn`, ShelfRow's was `<p class="why caption">` styled as `.caption` -- neither file
    contains the string this guard is about, and both were escapes.

    Three ways out and not one, because a register is a claim about what the copy READS as: the
    face, the colour, and the volume it is drawn at. The face is read through the shorthand as
    well as the longhand -- `font: 13px/1.4 var(--mono)` sets it in one word, and a guard that
    knew only `font-family` was one word from being walked past.

    A FILE THAT CARRIES NO WHY-LINE STILL GETS READ IF IT NAMES ONE GLOBALLY, which the early
    return below used to decide the other way. Both halves are deliberate. The return is right for
    every scoped shape: Svelte prunes a `.why` rule in a component with no why-element, so seeding
    the register there would manufacture escapes out of dead selectors. It is wrong for exactly the
    shape `_reaches_a_why_line` unwraps -- `:global(.why)` NAMES the register literally, needs no
    cross-file knowledge to resolve, and by definition is written in the one kind of file this
    guard then refused to open: a shell or layout styling an element it does not render.
    `RateBattleCard.svelte` already reaches into `PosterCard`'s `.poster` that way, so the idiom is
    the tree's, and one `:global(.why) { font-family: var(--mono) }` re-monospaces all 129 quiet
    reasons in the app -- the failure decision 275 exists to prevent -- from a file that passed.

    What stays out, stated rather than implied: a class computed at runtime,
    `<p class={c ? 'why' : 'note'}>`, is invisible to any markup scan short of evaluating the
    expression, and that is a boundary rather than a gap.
    [§6.8; decision 275 as amended; review cycle 2: why-c2-01; review cycle 3: M415-C3-WHY-05]
    """
    reachable = _selectors_that_style(source, "why")
    if not reachable and not re.search(r":global\([^()]*\.why(?![\w-])", _style_blocks(source)):
        return []
    reachable.add(".why")
    offenders = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", _style_blocks(source)):
        if not any(_reaches_a_why_line(part, reachable) for part in selector.split(",")):
            continue
        for prop, face in re.findall(r"(?<![a-z-])(font(?:-family)?)\s*:\s*([^;}]+)", body):
            offenders.append(f"{name}: {selector.strip()} {{ {prop}: {face.strip()} }}")
        # `(?<![a-z-])` so `background-color`, `border-color` and `accent-color` are not read as
        # the text colour: a why-line inside a tinted callout keeps its frame.
        for colour in re.findall(r"(?<![a-z-])color\s*:\s*([^;}]+)", body):
            if colour.strip() in WHY_REGISTER_COLOURS:
                continue
            offenders.append(f"{name}: {selector.strip()} {{ color: {colour.strip()} }}")
        for dim in re.findall(r"(?<![a-z-])opacity\s*:\s*([^;}]+)", body):
            if _dimmer_than_full(dim.strip()):
                offenders.append(f"{name}: {selector.strip()} {{ opacity: {dim.strip()} }}")
    return offenders


def _dimmer_than_full(value: str) -> bool:
    """Whether an `opacity` value draws at less than full strength, in either spelling.

    A value this cannot read -- a variable, a keyword, an animation's own -- is not called an
    escape: this guard's job is to catch the copy being dimmed, not to refuse what it cannot
    measure, and a false positive here would send a reader looking for a register bug in a fade.
    """
    number = value.rstrip("%").strip()
    try:
        level = float(number) / (100 if value.endswith("%") else 1)
    except ValueError:
        return False
    return level < 1


def test_no_component_re_monospaces_a_why_line():
    """The register is one thing, in one place, or it is not a register.

    §6.8 assigns JetBrains Mono to "every model number, ID and data annotation"; design.css's own
    header calls using it for anything else a design bug; and 129 `.why` elements across 27 files
    wore it anyway, half a pixel from the numbers they were explaining. Decision 275 moved the
    class to the display face, which fixes all 129 at once -- and leaves exactly the hole this
    guard fills, because two components had already overridden the class locally and a scoped rule
    outranks design.css silently.

    Colour is held on the same rule, in one direction. A surface may make its own why-line louder
    (Tonight's is --ink-3, because there the reason is the content) and may not make one quieter:
    ShelfRow's caption sat at --ink-5, the darkest text in the app. `--ember-lift` joins the ink
    ladder on that list at review cycle 2, because Rate's error banner IS its own why-line and the
    error colour is louder than the register rather than quieter -- the rule this holds.

    What it reads is a compound's tag and the SET of its classes from the same cycle on, not the
    compound as one string: every ordinary narrowing -- a second class, a negation, a position, a
    test hook -- left an escape inside the cascade and outside this guard, and the one shipped rule
    it had been missing is the banner above. [§6.8; decision 275 as amended; review cycle 2:
    why-c2-01; row `platform-quiet-reasons-are-prose-and-legible`]
    """
    offenders = []
    for path in sorted(FRONTEND.rglob("*.svelte")):
        offenders += _register_escapes(path.name, path.read_text(encoding="utf-8"))
    assert not offenders, (
        "the quiet-reason register is design.css's to set, and these take it back locally:\n  "
        + "\n  ".join(offenders)
        + "\n\n`.why` is var(--display) at 13px in var(--ink-4). A fragment that really is a model"
        " number wears `.data` inside the sentence, as design.css's header describes; the sentence"
        " around it does not change face, and no why-line is dimmer than the register itself."
    )


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        (
            "the mono face on a co-class",
            '<p class="warn why">x</p><style>.warn { font-family: var(--mono); }</style>',
            ["x.svelte: .warn { font-family: var(--mono) }"],
        ),
        (
            "quieter than the register",
            '<p class="why caption">x</p><style>.caption { color: var(--ink-5); }</style>',
            ["x.svelte: .caption { color: var(--ink-5) }"],
        ),
        (
            "a raw colour, whatever its value",
            '<p class="why">x</p><style>.why { color: #8a8a8a; }</style>',
            ["x.svelte: .why { color: #8a8a8a }"],
        ),
        (
            "styled by its tag rather than its class",
            '<p class="why">x</p><style>p { font-family: monospace; }</style>',
            ["x.svelte: p { font-family: monospace }"],
        ),
        # The two shapes that are not violations, which matter as much: a surface is allowed to
        # raise its own reasons, and a frame around one is a frame, not a face.
        (
            "louder is a surface's own business",
            '<p class="why">x</p><style>.why { color: var(--ink-3); }</style>',
            [],
        ),
        (
            "a tinted callout keeps its frame",
            '<p class="warn why">x</p><style>.warn { background-color: var(--ember-wash); }</style>',
            [],
        ),
        (
            "a rule that reaches no why-line",
            '<p class="note">x</p><style>.note { color: #8a8a8a; }</style>',
            [],
        ),
        # The four the whole-selector comparison could not see. Two of the first shape ship.
        (
            "reached through a descendant selector, which is how two live rules reach one",
            '<p class="why">x</p><style>.empty .why { color: var(--ink-5); }</style>',
            ["x.svelte: .empty .why { color: var(--ink-5) }"],
        ),
        (
            "reached through Svelte's own escape hatch",
            '<p class="why">x</p><style>:global(.why) { font-family: var(--mono); }</style>',
            ["x.svelte: :global(.why) { font-family: var(--mono) }"],
        ),
        # The same escape hatch from the file it is actually written in. A component that reaches
        # OUT with `:global` renders no why-line of its own by definition, and the guard used to
        # return on that -- so the one rule that re-monospaces all 129 quiet reasons at once was
        # written in the one kind of file nothing opened.
        (
            "the escape hatch from a file carrying no why-line of its own",
            "<p>x</p><style>:global(.why) { font-family: var(--mono); }</style>",
            ["x.svelte: :global(.why) { font-family: var(--mono) }"],
        ),
        (
            "the same shell rule quieting the register instead of re-facing it",
            "<p>x</p><style>:global(.why) { opacity: 0.4; }</style>",
            ["x.svelte: :global(.why) { opacity: 0.4 }"],
        ),
        (
            "a global that reaches another component's element and not the register",
            "<p>x</p><style>:global(.poster) { opacity: 0.4; }</style>",
            [],
        ),
        # `[class~="why"]` matches exactly what `.why` matches. It escaped twice over: the
        # combinator split tore it at the `~`, and the attribute strip dropped what was left.
        (
            "the register named by an attribute selector rather than a class",
            '<p class="why">x</p><style>[class~="why"] { font-family: var(--mono); }</style>',
            ['x.svelte: [class~="why"] { font-family: var(--mono) }'],
        ),
        (
            "an attribute selector that really does narrow is still a narrowing",
            '<p class="why">x</p><style>div[data-testid=\'w\'] { font-family: var(--mono); }</style>',
            [],
        ),
        (
            "the face set by the shorthand, where one word leaves the register",
            '<p class="why">x</p><style>.why { font: 13px/1.4 var(--mono); }</style>',
            ["x.svelte: .why { font: 13px/1.4 var(--mono) }"],
        ),
        (
            "dimmed rather than recoloured, which the contrast guard measures past",
            '<p class="why">x</p><style>.why { opacity: 0.4; }</style>',
            ["x.svelte: .why { opacity: 0.4 }"],
        ),
        (
            "full strength is the register, not an escape from it",
            '<p class="why">x</p><style>.why { opacity: 1; }</style>',
            [],
        ),
        # The five a whole-compound comparison could not see. The last compound was matched as one
        # string, so every ordinary way of NARROWING a selector -- a second class, a negation, a
        # position, a test hook -- put the rule outside the guard while leaving it inside the
        # cascade. The property never decided; the selector's punctuation did.
        (
            "narrowed by a second class the same element carries",
            '<p class="why loud">x</p><style>.why.loud { font-family: var(--mono); }</style>',
            ["x.svelte: .why.loud { font-family: var(--mono) }"],
        ),
        (
            "narrowed by a negation, which is a class the element does not carry",
            '<p class="why">x</p><style>.why:not(.loud) { color: var(--ink-5); }</style>',
            ["x.svelte: .why:not(.loud) { color: var(--ink-5) }"],
        ),
        (
            "narrowed by position, which is how a writer tells two why-lines apart",
            '<p class="why">x</p><style>.why:first-child { opacity: 0.4; }</style>',
            ["x.svelte: .why:first-child { opacity: 0.4 }"],
        ),
        (
            "narrowed by a test hook, which is an attribute and not a class",
            '<p class="why" data-testid="w">x</p>'
            "<style>p[data-testid='w'] { font-family: var(--mono); }</style>",
            ["x.svelte: p[data-testid='w'] { font-family: var(--mono) }"],
        ),
        (
            "a class attribute in the other quote, which made the whole file invisible",
            "<p class='why'>x</p><style>.why { color: var(--ink-5); }</style>",
            ["x.svelte: .why { color: var(--ink-5) }"],
        ),
        (
            "an error banner that is its own reason keeps the error's colour",
            '<p class="banner error why" role="alert">x</p>'
            "<style>.banner.error { background: var(--ember-wash); color: var(--ember-lift); }"
            "</style>",
            [],
        ),
    ],
)
def test_the_why_register_guard_catches_each_way_out_of_the_register(name, source, expected):
    """The guard above passes on a clean tree, so it is shown refusing each escape it exists for
    -- including the two this project deliberately allows, which a stricter reader would break."""
    assert _register_escapes("x.svelte", source) == expected, name


def test_the_why_register_guard_leaves_innocent_files_alone():
    """The side of the last-compound trade that costs, which the cases above do not show.

    `_invented_paddings` compares whole selectors and says why in as many words: `.rooms li` styles
    a row inside a card, and reading `li` alone would call every list row a card. This guard takes
    the opposite trade, so it carries the opposite risk -- once a file's why-line is a `<p>`, every
    rule in that file ending in `p` is read as reaching one. Measured rather than assumed: eight
    rules in the shipped tree are reached only by the widening and none of them touches the face,
    the colour or the volume, which is why the trade is worth making here and not there.

    What stays out is what a reader would expect to stay out: a file with no why-line at all, and
    a rule whose last compound is a class no why-line carries.
    [§6.8; decision 275; review cycle 1: reach-01]
    """
    no_why = '<p class="note">a number</p><style>p { font: 10px var(--mono); }</style>'
    assert _register_escapes("x.svelte", no_why) == [], (
        "a file with no why-line has no register to leave: `reachable` is built from its markup"
    )
    cousin = '<p class="why">x</p><style>.tick .fill { color: #8a8a8a; }</style>'
    assert _register_escapes("x.svelte", cousin) == [], (
        "the last compound is `.fill`, which this file's why-line does not carry -- the widening "
        "reaches what a why-line's own selectors reach, not every descendant in the sheet"
    )
    # The cost the SECOND widening carries, and the one shape it has to refuse. Reading a compound
    # as a set of tokens rather than as one string is what admits `.why.loud` -- and a file whose
    # why-line is a `<td>` puts `td` in reach, so an any-token reading would call every other cell
    # in that sheet a why-line. All of the compound's classes, or none of it: the Data tab really
    # does carry `<td class="note why">`, and the rule below it is a different cell entirely.
    narrowed = '<td class="note why">x</td><style>td.active { color: #8a8a8a; }</style>'
    assert _register_escapes("x.svelte", narrowed) == [], (
        "`td.active` narrows to a class no why-line carries, so it reaches a different element -- "
        "a compound is read as ALL of its classes, which is what tells a narrowing from a co-class"
    )


def _declarations(body: str) -> dict[str, str]:
    """One rule's declarations. Longhands are compared by name, so `min-height` and `height` are
    two properties rather than one substring of the other."""
    return {
        name.strip(): value.strip()
        for name, value in re.findall(r"([a-z-]+)\s*:\s*([^;}]+)", body)
    }


def test_the_wizard_step_indicator_stays_a_hairline():
    """The one control §6's 48 px floor is exempt from, exempt in writing rather than by silence.

    `.progress button` is a 3px hairline drawn as three buttons, and design.css's coarse-pointer
    block raises every `button` to `min-height: var(--touch)`. With no `min-height` of its own the
    scoped rule lost on the only property that decides, so the first thing a household saw on a
    phone at first boot was three 48px grey blocks above the heading. The repair is the narrow
    one: `min-height` beside the `height`, in the component, so the scoped rule wins where it
    matters and the floor still holds everywhere else. Decision 280 refuses the wide one -- a
    blanket `button { min-height: auto }` in design.css would exempt every button in the app from
    §6's preamble to spare this one, and the sweep that measures 48 would then measure nothing.

    The role moves with it. `role="progressbar"` sat on the container holding the three focusable
    buttons, and ARIA makes a progressbar's children presentational -- so the only way to reach a
    step was through controls the accessibility tree had been told not to expose, which is also
    the path `01-first-boot.spec.js` walks. It carried no `aria-valuemin` either.
    [§6 preamble; decision 280; row `platform-every-touch-target-meets-the-token`]
    """
    source = SETUP_PAGE.read_text(encoding="utf-8")
    rule = re.search(r"\.progress button\s*\{([^}]*)\}", _style_blocks(source))
    assert rule, "the first-boot wizard has no `.progress button` rule"
    decls = _declarations(rule.group(1))
    assert decls.get("height") and decls.get("min-height") == decls["height"], (
        "the wizard's step indicator declares "
        f"height: {decls.get('height')} and min-height: {decls.get('min-height')}: design.css's "
        "coarse-pointer block gives every button min-height: var(--touch), so a hairline with no "
        "min-height of its own is 48px tall on every phone"
    )
    hatch = re.search(r"(?<![a-z-])button[^{}]*\{[^{}]*min-height:\s*auto", _css())
    assert not hatch, (
        "design.css exempts buttons from the touch floor wholesale, which spares this hairline by "
        "giving up §6's preamble for every other control in the app (decision 280)"
    )

    markup = _rendered_markup(SETUP_PAGE)
    bars = re.findall(r"<([a-z]+)([^>]*\brole=\"progressbar\"[^>]*)>", markup)
    assert len(bars) == 1, f"the wizard declares {len(bars)} progressbars"
    tag, attrs = bars[0]
    assert 'aria-valuemin="1"' in attrs, "the progressbar states no minimum, so its value is unscaled"
    element = re.search(rf"<{tag}[^>]*\brole=\"progressbar\"[^>]*>(.*?)</{tag}>", markup, re.S)
    assert element and "<" not in element.group(1), (
        "the progressbar has element children: ARIA makes them presentational, and here they are "
        "the three buttons that are the only way to reach a step"
    )


def test_the_first_boot_page_starts_below_the_status_bar():
    """The bare pages have no shell to inset them, so each one insets itself.

    `app.html` sets `viewport-fit=cover` and a translucent status bar, which is what lets the
    installed app draw to the physical top edge -- and means a page that pads itself by a flat
    40px starts its heading under the clock. `max()` rather than an addition: `env()` is 0 in
    every browser this suite runs and on every desktop, so the page keeps exactly the padding it
    had unless the inset is larger than it, and nothing moves where there is nothing to clear.

    Asserted at the source because no run in this suite reaches this page with an inset reported:
    `01-first-boot.spec.js` is the only spec that opens the wizard, it runs on the desktop project
    alone, and nothing in it asks Chromium for one -- so a wrong `max()` here would pass every
    browser test. (The shell's own header is measured under an injected inset by
    `06-responsive.spec.js`; a bare page is a different rule on a page that run never visits.)

    `test_the_shell_reserves_the_top_safe_area` sweeps for this too, and keeps the wider claim; it
    finds this page by its `min-height: 100vh`, and accepts any top padding the inset appears in.
    This one names the page and the shape -- `max()` first, so the padding is the larger of the
    two rather than their sum, and the wizard keeps exactly the 40px it had on every desktop.
    [§6 preamble; M4.15 finding 1; row `platform-shell-clears-the-status-bar-and-the-toolbar`]
    """
    source = SETUP_PAGE.read_text(encoding="utf-8")
    rule = re.search(r"\.page\s*\{([^}]*)\}", _style_blocks(source))
    assert rule, "the first-boot wizard has no `.page` rule"
    padding = _declarations(rule.group(1)).get("padding", "")
    assert re.match(r"max\(\s*[0-9.]+px\s*,\s*env\(safe-area-inset-top\)\s*\)", padding), (
        f"the first-boot page pads its top with `{padding}`: on a notched phone in standalone "
        "mode that is measured from under the status bar. The top component is "
        "max(<the page's own padding>, env(safe-area-inset-top))"
    )


# --- M4.15: the box every surface sits inside (§6 preamble) --------------------------------

SHELL_LAYOUT = FRONTEND / "routes" / "+layout.svelte"
ERROR_PAGE = FRONTEND / "routes" / "+error.svelte"
SESSION_STORE = FRONTEND / "lib" / "session.svelte.js"

# The one line of the shell that STATES a fact about the household rather than rendering one, and
# therefore the one line whose condition is worth reading character by character (decision 271).
NO_BUNDLE = "no bundle imported"

# The one inset the whole app is wrong without, spelt exactly once here so the guard below
# rejects a near miss (`safe-area-inset` alone, or the bottom one) rather than accepting it.
TOP_INSET = "env(safe-area-inset-top)"

# The header's own height, and the number three fixed overlays anchor themselves to. It is a
# literal in five places and that is fine -- what is not fine is a literal that forgot the inset.
HEADER_HEIGHT = "54px"

_HEIGHT = re.compile(r"(?<![a-z-])height\s*:\s*([^;]+);")
_MIN_HEIGHT = re.compile(r"min-height\s*:\s*([^;]+);")
_PADDING_SHORTHAND = re.compile(r"(?<![a-z-])padding\s*:\s*([^;]+);")
_ANY_DECLARATION = re.compile(r"([a-z-]+)\s*:\s*([^;{}]+)")


def _stylesheet(path: Path) -> str:
    """The CSS a file ships, comments stripped, whether it is a component or the stylesheet."""
    if path.suffix == ".svelte":
        return _style_blocks(_src(path))
    return re.sub(r"/\*.*?\*/", "", _src(path), flags=re.S)


def _css_rule(styles: str, selector: str, start: int = 0, end: int | None = None) -> str:
    """The declarations of the first `selector { ... }` in a window of a stylesheet.

    A window rather than the whole sheet because the same selector is written twice on purpose:
    `header` has a base rule and a `@media (max-width: 720px)` override, and decision 279 is
    exactly the claim that the second one has to repeat what the first says.
    """
    window = styles[start:end if end is not None else len(styles)]
    found = re.search(rf"(?m)^\s*{re.escape(selector)}\s*\{{([^{{}}]*)\}}", window)
    assert found, f"no `{selector}` rule in that part of the stylesheet"
    return found.group(1)


def _shorthand_parts(value: str) -> list[str]:
    """Split a shorthand on top-level whitespace, so `max(24px, env(...))` stays one part."""
    parts: list[str] = []
    current = ""
    depth = 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char.isspace() and depth == 0:
            if current:
                parts.append(current)
                current = ""
        else:
            current += char
    if current:
        parts.append(current)
    return parts


def test_the_shell_uses_the_dynamic_viewport():
    """§6 preamble: phone-first, and the phone's first session is in a Safari tab.

    In an iOS Safari tab `100vh` is the LARGE viewport -- 745 px on an iPhone 13 -- while 664 px
    are visible with the toolbar expanded. A shell sized to it puts the 61 px bottom bar inside
    the 81 px strip the toolbar covers, and because the document is then exactly the layout
    viewport nothing overflows, so Safari never has a reason to collapse the toolbar and show it.
    The surface switcher is simply absent, on the form factor §6's preamble makes primary and in
    the session §3.1 puts before the install.

    The pair, in this order: `100dvh` alone drops iOS < 15.4 to `auto`, and a browser that does
    not know the second declaration keeps the first. Nothing in this suite can fail this rule --
    Playwright's viewport is the visible one by construction, which is why `06-responsive` passed
    while the bar was under the toolbar -- so it is asserted at the source. [M4.15 finding 2]
    """
    styles = _stylesheet(SHELL_LAYOUT)
    for selector in (".boot", ".shell"):
        declared = _HEIGHT.findall(_css_rule(styles, selector))
        assert declared == ["100vh", "100dvh"], (
            f"`{selector}` declares height {declared}: the shell has to be the VISIBLE viewport "
            f"(100dvh) with 100vh in front of it as the fallback for iOS < 15.4, in that order"
        )


def test_the_shell_reserves_the_top_safe_area():
    """§6 preamble: installable. `app.html` says how, and the CSS never answered.

    `app.html:5` sets `viewport-fit=cover` and `:12` sets
    `apple-mobile-web-app-status-bar-style=black-translucent`, so the installed app's web view
    starts at the physical top edge with the status bar drawn over it -- 47 px on an iPhone 13,
    59 px from the 14 Pro on. Under a 54 px header that is the whole header, and the account chip
    is a 32 px control centred in it: a tap where it appears to be is a tap on the status bar,
    which scrolls to top instead.

    Four rules, not one, because the inset is discarded by whichever of them forgets it:

    1. the base `header` rule pads and sizes itself by the inset;
    2. the `@media (max-width: 720px)` override repeats it on `min-height`, because M4.9 gave the
       phone header `height: auto` so four badges can wrap and that discards the base `height`
       on exactly the form factor the inset exists for (decision 279);
    3. nothing anywhere anchors to the bare header height -- the two full-bleed overlays offset
       from `calc(54px + inset)`, not from `54px`;
    4. a page that IS the screen (`min-height: 100vh`) starts its padding below the status bar.

    `env()` resolves to 0 on every engine this suite runs UNLESS one is asked to report an inset,
    and one can be: `06-responsive.spec.js`'s `the header grows by the status-bar inset when one
    is reported` sends Chromium's `Emulation.setSafeAreaInsetsOverride` and then measures the two
    header rules COMPOSED -- which is the half this guard cannot read, because it matches text.
    The two are not redundant and neither replaces the other. This one reaches every file in the
    frontend, including the pages no browser test loads and the overlays no run opens; that one
    reaches the cascade, where a `box-sizing` regression, a later `padding-top: 0` or a
    `min-height: unset` would each break the rule while writing none of the literals swept for
    below. WebKit has no CDP, so the phone project has only this guard.
    [M4.15 finding 1; decision 279; review cycle 1: M415-C1-COV-02]
    """
    styles = _stylesheet(SHELL_LAYOUT)
    phone_at = styles.find("@media (max-width: 720px)")
    assert phone_at > 0, "the shell has no phone override to check the inset in"

    base = _css_rule(styles, "header", 0, phone_at)
    assert f"padding-top: {TOP_INSET}" in base, (
        "the header does not pad itself by env(safe-area-inset-top): with viewport-fit=cover and "
        "a translucent status bar its content sits under the clock"
    )
    height = _HEIGHT.search(base)
    assert height and HEADER_HEIGHT in height.group(1) and TOP_INSET in height.group(1), (
        f"the header's height is {height.group(1).strip() if height else 'unset'}: it has to GROW "
        f"by the inset it pads by, or the row below it moves up under the status bar"
    )

    floor = _MIN_HEIGHT.search(_css_rule(styles, "header", phone_at))
    assert floor and HEADER_HEIGHT in floor.group(1) and TOP_INSET in floor.group(1), (
        f"the phone header's min-height is {floor.group(1).strip() if floor else 'unset'}: the "
        f"override sets height: auto so the badges can wrap, which discards the base rule's "
        f"calc() on the primary form factor -- the inset has to be repeated here (decision 279)"
    )

    anchored = []
    unlifted = []
    for path in sorted(_frontend_sources()):
        if path.suffix not in {".svelte", ".css"}:
            continue
        css = _stylesheet(path)
        where = path.relative_to(REPO).as_posix()
        for prop, value in _ANY_DECLARATION.findall(css):
            if HEADER_HEIGHT in value and TOP_INSET not in value:
                anchored.append(f"{where}: {prop}: {value.strip()}")
        for body in re.findall(r"\{([^{}]*)\}", css):
            if not re.search(r"min-height\s*:\s*100vh", body):
                continue
            padding = _PADDING_SHORTHAND.search(body)
            if not padding:
                continue
            top = _shorthand_parts(padding.group(1))[0]
            if TOP_INSET not in top:
                unlifted.append(f"{where}: padding: {padding.group(1).strip()}")

    assert not anchored, (
        f"these anchor to the header's bare height and so sit under the status bar in the "
        f"installed app, where the header itself does not: {anchored}"
    )
    assert not unlifted, (
        f"these pages are the whole screen (min-height: 100vh) and start their padding at the "
        f"physical top edge, which is behind the status bar: {unlifted}. The rule is "
        f"max(<the page's own value>, env(safe-area-inset-top)) for the top component"
    )


def test_the_app_ships_an_error_boundary():
    """§3.1 asks for an explicit state instead of an error; §6.8 gives the app one language.

    Two things reach this page, and neither is the one the first version of this docstring named:
    there is no `load` function anywhere in `src/routes` -- `+layout.js` exports `ssr` and
    `prerender` and nothing else -- so "a throw from a load" had no instances in this app. What
    does reach it is a client-router 404 (`app.py`'s SPA fallback answers index.html for every GET
    outside `api/`, and `ssr = false` makes the first navigation unhydrated, so SvelteKit renders
    the root error page rather than reloading the address it is already on) and a route chunk that
    fails to import while `_app/version.json` has not moved. Either way the alternative was
    SvelteKit's built-in page, a bare light-themed "Internal Error" on white, outside the design
    system and with no way back into the dark shell -- in an installed standalone web view there
    is no address bar to type into, so that page is a dead end on the primary form factor.

    This guard reads the file. `02-shell.spec.js`'s `an unknown address renders the app's own
    error card, not the framework's page` is what renders it, and the two are not redundant: a
    file nothing mounts can satisfy every substring below while drawing nothing at all.

    The retraction reaches the MESSAGE and not only this docstring, which is where it went the
    first time: pytest prints the assertion, not the prose above it, so the reader at a red log was
    still being sent after a `load` this app does not have while the paragraph correcting that sat
    four lines up, unprinted. [M4.15 finding 19, fe-15; review cycle 2: M415-C2-COMP-04;
    review cycle 3: M415-C3-COMP-03]
    """
    # The premise both sentences rest on, asserted rather than restated. `load` is where every
    # other SvelteKit app's errors come from, so "a throw from a load" is the sentence that writes
    # itself here -- it wrote itself once already -- and it is only wrong for as long as this app
    # has no load. The day one appears, this reddens and sends the reader to the two sentences
    # above instead of letting the wrong one quietly become right for the wrong reason.
    loaders = sorted(
        path.relative_to(FRONTEND).as_posix()
        for path in (FRONTEND / "routes").rglob("+*.js")
        if re.search(r"(?m)^\s*export\s+(?:async\s+function|function|const)\s+load\b", _src(path))
    )
    assert not loaders, (
        f"these route modules export a `load`: {loaders}. This guard's docstring and message both "
        "argue from there being none -- the two things that reach +error.svelte are a client-"
        "router 404 and a chunk that fails to import -- so re-read both before adding one."
    )
    assert ERROR_PAGE.is_file(), (
        "frontend/src/routes/+error.svelte does not exist, so a client-router 404 or a route "
        "chunk that fails to import renders SvelteKit's own light-themed page outside the app's "
        "design language -- and in an installed standalone web view there is no address bar, so "
        "that page is a dead end on the primary form factor"
    )
    source = _src(ERROR_PAGE)
    assert "$lib/design.css" in source, (
        "+error.svelte does not import design.css. SvelteKit keeps the root layout in the branch "
        "when it swaps in a root error page, so `+layout.svelte`'s import is in force and this one "
        "is belt and braces -- which is the right trade for the page whose whole job is to render "
        "when something upstream did not, and it costs nothing because the import is deduplicated "
        "at build time"
    )
    assert "$page.error" in source, "+error.svelte renders no reason at all"
    for door, why in (
        ("location.reload()", "a chunk that failed to import is the case a reload repairs"),
        ('href="/"', "an installed standalone view has no address bar to type a way out into"),
    ):
        assert door in source, f"+error.svelte offers no {door}: {why}"

    # AND THE CARD HAS TO SAY WHICH DOOR IS FOR WHICH FAILURE. The reason above used to read "the
    # error is usually a stale chunk, and a reload is the fix" -- a frequency nobody measured,
    # about a case this app routes away from this page twice over (`beforeNavigate` takes the
    # deploy while `$updated` is up, and the docstring's own second trigger is the chunk failure
    # where the version has NOT moved). On the one trigger any test in this repository drives, a
    # client-router 404, a reload is a no-op by construction: the SPA fallback answers the same
    # address with index.html and 200 and this card renders again, which on a phone in standalone
    # mode is a door with no address bar behind it. §6.8 asks for a sentence a person can act on,
    # so the card names both doors rather than leaving the ember-filled one to look like the
    # answer. [§6.8; review cycle 3: M415-C3-COMP-02]
    quiet = re.findall(r'<p class="why">(.*?)</p>', source, re.S)
    named = [line for line in quiet if "Reload" in line and "Home" in line]
    assert named, (
        "+error.svelte draws two doors that repair different failures and says which for "
        "neither: a `.why` line has to name both, because on a client-router 404 -- the only "
        "trigger this suite drives -- the primary door re-renders this same card"
    )


# --- M4.15: one ring and one accent, both decided in design.css (§6.8) ----------------------

# Every rule in a shipped component that spends the full-strength accent, and the reason it is
# allowed to. Keyed `file: selector` on the path relative to `frontend/src`, because two of the
# sixteen live in a `+page.svelte` and a basename would let one route's licence excuse another's.
#
# §6.8 rations the colour in one sentence -- "one ember accent #c8613a spent on selection and
# primary actions" -- and a sentence with no list under it was read as "the accent colour", so it
# also drew two statuses, an avatar, three progress fills and two pieces of text: eight sites where
# it meant nothing at all, and where a selection next to one of them was no longer legible AS a
# selection. This list is what those two words mean in this app, written down once. Adding a line
# is the point at which the reader has to say which of the two the new site is.
#
# Scope is the full-strength token only. `--ember-lift`, `--ember-wash` and `--ember-edge` are the
# accent quoted rather than spent -- error copy, a tinted callout's frame, a hover border -- and
# `--facet-mood` is #c8613a by §6.4's own binding, which decision 276 records as the deliberate
# exception rather than recolouring a facet to protect a guard.
ACCENT_ALLOWLIST = {
    "lib/components/AccountChip.svelte: .track.on":
        "the show-model switch, on: a switch's on state is a selection the person made",
    "lib/components/ModelRail.svelte: .chip.on":
        "the event-kind filter the rail is currently showing",
    "lib/components/RateBattleCard.svelte: .side:hover:not(:disabled) :global(.poster)":
        "the poster a pointer is about to pick: hover is the selection in progress",
    "lib/components/RateBattleCard.svelte: .side:focus-visible :global(.poster)":
        "the same pick reached by keyboard, so both input devices tell the same story",
    "lib/components/RateBattleCard.svelte: .cell:hover:not(:disabled)":
        "proposal 48's mirrored strip: the verdict under the thumb",
    "lib/components/RateBattleCard.svelte: .cell:focus-visible":
        "the same verdict reached by keyboard",
    "lib/components/RateBattleCard.svelte: .track.on":
        "the decisive switch, on",
    "lib/components/RateCorrections.svelte: .side:hover:not(:disabled)":
        "the side of a correction pair about to be chosen",
    "lib/components/RateCorrections.svelte: .side:focus-visible":
        "the same side reached by keyboard",
    "lib/components/RateSweepCard.svelte: .verdict:hover:not(:disabled)":
        "the sweep verdict about to be given",
    "lib/components/RateSweepCard.svelte: .verdict:focus-visible":
        "the same verdict reached by keyboard",
    "lib/components/RateUndo.svelte: .chip:hover:not(:disabled)":
        "the undo about to be taken -- a primary action, and the only one on that strip",
    "lib/components/RateUndo.svelte: .chip:focus-visible":
        "the same undo reached by keyboard",
    "lib/components/ShelfRow.svelte: .nudge:focus-visible":
        "a focus ring, which is the keyboard's selection (see FOCUS_RING_EXCEPTIONS)",
    "routes/rank/+page.svelte: .tile.picked":
        "§4.2's lifted entry: the card tap-to-tier has picked up and is waiting to place",
    "routes/tonight/+page.svelte: .controls input[type='range']":
        "decision 277: `accent-color` paints a native control's own chosen value, which is the "
        "same grammar as `.pill[aria-pressed='true']` and not a second answer to it",
}

# Where the fourth spelling is spent, which is a SECOND list and deliberately not an entry in the
# one above. `ACCENT_ALLOWLIST` is selection and primary actions -- that is the sentence §6.8
# writes and exit criterion 7 measures -- and a facet stripe is neither, so putting one in there
# would license the retired colour under a reason that is false. What a spend of `--facet-mood`
# has to argue instead is that it is the FACET: proposal 124 gives mood the warm end of §6.4's
# ramp, and `ModelRail`'s event-kind stripes are proposal 118's colour-coding bound to that ramp
# rather than to eleven new hues.
#
# The collision the decision leaves standing is real and is named here rather than papered over:
# forty lines above that stripe, `.chip.on` spends the accent on the kind filter the rail is
# showing, so inside one component #c8613a means both "the filter you selected" and "this event is
# a verdict". Recolouring the facet is the only repair and decision 276 refuses it by name --
# §6.4's palette belongs to whoever ships it. An enumerated exception is what was available: it
# keeps the collision to one selector a reader can see, instead of a token-wide licence nothing
# counts. [§6.8; proposals 118, 124; decision 276 as amended; review cycle 3: M415-C3-ACC-01]
FACET_ACCENT_SITES = {
    "lib/components/ModelRail.svelte: .events li[data-kind='verdict']":
        "proposal 118's event-kind stripe, bound to the facet palette rather than to a new colour "
        "-- the facet is mood and mood is #c8613a by §6.4's binding, not a spend of the accent",
}

# The eight the sweep retired, with what each of them actually meant, and the ninth review cycle 1
# sent after them. They are not a second list the guard consults -- they are absent from the one
# above, which is what makes their return a failure -- but they are the reason it exists, and
# `_accent_sites` is shown refusing each below.
#
# The ninth is the one this list had already licensed, which is what makes it worth naming: the
# entry read "the swap step the admin has reached, of four -- the wizard's own selection", and the
# markup is four NON-interactive spans lit cumulatively by `phase`. Nothing there is selected, the
# last of the four is a server outcome nobody picks, and the component renders inside the first-boot
# wizard at step 3 -- under the step ramp this same milestone retired to --progress-now and over a
# `.btn-primary` whose fill IS the accent. A licence with a false reason reads exactly like a true
# one, which is why these reasons are prose. [review cycle 1: acc-01; decision 276 as amended]
RETIRED_ACCENT_SITES = (
    "PosterCard's cold badge (a status, now --status)",
    "RateClassBalance's .high count (a status)",
    "AccountChip's avatar (an identity, now --identity)",
    "RateBlockCounter's .tick.now and RateRail's .fill (progress)",
    "the first-boot wizard's current step (progress)",
    "Tonight's room code and beat label (text, now --ink and the file's own .data rule)",
    "BundleImport's reached swap steps (progress, now --progress-now)",
)

# design.css's own spends, which the component scan deliberately does not read. A second list and
# not a widening of the first, because the two answer different questions: a COMPONENT asking for
# the accent has to argue for it, while design.css is where the argument was already won -- the
# rule named `.btn-primary` IS the primary action, and `.pill.on` IS the selection. Reading the
# sheet against the component allowlist would mean licensing the app's own selection rules, which
# inverts the sweep's direction (decision 276 as amended).
#
# What a list buys here is the other half of the sentence. The scan stops at `.svelte`, so the
# highest-leverage stylesheet in the app -- the one every surface inherits from -- was the one
# place a global `.badge { background: var(--ember) }` could be added and reach every screen at
# once with both guards green. `:root` is absent because a token DEFINITION is not a spend:
# `--ember` and `--facet-mood` are design.css saying what the colour IS, which is
# `test_the_ember_accent_is_not_reused_as_a_neutral`'s half of the clause.
# [§6.8; decision 276 as amended; review cycle 1: acc-04]
DESIGN_CSS_ACCENT_SITES = {
    "a": "a link is the one thing a person can act on from inside a sentence, so it is an action "
         "-- unlike the text the sweep retired, a room code and a beat label that do nothing",
    ":focus-visible": "the app's one ring, which is the keyboard's selection; its VALUE is held by "
                      "test_the_app_has_one_focus_ring_rule rather than here",
    ".pill[aria-pressed='true']": "a pill that is pressed: the selection, stated by ARIA",
    ".pill.on": "the same pill, where the state is a class rather than an attribute",
    ".btn-primary": "the primary action, in the class named after it",
}

# Three spellings of ONE colour, because the rule is about the colour and a guard that enumerates
# spellings is a guard the next spelling walks past -- the same argument decision 276's amendment
# makes about the two places a spend can be WRITTEN, applied to the ways it can be SPELLED. The
# literal `var(--ember)` missed both of the token's other legal forms: `var( --ember )`, and
# `var(--ember, #fff)`, where the fallback list breaks the closing paren. The raw triple missed
# `rgb(200, 97, 58)` entirely, which is one keystroke pattern away from the hex and invisible to
# every token audit there is.
#
# Full strength only, which is why the alpha is spelled out rather than left open: `--ember-wash`
# and `--ember-edge` ARE `rgba(200, 97, 58, ...)` in design.css, so a component writing the wash
# out by hand is quoting the accent rather than spending it, and matching the triple alone would
# call the quietest use of this colour its loudest. The boundary this cannot hold is a near miss
# in another colour space -- `hsl(16 55% 51%)` is three units off #c8613a and indistinguishable on
# screen -- and closing it needs a conversion and a tolerance, which is a different guard from one
# that asks whether a writer named the accent.
#
# The fourth spelling is a token name, and it is the only one left in this tree: design.css binds
# `--facet-mood` to #c8613a, so `var(--facet-mood)` paints pixel for pixel what `var(--ember)`
# paints and was invisible to all three accent guards. A writer who wants the retired colour back
# does not have to reach for the accent's own name -- decision 276 settled that the token's VALUE
# stays (recolouring a facet is §6.4's call), and the guard read that as a licence for its SPENDS,
# which is one token wider than the decision.
#
# Admitted as a spelling rather than as an exception, which is what keeps this a rule about the
# COLOUR: `test_the_accent_pattern_names_facet_mood_only_while_it_is_the_accent` holds the binding
# this alternative rests on, so the day §6.4 gives mood its own hue the guard reddens and says to
# drop the alternative rather than quietly demanding an accent licence for a colour that is no
# longer the accent. [§6.8; review cycle 1: acc-03; decision 276 as amended;
# review cycle 3: M415-C3-ACC-01]
ACCENT_ALIAS_TOKEN = "--facet-mood"
_ACCENT = re.compile(
    r"var\(\s*--ember\s*[,)]"
    r"|var\(\s*--facet-mood\s*[,)]"
    r"|#c8613a"
    r"|rgba?\(\s*200\s*[, ]\s*97\s*[, ]\s*58\s*(?:[,/]\s*(?:1|1\.0*|100%)\s*)?\)",
    re.I,
)

# How a colour written in JavaScript names itself: the declaration binding it, or the object key
# it sits under. `<expression>` where the line carries neither, which asks the writer to name the
# site rather than guessing at what it is called.
_JS_BINDING = re.compile(r"(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)|([A-Za-z_$][\w$]*)\s*[:=]")

# `outline-offset` is deliberately not a ring: it positions one. Matching it would make the single
# global rule look like two answers to its own question.
_A_RING = re.compile(r"(?<![a-z-])outline(?:-(?:color|style|width))?\s*:\s*([^;}]+)")

# ZERO OF ANY UNIT, and not a list of units, because `^(none|0)\b` got both directions of this
# backwards. `\b` wants a non-word character after the `0` and `p` is a word character, so `0px`
# -- the commonest way after `none` to kill a ring -- fell into the DRAWING bucket and reddened
# the build with "these declare a second ring... put the change there if the whole app should
# have it", which is the repair for the opposite defect. `0.5px` went the other way: `.` IS a
# non-word character, so a genuine sub-pixel hairline was reported as the ring being taken away.
# Both send the reader to the wrong fix, which is ring-c2-01's own finding one pattern over --
# "nothing wrong could ship on it... what shipped was the wrong sentence, and in a file whose
# whole output is sentences that is the product."
#
# So the rule is stated rather than the units enumerated: an optional fractional part that is all
# zeroes, an optional unit of any name, and nothing word-like or decimal after it. `0q`, `0vh`,
# `0ch` and `0in` are covered by the rule and would each have needed a line in a list; the
# trailing `(?![\w.])` rather than `$` is what keeps `outline: 0 solid red` a removal, which the
# boundary read correctly before. [proposals 127, 131; review cycle 2: ring-c2-01;
# review cycle 3: M415-C3-RING-06]
_RING_SUPPRESSED = re.compile(r"^(?:none\b|0(?:\.0+)?(?:[a-z]+|%)?(?![\w.]))", re.I)

# A ring drawn with `box-shadow` is still a second ring, and it is the commonest modern way to draw
# one: a spread shadow follows the border radius, which is the one thing an outline could not do
# until recently, so it is what a component reaching for a rounder ring reaches for. Only on a
# focus rule, because `box-shadow` anywhere else is a shadow -- AccountChip's menu has one -- and
# only where it draws something: `box-shadow: none` takes away a shadow, not a ring, which is why
# the suppression half stays outline-only. In this app the outline IS the ring.
# [proposals 127, 131; review cycle 1: reach-01]
_A_FOCUS = re.compile(r":focus(?:-visible|-within)?\b")
_A_SHADOW = re.compile(r"(?<![a-z-])box-shadow\s*:\s*([^;}]+)")
# Not `_RING_SUPPRESSED`, which reads an OUTLINE value: every shadow ring in the idiom begins with
# an offset of `0`, so the removal pattern an outline uses would have read `0 0 0 2px var(--ember)`
# as the ring being taken away and dropped exactly the shape this exists to see.
_SHADOW_NONE = re.compile(r"^none\b")

# The one rule, in the one file. Everything else that draws a ring is an exception or a bug.
GLOBAL_RING = "lib/design.css: :focus-visible"

# A component may still ring something itself, if the global rule cannot do what it needs. One
# does, and the reason is the whole test of whether an exception is real.
#
# Licensed by SITE, held to VALUE. The first draft licensed the site alone -- `ring.split(...)[0]
# not in FOCUS_RING_EXCEPTIONS` -- which dropped the value entirely, so this selector could carry
# any ring of any width and colour and the test that exists to prevent six competing rings would
# not have noticed. It was already carrying one: `1px solid var(--ember)` under a docstring saying
# in as many words that "a different width or a different colour is not" a legitimate exception.
# So an exception may RESTATE the one ring and may not REDEFINE it; what it buys is the property
# design.css cannot reach from outside the component.
# [proposals 127, 131; M4.15 review cycle 1: m415-rev1-css-06]
FOCUS_RING_EXCEPTIONS = {
    "lib/components/ShelfRow.svelte: .nudge:focus-visible":
        "the shelf's chevron is opacity: 0 until its row is hovered, so this rule has to REVEAL "
        "the control before ringing it -- a ring drawn around something invisible is not a ring, "
        "and `opacity: 1` is the half design.css cannot express",
}


def _rules(styles: str) -> list[tuple[str, str]]:
    """(selector, body) for every rule in a stylesheet, one entry per comma-separated selector.

    Per selector rather than per rule, so an allowlist entry says which element it licenses:
    `.side:hover:not(:disabled), .side:focus-visible` is two claims about two input devices, and
    a key holding both would be silently rewritten by anyone who reordered them.

    `[^{}]` on both halves means an `@media` wrapper is stepped over rather than swallowed -- the
    rules inside it are found on their own, which is how Rank's coarse-pointer block is read.

    THE SAME `[^{}]` DOES NOT STEP OVER A BRACE INSIDE A STRING, and the consequence is stated
    here because six guards inherit it: the component accent sweep, both design.css readers,
    `_ring_sites`, `_coarse_declarations` and the card-padding reader. `content: "}"` ends the body
    early, so everything after it in that rule is deleted from all six at once -- `_accent_sites`
    on `.a::after { content: "}"; background: var(--ember); }` returns nothing. `content: "{"` goes
    the other way and yields a rule whose SELECTOR is `content: "`, which no allowlist could
    sensibly hold. A real parser is not worth the one shape; a boundary nobody wrote down is what
    made the `@media` half worth writing down, and `_coarse_blocks` a few thousand lines on
    hand-matches braces precisely because this reader cannot. `test_no_style_block_hides_a_brace_
    in_a_string` keeps the shape out of reach instead, which is the trade this file already makes
    for `_accent_in_code` and a `//` inside a string literal.
    [review cycle 3: M415-C3-GUARD-04]
    """
    out = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", styles):
        for part in selector.split(","):
            part = " ".join(part.split())
            if part:
                out.append((part, body))
    return out


def _element_site(tag: str, attrs: str) -> str:
    """How an accent spend written in the markup names itself, since it has no selector.

    The tag plus its class list, because that is what a reader greps for and what an allowlist
    entry has to be recognisable as. Two `<span>`s in one file are then two keys rather than one.

    Both quotes, which is the invariant the sentence above claims and a double-quote-only read did
    not hold: a single-quoted `class` collapsed every `<span>` in a file onto one key, so a licence
    written for one of them licensed the others, and a licence written as `<span class="badge">`
    stopped matching the moment someone rewrote the quotes. Nothing enforces quote style here --
    `frontend/` ships neither prettier nor eslint, which is the argument `_classes` already carries
    for the same one-line widening at the top of this file. Behaviour-neutral on the shipped tree:
    every class attribute in it is double-quoted, so no label moves.
    [§6.8; review cycle 2: why-c2-01; review cycle 3: M415-C3-ACC-07]
    """
    marker = re.search(r"class=(\"|')(.*?)\1", attrs)
    return f"<{tag}" + (f' class="{marker.group(2)}"' if marker else "") + ">"


def _design_css_accent_sites(css: str) -> list[str]:
    """Every selector in design.css that SPENDS the accent, in sheet order, definitions excluded.

    Per declaration rather than per rule body, because `:root` holds both halves of the clause at
    once: `--ember: #c8613a` says what the colour is and `--facet-mood: #c8613a` says what else is
    allowed to be it, and neither is a use. A property starting `--` is a name being given a value;
    everything else is that value being drawn.
    """
    sites = []
    for selector, body in _rules(css):
        if any(
            not prop.startswith("--") and _ACCENT.search(value)
            for prop, value in _declarations(body).items()
        ):
            sites.append(selector)
    return sites


def _design_css_accent_names(css: str) -> list[str]:
    """Every custom property in design.css whose VALUE is the accent, in sheet order.

    The exact complement of `_design_css_accent_sites`' filter, and the other half of §6.8's
    sentence: that reader takes the properties that DRAW a colour, this one takes the names being
    given one. Written here rather than left inline in the M0 guard six thousand lines above
    because the two halves are one rule and were drifting apart -- the seam between them was a
    spelling, and a spelling is exactly what `_ACCENT` exists to stop being a seam.

    Every rule and not `:root` alone, because the pattern this replaces scanned the whole sheet and
    a token minted inside an `@media` block is still a token.
    [§6.8; decision 276 as amended; review cycle 2: acc-c2-02]
    """
    return [
        prop
        for _, body in _rules(css)
        for prop, value in _declarations(body).items()
        if prop.startswith("--") and _ACCENT.search(value)
    ]


def _accent_sites(name: str, source: str) -> list[str]:
    """Every `file: selector` in one component that spends the full-strength accent.

    Style blocks AND markup. A `<style>`-only scan is a scan of one of the two places this app
    writes colour: `style:color={facetColour(t.facet)}` is the house idiom in four of the very
    files the sweep retired a fill from, and `AccountChip.svelte` already takes an avatar's
    background from a database column. So the sweep's eight retirements held only for as long as
    nobody reached for the spelling their neighbours use, and the guard whose whole job is to make
    a new spend NAME itself would have stayed green through it.

    Read across the element's attributes rather than out of a `style=` value, because the rule is
    about the colour and not about which of Svelte's two spellings carried it -- a directive, a
    quoted attribute and a shorthand are three ways to write one spend, and a guard that
    enumerates spellings is a guard a fourth spelling walks past.

    The reader is brace-aware for the same sentence, one level down. An attribute run of
    `[^<>"]` ends at the first bare `>`, and a Svelte attribute IS an expression: `{() => ...}`,
    `{n > 3 ? ... : ...}` and `{n >= k}` all close the match mid-attribute, so everything after
    the arrow was read by nothing -- and `{n < 3 ? ...}` was worse, because the group can give
    back no `>` and the whole element is dropped. That is not a hypothetical spelling: a returning
    cold badge is `style:background={n > 3 ? 'var(--ember)' : 'var(--status)'}`, one keystroke
    from `style:color={facetColour(t.facet)}` at `ShelfRow.svelte:133`, and it passed all three
    accent guards. Measured on this tree, the old run truncated 101 elements across 22 of 23
    components; the brace-aware one truncates none and reports the same seventeen sites, so it is
    four spellings closed at zero cost to the allowlist. One nested level is the limit, which is
    an object literal inside an expression and is where a source scan should stop.
    [§6.8; decision 276 as amended; M4.15 review cycle 1: M415-REV1-08;
     review cycle 3: M415-C3-ACC-02]
    """
    sites = [
        f"{name}: {selector}"
        for selector, body in _rules(_style_blocks(source))
        if _ACCENT.search(body)
    ]
    for tag, attrs in re.findall(
        r"<([A-Za-z][A-Za-z0-9-]*)((?:[^<>\"{]|\"[^\"]*\"|\{(?:[^{}]|\{[^{}]*\})*\})*)>",
        _markup(source),
    ):
        if _ACCENT.search(attrs):
            sites.append(f"{name}: {_element_site(tag, attrs)}")
    sites += _accent_in_code(
        name, "\n".join(re.findall(r"<script\b[^>]*>(.*?)</script>", source, re.S))
    )
    return sites


def _accent_in_code(name: str, code: str) -> list[str]:
    """Every `file: identifier` in one file's JavaScript that names the full-strength accent.

    The third place this app can write a colour, and the one the other two point at: the value
    reaching `style:background={expr}` is an EXPRESSION, and an expression carries no literal for
    a colour pattern to match. So the spend moves one scope out -- into the component's own script
    block, which `_markup` strips, or into a module beside it, which the walk never opened -- and
    every accent guard in this file stays green while a status badge wears §6.8's one colour again.
    That is the same widening decision 276 already took twice, applied to the third place rather
    than to a fourth spelling.

    Named by the identifier it is bound to, because the mechanism is unchanged: the point is not to
    forbid the colour but to make a writer say which of the two things §6.8 rations it to this is,
    and `lib/theme.js: ACCENT` is what an allowlist entry would have to be recognisable as.

    Comments are stripped first, at the cost this file already accepts elsewhere for the same
    reason: a `//` inside a string literal takes the rest of that line with it, which can only
    lose a site, while a comment explaining the accent -- the likeliest line in the tree to name
    it -- would otherwise be reported as a spend of it.
    [§6.8; decision 276 as amended; review cycle 2: M415-C2-CSS-07]
    """
    code = re.sub(r"(?m)//.*$", "", re.sub(r"/\*.*?\*/", "", code, flags=re.S))
    sites = []
    for line in code.splitlines():
        if not _ACCENT.search(line):
            continue
        bound = _JS_BINDING.search(line)
        label = (bound.group(1) or bound.group(2)) if bound else "<expression>"
        sites.append(f"{name}: {label}")
    return sites


def _live_accent_sites(root: Path) -> list[str]:
    """Every accent spend in the shipped frontend under `root`, in file order.

    A root rather than the constant, so the sweep can be shown reading a tree that has one in it
    -- the shape `_invented_paddings` and `_unsigned_exceptions` already use here. Colocated
    `*.test.js` are skipped for `_frontend_sources`' own reason: a falsifier naming a colour is
    asserting about it, and vitest is what reads those files.
    [§6.8; decision 276 as amended; review cycle 2: M415-C2-CSS-07, acc-c2-04]
    """
    sites = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.endswith(".test.js"):
            continue
        name = path.relative_to(root).as_posix()
        if path.suffix == ".svelte":
            sites += _accent_sites(name, _src(path))
        elif path.suffix == ".js":
            sites += _accent_in_code(name, _src(path))
    return sites


def _ring_sites(name: str, styles: str) -> list[tuple[str, str, str]]:
    """Every `(file: selector, value, property)` that draws or removes a ring.

    Two properties and not one, because this is a guard about the RING and not about the property
    a ring was drawn with: a `box-shadow` spread on a focus rule is a second ring by every measure
    that matters to a keyboard, and an outline-only reader calls a tree with one in it clean.

    Which of the two a site came from travels WITH it, because the reader downstream cannot
    recover it from the value: `0` is the ring taken away on an outline and the first offset of a
    ring being drawn on a shadow, and a bucketing that reads the value alone gets exactly that case
    backwards. [review cycle 2: ring-c2-01]
    """
    sites = []
    for selector, body in _rules(styles):
        sites += [
            (f"{name}: {selector}", value.strip(), "outline") for value in _A_RING.findall(body)
        ]
        if not _A_FOCUS.search(selector):
            continue
        sites += [
            (f"{name}: {selector}", value.strip(), "box-shadow")
            for value in _A_SHADOW.findall(body)
            if not _SHADOW_NONE.match(value.strip())
        ]
    return sites


def _rings_and_removals(sites: list[tuple[str, str, str]]) -> tuple[list[str], list[str]]:
    """The sites that DRAW a ring and the sites that take one away, told apart by property.

    Split out of the guard for the reason `_ring_offences` was, and after the same kind of defect:
    a bucketing that lives in a test body is the one piece of a guard nothing can be shown
    refusing, and this one was wrong. `_SHADOW_NONE` says four lines above `_A_SHADOW` why
    `_RING_SUPPRESSED` must not be pointed at a shadow -- every shadow ring in the idiom begins
    with an offset of `0` -- and the guard then pointed it at every value this returns, so a
    component drawing the rounder ring the shadow branch exists to catch was told it had removed
    the ring and sent to undo a removal it had not made. [proposals 127, 131; review cycle 2:
    ring-c2-01]
    """
    rings, removed = [], []
    for site, value, prop in sites:
        taken = prop == "outline" and _RING_SUPPRESSED.match(value)
        (removed if taken else rings).append(f"{site} -> {value}")
    return rings, removed


def _ring_offences(rings: list[str], global_ring: str) -> tuple[list[str], list[str]]:
    """The two ways a ring can be wrong, told apart: a second one, and a licensed one redrawn.

    Split out of the guard so it can be shown refusing a licensed site that changes the value,
    which is the case the site-only filter it replaces could not refuse and the case that shipped.
    """
    competing, redrawn = [], []
    for ring in rings:
        site, _, value = ring.partition(" -> ")
        if site == GLOBAL_RING:
            continue
        if site not in FOCUS_RING_EXCEPTIONS:
            competing.append(ring)
        elif " ".join(value.split()) != " ".join(global_ring.split()):
            redrawn.append(f"{ring} (the one ring is `{global_ring}`)")
    return competing, redrawn


def test_the_ember_accent_is_spent_only_on_selection_and_primary_actions():
    """§6.8: "one ember accent #c8613a spent on selection and primary actions."

    `test_the_ember_accent_is_not_reused_as_a_neutral` has guarded the token side of that sentence
    since M0 -- no second design.css token may hold the colour -- and nothing guarded the spending
    of it, which is where the sentence actually applies. Seven components had taken the accent for
    a status badge, a count, an avatar, three progress fills, a room code and a beat label: eight
    sites, none of them a selection, none an action, and each one costing the selections beside it
    the only thing that made them legible as selections. That is the failure the rationing exists
    to prevent, and it was invisible to every other guard here, because each of the eight was a
    perfectly ordinary line of CSS in a perfectly ordinary component.

    An allowlist rather than a heuristic. "Selection" and "primary action" are design judgements
    and no regex holds them; what a regex can do is make every new spend name itself, which turns
    a silent widening into one line a writer has to write and a reader has to read. Decision 276
    chose exactly that over recolouring `--facet-mood`, whose #c8613a is §6.4's binding and not a
    reuse.

    Components only, which is a division of labour and not a hole: design.css's own spends are a
    different question with a different answer, and they are asked one test below. Components
    INCLUDING the JavaScript beside them from review cycle 2 on -- a fill bound to a constant and
    applied with `style:background={FILL}` is the same spend written one scope out, and the
    expression the directive carries holds no colour for a scan of the markup to find.

    The list is held in both directions, which is the half its design.css sibling had and this one
    did not, under prose arguing the rule generally: a licence for a rule that has moved is how an
    allowlist rots into a list of things somebody once wrote. Three times the size and spread over
    eight files, this is the list a component rewrite actually moves.
    [§6.8; decision 276 as amended; review cycle 2: M415-C2-CSS-07, acc-c2-04;
    row `map-taste-admin-palette-facet-binding-and-accent`]
    """
    sites = _live_accent_sites(FRONTEND)
    licensed = {**ACCENT_ALLOWLIST, **FACET_ACCENT_SITES}
    offenders = [site for site in sites if site not in licensed]
    assert not offenders, (
        "§6.8 spends the ember accent on selection and primary actions and on nothing else, and "
        "these sites are on neither list:\n  " + "\n  ".join(offenders) + "\n\nIf the site really "
        "is a selection or a primary action, add it to ACCENT_ALLOWLIST with the one line that "
        "says which of the two it is. If it is a status, an identity, a progress or a piece of "
        "text, design.css already has a token for it (--status, --identity, --progress-track / "
        "-fill / -now, --ink*): the accent is the only colour in this app that means something, "
        f"and it means it by being rare. Retired for that reason: {'; '.join(RETIRED_ACCENT_SITES)}."
        f" `var({ACCENT_ALIAS_TOKEN})` reaches this list too and is not a way around it: it is the "
        "same six hex digits under §6.4's binding, and FACET_ACCENT_SITES is where a spend argues "
        "it is the FACET rather than the accent."
    )
    gone = [site for site in licensed if site not in sites]
    assert not gone, (
        f"these are licensed to spend the accent and no longer spend it: {gone}. Drop the entry "
        "rather than leaving a licence behind for whatever is written at that selector next: the "
        "keys are `file: selector`, so the file that renames or deletes a rule inherits its own "
        "stale licence the moment it writes that name again."
    )


def test_the_accent_pattern_names_facet_mood_only_while_it_is_the_accent():
    """What the fourth spelling in `_ACCENT` rests on, asserted rather than assumed.

    `var(--facet-mood)` is read as a spend of the accent for exactly one reason: design.css binds
    `--facet-mood` to #c8613a, so the two names paint the same pixels and a sweep that reads only
    one of them can be satisfied by moving a colour rather than by retiring it. That is a fact
    about today's palette and not a rule -- decision 276 leaves the recolour to whoever ships §6.4,
    and proposal 124's warm end is a palette decision this milestone has no standing to take.

    So the alternative is held to its own premise. On the day mood gets a hue of its own, this
    fails first and says to drop the alternative: without it `_ACCENT` would go on demanding an
    accent licence for a colour that is no longer the accent, and `FACET_ACCENT_SITES`' one entry
    would be a licence for a spend nobody needs to argue for any more.
    [§6.8; §6.4's binding note; decision 276 as amended; review cycle 3: M415-C3-ACC-01]
    """
    assert ACCENT_ALIAS_TOKEN in _design_css_accent_names(_stylesheet(DESIGN)), (
        f"design.css no longer binds `{ACCENT_ALIAS_TOKEN}` to the accent hex, so `_ACCENT`'s "
        f"`var({ACCENT_ALIAS_TOKEN})` alternative now matches a DIFFERENT colour and every site "
        "in FACET_ACCENT_SITES is licensed against a rule that has moved. Drop the alternative "
        "and the list together: the pattern is spellings of one colour, and this was one of them."
    )


def test_design_css_spends_the_accent_where_the_app_selects_and_acts():
    """§6.8's other half, in the file the sentence was always about.

    The component scan above stops at `.svelte`, and that left the sheet every surface inherits
    from as the one place the accent could be widened with both guards green: a new
    `.badge { background: var(--ember) }` in design.css reaches every screen in the app at once,
    and `test_the_ember_accent_is_not_reused_as_a_neutral` -- the M0 guard that does read this file
    -- matches `--name: #c8613a` token DEFINITIONS, so it cannot see a use of any spelling. The
    row's own sentence reads as a rule about the app while its two tests between them read
    components and token names.

    Five sites, and the list is held in both directions. An unlisted spend is a widening nobody
    argued for; a listed site that is no longer there is a licence for a rule that has moved,
    which is how an allowlist rots into a list of things somebody once wrote.

    And the division of labour has to be exhaustive or it is a third hole, which is what acc-04's
    repair left behind: the component sweep dispatches on suffix and reads `.svelte` and `.js`, and
    this test reads ONE named path, so `frontend/src/lib/anything-else.css` -- imported from
    `+layout.svelte` exactly the way `$lib/design.css` already is in four route files -- would be
    read by neither, and reach every surface the same way the hole acc-04 closed did. The two
    readers are checked to cover the tree between them rather than assumed to, below. Note what
    this does NOT claim: `frontend/static/fonts/fonts.css` is a shipped stylesheet linked from
    `app.html` and lives outside this root; it is closed by
    `test_the_font_stylesheet_is_what_its_generator_emits`, which holds it byte-for-byte to
    `ops/fetch-fonts.py`, and not by any accent reader.
    [§6.8; decision 276 as amended; review cycle 1: acc-04; review cycle 3: M415-C3-ACC-03;
    row `map-taste-admin-palette-facet-binding-and-accent`]
    """
    stylesheets = sorted(p.relative_to(FRONTEND).as_posix() for p in FRONTEND.rglob("*.css"))
    assert stylesheets == ["lib/design.css"], (
        f"frontend/src holds stylesheets this guard does not read: {stylesheets}. The accent sweep "
        "dispatches on suffix and takes `.svelte` and `.js`; this test takes design.css by name, "
        "so a second sheet is read by neither and a global `.badge { background: var(--ember) }` "
        "in it reaches every surface with both guards green. Fold it into design.css, or widen "
        "both readers in the change that adds it."
    )
    inline = [
        block
        for block in re.findall(
            r"<style\b[^>]*>(.*?)</style>", _src(FRONTEND / "app.html"), re.S
        )
        if block.strip()
    ]
    assert not inline, (
        "frontend/src/app.html carries a <style> block, and no accent guard opens that file: the "
        "sweep skips it on suffix and this test reads design.css. It is the one document every "
        "surface is served inside, so a rule here outranks the component scan entirely."
    )

    sites = _design_css_accent_sites(_stylesheet(DESIGN))
    unlisted = [site for site in sites if site not in DESIGN_CSS_ACCENT_SITES]
    assert not unlisted, (
        "design.css spends the ember accent on selectors nobody has named: " + str(unlisted)
        + "\n\nThis is the app's own stylesheet, so a rule here reaches every surface at once. If "
        "it really is a selection or a primary action, add it to DESIGN_CSS_ACCENT_SITES with the "
        "line that says which; if it is a status, an identity, a progress or a piece of text, the "
        "file already mints --status, --identity, --progress-* and the ink ladder for it."
    )
    gone = [site for site in DESIGN_CSS_ACCENT_SITES if site not in sites]
    assert not gone, (
        f"these are licensed to spend the accent in design.css and no longer do: {gone}. Drop the "
        "entry rather than leaving a licence behind for whatever is written at that selector next."
    )


def test_the_design_css_accent_guard_tells_a_spend_from_a_definition():
    """The distinction the sheet's own `:root` turns on, shown rather than asserted in prose.

    `:root` holds `--ember: #c8613a` and `--facet-mood: #c8613a`, so a guard that read rule bodies
    would report the two lines that say what the colour IS as two spends of it -- and the reader
    sent to justify them has nothing to justify.

    Which means the division of this sentence into two readers is only as good as the WEAKER of
    them, and the seam between them is a name: a token is invisible to the spend reader by the
    rule asserted directly below, so everything a `--` declaration can hide rests on the name
    reader alone. Until review cycle 2 that reader was M0's lowercase hex literal, so
    `--badge-bg: rgb(200, 97, 58)` in `:root` -- with `.badge { background: var(--badge-bg) }`
    under it, reaching every surface in the app at once -- was a name to one half and a non-hex to
    the other, and the only thing between caught and uncaught was the colour space the author
    typed. `#C8613A` and the plain alias `var(--ember)` walked past it too, and the alias is the
    one a writer reaches for first because it needs no colour literal at all. Both halves read
    `_ACCENT` now, which is the same argument decision 276's amendment already makes about the
    ways a spend can be SPELLED, applied where it had not been.
    [§6.8; decision 276 as amended; review cycle 1: acc-04; review cycle 2: acc-c2-02]
    """
    css = _stylesheet(DESIGN)
    assert ":root" not in _design_css_accent_sites(css), (
        "a token definition is not a spend: naming the colour is this file's job, and the M0 guard "
        "test_the_ember_accent_is_not_reused_as_a_neutral is the half that reads the names"
    )
    widened = _design_css_accent_sites(css + "\n.badge { background: var(--ember); }\n")
    assert ".badge" in widened, "a new global spend in design.css is exactly what this must see"

    for spelling in ("#c8613a", "#C8613A", "rgb(200, 97, 58)", "var(--ember)"):
        minted = css.replace("--ember: #c8613a;", f"--ember: #c8613a;\n  --badge-bg: {spelling};")
        assert "--badge-bg" in _design_css_accent_names(minted), (
            f"a token minted as `{spelling}` is the accent under a second name, which is the one "
            "shape neither reader was looking at: a spend reader skips it because it is a name, "
            "and a name reader that enumerates spellings is one the next spelling walks past"
        )
    for derivative in ("rgba(200, 97, 58, 0.1)", "rgba(200, 97, 58, 0.35)"):
        minted = css.replace("--ember: #c8613a;", f"--ember: #c8613a;\n  --tint: {derivative};")
        assert "--tint" not in _design_css_accent_names(minted), (
            "the accent at an alpha is the accent QUOTED rather than spent -- --ember-wash and "
            "--ember-edge ARE that triple -- and a reader that called the quietest use of this "
            "colour its loudest would retire the two derivatives §6.8's own ramp is built from"
        )


def test_the_app_has_one_focus_ring_rule():
    """Proposals 127 and 131: dismissal and focus are the same clause of baseline hygiene.

    A keyboard needs to know where it is, and the prototype answered that question six times --
    six components with a ring of their own, six widths, six colours, six different ideas of
    whether a tap draws one. §6.8 answers it once, the way it answers the card's box model once,
    and the answer lives in design.css: `:focus-visible`, a 2px ember outline, offset by 2.

    So this reads two things. Nothing may suppress the ring -- `outline: none` on a focus rule is
    the commonest single way an app loses its keyboard, and it is invisible to every other test
    here because the app goes on working perfectly for a pointer. And no component may draw a
    second one, unless it needs something the global rule cannot express: ShelfRow's chevron is
    `opacity: 0` until its row is hovered, so it has to become visible before a ring around it
    means anything, and that is the shape of a real exception. A different width or a different
    colour is not -- which this now ASSERTS rather than only saying. The licence was read off the
    selector alone, so the one exception in the tree quietly carried a 1px ring under a rule that
    says 2px, and any future value at that selector would have passed too.

    Which bucket a site lands in is `_rings_and_removals`' answer rather than this body's, because
    the answer turns on the property and the body only had the value: it read a shadow ring's first
    offset as `outline: 0` and reported a second ring as a missing one.
    [§6.8; proposals 127, 131; M4.15 review cycle 1: m415-rev1-css-06; review cycle 2: ring-c2-01;
    row `platform-menus-and-overlays-dismiss`]
    """
    declared = {site: value for site, value, _ in _ring_sites("lib/design.css", _stylesheet(DESIGN))}
    global_ring = declared.get(GLOBAL_RING)
    assert global_ring and "var(--ember)" in global_ring, (
        f"design.css declares no global `:focus-visible` outline (found: {global_ring}), so every "
        "component that stopped drawing its own now has no ring at all"
    )

    sites = []
    for path in sorted(_frontend_sources()):
        if path.suffix not in {".svelte", ".css"}:
            continue
        sites += _ring_sites(path.relative_to(FRONTEND).as_posix(), _stylesheet(path))
    rings, suppressed = _rings_and_removals(sites)

    assert not suppressed, (
        "these take the focus ring away, and a control with no ring is a control a keyboard "
        f"cannot find: {suppressed}. §6.8's ring is `:focus-visible`, so it is already not drawn "
        "on a tap -- which is the only reason anyone has ever had for removing one."
    )
    competing, redrawn = _ring_offences(rings, global_ring)
    assert not competing, (
        "the app has one focus ring and it is declared in frontend/src/lib/design.css; these "
        f"declare a second: {competing}\n\nPut the change there if the whole app should have it, "
        "or add the selector to FOCUS_RING_EXCEPTIONS with the one line saying what the global "
        "rule cannot do for that control."
    )
    assert not redrawn, (
        "these are licensed in FOCUS_RING_EXCEPTIONS and draw a DIFFERENT ring, which is the one "
        f"thing the licence does not buy: {redrawn}\n\nAn exception exists for the property "
        "design.css cannot reach -- ShelfRow's `opacity: 1` on a chevron that is invisible until "
        "its row is hovered. The ring beside it restates the app's one ring; a second width or a "
        "second colour is a second answer to a question §6.8 answers once."
    )
    assert "outline-offset" in _css_rule(_stylesheet(DESIGN), ":focus-visible"), (
        "the global ring has no outline-offset: flush against a card's own border an outline "
        "reads as a border colour change rather than as focus"
    )


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        # The shapes the eight retired sites actually had.
        (
            "a status badge",
            "<style>.badge { background: var(--ember); color: var(--ember-ink); }</style>",
            ["x.svelte: .badge"],
        ),
        (
            "a progress fill",
            "<style>.fill { background: var(--ember); }</style>",
            ["x.svelte: .fill"],
        ),
        (
            "the raw hex, which no token audit would see",
            "<style>.code { color: #c8613a; }</style>",
            ["x.svelte: .code"],
        ),
        (
            "inside a media query, where the rule is still a rule",
            "<style>@media (pointer: coarse) { .tick.now { background: var(--ember); } }</style>",
            ["x.svelte: .tick.now"],
        ),
        (
            "one selector of a group, not the group",
            "<style>.a:hover,\n  .b:focus-visible { border-color: var(--ember); }</style>",
            ["x.svelte: .a:hover", "x.svelte: .b:focus-visible"],
        ),
        # The spellings that are the same token and the same colour, written differently.
        (
            "the token with a fallback, where the closing paren is not where it was",
            "<style>.chip { color: var(--ember, #ffffff); }</style>",
            ["x.svelte: .chip"],
        ),
        (
            "the token with whitespace inside its own parens",
            "<style>.chip { border-color: var( --ember ); }</style>",
            ["x.svelte: .chip"],
        ),
        (
            "the colour as a triple, which is one keystroke pattern from the hex",
            "<style>.badge { background: rgb(200, 97, 58); }</style>",
            ["x.svelte: .badge"],
        ),
        (
            "the triple at full strength, with its alpha spelled out",
            "<style>.badge { background: rgba(200, 97, 58, 1); }</style>",
            ["x.svelte: .badge"],
        ),
        # The spellings that are not a style block at all. The retired fills were unconditional
        # backgrounds, which is exactly what a style attribute can express -- and four of the
        # files the sweep touched already write colour in the markup, so this is the idiom a
        # returning spend would reach for rather than an exotic one.
        (
            "the same fill, moved into a style attribute",
            '<span class="badge" style="background: var(--ember)">cold</span>',
            ['x.svelte: <span class="badge">'],
        ),
        (
            "a style: directive, which is how this tree writes colour in markup",
            "<span class=\"avatar\" style:background={u.colour ?? 'var(--ember)'}></span>",
            ['x.svelte: <span class="avatar">'],
        ),
        (
            "the raw hex in the markup, where no token audit and no style scan would see it",
            '<div style="border-color: #c8613a"></div>',
            ["x.svelte: <div>"],
        ),
        # The four spellings a `[^<>"]` attribute run walked past, which are not exotic: every one
        # of them is an operator inside a Svelte expression, and the first two are how a retired
        # status fill comes back. The `<` case is the worst of them -- the old run could give back
        # no `>`, so the element was not truncated but DROPPED.
        (
            "an arrow ahead of the spend, which ends a non-brace-aware attribute run",
            "<button class=\"cta\" style:background={() => on ? 'var(--ember)' : 'var(--line)'}>x"
            "</button>",
            ['x.svelte: <button class="cta">'],
        ),
        (
            "the retired cold badge, returning behind a greater-than",
            "<span class=\"cold\" style:background={n > 3 ? 'var(--ember)' : 'var(--status)'}>"
            "</span>",
            ['x.svelte: <span class="cold">'],
        ),
        (
            "the same comparison the other way round, which dropped the element entirely",
            "<span class=\"cold\" style:background={n < 3 ? 'var(--ember)' : 'var(--status)'}>"
            "</span>",
            ['x.svelte: <span class="cold">'],
        ),
        (
            "a single-quoted class, which named the site `<span>` and merged it with every other",
            "<span class='badge' style:background={'var(--ember)'}></span>",
            ['x.svelte: <span class="badge">'],
        ),
        # The two shapes that look like spends and are not: the derivatives, and the binding.
        (
            "the accent quoted rather than spent",
            "<style>.err { color: var(--ember-lift); background: var(--ember-wash); }</style>",
            [],
        ),
        # Not a shape that looks like a spend and is not: the same six hex digits under a second
        # name, which is the one legal spelling of #c8613a this tree still had. The spend is
        # REPORTED and argued on `FACET_ACCENT_SITES` -- a facet stripe is not a selection, so the
        # licence has to say facet rather than borrow the allowlist's sentence.
        (
            "facet-mood, which is #c8613a by §6.4's binding, spent on something that is no facet",
            "<style>.dot { background: var(--facet-mood); }</style>",
            ["x.svelte: .dot"],
        ),
        (
            "the accent quoted rather than spent, in the markup too",
            '<p style="border-color: var(--ember-edge)">x</p>',
            [],
        ),
        (
            "--ember-wash written out as a triple is still the derivative, not the accent",
            "<style>.callout { background: rgba(200, 97, 58, 0.1); }</style>",
            [],
        ),
        (
            "markup that has been commented out is not markup",
            '<!-- <span style="background: var(--ember)"></span> -->',
            [],
        ),
        # The third place a component writes colour, which is the one the retired sites' own idiom
        # points at: `style:background={expr}` carries an EXPRESSION, and an expression holds no
        # literal for a colour pattern to match. The spend is one scope away, in the script block
        # this guard's markup reader strips.
        (
            "the fill bound to a constant in the component's own script",
            "<script>const FILL = 'var(--ember)';</script>\n"
            '<span class="badge" style:background={FILL}></span>',
            ["x.svelte: FILL"],
        ),
        (
            "a comment naming the colour is a comment",
            "<script>// the one accent is #c8613a, and nothing here spends it\n</script>",
            [],
        ),
    ],
)
def test_the_accent_guard_sees_each_way_a_retired_site_would_come_back(name, source, expected):
    """The guard above passes on a swept tree, so it is shown refusing each shape that was in it
    before the sweep -- and passing the two that look like spends and are not."""
    assert _accent_sites("x.svelte", source) == expected, name


def test_no_style_block_hides_a_brace_in_a_string():
    """`_rules` reads CSS with a regex, and this is the shape that regex cannot survive.

    Held here rather than repaired there, because the repair is a real parser and the exposure is
    one declaration. `content` is the only property in CSS whose value is routinely a string, and a
    brace in one truncates or shatters the rule for every reader built on `_rules`: the component
    accent sweep, both design.css readers, `_ring_sites`, `_coarse_declarations` and the padding
    reader. `.a::after { content: "}"; background: var(--ember); }` reports no accent spend at all
    -- a decoration nobody would look at twice, deleting the guard that rations §6.8's one colour.

    So the blind spot is kept out of reach instead of taught to the parser, which is the same trade
    `_accent_in_code` makes for a `//` inside a string and the same shape as the `button
    { min-height: auto }` hatch this file already forbids by name. It passes today -- the tree
    holds no `content:` declaration at all, only `justify-content` and `align-content` -- and
    reddens on the first one, at which point a reader has this docstring instead of six guards
    silently agreeing about a rule that is not there. [review cycle 3: M415-C3-GUARD-04]
    """
    hiding = []
    for path in sorted(FRONTEND.rglob("*")):
        if not path.is_file() or path.suffix not in {".svelte", ".css"}:
            continue
        styles = _stylesheet(path) if path.suffix == ".css" else _style_blocks(_src(path))
        # The value is read as declaration text OR a quoted string, and not as `[^;}]`, because
        # `[^;}]` stops at the brace this is looking for: on `content: "}"` it captures the opening
        # quote alone and the guard reports nothing. Reading the string as a unit is the same step
        # `_rules` does not take and is exactly why this test exists.
        for value in re.findall(
            r"(?<![a-z-])content\s*:\s*((?:[^;}'\"]|'[^']*'|\"[^\"]*\")*)", styles
        ):
            if "{" in value or "}" in value:
                hiding.append(f"{path.relative_to(FRONTEND).as_posix()}: content: {value.strip()}")
    assert not hiding, (
        "a brace inside a CSS string is invisible to `_rules`, which reads braces with a regex:\n  "
        + "\n  ".join(hiding)
        + "\n\nEverything built on that reader loses the rest of this rule -- the accent sweep, "
        "both design.css readers, the ring sweep, the coarse-pointer reader and the card-padding "
        "reader. Draw the glyph some other way, or teach `_rules` to match braces before you write "
        "it: a decoration is not worth six guards going quiet on the rule it sits in."
    )


def test_the_accent_sweep_reads_the_modules_beside_the_components(tmp_path):
    """A spend routed through a JS identifier, which is the one route out of a colour scan.

    `_accent_sites` reads two of the three places this app writes colour, and the third is where
    `style:background={expr}` gets its value from: an expression carries no literal, so the spend
    is one scope away in a script block the markup reader strips, or one file away in a module the
    walk never opened. `export const ACCENT = 'var(--ember)'` in `lib/theme.js`, bound on a status
    badge, passes every accent guard in this file -- the token list is untouched, design.css is
    untouched, and the component's own style block and markup hold no colour.

    Held open rather than papered over, in the same shape `_frontend_sources` already keeps: a
    colocated `*.test.js` names colours to ASSERT about them and is read by vitest, not by a
    palette audit. What a widened scan cannot see stays unseeable -- a colour computed at runtime,
    or one that arrives from the database the way `AccountChip`'s avatar does -- which is decision
    276's accepted boundary and not this one.
    [§6.8; decision 276 as amended; review cycle 2: M415-C2-CSS-07]
    """
    (tmp_path / "Badge.svelte").write_text(
        "<script>const FILL = '#c8613a';</script>\n"
        '<span class="badge" style:background={FILL}></span>\n',
        encoding="utf-8",
    )
    (tmp_path / "theme.js").write_text("export const ACCENT = 'var(--ember)';\n", encoding="utf-8")
    (tmp_path / "theme.test.js").write_text(
        "it('is the accent', () => expect(ACCENT).toBe('var(--ember)'));\n", encoding="utf-8"
    )
    (tmp_path / "quiet.js").write_text("export const TINT = 'var(--ember-wash)';\n", encoding="utf-8")

    assert _live_accent_sites(tmp_path) == ["Badge.svelte: FILL", "theme.js: ACCENT"], (
        "the sweep reads a component's script block and the modules beside it, skips the vitest "
        "file that names the colour in order to assert about it, and leaves the derivative alone"
    )


def test_a_component_licence_outliving_its_rule_is_an_offence(monkeypatch):
    """The direction the component allowlist was held in only one of.

    Its design.css sibling argues the rule generally -- "a listed site that is no longer there is a
    licence for a rule that has moved, which is how an allowlist rots into a list of things
    somebody once wrote" -- and then holds five entries in one file to it, while the sixteen over
    eight files, three times the size and the ones a component rewrite actually moves, were read in
    one direction only. The keys are `file: selector`, so a stale licence is inherited by the same
    file writing that selector again, which is exactly what a rewrite of that component does.
    [§6.8; decision 276 as amended; review cycle 1: acc-04; review cycle 2: acc-c2-04]
    """
    monkeypatch.setitem(
        ACCENT_ALLOWLIST,
        "lib/components/ShelfRow.svelte: .nudge:hover",
        "a selector that file does not carry: the licence has outlived the rule it licensed",
    )
    with pytest.raises(AssertionError, match="no longer spend it"):
        test_the_ember_accent_is_spent_only_on_selection_and_primary_actions()


@pytest.mark.parametrize(
    ("name", "styles", "expected"),
    [
        (
            "the ring taken away",
            "button:focus { outline: none; }",
            [("x.css: button:focus", "none", "outline")],
        ),
        (
            "the ring taken away by a longhand",
            ".chip:focus-visible { outline-style: none; }",
            [("x.css: .chip:focus-visible", "none", "outline")],
        ),
        (
            "a second ring, in a component",
            ".tile:focus-visible { outline: 1px dashed #fff; }",
            [("x.css: .tile:focus-visible", "1px dashed #fff", "outline")],
        ),
        (
            "an offset is not a ring",
            ":focus-visible { outline-offset: 4px; }",
            [],
        ),
        (
            "a hover/focus pair that moves a border is not a ring",
            ".side:hover, .side:focus-visible { border-color: var(--ember); }",
            [],
        ),
        (
            "a ring drawn with a shadow, which follows a radius an outline did not",
            ".tile:focus-visible { box-shadow: 0 0 0 2px var(--ember); }",
            [("x.css: .tile:focus-visible", "0 0 0 2px var(--ember)", "box-shadow")],
        ),
        (
            "a shadow that is a shadow: no focus in the selector, so no ring",
            ".menu { box-shadow: 0 16px 40px rgba(0, 0, 0, 0.6); }",
            [],
        ),
        (
            "a shadow taken off a focus rule is a shadow removed, not a ring removed",
            ".chip:focus-visible { box-shadow: none; }",
            [],
        ),
    ],
)
def test_the_focus_ring_guard_tells_a_ring_from_a_border(name, styles, expected):
    """Every shape this guard has to tell apart: the two ways a ring is taken away, a second ring,
    and the two that are neither -- the offset the global rule itself carries, and the four
    components that answer `:focus-visible` with the same border their `:hover` draws.

    Each site carries the property it was written with, which is load-bearing rather than
    descriptive: `outline: 0` is the ring taken away and `0 0 0 2px ...` is a shadow's first
    offset, so nothing downstream can tell a removal from a ring without it.
    [review cycle 2: ring-c2-01]"""
    assert _ring_sites("x.css", styles) == expected, name


def test_a_licensed_exception_may_restate_the_ring_and_not_redefine_it():
    """The case the licence could not refuse, which is why it shipped.

    `FOCUS_RING_EXCEPTIONS` is keyed by site, and the filter it feeds read the site and discarded
    the value -- so the one licensed selector in the tree carried `1px solid var(--ember)` while
    the rule beside it said 2px, and a later edit could have put any width and any colour there
    under the same licence. The licence is for a PROPERTY design.css cannot reach from outside a
    component, never for a different answer to the question §6.8 answers once.
    [proposals 127, 131; M4.15 review cycle 1: m415-rev1-css-06]
    """
    one_ring = "2px solid var(--ember)"
    licensed = next(iter(FOCUS_RING_EXCEPTIONS))

    assert _ring_offences([f"{GLOBAL_RING} -> {one_ring}"], one_ring) == ([], [])
    assert _ring_offences([f"{licensed} -> {one_ring}"], one_ring) == ([], [])
    # Whitespace is not a redefinition; a width is, and so is a colour.
    assert _ring_offences([f"{licensed} -> 2px  solid   var(--ember)"], one_ring) == ([], [])

    _, redrawn = _ring_offences([f"{licensed} -> 1px solid var(--ember)"], one_ring)
    assert len(redrawn) == 1 and "1px solid var(--ember)" in redrawn[0]
    _, recoloured = _ring_offences([f"{licensed} -> 2px solid #ffffff"], one_ring)
    assert len(recoloured) == 1

    competing, redrawn = _ring_offences(["lib/components/Other.svelte: .x:focus-visible -> "
                                         + one_ring], one_ring)
    assert competing and not redrawn, (
        "an unlicensed site drawing the SAME ring is still a second rule: it survives a change to "
        "design.css that the global rule's own consumers do not"
    )


def test_a_shadow_ring_is_a_second_ring_and_not_the_ring_taken_away():
    """The bucketing, which was the one piece of ring logic living in a test body.

    `_RING_SUPPRESSED` reads an OUTLINE value, and `_SHADOW_NONE` exists four lines under it saying
    in as many words why it must not be pointed at a shadow: every shadow ring in the idiom begins
    with an offset of `0`, so the removal pattern reads `0 0 0 2px var(--ember)` as the ring being
    taken away. The guard then applied it to every value `_ring_sites` returned, shadows included
    -- so the commonest modern way to DRAW a second ring was reported under the message for losing
    one, and the reader was sent to undo a removal nobody had made. Nothing wrong could ship on it,
    because both buckets are asserted and both are red; what shipped was the wrong sentence, and in
    a file whose whole output is sentences that is the product.

    Routed by PROPERTY, because no value tells the two apart: a leading `0` is a suppression on an
    outline and an offset on a shadow.

    What the licence still refuses is asserted here rather than left implied. `_ring_offences`
    holds an exception to the global ring's VALUE and the global ring is an outline, so a licensed
    shadow ring lands in `redrawn` even with the bucketing repaired -- which is the right answer
    and not a second defect: a radius-following ring is a second answer to the question §6.8
    answers once, and the licence buys the property design.css cannot reach from outside a
    component, never a different ring. [§6.8; proposals 127, 131; review cycle 2: ring-c2-01]
    """
    shadow = _ring_sites("x.svelte", ".t:focus-visible { box-shadow: 0 0 0 2px var(--ember); }")
    assert shadow == [("x.svelte: .t:focus-visible", "0 0 0 2px var(--ember)", "box-shadow")]

    rings, removed = _rings_and_removals(shadow)
    assert removed == [], (
        "a shadow ring begins with an offset of 0 and was read as `outline: 0`: the site that DREW "
        "a ring is reported under the message for the one thing it did not do"
    )
    assert rings == ["x.svelte: .t:focus-visible -> 0 0 0 2px var(--ember)"]

    # Zero of any unit, because the unit is not the rule and `\b` could not see past one: `0px`
    # is the commonest way after `none` to kill a ring and it was bucketed as a second ring being
    # DRAWN. `0 solid red` is the same zero with the shorthand's other two parts after it.
    for taken in ("none", "0", "0px", "0em", "0rem", "0vh", "0%", "0.0px", "0 solid red"):
        sites = _ring_sites("x.css", f".chip:focus-visible {{ outline: {taken}; }}")
        assert _rings_and_removals(sites)[1] == [f"x.css: .chip:focus-visible -> {taken}"], (
            f"`outline: {taken}` takes the ring away, and a control with no ring is a control a "
            "keyboard cannot find -- reported as a second ring, the reader is sent to move a rule "
            "into design.css instead of to put the ring back"
        )
    # And the converse, which the same boundary got wrong in the other direction: `.` is not a
    # word character, so a sub-pixel hairline satisfied `^0\b` and a ring that DRAWS was reported
    # under the message for one that was removed.
    for drawn in ("0.5px solid var(--ember)", "2px solid var(--ember)"):
        sites = _ring_sites("x.css", f".chip:focus-visible {{ outline: {drawn}; }}")
        assert _rings_and_removals(sites)[0] == [f"x.css: .chip:focus-visible -> {drawn}"], (
            f"`outline: {drawn}` draws a ring, however thin, and was read as one being taken away"
        )

    licensed = next(iter(FOCUS_RING_EXCEPTIONS))
    _, redrawn = _ring_offences([f"{licensed} -> 0 0 0 2px var(--ember)"], "2px solid var(--ember)")
    assert len(redrawn) == 1, (
        "a licensed site may restate the one ring and may not draw a rounder one: the licence is "
        "for `opacity: 1` on an invisible chevron, not for a second answer to §6.8's question"
    )


# --- M4.15 review cycle 1: the coarse-pointer floor, where design.css cannot reach ------------

SVELTE_CONFIG = REPO / "frontend" / "svelte.config.js"
COARSE = "@media (pointer: coarse)"
TOUCH = "var(--touch)"


def _coarse_blocks(styles: str) -> str:
    """Everything inside a stylesheet's `@media (pointer: coarse)` blocks, concatenated.

    Brace-matched rather than regexed to the first `}`, because these blocks hold whole rules and
    a lazy match would return the first declaration list and call the rest of the block absent.
    """
    out = []
    for opener in re.finditer(re.escape(COARSE) + r"\s*\{", styles):
        depth, cursor = 1, opener.end()
        while cursor < len(styles) and depth:
            if styles[cursor] == "{":
                depth += 1
            elif styles[cursor] == "}":
                depth -= 1
            cursor += 1
        out.append(styles[opener.end():cursor - 1])
    return "\n".join(out)


def _coarse_declarations(path: Path, selector: str) -> dict[str, str]:
    """What a component gives one selector inside its coarse-pointer blocks.

    Through `_rules`, not `_css_rule`, because these sites are written as comma-separated groups:
    `.empty a` shares a rule with six other selectors in Rank's sheet, and a guard that only
    matched a selector standing alone would report the repaired rule as missing.
    """
    found: dict[str, str] = {}
    for candidate, body in _rules(_coarse_blocks(_stylesheet(path))):
        if candidate == selector:
            found.update(_declarations(body))
    return found


# Every interactive control this app draws that `design.css`'s coarse block cannot reach, and the
# axis each one was short on -- or, since review cycle 2, an axis its own component declares under
# a coarse block and nothing else holds. That block names `.pill, .btn-primary, .btn-ghost,
# button, select,
# [role='button']` and raises `min-height` only -- so a bare `<a>` is outside the selector list
# entirely (deliberately: adding one would grow every inline prose link), and every `button` in
# the app clears a height-only assertion by construction while its narrow axis goes unmeasured.
# None of these is an inline prose link: two are standalone flex-item CTAs, four are chrome, two
# are the escape hatch on the §4 loop's own card and one is a bordered, padded, radiused chip --
# which is what separates every one of them from `AccountChip.svelte`'s "Set yours on the account
# page." and keeps the design.css refusal intact.
#
# Review cycle 3 adds the last three, and they are the two gaps stated above meeting the two
# surfaces the phone project actually opens. `.text` is a `button`, so the coarse block reaches it
# on the axis it sets and not on the one that was short; `.trailer` is a bare `<a>`, so the block
# never reaches it at all. Neither was held anywhere else: `rate-skip`, `rate-battle-skip` and the
# trailer link appear in no e2e assertion, and `11-rate.spec.js` is not in the phone project's
# `testMatch` at all -- though `02-shell` and `14-tonight` both ARE, and both open /rate on an
# iPhone 13, so the 25 px control is painted on the primary form factor during a phone run and
# measured by nothing. [review cycle 3: M415-C3-CSS-01, M415-C3-COMP-01]
#
# M4.16 adds the tenth, and it is the first entry on a surface built AFTER the rule existed.
# Decision 293's Data sources block draws three licence deeds as bare `<a>`s wearing `.data`, so
# design.css reaches them with none of its six primitives and sizes them at 11 px under a coarse
# pointer. The component declares the floor for itself and argues for it in a comment that names
# `19-phone-shell.spec.js`'s `meetsTheTouchFloor` as the instrument -- which never measures this
# file: that helper's six calls stop at the wizard, and `06-responsive.spec.js`'s sweep reaches
# the nav links and the kind toggles and never opens /account. A floor a component asks for and
# nothing holds is the shape this tuple exists for.
#
# BOTH AXES ARE ENTERED, which cycle 1 decided against and cycle 2 reverses. The argument then was
# this tuple's own header -- these are the axes a control was SHORT on -- and `CC BY-SA 4.0` is
# twelve characters of a mono face that clears 48 wide on content alone, so the width was said to
# be "held by the string rather than by the rule". A string is content, not a floor. The component
# declares `min-width: var(--touch)` and argues for it in the comment beside it, and NOTHING in
# this repository read that declaration: deleting it as redundant was invisible to every test
# here and to every sweep in the browser suite, and then one copy edit inside section 6.8's quiet
# register -- `CC BY-SA 4.0` to `deed` -- puts three controls under the floor with the whole gate
# green. The two-step is what makes it worth an entry, because the first step is the one that
# looks harmless. So the header is widened rather than this entry excused: an axis a component
# declares inside a coarse block is one this rule holds, whether the control was short on it or
# not. [M4.16 cycle 1, M416-C1-D3-05; M4.16 cycle 2, M416-C2-ATTR-03]
#
# M5.6's Data surface adds the last entries, and they are the controls plan E3 names as the ones
# that get drawn at 16 px: the flywheel's row and provider checkboxes, whose labels carry the whole
# target because design.css's block reaches neither a label nor a checkbox; the ledger editors'
# export downloads, which are bare `<a>`s; and the board's stage picker, a `select` the block
# raises on height only. `20-admin-data.spec.js` measures the row labels and Launch on the phone;
# the rest render only where a household row or a provider exists, so this is what holds them.
# [§6 preamble; M5.6 plan E3]
COARSE_FLOOR_SITES = (
    (
        "frontend/src/routes/+layout.svelte",
        "a.nobundle",
        "min-height",
        "the header's no-bundle badge is an admin's door out of a bundle-less app, on the screen "
        "a brand-new household spends its first session on, and it measured 137 by 25",
    ),
    (
        "frontend/src/lib/components/NavRail.svelte",
        "a",
        "min-height",
        "the six surface links are this app's whole switcher and the one bare `<a>` in its chrome; "
        "the floor was keyed on `max-width: 720px` instead of on the pointer, so on every iPad in "
        "either orientation -- and on an iPhone 13 turned sideways at 844 -- they measured 40 tall "
        "while every button, pill and input beside them was 48",
    ),
    (
        "frontend/src/routes/rank/+page.svelte",
        ".empty a",
        "min-height",
        "Rank's empty-state CTA is a 48px button in the `no-match` branch and a 17px anchor in "
        "the `fitting`/`unrated`/`thin` ones, so the floor held or not depending on which "
        "sentence emptyState() returned",
    ),
    (
        "frontend/src/lib/components/ModelRail.svelte",
        ".chip",
        "min-width",
        "the rail's kind filters are three and four characters of a 10px mono face -- 48 tall and "
        "36 wide, on the surface §6.7 calls the primary M2 debugging instrument",
    ),
    (
        "frontend/src/lib/components/ModelRail.svelte",
        ".close",
        "min-width",
        "the rail's exit measured 48 by 32",
    ),
    (
        "frontend/src/lib/components/TitleDetail.svelte",
        ".close",
        "min-width",
        "the title panel's exit measured 48 by 32, and it is the only way out of a panel that "
        "covers a phone",
    ),
    (
        "frontend/src/lib/components/RateSweepCard.svelte",
        ".text",
        "min-width",
        "proposal 38's `skip` is four characters of an 11.5 px mono face with zero horizontal "
        "padding, so the sweep card's escape hatch measured 48 tall and 25 wide -- 53% of the "
        "floor on its narrow axis, with `not seen` 18 px away and that one writes a §4.2 "
        "observation",
    ),
    (
        "frontend/src/lib/components/RateBattleCard.svelte",
        ".text",
        "min-width",
        "the same four characters and the same rule on the duel card, where the strip they sit "
        "in wraps, so the two halves of §4's most repeated interaction fail the floor together",
    ),
    (
        "frontend/src/lib/components/TitleDetail.svelte",
        ".trailer",
        "min-height",
        "§6.0's trailer affordance is a bordered inline-flex chip of an 11 px mono face -- 183 "
        "wide and 26 tall, inside the overlay whose only other exit this milestone repaired, and "
        "a bare `<a>` so design.css's list cannot reach it on either axis",
    ),
    (
        "frontend/src/lib/components/DataSources.svelte",
        ".licence",
        "min-height",
        "decision 293's three licence deeds are bare `<a>`s wearing `.data`, which design.css "
        "sizes at 11 px under a coarse pointer and reaches with none of its six primitives -- so "
        "without this rule a deed is its own line box and nothing more, on the one surface §6.8 "
        "makes every signed-in member reach and no sweep in the browser suite opens",
    ),
    (
        "frontend/src/lib/components/DataSources.svelte",
        ".licence",
        "min-width",
        "the same three deeds on the narrow axis, which is the axis every real failure in this "
        "app has been on -- two overlay exits at 48 by 32, a rail filter at 48 by 36. This one is "
        "not short today: `CC BY-SA 4.0` measures about 79 px of an 11 px mono face, and the "
        "component declares the floor anyway. What the entry holds is the declaration, against "
        "the day somebody deletes it as redundant and somebody else shortens the deed text",
    ),
    (
        "frontend/src/lib/components/FlywheelQueue.svelte",
        ".pick",
        "min-height",
        "a queue row's selection is a native checkbox inside a label, drawn at the engine's 13-16 px "
        "and reached by no rule in design.css, on the card where one tap can select a batch that "
        "spends money (plan E3)",
    ),
    (
        "frontend/src/lib/components/FlywheelQueue.svelte",
        ".pick",
        "min-width",
        "the same label on the narrow axis, which a lone checkbox is short on by construction",
    ),
    (
        "frontend/src/lib/components/FlywheelQueue.svelte",
        ".provider",
        "min-height",
        "the batch's provider toggles are the same checkbox-in-a-label, and choosing one changes "
        "what the batch reserves against the cap",
    ),
    (
        "frontend/src/lib/components/FlywheelQueue.svelte",
        ".provider",
        "min-width",
        "the same toggles on the narrow axis; `gemini` is six characters and holds the width "
        "today by content, which a string is not",
    ),
    (
        "frontend/src/lib/components/LedgerEditor.svelte",
        ".export",
        "min-height",
        "each ledger's export is a bare `<a download>`, which design.css's coarse list does not "
        "name, beside 48 px buttons a thumb is already aimed at",
    ),
    (
        "frontend/src/lib/components/LedgerEditor.svelte",
        ".export",
        "min-width",
        "the same downloads on the narrow axis, declared rather than left to the link text",
    ),
    (
        "frontend/src/lib/components/AcquisitionBoard.svelte",
        ".from select",
        "min-width",
        "the board's stage picker is a `select`, which design.css raises on height and never on "
        "width, and it is the control a retry from stage N is aimed with",
    ),
    (
        "frontend/src/lib/components/FlywheelQueue.svelte",
        ".passes select",
        "min-width",
        "the batch's pass picker is a `select` showing one digit, about 41 px wide by content on "
        "an iPhone 13, and it is the control that doubles what a batch reserves against the cap "
        "when it goes from 1 to 2 [M5.6 review cycle 1, M56-DATA-04]",
    ),
)


def test_every_control_the_coarse_selector_list_misses_declares_the_floor_itself():
    """§6 preamble: "48 px targets", on both axes and for every control -- not only the ones a
    selector list happens to name.

    `design.css`'s coarse block raises `min-height` on six primitives. That leaves two gaps and
    both of them shipped: a bare `<a>` matches none of the six, and nothing in that block ever
    sets a width, so every control it does reach clears a height-only assertion by construction.
    This app's real failures were all on the axis nobody measured -- two overlay exits at 48 by
    32, a rail filter at 48 by 36, a header badge 25 tall and a Rank CTA 17.

    AND THE DECLARATION HAS TO LIVE WHERE THE RULE IT COMPLETES LIVES. `min-width: var(--touch)`
    written outside a coarse block makes the control 48 wide and 32 tall on a mouse -- the same
    lopsidedness rotated ninety degrees -- and in `TitleDetail` it also pushed the hit area 12px
    over the end of the heading beside it. §6's preamble writes the floor for fingers; a pointer
    has no such threshold, and a rule that fires for both is not that rule.

    The e2e sweep measures what it can reach on the phone project, and these are the ones it
    cannot: the badge renders only where `/config` says there is no bundle (phase 2 asserts there
    is one), Rank's CTA needs a member below the tier threshold, and the rail's filter row is
    `{#if kinds.length > 1}`. A control a sweep steps over is a control a sweep cannot hold, which
    is the argument decision 280 already makes for the wizard's hairline.

    The three licence deeds are the exception to that sentence and are held here anyway. They are
    unconditional markup on a surface `19-phone-shell.spec.js` already opens on the phone project,
    so the stronger holder is a browser measurement and this is the weaker one: it reads what the
    component DECLARES rather than what an engine computes. It is here because a source rule is
    what this milestone can measure and a browser rule is what it cannot, and because the axis
    nobody held was the one every real failure has been on.
    [§6 preamble; row `platform-every-touch-target-meets-the-token`; M4.15 review cycle 1;
     M4.16 cycle 2, M416-C2-ATTR-03]
    """
    missing = []
    for where, selector, prop, why in COARSE_FLOOR_SITES:
        declared = _coarse_declarations(REPO / where, selector).get(prop)
        if declared != TOUCH:
            missing.append(f"{where}: {COARSE} {{ {selector} {{ {prop}: {declared} }} }} -- {why}")
    assert not missing, (
        "these controls take no touch floor on the pointer the floor is written for:\n  "
        + "\n  ".join(missing)
        + f"\n\nDeclare the missing axis as `{TOUCH}` in the component, inside its own `{COARSE}` "
        "block. Not in design.css's coarse list: a bare `a[href]` there would grow every inline "
        "prose link, and a blanket min-width would reach every narrow control in the app at once."
    )


# The other coarse rule with the same reach problem, and the sites that answer for themselves.
#
# `design.css`'s coarse block writes `.data { font-size: 11px }` and argues for it in a sentence -
# "10 px of a narrow mono face on a phone is under the floor for reading a digit correctly, and a
# misread digit is worse than no digit". A bare `.data` is (0,1,0) and a media query adds no
# specificity, while Svelte compiles every component rule to `.sel.svelte-hash` at (0,2,0) - so
# any scoped rule that sizes a `.data` element defeats it, silently and everywhere at once. The
# sibling rule fourteen lines above it (16 px against Safari's focus zoom) answers the same trap
# with per-component overrides AND a computed-size sweep on the phone; this one answers it here,
# because a badge 2 px under the floor is a legibility cost and not a control a member is stranded
# on.
#
# An entry is the OTHER answer to the same question, and it costs a sentence saying which: raising
# the site in the file's own coarse block is the first. Two raise themselves and both are prose,
# not overlays: /login's divider and the Users page's group heading. Five are named below, four of
# them M4.9's corner overlays under proposal 29's geometry, and the fifth a `.data` Tonight
# re-declares at 12 px on the line after - none of them prose, none of them a number a household
# has to read off a scrim to act.
#
# Both numerals are re-derived by `test_the_data_voice_counts_this_record_publishes_are_the_ones_
# it_measures`, because the sentence they replace read "Both entries here" over a list of five and
# design.css carried the identical stale numeral one file over. A reader adding a sixth reads a
# numeral as the composition, and decision 184's rule -- the run that produced it, or nothing --
# is a rule about counts as much as about milliseconds. [decision 184; review cycle 3:
# M415-C3-CSS-03]
DATA_VOICE_FLOOR_PX = 11.0
_FONT_SIZE = re.compile(r"(?<![a-z-])font-size\s*:\s*([0-9.]+)px")
DATA_VOICE_EXEMPT = {
    ("frontend/src/lib/components/PosterCard.svelte", ".badge"): (
        "proposal 29's overlay geometry: a 9px chip in the corner of a 2:3 poster, reading `new`, "
        "whose sentence the surface carries in full (decision 278) - the chip is a marker, not the "
        "digit the floor is written for"
    ),
    ("frontend/src/lib/components/PosterCard.svelte", ".seen"): (
        "the same overlay, the same word-length, the opposite corner"
    ),
    ("frontend/src/lib/components/ShelfRow.svelte", ".rank"): (
        "M4.9 sized the four corner overlays together under proposal 29 (`new`/`seen` on top, rank "
        "and tier below) and raising one of them alone breaks the set; the rank is also the one "
        "number on the card that is legible from the row's ORDER without reading it"
    ),
    ("frontend/src/lib/components/ShelfRow.svelte", ".tierbadge"): (
        "the fourth corner of that same set"
    ),
    ("frontend/src/routes/tonight/+page.svelte", ".label"): (
        "not defeated at all: this file re-declares `.data` at 12px on the line AFTER it, and the "
        "two tie on specificity, so every `class=\"data label\"` element on Tonight computes 12 - "
        "listed so the scan's own answer is on the page rather than in a reader's head"
    ),
}


def test_the_data_voices_coarse_floor_reaches_every_element_it_claims():
    """§6 preamble, on the rule design.css states for the data voice and cannot enforce alone.

    Specificity, not intent, decides this - the same sentence the 16 px rule above it already
    carries, and the same trap. What is different is the answer: the 16 px rule gets a computed-
    size sweep on the phone project because focus zoom strands a member on a magnified page with
    no gesture to undo it, and a `.data` size is a legibility cost with no such cliff. So it is
    held at the source, where every file is reachable including the ones no browser test loads.

    The reach test is the LAST compound of the selector, because `.sub h3` styles an `h3` inside a
    `.sub` and a whole-selector match would call that rule unreachable. A file that raises the
    same selector inside its own `@media (pointer: coarse)` block has answered; anything else has
    to be named above, with the reason it is the exception. [§6 preamble; decision 275;
    row `platform-quiet-reasons-are-prose-and-legible`; M4.15 review cycle 2: M415-C2-CSS-04]
    """
    unheld = []
    for path in sorted(FRONTEND.rglob("*.svelte")):
        source = _src(path)
        reachable = _selectors_that_style(source, "data")
        if not reachable:
            continue
        reachable.add(".data")
        where = path.relative_to(REPO).as_posix()
        styles = _style_blocks(source)
        raised = {
            selector
            for selector, body in _rules(_coarse_blocks(styles))
            if (found := _FONT_SIZE.search(body))
            and float(found.group(1)) >= DATA_VOICE_FLOOR_PX
        }
        for selector, body in _rules(styles):
            if selector not in reachable and re.split(r"[\s>+~]+", selector)[-1] not in reachable:
                continue
            size = _FONT_SIZE.search(body)
            if not size or float(size.group(1)) >= DATA_VOICE_FLOOR_PX:
                continue
            if selector in raised or (where, selector) in DATA_VOICE_EXEMPT:
                continue
            unheld.append(f"{where}: {selector} {{ font-size: {size.group(1)}px }}")
    assert not unheld, (
        "these rules outrank design.css's coarse `.data` floor and leave the element under it:\n  "
        + "\n  ".join(unheld)
        + f"\n\nRaise the selector to at least {DATA_VOICE_FLOOR_PX:.0f}px inside the file's own "
        f"`{COARSE}` block, after the rule it overrides so source order decides the tie - or add "
        "it to DATA_VOICE_EXEMPT with the reason it is one. The rule in design.css says a misread "
        "digit is worse than no digit; a rule that cannot reach the digit does not say it."
    )


def test_the_data_voice_counts_this_record_publishes_are_the_ones_it_measures():
    """Decision 184 applied to a count, which is a measurement with a smaller number in it.

    The rule design.css states for the data voice is answered two ways -- a file raises the site in
    its own coarse block, or the site is named as an exemption -- and both records describing the
    split publish the size of each side as a word. The sentence that stood here opened "Both
    entries here" over a list of FIVE, and the twin sentence in design.css had drifted identically
    in the same cycle. That is the shape `test_every_contrast_figure_the_record_publishes_is_the
    _one_it_measures` already refuses for a ratio: a figure that was true when it was written and
    that nothing re-derives is a figure the record keeps publishing after it stops being true.

    Two files and not four. `spec_coverage.toml` and the proposals register publish the same split
    in a different form ("seven rules: two raised in place, four overlays, the seventh Tonight's
    `.label`") and both were correct; what drifted is this sentence, in the two places that carry
    it word for word, so this is what those two are held to.
    [decision 184; decision 275; §6 preamble; review cycle 3: M415-C3-CSS-03]
    """
    raised, exempt = [], []
    for path in sorted(FRONTEND.rglob("*.svelte")):
        source = _src(path)
        reachable = _selectors_that_style(source, "data")
        if not reachable:
            continue
        reachable.add(".data")
        where = path.relative_to(REPO).as_posix()
        styles = _style_blocks(source)
        answered = {
            selector
            for selector, body in _rules(_coarse_blocks(styles))
            if (found := _FONT_SIZE.search(body))
            and float(found.group(1)) >= DATA_VOICE_FLOOR_PX
        }
        for selector, body in _rules(styles):
            if selector not in reachable and re.split(r"[\s>+~]+", selector)[-1] not in reachable:
                continue
            size = _FONT_SIZE.search(body)
            if not size or float(size.group(1)) >= DATA_VOICE_FLOOR_PX:
                continue
            if selector in answered:
                raised.append(f"{where}: {selector}")
            elif (where, selector) in DATA_VOICE_EXEMPT:
                exempt.append(f"{where}: {selector}")

    # Each phrase carries enough of its own sentence to be the one being counted: a bare
    # `are named` reads four unrelated sentences in this file, and a guard that greps a common
    # phrase is a guard that reddens on prose it was never about.
    words = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
    measured = {
        r"raise themselves and both are prose": len(raised),
        r"are named(?: below)?, four of them": len(exempt),
    }
    assert len(exempt) == len(DATA_VOICE_EXEMPT), (
        f"DATA_VOICE_EXEMPT holds {len(DATA_VOICE_EXEMPT)} entries and {len(exempt)} of them are "
        f"reached by the sweep: {sorted(set(DATA_VOICE_EXEMPT) - {tuple(e.split(': ')) for e in exempt})}"
        ". An exemption nothing reaches is a licence for a rule that has moved."
    )
    wrong, missing = [], []
    for path in (DESIGN, Path(__file__).resolve()):
        where = path.relative_to(REPO).as_posix()
        # Comment markers off and whitespace collapsed before the phrases are read, because both
        # records carry these sentences wrapped inside a comment block and a count must not be
        # able to leave this guard's reach by moving two words onto the next line. The opposite
        # trade from `_PUBLISHED_RATIO`, which reads figures written one per line on purpose:
        # there the line IS the form, here the sentence is, and a reflow is not an edit.
        text = " ".join(re.sub(r"(?m)^[ \t]*#", " ", path.read_text(encoding="utf-8")).split())
        for phrase, count in measured.items():
            published = re.findall(rf"(\w+) {phrase}", text)
            # Per FILE and not pooled, because the defect this exists for was one record drifting
            # while its twin was right: a floor counting both together is discharged by the file
            # that did not drift. And a numeral rephrased out of the form is a numeral nothing
            # re-derives, which is the one way to satisfy a guard like this without obeying it --
            # the stale sentence here opened "Both entries here" and carried neither phrase.
            if not published:
                missing.append(f"{where}: no sentence in the form `<count> {phrase}`")
            wrong += [
                f"{where}: publishes `{one}` where {words[count]} rules do"
                for one in published
                if one.lower() != words[count]
            ]
    assert not missing, (
        "these records describe the data voice's two answers and no longer publish the size of "
        "each side in the form this guard re-derives:\n  " + "\n  ".join(missing)
        + "\n\nKeep the form, or take the sentence out of both files in the same change. A count "
        "that reads as prose is the half of decision 184 a rephrase walks past."
    )
    assert not wrong, (
        "the record publishes a count of these rules that is not the count it holds:\n  "
        + "\n  ".join(wrong)
        + "\n\nRestate it from the list. A reader adding a sixth exemption reads the numeral as "
        "the composition, and the two sentences this guard reads are the argument for what each "
        "side of the split is FOR -- one raises itself, the other is named with its reason."
    )


def test_the_title_panels_heading_clears_its_close_control():
    """The other half of that repair, and the one no measurement of the BUTTON can see.

    `.close` is absolutely positioned at `right: 16px` inside the panel's 20px padding, so on a
    coarse pointer its box begins `--touch` in from the text edge. The heading beside it reserved
    32px, which was right while the control was 32 wide and wrong the moment it was not: the last
    characters of a long title sat under a transparent hit area, and a thumb aimed at them
    dismissed the panel. Exit criterion 4 passes on that control the whole time, because it
    measures the button and never what the button is standing on. [§6 preamble]
    """
    margin = _declarations(_css_rule(_stylesheet(TITLE_DETAIL), "h2")).get("margin", "")
    parts = _shorthand_parts(margin)
    assert len(parts) == 4 and parts[1] == TOUCH, (
        f"the title panel's heading reserves `{margin}` on its right: the close control's box is "
        f"{TOUCH} wide on a coarse pointer and starts at the panel's padding edge, so anything "
        "narrower puts the end of a long title under a transparent hit area"
    )


def test_the_wizard_paints_its_steps_from_the_progress_ramp():
    """§6.8, and design.css's own sentence about the eight fills the accent stopped playing:
    "None of these is a new hue: a second accent would be the same violation with a different hex,
    and a facet colour may not be borrowed either, because a facet colour is an identity that
    means one vocabulary term on every surface it appears."

    design.css mints `--progress-track / -fill / -now` for three named consumers -- "Rate's rail
    fill, its block ticks, the first-boot wizard's steps" -- and the wizard was spending one of
    the three and writing the other two out by hand: the track as the raw value behind its token,
    and the recorded step as `#5fae7a`, which is `--facet-characters`. A household that has seen
    the Map or a DNA chip has been taught that that green means one vocabulary term, and on the
    first screen it was being used to mean "step done".

    The ember scan cannot see this: it matches `var(--ember)` and `#c8613a` and nothing else, so a
    component may take any OTHER token's colour raw and the build stays green. Widening it to the
    eleven facet hexes is not this milestone's to do -- four further sites carry the same green as
    an ok/success colour and one of them is the sibling lane's file (decision 270). What IS this
    milestone's is the ramp it minted, held on the consumer design.css names.
    [§6.8; decision 276; row `map-taste-admin-palette-facet-binding-and-accent`]
    """
    styles = _stylesheet(SETUP_PAGE)
    ramp = {
        ".progress button": ("background", "var(--progress-track)"),
        ".progress button.done": ("background", "var(--progress-fill)"),
        ".progress button.on:not(.done)": ("background", "var(--progress-now)"),
    }
    wrong = []
    for selector, (prop, expected) in ramp.items():
        declared = _declarations(_css_rule(styles, selector)).get(prop)
        if declared != expected:
            wrong.append(f"{selector} {{ {prop}: {declared} }} -- the ramp says {expected}")
    assert not wrong, (
        "the first-boot wizard's step indicator is one of the three consumers design.css names "
        "for --progress-track / -fill / -now, and it paints itself from something else:\n  "
        + "\n  ".join(wrong)
        + "\n\nThe three states ARE the ramp, and its brightness carries the meaning the accent "
        "used to. A raw hex here is either a second accent or a borrowed facet identity, and "
        "design.css refuses both in as many words."
    )


BUNDLE_IMPORT = FRONTEND / "lib" / "components" / "BundleImport.svelte"
PROGRESS_RAMP = ("var(--progress-track)", "var(--progress-fill)", "var(--progress-now)")


def test_the_two_step_ramps_answer_in_one_register():
    """One screen, two step strips, and for a milestone they were two colours.

    `setup/+page.svelte:146` renders `<BundleImport>` on the wizard's last step, so a household's
    first session shows both at once: the wizard's own hairline above, retired to --progress-now by
    the test beside this one, and the swap strip below it saying the same thing -- how far a
    sequence has got -- in var(--ember). Between them sits the `.btn-primary` whose fill IS the
    accent, which is the whole of why §6.8 rations it: a selection next to four ember boxes is
    no longer legible AS a selection.

    The ember scan cannot catch this on its own, and that is the point of holding it here. The
    swap strip was on ACCENT_ALLOWLIST, licensed as "the swap step the admin has reached, of four
    -- the wizard's own selection", over markup that is four non-interactive spans lit cumulatively
    by `phase`. A licence with a false reason reads exactly like a true one, so what a second guard
    can add is not another look at the colour but the agreement: two strips, one question, one
    register.

    One token and not the ramp's three, because this strip does not tell a step behind you from
    the one you are on -- `class:on` is cumulative and index-driven, and drawing that distinction
    belongs to whoever owns the phase machine rather than to a colour sweep.
    [§6.8; decision 276 as amended; review cycle 1: acc-01;
    row `map-taste-admin-palette-facet-binding-and-accent`]
    """
    ramps = (
        (SETUP_PAGE, ".progress button.on:not(.done)", "background"),
        (BUNDLE_IMPORT, ".step.on", "border-color"),
    )
    wrong = []
    for path, selector, prop in ramps:
        declared = _declarations(_css_rule(_stylesheet(path), selector)).get(prop)
        if declared not in PROGRESS_RAMP:
            wrong.append(f"{path.name}: {selector} {{ {prop}: {declared} }}")
    assert not wrong, (
        "both of these say how far a sequence has got, on the same screen, and they answer in "
        "different colours:\n  " + "\n  ".join(wrong)
        + f"\n\nThe answer is one of {PROGRESS_RAMP}. The accent is not available to either: "
        "§6.8 spends it on selection and primary actions, a step nobody picked is neither, and "
        "the primary action on that screen is the button standing between the two strips."
    )


def test_the_deploy_is_taken_at_a_route_change_the_person_chose():
    """Finding 23: a deploy reloaded every open phone at its next tap.

    `service-worker.js` calls `skipWaiting()` and deletes every other cache on activate, so a tab
    open across a deploy has lost the chunks it is about to import. SvelteKit's fallback for that
    is a native navigation and it is the right one; what was wrong was WHEN it fired. With
    `kit.version.pollInterval` unset the check happens on the failed import itself -- mid-round or
    mid-block, on the tap that was meant to record a verdict. Polled, the flag is already up by
    the time a route changes, and a route change is the one moment the person has just said they
    are leaving the screen.

    Two halves, and neither is observable anywhere else in this project. `pollInterval` is read by
    Vite at build time into `__SVELTEKIT_APP_VERSION_POLL_INTERVAL__`, and SvelteKit
    short-circuits the poller under `__SVELTEKIT_DEV__`, so no dev server and no browser in this
    suite can exercise it; the `beforeNavigate` fires on a navigation no spec performs against a
    version that has moved. Deleting either reddens nothing -- which is the shape decision 281
    answers with a guard at the source.
    [§6 preamble (installable PWA, service-worker shell cache); finding 23;
     row `platform-an-unreachable-appliance-is-not-a-sign-out`]
    """
    config = _src(SVELTE_CONFIG)
    poll = re.search(r"pollInterval\s*:\s*([0-9_]+)", config)
    assert poll and int(poll.group(1).replace("_", "")) > 0, (
        "frontend/svelte.config.js declares no non-zero `kit.version.pollInterval`, so SvelteKit "
        "checks for a new deploy only once a chunk import has already failed -- which is the tap "
        "the person was making, not a moment they chose"
    )
    hook = re.search(r"beforeNavigate\(\((\w+)\)\s*=>\s*\{([^}]*)\}", _src(SHELL_LAYOUT), re.S)
    assert hook, "the shell registers no `beforeNavigate`, so the polled flag is read by nobody"
    assert "location.href" in hook.group(2) and f"{hook.group(1)}.to" in hook.group(2), (
        "the shell's `beforeNavigate` does not send the browser to the navigation's own "
        f"destination (`{hook.group(1)}.to.url.href`): a reload names no destination of its own, "
        "and the point of taking the deploy here is that the person just said where they are going"
    )
    # And the one navigation this shell makes that must NOT become a document load. Decision 272's
    # amendment is deliberate about the branch where the logout POST never landed: the cookie is
    # HttpOnly, so leaving the document re-boots into the session the tap was meant to end --
    # `/auth/me` answers 200 and `guard()` carries the person who just tapped Log out back to Home
    # under their own name. The hook rewrites EVERY client-side navigation while the flag is up,
    # which for the sixty seconds after a deploy lands includes that one.
    assert "!leaving" in hook.group(2) and re.search(r"leaving\s*=\s*true", _src(SHELL_LAYOUT)), (
        "the shell's `beforeNavigate` rewrites a failed sign-out's `goto('/login')` into a "
        "document load: a deploy is taken at a route change the person CHOSE, and a sign-out that "
        "could not reach the appliance is not one (decision 272, amended; decision 285)"
    )


def test_the_header_states_the_bundle_from_a_read_that_answered():
    """Decision 271's whole content is one operator, and nothing in the gate could see it.

    `hasBundle` was made tri-state so that "no bundle imported" is said only about a `/config` that
    ANSWERED. `session.hasBundle === false` and `!session.hasBundle` differ in exactly one state --
    null, the read that never arrived -- and that state is reachable on a path that is NOT offline
    at all: `bootstrap()` swallows a failed `/config` into null while `/auth/me` answers 200, so
    `session.offline` stays false, the shell renders, and the badge's condition is the only thing
    deciding whether a household with a full library is told it has none.

    Held at the source because no run in this suite can see it. `01-first-boot` measures a
    genuinely bundle-less household, where both spellings render the badge; `19-phone-shell`'s
    offline test asserts the badge absent inside the branch that renders no `<header>` at all, so
    its count is zero whichever operator is used; and until this cycle every case in the one vitest
    file that mounts this layout wired `/config` to an answer. Flipping the operator back reddened
    nothing anywhere, which is the shape decision 281 answers with a guard at the source.

    The store's half is asserted beside it because `=== false` means nothing against a field that
    never holds null: the comparison and the initial value are one rule in two files.
    [§3.1; decision 271; row `platform-an-unreachable-appliance-is-not-a-sign-out`;
     review cycle 2: M415-C2-COV-01]
    """
    shell = _src(SHELL_LAYOUT)
    assert NO_BUNDLE in shell, (
        f"the shell no longer draws {NO_BUNDLE!r} at all, so decision 271's badge is gone and "
        "this guard reads nothing"
    )
    owning = [
        condition
        for condition in re.findall(r"\{#if ([^}]+)\}", shell[: shell.index(NO_BUNDLE)])
        if "hasBundle" in condition
    ]
    assert owning, (
        f"the shell draws {NO_BUNDLE!r} under no `hasBundle` condition at all, so it says it "
        "whatever /config answered"
    )
    condition = owning[-1].strip()
    assert re.fullmatch(r"session\.hasBundle\s*===\s*false", condition), (
        f"the header's bundle badge is gated on `{condition}`, which reads null -- a /config that "
        "did not answer -- as 'no bundle imported'. That is the claim decision 271 took out of "
        "the header: a read that failed is not a fact about the household's library, and this is "
        "the one line of the shell that states a fact rather than rendering one. Compare with "
        "`=== false`. Home's three reads stay bare because decision 271 scoped this row to "
        "the header, and M4.9's surface still says it in more words -- re-filed, not denied."
    )
    store = _src(SESSION_STORE)
    assert re.search(r"hasBundle:\s*null", store), (
        "frontend/src/lib/session.svelte.js no longer initialises `hasBundle` to null, so the "
        "header's `=== false` compares against a value the boot invented and the third state "
        "decision 271 exists for is gone"
    )


def test_the_logout_row_states_the_branch_on_which_it_holds():
    """The record, not the code. §3.2's totality is real on one branch and the row claims both.

    `logout()` leaves the document -- `location.assign('/login')`, decision 285 -- only where the
    POST was CONFIRMED, because a sign-out the server never heard of comes back as a live session:
    the cookie is HttpOnly, `/auth/me` answers 200, and leaving would carry the person who just
    tapped Log out to Home under their own name. Decision 272's amendment argues that trade and it
    is the right one. What it leaves standing is the module state on the OTHER branch, where the
    shell client-navigates: Rate's card and log, Rank's `tiers` and `ratedTotal`, Tonight's step,
    ballot and approvals all cross into the next person's session.

    The row's `what` stated the guarantee with no condition, and its one registered test signs out
    against a live appliance and waits for a `load` event -- so it can only ever run the branch
    where the claim is true. `spec_coverage.toml`'s own header says nothing there is aspirational,
    and this project already treats an unqualified claim in the record as a first-class defect:
    `test_the_testing_ledger_does_not_claim_a_skip_this_milestone_did_not_take` exists for exactly
    that shape, one file over, and its remedy is the same one -- scope the sentence to what the
    milestone did, or name the survivors.

    So the guard reads the code for the branch and the row for the condition: while `logout()`
    has a path that does not leave the document, the row has to say so.
    [§3.2; decisions 272, 285; row `platform-signing-out-leaves-nothing-of-the-previous-person`]
    """
    shell = _src(SHELL_LAYOUT)
    body = re.search(r"async function logout\(\)\s*\{(.*?)\n  \}", shell, re.S)
    assert body, "the shell has no `logout()` to read"
    leaves = "location.assign(" in body.group(1)
    stays = re.search(r"else\s+await\s+goto\(", body.group(1)) is not None
    assert leaves, (
        "logout() no longer leaves the document, so nothing clears the three surface stores that "
        "outlive a client-side navigation (decision 272)"
    )

    rows = tomllib.loads(COVERAGE.read_text(encoding="utf-8"))["requirement"]
    row = next(
        r for r in rows
        if r["id"] == "platform-signing-out-leaves-nothing-of-the-previous-person"
    )
    if not stays:
        return
    # Both halves, because either one alone is still a claim the tree does not support: the
    # condition on which totality holds, and what survives on the branch where it does not.
    what = row["what"]
    named = re.search(r"(?i)\bconfirmed\b", what) and re.search(
        r"(?i)never landed|did not land|was not confirmed", what
    )
    assert named, (
        "logout() keeps a branch that does not leave the document -- the POST that never landed, "
        "where the shell client-navigates to /login and Rate's card, Rank's board and Tonight's "
        "ballot all survive -- and the row claims the guarantee unconditionally:\n\n  "
        + what
        + "\n\nName the condition the way decisions 272 and 285 do, or name the survivors. The "
        "one registered test signs out against a live appliance and waits for a `load`, so it "
        "cannot reach the branch the sentence is silent about."
    )


API_CLIENT = FRONTEND / "lib" / "api.js"
AUTH_ROUTER = REPO / "backend" / "spielplan" / "api" / "auth.py"

# The number words the seam's own comment is allowed to count its anonymous doors in. Spelt out
# because the comment is prose: "the first eight" is what a reader meets, not `len(...)`.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def test_the_seam_describes_the_credential_routes_it_actually_carries():
    """A coordinate into another file is a citation nothing can keep true, in either direction.

    `test_no_backend_comment_cites_the_tonight_client_by_line_number` settles that rule for the
    backend citing the frontend, and its verdict is "cite the function, not the line". The same
    defect had landed the other way round and nothing scanned for it: `api.js`'s CREDENTIAL_ROUTES
    comment cited `api/auth.py:306,380,416` for the three routes that answer 401 "wrong current
    password". Those numbers were true at HEAD and were falsified by THIS milestone's own
    seven-line edit to that router, in the same uncommitted change set -- so at the close they
    landed on a comment fragment, a bare docstring terminator and a sentence about PIN lockout
    counters. They also named the wrong two routes, having been listed in file order and read back
    in another.

    So this guard reads both halves the comment states and neither is a line number. The three
    handlers are named, and each one exists in the router and raises the 401 it is cited for. And
    the anonymous doors are COUNTED, because the same comment said "the first seven" while the
    array held eight of them and "the last three" beside it -- seven plus three against a list of
    eleven, a sum that never closed.
    [CLAUDE.md; M412-CONC-03; row `platform-a-401-returns-the-member-to-login`;
     review cycle 3: M415-C3-API-05]
    """
    client = _src(API_CLIENT)
    block = re.search(r"const CREDENTIAL_ROUTES = \[(.*?)\];", client, re.S)
    assert block, "api.js declares no CREDENTIAL_ROUTES, so finding 14's exemption is gone"
    routes = re.findall(r"'([^']+)'", block.group(1))
    assert routes, "CREDENTIAL_ROUTES is empty, so this guard is reading nothing"

    blocks = re.findall(r"/\*\*(.*?)\*/", client, re.S)
    comment = next((block for block in blocks if "the anonymous doors" in block), "")
    assert comment, "CREDENTIAL_ROUTES no longer carries the comment that argues for it"
    cited = re.findall(r"api/(\w+)\.py:[\d,\- ]*\d", comment)
    assert not cited, (
        f"the seam cites {sorted(set(cited))} by line number. Cite the function: a coordinate "
        "into another file is a citation nothing can keep true, and the three this comment "
        "shipped with were falsified by this milestone's own edit to that router"
    )

    # The count, read as prose and checked against the list. The password routes are the tail, so
    # the anonymous doors are everything before the first of them.
    password = [route for route in routes if route.startswith("/auth/") and "passkey" not in route]
    anonymous = routes.index("/auth/password")
    said = re.search(r"The first (\w+) are the anonymous doors", comment)
    assert said, "the seam no longer says how many of its entries are the anonymous doors"
    counted = _NUMBER_WORDS.get(said.group(1).lower())
    assert counted == anonymous, (
        f"the seam says the first {said.group(1)} entries are the anonymous doors and lists "
        f"{anonymous} of them before /auth/password. A reader checking the exemption against the "
        "array meets two different lists."
    )
    assert password[-3:] == ["/auth/password", "/auth/pin", "/auth/reauth"], (
        f"the three authenticated credential routes are {password[-3:]}, and the comment above "
        "the list is written about /auth/password, /auth/pin and /auth/reauth"
    )

    # And the three handlers, because a name is only better than a number if something checks it.
    router = _src(AUTH_ROUTER)
    named = re.findall(r"`api/auth\.py`'s `(\w+)`, `(\w+)` and `(\w+)`", comment)
    assert named, (
        "the seam names no `api/auth.py` handler for the three routes that answer 401 'wrong "
        "current password', so the claim it makes about them is checkable by nothing"
    )
    unknown = []
    for handler in named[0]:
        body = re.search(rf"async def {handler}\((.*?)(?=\nasync def |\n@router|\Z)", router, re.S)
        if not body or "wrong current password" not in body.group(1):
            unknown.append(handler)
    assert not unknown, (
        f"api.js names {unknown} as the handlers that answer 401 'wrong current password', and "
        "api/auth.py has no such function raising it. A name survives the edit that moves a line; "
        "it is only better than a coordinate while something reads it."
    )


# --- M4.15 review cycle 3: two records, held against the tree they describe -----------------

HOME_PAGE = FRONTEND / "routes" / "+page.svelte"
PHONE_SPEC = REPO / "e2e" / "specs" / "19-phone-shell.spec.js"
E2E_SPECS = REPO / "e2e" / "specs"

# The leaders a comment, a JSDoc block or a TOML comment puts in front of its prose. Stripped so
# the sentence a reader sees is the sentence these guards read, and nothing else about the line.
_COMMENT_MARKER = re.compile(r"^\s*(?:#+|//+|/\*\*?|\*/|\*)\s?")

# Two ways of saying that a branch draws nothing. Enumerated rather than judged, because the
# repair for a record that says one of them is to say what the branch actually draws, and a guard
# asking for that positively would be asking somebody else's prose to match a template.
_RENDERS_NOTHING = re.compile(r"(?i)empty grid|renders nothing|renders no panel")

# The shapes a record uses to say a spec file owed the contract nothing. `named neither` is in the
# list because that is the spelling this milestone shipped; the others are what a rewrite reaches
# for next, and the point of a guard over prose is that it survives the rewording.
_OWED_NOTHING = re.compile(
    r"(?i)no (?:coverage )?rows? at all|named by no (?:coverage )?row"
    r"|in a single (?:coverage )?row|in no (?:coverage )?row|named neither"
)


def _prose_blocks(path: Path) -> list[str]:
    """The prose in a record, with the code around it removed.

    A record's claims live in comments, docstrings and assertion messages; its data does not. The
    naive reading -- join every line and split on full stops -- glues a TOML `what =` string to the
    `tests = [...]` array under it and then finds spec filenames in a sentence that never mentioned
    one, which is how the first draft of the guard below reported `01-first-boot` against a row
    about an absent bundle directory. So each file is read for the thing it keeps prose in:
    paragraphs for Markdown, runs of comment lines elsewhere, and every string constant as well for
    Python, because an assertion message is where this file does its arguing.
    """
    text = _src(path)
    blocks: list[str] = []
    run: list[str] = []
    if path.suffix == ".md":
        for line in text.splitlines():
            if line.strip():
                run.append(line.strip())
            elif run:
                blocks.append(" ".join(run))
                run = []
    else:
        marks = ("#",) if path.suffix in {".toml", ".py"} else ("//", "*", "/*")
        for line in text.splitlines():
            if line.lstrip().startswith(marks):
                run.append(_COMMENT_MARKER.sub("", line).strip())
            elif run:
                blocks.append(" ".join(run))
                run = []
    if run:
        blocks.append(" ".join(run))
    if path.suffix == ".py":
        blocks += [
            " ".join(node.value.split())
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
    return blocks


def _record_sentences(path: Path) -> list[str]:
    return [
        sentence
        for block in _prose_blocks(path)
        for sentence in re.split(r"(?<=[.!?])\s+", block)
        if sentence
    ]


def test_the_record_names_the_surface_that_still_states_the_bundle_claim():
    """Decision 271 took the claim out of the header and left it standing one element lower.

    The header's badge reads `session.hasBundle === false` and Home's three reads are bare, so the
    two conditions differ in exactly the state the decision exists for: a `/config` that never
    answered while `/auth/me` did, which is not offline and does not clear. There the shell is
    silent, and `routes/+page.svelte` says the same four words in its count line and replaces the
    shelf list with a heading, a stated sentence and a CTA to import a bundle the household may
    already have. The divergence is defensible -- that panel is M4.9's surface, `=== false` on that
    branch would send a genuinely bundle-less household into `ShelfList`'s "rate a few titles"
    instead, and the honest repair reads the server's `degraded` payload rather than this field --
    but it is only defensible while the record STATES it. This milestone's own guard argued the
    opposite, from a description of that branch which the branch does not answer to, and that is
    the defect class review cycle 2 repaired twice: a record publishing a claim the tree refutes.

    So the source supplies the fact and the records are held to it. The check is negative on
    purpose. A positive one would be a template for somebody else's sentence, while what is
    actually forbidden is narrow: describing a branch that draws a heading and a CTA as one that
    draws nothing. Sentences naming `_RENDERS_NOTHING` itself are skipped, for the obvious reason.
    [§3.1; decision 271, amended; row `platform-an-unreachable-appliance-is-not-a-sign-out`;
     review cycle 3: M415-C3-COV-01]
    """
    home = _src(HOME_PAGE)
    reads = [match.group(0) for match in re.finditer(r"session\.hasBundle(?:\s*===\s*\w+)?", home)]
    assert reads, "frontend/src/routes/+page.svelte no longer reads `session.hasBundle` at all"
    if all("===" in read for read in reads):
        return  # Home took the operator too, and there is no divergence left to disclose

    # What the bare branch draws, read off the surface rather than asserted about it.
    assert NO_BUNDLE in home and "No artifact bundle has been imported" in home, (
        "Home no longer carries the bundle-less copy this guard is about, so the records it holds "
        "to that copy are describing something that is gone"
    )

    for path in (COVERAGE, Path(__file__).resolve(), REGISTER):
        for sentence in _record_sentences(path):
            if "_RENDERS_NOTHING" in sentence or "Home" not in sentence:
                continue
            if "hasBundle" not in sentence and "reads" not in sentence:
                continue
            assert not _RENDERS_NOTHING.search(sentence), (
                f"{path.name} describes Home's bundle-less branch as drawing nothing:\n\n  "
                + sentence
                + "\n\nIt draws a heading, a stated sentence and a CTA, and it replaces the shelf "
                "list -- in exactly the state decision 271 stopped the header claiming. The "
                "divergence between the header's `=== false` and Home's bare reads is a decision, "
                "not an accident, and a decision the record misdescribes is one nobody can audit."
            )


def test_no_record_says_this_map_never_named_a_spec_it_did():
    """The same defect class in the same records, about a different file and a louder claim.

    M4.15 opened on the argument that §6's preamble had never had an owner, and published it as a
    measurement: `02-shell.spec.js` and `06-responsive.spec.js` "were named by no row at all". Half
    of that is true. The other half is refuted by the map itself, 5600 lines above the sentence --
    three ids in two rows, M0's session-cookie contract and M4.9's model rail -- and the sentence
    was repeated into `test_spec_coverage.py`, `docs/TESTING.md` and the phone spec's own header,
    which is where an auditor sent here by CLAUDE.md "rather than assuming status" reads it. The
    conclusion survives the correction, because neither of those rows cites the preamble. The
    universal does not, and nothing keyed on it, so nothing could go red.

    The rule is therefore the one a record can be held to mechanically: a claim that this map owed
    a spec file nothing has to be true of the map. Rows of the CURRENT milestone are excluded,
    because the claim is always about what the map held BEFORE those rows were written; a later
    milestone naming one of these files makes the sentence a dated one and this guard say so,
    which is the right outcome rather than a false alarm.
    [decision 184; §6 preamble; row `platform-shell-clears-the-status-bar-and-the-toolbar`;
     review cycle 3: M415-C3-COV-01]
    """
    stems = sorted({path.name.split(".", 1)[0] for path in E2E_SPECS.glob("*.spec.js")})
    assert stems, "e2e/specs holds no spec files, so this guard is reading nothing"
    contract = tomllib.loads(COVERAGE.read_text(encoding="utf-8"))
    current = contract["current_milestone"]
    older: dict[str, set[str]] = {}
    for row in contract["requirement"]:
        if row.get("milestone") == current:
            continue
        for test in row.get("tests", []):
            for stem in stems:
                if test.startswith(f"e2e/specs/{stem}."):
                    older.setdefault(stem, set()).add(row["id"])

    for path in (COVERAGE, COVERAGE_REPORT, TESTING_LEDGER, PHONE_SPEC):
        for sentence in _record_sentences(path):
            if not _OWED_NOTHING.search(sentence):
                continue
            named = sorted(stem for stem in stems if stem in sentence and stem in older)
            assert not named, (
                f"{path.name} says this map owed {', '.join(named)} nothing:\n\n  "
                + sentence
                + f"\n\nThe map names {named[0]} in "
                + ", ".join(sorted(older[named[0]]))
                + ". Narrow the sentence to the file that was genuinely unnamed and say which "
                "rows held the other, or the next auditor greps the map and finds the record "
                "refuted by the file it is written in."
            )


# --- M4.16: the records this milestone rewrote, held against the tree they describe ------------
#
# Every guard below reads a DOCUMENT rather than a symbol, which is the shape the decision guards
# and the two ledger guards above already have. What is new is why there are five of them at once.
# M4.16 exists because this project's own records had drifted from the app that shipped: a
# normative file still describing a TV client nobody can reach and a wizard step nobody can take,
# a register calling itself provisional under fourteen shipped rows that cite it as authority, a
# comment pointing at a module that was never written, an exit criterion nobody had ever run. Each
# was survivable for the same reason -- nothing read it -- and each would drift again on the next
# wave, because a record is only as true as the last person who happened to look.
#
# So the rule decision 184 states for measurements is generalised here to prose: a document that
# states a fact about this tree is held to the tree. These are the cheap half of that; the half a
# person reads is the documents themselves.

README = REPO / "README.md"
DOCS = REPO / "docs"
RELEASE_RECORD = DOCS / "RELEASE.md"
SPIELPLAN = REPO / "backend" / "spielplan"
FRONTEND_STATIC = REPO / "frontend" / "static"

# `docs/milestones/` is excluded from every sweep below, for the reason the company-claim sweep
# 4,700 lines up gives and decision 296 then made a rule: the plan is the plan, the workflow
# forbids editing it, and a correction owed to a milestone record goes to `docs/RELEASE.md` under
# "Corrections owed to the milestone records" rather than into the plan it corrects. Four plans
# and the roadmap quote the retired phrase below in full, and all five are quoting history.
MILESTONE_RECORDS = DOCS / "milestones"


# --- M4.16 spec-01: the file README calls normative describes the surface that shipped ----------

# The four surfaces the normative file promised and the code does not have. `/tv` is the route
# decision 165 deleted and M4.12 carried out; "TV kiosk" is the same client under the name
# §6.2's old step 8 gave it; "member-account creation" is the wizard step decision 164
# struck (everyone after the admin is added from Admin > Users); "three existing seams" is
# §11's claim about Home Assistant, which decision 290 narrowed to three DESIGNATED seams,
# none of them built. Read case-insensitively because the file spelled the client three ways and
# the guard is about the promise, not the capitalisation.
#
# A FIFTH since review cycle 4, and it is not a client but a COUNT: "the ~10-vote round" is the
# fixed-length round the app has not had since M4.12. `tonight/round.py` ships `BOUNDARY_Z = 0.6`,
# `CAP_PAIRS = 20`, an escape from the sixth pair and `ended_by` in {converged, cap, escape}, and
# this milestone rewrote every other site of the figure -- §0's row, §6.2's preamble ("and so is
# the fixed count"), §6.2 step 4, §14 risk 6 and §14's pointer table -- leaving the one table
# decision 290 calls "the one table an auditor reads to learn what shipped". Anchored on the exact
# spelling rather than on `~10`, because §0's superseded v2.1 line keeps the old figure on purpose
# under decision 288's amend-in-place convention and a looser fingerprint would redden it.
# [decision 290; M4.16 cycle 4, M416-C4-SPEC-02]
_STRUCK_SURFACES = re.compile(
    r"\btv kiosk\b|/tv\b|member-account creation|three existing seams|the ~10-vote round", re.I
)

# The two words the 54a-54h fold put into the file, and the reason this guard is stated positively
# as well as negatively. A negative check alone is satisfied by deleting §6.2, which is not
# the repair: `NEITHER` is the round's abstain verdict and `uniform_holdout` is §13's
# hold-out draw, both of which the schema and `tonight/round.py` have shipped since M4.12 while
# the normative file still described a fixed ten-vote round. The pair is the cheapest proof that
# the fold landed rather than that the offending sentences were cut.
_FOLD_TERMS = ("NEITHER", "uniform_holdout")

# Review cycle 1 adds two more, and they are the other direction of the same defect: a normative
# file that OMITS a surface the app has, and one that promises a branch release data cannot reach.
# "Ending a room" is decision 169's §6.2 step 2 amendment, owed since M4.12 -- `api/tonight.py`'s
# `POST /sessions/{id}/end` shipped with six integration tests and a coverage row citing a clause
# that was not in the file. "ships no axis artifact" is decision 173's, owed since the same
# milestone: §6.2 step 5 described a reserved opposite-pole slot and named conflict copy for every
# D >= 0.20 evening, while `combine.contested_facet` returns None over an empty `dna_axis_weight`
# and no axis TSV has ever been authored. [M4.16 cycle 1, SPEC-02 and SPEC-03]
_FOLD_TERMS += ("Ending a room", "ships no axis artifact")

# And the four sentences decisions 164, 289 and 290 actually WROTE, held the way the two words
# above hold the fold. The negative half is a fingerprint on four RETIRED spellings, which is a
# rule calibrated to the defect rather than to the claim -- the same reading this file already
# applied to the sigma rule one cycle earlier. Measured over the file that ships: §11 re-promised
# as "three seams the app already serves", §7.3's "It is not built" replaced by "It ships with
# §12's M1 row", §2's rotation re-promised as a control in Admin > System, and the whole of §11
# deleted, are four mutations a person would actually write and every one of them left
# `_normative_problems` empty. Nothing else in the tree reads these clauses: the two coverage rows
# that name the seams are M7 with no tests, and `_unresolvable_sections` skips a row above
# `current_milestone`.
#
# Kept as their own tuple rather than folded into `_FOLD_TERMS` because the message differs: a
# missing fold term says the fold is not in the file, and a missing clause here says a strike was
# undone. `_FOLD_TERMS`' own comment already argues the mechanism -- "a negative check alone is
# satisfied by deleting §6.2, which is not the repair" -- and this is that argument applied to the
# other three surfaces the same test names. [decisions 164, 289, 290; M4.16 cycle 3, M416-C3-SPEC-01]
_STRIKE_CLAUSES = (
    ("is the only place they are made",
     "§3.1's wizard stops at the bundle import and the household's other accounts are made in "
     "§6.6 Users (decision 164)"),
    ("there is no admin-facing rotation surface",
     "§2's `SECRETS_KEY` rotation is the operator command and nothing else, because the rewrap "
     "needs the old key at the same time as the new one (decision 289)"),
    ("It is not built, and it is not part of",
     "§7.3's `POST /events/playback` is owed by M7 and no router serves it (decision 290)"),
    ("none of which is built",
     "§11's three Home Assistant seams are DESIGNATED rather than existing, which is the word "
     "that decision closed (decision 290)"),
)

# The data voice prints every model number through `{:.2f}` (`home/shelves.py`, `scoring/serve.py`),
# so `b(t) 0.52 . beta 0.8 . gate 0.93` is an example of a line the app cannot render -- one
# decimal where it always prints two. Decision 167 mandated the correction and only its §5.1 half
# landed, leaving the file contradicting itself on the one constant a member reads. Matched only in
# the data-voice form, a bare number after the symbol: §5.1's prose says `beta = 0.2` and
# `beta_app = 1 - beta_corpus`, which are statements about the algebra rather than renderings.
# [decision 167; M4.16 cycle 1, SPEC-01]
_DATA_VOICE_BETA = re.compile(r"β (\d+(?:\.\d+)?)")

# The universal that a re-measurement disproved. `scoring/backbone.py` says in its own words that
# a cold-masked row "is treated as ABSENT", so HAVING a Backbone row and having a Backbone
# COORDINATE are different facts -- and on v20260828, 1,055 of the 5,315 genome-carrying titles
# have the first and not the second. Decision 291's behaviour stands; the sentence it was argued
# from does not, and it had been copied into three records.
# [decision 304; M4.16 cycle 1, M416-291-02]
_GENOME_UNIVERSAL = re.compile(
    r"\b(?:every|all)\b[^.\n]{0,40}titles?[^.\n]{0,80}(?:genome|vector)[^.\n]{0,60}"
    r"(?:already )?ha(?:s|ve) a Backbone coordinate",
    re.I,
)

# Decision 288: the spec is amended IN PLACE and never forked, so each wave is a dated point
# release in the Status block. Matched by shape rather than by the version this wave wrote, so the
# next wave's `v2.1.2` satisfies it and a wave that amends the file silently does not.
_POINT_RELEASE = re.compile(r"^\*\*v\d+\.\d+\.\d+ \(\d{4}-\d{2}-\d{2}\).*$", re.M)

# And the line has to say WHICH decisions it folded in. A dated line that names none is a wave
# that happened and cannot be audited: the register is the provenance, the spec is the normative
# text, and the point release is the only thing that joins one to the other.
_FOLDED_DECISIONS = re.compile(r"decisions? \d+")

# TIGHT on purpose. `grep -rn 'not spec' docs/` matches "not specified" and is never empty, so a
# guard anchored on that phrase would either fail forever or be quietly narrowed by the first
# person it inconvenienced. The anchor is the sentence decision 288 retired, verbatim.
_PROVISIONAL = "proposals, not spec"


def _normative_file() -> Path:
    """The file README names as normative, resolved from README rather than named here.

    Hard-coding `spielplan-spec_v2.1.md` would make this guard agree with itself: the claim being
    held is README's -- "the spec is the authority" -- and a guard that picks its own subject
    cannot notice the day README starts pointing somewhere else.
    """
    claim = re.search(
        r"\*\*The spec is the authority\.\*\*(.*?)(?:\r?\n\r?\n|\Z)",
        README.read_text(encoding="utf-8"),
        re.S,
    )
    assert claim, (
        "README no longer opens with `**The spec is the authority.**`, which is the sentence that "
        "says which file this repository answers to. Every assertion below reads what it names."
    )
    named = re.search(r"docs/[\w.-]+\.md", claim.group(1))
    assert named, f"README's authority paragraph names no file under docs/:\n  {claim.group(1)}"
    path = REPO / named.group(0)
    assert path.exists(), f"README names {named.group(0)} as normative and it is not in the tree"
    return path


def _status_block(text: str) -> str:
    """The run of `**Key:**` lines under the title, which is where the file dates itself."""
    start = re.search(r"^\*\*Status:\*\*", text, re.M)
    if start is None:
        return ""
    rest = text[start.start():]
    end = re.search(r"\r?\n\s*\r?\n", rest)
    return rest[: end.start()] if end else rest


def _normative_problems(text: str) -> list[str]:
    """Everything wrong with a candidate normative file, as sentences a reader can act on."""
    problems = []
    for hit in dict.fromkeys(match.group(0) for match in _STRUCK_SURFACES.finditer(text)):
        problems.append(f"still describes a surface the code does not have: {hit!r}")
    for term in _FOLD_TERMS:
        if term not in text:
            problems.append(f"does not contain {term!r}, so the Tonight fold is not in it")
    for clause, why in _STRIKE_CLAUSES:
        if clause not in text:
            problems.append(
                f"does not contain {clause!r}, so a strike this file recorded has been undone: "
                f"{why}. The four spellings above are the RETIRED ones; re-promising a surface in "
                "plain English, or deleting the section that carries the strike, walks past them."
            )
    for example in dict.fromkeys(_DATA_VOICE_BETA.findall(text)):
        if len(example.partition(".")[2]) != 2:
            problems.append(
                f"prints the blend weight as `β {example}` in the data voice, which the app "
                "cannot render: every model number goes through `{:.2f}` (decision 167)"
            )
    wave = _POINT_RELEASE.search(_status_block(text))
    if wave is None:
        problems.append("its Status block carries no dated point-release line (decision 288)")
    elif not _FOLDED_DECISIONS.search(wave.group(0)):
        problems.append("its point-release line names none of the decisions it folded in")
    return problems


def _provisional_records() -> list[str]:
    """Files under docs/ that still say of themselves what decision 288 stopped being true."""
    found = []
    for path in sorted(DOCS.rglob("*.md")):
        if MILESTONE_RECORDS in path.parents:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _PROVISIONAL in line:
                found.append(f"{path.relative_to(REPO).as_posix()}:{number}")
    return found


def test_the_normative_file_describes_the_surface_that_shipped():
    """CLAUDE.md's own rule, turned on the file that states it.

    "Where code and spec disagree, the code is the bug" was this project's operating law for four
    milestones while the normative file described a TV client (§6.2's old step 8), a wizard
    step that creates the household's other accounts (§3.1), a §2 key rotation with
    no operator gesture, and three Home Assistant seams it called existing. By that law the whole
    M4 surface was a bug, which is the reading nobody took, because the honest reading is that the
    file was stale and no gate could say so. Decisions 164, 165, 289 and 290 struck or narrowed
    each promise and decision 288 settled the mechanism: one normative file, amended in place,
    each wave a dated point release in its own Status block.

    Stated in both directions deliberately. The negative half alone is satisfied by deleting
    §6.2, and the positive half alone by pasting two words into a file that still promises
    a kiosk. Together they say the fold landed.

    The last clause is the register's, and it sits here rather than beside the register guards
    because it is the same claim one file over: while `spec-v2.2-proposals.md` described itself as
    "proposals, not spec. Nothing here is normative", fourteen shipped coverage rows cited it as
    the authority for a constant. `docs/milestones/` is excluded because four plans and the
    roadmap quote that sentence as history and decision 296 forbids editing them.
    [§0 header; §3.1, §4.2, §6.2, §11, §12; decisions 164, 165, 288, 289, 290;
     row `platform-the-normative-file-describes-the-shipped-surface`]
    """
    spec = _normative_file()
    problems = _normative_problems(spec.read_text(encoding="utf-8"))
    assert not problems, (
        f"{spec.relative_to(REPO).as_posix()} is the file README calls normative, and it:\n  "
        + "\n  ".join(problems)
        + "\n\nThe direction is measure, take the decision, amend the spec -- never narrow this "
        "guard. A promise the code does not keep is struck with a numbered decision, or built."
    )
    provisional = _provisional_records()
    assert not provisional, (
        'a document under docs/ still describes itself as "proposals, not spec", which is what '
        "decision 288 retired: proposals 1-161 are provenance, entries 162 onward are numbered "
        "owner decisions and each is normative from the day it is taken. Fourteen shipped rows "
        "cited that file as authority while it said nothing in it was authority:\n  "
        + "\n  ".join(provisional)
    )


# --- M4.16 review cycle 1: the vendored extract attributes what it promotes -------------------
#
# Decision 294 vendored §3 and Appendix C of the corpus project's ARCHITECTURE.md precisely because
# "a normative sentence nobody in this repository can read is not normative". The authored header
# then promoted six clauses to normative and got two of the attributions wrong -- Davidson-with-ties
# is §3's arm 2 and Appendix C's only comparison model is the ridge anchor plus margin-weighted BT,
# and Appendix C's sigma belongs to the rank-Gaussian target, which is not what shipped. A reader
# chasing either pointer into the file it names finds nothing, which is the exact failure vendoring
# was meant to end. [decision 306; M4.16 cycle 1, SPEC-05]

EXTRACTS = DOCS / "ARCHITECTURE-extracts.md"

# Short and explicit, for `_STRUCK_SURFACES`' reason: a rule over every noun in the list would
# redden on prose. These are the model names a reader actually goes and looks up.
_VENDORED_ATTRIBUTIONS = (
    ("Davidson", "3"),
    ("margin", "Appendix C"),
    ("preconditioned", "Appendix C"),
)

# "- section 3's four-arm likelihood - ..." / "- Appendix C's margin weighting ...": one bullet's
# credit and everything it claims under it, up to the next bullet.
_CREDIT = re.compile(
    r"(?P<where>section 3|Appendix C)'s(?P<claim>.*?)(?=\n- |\Z)", re.S
)

# The claim the shipped fit does not keep. `ledger/observations.py` writes `ord_weight` as ones and
# `ledger/model.py`'s `_objective` carries no sigma at all -- sigma is a Laplace-diagonal OUTPUT.
# Appendix C's measured result is the NEGATIVE half ("never as a sample weight"), which does carry
# over; the positive half belongs to a regression target nobody built. If it is ever built, this
# assertion is deleted in the same change as the sentence it refuses.
#
# READ OVER THE AUTHORED REGIONS, AND SPELLED AS A CLAIM RATHER THAN AS A FINGERPRINT. The
# rule was the deleted sentence's own characters -- the literal word "inside", one space
# either side, no line break -- so it was calibrated to the defect's fingerprint and not to
# the claim, and three re-introductions a person would actually write walked past it:
# `sigma-in-the-likelihood`, which is how this project's own plan writes this clause;
# "keep sigma in the likelihood, never as sample weights", which is Appendix C's own sentence
# and therefore the wording somebody restating the vendored text would copy; and the caught
# phrase broken over a line, which is how the authored header in that file already wraps.
#
# Widening alone could not be done, which is why the SCOPE moves with it: the guard read
# `_src(EXTRACTS)` whole, vendored body included, and Appendix C's verbatim line carries that
# exact sentence -- so the wider rule would redden the file over text the header says is never
# edited. The authored region is everything above the first vendored heading, which
# `_vendored_bodies` already computes the boundary of; the normative file has no vendored text
# and is still read whole. Measured: one hit in the vendored body, none in either scanned
# region, so the scoping is load-bearing rather than a convenience -- the self-test below
# asserts both halves.
#
# What it still does not reach is a paraphrase that changes the verb -- "sigma belongs in the
# likelihood" -- and that is the honest limit rather than an oversight: a rule over every verb
# is a rule over prose, and the negative sentence this extract MUST keep stating ("there is no
# sigma in this app's likelihood to keep there") sits one word away from every widening of it.
# [decision 306; M4.16 cycle 2, SPEC-C2-05]
_SIGMA_IN_LIKELIHOOD = re.compile(
    r"(?:sigma|σ)[\s*-]+(?:inside|in|within)[\s*-]+the[\s*-]+likelihood", re.I
)


_VENDORED_HEAD = re.compile(r"^## (?:3\.|Appendix C)", re.M)

# Decision 308, and the same class as the sigma clause one bullet over. Section 3's arm 1 is
# `sigma(kappa_c - a_r * s)` and `ledger/model.py` has no term for `a_r`: the layout line below is
# the module's own statement of its parameter vector, `_ordinal_terms` enters the latent with
# coefficient 1, and `_objective` hands the same unscaled `s` to both ordinal arms. Free cutpoints
# do not absorb it -- an arm-specific SHIFT goes into kappa and an arm-specific SCALE does not --
# so a record saying the app fits one makes `ledger/model.py` the bug under CLAUDE.md's rule,
# which Phase C's non-goal forbids repairing.
#
# Written as a REQUIREMENT rather than as a ban, because the sentence that has to go on being said
# contains the words a ban would look for: section 4.3 keeps "per-arm sensitivities" in its first
# clause so `ledger/hyperparams.py`'s two verbatim quotes stay accurate. So any record naming a
# per-arm sensitivity has to say, in place, that none is built. On the day one enters the layout,
# this assertion is deleted together with the clauses it refuses.
# [decision 308; M4.16 cycle 2, SPEC-C2-01]
_PER_ARM_SENSITIVITY = re.compile(r"per-arm sensitivit(?:y|ies)|protocol sensitivity", re.I)
_PER_ARM_UNBUILT = re.compile(r"not built|no per-arm sensitivity is fitted", re.I)
LEDGER_MODEL = SPIELPLAN / "ledger" / "model.py"
_MODEL_LAYOUT = "theta = (mu, v[64], gamma[2], cuts[K-1], psi)"


def _per_arm_promises(text: str) -> list[str]:
    """Every mention of a per-arm sensitivity that does not say, in place, that none is built."""
    flat = " ".join(text.split())
    return [
        flat[max(0, m.start() - 60): m.end() + 120]
        for m in _PER_ARM_SENSITIVITY.finditer(flat)
        if not _PER_ARM_UNBUILT.search(flat[max(0, m.start() - 60): m.end() + 280])
    ]


def _authored_extract() -> str:
    """`ARCHITECTURE-extracts.md` down to its first vendored heading -- the part this repo wrote.

    The rule below is about what a RECORD promises, and the vendored bodies are not this
    repository's record: the file's own header says "Nothing in this file is edited". Reading them
    with the same rule would fail the file over Appendix C's measured sentence, which is the one
    thing vendoring exists to make readable. [decision 294; M4.16 cycle 2, SPEC-C2-05]
    """
    text = _src(EXTRACTS)
    head = _VENDORED_HEAD.search(text)
    return text[: head.start()] if head else text


def _vendored_bodies() -> dict[str, str]:
    """The vendored sections of `ARCHITECTURE-extracts.md`, keyed by the name the header uses."""
    text = _src(EXTRACTS)
    heads = list(re.finditer(r"^## (?P<name>.+)$", text, re.M))
    bodies = {}
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        name = head.group("name")
        key = "Appendix C" if name.startswith("Appendix C") else name.split(".", 1)[0]
        bodies[key] = text[head.end():end]
    return bodies


# The extract's own heading, and the two subjects §5.2's "What this section supersedes" sentence
# names. The heading carried five bullets, three of which said in their own text that they are NOT
# superseded -- the rank-Gaussian target ("simply unbuilt"), the "Guests" paragraph (superseded by
# decision 166 and §0's deletion of the mood round) and the protocol-reversal guard ("It is not
# superseded, it is simply unbuilt"). A reader of the file §5.2 promotes to normative was told,
# under a heading, that §5.2 had decided against two debts nobody has scheduled; the bullets
# self-correct in prose, and the heading is what an index reads.
# [decisions 294, 307; M4.16 cycle 4, M416-C4-SPEC-04]
_SUPERSEDED_HEADING = "**Superseded by section 5.2, and NOT implemented here:**"
_SELF_DENIAL = re.compile(r"not superseded|simply unbuilt", re.I)


# Decision 293 put the Data sources block on /account and argued it, in part, from a navigation
# fact the app does not have: "/account is also the one surface the phone shell already routes to
# from the tab bar". It does not. `api/auth.py`'s `SURFACES` is six entries -- home, rate, tonight,
# rank, map, taste -- and `NavRail.svelte` renders `nav.surfaces` and nothing else; /account lives
# in `_nav`'s separate `account` list, which `AccountChip` renders in its dropdown, and
# `AccountChip.svelte` already says so in as many words. The CONCLUSION survives -- /account really
# is reachable by every signed-in member -- but the reason given for it was false, and two comments
# this milestone wrote repeat it. Decision 318 corrects the premise; decision 293's own entry is
# left as the dated record it is, which is decision 304's mechanism.
#
# Scoped to the two live records rather than to the register, for that reason: an entry is a dated
# record and a later number supersedes it. The positive half is what makes this more than a
# fingerprint -- it fails the day /account moves out of the chip menu, which is the day these
# comments stop being true for the other reason. [decisions 293, 318; M4.16 cycle 4, M416-C4-ATTR-02]
_ATTRIBUTION_RECORDS = (
    "frontend/src/lib/components/DataSources.svelte",
    "frontend/src/routes/account/+page.svelte",
)
_TAB_BAR_ROUTE = re.compile(r"tab bar (?:already )?routes to", re.I)


def _flat_comment(path: Path) -> str:
    """One file's text with its comment furniture taken off, so a claim split across two lines of
    a `/* ... */` block reads as one sentence. Both offending sentences wrapped mid-claim -- one
    between "already" and "routes", the other between "routes" and "to" -- and a rule that only
    saw them unwrapped would have passed the file it was written for."""
    return " ".join(_src(path).replace("*", " ").split())


def test_no_record_says_the_tab_bar_routes_to_the_account_surface():
    """The navigation fact first, then the records that are about it.

    Read out of `api/auth.py` rather than restated here, because the claim is about what the
    server sends: the tab bar is `NavRail.svelte` rendering `nav.surfaces`, so a surface absent
    from `SURFACES` is a surface no tab reaches, whatever a comment says.
    """
    auth_source = _src(REPO / "backend" / "spielplan" / "api" / "auth.py")
    surfaces = next(
        ast.literal_eval(node.value)
        for node in ast.walk(ast.parse(auth_source))
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "SURFACES"
    )
    hrefs = [entry["href"] for entry in surfaces]
    assert "/account" not in hrefs, (
        f"/account is now one of the tab bar's surfaces ({hrefs}), so decision 293's argument has "
        "become true and these records may say so -- and decision 318's correction, plus this "
        "guard, come out in the same change"
    )
    nav_body = auth_source.partition("def _nav")[2].partition("\ndef ")[0]
    assert '"href": "/account"' in nav_body, (
        "/account is no longer in `_nav`'s account list either, so nothing routes a member to the "
        "surface the Data sources block lives on and decision 293's conclusion has gone with its "
        "premise"
    )
    guilty = [
        f"{name}: {found.group(0)!r}"
        for name in _ATTRIBUTION_RECORDS
        if (found := _TAB_BAR_ROUTE.search(_flat_comment(REPO / name)))
    ]
    assert not guilty, (
        "a record still argues the Data sources block's placement from a tab-bar route that does "
        "not exist:\n  " + "\n  ".join(guilty)
        + "\n\n/account is reached from the ACCOUNT CHIP's dropdown, for every signed-in member "
        "(decision 318). The conclusion is unchanged; the reason is."
    )


# TMDB's API Terms of Use section 3 (Attribution) does not ask for a notice to this effect, it
# FIXES the sentence -- and licenses exactly one variation in it: the bracketed list is a
# choose-one for the integrator's own category, and nothing else in it is theirs to write. The
# block shipped "This product uses the TMDB API but is not endorsed, certified, or otherwise
# approved by TMDB." for the whole of the milestone that added it: the head of TMDB's
# developer-FAQ form ("uses the TMDB API but is not endorsed or certified by TMDB") welded to the
# tail of the terms form, verbatim to neither published string, on a build that makes no TMDB
# request of any kind -- `connectors/` holds jellyfin, registry and resolve, and the TMDB
# overviews and poster paths arrive inside the corpus bundle. `docs/RELEASE.md` section 7.1 says
# all five notices "ship now, verbatim, and are asserted"; four of five did.
#
# The sentence is DERIVED from the terms here rather than pasted beside the component, because the
# three assertions that already pinned the paraphrase are the reason it survived a milestone:
# `expect(...).toBe(literal)` cannot tell a quotation from a rewrite, and a fourth hand-typed copy
# would be a fourth thing to edit in step. Resolving the bracket is the one edit TMDB licenses, so
# the rule takes that one and refuses every other.
# [decisions 298, 319; M4.16 cycle 4, M416-C4-ATTR-01]
_TMDB_TERMS_SENTENCE = (
    "This [website, program, service, application, product] uses TMDB and the TMDB APIs but is "
    "not endorsed, certified, or otherwise approved by TMDB."
)
_TMDB_CATEGORY = "product"
_TMDB_NOTICE_CARRIERS = (
    "frontend/src/lib/components/DataSources.svelte",
    "frontend/src/lib/components/data-sources.test.js",
    "e2e/specs/19-phone-shell.spec.js",
)
# Anything of the notice's SHAPE, so a rewritten head is caught by the same rule that catches a
# rewritten tail. TMDB's own shorter FAQ form matches this pattern too, which is the point: it is
# a TMDB-published string and still not the one the terms make a condition of display.
_TMDB_NOTICE_SHAPED = re.compile(r"This [A-Za-z]+ uses (?:TMDB|the TMDB)[^.]*\.")
_JS_CONCAT = re.compile(r"'\s*\+\s*'|\"\s*\+\s*\"")


def _flat_notice_text(path: Path) -> str:
    """One file's words, with markup wrapping and JS string concatenation taken off.

    Both halves are load-bearing here. The component wraps the sentence across two lines of markup
    and the two test files write it as two adjacent literals, because 104 characters plus quoting
    and indentation does not fit the line this repository holds to -- and a rule that only saw the
    sentence unsplit would pass every file it was written for.
    """
    return _JS_CONCAT.sub("", " ".join(_src(path).split()))


def _tmdb_notice() -> str:
    """TMDB's sentence with its one licensed variation taken."""
    head, _, rest = _TMDB_TERMS_SENTENCE.partition("[")
    options, _, tail = rest.partition("]")
    assert _TMDB_CATEGORY in [word.strip() for word in options.split(",")], (
        f"{_TMDB_CATEGORY!r} is not one of the categories TMDB's bracket offers ({options!r}), and "
        "the bracket is the only part of the sentence an integrator is licensed to write"
    )
    return f"{head}{_TMDB_CATEGORY}{tail}"


def test_the_tmdb_notice_is_tmdbs_own_sentence_and_not_a_paraphrase_of_one():
    """The one notice on this surface that is not its licence's own string was the one that drifted.

    Held over all three carriers at once, and not over the component alone, because the component
    is the only one of them a member reads and the other two are what make a correction expensive:
    a vitest `toBe` and a Playwright `exact` both go red on the fix, so the three move together or
    the surface and the assertions that pin it have come apart. [decision 319]
    """
    required = _tmdb_notice()
    wrong = []
    for name in _TMDB_NOTICE_CARRIERS:
        found = _TMDB_NOTICE_SHAPED.findall(_flat_notice_text(REPO / name))
        assert found, (
            f"{name} no longer carries a TMDB attribution notice at all, so either the surface has "
            "dropped a condition of displaying TMDB content or one of the two assertions that hold "
            "it to the licence's words has gone"
        )
        wrong += [(name, sentence) for sentence in found if sentence != required]
    assert not wrong, (
        "the TMDB notice is not the sentence TMDB's API terms fix:\n  "
        + "\n  ".join(f"{name}: {sentence!r}" for name, sentence in wrong)
        + f"\n\nrequired: {required!r}\n"
        "Section 3 licenses exactly one edit, the bracketed category. A notice that is TMDB-shaped "
        "but not TMDB's words is a breach rather than a copy edit (decision 319)."
    )


def test_the_extract_files_each_debt_under_a_heading_that_is_true_of_it():
    """A bullet that denies its own heading is a heading that is wrong about it.

    Held both ways. Negatively: nothing under the §5.2 heading may say it is not superseded or is
    simply unbuilt, which is what three of the five bullets said. Positively: the two clauses §5.2
    does name must still be there, so the repair cannot be "delete the heading", which would take
    the §5.2 attribution away from the two things it is true of.
    """
    text = _src(EXTRACTS)
    assert _SUPERSEDED_HEADING in text, (
        "ARCHITECTURE-extracts.md no longer carries the section 5.2 supersedes heading this guard "
        "reads; re-read it against whatever replaced it before narrowing this rule"
    )
    start = text.index(_SUPERSEDED_HEADING) + len(_SUPERSEDED_HEADING)
    block = text[start:].split("\n**", 1)[0]
    denials = sorted({found.lower() for found in _SELF_DENIAL.findall(block)})
    assert not denials, (
        f"a bullet under {_SUPERSEDED_HEADING!r} denies that heading in its own words: {denials}. "
        "Section 5.2 supersedes the Crowd Head and the random-walk prior on b_i and nothing else "
        "(decision 294); a debt nobody has scheduled is unbuilt, which is not the same claim."
    )
    for subject in ("Crowd Head", "random-walk prior"):
        assert subject in block, (
            f"{subject!r} is no longer filed under the section 5.2 heading, so the one attribution "
            "that sentence does make has been lost with the ones that were wrong"
        )


def test_the_vendored_extract_promotes_nothing_from_a_section_that_does_not_carry_it():
    """A normative pointer is only normative if the thing it points at is there.

    Two rules, and the second is the one that cost a reader a wasted search: a term the authored
    header attributes to a vendored section must occur in THAT section, and no record may claim
    sigma sits inside this app's likelihood while `ord_weight` is ones and `_objective` has no
    sigma term in it. Both the extract and the normative file are read, because §5.2 states the
    same list in its own words and the two went wrong together. [decision 306]
    """
    bodies = _vendored_bodies()
    assert set(bodies) >= {"3", "Appendix C"}, (
        f"ARCHITECTURE-extracts.md no longer carries both vendored sections: {sorted(bodies)}"
    )
    header = _src(EXTRACTS).split("**Normative in Spielplan**", 1)
    assert len(header) == 2, "the extract no longer marks what of it is normative in Spielplan"
    promoted = header[1].split("**Superseded", 1)[0]
    misattributed = []
    for term, home in _VENDORED_ATTRIBUTIONS:
        for credit in _CREDIT.finditer(promoted):
            where = "Appendix C" if credit.group("where").startswith("Appendix") else "3"
            if term not in credit.group("claim"):
                continue
            if where != home or term not in bodies[where]:
                misattributed.append(
                    f"the list credits section {where!r} with {term!r}; it is section {home!r}'s, "
                    f"and a reader who goes to {where!r} to read the equations finds none"
                )
    assert not misattributed, "\n  ".join(["ARCHITECTURE-extracts.md:", *dict.fromkeys(misattributed)])

    # Whitespace collapsed first, because a claim that wraps is still the claim: the authored
    # header in this very file breaks a sentence mid-phrase at about column 100.
    spec = _normative_file()
    claimed = [
        path.relative_to(REPO).as_posix()
        for path, text in ((EXTRACTS, _authored_extract()), (spec, _src(spec)))
        if _SIGMA_IN_LIKELIHOOD.search(" ".join(text.split()))
    ]
    assert not claimed, (
        f"{claimed} still promote sigma INSIDE the likelihood to normative. Appendix C's sigma is "
        "the rank-Gaussian target's per-level CDF band and that target is not what shipped: "
        "`ledger/observations.py` writes `ord_weight` as ones with a comment saying why, and "
        "`ledger/model.py`'s `_objective` has no sigma term. The measured result that DOES carry "
        "over is the negative one -- never a sample weight. If the target is ever built, delete "
        "this assertion in the same change as the sentence it refuses."
    )

    assert _MODEL_LAYOUT in _src(LEDGER_MODEL), (
        f"`ledger/model.py` no longer states its parameter vector as `{_MODEL_LAYOUT}`, so the "
        "premise below is unread rather than false. Re-read the layout: if a per-arm scale has "
        "landed, delete this assertion together with section 4.3's and the extract's `not built` "
        "clauses in one change."
    )
    unbuilt = {
        path.relative_to(REPO).as_posix(): promises
        for path, text in ((EXTRACTS, _authored_extract()), (spec, _src(spec)))
        if (promises := _per_arm_promises(text))
    }
    assert not unbuilt, (
        "a record names a per-arm sensitivity without saying none is built. Section 3's arm 1 "
        "carries `a_r` and this app has no term for it: the two ordinal arms share one unscaled "
        "latent with free cutpoints each, and a free cutpoint absorbs an arm-specific shift but "
        "never an arm-specific scale. Under CLAUDE.md's rule a sentence saying the app fits one "
        "makes `ledger/model.py` the bug, which is the reading decision 306 refused for sigma:\n  "
        + "\n  ".join(
            f"{where}: {found}"
            for where, promises in sorted(unbuilt.items())
            for found in promises
        )
    )


def test_the_attribution_guard_reads_the_section_and_not_the_word():
    """The self-test, and the direction that keeps it honest: a term genuinely in the section it is
    credited to passes, and the spelling that shipped does not."""
    bodies = _vendored_bodies()
    assert "Davidson" in bodies["3"] and "Davidson" not in bodies["Appendix C"], (
        "the vendored bodies no longer place Davidson-with-ties in section 3 alone, so the "
        "attribution this guard is about has stopped being wrong in the way it was"
    )

    def flat(text: str) -> str:
        return " ".join(text.split())

    # The spelling that shipped, and the three this rule missed while being named for the
    # claim rather than for them. The first is how `docs/milestones/M4.16-plan.md` writes this
    # very clause; the second is Appendix C's own sentence, which is what a person restating
    # the vendored text would copy; the third is the caught phrase wrapped, which is how the
    # authored header in that file already breaks its lines. [M4.16 cycle 2, SPEC-C2-05]
    for refused in (
        "sigma inside the likelihood and never as a sample weight",
        "σ **inside** the likelihood and never as a weight",
        "the margin weighting, sigma-in-the-likelihood, the ridge-anchor + BT fusion",
        "keep sigma in the likelihood, never as sample weights",
        "the preconditioner, and keep sigma inside\nthe likelihood",
    ):
        assert _SIGMA_IN_LIKELIHOOD.search(flat(refused)), refused
    # And the two sentences this extract MUST go on stating, one word from every widening of
    # the rule above: the negative result that DOES carry over, and the denial itself.
    assert not _SIGMA_IN_LIKELIHOOD.search(flat("σ never as a sample weight; a Laplace output"))
    assert not _SIGMA_IN_LIKELIHOOD.search(
        flat("there is no sigma in this app's likelihood to keep there")
    )
    # Decision 308's rule, both ways round. The first is the sentence section 4.3 shipped, which
    # says the app fits something it has no term for; the second is the repair, which keeps the
    # words `ledger/hyperparams.py` quotes verbatim and adds the one clause that makes them true.
    # [decision 308; M4.16 cycle 2, SPEC-C2-01]
    assert _per_arm_promises(
        "Per-user cutpoints and per-arm sensitivities are **not** shipped - they are fitted "
        "in-app by design."
    )
    assert not _per_arm_promises(
        "Per-user cutpoints and per-arm sensitivities are **not** shipped - the cutpoints are "
        "fitted in-app by design, and no per-arm sensitivity is fitted either."
    )
    assert not _per_arm_promises(
        "section 3's arm 1 carries a protocol sensitivity a_r, and it is not built here."
    )
    # The scope is load-bearing and not a convenience: Appendix C's vendored line carries the
    # refused claim verbatim, so a rule this wide read over the whole file would fail it over
    # text the header says is never edited -- and the authored region, which is this
    # repository's own record, carries none of it.
    assert _SIGMA_IN_LIKELIHOOD.search(flat(bodies["Appendix C"])), (
        "Appendix C no longer carries the sentence this guard is scoped around, so the scoping "
        "above has stopped proving anything: re-read the vendored body before narrowing it"
    )
    assert not _SIGMA_IN_LIKELIHOOD.search(flat(_authored_extract()))


# The three records decision 291's premise was copied into. All three are this milestone's own
# text: the normative clause, the loader's SKIPPED_TABLES reason, and the `Upheld` block quote
# M4.16 added under the vendored v1.1 sentence it upholds.
_GENOME_RECORDS = (
    "docs/spielplan-spec_v2.1.md",
    "backend/spielplan/importer/load.py",
    "docs/media-graph-spec_v1.1.md",
)

# The SECOND universal in the same paragraph, and the one decision 304's guard was never about. It
# is not a claim about why the block fed nothing; it is a claim about the PATH those 983 columns
# take -- "zero-imputed always", "never populated", "empty by construction", "the only path those
# 983 columns ever take" -- and it is false on any install seeded before decision 291. That
# decision stopped the IMPORT and emptied no table: no migration drops the rows, decision 162
# seeds content once, and `placement/features.py`'s `_genome` is still wired into `BLOCK_SOURCES`,
# so on such a box `build_vector` reaches `impute == "zero"` only via `if not pairs` and the block
# is PRESENT. Under this house's own rule -- where code and spec disagree, the code is the bug --
# a false universal in the normative file is a licence to delete that reader.
#
# A FIXED CLAUSE RATHER THAN A SWEEP, for `_FAMILY_SIZE`'s reason one file over: a rule over every
# way an unconditional path can be phrased is a rule over prose. And it reads the PATH and not the
# RULE, because the contract's rule genuinely IS unconditional -- `contract.py`'s
# `ZERO_IMPUTED = ("genome",)` -- so a sentence saying the contract declares the block zero-imputed
# must pass, and one saying the columns are zero-imputed ALWAYS must not.
#
# More records than the decision's own census named, which is why the count below is DERIVED from
# the tuple rather than written into this paragraph: decision 304 held its own universal over the
# three files that had it, decision 311 found it copied into three more, and review cycle 4 found
# two more again. A guard scoped to a census goes stale the moment the census does, and the count
# in prose here said "five" over a tuple of six before it was derived -- the defect class this
# whole milestone exists to close, inside the guard written to close it.
#
# READ FLATTENED, since cycle 4. `_src` returns raw text, so a claim that wrapped across two
# comment lines -- which every one of these does at 108 columns -- was never one string to search.
# `importer/validate.py` and `tests/test_backup.py` each stated the path unconditionally and were
# invisible twice over: not in the tuple, and spelled across a `#`.
# [decisions 304 and 311; M4.16 cycle 4: M416-C4-GEN-01, M416-C4-SPEC-01, M416-C4-GEN-03]
_GENOME_PATH_RECORDS = _GENOME_RECORDS + (
    "backend/spielplan/backup/movie_data.py",
    "backend/tests/test_placement.py",
    "backend/tests/test_load_mapping.py",
    "backend/spielplan/importer/validate.py",
    "backend/tests/test_backup.py",
)
_GENOME_PATH_CLAIMS = tuple(re.compile(pattern, re.I) for pattern in (
    r"zero-imputed always",
    r"\*{0,2}never\*{0,2} populated",
    r"empty by construction",
    # The one spelling with a legitimate narrowed neighbour: "empty on every install THIS BUILD
    # seeds" is the repair, and a bare match would redden the sentence that fixed it.
    r"empty on every install(?! this build)",
    r"the only path those 983 columns ever take",
    r"zero-imputes for every title",
    # The two cycle 4 found. `stay empty` has no plural 's' ON PURPOSE: `test_placement.py` says
    # "`blocks_dropped` stays empty" and "`blocks_empty` stays empty" of the vector build's own
    # diagnostic fields, which are not these tables, and a rule that reddened those would be a
    # rule about the word rather than about the claim.
    r"\bstay empty\b",
    r"zero-imputes? [^.]{0,40}either way",
))


def _flattened(text: str) -> str:
    """Comment furniture off and whitespace collapsed, over text from anywhere.

    Split out of `_genome_src` in review cycle 5 so that the self-tests below can be handed the
    same reading the records get. A flattener asserted only through a file is a flattener nobody
    has shown a wrapped sentence to. [M4.16 cycle 5, M416-C4-GEN-06]
    """
    return " ".join(re.sub(r"(?m)^\s*(#|--)\s?", " ", text).split())


def _genome_src(name: str) -> str:
    """One record, flattened: comment furniture off and whitespace collapsed.

    The claims this rule is about are sentences, and every file that carries one wraps it -- two
    of them between the two words that matter. Reading raw text asks whether a sentence happens to
    fit on one line, which is a question about the formatter. [M4.16 cycle 4]
    """
    return _flattened(_src(REPO / name))


def test_no_record_claims_the_genome_fed_nothing_for_every_title():
    """Decision 291 stopped importing the MovieLens slice, and argued it from a universal that a
    grep of the shipped artifacts disproves.

    "Every title carrying a genome vector already has a Backbone coordinate, so the block feeds
    nothing" was written into §4.1, into `importer/load.py`'s SKIPPED_TABLES reason and into the
    `Upheld` note under `media-graph-spec_v1.1.md:175`. Re-measured on v20260828 with the app's own
    rule: 1,063 of the 5,315 genome-carrying titles are NOT warm -- precisely the set §5.3's sweep
    hands to the Cold Tower -- and 1,055 of those carry a `cold_mask` row, which `scoring/
    backbone.py` states in its own comment "is treated as ABSENT". Having a row of E and having a
    coordinate are different facts, and the sentence conflated them.

    The behaviour is not the defect and is not reverted: §4.3 zero-imputes the block always, the
    Cold Tower's dropout training saw all-zero blocks, and nothing reads `blocks_imputed`. What is
    refused is a record that makes a true ruling out of a false premise, because the next reader
    inherits the premise. Held over all three records together, since the sentence spread by being
    copied. [decision 304; M4.16 cycle 1, M416-291-02]

    READ FLATTENED, since review cycle 5, which is the repair the sibling rule below got in cycle 4
    and this one did not. `_GENOME_UNIVERSAL`'s gap classes are `[^.\\n]`, so no match crosses a
    line break; two of the three records are single-long-line Markdown and the third,
    `importer/load.py`, wraps every comment at 108 columns. The sentence is 104 characters, 110
    with that module's comment indent, so E501 forbids the one-line form the rule could see and
    admits only the form it could not. Measured: over forty word-boundary placements of the
    retired sentence inside one wrapped comment paragraph, the raw read missed 26 and the
    flattened read missed none. Reading raw text asks whether a sentence happens to fit on one
    line, which is a question about the formatter. [decision 304; M4.16 cycle 5, M416-C4-GEN-06]
    """
    guilty = [
        f"{name}: {_GENOME_UNIVERSAL.search(_genome_src(name)).group(0)!r}"
        for name in _GENOME_RECORDS
        if _GENOME_UNIVERSAL.search(_genome_src(name))
    ]
    assert not guilty, (
        "a record still argues decision 291 from a universal the bundle disproves:\n  "
        + "\n  ".join(guilty)
        + "\n\nThe ruling stands; the premise is corrected. A cold-masked Backbone row is not a "
        "coordinate -- scoring/backbone.py says so -- and a fifth of the genome-carrying titles "
        "are exactly the ones the Cold Tower is asked to place."
    )


def test_no_record_says_the_genome_block_is_zero_on_every_install():
    """The second universal in the same paragraph, and the one no guard has ever read.

    Decision 291 stopped the IMPORT of the MovieLens slice. It emptied no table: no migration
    drops the 888,023 `ml_genome_score` rows a shipped build up to M4.15 loaded, decision 162
    seeds content once so nothing re-seeds them away, and `placement/features.py`'s `_genome` is
    still wired into `BLOCK_SOURCES`. `build_vector` only reaches `impute == "zero"` through
    `if not pairs`, so on such an install the block is PRESENT with real relevance values -- for
    exactly the 1,055 cold-masked titles decision 304 re-measured as the set the Cold Tower is
    asked to place.

    So "zero-imputed always" and "never populated" are false there, and this house's own rule --
    where code and spec disagree, the code is the bug -- makes a false universal in the normative
    file a licence to delete `_genome` as dead code. Decision 311's repair is to state what this
    build IMPORTS instead, and to name the pre-291 install as the one case the sentence does not
    cover. Held over every record the tuple names rather than over decision 311's census, because
    by the time this was written the sentence had been copied into more files than any census had
    counted -- twice, one cycle apart. That is the mechanism decision 304's own Cost paragraph
    named, and the reason the tuple is the count.
    [decision 311; M4.16 cycle 4: M416-C4-GEN-01, M416-C4-SPEC-01, M416-C4-GEN-03]
    """
    guilty = [
        f"{name}: {found.group(0)!r}"
        for name in _GENOME_PATH_RECORDS
        for claim in _GENOME_PATH_CLAIMS
        if (found := claim.search(_genome_src(name)))
    ]
    assert not guilty, (
        "a record still states the genome block's PATH as unconditional:\n  "
        + "\n  ".join(guilty)
        + "\n\nThe contract's RULE is unconditional and may be said so; the path is not. An "
        "install seeded before decision 291 keeps its rows, `features._genome` still reads them, "
        "and the block is populated there. Say what this build IMPORTS (decision 311)."
    )


@pytest.mark.parametrize(
    ("name", "text", "refused"),
    [
        ("the sentence as it shipped in section 4.3",
         "The genome block is **never** populated - the slice is not imported", True),
        ("the loader's own spelling",
         "`0003_content.sql`'s tables stay too -- they are empty by construction now", True),
        ("the archive's spelling",
         "the three tables and they are empty on every install, so a restore", True),
        ("the narrowed sentence that replaced it",
         "the three tables and they are empty on every install THIS BUILD seeds, so a restore",
         False),
        ("a statement about the contract's declared rule, which IS unconditional",
         "`placement/contract.py` declares the genome block zero-imputed for the whole run",
         False),
    ],
)
def test_the_genome_path_guard_reads_the_path_and_not_the_contracts_rule(name, text, refused):
    """Both directions, because either one alone would be a rule nobody can satisfy.

    Refusing the four spellings is the repair. ADMITTING the fifth and sixth is what keeps it
    honest: `ZERO_IMPUTED = ("genome",)` really is unconditional, so a record that says the
    contract declares the block zero-imputed is true and must pass -- and so must the narrowed
    sentence that fixed the third case, which differs from it by two words.
    """
    assert any(claim.search(text) for claim in _GENOME_PATH_CLAIMS) is refused, name


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("the sentence as it shipped",
         "every title carrying a genome vector already has a Backbone coordinate, so the block"),
        ("the plural it was copied as",
         "all 5,315 titles carrying a vector already have a Backbone coordinate and the 19"),
        ("the honest replacement",
         "a fifth of the titles carrying a genome vector are not warm, and nearly all of those "
         "carry a cold-masked row that §5.1 already treats as no coordinate"),
        ("the sentence as `importer/load.py` can legally carry it, wrapped across a `#`",
         "    # word word word word word word word every title carrying a genome vector already "
         "has a Backbone\n    # coordinate, so the block feeds nothing."),
    ],
)
def test_the_genome_premise_guard_reads_the_claim_and_not_the_subject(name, text):
    """Two spellings refused, one accepted. The third is the direction that keeps this honest: a
    sentence that talks about genome vectors and Backbone coordinates without asserting the
    universal is what the repair looks like, and a guard that reddened on it would be a guard
    nobody could satisfy except by deleting the paragraph.

    The fourth is the flattener, made load-bearing. `importer/load.py` is one of the three records
    and wraps every comment at 108 columns, so the only form the retired sentence can legally take
    there is a wrapped one -- and `_GENOME_UNIVERSAL` refuses newlines by construction. The case
    is read through `_flattened`, which is what the records are read through, so a repair that
    reverted to raw text would fail HERE rather than silently on the one record that wraps.
    [M4.16 cycle 5, M416-C4-GEN-06]
    """
    assert bool(_GENOME_UNIVERSAL.search(_flattened(text))) is (name != "the honest replacement"), name


# The other half of decision 311, and the one no record can carry: the two readers it KEPT. The
# five sentences were narrowed rather than `_genome` deleted to fit them, which leaves this one
# file the only place in the tree saying the path is live -- live on exactly the installs every
# other record describes as empty. A maintainer opening it for M5's flywheel finds a 0.5 relevance
# cut argued from 888,023 bundle rows this build declines and a docstring whose only stated absence
# is the §8-acquired one decision 304 upheld, and concludes the slice is still imported. Both acts
# that follow are expensive: re-adding the import, which `test_load_mapping.py` refuses, or
# deleting the reader as dead code, which nothing refuses and which changes the placement inputs of
# the 1,055 cold-masked titles §5.3's sweep hands the Cold Tower on every pre-291 install.
#
# Held on the decision NUMBER at both sites and not on the argument, because the argument is prose
# and the number is what a reader follows. The premise is asserted first: if `_genome` has left
# `BLOCK_SOURCES` then the reader really is gone, and that is a re-read of decision 311 rather than
# a line to delete here. [decision 311; M4.16 cycle 4, M416-C4-GEN-04]
#
# AND THE NUMBER IS 311, since review cycle 5. The rule shipped holding 291 -- the decision that
# STOPPED the import, which is the number a deletion argument cites -- while the row's `what` and
# this guard's own name say 311, the decision that kept the readers. Both directions were
# measured: a docstring reading "Dead code since decision 291 stopped the import: nothing populates
# these tables, so this reader can be deleted at M5" passed the guard written to refuse exactly
# that sentence, and the repair the `what` asks for -- naming 311 alone -- went red. Nothing else
# reads this file's prose, `features.py` not being in `_GENOME_PATH_RECORDS`, so the one sentence
# was unchallenged in the tree.
#
# A PATTERN RATHER THAN A SUBSTRING, because the two sites spell the citation differently and a
# naive flip would redden a clean tree: `_genome`'s docstring writes `(decision 311)` while the
# constant's comment writes `[decisions 291, 304 and 311]`, which does not contain the string
# "decision 311" at all. `test_load_mapping.py:152` already admits two spellings of the same
# citation by hand, which is this tree's own precedent for the hazard.
# [decision 311; M4.16 cycle 5, M416-C4-GEN-05]
_KEPT_BY_311 = re.compile(r"\bdecisions?[\d,\s]*(?:and\s*)?311\b", re.I)


def _comment_block_above(text: str, needle: str) -> str:
    """The run of `#` lines immediately above the line that starts with `needle`."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(needle)]
    assert starts, f"no line in this file starts with {needle!r}, so there is no block to read"
    start, block = starts[0], []
    while start and lines[start - 1].lstrip().startswith("#"):
        start -= 1
        block.append(lines[start])
    return " ".join(reversed(block))


def test_the_surviving_genome_readers_carry_the_decision_that_kept_them():
    """A reader kept against five records that read as denying it says so where it stands.

    This is the positive half of the guard above. That one refuses the unconditional path in the
    records; this one holds the code those records are now narrowed around, because the narrowing
    only works if the reader itself says why it survived. Comment-only, and that is the point --
    nothing about the query changes, and the whole defect is that a correct query read like an
    oversight. [decision 311]
    """
    source = _src(SPIELPLAN / "placement" / "features.py")
    assert '"genome": _genome' in source, (
        "`_genome` is no longer wired into BLOCK_SOURCES, so the reader decision 311 kept is gone. "
        "Re-read that decision before accepting this: the five records were narrowed rather than "
        "the reader deleted, precisely so a pre-291 install's placement inputs could not change on "
        "the strength of a universal the bundle disproves."
    )
    docs = [
        ast.get_docstring(node) or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_genome"
    ]
    assert docs, "`features.py` no longer defines `_genome`, which the assertion above should have said"
    sites = {
        "_genome's docstring": docs[0],
        "the comment above _GENOME_MIN_RELEVANCE": _comment_block_above(source, "_GENOME_MIN_RELEVANCE"),
    }
    silent = sorted(where for where, prose in sites.items() if not _KEPT_BY_311.search(prose))
    assert not silent, (
        "these read the MovieLens slice and no longer say why they are allowed to:\n  "
        + "\n  ".join(silent)
        + "\n\nDecision 291 stopped the import and emptied no table, so both sites are live on any "
        "install seeded before it. Naming decision 311 -- the one that KEPT them, not the one that "
        "stopped the import -- is what stops the next reader taking the reader for dead code or "
        "the cut for a leak. Citing 291 alone sends them to the entry that argues for the deletion."
    )


@pytest.mark.parametrize(
    ("name", "prose", "silent"),
    [
        ("the docstring's spelling, as it ships",
         "KEPT DELIBERATELY AFTER DECISION 291, and §4.1/§4.3 were narrowed to say so rather than "
         "this reader being deleted to fit them (decision 311).", False),
        ("the constant comment's spelling, as it ships",
         "THE CUT IS STILL LOAD-BEARING AFTER DECISION 291 [decisions 291, 304 and 311]", False),
        ("the two-number spelling the loader's reason uses",
         "the slice is not imported (decisions 291 and 311)", False),
        ("the sentence this guard exists to refuse",
         "Dead code since decision 291 stopped the import: nothing populates these tables, so "
         "this reader can be deleted at M5.", True),
        ("prose that argues the reader and names nothing",
         "Reads the MovieLens genome relevance through the link slice, above a relevance cut.",
         True),
    ],
)
def test_the_kept_reader_guard_reads_the_decision_that_kept_them(name, prose, silent):
    """Both directions, and the wrong-number direction is the one this rule shipped with.

    The refusal is the repair: a comment declaring `_genome` dead code while citing 291 is the
    sentence the guard above exists to stop, and a rule holding 291 admitted it word for word.
    The three ACCEPTED cases are what keeps the repair from being a rule about a string -- the two
    sites spell the citation differently, and the second of them carries no substring
    "decision 311" at all, so a guard written as one would redden the tree it was written to
    protect. [decision 311; M4.16 cycle 5, M416-C4-GEN-05]
    """
    assert (not _KEPT_BY_311.search(prose)) is silent, name


# The second premise decision 291's wave spread by copying, and the one that reached §10. There is
# no scheduled movie-data archive: `worker.py`'s JOBS holds exactly one backup job, the one named
# `nightly-backup`, and `_nightly_backup` calls `backup/nightly.run` -> pg_dump. The writer here
# has no job, no route and no scheduler, its only reachable caller being the console script
# `backend/pyproject.toml` declares, and `backup/__init__.py` states that split in
# its own words. `nightly.prune` rotates by its own dump-name pattern (`_is_own`), so nothing on a
# clock writes such an archive and nothing on a clock removes one. Decision 292 mandated §10's
# sentence WITHOUT a cadence -- the plan, README and the decision entry all carry it without the
# word -- and the word arrived with the transcription into the one file that is normative.
#
# Anchored on the SUBJECT rather than on the word, because §2's nightly pg_dump is real and is
# described as nightly all over this tree; what is refused is a cadence attached to the movie-data
# archive or to the three tables it used to hold. The register is not read: decision 291's entry is
# a dated record and is superseded rather than edited, which is decision 304's mechanism.
# [decision 310; M4.16 cycle 3, M416-C3-291-01]
_ARCHIVE_CADENCE = re.compile(
    r"(?:movie-data archive|ml_genome_score|three tables|archived)[^.\n]{0,60}"
    r"(?:nightly|every night)"
    r"|(?:nightly|every night)[^.\n]{0,60}(?:movie-data archive|ml_genome_score|three tables)",
    re.I,
)

# Where that premise was written. The normative clause, the loader's SKIPPED_TABLES reason, the two
# tests decision 291 landed with, and the module that owns the archive -- which claims no cadence
# today and is the first place the next copy would go.
_ARCHIVE_RECORDS = (
    "docs/spielplan-spec_v2.1.md",
    "backend/spielplan/importer/load.py",
    "backend/spielplan/backup/movie_data.py",
    "backend/tests/test_backup.py",
    "backend/tests/test_load_mapping.py",
)


def test_no_record_puts_the_movie_data_archive_on_a_schedule():
    """§10 called the movie-data archive nightly, and nothing writes it at all.

    A reader of the normative file budgets disk and retention for a content archive in
    `/data/backups` and goes looking for the job that produces it; there is none, and
    `nightly.prune(KEEP=14)` would not rotate one if there were. The restriction the sentence
    states is right and stands -- the bundle is private household data and so is anything cut from
    it -- so what is refused is the cadence and not the clause. Held over five records together,
    because this sentence spread the same way decision 291's other premise did: by being copied.
    [decision 310; M4.16 cycle 3, M416-C3-291-01]
    """
    guilty = [
        f"{name}: {_ARCHIVE_CADENCE.search(_src(REPO / name)).group(0)!r}"
        for name in _ARCHIVE_RECORDS
        if _ARCHIVE_CADENCE.search(_src(REPO / name))
    ]
    assert not guilty, (
        "a record puts the movie-data archive on a schedule this app does not run:\n  "
        + "\n  ".join(guilty)
        + "\n\n`backup/movie_data` has no worker job, no route and no scheduler - its only caller "
        "is the `spielplan-movie-data` console script. Section 2's pg_dump is the nightly one; "
        "this artifact is an operator gesture, and decision 162 seeds content once."
    )


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("the sentence as it shipped",
         "The nightly `/data/backups` movie-data archive inherits the same restriction"),
        ("the loader's copy",
         "the 888,023 rows were loaded on every import and archived every night into a block"),
        ("the test's copy",
         "The three tables were archived nightly while the importer filled them"),
        ("section 2's dump, which really is nightly",
         "the nightly pg_dump under /data/backups keeps fourteen days of the whole database"),
        ("the honest replacement",
         "The `/data/backups` movie-data archive inherits the same restriction, and an operator "
         "writes it by hand"),
    ],
)
def test_the_archive_cadence_guard_reads_the_claim_and_not_the_subject(name, text):
    """Three spellings refused, two accepted, and the two acceptances are what keep this usable.

    Section 2's dump IS nightly and is described that way in a dozen places, so a guard anchored on
    the word rather than on the artifact would either fail forever or be narrowed by the first
    person it stopped -- `_PROVISIONAL`'s argument, one rule over. The last case is the repair
    itself: the restriction with the cadence taken off it. [M4.16 cycle 3, M416-C3-291-01]
    """
    refused = name not in ("section 2's dump, which really is nightly", "the honest replacement")
    assert bool(_ARCHIVE_CADENCE.search(text)) is refused, name


@pytest.mark.parametrize(
    ("name", "mutation"),
    [
        ("the kiosk route", lambda t: t + "\n\nThe slate also renders at `/tv` for the room.\n"),
        ("the kiosk by name", lambda t: t + "\n\nA TV kiosk shows the slate to the room.\n"),
        ("the wizard step", lambda t: t + "\n\nStep 5 is member-account creation for the rest.\n"),
        ("the seams as existing", lambda t: t + "\n\nAdditive via three existing seams.\n"),
        # Review cycle 4: the count, not a client. Stated as a REPLACEMENT of the amended cell
        # rather than an append, so the case stops passing the day somebody rewords that row --
        # which is the assertion above this docstring's whole point. [M4.16 cycle 4, M416-C4-SPEC-02]
        ("the fixed-count round back in the milestone table",
         lambda t: t.replace("the adaptive round (median ~11 pairs", "the ~10-vote round (median")),
        ("the abstain verdict", lambda t: t.replace("NEITHER", "neither")),
        ("the hold-out draw", lambda t: t.replace("uniform_holdout", "holdout")),
        ("the dated point release", lambda t: _POINT_RELEASE.sub("**v2.1.1 undated:**", t)),
        ("the decisions it folded in", lambda t: _POINT_RELEASE.sub("**v2.1.2 (2026-10-01):**", t)),
        # Review cycle 3, and these are the four the negative half above cannot see: a surface
        # re-promised in words nobody retired, and a section deleted along with its strike. Every
        # one was measured green against the file that ships. [M4.16 cycle 3, M416-C3-SPEC-01]
        ("the seams re-promised as served",
         lambda t: t.replace("three **designated** seams, **none of which is built**, each owed "
                             "by **M7**", "three seams the app already serves")),
        ("the playback route re-filed to the milestone that shipped",
         lambda t: t.replace("**It is not built, and it is not part of §12's M1 row:**",
                             "**It ships with §12's M1 row:**")),
        ("the whole Home Assistant section deleted",
         lambda t: re.sub(r"\n## 11\. .*?(?=\n## 12\.)", "\n", t, flags=re.S)),
        ("the key rotation re-promised as an admin control",
         lambda t: t.replace("there is no admin-facing rotation surface",
                             "the System card carries the control")),
        ("the wizard's member-creation step put back",
         lambda t: t.replace("which is the only place they are made",
                             "which is where the wizard's fourth step makes them")),
    ],
)
def test_the_normative_file_guard_sees_each_promise_come_back(name, mutation):
    """docs/TESTING.md: a guard that cannot fail reads as coverage while providing none.

    Thirteen mutations, and six of them are the file as it stood when this milestone opened. Two
    are what a later wave reaches for when it amends the file in a hurry: a point release with no
    date, and one with a date and no decisions. Decision 288's mechanism is both -- the date says
    which wave, the numbers say what the wave was answering to, and a line carrying neither
    records that the file changed and nothing else.

    The last five are review cycle 3's and they are the reason the positive half exists at all:
    the negative half is four RETIRED spellings, so the surfaces come back the moment somebody
    writes them in their own words -- which is what M7 will be doing to §7.3, §11 and §12's rows,
    after M5 opens with no plan document. [M4.16 cycle 3, M416-C3-SPEC-01]
    """
    original = _normative_file().read_text(encoding="utf-8")
    text = mutation(original)
    # A mutation that no longer matches proves the guard against a file that has gone. Five of
    # these quote the normative file verbatim, and a wave that rewords one of those sentences
    # would otherwise leave its case passing over an edit it never made.
    # [`test_release_gate.py`'s idiom; M4.16 cycle 3, M416-C3-SPEC-01]
    assert text != original, (
        f"the {name!r} mutation no longer changes the normative file -- restate it from the file "
        "rather than leaving it to pass over a sentence nobody has any more"
    )
    assert _normative_problems(text), f"{name} came back and this guard read the file as clean"


def test_the_normative_file_guard_leaves_the_words_around_it_alone():
    """The other half of a self-test: what it must NOT say.

    "TV series" is half of what this app is about and appears on nearly every page of the file;
    `tvmaze` is a rating source; "not specified" is the string that makes the naive grep for the
    register's retired sentence useless. None of the three is a promise, and a guard that read one
    as a promise would be narrowed by the first person it stopped.
    """
    innocent = (
        "Kind is a selection of movies and TV series, never a merge (§4.1 rule 5).\n"
        "TVmaze credits are CC BY-SA; the join key is not specified by that source.\n"
        "**Status:** implementation spec.\n"
        "**v2.1.1 (2026-09-17):** amended in place; this wave folds in decisions 288-303.\n\n"
        "NEITHER is the abstain verdict; uniform_holdout draws §13's hold-out.\n"
        "Ending a room is the host's, and the corpus bundle ships no axis artifact.\n"
        "The model line reads `b(t) 0.52 · β 0.20 · gate 0.93` in the data voice.\n"
        "Accounts are made in §6.6 Users, which is the only place they are made.\n"
        "Rotation is the operator command; there is no admin-facing rotation surface.\n"
        "It is not built, and it is not part of §12's M1 row; three seams, none of which is "
        "built, are owed by M7.\n"
    )
    assert not _normative_problems(innocent)


# The one §6.2 figure the normative file borrows from `tonight/round.py`'s calibration paragraph,
# held under decision 184: a published figure is the one a run produced. §6.2 step 4 said "at the
# shipped 1.0 no round could ever report `converged`" -- an IMPOSSIBILITY, where the module beside
# the constant records 0-2 in 20 and `test_tonight_round.py::test_the_sweep_this_constant_was_
# calibrated_against_still_reads_this_way` asserts that range rather than zero. The sentence was
# true of decision 175's older sweep ("z = 1.0: cap 10/10") and M4.12 re-measured at the shipped
# owned-pool scale; round.py, docs/TESTING.md and docs/RELEASE.md all carry the newer figure and
# the normative file, which CLAUDE.md makes the authority, was the one record never updated. A
# reader re-deriving `BOUNDARY_Z` from §6.2 -- which is that paragraph's stated purpose -- starts
# from a false absolute, and a retune that produced a rare `converged` at z = 1.0 reads as a
# regression against the spec rather than as the documented behaviour.
#
# Two groups and a denominator rather than a phrase, so an en dash in prose and a hyphen in a
# comment are one measurement. [decision 184; M4.16 cycle 4, M416-C4-SPEC-05]
_CONVERGED_AT_ONE = re.compile(
    r"z = 1\.0[^.\n]{0,40}?`?converged`? fires (\d+)\D{1,4}(\d+) times? in (\d+)"
)


def test_the_spec_and_the_round_publish_one_convergence_measurement_at_z_one():
    """The normative file may not state as impossible what this project measured at up to 2 in 20.

    Held against `tonight/round.py`'s own paragraph rather than against a number written here,
    because that paragraph is the one the calibration harness re-runs -- which is what makes this
    a comparison of two records of one run rather than a third place for the figure to be wrong.
    """
    source = _src(REPO / "backend" / "spielplan" / "tonight" / "round.py")
    measured = _CONVERGED_AT_ONE.search(source)
    assert measured, (
        "`tonight/round.py` no longer publishes the z = 1.0 convergence figure this guard reads, "
        "so re-read the calibration paragraph before restating it in the spec"
    )
    published = _CONVERGED_AT_ONE.search(_normative_file().read_text(encoding="utf-8"))
    # ASCII in the message, not because the file avoids the section mark but because THIS one is
    # printed: a cp1252 console is what the operator reads a failing suite on (CLAUDE.md).
    assert published, (
        "section 6.2 step 4 no longer states the z = 1.0 convergence figure. It once said no "
        "round could EVER report `converged` at 1.0, which the project's own sweep disproves at "
        f"up to 2 in 20 -- state the measurement `tonight/round.py` publishes: {measured.group(0)!r}"
    )
    assert published.groups() == measured.groups(), (
        f"section 6.2 step 4 publishes {published.group(0)!r} and `tonight/round.py` measured "
        f"{measured.group(0)!r}: one run produced one number (decision 184)"
    )


# --- M4.16 cycle 4: the fold moved subjects BETWEEN steps, and the source cites step numbers ---

# The 54a-54h fold inserted the blind ballot as step 6, which renumbered everything after it --
# and it also MOVED three subjects: the tilt and its centring lever out of the old "Combine" into
# the new step 4, the no-re-ranking clause out of the old "Result" into step 7, and the budget-fit
# copy out of the old "Solo mode" into step 8. `spec_coverage.toml:2404-2407` records an audit of
# "all fifty step citations in this map" and two repairs, and stops at the map's edge. Nobody
# audited the 146 citations under `backend/spielplan/` and `frontend/src/`: eight resolved to the
# right paragraph BEFORE this milestone and to the wrong one after it, which is the failure
# `ROADMAP-to-M5.md:534-538` measured before the fold (21 of 30 map rows misresolving) and the
# fold's stated purpose was to end -- reproduced one directory over, by the fold itself.
#
# Written as (what the source calls the subject, the phrase in the normative file that fixes which
# step carries it) rather than as a table of step numbers, because a table of step numbers is the
# artifact that just went stale. The number is RE-DERIVED from the file on every run, so the
# owner's next fold re-points these citations' guard instead of outliving it; the only escape is
# to restate the phrase, which is a change to what is being asserted and reads as one in a diff.
# ASCII in every message below -- the citations interpolated into them carry a section mark, and
# a cp1252 console is what a failing suite is read on (CLAUDE.md).
# [CLAUDE.md Conventions; ROADMAP-to-M5.md:534-538; M4.16 cycle 4, M416-C4D2-SPEC-02]
_FOLDED_STEP_CITATIONS: tuple[tuple[str, str], ...] = (
    (r"step (\d)'s centring lever", "centred on the candidate-pool mean"),
    (r"step (\d)'s separating answer", "chosen-minus-rejected DNA"),
    (r"step (\d)'s observation for one answer", "(decision 154)"),
    (r"decision 218; §6\.2 step (\d)", "(decision 218)"),
    (r"step (\d)'s tilt\b", "a **mood tilt** learned from this round's answers"),
    (r"step (\d).{0,15}?nothing re-ranks within the evening",
     "nothing re-ranks within the evening"),
    (r"step (\d)'s two branches", "fits your 130 min"),
)


def _step_citation_sources() -> list[Path]:
    """The population `spec_coverage.toml`'s own audit note stops short of: the shipped source."""
    return [
        *sorted(SPIELPLAN.rglob("*.py")),
        *[p for p in sorted(FRONTEND.rglob("*")) if p.suffix in {".js", ".svelte"}],
    ]


def _flat_citations(source: str) -> str:
    """One line, comment markers dropped, so a citation that wraps is still one sentence.

    `tilt.py` puts "because section 6.2 step 7 says" at the end of one line and "nothing re-ranks
    within the evening" at the start of the next, which is the shape a line-oriented reader misses
    and the shape the house comment style produces on every long argument.

    Not `_flattened` above, which reads RECORDS -- `#` and `--` furniture, the two shapes a
    markdown or TOML record comes in. This reads SOURCE, where the marker is also `*` and `//`
    and where `*/` is furniture rather than text. Sharing one reader between the two would make
    the records guard strip a JSDoc terminator it bounds a paragraph at.
    """
    return " ".join(
        re.sub(r"^\s*(?:#+|\*|//)\s?", "", line).strip() for line in source.splitlines()
    )


def _printable(text: str) -> str:
    """The matched citation, on a console that is cp1252 (CLAUDE.md).

    This is the one guard here whose message QUOTES the source, and the thing it quotes is a
    section mark followed by a step number, inside sentences this codebase writes with em
    dashes -- so it is guaranteed to interpolate both. Two named substitutions because those
    two have readable ASCII spellings the rest of this file already uses, then a hard fallback:
    a failure message that crashes the console is a failure message nobody reads.
    """
    spelled = text.replace("\u00a7", "section ").replace("\u2014", "--")
    return spelled.encode("ascii", "replace").decode("ascii")


def _spec_62_steps() -> dict[int, str]:
    """Section 6.2's numbered steps, keyed by the number the normative file gives them today."""
    body = _normative_file().read_text(encoding="utf-8")
    section = re.search(r"^### 6\.2 .*?(?=^### 6\.3 )", body, re.S | re.M)
    assert section, (
        "the normative file no longer carries a section 6.2, so no step citation in the source "
        "resolves to anything -- read the file before restating this rule"
    )
    steps: dict[int, str] = {}
    current: int | None = None
    for line in section.group(0).splitlines():
        head = re.match(r"(\d+)\. \*\*", line)
        if head:
            current = int(head.group(1))
            steps[current] = ""
        if current is not None:
            steps[current] += line + "\n"
    assert len(steps) >= 2, "section 6.2 no longer reads as a numbered list of steps"
    return steps


def _step_citation_problems(name: str, source: str) -> list[str]:
    """Every citation in `source` naming a step whose text does not carry the subject cited."""
    steps = _spec_62_steps()
    flat = _flat_citations(source)
    problems: list[str] = []
    for pattern, phrase in _FOLDED_STEP_CITATIONS:
        owning = sorted(n for n, text in steps.items() if phrase in text)
        if len(owning) != 1:
            problems.append(
                f"{phrase!r} occurs in {len(owning)} of section 6.2's steps {owning}, so it "
                "cannot fix a step number: re-anchor this rule on a phrase the file carries once"
            )
            continue
        for match in re.finditer(pattern, flat):
            if int(match.group(1)) != owning[0]:
                problems.append(
                    f"{name} cites {_printable(match.group(0))!r}, and section 6.2 carries "
                    f"{phrase!r} in step {owning[0]}"
                )
    return problems


def test_no_source_citation_names_a_step_the_fold_moved_its_subject_out_of():
    """CLAUDE.md makes these citations load-bearing: they are how a reader checks a rule.

    The map audited itself and the source was left as it stood. `pool.py`'s fit line promises its
    two branches are "verbatim" from a step that does not contain them, and a reader following
    `tilt.py` to the "separating answer" lands on the shortlist paragraph, which has no
    chosen-minus-rejected rule in it at all. [M4.16 cycle 4, M416-C4D2-SPEC-02]
    """
    guilty = [
        problem
        for path in _step_citation_sources()
        for problem in _step_citation_problems(path.relative_to(REPO).as_posix(), _src(path))
    ]
    assert not guilty, (
        "a source citation names a section 6.2 step that does not carry what it cites:\n  "
        + "\n  ".join(guilty)
        + "\n\nThe fold renumbered the steps AND moved three subjects between them. Re-point the "
        "citation; the step numbers above are read from the normative file on every run."
    )


def test_every_folded_step_citation_rule_still_has_a_citation_to_hold():
    """A rule about a subject nobody cites any more reads as coverage while providing none.

    The other half of the same worry: `_FOLDED_STEP_CITATIONS` is seven hand-written patterns over
    a source tree that moves, and a pattern matching nothing would go on passing for ever over a
    comment somebody reworded. Held per pattern rather than in total, because six live patterns
    hide the seventh. [docs/TESTING.md; M4.16 cycle 4, M416-C4D2-SPEC-02]
    """
    flat = [_flat_citations(_src(path)) for path in _step_citation_sources()]
    idle = [
        pattern
        for pattern, _ in _FOLDED_STEP_CITATIONS
        if not any(re.search(pattern, text) for text in flat)
    ]
    assert not idle, (
        "these step-citation rules match nothing under `backend/spielplan` or `frontend/src` and "
        f"hold nothing: {idle}. Re-anchor each on the wording that replaced it, or delete it in "
        "the same change as the comment it was about."
    )


@pytest.mark.parametrize(
    ("name", "relative", "mutation"),
    [
        # Each case is the citation exactly as it stood before this cycle repaired it, so a case
        # that stops changing the file is a case whose site was reworded -- which is why the
        # assertion below reads the mutation as well as the guard. [`test_release_gate.py`'s idiom]
        ("the centring lever back in the old Combine", "backend/spielplan/home/rail.py",
         lambda t: t.replace("§6.2 step 4's centring lever",
                             "§6.2 step 5's centring lever")),
        ("the separating answer back in the old Combine", "backend/spielplan/tonight/tilt.py",
         lambda t: t.replace("§6.2 step 4's separating answer",
                             "§6.2 step 5's separating answer")),
        ("the four-branch observation back in the old Combine",
         "backend/spielplan/tonight/tilt.py",
         lambda t: t.replace("§6.2 step 4's observation for one answer",
                             "§6.2 step 5's observation for one answer")),
        ("decision 218's own citation back in the old Combine",
         "backend/spielplan/tonight/tilt.py",
         lambda t: t.replace("decision 218; §6.2 step 4]",
                             "decision 218; §6.2 step 5]")),
        ("the tilt back in the old Combine", "backend/spielplan/tonight/play.py",
         lambda t: t.replace("§6.2 step 4's tilt", "§6.2 step 5's tilt")),
        ("the no-re-ranking clause back in the old Result", "backend/spielplan/tonight/pool.py",
         lambda t: t.replace("§6.2 step 7 — nothing re-ranks",
                             "§6.2 step 6 — nothing re-ranks")),
        ("the wrapped no-re-ranking clause back in the old Result",
         "backend/spielplan/tonight/tilt.py",
         lambda t: t.replace("§6.2 step 7 says", "§6.2 step 6 says")),
        ("the budget-fit copy back in the old Solo mode", "backend/spielplan/tonight/pool.py",
         lambda t: t.replace("§6.2 step 8's two branches",
                             "§6.2 step 7's two branches")),
    ],
)
def test_the_step_citation_guard_sees_a_citation_the_fold_left_behind(name, relative, mutation):
    """The eight sites as they stood, restored one at a time.

    Not a synthetic string: this guard exists because these exact comments were green, and a
    self-test over invented text would prove a regex rather than the rule. [M4.16 cycle 4,
    M416-C4D2-SPEC-02]
    """
    path = REPO / relative
    original = _src(path)
    text = mutation(original)
    assert text != original, (
        f"the {name!r} mutation no longer changes {relative} -- restate it from the file rather "
        "than leaving it to pass over a comment nobody has any more"
    )
    assert not _step_citation_problems(relative, original), f"{relative} is not clean to start with"
    assert _step_citation_problems(relative, text), (
        f"{name} came back and this guard read the source as clean"
    )


# --- M4.16 spec-10: the decision register counts itself, and two documents publish that count ---


# A decision range as either document spells it -- `162-303`, and the en dash a markdown editor
# substitutes. Three digits each side on purpose: `2026-09-17` and `1-161` are not decision
# ranges, and a line reference like `:126-134` never appears inside the two blocks read here.
_DECISION_RANGE = re.compile(r"\b(\d{3})\s*[-\u2013]\s*(\d{3})\b")


# --- M4.16 spec-15: a comment that names a file names one that is there ------------------------
#
# CLAUDE.md makes these citations load-bearing -- "comments argue why and cite the spec", and in
# practice they also cite the guard that enforces the rule, which is how the next reader finds it.
# A citation to a file that does not exist costs that reader the same hour every time, and there
# is no way to notice one except by following it.
#
# Scope is the four shapes the coverage row names, resolved the way this repository spells them.
# Everything else a comment mentions is deliberately NOT read: `Lib/asyncio/mixins.py` is CPython,
# `mdc/export.py` and `scripts/build_content.py` are the corpus project, `artifacts/manifest.json`
# is a path inside the bundle and `_app/version.json` is a URL SvelteKit serves. None of them is a
# file this tree could be asked to hold, and a guard that demanded them would be switched off.

_COMMENT_SOURCES = {".py", ".js", ".svelte", ".css", ".html"}

# A path with at least one directory in it, ending in a source extension. The lookbehind refuses a
# preceding `/`, which is what keeps `https://example.com/app.js` from being read as `com/app.js`.
#
# `+` IS IN THE FINAL SEGMENT since review cycle 5, and without it this rule could not see a single
# SvelteKit route file. `+` was outside `[\w.-]` and the `(?:[\w.-]+/)+` prefix forces the last
# segment to start immediately after a `/` -- which is where the `+` sits -- so no backtrack
# reached `page.svelte` and `frontend/src/routes/admin/data/+page.svelte`, named in
# `api/artifacts.py`'s `bundle_state` docstring as a fully qualified `frontend/...` path, was
# invisible to the guard whose row promises exactly that shape exists. It is not a deliberate
# non-read either: the scope comment above enumerates what this knowingly does not follow --
# CPython, the corpus project, a path inside the bundle, a URL SvelteKit serves -- and route files
# are in none of those. Measured: the widening takes the candidate set from 98 to 101, all three
# new ones exist, and `_missing_named_files` stays empty. It does not make the guard red; it makes
# it able to go red. [M4.16 cycle 5, M416-C5-SPEC15-01]
_QUALIFIED_PATH = re.compile(
    r"(?<![\w./-])((?:[\w.-]+/)+[\w.+-]+\.(?:py|js|mjs|svelte|css|json|html|svg))(?![\w])"
)
# And the one unqualified shape this codebase uses constantly: `test_landmine_guards.py`, named
# without its directory because there is only one place tests live.
_BARE_TEST = re.compile(r"(?<![\w./-])(test_\w+\.py)(?![\w])")

# The first frontend segments a comment writes: `frontend/...` in full, or relative to the app.
_FRONTEND_ROOTS = ("frontend/", "src/", "static/", "lib/", "routes/")

# Dated exceptions. Each names a path a comment states is ABSENT, together with the reason it
# cannot simply be repaired -- and each is held NOT STALE in both directions below: the entry is
# only allowed while the file is still missing AND still named. The day either changes, the entry
# goes in the same change as the comment that needed it.
COMMENT_PATH_EXCEPTIONS = {
    "db/dna.py": (
        "2026-09-17: `0004_dna.sql:11` names it as the read layer holding §4.1 rule 2. It "
        "was never written -- the reads are in `db/library.py` and `home/why.py` -- and the "
        "migration is applied and sha256-checksummed, so correcting the comment in place is a "
        "hard startup error for every existing install. The correction is recorded in "
        "`db/library.py`'s module docstring instead, which is why this path is named twice in "
        "the tree and cannot be made to resolve."
    ),
    "frontend/static/tmdb-logo.svg": (
        "2026-09-17: decision 298's owed asset. TMDB's terms ask for the logo beside the notice "
        "and no agent may fabricate a trademarked file, so `DataSources.svelte` renders a named "
        "slot when the file is there and nothing when it is not -- a comment about an absence "
        "rather than a citation to follow. `docs/RELEASE.md` section 7.1 carries the debt, and "
        "the day the owner drops the file in this entry and that section go together."
    ),
    "sync/resolve.py": (
        "2026-09-17: `backup/movie_data.py:32` means `connectors/resolve.py`, which is where "
        "`resolve_title_id` mints a title for a Jellyfin item nobody has seen before. The "
        "sentence is right about the mechanism and wrong about the module, and it is repeated in "
        "`test_backup.py:1367`. Both files belong to the milestone that owns `backup/`; this is "
        "recorded here rather than repaired across an ownership line."
    ),
}


def _comment_prose(path: Path) -> list[tuple[int, str]]:
    """Every comment in one source file, as (line, text).

    Python is tokenized rather than scanned, so a trailing `# see api/deps.py` is read and a `#`
    inside a string is not; docstrings come from the AST, because in this codebase the docstring
    IS the argued comment and most citations live there. Everything else is scanned for `//`,
    `/* */` and `<!-- -->`, which over-reads a `//` inside a string literal -- harmless here, since
    the only consequence is holding one more sentence to the tree.

    The markup form arrived in review cycle 5, and it is the one shape unique to the files
    `.svelte` and `.html` were added to `_COMMENT_SOURCES` for: this frontend writes 142 argued,
    spec-citing `<!-- -->` blocks and none of them was read, so a rule whose own self-test is named
    "reads every shape a citation comes in" was blind to roughly half the comment mass of the
    surfaces it scans. Three of those blocks already name an in-scope module. The tree is green
    either way today -- both paths happen to be named in a Python comment as well -- which is the
    point: the guard was passing by coincidence rather than by looking.
    [M4.16 cycle 5, M416-C5-SPEC15-02]
    """
    text = _src(path)
    found: list[tuple[int, str]] = []
    if path.suffix == ".py":
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            tokens = []
        found += [(tok.start[0], tok.string) for tok in tokens if tok.type == tokenize.COMMENT]
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    doc = ast.get_docstring(node, clean=False)
                    if doc:
                        found.append((getattr(node, "lineno", 1), " ".join(doc.split())))
        return found
    for block in re.finditer(r"/\*.*?\*/|<!--.*?-->", text, re.S):
        found.append((text.count("\n", 0, block.start()) + 1, " ".join(block.group(0).split())))
    for line_number, line in enumerate(text.splitlines(), 1):
        for slashes in re.finditer(r"(?<![:/])//", line):
            found.append((line_number, line[slashes.start():]))
    return found


def _comment_paragraphs(path: Path) -> list[tuple[int, str]]:
    """`_comment_prose`, with a run of adjacent `#` lines read as the one paragraph it is.

    `_comment_prose` yields a comment PER LINE, which is the right grain for a rule about a single
    comment and the wrong one for a rule about a sentence. This codebase wraps its prose at 108
    columns, so a sentence long enough to state a number AND the noun it counts is exactly the
    sentence likely to state them on either side of a line break, and neither line then carries the
    pair. That is not hypothetical: the note above `ALLOWED_RESIDUE` in `test_layering_guards.py`
    said "imports all twelve" and the line under it said "routers by definition", and the guard
    written to hold that very note to the tree read neither half, over two stale sentences, in a
    file that stayed green all milestone. [decision 184; M5.1 review cycle 3, M51-C3-REG-02]

    `test_worker_schedule.py:1153` records the same limit and declines to close it, on the argument
    that gluing blocks would join a comment to the DOCSTRING under it and so exempt a count from a
    citation that is not in its sentence. That argument is about gluing the two kinds together and
    it stands; it says nothing against gluing a comment run to itself, which is all this does --
    consecutive lines, both comments, a bare `#` ending the run the way it ends a paragraph for a
    reader, and docstrings passed through whole and untouched. The sibling's limit therefore stays
    true of the reader it is written about, which still uses `_comment_prose`.

    The line reported is the run's first, in `_job_tallies`' idiom of reporting a docstring's `def`:
    enough to find the paragraph, and not a second number that has to stay true.
    """
    runs: list[tuple[int, list[str]]] = []
    rest: list[tuple[int, str]] = []
    for line, text in _comment_prose(path):
        if not text.startswith("#"):
            rest.append((line, text))
            continue
        body = re.sub(r"^#+[ \t]?", "", text).strip()
        if not body:
            runs.append((line, []))
            continue
        if runs and runs[-1][1] and runs[-1][0] + len(runs[-1][1]) == line:
            runs[-1][1].append(body)
        else:
            runs.append((line, [body]))
    return [
        (line, " ".join(("# " + " ".join(lines)).split())) for line, lines in runs if lines
    ] + rest


def _named_in_scope(named: str, packages: set[str]) -> Path | None:
    """Where this repository would keep `named`, or None when it is not this repository's to keep."""
    head = named.split("/", 1)[0]
    if named.startswith("backend/tests/") and named.endswith(".py"):
        return REPO / named
    # The same path with the `backend/` left off, which is how this package actually writes it.
    # `db/library.py:14` cited `tests/test_no_weight_filters.py` -- a file that has never existed
    # on any branch -- and this reader returned None for it, so `_comment_named_files` dropped the
    # candidate unplaced and the guard reported nothing: `_QUALIFIED_PATH` extracts the path,
    # `_BARE_TEST`'s lookbehind refuses it for the slash, and no branch here knew the prefix. The
    # shape the row was opened for was the one shape it could not see, and that comment was
    # repaired by hand rather than by this guard catching it. There is exactly one tests directory
    # in the tree -- `_BARE_TEST`'s own argument -- so the resolution is not a guess; `.py` only,
    # because `tests/fixtures/*.json` is wider than what the row's `what` claims.
    # [M4.16 cycle 4, M416-C4-SPEC15-01]
    if named.startswith("tests/") and named.endswith(".py"):
        return REPO / "backend" / named
    if _BARE_TEST.fullmatch(named):
        return REPO / "backend" / "tests" / named
    if named.startswith("ops/") and named.endswith(".py"):
        return REPO / named
    if named.startswith("backend/spielplan/") and named.endswith(".py"):
        return REPO / named
    if named.startswith("spielplan/") and named.endswith(".py"):
        return REPO / "backend" / named
    if head in packages and named.endswith(".py"):
        return REPO / "backend" / "spielplan" / named
    if named.startswith("frontend/"):
        return REPO / named
    if named.startswith(_FRONTEND_ROOTS):
        here = REPO / "frontend" / named
        return here if here.exists() else REPO / "frontend" / "src" / named
    return None


def _spielplan_packages() -> set[str]:
    """`api`, `db`, `home`, `sync`, ... read off the tree, because the relative spelling is what
    the citations use: `db/library.py`, never `spielplan/db/library.py`."""
    return {p.name for p in SPIELPLAN.iterdir() if p.is_dir() and not p.name.startswith("_")}


def _comment_named_files(roots) -> dict[str, list[str]]:
    """Every in-scope path named by a comment under `roots`, mapped to where it is named."""
    packages = _spielplan_packages()
    named: dict[str, list[str]] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in _COMMENT_SOURCES:
                continue
            where = path.name if REPO not in path.parents else path.relative_to(REPO).as_posix()
            for line, prose in _comment_prose(path):
                found = _QUALIFIED_PATH.findall(prose) + _BARE_TEST.findall(prose)
                for candidate in found:
                    if _named_in_scope(candidate, packages) is not None:
                        named.setdefault(candidate, []).append(f"{where}:{line}")
    return named


def _missing_named_files(roots) -> list[str]:
    packages = _spielplan_packages()
    return [
        f"{candidate} (named at {', '.join(sorted(set(sites)))})"
        for candidate, sites in sorted(_comment_named_files(roots).items())
        if candidate not in COMMENT_PATH_EXCEPTIONS
        and not _named_in_scope(candidate, packages).exists()
    ]


COMMENTED_TREES = (SPIELPLAN, FRONTEND, FRONTEND_STATIC)


def test_every_file_a_comment_names_exists():
    """The citation a reader follows resolves, or it costs them the hour it cost the last one.

    Three named files did not exist when this milestone opened, and the reason the count is three
    rather than zero is that nothing had ever followed one mechanically. `ops/fetch-fonts.py` was
    the ordinary case and M4.15 wrote the script, which is the repair; two of the standing
    exceptions below are the shapes that cannot be repaired that way -- a comment inside an
    applied, checksummed migration, and a comment about an asset the project deliberately does
    not hold. The list holds THREE, and the third is neither: `sync/resolve.py` is an ordinary
    wrong citation (it means `connectors/resolve.py`) that happens to sit across an ownership
    line, in two files the milestone that owns `backup/` will touch next, so it is recorded here
    rather than repaired there. That is the count a reader auditing this list needs, and it went
    unsaid in the file whose whole subject is a record claiming something the object beneath it
    does not hold; `docs/RELEASE.md` section 5.6 carries the same three against the row's
    deliberate ONE. Note which SITE each entry is held at, too: `.sql` is not in
    `_COMMENT_SOURCES`, so `0004_dna.sql:11` is never read here at all -- the `db/dna.py` entry
    stays live because `db/library.py`'s repair docstring names the path a second time.
    [M4.16 cycle 4, M416-C4-SPEC15-02]

    The scope is the row's four shapes, resolved the way this codebase spells them: a bare
    `test_x.py` because there is one tests directory, and `db/library.py` because inside the
    package nobody writes the package name. What is NOT read is everything a comment mentions
    that this tree could not hold -- CPython's `Lib/asyncio/mixins.py`, the corpus project's
    `mdc/export.py`, the bundle's internal `artifacts/manifest.json`, SvelteKit's served
    `_app/version.json`. Demanding those would make this guard wrong on arrival, and a guard that
    is wrong on arrival is switched off rather than obeyed.
    [CLAUDE.md Conventions; row `platform-comments-name-files-that-exist`]
    """
    missing = _missing_named_files(COMMENTED_TREES)
    assert not missing, (
        "a comment names a file that is not in the tree:\n  "
        + "\n  ".join(missing)
        + "\n\nCorrect the citation, or write the file. An entry in COMMENT_PATH_EXCEPTIONS is "
        "for a path that cannot be made to resolve at all -- it is dated, it carries its reason, "
        "and it is held to the absence it describes."
    )


def test_the_comment_path_exceptions_are_not_stale():
    """An exception list nobody re-reads becomes an allow-list, which is one indirection worse
    than the defect it was written for.

    Held in both directions. A path that now EXISTS is no longer an exception, and leaving the
    entry would quietly exempt a real citation from the guard the day somebody re-used the name.
    A path no comment names any more is an entry describing nothing, which is the same record
    drift this milestone exists to close, in the file that closes it.
    """
    packages = _spielplan_packages()
    named = _comment_named_files(COMMENTED_TREES)
    landed = [
        f"{candidate}: {COMMENT_PATH_EXCEPTIONS[candidate].split(':', 1)[0]}"
        for candidate in COMMENT_PATH_EXCEPTIONS
        if _named_in_scope(candidate, packages).exists()
    ]
    assert not landed, (
        "these paths are excepted from the comment-citation guard and now exist, so the exception "
        "is exempting a live citation rather than recording an absence. Delete the entry, in the "
        "same change as whatever landed the file:\n  " + "\n  ".join(landed)
    )
    unnamed = [candidate for candidate in COMMENT_PATH_EXCEPTIONS if candidate not in named]
    assert not unnamed, (
        "these exceptions describe a citation no comment makes any more; the comment was fixed "
        "and the exception outlived it:\n  " + "\n  ".join(sorted(unnamed))
    )
    # And the third direction, which is neither the dict nor the tree but the sentence ABOVE
    # them: the guard's docstring enumerated "the two shapes that cannot be repaired that way"
    # over a dict holding three, and the third -- an ordinary wrong citation sitting across an
    # ownership line -- was described nowhere. A reader auditing the list counted two, found
    # three, and could not tell whether the extra entry was argued or merely added. That is the
    # record-versus-object gap this milestone exists to close, two lines apart, in the file that
    # closes it. Held as a count rather than as prose so the sentence cannot go stale again the
    # next time an entry is added. [decision 184; M4.16 cycle 4, M416-C4-SPEC15-02]
    doc = test_every_file_a_comment_names_exists.__doc__ or ""
    published = re.search(r"The list holds ([A-Za-z]+)", doc)
    word = published.group(1).lower() if published else None
    counted = _COUNT_WORDS.index(word) if word in _COUNT_WORDS else -1
    assert counted == len(COMMENT_PATH_EXCEPTIONS), (
        f"the comment-path guard's docstring publishes {word!r} standing exceptions and the list "
        f"holds {len(COMMENT_PATH_EXCEPTIONS)}. The docstring is what a reader audits this list "
        "against, so it states the count as a word -- \"The list holds THREE\" -- and says what "
        "shape each entry beyond the two unrepairable ones is."
    )
    # And the fourth, which is the identical drift twenty-five lines down and was left there by the
    # repair above. The self-test's docstring opened "Seven shapes" over a parametrize list of
    # eight, cycle 4 having added the eighth and not the word -- so a reader auditing whether that
    # guard reads "every shape a citation comes in" counted seven, found eight, and could not tell
    # which case was argued and which was merely added. Held as a count for the same reason, off
    # the decorator rather than off a second sentence, so the two cannot disagree again.
    # [decision 184; M4.16 cycle 5, M416-C5-SPEC15-03]
    shapes = test_the_comment_path_guard_reads_every_shape_a_citation_comes_in
    cases = [
        len(mark.args[1]) for mark in getattr(shapes, "pytestmark", [])
        if mark.name == "parametrize"
    ]
    assert len(cases) == 1, (
        f"the shape self-test carries {len(cases)} parametrize decorators, and this rule reads its "
        "case count off exactly one. Re-read it before trusting the number below."
    )
    published = re.search(r"([A-Za-z]+) shapes", shapes.__doc__ or "")
    word = published.group(1).lower() if published else None
    counted = _COUNT_WORDS.index(word) if word in _COUNT_WORDS else -1
    assert counted == cases[0], (
        f"the shape self-test's docstring publishes {word!r} shapes and its parametrize list holds "
        f"{cases[0]}. That sentence is the only statement anywhere of what 'every shape a citation "
        "comes in' means, so it is stated as a word and re-derived here rather than restated by "
        "hand the next time a shape is added."
    )


@pytest.mark.parametrize(
    ("name", "filename", "body"),
    [
        ("a hash comment", "m.py", "# the write is claimed in `api/nowhere.py` first\nX = 1\n"),
        ("a docstring", "m.py", '"""Reads §4.1. See `spielplan/db/nowhere.py`."""\nX = 1\n'),
        ("a trailing comment", "m.py", "X = 1  # measured by ops/nowhere.py against the bundle\n"),
        ("a bare test name", "m.py", '"""test_nowhere.py greps this package to keep it that way."""\n'),
        ("a tests/-prefixed path", "m.py", "# greps this package: tests/test_nowhere.py\nX = 1\n"),
        ("a JSDoc block", "S.svelte", "<script>\n/** see frontend/src/lib/nowhere.js */\n</script>\n"),
        ("a line comment", "S.svelte", "<script>\n// finished in lib/nowhere.js\n</script>\n"),
        ("a css comment", "f.css", "/* written by backend/tests/test_nowhere.py */\nbody { color: red }\n"),
        ("a markup comment", "S.svelte", "<!-- the write is claimed in lib/nowhere.js first -->\n"),
        ("a SvelteKit route file", "m.py",
         "# rendered by frontend/src/routes/admin/nowhere/+page.svelte\nX = 1\n"),
    ],
)
def test_the_comment_path_guard_reads_every_shape_a_citation_comes_in(tmp_path, name, filename, body):
    """docs/TESTING.md: a guard that cannot fail reads as coverage while providing none.

    Ten shapes, and five of them are ones an earlier draft of this reader missed. A trailing
    `#` comment is why Python is tokenized rather than scanned line by line; a docstring is where
    most of this codebase's citations actually live; and a bare `test_x.py` is the spelling that
    makes the guard worth having, since a registered test file being renamed is the drift most
    likely to happen next. The `tests/`-prefixed path is the fourth, the shape this row was opened
    for and the one it could not see (M416-C4-SPEC15-01).

    The last two arrived in review cycle 5 and are the two shapes unique to the frontend. A
    `<!-- -->` block is the comment form of Svelte markup -- the reason `.svelte` and `.html` are
    in `_COMMENT_SOURCES` at all -- and was read by nothing, while both of this function's other
    `.svelte` cases sit inside `<script>`. And a `+page.svelte` could not be extracted by
    `_QUALIFIED_PATH` at all, because `+` was outside its final segment's character class: every
    SvelteKit route file in the tree was invisible, including the one `api/artifacts.py`'s
    docstring names in full. [M4.16 cycle 5: M416-C5-SPEC15-01, M416-C5-SPEC15-02]
    """
    (tmp_path / filename).write_text(body, encoding="utf-8")
    assert _missing_named_files([tmp_path]), f"{name} named a file that is not there, unnoticed"


def test_the_comment_path_guard_leaves_the_citations_around_it_alone(tmp_path):
    """The other half: four things it must NOT call a broken citation.

    A path this tree really holds; the corpus project's own modules, which this repository has no
    copy of by design (README: the bundle is not vendored); a URL, which is `//` and a path and
    neither a comment nor a citation; and CPython's source, cited in `core/auth.py` to explain why
    a semaphore is re-made. A guard that failed on any of these would be narrowed by the first
    person it stopped, and a narrowed guard is the thing this file keeps arguing against.
    """
    (tmp_path / "m.py").write_text(
        "# resolved in `connectors/resolve.py` and archived by `backup/movie_data.py`\n"
        "# `mdc/export.py:34-45`, verbatim; the corpus is not vendored here\n"
        "# CPython `Lib/asyncio/mixins.py` binds it; the bundle's own artifacts/manifest.json\n"
        '"""See https://example.com/app.js and the served _app/version.json."""\n',
        encoding="utf-8",
    )
    assert not _missing_named_files([tmp_path])


# --- M4.16 cycle 4: rule 8 is quoted as it reads, or dated as it read --------------------------
#
# Phase B item 8 replaced §4.1 rule 8 with the conservative heuristic `importer/reviews.py` has
# always applied, and the amended clause closes by saying that "fixed individually" was per-row
# knowledge this repository has never had. The sentence it replaced had been copied into five
# places and none of them moved with it. `load.py:17` was the expensive one: it listed the row
# count under "Rules enforced during the load", with nothing anywhere in that module to say the
# clause had been struck -- so a maintainer following CLAUDE.md's "where code and spec disagree,
# the code is the bug" reads the retired sentence as the requirement and `reviews.py`'s
# marker-guarded round trip as over-broad. What that reading licenses is narrowing the repair to a
# row list this repository has never possessed, which is the amendment's own direction inverted.
#
# The rule is NOT "the words must be gone", because three carriers have to keep them.
# `importer/reviews.py` and `test_review_mojibake.py` were written against the sentence and the
# census that retired it only reads as an argument beside the claim it answers; the third,
# `0003_content.sql`, is applied and sha256-checksummed, so an edit there is a hard startup error
# on every install that has run it and the correction has to live somewhere else. So the rule is
# that the words may not stand UNDATED: every occurrence sits within a paragraph of `_RULE8_DATED`,
# a phrase nobody types by accident, and the sealed one is an exception held to the module that
# carries its correction -- the shape `0004_dna.sql`'s entry takes one section up.
#
# A form of words IS the mechanism here, and what stops it being only a form of words is the
# premise asserted first: the normative file must still carry the heuristic and must not carry the
# row list. The day the strike is undone the escape stops being available, rather than quietly
# outliving the amendment it dates. [§4.1 rule 8; M4.16 cycle 4, M416-C4-SPEC-03]

# Two spellings of one retired claim: the sentence as §4.1 carried it, and the expectation
# `importer/validate.py` kept as a dict key that nothing read and nothing could check.
_RULE8_ROW_LIST = re.compile(r"73 known[- ]mojibake review rows|mojibake_review_rows", re.I)
_RULE8_DATED = "as it read until M4.16"
_RULE8_WINDOW = 700
_RULE8_HEURISTIC = "conservative heuristic, not a row list"

# The carrier that cannot be dated and cannot be deleted. `0003_content.sql:236-237` block-quotes
# rule 8 above `review_store.review`, and the file is applied and sha256-checksummed. It is held
# in both directions below: the quote must still be there, because its disappearance means the
# migration was edited, and the module that owns the rule must name it.
_RULE8_SEALED = "backend/migrations/0003_content.sql"
_RULE8_SEALED_CORRECTION = "backend/spielplan/importer/reviews.py"


def _rule8_flat(text: str) -> str:
    """One file's prose with wrapping, indentation and `#` / `--` comment markers taken off."""
    return " ".join(" ".join(re.sub(r"^\s*(?:#+|--)\s?", "", line) for line in text.splitlines()).split())


def _rule8_undated(text: str) -> list[str]:
    """The retired row list wherever it stands in one body with nothing above it dating it.

    Read flat, and read case-insensitively, because both halves of this sentence wrap: `load.py`
    splits the dating heading across two lines of a docstring and `validate.py` splits the claim
    itself across two `#` lines, so a rule that saw either only unsplit would pass every file it
    was written for. `_flat_notice_text` two sections up flattens for the same reason. The heading
    also opens a sentence in two carriers and sits mid-sentence in the third, and a capital A is
    not a different claim -- a rule that turned on it would be a rule about punctuation.
    """
    flat = _rule8_flat(text)
    undated = []
    for found in _RULE8_ROW_LIST.finditer(flat):
        window = flat[max(0, found.start() - _RULE8_WINDOW):found.start()].lower()
        if _RULE8_DATED.lower() not in window:
            undated.append(found.group(0))
    return undated


def _rule8_carriers() -> list[Path]:
    """Everything the sweep reads: the package, the suite, the migrations, and the map.

    The map is in scope because it is where this repository writes down what a rule requires, and
    its rows quote spec sentences by design; the normative file is not swept but asserted directly,
    since the one thing it may not do is carry the sentence at all. `docs/milestones/*.md` is out,
    the way `test_no_file_repeats_a_retired_claim_about_the_company_table` puts it out: the plans
    are dated records the workflow forbids editing, so a sweep over them could never be satisfied.
    """
    return sorted(
        [*SPIELPLAN.rglob("*.py"),
         *(REPO / "backend" / "tests").rglob("*.py"),
         *(REPO / "backend" / "migrations").glob("*.sql"),
         REPO / "backend" / "tests" / "spec_coverage.toml"]
    )


def test_no_file_states_rule_8_as_the_row_list_the_spec_stopped_carrying():
    """A struck normative sentence restated as the rule is worse than a stale file name.

    `docs/milestones/*.md` is not swept -- it is the plan, the workflow forbids editing it, and
    the corrections owed there go to the owner by hand -- which is the same scope
    `test_no_file_repeats_a_retired_claim_about_the_company_table` takes for the same reason.
    [§4.1 rule 8; M4.16 cycle 4, M416-C4-SPEC-03]
    """
    spec = _src(_normative_file())
    assert _RULE8_HEURISTIC in spec, (
        f"the normative file no longer says rule 8 is repaired by a {_RULE8_HEURISTIC!r}, so the "
        "amendment this guard dates against has been undone or reworded. Re-read rule 8 in the "
        "normative file first: the dating escape is only honest while the strike holds."
    )
    assert not _RULE8_ROW_LIST.search(spec), (
        "the normative file carries rule 8's row list again. The strike rests on a census of 86 "
        "marked rows and 0 repairs over 485,602 -- 'fixed individually' was per-row knowledge "
        "this repository has never had, and no artifact here enumerates those rows."
    )
    offenders = []
    scanned = 0
    for path in _rule8_carriers():
        if path == Path(__file__).resolve():
            continue        # this file states the sentence in order to refuse it
        where = path.relative_to(REPO).as_posix()
        if where == _RULE8_SEALED:
            continue        # sealed; held by the test below, which is the only repair available
        scanned += 1
        offenders += [f"{where}: {spelling!r}" for spelling in _rule8_undated(_src(path))]
    assert scanned > 150, f"the sweep read {scanned} files and is not covering the tree"
    assert not offenders, (
        "rule 8's retired row list stands here as the rule:\n  "
        + "\n  ".join(offenders)
        + "\n\nRule 8 states a heuristic, not an enumeration. Either say what ships, or head the "
        f"quote with {_RULE8_DATED!r} so it reads as history rather than as the requirement."
    )


def test_the_sealed_migration_that_quotes_rule_8_is_corrected_where_it_can_be():
    """The one copy that cannot be repaired in place, and the module that answers for it.

    Both halves matter. If the quote leaves `0003_content.sql`, an applied and sha256-checksummed
    migration has been edited, and every install that has run it fails its next boot on a checksum
    mismatch -- so the guard reports that rather than quietly passing on a file that got "tidied".
    If the naming leaves `importer/reviews.py`, the sealed copy is again a sentence with nothing
    anywhere saying it was struck, which is the defect this section exists for.
    """
    sealed = _src(REPO / _RULE8_SEALED)
    assert _RULE8_ROW_LIST.search(sealed), (
        f"{_RULE8_SEALED} no longer quotes rule 8's row list, which means an applied, checksummed "
        "migration has been edited in place. That is a hard startup error for every install that "
        "has run it; restore the file. The quote is why this exception exists."
    )
    correction = _rule8_flat(_src(REPO / _RULE8_SEALED_CORRECTION))
    assert _RULE8_DATED.lower() in correction.lower() and "0003_content.sql" in correction, (
        f"{_RULE8_SEALED_CORRECTION} no longer dates rule 8's retired sentence and names "
        f"{_RULE8_SEALED}, so the one copy that cannot carry its own correction has lost it. The "
        "module that owns the rule is where it goes, the way `db/library.py` answers for "
        "`0004_dna.sql`."
    )


@pytest.mark.parametrize(
    ("name", "text", "refused"),
    [
        ("the loader's bullet as it stood",
         "  * rule 8 - text is passed through untouched except for the 73 known-mojibake review "
         "rows.", True),
        ("the validator's dict key",
         '    "mojibake_review_rows": 73,      # rule 8', True),
        ("the claim wrapped across two comment lines, undated",
         "# text is passed through untouched except for the 73\n# known-mojibake review rows.",
         True),
        ("the loader's bullet, dated mid-sentence and wrapped between the two",
         '  * rule 8 - rule 8 as it read until\n             M4.16 named "the 73 known-mojibake '
         'review rows".', False),
        ("the block quote, dated by a heading that opens a sentence",
         'As it read until M4.16:\n\n    "... the 73 known-mojibake review rows are fixed '
         'individually in the importer."', False),
        ("the module's own refusal of the phrase",
         'Rule 8\'s "fixed individually" is per-row knowledge this repository does not have', False),
        ("a census, which states rows and a count and no enumeration",
         "486 of 485,602 review rows carry a mojibake marker and 73 of those are truncated", False),
    ],
)
def test_the_rule_8_guard_reads_an_undated_claim_and_not_the_prose_around_it(name, text, refused):
    """Both directions, because either alone would be a rule nobody can satisfy.

    Refusing the first three is the repair, and the third is the one a 108-column file produces by
    itself: the claim wrapped over two comment lines is the same claim. ADMITTING the last four is
    what keeps the rule usable. The module that implements rule 8 has to be able to quote the
    sentence it was written against -- dated mid-sentence or under a heading, and wrapped either
    way -- it has to be able to say in its own words that "fixed individually" is knowledge nobody
    here has, and a census that happens to count 73 of something is prose about a measurement
    rather than the enumeration §4.1 stopped promising. A guard that reddened on those would be
    narrowed by the first person it stopped, and a narrowed guard is what this file keeps arguing
    against.
    """
    assert bool(_rule8_undated(text)) is refused, name


# --- M4.16 review cycle 4: the diff report §10 replaced with its own denial ---------------------
#
# Phase B item 4 replaced §10's re-import sentence with decisions 162/163's reading, and unlike
# rule 8's strike one section up it does not merely drop the old words - it denies the referent:
# "Model re-import (retrained backbone) is a planned admin event with a migration report - never a
# silent sync, and never a diff report, which does not exist." The sentence it replaced had been
# copied into sixteen live carriers across thirteen files, and Phase E moved exactly one of them
# (README, which the phase names by line). Two of the sixteen are why this is a guard and not a
# one-off sweep: `frontend/src/routes/admin/data/+page.svelte` RENDERS the retired clause to the
# operator inside §6.6's Data card, and `data-rules-the-stored-report-is-the-whole-report` quoted
# it in its `spec` field - so an auditor following a shipped row's authority to §10 met the
# phrase's explicit denial with the map green.
#
# Decision 305's authority rule is the one that should have caught that row and provably cannot:
# it asks a `spec` string to NAME a section, a decision or a document path, and "§10" is named.
# The quotation reader beside it resolves a quoted sentence only against a `spec` field that also
# names a FILE, which the section-only idiom most rows use never does. So neither half of the map
# guard could see a row citing the sentence its own section refuses.
#
# The rule is NOT "the words must be gone", for rule 8's reason: three carriers have to keep them.
# `0001_system.sql:32` block-quotes the sentence above `artifact_bundle` and is applied and
# sha256-checksummed, so an edit there is a hard startup error on every install that has run it;
# README's "Where the model comes from" paragraph and `importer/report.py`'s docstring both state
# the phrase in order to say it was retired. So the rule is that the words may not stand UNDATED:
# every occurrence sits within a paragraph of `_DIFF_REPORT_DATED`, a phrase nobody types by
# accident, and the sealed one is answered for in the module that owns §10's report - the shape
# `importer/reviews.py` takes for `0003_content.sql` one section up, and `db/library.py` for
# `0004_dna.sql`.
#
# A form of words IS the mechanism here, and what stops it being only a form of words is the
# premise asserted first: the normative file must carry the migration report AND the denial. The
# day the amendment is undone the escape stops being available rather than quietly outliving the
# sentence it dates. [§10; M4.16 cycle 4, M416-C4D2-SPEC-01]
_DIFF_REPORT = re.compile(r"diff report", re.I)
_DIFF_REPORT_DATED = "as §10 read until M4.16"
_DIFF_REPORT_WINDOW = 700
_DIFF_REPORT_KEPT = "a planned admin event with a migration report"
_DIFF_REPORT_STRUCK = "never a diff report, which does not exist"

# The carrier that cannot be dated and cannot be deleted, and the module that answers for it.
# `0001_system.sql` creates `artifact_bundle` under the retired sentence; `importer/report.py` is
# the module §10's report belongs to and the only other file that has to quote the clause.
_DIFF_REPORT_SEALED = "backend/migrations/0001_system.sql"
_DIFF_REPORT_CORRECTION = "backend/spielplan/importer/report.py"


def _diff_report_flat(text: str) -> str:
    """One body's prose with wrapping, indentation and comment furniture taken off.

    Wider than `_rule8_flat` because these carriers are not all Python: the clause travels through
    `#` and `--` comments, through a JSDoc block's leading `*`, through `//`, through a `<!-- -->`
    markup comment and through Markdown emphasis, and at 108 columns either word can end a line.
    A rule that saw only the unsplit spelling would pass most of the files it was written for.
    """
    stripped = re.sub(r"(?m)^\s*(?:#+|--|//|\*+|<!--|-->)\s?", " ", text)
    return " ".join(stripped.split())


def _diff_report_undated(text: str) -> list[str]:
    """§10's retired clause wherever it stands in one body with nothing above it dating it.

    Two words are all the spelling there is, so the excerpt rather than the match is what comes
    back: a list of matches would say how many and never where, and sixty characters of lead-in is
    what a reader needs to find the line.

    The second escape is not a form of words the way the dating one is. §10's replacement DENIES
    the referent rather than dropping a phrase, so the live sentence contains the retired words --
    and a record quoting that denial is quoting the spec as it stands, not citing the clause it
    replaced. Held to the denial's own span rather than to a window, which is the tight reading: a
    stale sentence elsewhere in a file that also quotes the denial is a separate match outside it
    and stays refused, so the escape cannot be spent on a sentence it does not cover.
    """
    flat = _diff_report_flat(text)
    quoting = [
        (found.start(), found.end())
        for found in re.finditer(re.escape(_DIFF_REPORT_STRUCK), flat, re.I)
    ]
    undated = []
    for found in _DIFF_REPORT.finditer(flat):
        if any(lo <= found.start() and found.end() <= hi for lo, hi in quoting):
            continue        # §10's own current words, not the clause they replaced
        window = flat[max(0, found.start() - _DIFF_REPORT_WINDOW):found.start()].lower()
        if _DIFF_REPORT_DATED.lower() not in window:
            undated.append(flat[max(0, found.start() - 60):found.end() + 40])
    return undated


def _diff_report_carriers() -> list[Path]:
    """Everything the sweep reads: the package, the suite, the migrations, the map, the surface
    and the two satellite documents Phase E owns.

    `frontend/src` is in scope because the headline carrier is markup an operator READS, which is
    where a stale citation stops being a maintainer's problem and becomes a false statement to the
    household. `docs/milestones/*.md` and `docs/spec-v2.2-proposals.md` are out, for the reasons
    the two sweeps above give: the plans are dated records the workflow forbids editing, and the
    register is dated reasoning decision 304 keeps as written. The normative file is not swept but
    asserted directly, since the one thing it has to do is carry the denial.
    """
    return sorted(
        [*SPIELPLAN.rglob("*.py"),
         *(REPO / "backend" / "tests").rglob("*.py"),
         *(REPO / "backend" / "migrations").glob("*.sql"),
         *(p for p in FRONTEND.rglob("*") if p.suffix in {".svelte", ".js", ".css"}),
         REPO / "backend" / "tests" / "spec_coverage.toml",
         REPO / "README.md",
         REPO / "docs" / "TESTING.md"]
    )


def test_no_file_cites_the_diff_report_the_spec_replaced_with_its_denial():
    """A retired clause is worse than a stale file name when the section now denies the referent.

    `docs/milestones/*.md` is not swept -- it is the plan, the workflow forbids editing it, and
    the corrections owed there go to `docs/RELEASE.md` section 4 under decision 296 -- which is
    the scope `test_no_file_states_rule_8_as_the_row_list_the_spec_stopped_carrying` takes for the
    same reason. [§10; M4.16 cycle 4, M416-C4D2-SPEC-01]
    """
    spec = _src(_normative_file())
    assert _DIFF_REPORT_KEPT in spec, (
        f"the normative file no longer says a re-import is {_DIFF_REPORT_KEPT!r}, so the "
        "amendment this guard dates against has been undone or reworded. Re-read §10 first: the "
        "dating escape is only honest while the replacement holds."
    )
    assert _DIFF_REPORT_STRUCK in spec, (
        f"the normative file no longer says {_DIFF_REPORT_STRUCK!r}. The point of this amendment "
        "is that it denies the referent rather than dropping a phrase, and a carrier dated "
        "against a denial the file stopped making is dated against nothing."
    )
    offenders = []
    scanned = 0
    for path in _diff_report_carriers():
        if path == Path(__file__).resolve():
            continue        # this file states the clause in order to refuse it
        where = path.relative_to(REPO).as_posix()
        if where == _DIFF_REPORT_SEALED:
            continue        # sealed; held by the test below, which is the only repair available
        scanned += 1
        offenders += [f"{where}: ...{excerpt}..." for excerpt in _diff_report_undated(_src(path))]
    assert scanned > 200, f"the sweep read {scanned} files and is not covering the tree"
    assert not offenders, (
        "§10 says a re-import produces a migration report and that a diff report does not exist, "
        "and these cite the clause it replaced as the live authority:\n  "
        + "\n  ".join(offenders)
        + "\n\nName the report the importer actually writes, or head the quote with "
        f"{_DIFF_REPORT_DATED!r} so it reads as history rather than as the requirement."
    )


def test_the_sealed_migration_that_quotes_the_diff_report_is_corrected_where_it_can_be():
    """The one copy that cannot be repaired in place, and the module that answers for it.

    Both halves matter. If the quote leaves `0001_system.sql`, an applied and sha256-checksummed
    migration has been edited, and every install that has run it fails its next boot on a checksum
    mismatch -- so the guard reports that rather than quietly passing on a file that got "tidied".
    If the dating leaves `importer/report.py`, the sealed copy is again a sentence with nothing
    anywhere saying it was retired, which is the defect this section exists for.
    """
    sealed = _src(REPO / _DIFF_REPORT_SEALED)
    assert _DIFF_REPORT.search(sealed), (
        f"{_DIFF_REPORT_SEALED} no longer quotes §10's retired clause, which means an applied, "
        "checksummed migration has been edited in place. That is a hard startup error for every "
        "install that has run it; restore the file. The quote is why this exception exists."
    )
    correction = _diff_report_flat(_src(REPO / _DIFF_REPORT_CORRECTION))
    assert _DIFF_REPORT_DATED.lower() in correction.lower() and "0001_system.sql" in correction, (
        f"{_DIFF_REPORT_CORRECTION} no longer dates §10's retired clause and names "
        f"{_DIFF_REPORT_SEALED}, so the one copy that cannot carry its own correction has lost "
        "it. The module that owns §10's report is where it goes, the way `importer/reviews.py` "
        "answers for `0003_content.sql`."
    )


@pytest.mark.parametrize(
    ("name", "text", "refused"),
    [
        ("the Data card's rendered sentence, as it shipped",
         "      re-import is a planned event with a diff report - never a silent sync.", True),
        ("the shipped row's spec field",
         'spec = "§10 (\\"a planned admin event with a diff report\\") + §6.6 Data"', True),
        ("the claim wrapped across two comment lines, undated",
         "    # §10 calls a re-import a planned admin event\n    # with a diff report, so "
         "replacing the tier is what that report describes.", True),
        ("a JSDoc line, undated",
         " * The operator reads §10's diff report on the Data card.", True),
        ("README's sentence, dated",
         "As §10 read until M4.16 it called this a diff report; no diff report was ever written, "
         "and §10 now names the one the importer produces.", False),
        ("the module's dated correction, naming the sealed migration",
         'As §10 read until M4.16 this was "a planned admin event with a diff report"; '
         "`backend/migrations/0001_system.sql:32` block-quotes it and is checksummed.", False),
        ("the report §10 now names",
         "§10 calls a re-import a planned admin event with a migration report.", False),
        ("a diff that is not a report",
         "the stored report is compared against the previous one, and that diff is the rebuild "
         "set the swap recomputes.", False),
        ("a record quoting §10's denial as §10 now writes it",
         "§10 now says in as many words that what the importer writes is a migration report, "
         "\"never a diff report, which does not exist\".", False),
        ("a stale sentence in a body that also quotes the denial",
         "the row is §10's diff report. Elsewhere: §10 says never a diff report, which does not "
         "exist.", True),
    ],
)
def test_the_diff_report_guard_reads_an_undated_claim_and_not_the_prose_around_it(
    name, text, refused
):
    """Both directions, because either alone would be a rule nobody can satisfy.

    Refusing the first four is the repair, and two of them are shapes `_rule8_flat` cannot see:
    the rendered markup line and the JSDoc `*`, which is why this section flattens wider than that
    one. ADMITTING the last four is what keeps the rule usable. README and the report module have
    to be able to say what the clause used to be; the sentence §10 now carries must not be read as
    the thing it replaced; and the last case is the false negative that matters most -- "diff" is
    still the right word for the comparison the rebuild set IS, and a rule that reddened on it
    would be a rule about a word rather than about the retired claim.

    The last pair is the denial escape held in both directions: a record quoting §10's current
    sentence is quoting the spec, and the SAME body carrying a stale citation as well is still
    refused for that citation -- otherwise one quotation of the denial would buy silence for every
    retired claim in the file.
    """
    assert bool(_diff_report_undated(text)) is refused, name


# --- M4.16 review cycle 5: the CC BY-SA credit links no material, and says so under a number ----
#
# Phase J asked two things of the Wikipedia and TVmaze credits -- "CC BY-SA credit, linking to the
# article/show where `title_meta` carries it" -- and only the credit shipped. The second half fell
# between the plan and decision 298, whose ruling enumerates the five notices and omits the link
# without saying it is omitting one, so the only argument anywhere was four lines of
# `DataSources.svelte` citing no decision at all, in a header where every other argued departure
# cites 276, 293, 298, 318 or 319. Decision 320 rules on it, and the ruling rests on a measurement
# this guard RE-RUNS rather than restates: nothing under `backend/spielplan` or `frontend/src`
# reads `title_meta`'s `homepage` or the corpus's `wikipedia_title`, so this build holds no article
# identifier outside a `payload` jsonb nothing queries and `/api/titles/{id}` exposes no field a
# link could be built from.
#
# The polarity is decision 307's, which section 5.4 already uses: the guard goes red the day a
# reader lands, because that is the day `docs/RELEASE.md` section 4.7 becomes the false record and
# the two come out in one change. What it does NOT assert is the absence of an anchor in the
# markup -- the block may carry one the moment there is something to link, and the deferral is
# about the identifier, not about the design. [decision 320; M4.16 cycle 5, M416-C5-ATTR-01]
_MATERIAL_LINK_ARGUMENT = "CREDITED, NOT DEEP-LINKED"
_MATERIAL_LINK_DECISION = 320
_MATERIAL_LINK_WINDOW = 600      # only for a body with no closing `*/`; see below

# An ACCESS, not a mention, for `_SOURCES_READ`'s reason one section down: the paragraph that
# argues why this build carries no material link has to NAME the columns it cannot read, so a rule
# over the bare word reads the argument for this guard as the thing that discharges it -- measured,
# on the first run after that paragraph landed. An attribute or a string literal is every shape a
# read comes in here: `payload["homepage"]` and `.get("homepage")` in the importer, `->>'homepage'`
# in a query, `meta.homepage` on the surface. Prose spells a column in backticks, which is none of
# them -- EXCEPT when it spells the table with it, and the next author to rewrite that paragraph
# should know before this reddens on them: a prose "`title`'s `wikipedia_title`" is invisible here
# and a prose "`title.wikipedia_title`" is an ACCESS as far as the pattern is concerned, so the
# argument for the deferral can turn the guard over the deferral red by saying the same thing in
# the ordinary way. Measured on the first run after decision 425's rewrite of it.
# [M5.3 review cycle 1, M53-COV-01]
# The two names are `title_meta`'s own field and the corpus's `title` column -- `load.py`
# does not map the second onto `title` at all, and `importer/meta.py` keeps the whole corpus row as
# `payload` jsonb that only `resolve_title_fields` reads back, by field name and never by either.
_ARTICLE_CARRIER = re.compile(
    r"""\.(?:homepage|wikipedia_title)\b|['"](?:homepage|wikipedia_title)['"]"""
)


# WHERE THE PREMISE STOPPED BEING TRUE, AND WHAT IS LEFT OF IT. Decision 320's premise has two
# halves and the paragraph above states both: this build "holds no article identifier", AND
# "`/api/titles/{id}` exposes no field a link could be built from". M5.3 makes the FIRST half false
# and does not get to narrow a guard past it. `0026_acquisition_sources.sql` adds
# `title.wikipedia_title`, `wikidata:resolve` fills it, `sources/wikipedia.py:213` reads it back,
# and `derive/parse.py:884` writes `https://en.wikipedia.org/wiki/{article}` into `title_meta`'s
# `homepage` - so this build now holds the identifier AND computes the exact URI CC BY-SA asks to
# be linked. Decision 320's instrument was commissioned to go red on that day and section 4.7 to
# come out with the anchor; M5.3 ships no title surface and serves no new field, so decision 425
# re-measures the ground instead: the record says what is now true, the component's paragraph says
# it, and the debt stays published under a second number rather than being quietly re-scoped.
#
# THE RULE THEREFORE HAS TWO SIDES, because the deferral now rests on the exposure half alone and
# a one-sided guard cannot see which half moved:
#
#   * the package HOLDS it - asserted, not assumed, so the day nothing reads either column again
#     section 4.7's re-measurement is the false record in the other direction;
#   * nothing SERVES it - `api/` shapes every response this app sends, `db/` is where the rows
#     those responses carry are selected, and `frontend/src` renders them.
#
# `db` is in the roots and its absence was the second detection hole: `db/library.py:400` is
# `SELECT t.*` and `:408` returns `dict(row)`, so `title.wikipedia_title` already transits into
# `api/library.py` as a live value with the string appearing in no file at all. What keeps it off
# the wire is the explicit key tuple at `api/library.py:107-111`, which a string-matching walk over
# `api/` cannot hold - naming `db` is what puts the layer that builds the row inside the rule.
# [decisions 320, 372, 425; M5.3 review cycle 1, M53-COV-01]
_ARTICLE_CARRIER_ROOTS = ("api", "db")
_MATERIAL_LINK_REMEASURED = 425


def _carries_the_article_identifier(roots: list[Path]) -> list[str]:
    """Files under `roots` that read `title_meta`'s `homepage` or the corpus's `wikipedia_title`."""
    found = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".svelte", ".js"}:
                continue
            if _ARTICLE_CARRIER.search(_src(path)):
                found.append(path.relative_to(REPO).as_posix())
    return found


def _reads_the_article_carrier() -> list[str]:
    """Route, query and surface files that read the column a material link would come from."""
    return _carries_the_article_identifier(
        [SPIELPLAN / part for part in _ARTICLE_CARRIER_ROOTS] + [FRONTEND]
    )


def _holds_the_article_carrier() -> list[str]:
    """Anywhere in the package at all: decision 425's half of the re-measurement."""
    return _carries_the_article_identifier([SPIELPLAN])


def _material_link_argument(text: str) -> str:
    """The stretch of `DataSources.svelte`'s header that argues the credit carries no link.

    Bounded at both ends, and the ends are doing different work. It OPENS at the marker because
    this header is a run of argued paragraphs each closing on its own decision number, so a rule
    reading the whole block would be discharged by the paragraph above -- which argues placement,
    under decision 318, and says nothing about a link. It CLOSES at the JSDoc block's `*/` because
    a citation outside the argument is a citation to something else: the next block down cites
    decision 298 for the logo slot, and that must not answer for this one. The window is the
    fallback for a body with no closing marker, which is what the self-tests hand it.
    """
    flat = _flattened(text)
    found = flat.find(_MATERIAL_LINK_ARGUMENT)
    if found < 0:
        return ""
    closes = flat.find("*/", found)
    return flat[found:closes if closes >= 0 else found + _MATERIAL_LINK_WINDOW]


def test_the_cc_by_sa_credit_records_the_material_link_it_does_not_carry():
    """A departure argued from a licence is a reading; a departure argued from a decision is a
    record somebody signed.

    Decision 320 refused both easy repairs. BUILDING the link was unavailable: that build imported
    no article identifier at all, and for TVmaze the corpus's `homepage` is the show's own
    marketing site rather than the TVmaze page, so an anchor under a CC BY-SA credit would point
    at the wrong work. EDITING `map-taste-data-sources-are-attributed`'s `what` to stop at the
    credit is the move `M4-open-points.md:212-213` forbids, and that `what` never promised the
    link anyway. So the clause is recorded as not reasonably practicable on this build, under a
    number, in `docs/RELEASE.md` section 4, where decision 296 routes a plan-versus-tree
    correction.

    THE FIRST HALF OF THAT PREMISE DIED AT M5.3 AND THIS GUARD ALMOST DIED WITH IT. §8 stage 2
    imports the article identifier and the derive computes the article URL, which is the day
    decision 320's Cost paragraph says the guard goes red, section 4.7 becomes the false record
    and the two come out with the anchor. What happened instead was a narrowing: the walk was
    re-pointed at `api/` alone, the four new readers fell outside it, and both published carriers
    of the old measurement were left standing - a landmine disarmed by the change that trod on it.
    Decision 425 re-measures the ground rather than re-scoping the instrument: the identifier is
    HELD and is served nowhere, both halves are asserted here, and the record and the component
    say so under that number. The escape hatch survives on the half that still stands - serve the
    field and this goes red of its own accord - and it is now the only half left, which is why the
    day it fires the deferral is over rather than narrowed again.
    [decisions 320, 425; row `map-taste-data-sources-are-attributed`;
     M5.3 review cycle 1, M53-COV-01]
    """
    # A narrowing that also blunted the detector would be a retirement with the name of a rule, so
    # both halves are re-proved here: the pattern still catches every shape a read comes in, and
    # every root it walks is a directory with files of the kinds it reads.
    for spelling in ('payload["homepage"]', ".get('homepage')", "meta.homepage",
                     'row["wikipedia_title"]', ".wikipedia_title", "->>'homepage'"):
        assert _ARTICLE_CARRIER.search(spelling), f"the detector missed {spelling}"
    for root in [SPIELPLAN / part for part in _ARTICLE_CARRIER_ROOTS] + [FRONTEND]:
        assert any(p.suffix in {".py", ".svelte", ".js"} for p in root.rglob("*") if p.is_file()), (
            f"the guard walks {root}, which holds nothing it can read"
        )
    assert _holds_the_article_carrier(), (
        "nothing in the package reads the article identifier any more, so decision 425's "
        "re-measurement -- that this build HOLDS it and only declines to serve it -- is now the "
        "false record. Re-run the reading and correct docs/RELEASE.md section 4.7 with it: the "
        "deferral's ground moved back to decision 320's, and that is a change somebody has to "
        "state rather than inherit."
    )
    reads = _reads_the_article_carrier()
    assert not reads, (
        f"{reads} now serve the corpus's article identifier, so the surviving half of decision "
        "320's premise -- that no response carries a field a link could be built from -- no "
        "longer holds and the CC BY-SA material link has become reasonably practicable. Delete "
        "this guard and docs/RELEASE.md section 4.7 in the same change as the anchor, and let the "
        "block carry the link. It is not to be re-scoped again: decision 425 spent the one "
        "narrowing this rule had in it."
    )
    argument = _material_link_argument(_src(FRONTEND / "lib" / "components" / "DataSources.svelte"))
    assert argument, (
        "DataSources.svelte no longer argues why the CC BY-SA credits carry no material link. "
        "That paragraph is the only thing standing between a reader and the conclusion that the "
        "link was forgotten; if the link shipped, this guard comes out with it."
    )
    for number in (_MATERIAL_LINK_DECISION, _MATERIAL_LINK_REMEASURED):
        assert f"decision {number}" in argument, (
            f"the paragraph arguing the credit carries no material link cites no decision "
            f"{number}:\n  "
            + argument[:200]
            + "\n\nEvery other argued departure in this header cites the ruling that took it. A "
            "departure argued from the licence text alone is this milestone's own defect class: a "
            "record that owes a debt with nowhere for a reader to find it. Both numbers are owed "
            "now and for different sentences: 320 is why there is no link, 425 is why the reason "
            "given for it is no longer the one 320 gave."
        )
    register = _src(REGISTER)
    for number in (_MATERIAL_LINK_DECISION, _MATERIAL_LINK_REMEASURED):
        assert re.search(rf"(?m)^### {number}\.", register), (
            f"DataSources.svelte cites decision {number} and the register heads no `### {number}.` "
            "entry. A citation to a number nobody took is the trap this milestone's plan opens "
            "with."
        )
    # And the record carries the re-measurement too, which is the half nothing read at all: 4.7
    # published a dated grep as its ground, M5.3 falsified the grep, and the section went on
    # saying it because the only instrument over that paragraph asked the COMPONENT for a
    # citation. A number in the section is not the measurement -- the two assertions above are --
    # but it is what sends a reader to the entry that re-took it. [M5.3 cycle 1, M53-COV-01]
    record = _src(RELEASE_RECORD)
    section = record[record.find("### 4.7"):]
    section = section[:section.find("\n### ") if "\n### " in section else len(section)]
    assert section, "docs/RELEASE.md no longer holds a section 4.7 for the material-link debt"
    assert re.search(rf"\b{_MATERIAL_LINK_REMEASURED}\b", section), (
        f"docs/RELEASE.md section 4.7 cites no decision {_MATERIAL_LINK_REMEASURED}. Its dated "
        "measurement is the ground the whole deferral stands on, and that ground moved: the "
        "section has to carry the entry that re-took it, or a reader is left with a grep that "
        "this tree no longer answers the way the paragraph says it does."
    )


@pytest.mark.parametrize(
    ("name", "text", "cited"),
    [
        ("the paragraph that shipped, arguing from the licence alone",
         "   * WIKIPEDIA AND TVMAZE ARE CREDITED, NOT DEEP-LINKED. CC BY-SA wants the licence\n"
         "   * identified and, where reasonable, the material linked; the material here is a\n"
         "   * per-title article, which belongs on the title card.", False),
        ("the same paragraph under the ruling that took it",
         "   * WIKIPEDIA AND TVMAZE ARE CREDITED, NOT DEEP-LINKED. This build imports no article\n"
         "   * identifier, so the link is not reasonably practicable here (decision 320).", True),
        ("a decision number in the block below it",
         "   * WIKIPEDIA AND TVMAZE ARE CREDITED, NOT DEEP-LINKED. The material link is a\n"
         "   * per-title article and this block is household-wide.\n   */\n\n  /**\n"
         "   * The logo slot (decision 320).\n   */", False),
        ("a decision number that belongs to the paragraph above it",
         "   * WHY /account. The account chip routes here (decision 318).\n   *\n"
         "   * WIKIPEDIA AND TVMAZE ARE CREDITED, NOT DEEP-LINKED. The material link is a\n"
         "   * per-title article and this block is household-wide.", False),
    ],
)
def test_the_material_link_guard_reads_the_citation_and_not_the_argument(name, text, cited):
    """Both directions. Refusing the first is the repair; ADMITTING the second is what makes the
    rule satisfiable at all.

    The last two are the ones that matter, and they are the two ends of the reader. A citation
    in the NEXT block answers for the next block -- the real header's is decision 298's logo
    slot -- and one in the PREVIOUS paragraph answers for placement, under decision 318. Either
    would discharge this rule while saying nothing about a link, which is how a citation guard
    turns into a rule about whether a number appears anywhere nearby.
    """
    assert (f"decision {_MATERIAL_LINK_DECISION}" in _material_link_argument(text)) is cited, name


# --- M4.16 dd26-bundle: the private bundle is never tracked and never shipped -------------------
#
# The release is MIT source plus a ~1 GB bundle assembled under personal and non-commercial terms
# -- GroupLens's no-redistribution clause, IMDb's personal-use licence, four blogs' whole
# articles, TMDB's non-commercial terms -- and until decision 292 nothing in this repository told
# a contributor where that line runs. The terms section in README is the half a person reads; this
# is the half that notices a `git add` nobody meant.

# The three shapes a bundle artefact arrives in: the review corpus (`reviews.sqlite`), the model
# arrays (`content_X.npz`, the backbone factors) and the manifest that names a cut of them.
_BUNDLE_ARTEFACT = re.compile(r"\.sqlite$|\.npz$|(?:^|/)BUNDLE\.json$", re.I)

# Where the bundle lives when it is on this box: `docker-compose.yml` bind-mounts `./data` for
# every `/data/*` path, so `data/` is the directory both ignore files have to exclude.
_BUNDLE_HOME = "data"


def _ignores_directory(text: str, directory: str) -> bool:
    """True when an ignore file excludes `directory` and nothing below it re-includes it.

    Spelled once per file and differently in each -- `/data/` in `.gitignore`, anchored at the
    repository root, and `data/` in `.dockerignore`, which has no anchoring syntax -- so the entry
    is normalised rather than matched literally. The two sides are deliberately not symmetric: an
    entry excludes the directory only when it IS the directory, since `data/artifacts/` leaves the
    bundle itself tracked, while a NEGATION anywhere underneath re-includes part of it. That is
    how the rule comes back without anybody touching the line that states it.
    """
    ignored = False
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        candidate = entry.lstrip("!").strip("/")
        if entry.startswith("!"):
            if candidate == directory or candidate.startswith(directory + "/"):
                ignored = False
        elif candidate == directory:
            ignored = True
    return ignored


def _bundle_artefacts(tracked) -> list[str]:
    return sorted(path for path in tracked if _BUNDLE_ARTEFACT.search(path))


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, timeout=300,
    )
    assert out.returncode == 0, (
        "`git ls-files` did not answer, so this guard is reading nothing:\n" + out.stderr
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def test_the_private_bundle_is_never_tracked_or_shipped():
    """Decision 292, held by something other than a contributor's care at `git add` time.

    The bundle is private household data. It is not published with a release, it is not a release
    asset, and the `/data/backups` movie-data archive inherits the same restriction because it
    carries the review bodies. What makes that easy to break is that the bundle lands in `data/`
    on every developer's box by default, and a single `git add -f` or a widened ignore rule puts
    a licensed corpus into a public MIT repository permanently -- git keeps it whether or not the
    next commit removes it.

    Both halves are asserted because they fail independently. The ignore files are the rule, and
    the tracked list is the outcome: `-f` beats the rule, and a file added before the rule existed
    was never subject to it. README's Data terms section is the third half, and it is here because
    a guard tells a contributor they cannot do something while only that section says why.
    [§10; LICENSE (MIT, code only); decision 292;
     row `platform-the-private-bundle-is-never-tracked-or-shipped`]
    """
    for ignore in (REPO / ".gitignore", REPO / ".dockerignore"):
        assert _ignores_directory(_src(ignore), _BUNDLE_HOME), (
            f"{ignore.name} no longer excludes `{_BUNDLE_HOME}/`, which is where the bundle, the "
            "artifacts tree and every nightly dump live on a developer's box. The next `git add "
            "-A` or `docker build` takes a licensed corpus with it."
        )
    tracked = _tracked_files()
    assert tracked, "`git ls-files` returned nothing, so this guard is reading an empty tree"
    artefacts = _bundle_artefacts(tracked)
    assert not artefacts, (
        "these tracked files are bundle artefacts, and the bundle is private household data "
        "assembled under personal and non-commercial terms (decision 292):\n  "
        + "\n  ".join(artefacts)
        + "\n\nGit keeps them after a later delete, so the repair is a history rewrite rather "
        "than a commit. See README's Data terms section for what travels under which licence."
    )
    assert "## Data terms" in _src(README), (
        "README no longer carries the Data terms section. The guard above is the cheap half of "
        "decision 292 and this is the half a person reads: which source allows what, and that "
        "the MIT LICENSE covers the code and nothing in the bundle."
    )


@pytest.mark.parametrize(
    ("name", "tracked"),
    [
        ("the review corpus", ["data/import/reviews.sqlite"]),
        ("a model array", ["data/artifacts/v1/content_X.npz"]),
        ("the manifest", ["BUNDLE.json"]),
        ("the manifest one directory down", ["data/import/BUNDLE.json"]),
        ("a capitalised extension", ["fixtures/Reviews.SQLite"]),
    ],
)
def test_the_tracked_bundle_guard_sees_each_artefact(name, tracked):
    """Five shapes, and the last two are what an earlier draft of this reader missed: a manifest
    at the repository root rather than inside a bundle directory, and an extension a case-folding
    filesystem hands back differently from how it was written."""
    assert _bundle_artefacts(tracked + ["README.md", "backend/spielplan/app.py"]), name


@pytest.mark.parametrize(
    ("name", "text", "ignored"),
    [
        ("anchored", "/data/\n", True),
        ("unanchored", "data/\n", True),
        ("a child", "data/artifacts/\n", False),
        ("absent", "node_modules/\n*.log\n", False),
        ("re-included underneath", "/data/\n!data/import/\n", False),
        ("commented out", "# /data/\n", False),
    ],
)
def test_the_ignore_guard_reads_both_spellings_and_the_way_back(name, text, ignored):
    """The rule is spelled differently in the two files, and the interesting failure is neither
    spelling: a `!data/...` negation added years later re-includes the directory without touching
    the line that excludes it, and nothing above would have read the pair."""
    assert _ignores_directory(text, _BUNDLE_HOME) is ignored, name


# --- M4.16 spec-11: an exit criterion is closed by a committed measurement, or it is open -------
#
# "The suite is green" was written down as a stronger statement than §12's exit criterion.
# It is not stronger, it is different: five of the fifteen rows describe shipped surfaces and have
# never been run at all, seven are measured by an `ops/` script and not one of those scripts had a
# committed output anywhere in the tree, and M4.5's criterion contained a check whose predicate
# was the literal `True` until M4.8 repaired it. `docs/RELEASE.md` is where that is recorded, row
# by row, with the owner's verdict column left to the owner.

# The statuses that mean "no output file, and that is the honest answer". A row with none of these
# and no file is a row claiming a measurement nobody can find.
_UNMEASURED = {"UNMEASURED", "NOT BUILT", "RUN, OUTPUT NOT COMMITTED"}

_RELEASE_HEADING = re.compile(r"^### (M[\d.]+)\s", re.M)
_RELEASE_FIELDS = re.compile(
    r"\*\*Status:\*\*\s*(.+?)\.\s*\*\*Output file:\*\*\s*(.+?)\.\s*\*\*Blocking:\*\*", re.S
)
_RELEASE_VERDICT = re.compile(r"\*\*Owner verdict:?\*\*:?\s*`([^`\n]*)`")
# Section 1's summary table: `| M4.5 | MEASURED | `docs/...` | yes | `________` |`. Five cells,
# and the three this file has rules about are the first, the second and the last.
_RELEASE_TABLE_ROW = re.compile(
    r"^\|\s*(M[\d.]+)\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|\s*$", re.M
)
# And section 3's own verdict, which is spelled the other way round -- `**Owner verdict:
# `________`**`, with the bold closing AFTER the blank -- so `_RELEASE_VERDICT` cannot match it.
# M2's row says "See §3 below", which makes that section the place a reader is sent to and the
# second place M2 can be signed. [decision 297; M4.16 cycle 5, M416-C4-REL-08]
_RELEASE_INLINE_VERDICT = re.compile(r"\*\*Owner verdict:?\s*`([^`\n]*)`\*\*")

# The row M4.5 is: §12 does not carry it, and the milestone measured the criterion section
# 12 gives M0's importer against the real corpus rather than the fixture. RELEASE.md carries it
# beside §12's own rows because it is the one row with a committed output to point at.
_EXTRA_RELEASE_ROWS = ("M4.5",)

# Decision 298's owed asset, and the sentence that has to survive with it. Named here rather than
# inside the test so the two halves -- the record and the absence -- cannot drift apart.
TMDB_LOGO = FRONTEND_STATIC / "tmdb-logo.svg"


def _section_12_milestones() -> list[str]:
    """The build order's own rows, read off the normative file rather than restated here."""
    spec = _src(_normative_file())
    table = spec.split("## 12. Build order", 1)
    assert len(table) == 2, "the normative file no longer has a `## 12. Build order` section"
    body = re.split(r"^## ", table[1], maxsplit=1, flags=re.M)[0]
    rows = re.findall(r"^\|\s*\*\*(M[\d.]+)\*\*\s*\|", body, re.M)
    assert rows, "§12's table has no milestone rows, so this guard is reading nothing"
    return rows


def _release_section_one() -> str:
    """Section 1, whole -- the summary table AND the blocks under it.

    Split out in review cycle 5 because the table was the half nothing read. `_release_rows` keys
    off `^### M`, so everything above the first heading was discarded, and section 1 opens with a
    table restating every row's Status, Output file and verdict, a row for each block under it.
    That is the FIRST statement a reader meets of each of them, and it could disagree with the
    block below it in every cell without a guard anywhere noticing. [M4.16 cycle 5, M416-C4-REL-08]
    """
    body = _src(RELEASE_RECORD).split("## 1. ", 1)
    assert len(body) == 2, "docs/RELEASE.md has no `## 1.` section"
    return re.split(r"^## \d", body[1], maxsplit=1, flags=re.M)[0]


def _release_rows() -> dict[str, str]:
    """Section 1's blocks, one per milestone, keyed by the milestone they are about."""
    section = _release_section_one()
    heads = list(_RELEASE_HEADING.finditer(section))
    return {
        head.group(1): section[head.start(): heads[i + 1].start() if i + 1 < len(heads) else len(section)]
        for i, head in enumerate(heads)
    }


def _release_summary(section: str) -> dict[str, tuple[str, str, str]]:
    """Section 1's summary table, keyed by milestone: (Status, Output file, Owner verdict)."""
    return {
        row.group(1): tuple(row.group(i).strip().strip("`") for i in (2, 3, 5))
        for row in _RELEASE_TABLE_ROW.finditer(section)
    }


def _release_summary_problems(summary: dict[str, tuple[str, str, str]],
                              rows: dict[str, str]) -> list[str]:
    """Every way the table at the top of section 1 can say something the blocks below do not.

    A duplicated record read by no rule is the defect this milestone exists to close, and this one
    is a duplicate of the three fields the rule is ABOUT: a cell flipped to `MEASURED` against an
    output file that is not in the tree, or a verdict cell signed while the block below it still
    says `UNMEASURED`, satisfied every guard here -- measured at M4.16 cycle 5, one cell at a time,
    over all sixteen rows it then held. Held against the blocks rather than re-deriving the answer,
    because the blocks are where the criterion is quoted and the table is the summary OF them; and
    the same unfilled-verdict rule is applied to the cell, because a reader who stops at the table
    has read a verdict either way. [M4.16 cycle 5, M416-C4-REL-08]
    """
    problems = [
        f"the summary table names {name}, which has no block in section 1"
        for name in sorted(set(summary) - set(rows))
    ]
    problems += [
        f"{name} has a block in section 1 and no row in the summary table above it"
        for name in sorted(set(rows) - set(summary))
    ]
    for name, (status, output, verdict) in sorted(summary.items()):
        fields = _RELEASE_FIELDS.search(rows.get(name, ""))
        if fields is None:
            continue
        for label, cell, stated in (
            ("Status", status, fields.group(1).strip()),
            ("Output file", output, fields.group(2).strip().strip("`")),
        ):
            if cell != stated:
                problems.append(
                    f"the summary table gives {name} the {label} {cell!r} and its own block says "
                    f"{stated!r}; the table is the first of the two a reader meets"
                )
        if status in _UNMEASURED and not re.fullmatch(r"_+", verdict):
            problems.append(
                f"the summary table records {name} as {status!r} and signs its verdict {verdict!r}"
            )
    return problems


def _release_row_problems(rows: dict[str, str], owed: list[str]) -> list[str]:
    """Every way the record can claim more than the tree holds, as one list.

    The verdict was read for its PRESENCE and never for its value until review cycle 5, so half of
    the rule `docs/RELEASE.md` publishes in its own voice -- "every §12 row either names an output
    file that exists in the tree, or is recorded as `UNMEASURED` / `NOT BUILT` / `RUN, OUTPUT NOT
    COMMITTED` with an unfilled verdict" -- was enforced by nothing. Measured at M4.16 cycle 5:
    signing any of the sixteen section-1 rows one at a time left this function returning `[]` every
    time, and section 1 has gained rows since. Only M2's value was held, by a bespoke guard below,
    which is decision 297's scope rather than the row's:
    the `what` states the universal, and a row is repaired by widening its guard rather than by
    narrowing its claim. [docs/RELEASE.md "How to read this file"; M4.16 cycle 5, M416-C4-REL-08]
    """
    problems = [f"{name} has no row in section 1" for name in owed if name not in rows]
    for name, block in sorted(rows.items()):
        fields = _RELEASE_FIELDS.search(block)
        if fields is None:
            problems.append(f"{name} states no Status / Output file / Blocking line")
            continue
        status, output = fields.group(1).strip(), fields.group(2).strip().strip("`")
        verdict = _RELEASE_VERDICT.search(block)
        if verdict is None:
            problems.append(f"{name} carries no Owner verdict field")
        elif status in _UNMEASURED and not re.fullmatch(r"_+", verdict.group(1)):
            problems.append(
                f"{name} is recorded as {status!r} and carries the verdict {verdict.group(1)!r}; "
                "nothing in this repository can produce one, so it was filled in from memory"
            )
        if output == "none":
            if status not in _UNMEASURED:
                problems.append(
                    f"{name} is {status!r} and names no output file; a measured row points at one"
                )
        elif not (REPO / output).exists():
            problems.append(f"{name} names the output file {output!r} and it is not in the tree")
    return problems


def test_exit_criteria_are_closed_by_a_committed_measurement():
    """Decision 184 one level up: not "is this figure re-derived" but "was this run at all".

    §12's criteria are the only statement this project has about whether it is shippable,
    and `current_milestone` -- the lever the build actually had -- answers a different question:
    whether the map is covered. A green suite over a covered map says nothing about whether fifty
    verdicts produce a visibly personal ranking, which is the sentence §12 calls the gate.

    What this holds is the shape that makes the difference visible. A row either points at an
    output file that is IN THE TREE, or it says in so many words that it was not measured. Both
    are acceptable answers and the second one is the honest one; what is refused is the third,
    a row that reads as measured and names nothing a reader can open.
    [§12; docs/TESTING.md; decision 296;
     row `platform-exit-criteria-are-closed-by-a-committed-measurement`]
    """
    rows = _release_rows()
    owed = _section_12_milestones() + list(_EXTRA_RELEASE_ROWS)
    problems = _release_row_problems(rows, owed)
    problems += _release_summary_problems(_release_summary(_release_section_one()), rows)
    assert not problems, (
        "docs/RELEASE.md is the record of what §12 actually measured, and:\n  "
        + "\n  ".join(problems)
        + "\n\nA criterion nobody ran is recorded as unmeasured, with the verdict left to the "
        "owner. That is an honest row; a row implying a run whose output nobody committed is not."
    )


def test_the_unmeasured_criterion_and_the_owed_asset_stay_unsigned():
    """Decisions 297 and 298, in the idiom of `test_the_owed_device_checks_are_recorded_and_still_unsigned`.

    M2 is the gate -- "50-100 verdicts each produce visibly personal rankings", §12's own
    "first real-user validation of the whole corpus project" -- and it has never been measured by
    anybody. The record says so and leaves the verdict column empty, because the alternative shape
    is the one this milestone exists to end: a column filled in from memory, which tells the next
    reader to stop looking. Nothing in this repository can fill it; only a seeded stack, two
    members with fifty verdicts each and a Spearman the owner sets a threshold for can.

    The TMDB logo is the same polarity over an artefact. No agent may fabricate a trademarked
    file, so the Data sources block renders a named slot and `docs/RELEASE.md` section 7.1 carries
    the debt. Both halves are asserted together: a record of an absence stops being true the
    moment the absence ends, and the repair then is to delete this test with its row entry in the
    same change -- which is the point. Filling either one costs a deletion that quotes the
    decision at you.
    [decisions 297, 298; row `platform-exit-criteria-are-closed-by-a-committed-measurement`]
    """
    rows = _release_rows()
    assert "M2" in rows, "docs/RELEASE.md carries no M2 row, and M2 is the row §12 calls the gate"
    fields = _RELEASE_FIELDS.search(rows["M2"])
    assert fields and fields.group(1).strip() == "UNMEASURED", (
        "docs/RELEASE.md no longer records M2's criterion as UNMEASURED. If it was measured, the "
        "run's output belongs in the tree beside `M4.5-exit.txt` and this guard goes with it; if "
        "the marker was simply deleted, that is decision 297's defect wearing a status."
    )
    verdict = _RELEASE_VERDICT.search(rows["M2"])
    assert verdict and re.fullmatch(r"_+", verdict.group(1)), (
        "M2's owner verdict carries a value, and no run in this repository can produce one: "
        "nothing here seeds two members with fifty verdicts, reads both orderings and compares "
        f"them against `title_prior.b`. Found: {verdict.group(1) if verdict else None!r}. If the "
        "owner has signed it, delete this guard and its coverage row entry in the same change."
    )
    # And the same blank signed BESIDE itself, which the rule above cannot see: it reads what is
    # between the backticks and nothing else, so `**Owner verdict:** `________` -- PASS, signed by
    # the owner on 2026-09-17.` satisfied it, measured, and reads as a verdict to every human
    # being. Held over the FIELDS paragraph rather than the whole row, because M2's row
    # legitimately discusses runs that did happen -- and only over M2, because M4.5's row carries
    # a date and a PASS for the honest reason that somebody ran it. The reader is shared with
    # `test_the_owed_device_checks_are_recorded_and_still_unsigned`, which is the same mechanism
    # one document over. [decisions 281 and 297; M4.16 cycle 4, M416-C4-COV-04]
    from tests.test_spec_coverage import _paragraph_at, _signature_prose

    fields = rows["M2"].index("**Status:**")
    beside = _signature_prose(_paragraph_at(rows["M2"], fields), _RELEASE_VERDICT)
    assert not beside, (
        "M2's verdict is left blank and signed in the prose beside it, which discharges the "
        f"criterion for every reader while the blank stays blank: {', '.join(dict.fromkeys(beside))}. "
        "Decision 297 leaves the column empty because nothing here can fill it; a verdict written "
        "next to the column is that column filled."
    )

    # And M2's SECOND verdict site, which is the one M2's own row sends the reader to ("See §3
    # below"). Section 3 states the criterion and what a measurement would take, and ends in a
    # verdict of its own -- spelled `**Owner verdict: `________`**`, the bold closing after the
    # blank rather than before it, which `_RELEASE_VERDICT` cannot match. So the one row decision
    # 297 is written about could be signed off in the section it points at, with the row above
    # left honest and every guard in this file green. Held as a count as well as a value, because
    # a second blank added beside it is a second place to sign. [M4.16 cycle 5, M416-C4-REL-08]
    third = _src(RELEASE_RECORD).split("## 3. ", 1)
    assert len(third) == 2, "docs/RELEASE.md has no `## 3.` section, and M2's row points at it"
    inline = _RELEASE_INLINE_VERDICT.findall(re.split(r"^## \d", third[1], maxsplit=1, flags=re.M)[0])
    assert len(inline) == 1, (
        f"docs/RELEASE.md section 3 holds {len(inline)} owner-verdict blanks: {inline}. M2's row "
        "points a reader at this section, so it carries exactly one, and it is the same one."
    )
    assert re.fullmatch(r"_+", inline[0]), (
        f"docs/RELEASE.md section 3 signs M2's criterion {inline[0]!r}. It is the section M2's own "
        "row sends a reader to, and the section's own sentence says the verdict is \"fillable only "
        "by a run\" -- a run this repository cannot make. Decision 297's repair on the day the "
        "owner honestly signs is to delete this guard with its coverage row entry, in one change."
    )

    owed = _src(RELEASE_RECORD).split("### 7.1 ", 1)
    assert len(owed) == 2, "docs/RELEASE.md section 7.1 -- the TMDB logo debt -- is gone"
    claim = re.split(r"^### ", owed[1], maxsplit=1, flags=re.M)[0]
    assert TMDB_LOGO.relative_to(REPO).as_posix() in claim and "Owed" in claim, (
        "docs/RELEASE.md section 7.1 no longer names `frontend/static/tmdb-logo.svg` as owed. The "
        "notice ships without it and the e2e row asserts neither its presence nor its absence, so "
        "this record is the only thing that remembers the debt exists."
    )
    assert not TMDB_LOGO.exists(), (
        "`frontend/static/tmdb-logo.svg` is in the tree, so decision 298's debt is paid and the "
        "record that it is owed is now false. Delete section 7.1, this assertion and the "
        "COMMENT_PATH_EXCEPTIONS entry above in one change, and let the Data sources slot fill."
    )


@pytest.mark.parametrize(
    ("name", "block", "owed", "caught"),
    [
        ("a row that vanished", {}, ["M2"], True),
        (
            "measured, naming nothing",
            {"M2": "**Status:** MEASURED. **Output file:** none. **Blocking:** yes.\n"
                   "**Owner verdict:** `________`\n"},
            [], True,
        ),
        (
            "naming a file that is not there",
            {"M2": "**Status:** MEASURED. **Output file:** `docs/nowhere.txt`. **Blocking:** yes.\n"
                   "**Owner verdict:** `shipped`\n"},
            [], True,
        ),
        (
            "unmeasured, with no verdict field at all",
            {"M2": "**Status:** UNMEASURED. **Output file:** none. **Blocking:** yes.\n"},
            [], True,
        ),
        (
            "unmeasured, with a verdict nobody in this repository could have produced",
            {"M2": "**Status:** UNMEASURED. **Output file:** none. **Blocking:** yes.\n"
                   "**Owner verdict:** `PASS`\n"},
            [], True,
        ),
        (
            "not built, and signed as shipped",
            {"M5": "**Status:** NOT BUILT. **Output file:** none. **Blocking:** no.\n"
                   "**Owner verdict:** `shipped`\n"},
            [], True,
        ),
        (
            "unmeasured and honest",
            {"M2": "**Status:** UNMEASURED. **Output file:** none. **Blocking:** yes.\n"
                   "**Owner verdict:** `________`\n"},
            [], False,
        ),
        (
            "measured, pointing at a file the tree holds",
            {"M4.5": "**Status:** MEASURED. **Output file:** `README.md`. **Blocking:** yes.\n"
                     "**Owner verdict:** `________`\n"},
            [], False,
        ),
        (
            "measured, pointing at a file the tree holds, and honestly signed",
            {"M4.5": "**Status:** MEASURED. **Output file:** `README.md`. **Blocking:** yes.\n"
                     "**Owner verdict:** `PASS, owner, 2026-09-17`\n"},
            [], False,
        ),
    ],
)
def test_the_release_record_guard_sees_a_row_claiming_more_than_the_tree_holds(name, block, owed, caught):
    """Eight rows, five of them refused. The fourth is the shape that matters most: a row recorded
    as unmeasured with the verdict field deleted rather than left empty reads as complete to
    every sweep that counts fields, and decision 297's whole mechanism is that the column is
    there and blank.

    The fifth and sixth are that mechanism's other spelling, and the one the rule shipped unable
    to refuse: the column left in place and FILLED, over a status that says the run never
    happened. The last case is why the rule fires on `_UNMEASURED` and not on every verdict --
    M4.5 was measured, its output is in the tree, and the owner may sign it whenever they read it.
    A rule that refused that would be narrowed by the first person it stopped.
    [M4.16 cycle 5, M416-C4-REL-08]
    """
    assert bool(_release_row_problems(block, owed)) is caught, name


@pytest.mark.parametrize(
    ("name", "table", "rows", "caught"),
    [
        ("the table and the block agreeing",
         "| M2 | UNMEASURED | none | owner's call | `________` |\n",
         {"M2": "**Status:** UNMEASURED. **Output file:** none. **Blocking:** yes.\n"}, False),
        ("a cell flipped to MEASURED against a file that is not there",
         "| M2 | MEASURED | `docs/nowhere.txt` | owner's call | `________` |\n",
         {"M2": "**Status:** UNMEASURED. **Output file:** none. **Blocking:** yes.\n"}, True),
        ("the table's verdict signed while the block below stays unmeasured",
         "| M2 | UNMEASURED | none | owner's call | `PASS, owner, 2026-09-17` |\n",
         {"M2": "**Status:** UNMEASURED. **Output file:** none. **Blocking:** yes.\n"}, True),
        ("a row in the table and no block under it",
         "| M8 | NOT BUILT | none | no | `________` |\n", {}, True),
        ("a block with no row in the table",
         "",
         {"M2": "**Status:** UNMEASURED. **Output file:** none. **Blocking:** yes.\n"}, True),
    ],
)
def test_the_release_summary_table_says_what_the_blocks_under_it_say(name, table, rows, caught):
    """The table was read by nothing, and it restates every field the rules below are about.

    Five cases, four refused. The second and third are the reachable edits -- one cell, one line,
    no other change -- and both left every guard over this document green while the table told a
    reader the opposite of the block beneath it. The first is the direction that keeps the rule
    from being satisfied by deleting the table: agreement passes, and a summary that agrees with
    what it summarises is the whole point of having one. [M4.16 cycle 5, M416-C4-REL-08]
    """
    assert bool(_release_summary_problems(_release_summary(table), rows)) is caught, name


# --- M4.16 review cycle 1: the record covers the instrument the milestone exists to add ---------
#
# Everything above holds §12's own rows plus M4.5. None of it reaches the release workflow
# itself, so the file whose declared contract is "the fact of a run, or the fact of its absence"
# said neither about the one instrument this milestone was written for -- while `docs/TESTING.md`
# twice pointed a reader at this file for that workflow's answer, and once asserted in the present
# tense that two checklist items "are now a machine's answer". They are not: the workflow has never
# been dispatched, and the runner it names has never been registered. [M4.16 cycle 1, M416-REL-01]

RELEASE_GATE_SECTION = "### 2.1 "
# THREE and not two since review cycle 5, which is decision 312's fourth place and the one a
# guard could reach. The criterion the table resolves -- "counts only rows whose named tests
# actually executed AT OR ABOVE the row's declared kind" -- is two claims with two owners, and
# the kind guard's row in that table was the half nothing held: deleting it leaves a reader of
# section 2.1 told the criterion resolves to one instrument reading in one direction, with both
# coverage rows still asserting the cross-naming in the present tense.
# [decision 312; M4.16 cycle 5, M416-C5-GATE-01]
_GATE_INSTRUMENTS = (".github/workflows/release.yml", "ops/coverage_gate.py",
                     "backend/tests/test_spec_coverage.py")

# The sentences section 2.1 states its polarity IN, which is where a reader actually reads it.
# The guard read four things -- the `Status:` word, the `Output file:` value, the verdict's
# underscores and the two instrument paths -- and the section's own prose was read by nothing:
# the heading's verdict, the bolded never-run sentence and the job's exit code all survived
# being rewritten into a record of a dispatch that never happened, measured, with `UNMEASURED`
# and the underscored verdict sitting above them and the whole suite green. A reader takes away
# the paragraph, not the field, so a record honest in its fields and false in its sentences is
# the shape decision 297 exists to refuse.
#
# FIXED CLAUSES rather than a rule over prose, in the idiom section 7.2's guard already uses one
# section down: each is the claim exit criterion 4 is about, and the second carries BOTH of its
# halves -- no dispatch, and no leg deliberately broken and watched to fail. The middle column of
# the per-leg table is deliberately not here: decision 313 moved leg 2's cell to a recorded
# partial this cycle and that column moves again as work lands by hand, while these three and
# the "By this job" column below can only move when a workflow returns an exit code. ONE cell of
# that middle column is held, at the bottom of this file: leg 3's, because its first command can
# only skip in this lane, which is a fact about the tree rather than about what somebody has got
# round to running (`test_the_release_record_does_not_record_the_real_bundle_leg_as_run`).
# [decision 297; M4.16 cycle 4: REL-C4-02, REL-C4-10]
_GATE_POLARITY = (
    ("never dispatched", "the heading's own verdict on the workflow"),
    ("has never run, and no leg of it has ever been deliberately broken and watched to fail",
     "exit criterion 4's two halves, which is the sentence this section exists to carry"),
    ("has never returned an exit code",
     "the job's own answer, which is what five legs in order add up to"),
)

# "**Exactly one standing waiver remains**", and the bold is what makes it the record's claim
# rather than a passing mention. `_COUNT_WORDS` is the register guard's list, reused here for the
# reason it exists there: the guard rules on the count, not on how a paragraph chose to spell it.
_STANDING_WAIVERS = re.compile(r"\*\*(?P<count>[A-Za-z ]+?) standing waivers? remains?\b")


def _gate_record(text: str) -> str:
    """Section 2.1 of `docs/RELEASE.md`, the release gate's own row."""
    parts = text.split(RELEASE_GATE_SECTION, 1)
    return re.split(r"^#{2,3} ", parts[1], maxsplit=1, flags=re.M)[0] if len(parts) == 2 else ""


def _gate_record_problems(text: str) -> list[str]:
    """Every way that row can claim, or imply, a run nobody made."""
    block = _gate_record(text)
    if not block:
        return ["docs/RELEASE.md carries no `### 2.1` section for the release workflow itself"]
    problems = []
    fields = _RELEASE_FIELDS.search(block)
    if fields is None:
        problems.append("section 2.1 states no Status / Output file / Blocking line")
    else:
        status, output = fields.group(1).strip(), fields.group(2).strip().strip("`")
        if status not in _UNMEASURED:
            problems.append(
                f"section 2.1 records the release workflow as {status!r}. A dispatch happened, or "
                "the marker was deleted; if it happened, its artifacts belong in the tree and this "
                "guard goes with them in the same change"
            )
        if output != "none":
            problems.append(f"section 2.1 names the output file {output!r} for a job nobody ran")
    verdict = _RELEASE_VERDICT.search(block)
    if not (verdict and re.fullmatch(r"_+", verdict.group(1))):
        problems.append(
            "section 2.1's owner verdict is filled or missing, and no run in this repository can "
            "fill it: nothing here registers a runner or dispatches a workflow"
        )
    for instrument in _GATE_INSTRUMENTS:
        if instrument not in block:
            problems.append(f"section 2.1 does not name `{instrument}`")
    # Whitespace-collapsed, because every one of these sentences wraps in the document and a
    # rule pinned to a physical line would stop at the wrap. [M4.16 cycle 4, REL-C4-02]
    flat = " ".join(block.split())
    for sentence, carries in _GATE_POLARITY:
        if sentence.lower() not in flat.lower():
            problems.append(
                f'section 2.1 no longer says "{sentence}", which is where it records '
                f"{carries}. If the workflow HAS run, its artifacts belong in the tree and this "
                "guard goes with them in the same change"
            )
    # The per-leg table's LAST column is "By this job", and every cell in it says `never`. That
    # column is the one exit criterion 4 is actually about: the middle column records what has been
    # run by hand from this lane and moves as work lands -- decision 313 moved leg 2's to a partial
    # this cycle -- while the right-hand column can only move when a workflow returns an exit code,
    # which nothing in this repository can make happen. Held as a rule over the whole column rather
    # than over one leg, because the sentence a reader takes away is the column and not a row.
    # [decision 297's idiom; M4.16 cycle 4, M416-C4-GATE-02]
    # The cell rather than the row, and ASCII-escaped: this message reaches whatever console the
    # suite is read on, and the table's own em dashes crash a cp1252 one (CLAUDE.md).
    ran = [
        f"leg {row.group('leg')} says "
        + row.group("by").strip().encode("ascii", "backslashreplace").decode()
        for row in re.finditer(
            r"^\| *(?P<leg>\d+) [^|\n]*\|[^|\n]*\|(?P<by>[^|\n]*)\|", block, re.M
        )
        if row.group("by").strip().strip("*`") != "never"
    ]
    if ran:
        problems.append(
            "section 2.1's per-leg table claims a leg was run BY THIS JOB: " + "; ".join(ran)
            + ". The job has never returned an exit code; if it has, its artifacts belong in the "
            "tree and this guard goes with them in the same change"
        )
    return problems


def test_the_release_gate_records_its_own_unrun_status():
    """The half of §12's criterion this lane could satisfy, which is the half it owed.

    A dispatch needs a push, a registered `spielplan-corpus` runner and Docker, none of which
    exists here -- and the criterion says so: where a leg has never been executed from this lane,
    that absence is RECORDED rather than implied to have happened. It was not, and the omission was
    louder than an ordinary gap because `docs/TESTING.md` sends the reader here for exactly this
    answer. A gate that has never produced an exit code and is believed to is worse than no gate.
    [decision 297's idiom; row `platform-exit-criteria-are-closed-by-a-committed-measurement`]
    """
    problems = _gate_record_problems(_src(RELEASE_RECORD))
    assert not problems, (
        "docs/RELEASE.md is where the release workflow's answer is written down, and:\n  "
        + "\n  ".join(problems)
    )


# The honest record every case below spoils in exactly one place, so each case proves the rule it
# is named for rather than another rule's absence -- `_synthetic`'s argument, one module over. It
# carries the three polarity sentences and a per-leg row because the guard reads all of them, and
# the never-run sentence is written ACROSS a line break, which is how the document writes it and
# what the flattening in the guard is for. [M4.16 cycle 4, REL-C4-02]
_HONEST_GATE_RECORD = (
    "### 2.1 The release workflow itself - **never dispatched**\n\n"
    "**Status:** UNMEASURED. **Output file:** none. **Blocking:** no.\n"
    "**Owner verdict:** `________`\n\n"
    "**`.github/workflows/release.yml` has never run, and no leg of it has ever been\n"
    "deliberately broken and watched to fail.** **The JOB has never returned an exit code.**\n\n"
    "| Leg | Substance run from this lane | By this job |\n"
    "|---|---|---|\n"
    "| 1 - the full suite | yes, every cycle | never |\n\n"
    "ops/coverage_gate.py\n"
    "backend/tests/test_spec_coverage.py's kind guard\n"
)


@pytest.mark.parametrize(
    ("name", "text", "caught"),
    [
        ("the section deleted", "## 2. scripts\n\nnothing about the workflow\n", True),
        ("recorded as run, with nothing to open",
         _HONEST_GATE_RECORD.replace("UNMEASURED", "MEASURED"), True),
        ("signed by nobody who could have",
         _HONEST_GATE_RECORD.replace("`________`", "`green`"), True),
        # Review cycle 4. These four are where section 2.1 actually STATES the polarity, and all
        # four were rewritten into a record of a dispatch nobody made -- with a date, a broken leg
        # and a table saying every leg ran -- while the fields above them stayed honest and every
        # guard in this file stayed green. [M4.16 cycle 4, REL-C4-02]
        ("the never-run sentence rewritten into a dispatch",
         _HONEST_GATE_RECORD.replace(
             "has never run, and no leg of it has ever been\n"
             "deliberately broken and watched to fail.**",
             "was dispatched on 2026-09-18 and leg 3 was deliberately broken and watched to "
             "fail.**",
         ), True),
        ("the heading's own verdict deleted",
         _HONEST_GATE_RECORD.replace(" - **never dispatched**", ""), True),
        ("the job given an exit code it has never returned",
         _HONEST_GATE_RECORD.replace(
             "The JOB has never returned an exit code.", "The JOB returned 0 on run 41."
         ), True),
        ("a leg claimed for the job in the column that can only say never",
         _HONEST_GATE_RECORD.replace("| yes, every cycle | never |", "| yes | yes, run 41 |"),
         True),
        # Review cycle 5, and decision 312's fourth place: the instrument table's third row.
        # [decision 312; M4.16 cycle 5, M416-C5-GATE-01]
        ("the kind guard dropped from the instrument table",
         _HONEST_GATE_RECORD.replace(
             "backend/tests/test_spec_coverage.py's kind guard\n", ""), True),
        ("honest, and naming all three instruments", _HONEST_GATE_RECORD, False),
    ],
)
def test_the_gate_record_guard_sees_a_workflow_recorded_as_having_run(name, text, caught):
    """The shapes that cost nothing to write and everything to believe.

    Four of the eight are review cycle 4's, and they are the half that is not a field: a reader
    takes away the heading, the bolded sentence and the table, and each of those could be turned
    into a record of a run nobody made without touching the `Status:` word above them.
    [M4.16 cycle 4, REL-C4-02]
    """
    assert text != _HONEST_GATE_RECORD or not caught, (
        f"the {name!r} mutation no longer changes the record, so it would pass over an edit it "
        "never made"
    )
    assert bool(_gate_record_problems(text)) is caught, name


def test_the_release_record_states_the_waivers_the_coverage_map_actually_holds():
    """§12's criterion for this milestone is that EXACTLY ONE waiver stands. This file said two.

    It was true when the sentence was written and false by the time the milestone closed: the
    second waiver was retired inside the same change set, and the paragraph naming it kept
    instructing a reader to perform the retirement -- an instruction to redo work already done,
    which invites either a duplicate edit or a revert of the test that replaced the waiver.
    `docs/TESTING.md` faced the identical hazard and solved it with dated `[SUPERSEDED ...]`
    markers; this file simply went stale, and nothing read it.

    Held both ways. An id this record calls waived must carry `waived` in the map, and the count
    word it publishes must be the map's. [M4.16 cycle 1, M416-C1-REL-02]
    """
    text = _src(RELEASE_RECORD)
    waived = {
        row["id"]
        for row in tomllib.loads(COVERAGE.read_text(encoding="utf-8"))["requirement"]
        if "waived" in row
    }
    claim = _STANDING_WAIVERS.search(text)
    assert claim, (
        "docs/RELEASE.md no longer publishes how many standing waivers the map holds, in the form "
        "`**<count> standing waiver(s) remain(s)**`. §12's criterion for this milestone IS that "
        "count, so the record either states it or this guard is reading nothing."
    )
    published = _COUNT_WORDS.index(claim.group("count").lower().split()[-1])
    assert published == len(waived), (
        f"docs/RELEASE.md publishes {published} standing waiver(s) and the map holds "
        f"{len(waived)}: {sorted(waived)}"
    )
    # The same paragraph, because that is where the ids belong and where they went stale: the
    # sentence naming two ids survived the retirement of one of them and kept instructing a
    # reader to perform an edit that had already landed.
    paragraph = text[claim.start():].split("\n\n", 1)[0]
    named = set(re.findall(r"`([a-z][a-z0-9]*(?:-[a-z0-9]+)+)`", paragraph))
    assert named == waived, (
        f"docs/RELEASE.md's standing-waiver paragraph names {sorted(named)} and the map waives "
        f"{sorted(waived)}. A record that calls a retired waiver standing is an instruction to "
        "redo work already done, which invites a duplicate edit or a revert of the test that "
        "replaced it."
    )


def test_the_release_record_carries_the_owed_device_checks_by_reference_only():
    """7.2 says of itself "carried here by reference and by reference only ... Nothing is copied
    here", and then copied a list: three bullets over a ledger carrying six, plus a sentence
    saying a fourth check had been "retired rather than signed".

    Nothing was retired. Review cycle 2's CDP injection put the ARITHMETIC of the status-bar rule
    into CI and decision 281's own amendment says in terms that not one of the owed facts is
    discharged by it. So the record understated the debt AND reported a discharge that did not
    happen, in the section written to keep the debt visible -- which is a record made true in the
    wrong direction, the failure mode this milestone exists to remove.

    The repair is the one the section already claims: no list, no count, one pointer. This holds
    that, because a list is what went stale. [decisions 281, 302; M4.16 cycle 1, M416-C1-REL-05]
    """
    owed = _src(RELEASE_RECORD).split("### 7.2 ", 1)
    assert len(owed) == 2, "docs/RELEASE.md section 7.2 -- the owed device checks -- is gone"
    block = re.split(r"^### ", owed[1], maxsplit=1, flags=re.M)[0]
    assert "docs/TESTING.md" in block, (
        "section 7.2 no longer points at the file that holds the checks and the signature line, "
        "so the reference it says it is has nowhere to land"
    )
    bullets = [line for line in block.splitlines() if line.startswith("- ")]
    assert not bullets, (
        "section 7.2 lists the owed checks rather than pointing at them, and a copied list is what "
        f"went stale last time: {bullets}. docs/TESTING.md is the one place they are counted."
    )
    stale = [
        phrase
        for phrase in ("retired rather than signed", "Three facts", "three unsigned",
                       "the three unsigned")
        if phrase in block
    ]
    assert not stale, (
        f"section 7.2 states {stale} over a ledger that counts the checks itself and retires none "
        "of them. Decision 281's amendment: the CDP work discharges no owed fact and the signature "
        "line does not move."
    )

    # And the OTHER half of the section's own sentence, which nothing read: "no second signature
    # line is written anywhere, because a second one would let the debt be discharged in one file
    # while the guard reads the other". `test_the_owed_device_checks_are_recorded_and_still_
    # unsigned` counts `verified on ...` lines in docs/TESTING.md and only there, and the three
    # rules over this file read section 1, section 2.1 and section 7.1 -- so a filled signature
    # inserted under this heading left every gate green while README sends a reader here for
    # release verdicts. Read over the WHOLE file, because the split-record failure the sentence
    # names is about a line appearing anywhere in it, not under one heading.
    #
    # FILLED fields, not the idiom: decision 302's own wording quotes `verified on ...` as a
    # pointer, and a rule that reddened on the pointer would be a rule nobody could satisfy
    # except by never naming the thing. The day the debt is honestly signed this goes red and
    # comes out with section 7.2 in one change, which is the same polarity as its siblings.
    # [decisions 281, 302; M4.16 cycle 4, REL-C4-05]
    from tests.test_spec_coverage import _OWED_SIGNATURE

    signatures = [
        found.group(0)
        for found in _OWED_SIGNATURE.finditer(_src(RELEASE_RECORD))
        if not re.fullmatch(r"[_\s.]*", found.group(1))
    ]
    assert not signatures, (
        f"docs/RELEASE.md carries a filled device-check signature line: {signatures}. Section 7.2 "
        "says of itself that no second signature line is written anywhere, because a second one "
        "lets the debt be discharged in one file while the guard reads the other -- and the guard "
        "reads docs/TESTING.md."
    )


# --- M4.16 review cycle 2: the point release names the wave it actually was ---------------------
#
# Decision 288 makes the dated point release the ONE join between the register and the normative
# text -- the date says which wave, the numbers say what the wave was answering to -- and
# `_FOLDED_DECISIONS` above only asks that the line name at least one decision. So a line naming
# four of five passes: review cycle 1 landed decision 306's amendment in section 5.2, ending
# "- decision 306", while the Status block went on saying the cycle "folded four more" and naming
# 304 as its last. An auditor reading section 5.2 back to the wave that applied it is told the
# decision was not in it, and an auditor reading the register forward gets no confirmation from the
# file that is supposed to carry the amendment.
#
# Scoped to the wave, not to the register: only decisions taken on the point release's own DATE are
# owed a mention, because sections 1-14 cite decisions 174 and 183 from earlier waves and a rule
# that demanded those would be asking this line to restate the whole register. And only decisions
# the body actually CITES are owed one, because 296 and 297 mandate records rather than spec text
# and belong to no wave clause at all. [decision 288; M4.16 cycle 2, SPEC-C2-06]
_WAVE_DATE = re.compile(r"\((\d{4}-\d{2}-\d{2})\)")
_REGISTER_BLOCK_HEAD = re.compile(r"^## Decisions taken \((?P<head>[^)]*)\)\s*$", re.M)
_BLOCK_DECISION = re.compile(r"^### (\d+)\. ", re.M)
# `decisions 292 and 293`, `decisions 164, 165, 288`, `decision 307` -- a citation of one number or
# of a list, which is how sections 1-14 write them.
_BODY_DECISIONS = re.compile(r"\bdecisions?\s+(\d+(?:\s*(?:,|and|\+)\s*\d+)*)", re.I)
# What the line itself names: singles, and the ranges a wave collapses its own block into.
# `_DECISION_RANGE` above is reused rather than respelled -- it already carries the en dash a
# markdown editor substitutes, and two spellings of one range are how the ledger guards say
# this goes wrong.
_WAVE_SINGLE = re.compile(r"\b(\d{3})\b")


def _numbers_named(line: str) -> set[int]:
    """Every decision number a point-release line names, ranges expanded."""
    named = set()
    for low, high in _DECISION_RANGE.findall(line):
        named.update(range(int(low), int(high) + 1))
    named.update(int(n) for n in _WAVE_SINGLE.findall(line))
    return named


def _wave_decisions(date: str) -> set[int]:
    """Every decision the register heads under a block dated `date`."""
    register = _src(REGISTER)
    heads = list(_REGISTER_BLOCK_HEAD.finditer(register))
    held = set()
    for index, head in enumerate(heads):
        if date not in head.group("head"):
            continue
        end = heads[index + 1].start() if index + 1 < len(heads) else len(register)
        held.update(int(m.group(1)) for m in _BLOCK_DECISION.finditer(register[head.end(): end]))
    return held


def _unnamed_wave_decisions(text: str, wave: set[int]) -> list[str]:
    """Decisions of this wave that sections 1-14 cite and the point-release line does not name."""
    point = _POINT_RELEASE.search(_status_block(text))
    if point is None:
        return ["the Status block carries no dated point-release line"]
    named = _numbers_named(point.group(0))
    body = re.split(r"^## 0\.", text, maxsplit=1, flags=re.M)[-1]
    cited = set()
    for match in _BODY_DECISIONS.finditer(body):
        cited.update(int(n) for n in re.findall(r"\d+", match.group(1)))
    return [
        f"decision {n} is cited in the body and the point-release line does not name it"
        for n in sorted((cited & wave) - named)
    ]


def test_the_point_release_names_every_decision_of_its_own_wave_the_body_cites():
    """Decision 288's join, held in the direction it actually broke.

    The mechanism is that the dated line is the only thing connecting the register to the normative
    text, and the existing guard asks only that it name SOME decision. A line naming four of five
    reads as precise -- this one even says the counts above it are "left as first taken rather than
    quietly grown", which makes the short number look deliberate -- so a later reader has no signal
    that the wave was bigger than its own record of itself.
    [decision 288; row `platform-the-normative-file-describes-the-shipped-surface`]
    """
    spec = _normative_file()
    text = _src(spec)
    point = _POINT_RELEASE.search(_status_block(text))
    assert point, "the normative file's Status block carries no dated point-release line"
    date = _WAVE_DATE.search(point.group(0))
    assert date, f"the point-release line carries no date: {point.group(0)[:120]!r}"
    wave = _wave_decisions(date.group(1))
    assert wave, (
        f"the register heads no `## Decisions taken` block dated {date.group(1)}, so this guard "
        "reads nothing. Either the wave's own block is missing or its header stopped carrying the "
        "date the point release is stamped with."
    )
    missing = _unnamed_wave_decisions(text, wave)
    assert not missing, (
        f"{spec.relative_to(REPO).as_posix()} was amended under decisions this wave took and its "
        "point-release line does not say so:\n  "
        + "\n  ".join(missing)
        + "\n\nThe date says which wave and the numbers say what the wave was answering to "
        "(decision 288). A clause naming four of five is the shape an auditor trusts and cannot "
        "reconcile."
    )


def test_the_wave_guard_reads_the_line_and_not_the_register():
    """Both halves, and the second is what keeps the rule scoped.

    A decision this wave took, cited in the body and missing from the line, is caught. A decision
    from an EARLIER wave cited in the body is not -- sections 1-14 cite 174 and 183 that way, and a
    rule that demanded them would be asking one line to restate the whole register.
    """
    body = (
        "**Status:** implementation spec.\n"
        "**v2.1.1 (2026-09-17):** this wave folds in decisions 288-303 and 304.\n\n"
        "## 0. What changed\n\n"
        "## 5. Scoring\n\nThe form is attributed to section 3 - decision 306.\n"
        "The sweep may overwrite what decision 174 allows.\n"
    )
    assert _unnamed_wave_decisions(body, {304, 305, 306}) == [
        "decision 306 is cited in the body and the point-release line does not name it"
    ]
    assert not _unnamed_wave_decisions(body, {304, 305})

# --- M4.16 review cycle 2: the records do not promise a surface nothing serves ------------------
#
# Decision 307. The one code-and-UI item this milestone shipped is /account's Data sources block,
# and section 10 described it twice in sentences this wave wrote: `rating_source` as "the source of
# the licence text 6.8's Data sources block displays", and the four licence columns as "surfaced to
# every signed-in member". Neither is true. `DataSources.svelte` imports nothing from
# `$lib/api.js`, makes no request and renders five strings this app wrote, because the only route
# serving those columns is `api/admin.py`'s `data_sources` behind `AdminUser` -- 403 to a member,
# 401 with `X-Spielplan-Reauth: admin` to a lapsed admin. The component argues exactly that in its
# own header and then concludes the opposite of the spec.
#
# The rule is a CLAIM check and not a fingerprint, for the reason the sigma rule above gives: it
# fires on any paragraph saying the member surface DISPLAYS what `rating_source` carries, and it
# stops applying the day something under `frontend/src` reads such a route -- at which point the
# promise is kept, `docs/RELEASE.md` section 5.4 is the false record, and both come out in one
# change. That is the polarity `test_the_unmeasured_criterion_and_the_owed_asset_stay_unsigned`
# already uses. [decision 307; M4.16 cycle 2, M416-C2-ATTR-01]
_LICENCE_CARRIER = re.compile(r"licen[cs]e text|licence terms|rating_source", re.I)
_MEMBER_SURFACE = re.compile(r"/account|signed-in member|Data sources\*{0,2} block", re.I)
_DISPLAY_VERB = re.compile(r"\bdisplay(?:s|ed|ing)?\b|\bsurfaced\b|\bshown\b|\brenders\b", re.I)
# The marker this same wave uses for section 10's genome row, 7.3's playback route and 11's seams.
# A paragraph may make the promise as long as it says, in the file's own idiom, that it is not kept.
_NOT_BUILT = re.compile(r"\bnot built\b|\bnone of which is built\b", re.I)

# Where a member-readable answer would have to be read from: a CALL, not a mention. The
# component's own header names `GET /api/admin/data/sources` in the paragraph explaining why
# it does not fetch it, and `bundleImport.test.js` names the path twice inside a double that
# refuses it -- so a rule over the bare path would read the argument for this guard as the
# thing that discharges it. `routes/admin/` is excluded because that is where the admin Data
# card legitimately calls it, and `*.test.js` for `_frontend_sources`' reason: a vitest
# double is a falsifier, not a surface.
_SOURCES_READ = re.compile(r'''(?:get|post|fetch)\(\s*["'`][^"'`]*data/sources''')


def _member_reads_the_source_licences() -> list[str]:
    """Files under `frontend/src`, outside the admin routes, that read the licence columns."""
    found = []
    for path in sorted(FRONTEND.rglob("*")):
        if not path.is_file() or path.suffix not in {".svelte", ".js"}:
            continue
        rel = path.relative_to(REPO).as_posix()
        if "/routes/admin/" in rel or path.name.endswith(".test.js"):
            continue
        if _SOURCES_READ.search(_src(path)):
            found.append(rel)
    return found


def _member_licence_promises(text: str) -> list[str]:
    """Paragraphs asserting the member surface displays what `rating_source` carries, unqualified."""
    problems = []
    for paragraph in re.split(r"\n\s*\n", text):
        flat = " ".join(paragraph.split())
        if not (_LICENCE_CARRIER.search(flat) and _MEMBER_SURFACE.search(flat)):
            continue
        if not _DISPLAY_VERB.search(flat) or _NOT_BUILT.search(flat):
            continue
        problems.append(flat[:180])
    return problems


def test_the_spec_does_not_promise_member_licence_text_nothing_serves():
    """A promise marked unbuilt is honest; the same promise in the present indicative is not.

    Decision 307 refused both obvious repairs. NARROWING the clause would strike something
    decisions 292, 293 and 298 all say, and the map refuses that move in as many words -- "Not one
    of them is closed by a waiver, by a renamed test or by a narrowed sentence". LEAVING it is the
    direction this milestone exists to end, and the one M5 inherits with no plan document to
    correct it from: a normative file describing a member-visible per-source licence display that
    no code serves. So the clause stands and says it is not built, which is what section 10's own
    genome row, 7.3's playback route and 11's seams already do within a few paragraphs of it.

    The escape hatch is the point. Build the member-readable route and this guard goes red of its
    own accord, because then `docs/RELEASE.md` section 5.4 is the false record and the two are
    deleted together. [decision 307; section 10; row `map-taste-data-sources-are-attributed`]
    """
    served = _member_reads_the_source_licences()
    assert not served, (
        f"{served} now read a route serving `rating_source`'s licence columns to a member, so "
        "decision 307's debt is paid. Delete this guard, docs/RELEASE.md section 5.4 and section "
        "10's `is not built` clause in one change, and let the spec make the promise plainly."
    )
    spec = _normative_file()
    promises = _member_licence_promises(_src(spec))
    assert not promises, (
        f"{spec.relative_to(REPO).as_posix()} says the member Data sources block displays what "
        "`rating_source` carries, and nothing under frontend/src reads such a route -- the one "
        "route serving `url`/`license`/`version`/`notes` is `api/admin.py`'s `data_sources`, "
        "behind `AdminUser`:\n  "
        + "\n  ".join(promises)
        + "\n\nThe direction is measure, take the decision, amend the spec. A promise the code "
        "does not keep is marked not built under a numbered decision, or it is built -- never "
        "narrowed, which is what decisions 292, 293 and 298 each said out loud."
    )


@pytest.mark.parametrize(
    ("name", "text", "caught"),
    [
        (
            "the paragraph that shipped",
            "The per-source terms travel in `rating_source`'s `url` / `license` / `version` / "
            "`notes`, and are surfaced to every signed-in member on /account's **Data sources** "
            "block in 6.8's quiet data voice.",
            True,
        ),
        (
            "the table cell that shipped",
            "`rating_source` (mandatory always, and the source of the licence text 6.8's "
            "Data sources block displays)",
            True,
        ),
        (
            "the same promise, marked unbuilt",
            "The per-source terms travel in `rating_source`'s four columns and the admin Data card "
            "reads them out; /account's **Data sources** block carries this app's own notices, and "
            "a member-readable route for the per-source terms is not built.",
            False,
        ),
        (
            "a sentence about the block that promises no licence display",
            "/account's **Data sources** block is reachable by every signed-in member and carries "
            "one notice rather than a credit line under every poster.",
            False,
        ),
    ],
)
def test_the_member_licence_guard_reads_the_claim_and_not_the_subject(name, text, caught):
    """Two spellings refused and two accepted, and the fourth is what keeps this honest: a sentence
    about the block that promises no per-source display has to pass, or the guard would be narrowed
    by the first person it stopped."""
    assert bool(_member_licence_promises(text)) is caught, name


# The map escalates a claim it may not edit by saying, in prose, that the claim is recorded in
# `docs/RELEASE.md`. Three such claims stood and not one of them was there: section 5 held 5.1-5.3
# and section 7 held the logo, the device checks and a discharged restore. An escalation whose
# mitigation does not exist is a row's `what` outrunning its tests with an extra step in front of
# it -- an auditor follows the pointer, finds nothing, and either concludes the claim was met or
# re-discovers the gap from scratch. The parallel clause one line down, the TMDB logo's "its
# absence is recorded in docs/RELEASE.md as owed", resolves to section 7.1 AND is held by a guard,
# so the shape was already available. [M4.16 cycle 2, M416-C2-ATTR-02]
_ESCALATION_HEADER = re.compile(
    r"^# (?P<count>[A-Z]+) CLAIMS? (?:IS|ARE) ESCALATED RATHER THAN EDITED", re.M
)
_ESCALATED_ROW = re.compile(r"^#\s+-\s+`([a-z][a-z0-9]*(?:-[a-z0-9]+)+)`", re.M)


def test_every_escalated_claim_names_a_record_that_exists():
    """The map's own header block says of its escalated claims "each is in `docs/RELEASE.md`".

    That sentence is a factual claim about another file, and it is the only mitigation a widened
    `what` has: the row stays red prose because the owner has not agreed to change it, and what
    makes that acceptable rather than a quiet overclaim is that the gap is written where a release
    reader meets it. Read off the block rather than restated, so a claim repaired and removed from
    the list stops being owed a section on the same day. [decision 296; row
    `platform-exit-criteria-are-closed-by-a-committed-measurement`]
    """
    text = _src(COVERAGE)
    header = _ESCALATION_HEADER.search(text)
    assert header, (
        "spec_coverage.toml no longer carries an `N CLAIMS ARE ESCALATED RATHER THAN EDITED` "
        "header block. If the escalations are discharged, the RELEASE.md sections recording them "
        "come out in the same change; if the block was only reworded, this guard reads nothing."
    )
    block = text[header.start():].split("\n\n", 1)[0]
    ids = _ESCALATED_ROW.findall(block)
    assert ids, f"the escalation block names no row ids:\n{block}"

    spelled = _COUNT_WORDS.index(header.group("count").lower())
    assert spelled == len(ids), (
        f"spec_coverage.toml's escalation block opens {header.group('count')!r} and names "
        f"{len(ids)} rows: {ids}. Decision 184 -- restate the count against the list."
    )

    known = {row["id"] for row in tomllib.loads(text)["requirement"]}
    unknown = [i for i in ids if i not in known]
    assert not unknown, f"the escalation block names rows this map does not hold: {unknown}"

    missing = [i for i in ids if i not in _src(RELEASE_RECORD)]
    assert not missing, (
        "spec_coverage.toml's escalation block says each escalated claim is in `docs/RELEASE.md`, "
        f"and these are not in it by name: {missing}. An escalation is a row whose `what` outruns "
        "its tests, standing because the owner has not agreed to change it; the record is the only "
        "thing that makes that acceptable rather than a claim a grep disproves."
    )


# --- M5.4 review cycle 1: two words the record defines as different claims, read as one ---------
#
# `_UNMEASURED` groups `NOT BUILT` with `UNMEASURED` because both mean "no output file, and that is
# the honest answer", and every rule above reads that set. Section 1 of the record defines them as
# different claims -- never run, against does not exist yet -- and nothing held the difference.
# M5.4's row was written `NOT BUILT` as the milestone opened, under decision 331, and was still
# `NOT BUILT` in the change set that added `backend/spielplan/dna/`: the record said the package
# the criterion is about does not exist, in the commit that added nine modules of it, and the
# never-run count derived directly above therefore published six where seven was honest. M5.1's
# block, fourteen lines further up the same file, had already made the move and written down why.
#
# The discriminator is not the prose. A milestone that does not exist yet has nothing registered
# against it in the coverage map, which is why M5, M6 and M7's rows carry `tests = []` and why an
# open milestone's gate is red rather than green. A row the map names tests for is a milestone
# that exists and has not been run, and that is what UNMEASURED means.
# [decisions 296, 331; M5.4 cycle 1, M54-REV1-DOC-01]


def test_a_row_this_record_calls_not_built_is_a_milestone_the_map_names_no_test_for():
    """`NOT BUILT` is a claim about the tree, so the tree is what holds it.

    The status a row carries while its milestone is being built is the one place this record goes
    stale without anybody editing it: the lane writes `NOT BUILT` on the day the row opens and the
    code lands underneath it. Every other rule in this section reads `_UNMEASURED`, in which the
    two words are one, so the suite stayed green over a cell that had become false AND over a
    published count that under-reported the unrun criteria by one.
    [decisions 184, 296, 331; M5.4 cycle 1, M54-REV1-DOC-01]
    """
    rows = _release_rows()
    registered: dict[str, list[str]] = {}
    for row in tomllib.loads(COVERAGE.read_text(encoding="utf-8"))["requirement"]:
        if row.get("tests"):
            registered.setdefault(row["milestone"], []).append(row["id"])
    claimed = [
        name
        for name in _section_12_milestones()
        if (fields := _RELEASE_FIELDS.search(rows.get(name, ""))) is not None
        and fields.group(1).strip() == "NOT BUILT"
        and name in registered
    ]
    assert not claimed, (
        "docs/RELEASE.md records "
        + ", ".join(
            f"{name} as NOT BUILT while spec_coverage.toml names tests for it "
            f"({len(registered[name])} rows, first {registered[name][0]})"
            for name in claimed
        )
        + ". NOT BUILT means the milestone does not exist yet; a milestone the map holds tests "
        "for exists and has not been run against its criterion, which is UNMEASURED. Move the "
        "Status field, the summary table cell, this file's opening paragraph and the never-run "
        "count in spec_coverage.toml together, the way M5.1's row records doing."
    )


# --- M4.16 review cycle 4: a normative clause with more gestures than the command has ----------
#
# Decision 289 replaced a rotation §2 had promised since M0 with the one that ships, and the
# replacement sentence justified command-over-route by saying both of the command's gestures need
# the old key at the same time as the new one. `spielplan-secrets` has exactly two gestures and
# only one of them takes an old key: `rewrap` declares `--old-key` and `--new-key`, both required,
# while `reset` declares no arguments at all and reads the key the install should use from now on
# out of the environment. The source decision 289 cites for the argument -- `core/secrets_cli.py`'s
# own module docstring -- says "both HALVES need the old key", meaning the two halves of one
# rewrap, and the coverage row says it correctly too ("because both keys have to be in hand at
# once"). The normative file, the console-script declaration and this file's own strike message
# were the three that said it of the gestures, which is the one reading with no true referent: an
# operator sent to `spielplan-secrets reset` by `SecretsUnreadable` goes looking for an old-key
# argument that is not there, on an install whose custody is already broken.
#
# Read off the parser rather than restated, because a record restating it is the defect. The rule
# is two-sided by construction: give `reset` an `--old-key` and the claim becomes true and this
# goes quiet. [decision 289; M4.16 cycle 4, M416-C4D2-SPEC-03]
SECRETS_CLI = SPIELPLAN / "core" / "secrets_cli.py"

# The published form, in the two spellings the tree had. A fixed clause for `_STRIKE_CLAUSES`'
# reason: a rule over every way a sentence can phrase it is a rule over prose. The pattern cannot
# match its own source -- what follows `both ` here is a bracket.
_BOTH_GESTURES = re.compile(r"both (?:of (?:its|the) )?gestures need the", re.I)

# Every record that argues the rotation is a command: the normative file, the declaration of the
# console script, the module the argument is about, the map's row and this file's strike message.
_ROTATION_RECORDS = (
    "docs/spielplan-spec_v2.1.md",
    "backend/pyproject.toml",
    "backend/spielplan/core/secrets_cli.py",
    "backend/tests/spec_coverage.toml",
    "backend/tests/test_static_contracts.py",
)


def _secrets_gestures() -> dict[str, set[str]]:
    """Each `spielplan-secrets` subcommand and the option strings it declares, read with `ast`."""
    tree = ast.parse(_src(SECRETS_CLI))
    bound: dict[str, str] = {}
    gestures: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        call = node.value if isinstance(node, ast.Assign) else node
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        if call.func.attr == "add_parser" and call.args and isinstance(call.args[0], ast.Constant):
            name = str(call.args[0].value)
            gestures.setdefault(name, set())
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bound[target.id] = name
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if (node.func.attr == "add_argument" and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in bound):
            gestures[bound[node.func.value.id]].add(str(node.args[0].value))
    return gestures


def _gesture_claim_problems(records: dict[str, str], gestures: dict[str, set[str]]) -> list[str]:
    """Records claiming two gestures need the key being replaced, while fewer than two do."""
    holders = sorted(name for name, options in gestures.items() if "--old-key" in options)
    if len(holders) > 1:
        return []
    problems = []
    for name, text in sorted(records.items()):
        match = _BOTH_GESTURES.search(" ".join(text.split()))
        if match is None:
            continue
        problems.append(
            f"{name} says {match.group(0)!r}, and of the {len(gestures)} `spielplan-secrets` "
            f"gestures {sorted(gestures)} only {holders} declares `--old-key`"
        )
    return problems


def test_no_record_gives_the_secrets_command_two_gestures_that_need_the_old_key():
    """§2's reason for command-over-route, held against the command.

    The sentence is the load-bearing justification for the only key-custody gesture an operator
    ever performs, and it sits inside `_STRIKE_CLAUSES`' neighbourhood, so it is read closely.
    Held over every record that argues it rather than over the normative file alone: the clause
    spread by being copied, which is the mechanism decision 304's Cost paragraph named.
    [decision 289; M4.16 cycle 4, M416-C4D2-SPEC-03]
    """
    gestures = _secrets_gestures()
    assert len(gestures) > 1, (
        f"`spielplan-secrets` no longer declares more than one subcommand ({sorted(gestures)}), "
        "so a claim about what both of them need has stopped being about anything"
    )
    assert any("--old-key" in options for options in gestures.values()), (
        "no `spielplan-secrets` gesture declares `--old-key` any more, so §2's whole rotation "
        "argument is about a command that does not exist: amend the clause with the CLI"
    )
    problems = _gesture_claim_problems(
        {name: _src(REPO / name) for name in _ROTATION_RECORDS}, gestures
    )
    assert not problems, "\n  ".join(
        ["a record gives the rotation more gestures than the command has:", *problems]
    )


def test_the_gesture_guard_sees_the_two_spellings_the_tree_shipped():
    """The sentences as they shipped, kept as the synthetic violations they were -- and the third
    case is the other direction: the day `reset` takes an old key the claim becomes true, and a
    guard that went on refusing it would be a line nobody could remove honestly.

    Both are spelled in halves because this file is inside the sweep above, and a synthetic
    violation written whole would make the guard report its own test data as an offender. Joined
    at run time they are the two sentences verbatim.
    """
    gestures = _secrets_gestures()
    shipped = "because both " + "gestures need the **old** key at the same time as the new one"
    declared = "and both of its " + "gestures need the key that is being replaced"
    assert _gesture_claim_problems({"the normative file": shipped}, gestures), (
        "the guard passed §2's sentence as it shipped"
    )
    assert _gesture_claim_problems({"the declaration": declared}, gestures), (
        "the guard passed the console script's declaration as it shipped"
    )
    assert not _gesture_claim_problems(
        {"the normative file": shipped}, {"rewrap": {"--old-key"}, "reset": {"--old-key"}}
    ), "the guard still refuses the claim on a command where it would be true"


# --- M4.16 review cycle 4: a normative premise the importer forbids ----------------------------
#
# Phase B rewrote section 14's risk 7 to the refusal decision 163 rules, and the rewrite kept the
# conclusion it had while inverting the premise under it: "the importer REFUSES a vocabulary
# version change ... two vocabularies may THEREFORE coexist in the tables after a re-import". The
# refusal is what makes that unreachable, not what implies it. `importer/bundle.py` fails the
# report on both routes in - a bundle declaring a different version, and (decision 256) a bundle
# declaring none against an install that has one - and `import_bundle` returns before staging when
# the report is not ok, so no import this build performs can leave two versions in `dna_tag` and
# `dna_projected`. The rule the paragraph exists to carry is real and implemented (`fetch_blocks`
# takes `vocab_version`); only its stated premise is not, and the paragraph's own first clause -
# "a vocabulary change is a migration, not an import" - contradicts it three sentences later.
#
# Held against the importer rather than against the sentence, and two-sided by construction: the
# day the refusal comes out, a re-import can produce the state again and this goes quiet. What is
# NOT held here is the five code comments that quote the same claim from a section 10 sentence
# this wave retired; they belong to the milestones that own those modules and are reported rather
# than repaired across the ownership line. [decision 163; M4.16 cycle 4, M416-C4D2-SPEC-04]
VOCAB_IMPORTER = SPIELPLAN / "importer" / "bundle.py"
# The refusal id both arms of `refuse_on_install_state` fail the report with.
_VOCAB_REFUSAL = '"vocabulary-migration"'

# The published form: coexistence pinned on a re-import. A fixed clause for `_STRIKE_CLAUSES`'
# reason, and the gap is a character class so the pattern cannot match its own source.
_COEXIST_AFTER_REIMPORT = re.compile(r"coexist[^.]{0,80}after a re-import", re.I)


def _coexistence_problems(spec: str, importer: str) -> list[str]:
    """The normative file pinning two vocabularies on a re-import the importer refuses."""
    if _VOCAB_REFUSAL not in importer:
        return []
    match = _COEXIST_AFTER_REIMPORT.search(" ".join(spec.split()))
    if match is None:
        return []
    return [
        f"the normative file says {match.group(0)!r}, and `importer/bundle.py` fails the report "
        "with `vocabulary-migration` on both routes in, before `import_bundle` stages anything"
    ]


def test_the_normative_file_pins_no_second_vocabulary_on_a_re_import():
    """Section 14 risk 7's premise, held against the code that makes it unreachable.

    A reader deriving the version-scoping requirement from risk 7 was handed a premise the
    importer contradicts one paragraph earlier, and an M5 agent building the extraction flywheel -
    the one path that could actually mint a second vocabulary - was pointed at "after a re-import"
    as the case to handle, which is the case that cannot happen.
    [decision 163; M4.16 cycle 4, M416-C4D2-SPEC-04]
    """
    importer = _src(VOCAB_IMPORTER)
    assert _VOCAB_REFUSAL in importer, (
        "importer/bundle.py no longer fails a report with `vocabulary-migration`. If decision "
        "163's refusal has genuinely gone, risk 7's premise becomes reachable again and this "
        "guard comes out with it rather than being narrowed."
    )
    problems = _coexistence_problems(_src(_normative_file()), importer)
    assert not problems, "\n  ".join(
        ["a normative sentence rests on a state this build's importer refuses:", *problems]
    )


def test_the_coexistence_guard_sees_the_premise_that_shipped():
    """The sentence as it shipped, kept as the synthetic violation it was, and the other
    direction: with the refusal gone the claim is admissible again."""
    shipped = (
        "Until a migration plan exists the importer refuses a vocabulary version change and says "
        "why (decision 163). Two vocabularies may therefore coexist in the tables after a "
        "re-import, so every read is scoped to one version."
    )
    importer = _src(VOCAB_IMPORTER)
    assert _coexistence_problems(shipped, importer), "the guard passed risk 7 as it shipped"
    assert not _coexistence_problems(shipped, "no refusal here"), (
        "the guard still refuses the premise on an importer that has stopped refusing, where it "
        "would be true"
    )
    assert not _coexistence_problems(_src(_normative_file()), importer)


def _md_subsection(text: str, heading: str) -> str:
    """One `### N.M` block of a Markdown record, in `_gate_record`'s idiom."""
    parts = text.split(heading, 1)
    return re.split(r"^#{2,3} ", parts[1], maxsplit=1, flags=re.M)[0] if len(parts) == 2 else ""


# --- M4.16 review cycle 4: the one leg cell that claimed a run nothing has made -----------------
#
# Leg 3 is two commands, and the table recorded it as a `yes` naming the second one's artefact.
# The first is `pytest test_bundle_shapes.py test_bundle_validation.py -k real_bundle`, whose two
# tests are `skipif`-gated on `CORPUS_BUNDLE_DIR`; nothing in this repository sets that variable
# outside `real-bundle.yml`, whose runner has never been registered, so those two tests have never
# once reported as run - decision 183 has owed that since M4.8. Leg 1's cell is qualified and
# decision 313 moved leg 2's to a partial for a strictly WEAKER reason (its pytest half HAS been
# run once by hand), so the unqualified `yes` was the odd one out in a record whose stated job is
# "the fact of a run, or the fact of its absence".
#
# Held against the skip rather than against the sentence, and two-sided: the day those tests stop
# being gated, the cell may say `yes` and this goes quiet. The middle column is otherwise
# deliberately unguarded (`_GATE_POLARITY`'s comment says why: it moves as work lands by hand);
# what is held here is the one cell whose subject cannot be run at all from this lane.
# [decision 183; decision 313's idiom; M4.16 cycle 4, REL-C4-10]
_REAL_BUNDLE_MODULES = ("test_bundle_shapes.py", "test_bundle_validation.py")
_CORPUS_SKIP = re.compile(
    r"@pytest\.mark\.skipif\(\s*not os\.environ\.get\(\"CORPUS_BUNDLE_DIR\"\)"
)
_LEG_THREE_ROW = re.compile(r"^\| *3 [^|\n]*\|(?P<substance>[^|\n]*)\|", re.M)


def _leg_three_problems(block: str, gated: int) -> list[str]:
    """The leg-3 substance cell recorded as run while its first command can only skip."""
    if not gated:
        return []
    row = _LEG_THREE_ROW.search(block)
    if row is None:
        return ["section 2.1's per-leg table has no leg 3 row, and leg 3 is the one with a skip"]
    cell = row.group("substance").strip().strip("*`")
    if "partial" in cell.lower() or "skip" in cell.lower() or cell.startswith("no"):
        return []
    return [
        f"leg 3's substance cell reads "
        f"{cell.encode('ascii', 'backslashreplace').decode()!r}, and the {gated} test(s) its "
        "first command selects are skipif-gated on CORPUS_BUNDLE_DIR, which nothing outside "
        "real-bundle.yml sets and no registered runner has ever supplied"
    ]


def test_the_release_record_does_not_record_the_real_bundle_leg_as_run():
    """Exit criterion 4's own half, one cell in.

    A reader weighing that criterion reads down the middle column and concludes leg 3's substance
    has been exercised. `test_a_real_bundle_still_matches_the_committed_manifest` - the one test
    that holds a real bundle to the committed manifest, and the thing `real_bundle_shapes.json`
    exists for - has no record of ever having been run anywhere, and the cell that would say so
    said `yes`. [decision 183; M4.16 cycle 4, REL-C4-10]
    """
    gated = sum(
        len(_CORPUS_SKIP.findall(_src(REPO / "backend" / "tests" / name)))
        for name in _REAL_BUNDLE_MODULES
    )
    assert gated, (
        f"no test in {list(_REAL_BUNDLE_MODULES)} is skipif-gated on CORPUS_BUNDLE_DIR any more. "
        "If leg 3's first command can now run unattended, its cell may say so and this guard "
        "comes out in the same change."
    )
    problems = _leg_three_problems(_gate_record(_src(RELEASE_RECORD)), gated)
    assert not problems, "\n  ".join(
        ["docs/RELEASE.md records a leg as run whose first command can only skip here:", *problems]
    )


def test_the_leg_three_guard_sees_the_cell_that_shipped():
    """The cell as it shipped, and the other direction."""
    shipped = (
        "| Leg | Substance run from this lane | By this job |\n|---|---|---|\n"
        "| 3 - the real-bundle legs and M4.5's criterion | yes - "
        "`docs/milestones/M4.5-exit.txt` | never |\n"
    )
    assert _leg_three_problems(shipped, 2), "the guard passed leg 3's cell as it shipped"
    assert not _leg_three_problems(shipped, 0), (
        "the guard still refuses the cell on a tree where those tests no longer skip"
    )
    assert _leg_three_problems(_gate_record(_src(RELEASE_RECORD)), 2) == []


# --- M4.16 review cycle 4: a record reporting an amendment nobody made -------------------------
#
# Decision 311's Cost paragraph closed by saying that §4.1 and §4.3 "now say plainly that such a
# restore does change a pre-291 box's placement inputs". They say nothing of the kind: neither
# section carries the word restore, archive or /data/backups, and the substance exists only in
# `backup/movie_data.py`'s `RETIRED` block, which is not normative. The register is the file this
# milestone made normative for entries 162 onward, so a decision there stated as DONE an amendment
# to the normative file that was never written - and the same entry's own code comment carried the
# weaker true version ("and is why §4.1 and §4.3 now state what this build IMPORTS"), so the two
# records of one decision disagreed about what landed. A reader auditing 311 against §4.1 cannot
# tell a dropped amendment from a wrong decision.
#
# Held as a fixed clause over the family rather than over the one sentence, which would be a guard
# that can only pass: no record may attribute a RESTORE consequence to §4.1 or §4.3 while neither
# section names one. Two-sided by construction - the day either section gains a restore clause the
# claim becomes true and this goes quiet - and the order matters, because the true sentences in
# this tree put the restore BEFORE the sections it explains ("which is the restore doing what
# decision 309 rules ... and is why §4.1 and §4.3 now state what this build IMPORTS").
# [decisions 309 and 311; M4.16 cycle 4, M416-C4-GEN-07]
_GENOME_SECTIONS = ("### 4.1 ", "### 4.3 ")
_RESTORE_IN_SECTION = re.compile(r"\brestore|\barchive|/data/backups", re.I)
_SECTION_SAYS_RESTORE = re.compile(
    r"§4\.[13][^.]{0,120}\b(?:say|says|state|states)\b[^.]{0,200}\brestore\b", re.I
)


def _restore_attribution_problems(records: dict[str, str], sections: str) -> list[str]:
    """Records putting a restore's consequence in §4.1's or §4.3's mouth while neither says it."""
    if _RESTORE_IN_SECTION.search(sections):
        return []
    problems = []
    for name, text in sorted(records.items()):
        for match in _SECTION_SAYS_RESTORE.finditer(" ".join(text.split())):
            problems.append(
                f"{name} says {match.group(0).encode('ascii', 'backslashreplace').decode()!r}"
            )
    return problems


def test_no_record_says_the_genome_sections_speak_about_a_restore():
    """Decision 311's Cost paragraph, held against the sections it reported on.

    §4.1 is where a reader goes, and what the household is actually handed is README's Recovery
    gesture: write a movie-data archive, restore it into a rebuilt box. On a pre-291 install that
    zeroes the genome block. Whether the normative file should say so is an owner's call and no
    decision has taken it; what it may not do is have a record say it already does.
    [decisions 309 and 311; M4.16 cycle 4, M416-C4-GEN-07]
    """
    spec = _src(_normative_file())
    sections = "".join(_md_subsection(spec, heading) for heading in _GENOME_SECTIONS)
    assert sections.strip(), (
        f"the normative file no longer carries {list(_GENOME_SECTIONS)}, so this guard cannot "
        "tell whether a record describing them is right"
    )
    records = {
        name: _src(REPO / name)
        for name in (*_GENOME_PATH_RECORDS, "docs/spec-v2.2-proposals.md", "docs/RELEASE.md")
    }
    problems = _restore_attribution_problems(records, sections)
    assert not problems, "\n  ".join(
        [
            "a record puts a restore's consequence in the genome sections' mouth, and neither "
            "section mentions one:",
            *problems,
        ]
    )


def test_the_attribution_guard_sees_the_sentence_decision_311_shipped():
    """The Cost paragraph as it shipped, kept as the synthetic violation it was, and the other
    direction: the day §4.1 gains the clause, the same sentence becomes true."""
    shipped = {
        "the register": "what it rules is unaffected, and \u00a74.1 and \u00a74.3 now say plainly that "
                        "such a restore does change a pre-291 box's placement inputs"
    }
    assert _restore_attribution_problems(shipped, "no such clause here"), (
        "the guard passed decision 311's Cost paragraph as it shipped"
    )
    assert not _restore_attribution_problems(
        shipped, "a /data/backups movie-data archive passes over the three tables"
    ), "the guard still refuses the claim on a spec that has since said it"
# --- M5.1 review cycle 1: the figures the documents publish about these scripts -----------------

# Three figures about `ops/m*_exit_criterion.py` reach a reader as prose, and only one of them was
# derived from anything: how many of the scripts there are (held by hand, five `== 9` assertions a
# wave has to move deliberately, which is decision 184's shape), how many numbered checks one of
# them holds, and how long one of them is. The last two sat in section 12's row -- the file
# CLAUDE.md calls normative, and the row an owner signs -- and twice more in `docs/RELEASE.md`, as
# words somebody typed.
#
# THE CLASS HAS BITTEN TWICE, which is why decision 184 exists at all. `ops/m45_exit_criterion.py`
# published a bolded 18/18; M4.8 turned one of the eighteen into a real check and another into a
# plain print, and nobody in that build could say what the corrected number was. And it bit again
# inside this review cycle: "1,522 lines" was true at the commit that wrote it and false by the
# time a sibling group had finished repairing that script, with nothing anywhere to say so. So the
# rule this file already applies to prose -- a document that states a fact about this tree is held
# to the tree -- is applied to the three figures that describe the instruments section 12 names.
#
# The checks figure is read STRICTLY and the lines figure LENIENTLY, which is not an inconsistency:
# "N numbered checks" is a phrase about these scripts and nothing else, so a block making that
# claim without naming one is itself the defect; "N lines" is ordinary English, and a sentence
# counting something else in a block that happens to name a script is not a claim about the script.
# [decision 184; M5.1 review cycle 1, M51-REV-REG-02]
#
# WHITESPACE-TOLERANT, which `_RAW_STORE_REREAD` below already is and for this file's own recorded
# reason: a phrase re-wrapped across a line break stops being read. `docs/RELEASE.md`'s M5.3 block
# wrapped "eleven numbered / checks", so the one figure that block publishes was invisible here and
# `_published_figure_problems` returned no problem for it whatever number it said - silently,
# because a pattern that finds nothing reports nothing. [M5.3 review cycle 2, m53-c2-dim-exit-01]
_PUBLISHED_CHECKS = re.compile(r"(\d+|[A-Za-z]+)\s+numbered\s+checks?\b")
_PUBLISHED_LINES = re.compile(r"\b([\d,]+) lines\b")
_NAMED_SCRIPT = re.compile(r"ops/(m[a-z0-9]+_exit_criterion\.py)")


def _published_number(token: str) -> int | None:
    """A count as either spelling these documents use: `13`, `1,522` or `thirteen`."""
    bare = token.replace(",", "")
    if bare.isdigit():
        return int(bare)
    return _COUNT_WORDS.index(bare.lower()) if bare.lower() in _COUNT_WORDS else None


def _script_numbered_checks(name: str) -> int:
    """How many numbered checks `ops/<name>` holds, read off its own `CHECKS` tuple.

    Both spellings the scripts use are read -- `CHECKS = [...]` in the M4.11 and M4.12 scripts and
    the annotated `CHECKS: tuple[tuple[int, str], ...] = (...)` the three since -- and a script
    that exposes neither is reported rather than skipped. Four of the nine hold no `CHECKS` at all,
    so a guard that quietly passed on a name it could not resolve would cover three files while
    reading as though it covered nine, which is the defect one layer up from the one it is for.
    """
    path = REPO / "ops" / name
    assert path.exists(), f"a document names ops/{name} and the tree does not hold it"
    for node in ast.parse(_src(path)).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        else:
            continue
        if not (isinstance(target, ast.Name) and target.id == "CHECKS"):
            continue
        assert isinstance(value, (ast.Tuple, ast.List)), (
            f"ops/{name} declares CHECKS as something other than a tuple or list literal, so the "
            "count a document publishes about it cannot be derived. State it in a form this guard "
            "can read, or the figure goes back to being a number nobody measured."
        )
        return len(value.elts)
    raise AssertionError(
        f"a document publishes a count of ops/{name}'s numbered checks and that script declares "
        "no module-level CHECKS. Decision 184: a figure published as measured is derived, so "
        "either the script names its checks in one place or the document stops counting them."
    )


def _published_figure_problems(label: str, text: str) -> list[str]:
    """Every figure this document publishes about an exit script, against the script itself.

    The script a figure is about is the last one named before it inside the same block, which is
    how both documents write it: section 12's cell names the instrument and then counts it, and
    `docs/RELEASE.md` names it at the head of the paragraph that then says how big it is.
    """
    problems: list[str] = []
    for pattern, noun in ((_PUBLISHED_CHECKS, "numbered checks"), (_PUBLISHED_LINES, "lines")):
        for match in pattern.finditer(text):
            where = f"{label}:{text[: match.start()].count(chr(10)) + 1}"
            named = _NAMED_SCRIPT.findall(text[: match.start()].rsplit("\n\n", 1)[-1])
            if not named:
                if noun == "lines":
                    continue
                problems.append(
                    f"{where}: '{match.group(0)}' names no exit script in its own paragraph, so "
                    "there is nothing to derive it from. Name the script beside the count."
                )
                continue
            published = _published_number(match.group(1))
            if published is None:
                problems.append(f"{where}: '{match.group(0)}' is neither a figure nor a number word")
                continue
            held = (
                _script_numbered_checks(named[-1]) if noun == "numbered checks"
                else len(_src(REPO / "ops" / named[-1]).splitlines())
            )
            if published != held:
                problems.append(
                    f"{where}: it publishes {published} {noun} for ops/{named[-1]}, which holds "
                    f"{held}"
                )
    return problems


# A test id an exit script's docstring hands a criterion clause to. The docstring wraps long ids
# at 108 columns with a trailing underscore, so the reader joins those before it matches.
_HANDED_OFF = re.compile(r"backend/tests/test_\w+\.py::(test_\w+)")


def _clauses_the_script_hands_off(path: Path) -> list[str]:
    """The tests an exit script's own docstring says measure a clause none of its checks does."""
    doc = ast.get_docstring(ast.parse(_src(path))) or ""
    return _HANDED_OFF.findall(re.sub(r"_\n\s*", "_", doc))


def test_what_m52s_records_say_of_its_instrument_is_what_the_instrument_does():
    """Decision 184 over the two sentences an owner reads before signing M5.2's row.

    Section 12's M5.2 cell carries a clause the plan's pass table does not -- a re-stamped owned
    title exits at stage 1 without minting or paid work (decision 411) -- and then credited the
    whole criterion to `ops/m52_exit_criterion.py`'s eleven numbered checks. The script says in its
    own docstring that none of the eleven measures that clause and names the test that does, and
    `docs/RELEASE.md`'s copy, the page with the verdict blank, had dropped the clause without a
    word: an owner signing from an 11/11 run signed a sentence one clause of which the named
    instrument never looked at. So every test the script hands a clause to is named in the cell
    and in the record's copy. The second half is decision 371's cost, which gave "docker, a real
    install and the fake on :8097" as why only the owner can run the script, over a script that says
    NOTHING HERE NEEDS DOCKER, A PORT OR A BROWSER and refuses to run without the corpus bundle.
    [decisions 184, 371 and 411; M5.2 review cycle 3: M52-C3-PAPER-03, M52-C3-PAPER-07]
    """
    script = REPO / "ops" / "m52_exit_criterion.py"
    handed = _clauses_the_script_hands_off(script)
    assert handed, (
        "ops/m52_exit_criterion.py no longer hands any clause to a test, so this guard reads "
        "nothing. If every clause is now one of its checks, the guard comes out with the hand-off."
    )
    row = next(
        (line for line in _src(_normative_file()).splitlines() if line.startswith("| **M5.2** |")),
        "",
    )
    copy = re.search(r"^### M5\.2 .*?(?=^### )", _src(RELEASE_RECORD), re.M | re.S)
    assert row and copy, "section 12 or docs/RELEASE.md no longer carries M5.2's row"
    unnamed = [f"section 12's M5.2 cell does not name {name}" for name in handed if name not in row]
    unnamed += [
        f"docs/RELEASE.md's M5.2 section does not name {name}"
        for name in handed if name not in copy.group(0)
    ]
    assert not unnamed, (
        "ops/m52_exit_criterion.py hands a clause of section 12's criterion to a test, and a record "
        "the owner signs from still credits it to the script or drops it:\n  " + "\n  ".join(unnamed)
    )

    doc = ast.get_docstring(ast.parse(_src(script))) or ""
    entry = re.search(r"^### 371\. .*?(?=^##)", _src(REGISTER), re.M | re.S)
    assert entry, "the register no longer heads decision 371"
    assert "NOTHING HERE NEEDS DOCKER" in doc, (
        "ops/m52_exit_criterion.py no longer says it needs no docker; re-read decision 371's cost"
    )
    stated = " ".join(entry.group(0).split())
    assert not re.search(r"\bneeds docker\b|:8097", stated), (
        "decision 371 gives docker or the e2e fake's port as why the owner runs the script, which "
        "needs neither"
    )
    assert "corpus bundle" in stated, (
        "decision 371 does not name the corpus bundle, which is what the script refuses to run "
        "without and why it is the owner's to run"
    )


# --- M5.1 review cycle 4: an exit script cites the application by name, never by line ----------
#
# `check_eleven` of `ops/m51_exit_criterion.py` argued decision 332 by pointing at `app.py:522`
# for the rule that declines `/events`, and at HEAD that line was exactly that rule. Decision
# 332's own rewrite of the class -- the `/events` paragraph, `SERVER_NAMESPACES` and review cycle
# 3's lstrip comment -- pushed the rule twenty-five lines down inside the SAME uncommitted change
# set, so the coordinate came to rest on a prose fragment in the class docstring and the check's
# argument sent its reader to a sentence about 405s. An operator verifying check 11 then cannot
# tell a stale note from a rule that moved, which is the afternoon
# `test_no_backend_comment_cites_the_tonight_client_by_line_number` settles for the backend citing
# the frontend: cite the function, not the line. That guard globs `backend/` and the seam's
# sibling reads one comment block in `api.js`, so neither of them has ever read `ops/` -- and
# this is the one citation those scripts make into the module that decides the routing they
# measure over HTTP, which is why it is the one held by a name rather than by a coordinate.
#
# HELD IN BOTH DIRECTIONS, because a ban on its own is satisfied by deleting the citation: the
# names a script does cite have to RESOLVE, which is the property the coordinate never had. The
# class is found by walking the module rather than by reading its top level, since `SpaFallback`
# is declared inside `create_app` under the static-directory branch -- the mount only exists when
# a static build does. Over every milestone script rather than over the one that was wrong: M5.2
# through M5.7 each owe a script under decision 321, and §7.2, §7.3 and §11 all put routes under
# the namespace this rule decides. [decision 332; M5.1 review cycle 4, M51-C4-CITE-01]
_APP_BY_LINE = re.compile(r"app\.py:\d+")
_APP_SYMBOL = re.compile(r"`SpaFallback\.(\w+)`")


def _application_citations(text: str) -> tuple[list[str], set[str]]:
    """(coordinates into `app.py`, `SpaFallback` members named) one script's source carries."""
    return _APP_BY_LINE.findall(text), set(_APP_SYMBOL.findall(text))


def test_no_milestone_script_cites_the_applications_routing_rule_by_line_number():
    """A coordinate into another file is a citation nothing can keep true; a name is checkable.

    So both halves are asserted here, and the second is why this is not simply a ban: a script
    that answered the ban by dropping the citation would leave check 11 arguing decision 332 from
    nothing at all. [decision 332; M5.1 review cycle 4, M51-C4-CITE-01]
    """
    coordinates: dict[str, list[str]] = {}
    cited: set[str] = set()
    for path in EXIT_SCRIPTS:
        stale, symbols = _application_citations(_src(path))
        if stale:
            coordinates[path.name] = stale
        cited |= symbols
    assert not coordinates, (
        "cite the function, not the line: a milestone exit script cites the application by a "
        "coordinate the next edit to app.py moves, and this milestone's own edit moved it: "
        f"{coordinates}"
    )
    assert cited, (
        "no milestone script names a `SpaFallback.<member>` any more, so the half of this rule "
        "that holds a citation to the application is reading nothing. If check 11 has stopped "
        "citing the decline rule, this guard comes out together with it."
    )
    module = ast.parse(_src(REPO / "backend" / "spielplan" / "app.py"))
    declared = [
        node for node in ast.walk(module)
        if isinstance(node, ast.ClassDef) and node.name == "SpaFallback"
    ]
    assert len(declared) == 1, (
        f"backend/spielplan/app.py declares SpaFallback {len(declared)} time(s), so a citation "
        "naming one of its members cannot be resolved against it"
    )
    held = {
        node.name for node in declared[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    } | {
        target.id for node in declared[0].body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    missing = sorted(cited - held)
    assert not missing, (
        f"a milestone script cites SpaFallback members app.py does not declare: {missing}. The "
        f"class holds {sorted(held)} -- a name that no longer resolves is the same defect as a "
        "line number that no longer resolves, caught one edit later."
    )


def test_the_application_citation_reader_tells_a_coordinate_from_a_name():
    """Both ends of the reading, on the two spellings this milestone actually wrote.

    The first is the sentence check 11 shipped with and the second is the sentence it carries now,
    so neither is an invention: a reader that answered the same for both would make the rule above
    vacuous in whichever direction it was blind. [M5.1 review cycle 4, M51-C4-CITE-01]
    """
    stale, symbols = _application_citations("declines `/api` (`app.py:522`), so an unrouted path")
    assert stale == ["app.py:522"] and not symbols

    stale, symbols = _application_citations(
        "declines `/api` -- `SpaFallback.matches`, over `SpaFallback.SERVER_NAMESPACES` -- so an"
    )
    assert not stale and symbols == {"matches", "SERVER_NAMESPACES"}


# --- M5.1 review cycle 3: this file's prose sizes registries this file already derives ---------
#
# Three registries grew inside M5.1 and three sentences here went on stating the size they had
# before it: §12's build order gained a row, `docs/RELEASE.md`'s section 1 gained that row and the
# block answering it, and the register gained the sittings the milestone opened and closed with.
# The first figure was corrected by hand in `docs/RELEASE.md` inside the same change set and not
# here, which makes this a mirror that was missed rather than a class nobody had noticed -- and it
# was missed because nothing reads a comment. That is decision 184, a published figure is derived
# or it is a claim, applied one level in from the documents where it has always been enforced; it
# belongs here for two reasons rather than as a matter of taste. These sentences are the
# explanation attached to a live constant and to two live readers, which is the only kind of
# comment this codebase writes. And decision 331 puts each of M5.2 through M5.7 into §12 as it
# opens, so all six move the same figures again -- in the file that holds the guard over the very
# table being counted, where a reader has the most reason to trust what the prose says.
#
# Anchored on THE NOUN THAT NAMES THE SET, in `_router_tallies`' idiom, rather than on the bare
# word "rows": this file uses that word of coverage rows, map rows, genome rows and bundle rows,
# and a rule over all of them would be ruling on sets it knows nothing about, which is the mistake
# `test_layering_guards.py` declined to make first. The exemption is `_job_tallies`' exactly -- a
# sentence carrying a milestone tag is a dated measurement and stays, because a figure that says
# when it was true is not a claim about the tree the reader is looking at -- and its looseness
# comes with it, since a milestone named in that sentence for any other reason exempts the count
# too. Measured rather than assumed: the sentence opening the release-workflow block above names
# M4.5 for an unrelated reason and is exempt for it. That is the price of not inventing a second
# convention for what one file over already has, and it is why the sentences this was written for
# are REPAIRED rather than left standing for the guard to hold. A live claim drops the numeral, a
# diagnosis of a defect says which cycle measured it, and the one that read "beside the fifteen"
# -- a count with no noun at all, which no rule anchored on a noun can ever read -- names the set.
# [decision 184; decision 331; M5.1 review cycle 3, M51-C3-REG-03, M51-C3-REG-04]
_SELF_TALLIES = (
    (re.compile(r"§12[^.;]{0,60}?\b(?P<count>\w+(?:-\w+)?)\s+rows\b", re.I), "build-order"),
    (re.compile(r"\b(?P<count>\w+(?:-\w+)?)\s+build-order\s+rows\b", re.I), "build-order"),
    (re.compile(r"\b(?P<count>\w+(?:-\w+)?)[- ]row\s+table\b", re.I), "section-1"),
    (re.compile(r"\b(?P<count>\w+(?:-\w+)?)\s+section-1\s+rows\b", re.I), "section-1"),
    (re.compile(r"\b(?P<count>\w+(?:-\w+)?)\s+`?## Decisions taken`?\s+blocks\b", re.I), "sitting"),
)


def _self_tallies(path: Path) -> list[tuple[int, str, str, str]]:
    """Every sentence of `path`'s prose that sizes one of those registries, and how it spells it.

    Paragraphs rather than lines, because this file wraps at 108 columns and the sentence that went
    stale in `test_layering_guards.py` put its count and its noun on either side of a break -- the
    limit `_comment_paragraphs` exists to close. Sentences rather than whole paragraphs, because
    the dated form has to be read the way it is written: a rule that asked a whole paragraph for a
    milestone tag would exempt a live claim for sitting next to an unrelated citation.
    """
    found: list[tuple[int, str, str, str]] = []
    for line, text in _comment_paragraphs(path):
        for sentence in re.split(r"(?<=[.;]) ", text):
            if re.search(r"\bM\d", sentence):
                continue
            for pattern, registry in _SELF_TALLIES:
                for match in pattern.finditer(sentence):
                    word = match.group("count").lower()
                    if word.isdigit() or word in _COUNT_WORDS:
                        found.append((line, sentence.strip(), word, registry))
    return found


def test_the_registry_size_reader_reads_the_sentences_this_file_shipped(tmp_path):
    """Every spelling the three stale sentences came in, the wrap, and the dated form that stays.

    The stale sentences are kept as string literals, which are not prose: the guard above reads
    comments and docstrings, so it does not read its own fixture. The last block is the shape that
    must stay silent -- a diagnosis that says when it was taken -- or the repair reddens the guard
    that asked for it.
    """
    module = tmp_path / "note.py"
    module.write_text(
        "# §12 carries fifteen rows.\n"
        "# A record that counts fifteen build-order rows.\n"
        "# Section 1 opens with a sixteen-row table.\n"
        "# Measured over the sixteen section-1 rows.\n"
        "# The register holds 24 `## Decisions taken` blocks.\n"
        "\n"
        "# It wrapped: §12 carries\n"
        "# fifteen rows.\n"
        "\n"
        "# At M4.16 cycle 4 the register held 24 `## Decisions taken` blocks.\n"
        "\n"
        "# It gained a row, and section 1 gained the block that answers it.\n",
        encoding="utf-8",
    )

    found = _self_tallies(module)

    assert [(line, word, registry) for line, _sentence, word, registry in found] == [
        (1, "fifteen", "build-order"),
        (1, "fifteen", "build-order"),
        (1, "sixteen", "section-1"),
        (1, "sixteen", "section-1"),
        (1, "24", "sitting"),
        (7, "fifteen", "build-order"),
    ], f"the reader misses a spelling, reads the dated form, or loses the wrapped one: {found}"


def test_the_paragraph_reader_glues_a_wrapped_comment_and_leaves_the_docstring_whole(tmp_path):
    """The run is one paragraph, a bare `#` ends it, and a docstring is never glued to either.

    The last is the objection `test_worker_schedule.py` records against gluing at all, so it is
    asserted rather than argued: the docstring arrives on its own, keyed to the `def` it hangs
    under, and the comment above it is not joined to it. [M5.1 review cycle 3, M51-C3-REG-02]
    """
    module = tmp_path / "note.py"
    module.write_text(
        "# The note says all fourteen\n"
        "# routers by definition.\n"
        "#\n"
        "# A second paragraph.\n"
        "\n"
        "def f():\n"
        '    """A docstring under a comment."""\n'
        "\n"
        "\n"
        "# A trailing note.\n",
        encoding="utf-8",
    )

    assert _comment_paragraphs(module) == [
        (1, "# The note says all fourteen routers by definition."),
        (4, "# A second paragraph."),
        (10, "# A trailing note."),
        (6, "A docstring under a comment."),
    ]


# --- M5.3 review cycle 1: a criterion may not name an action nothing in the tree performs --------
#
# §12's M5.3 row promised that the admin retry "re-reads the content-addressed raw store instead of
# re-fetching", and no path in this tree does it. `pipeline._resume_index` answers the BOARD's
# stage, so a job parked at stage 4 re-enters at `reviews gate`, whose body is `gate.measure` and
# an advance-or-park and nothing else; `derive/gate.py` imports `dataclasses`, `datetime` and
# `asyncpg` and reads `title.overview` and `review_store.review`. The one `rawstore.read` in the
# app is `derive/rebuild.py`'s, which is stage 3 and BEHIND the resume point. What the retry does
# hold -- and what check 5 and `test_a_retry_of_a_parked_gate_resumes_at_stage_four_and_makes_no_
# request` measure three ways -- is that nothing is re-fetched, which is what decision 424 narrows
# the sentence to.
#
# BOTH DIRECTIONS, because the sentence may become true rather than staying false: decision 330
# leaves "retry from stage N" to M5.6, and a retry re-entering at stage 3 would re-read those bytes
# for real. The day the resume path reaches the raw store, this asks for the clause back rather
# than going quiet. The two owner documents carrying the older phrasing -- `M5.3-plan.md` §7 and
# `ROADMAP-M5.md:290` -- are deliberately not read: an agent may not edit a plan, so a guard that
# reported one would be reporting a file nothing here is allowed to repair. The map's own
# `jellyfin-acquisition-eval-stage-park-retry-idempotent` states the clause a third way, as the
# requirement M5 inherited, and its comment block records which reading closes it. The sentence
# itself is quoted verbatim in decision 424 and nowhere else in these three files, which is what
# this anchor costs: provenance goes in the register, and a document held here describes the old
# wording rather than repeating it.
# [decision 424; M5.3 review cycle 1, M53-EXIT-02]
#
# Whitespace-tolerant because the clause is WRAPPED in two of the three files it was written
# into, and a reader that walks lines misses it there -- which is this file's own recorded
# failure mode one guard family over: "a part re-wrapped across a line break stops being read".
_RAW_STORE_REREAD = re.compile(r"re-(?:reads?|uses?)\s+the\s+content-addressed\s+raw\s+store")

# The three files this milestone wrote the criterion into: the normative row (decision 346 gives
# M5.3 that row and nothing else in the spec), the release record's restatement, and the
# instrument's own quotation of the row it measures.
_CRITERION_COPIES = ("docs/RELEASE.md", "ops/m53_exit_criterion.py")


def test_no_criterion_promises_a_raw_store_read_the_parked_resume_cannot_perform():
    """The clause and the code path it is about, held to each other in one place.

    A sentence in the normative file is not falsifiable by reading the normative file, which is how
    this one survived: it was transcribed into §12's M5.3 row as the milestone opened, the check
    that measures it reports exactly the three zeros it does measure, and the gap between the two
    is visible only to a reader holding `acquire/pipeline.py`, `acquire/stages.py` and
    `derive/gate.py` open at once. An owner signing the row would close a clause nothing asked.
    [decision 424; M5.3 review cycle 1, M53-EXIT-02]
    """
    package = REPO / "backend" / "spielplan"
    stages = ast.parse(_src(package / "acquire" / "stages.py"))
    parked_stage = next(
        (node for node in ast.walk(stages)
         if isinstance(node, ast.AsyncFunctionDef) and node.name == "reviews_gate"),
        None,
    )
    assert parked_stage is not None, (
        "`acquire/stages.py` no longer defines `reviews_gate`, so this reads nothing about the "
        "stage a job parked by the reviews gate re-enters at. Name the stage it parks in now."
    )
    # `ast.unparse` and not the file: stage 2 three hundred lines above writes `raw_document` rows
    # and argues about them in prose, and the resume point is this function alone.
    resume = {
        "acquire/pipeline.py": _src(package / "acquire" / "pipeline.py"),
        "acquire/stages.py::reviews_gate": ast.unparse(parked_stage),
        "derive/gate.py": _src(package / "derive" / "gate.py"),
    }
    reaches = sorted(
        name for name, text in resume.items()
        if "rawstore" in text or "raw_document" in text
    )

    claimed = []
    for path in (_normative_file(), *(REPO / name for name in _CRITERION_COPIES)):
        text = _src(path)
        for found in _RAW_STORE_REREAD.finditer(text):
            line = text.count("\n", 0, found.start()) + 1
            claimed.append(f"{path.relative_to(REPO).as_posix()}:{line}")
    if reaches:
        assert claimed, (
            f"{', '.join(reaches)} now reads the raw store on the path a parked job resumes "
            "through, and no criterion says so. Decision 424 narrowed §12's M5.3 row to what the "
            "resume could be measured doing -- no request, no client, no duplicated row -- "
            "because the read was not among them. If M5.6's retry-from-stage-N has landed "
            "(decision 330), the clause belongs back in the row and in docs/RELEASE.md."
        )
    else:
        assert not claimed, (
            "a criterion promises the retry re-reads the content-addressed raw store, and nothing "
            f"on the resume path can: {', '.join(claimed)}. The resume point is the board's own "
            "stage, `reviews_gate` asks `gate.measure` and nothing else, and the app's only "
            "`rawstore.read` is stage 3's, which the resume is past. Say what the retry does -- it "
            "opens no socket and builds no client -- or make the sentence true first."
        )
