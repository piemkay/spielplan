"""Guards over things that are not Python and have no runtime to assert them.

The design language, the compose stack and the CPU-only constraint are all rules the spec
states and nothing enforces. Each of these reads the artifact and fails if the rule slipped.
"""

from __future__ import annotations

import ast
import dataclasses
import os
import re
import shutil
import subprocess
import sys
import tempfile
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


def _seed_fields() -> list[str]:
    from spielplan.core.config import Settings

    return [
        name.upper()
        for name in Settings.model_fields
        if name.startswith(("jellyfin_", "tmdb_", "omdb_", "trakt_"))
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
    stripped = _compose().replace("    stop_grace_period: 5m\n", "")
    assert _without_grace_period(stripped) == ["worker"]


def test_the_seed_guard_sees_a_variable_that_stopped_being_forwarded():
    dropped = _compose().replace("  TMDB_API_KEY: ${TMDB_API_KEY:-}\n", "")
    assert _unforwarded_seeds(dropped) == ["TMDB_API_KEY"]


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
        if name.startswith(("jellyfin_", "tmdb_", "omdb_", "trakt_"))
    ]
    assert seeds, "Settings should carry the connector seed fields"
    missing = [name for name in seeds if f"{name}=" not in example]
    assert not missing, f".env.example does not document: {missing}"


def test_env_example_marks_the_seed_variables_optional():
    """They are commented out on purpose: an uncommented empty `JELLYFIN_URL=` is not the same
    as absent, and "configured empty" is a state §2 never wants."""
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        if line.startswith(("JELLYFIN_", "TMDB_", "OMDB_", "TRAKT_")):
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


def _self_mounted_routers(root: Path) -> list[str]:
    """Test files that mount a router themselves."""
    offenders = []
    for path in sorted(root.rglob("test_*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^\s*[\w.]+\.include_router\(", line):
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


# --- §12: the seven exit scripts, and the console they print to ---------------------------
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

EXIT_SCRIPTS = tuple(sorted((REPO / "ops").glob("m*_exit_criterion.py")))
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
    assert len(EXIT_SCRIPTS) == 7, EXIT_SCRIPTS
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


def _fixed_predicate(node: ast.expr) -> str | None:
    """Why this predicate's answer is settled before the run, or None if it can vary.

    Rejected: a truthy literal, which records a PASS whatever happened, and a boolean
    expression with an operand that pins the result (`x or True`, `x and False`) -- a predicate
    that was written to vary and no longer does.

    Allowed: a falsy literal. `ops/m45_exit_criterion.py`'s block loop reports one per block
    that trains on counts and arrives as a presence bit; the surrounding `if` has already
    established that failure and the literal is only how it gets recorded. A deliberate FAIL is
    a report, an unconditional PASS is a certificate, and only the second one is a lie.
    """
    if isinstance(node, ast.Constant):
        return f"the literal {node.value!r}" if node.value else None
    if isinstance(node, ast.BoolOp):
        pin = isinstance(node.op, ast.Or)
        for operand in node.values:
            if isinstance(operand, ast.Constant) and bool(operand.value) is pin:
                joiner = "or" if pin else "and"
                return f"`{joiner} {operand.value!r}` pins the answer to {pin}"
    return None


def _constant_check_predicates(source: str, label: str) -> list[str]:
    """Read with `ast`, not a regex: a predicate wrapped across three lines is the same defect."""
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "check" or not node.args:
            continue
        fixed = _fixed_predicate(node.args[0])
        if fixed:
            out.append(f"{label}:{node.lineno}: check({ast.unparse(node.args[0])}) -- {fixed}")
    return out


def test_no_milestone_exit_check_has_a_constant_predicate():
    """Two of M4.5's eighteen checks could not fail, and both counted toward its published score.

    One stood in for §12's M2 exit criterion -- the query
    `backend/migrations/0008_placement.sql:63-64` builds a partial index for -- widened with a
    literal so it always passed; the other recorded a summary of the loop above it as a pass.
    The number behind the first was genuinely 0 on v20260828, so nothing was concealed on the
    day it was written; what was lost was the ability to notice the day it stops being 0.
    """
    assert len(EXIT_SCRIPTS) == 7, EXIT_SCRIPTS
    offenders = [
        line
        for path in EXIT_SCRIPTS
        for line in _constant_check_predicates(path.read_text(encoding="utf-8"), path.name)
    ]
    assert not offenders, (
        "a milestone exit check reports a verdict it settled before the run:\n  "
        + "\n  ".join(offenders)
    )


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
    ],
)
def test_the_constant_predicate_guard_catches_a_real_violation(name, source, expected):
    assert len(_constant_check_predicates(source + "\n", "probe.py")) == expected, name


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
    assert len(EXIT_SCRIPTS) == 7, EXIT_SCRIPTS
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
    assert len(EXIT_SCRIPTS) == 7, EXIT_SCRIPTS
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
    assert len(EXIT_SCRIPTS) == 7, EXIT_SCRIPTS
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
# one named person's credentials. These are the three names that custody is spelled as in those
# packages. `base_url` and `url` stay printable on purpose: the server a line is about is what makes
# the line useful, and hiding it would buy nothing (§7.1 ships it to the browser as a deep link).
SECRET_FIELD_NAMES = frozenset({"api_key", "token", "user_tokens"})


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


def _lane_files() -> list[str]:
    """Every file in the tree that names one of the lane's two symbols."""
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
        if path != Path(__file__).resolve()
        and any(marker in path.read_text(encoding="utf-8") for marker in LANE_MARKERS)
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
    the way `test_every_published_decision_range_ends_where_the_register_does` re-derives a range
    from the register. Rounding is read off the figure's own precision: a record that publishes two
    decimals is held to two, and one that publishes one is held to one.
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
# axis each one was short on. That block names `.pill, .btn-primary, .btn-ghost, button, select,
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
    [§6 preamble; row `platform-every-touch-target-meets-the-token`; M4.15 review cycle 1]
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
