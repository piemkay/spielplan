"""Guards over things that are not Python and have no runtime to assert them.

The design language, the compose stack and the CPU-only constraint are all rules the spec
states and nothing enforces. Each of these reads the artifact and fails if the rule slipped.
"""

from __future__ import annotations

import ast
import re
import shutil
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
    facet-mood's colour, which is deliberate and the only exception."""
    css = _css()
    assert "--ember: #c8613a;" in css
    ember_uses = re.findall(r"--([a-z-]+):\s*#c8613a", css)
    assert set(ember_uses) <= {"ember", "facet-mood"}, ember_uses


def test_the_data_voice_is_monospace_and_the_display_face_is_not():
    """§6.8: JetBrains Mono for every model number, ID and data annotation; Space Grotesk for
    everything else. A data voice that is not visually distinct is not a voice."""
    css = _css()
    assert "--mono: 'JetBrains Mono'" in css
    assert "--display: 'Space Grotesk'" in css
    data_rule = re.search(r"\.data\s*\{([^}]+)\}", css)
    assert data_rule and "var(--mono)" in data_rule.group(1)


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
    static = re.search(r'class="([^"]*)"', attrs)
    names = set(static.group(1).split()) if static else set()
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
    """
    poster = _src(POSTER_CARD)
    derived = re.search(r"noCrowdData\s*=\s*\$derived\((?P<expr>.*?)\n\s*\);", poster, re.S)
    assert derived, "PosterCard no longer derives noCrowdData"
    assert " ".join(derived.group("expr").split()) == _COLD_BADGE, (
        "the no-crowd-data badge is off `e_source`/`item_n`, NOT off `title.placement`:\n"
        f"  found:  {' '.join(derived.group('expr').split())}\n  wanted: {_COLD_BADGE}"
    )
    assert "Off `e_source`/`item_n`, NOT off `title.placement`." in poster, (
        "the comment stating the rule is the specification and travels with the expression"
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


# --- §12: the four exit scripts, and the console they print to ----------------------------
#
# §12's M2, M3, M4 and M4.9 rows are measured by hand, by `ops/m*_exit_criterion.py`, and a
# milestone is closed on what they print and the code they exit with. A verdict that cannot
# come out `no` is a certificate rather than a measurement, so these read the scripts as
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
    assert len(EXIT_SCRIPTS) == 4, EXIT_SCRIPTS
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
    assert len(EXIT_SCRIPTS) == 4, EXIT_SCRIPTS
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
    assert len(EXIT_SCRIPTS) == 4, EXIT_SCRIPTS
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
    assert len(EXIT_SCRIPTS) == 4, EXIT_SCRIPTS
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


def _rendered_markup(path: Path) -> str:
    """A Svelte file's template: script block, style block and HTML comments removed.

    Read this way because a sweep over the whole file passes on markup that has been commented
    out -- the failure a compose guard in this repository already shipped once, green against a
    file of nothing but comments (M4.7 ddocs-08). The claim below is that the card RENDERS these
    fields, so what is searched has to be what renders.
    """
    body = re.sub(r"<script\b.*?</script>", "", path.read_text(encoding="utf-8"), flags=re.S)
    body = re.sub(r"<style\b.*?</style>", "", body, flags=re.S)
    return re.sub(r"<!--.*?-->", "", body, flags=re.S)


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
    assert len(EXIT_SCRIPTS) == 4, EXIT_SCRIPTS
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
