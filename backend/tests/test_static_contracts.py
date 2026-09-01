"""Guards over things that are not Python and have no runtime to assert them.

The design language, the compose stack and the CPU-only constraint are all rules the spec
states and nothing enforces. Each of these reads the artifact and fails if the rule slipped.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DESIGN = REPO / "frontend" / "src" / "lib" / "design.css"
COMPOSE = REPO / "docker-compose.yml"
DOCKERFILE = REPO / "ops" / "backend.Dockerfile"

# §4.3 / §6.4: vocabulary v1's eleven facets. §6.4's binding note is explicit that the
# prototype's set is NOT this one — it invented dialogue/tone/setting/craft and lacked
# place/era/sensibility/register — and that "everything facet-shaped binds to the real 11".
VOCAB_V1_FACETS = {
    "mood", "themes", "pacing", "structure", "visual", "sound",
    "character", "place", "era", "sensibility", "register",
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


# --- §1, §2: the stack -----------------------------------------------------------------


def test_the_app_publishes_one_plain_http_port_and_terminates_no_tls():
    """§2: "the app itself serves plain HTTP on one internal port; the operator's existing
    Traefik + Cloudflare terminates TLS"."""
    compose = COMPOSE.read_text(encoding="utf-8")
    published = re.findall(r'^\s*-\s*"\$\{BIND_ADDR[^"]*"', compose, re.M)
    assert len(published) == 1, f"expected exactly one published app port, found {published}"
    assert "443" not in compose
    for tls in ("letsencrypt", "certresolver", "ssl_certificate", "traefik.http.routers"):
        assert tls not in compose.lower(), f"the app must not configure TLS itself ({tls})"


def test_every_data_volume_the_spec_names_is_mounted():
    """§1 names /data/pg, /data/raw, /data/artifacts, /data/cache; §2's nightly pg_dump adds
    /data/backups. A missing mount is data that does not survive a container replacement."""
    compose = COMPOSE.read_text(encoding="utf-8")
    for path in ("/var/lib/postgresql/data", "/data/raw", "/data/artifacts", "/data/cache",
                 "/data/backups"):
        assert path in compose, f"{path} is not mounted"


def test_postgres_is_pinned_to_16():
    """§1: Postgres 16. The schema is written against it and PGlite only approximates it."""
    assert re.search(r"image:\s*postgres:16\b", COMPOSE.read_text(encoding="utf-8"))


def test_the_image_pulls_torch_from_the_cpu_index_only():
    """§1's hard constraint: "every in-app model update runs on CPU… The image must build and
    run on a GPU-less VM." A CUDA wheel is 2 GB of accident."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "download.pytorch.org/whl/cpu" in dockerfile
    for cuda in ("cu118", "cu121", "cu124", "nvidia", "--gpus"):
        assert cuda not in dockerfile.lower(), f"the image must stay CPU-only ({cuda})"


def test_the_worker_does_not_inherit_the_backend_http_healthcheck():
    """They share an image; the worker serves no HTTP, so the inherited check can only fail,
    and a permanently unhealthy service teaches the operator to ignore the column."""
    compose = COMPOSE.read_text(encoding="utf-8")
    worker = compose[compose.index("  worker:") :]
    assert "healthcheck:" in worker and "disable: true" in worker


def test_the_build_context_excludes_host_state_and_secrets():
    """`context: .` ships whatever is not ignored — including .env and three node_modules
    trees, and a host node_modules copied over the one installed in the image."""
    ignore = (REPO / ".dockerignore").read_text(encoding="utf-8")
    for entry in (".env", "data/", "**/node_modules", "**/.venv"):
        assert entry in ignore, f"{entry} must not enter the build context"


@pytest.mark.parametrize("required", ["PUBLIC_URL", "SESSION_SECRET", "SECRETS_KEY"])
def test_the_stack_refuses_to_start_without_its_required_config(required):
    """§2 makes these three required, and `${VAR:?}` is what makes that true rather than
    aspirational — a missing SECRETS_KEY must stop the stack, not silently weaken it."""
    compose = COMPOSE.read_text(encoding="utf-8")
    assert re.search(rf"\$\{{{required}:\?", compose), f"{required} is not a required variable"


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


# --- §1: the image installs from pyproject, so imports must be declared there -------------


def test_every_third_party_import_is_a_declared_dependency():
    """The container installs from `pyproject.toml` and nothing else.

    A package that is in a developer's virtualenv but not in that file builds a perfectly
    healthy image which then dies on its first import — the failure is at *runtime*, in the
    container, after everything green. This walks the actual imports instead.
    """
    import ast
    import sys
    from importlib.metadata import packages_distributions

    pyproject = (REPO / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    declared = {
        re.split(r"[><=\[!~ ]", line.strip().strip('",'))[0].lower().replace("_", "-")
        for line in pyproject.splitlines()
        if line.strip().startswith('"') and "=" not in line.split('"')[0]
    }

    modules: set[str] = set()
    for path in (REPO / "backend" / "spielplan").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])

    third_party = modules - set(sys.stdlib_module_names) - {"spielplan", "__future__"}
    dist_of = packages_distributions()
    undeclared = []
    for module in sorted(third_party):
        dists = {d.lower().replace("_", "-") for d in dist_of.get(module, [])}
        if not dists:
            continue  # not installed here; the import would already have failed elsewhere
        if not (dists & declared):
            undeclared.append(f"{module} (provided by {sorted(dists)})")
    assert not undeclared, f"imported but not in pyproject dependencies: {undeclared}"
