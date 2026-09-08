"""Guards over things that are not Python and have no runtime to assert them.

The design language, the compose stack and the CPU-only constraint are all rules the spec
states and nothing enforces. Each of these reads the artifact and fails if the rule slipped.
"""

from __future__ import annotations

import re
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
