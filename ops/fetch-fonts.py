"""Regenerate the self-hosted webfont subsets under `frontend/static/fonts/`.

`fonts.css:4` has told its reader to "Regenerate with ops/fetch-fonts.py" since M0, and no such
script has ever existed -- so the one file in the app that claims to be machine-generated has
only ever been hand-edited, and the hand-edit was visible in it: every `@font-face` block carried
a blank line where the `/* latin */` comment it had been pasted from used to be. This is that
script. [M4.15 finding 12]

**Self-hosting is the constraint, not a preference.** §6's preamble makes Spielplan an installable
PWA a household opens over the LAN and over Tailscale, and `app.html:13-15` says the same thing
over its own `<link>`: "the app has to render on the LAN and over Tailscale with no route to the
internet, and a blocking stylesheet from a third-party host would make the shell wait on something
it may not be able to reach." That is the whole reason this is a script and not a `<link>` to
fonts.googleapis.com, and it has one consequence worth stating plainly: a weight that cannot be
fetched at *runtime* cannot be declared at *build time*. Every face this writes is backed by a
file in the repository, and `test_every_declared_font_face_ships_its_own_file` holds it there.

**Both families are variable fonts, and that is why several weights share one file.** Google's
css2 endpoint answers a modern browser with one woff2 per (family, subset) carrying a `wght` axis
-- Space Grotesk 300-700, JetBrains Mono 400-800 -- and emits a separate `@font-face` per weight
asked for, all pointing at it. Those heavier faces are real instances of the axis, not a
synthesised smear of the regular. M4.15's finding 12 read `-400-` in the file names and concluded
the opposite, and both repairs it proposed would have made the app worse: deleting the faces hands
bold back to the browser's synthesiser, and copying one file under three names makes
`service-worker.js`'s all-or-nothing `addAll` push the same bytes to the phone three times before
the shell will open offline. So the convention `<family>-<weight>-<subset>.woff2` records the
*base* weight of the faces a file serves, not the only weight it can render. If Google ever goes
back to static per-weight files the URLs stop coinciding, and this writes one file per weight with
its own weight in its name, with no edit here.

It is a one-shot maintenance script and deliberately not wired into anything -- no npm script, no
Dockerfile step, no CI job. The build must never reach for the network, which is the same rule the
app itself is held to. What keeps the output honest between runs is the pair of guards in
`backend/tests/test_static_contracts.py`: one re-renders the stylesheet from `render_css()` below
and fails on any difference, the other checks every `src` against the files on disk.

Usage:

    python ops/fetch-fonts.py

Rewrites `frontend/static/fonts/*.woff2` and `frontend/static/fonts/fonts.css`. Both are
committed. [M4.15 step 8, decision 268]
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FONTS = REPO / "frontend" / "static" / "fonts"
STYLESHEET = FONTS / "fonts.css"

# Google's css2 endpoint answers by User-Agent: an agent it does not read as woff2-capable is sent
# ttf, and one it does not read as variable-capable is sent static per-weight files. The primary
# form factor is an iPhone 13 on WebKit, which is both, so ask as a current desktop browser --
# urllib's own UA collects the 2013 fallback, which is not what the phone would ever load.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# latin and latin-ext only. §6.8's two faces carry English copy, film titles and model ids; the
# greek, cyrillic and vietnamese subsets Google also offers would be dead weight in a shell cache
# `service-worker.js` fills all-or-nothing before the app will open offline.
SUBSETS = ("latin-ext", "latin")

# The subset ranges Google publishes, held as constants so `render_css()` is a pure function of
# this module and the files on disk -- which is what lets a test re-render the stylesheet with no
# network. `fetch_faces()` asserts the live answer still matches, so a change on Google's side
# fails loudly here instead of quietly narrowing what the app can render.
UNICODE_RANGES = {
    "latin-ext": (
        "U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, "
        "U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, "
        "U+2113, U+2C60-2C7F, U+A720-A7FF"
    ),
    "latin": (
        "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, "
        "U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, "
        "U+FEFF, U+FFFD"
    ),
}


@dataclass(frozen=True)
class Family:
    """A face §6.8 names, the file-name slug it takes, and the weights the app spends."""

    name: str
    slug: str
    weights: tuple[int, ...]


# §6.8 gives the app two faces: Space Grotesk for display and body, JetBrains Mono for "every
# model number, ID and data annotation". The weights are the ones the components actually ask
# for. `design.css` and the component style blocks spend 500, 600 and 700; 600 is not declared
# because CSS font matching searches *upward* for a target above 500, so every `font-weight: 600`
# site resolves to the 700 face rather than falling back to the regular.
FAMILIES = (
    Family("Space Grotesk", "space-grotesk", (400, 500, 700)),
    Family("JetBrains Mono", "jetbrains-mono", (400, 500)),
)

HEADER = """\
/* Generated by ops/fetch-fonts.py. Do not hand-edit.
 *
 * Self-hosted, and that is load-bearing rather than a preference: §6's preamble makes this an
 * installable PWA a household opens over the LAN and over Tailscale, and app.html says the same
 * thing over its own <link> -- a blocking stylesheet from a third-party host would make the
 * shell wait on a host it may not be able to reach. Latin and latin-ext subsets only.
 *
 * Both families are VARIABLE fonts, which is why several weights share one file: Space Grotesk
 * carries a wght axis of 300-700 and JetBrains Mono one of 400-800, so the 500 and 700 faces
 * below are real instances of that axis and not a synthesised smear of the regular. Deleting
 * them is what would hand bold to the browser's synthesiser; copying the file under a name per
 * weight would make service-worker.js push the same bytes to the phone three times. The weight
 * in a file name is the base weight of the faces that file serves, not the only weight it can
 * render.
 *
 * Regenerate with ops/fetch-fonts.py. backend/tests/test_static_contracts.py re-renders this
 * file from that script on every run and fails on any difference.
 */
"""

FACE = """
/* {subset} */
@font-face {{
  font-family: '{name}';
  font-style: normal;
  font-weight: {weight};
  font-display: swap;
  src: url(/fonts/{file}) format('woff2');
  unicode-range: {ranges};
}}
"""

# WOFF2 spec, Table 1: the tags a table directory entry can name by its 5-bit index, in order.
# Index 63 means the four-byte tag follows inline instead.
WOFF2_KNOWN_TAGS = (
    "cmap", "head", "hhea", "hmtx", "maxp", "name", "OS/2", "post", "cvt ", "fpgm",
    "glyf", "loca", "prep", "CFF ", "VORG", "EBDT", "EBLC", "gasp", "hdmx", "kern",
    "LTSH", "PCLT", "VDMX", "vhea", "vmtx", "BASE", "GDEF", "GPOS", "GSUB", "EBSC",
    "JSTF", "MATH", "CBDT", "CBLC", "COLR", "CPAL", "SVG ", "sbix", "acnt", "avar",
    "bdat", "bloc", "bsln", "cvar", "fdsc", "feat", "fmtx", "fvar", "gvar", "hsty",
    "just", "lcar", "mort", "morx", "opbd", "prop", "trak", "Zapf", "Silf", "Glat",
    "Gloc", "Feat", "Sill",
)


def woff2_tables(data: bytes) -> list[str]:
    """The table tags a woff2 declares, read from its directory without decompressing it.

    woff2 brotli-compresses the table *data*; the directory naming the tables is in the clear.
    That is enough for the only question the stylesheet raises, and it means neither this script
    nor the guard that imports it needs a brotli decompressor the repo does not depend on: a file
    carrying `fvar` is a variable font, so the extra weights declared against it are instances of
    its axis rather than faces the browser would have to fake.
    """
    if data[:4] != b"wOF2":
        raise ValueError("not a woff2 file")
    count = int.from_bytes(data[12:14], "big")
    offset = 48
    tags: list[str] = []

    def base128() -> int:
        nonlocal offset
        value = 0
        while True:
            byte = data[offset]
            offset += 1
            value = (value << 7) | (byte & 0x7F)
            if not byte & 0x80:
                return value

    for _ in range(count):
        flags = data[offset]
        offset += 1
        index = flags & 0x3F
        if index == 63:
            tag = data[offset:offset + 4].decode("latin1")
            offset += 4
        else:
            tag = WOFF2_KNOWN_TAGS[index]
        base128()  # origLength
        # `glyf` and `loca` are the two tables woff2 can restructure, and for them transform
        # version 0 *is* the transform; every other table is transformed only at a non-zero
        # version. Either way a transformed entry carries a second length to step over.
        version = (flags >> 6) & 0x3
        transformed = version == 0 if tag in ("glyf", "loca") else version != 0
        if transformed:
            base128()
        tags.append(tag)
    return tags


def file_for(family: Family, weight: int, subset: str, fonts_dir: Path = FONTS) -> str:
    """The file a face is served by, decided from the repository rather than from a flag.

    A static family ships one file per weight and the name carries that weight. A variable family
    ships one file per subset, named for the base weight of the faces it serves, and the heavier
    faces instance its `wght` axis. Which world this is is a fact about the files on disk -- an
    `fvar` table -- so the stylesheet can be re-rendered offline and a guard needs no network.
    """
    own = f"{family.slug}-{weight}-{subset}.woff2"
    if (fonts_dir / own).is_file():
        return own
    base = f"{family.slug}-{min(family.weights)}-{subset}.woff2"
    path = fonts_dir / base
    if not path.is_file():
        raise FileNotFoundError(f"neither {own} nor {base} is in {fonts_dir}")
    if "fvar" not in woff2_tables(path.read_bytes()):
        raise ValueError(f"{base} carries no wght axis, so it cannot serve weight {weight}")
    return base


def render_css(families: tuple[Family, ...] = FAMILIES, fonts_dir: Path = FONTS) -> str:
    """The whole stylesheet, as a pure function of the families above and the files on disk."""
    blocks = [HEADER]
    for family in families:
        for weight in family.weights:
            for subset in SUBSETS:
                blocks.append(
                    FACE.format(
                        subset=subset,
                        name=family.name,
                        weight=weight,
                        file=file_for(family, weight, subset, fonts_dir),
                        ranges=UNICODE_RANGES[subset],
                    )
                )
    return "".join(blocks)


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _one(pattern: str, block: str, what: str) -> str:
    found = re.search(pattern, block)
    if found is None:
        raise SystemExit(f"a Google Fonts @font-face block declares no {what}: {block.strip()!r}")
    return found.group(1)


_BLOCK = re.compile(r"/\*\s*([a-z0-9-]+)\s*\*/\s*@font-face\s*\{(.*?)\}", re.S)


def fetch_faces(family: Family) -> dict[tuple[int, str], str]:
    """The woff2 URL Google serves for each (weight, subset) of a family.

    The request is itself the check that the declared weights are real. css2 answers a weight
    outside the family's `wght` axis with HTTP 400, so asking for all of them at once is how a
    face that could never render is caught here rather than in a browser nobody is watching --
    which matters because the axis bound lives inside the brotli stream, where the offline guard
    cannot reach it.
    """
    weights = ";".join(str(w) for w in family.weights)
    query = f"family={urllib.parse.quote_plus(family.name)}:wght@{weights}&display=swap"
    try:
        css = _get(f"https://fonts.googleapis.com/css2?{query}").decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"Google Fonts refused {family.name} at weights {weights} with HTTP {exc.code}. "
            f"A weight outside the family's wght axis is rejected, so check the axis before "
            f"declaring the face."
        ) from exc

    found: dict[tuple[int, str], str] = {}
    for subset, block in _BLOCK.findall(css):
        if subset not in SUBSETS:
            continue
        ranges = _one(r"unicode-range:\s*([^;]+);", block, "unicode-range").strip()
        if ranges != UNICODE_RANGES[subset]:
            raise SystemExit(
                f"Google's {subset} range for {family.name} is no longer the one this script "
                f"holds. Update UNICODE_RANGES and re-read what the new range drops.\n"
                f"  theirs: {ranges}\n  ours:   {UNICODE_RANGES[subset]}"
            )
        weight = int(_one(r"font-weight:\s*(\d+)", block, "font-weight"))
        found[(weight, subset)] = _one(r"src:\s*url\((\S+?)\)", block, "src")

    missing = [(w, s) for w in family.weights for s in SUBSETS if (w, s) not in found]
    if missing:
        raise SystemExit(f"Google Fonts served no {family.name} face for {missing}")
    return found


def main() -> int:
    downloads: dict[str, bytes] = {}
    for family in FAMILIES:
        faces = fetch_faces(family)
        # One name per distinct URL, carrying the base weight of the faces that share it. Sorted
        # by weight so `setdefault` keeps the lowest, which is the name app.html preloads and
        # service-worker.js precaches.
        names: dict[str, str] = {}
        for weight, subset in sorted(faces, key=lambda key: (key[1], key[0])):
            names.setdefault(faces[(weight, subset)], f"{family.slug}-{weight}-{subset}.woff2")
        for url, name in names.items():
            data = _get(url)
            if data[:4] != b"wOF2":
                raise SystemExit(f"{url} did not answer with a woff2 file")
            downloads[name] = data
        print(f"{family.name}: {len(faces)} faces over {len(names)} files")

    FONTS.mkdir(parents=True, exist_ok=True)
    for name, data in sorted(downloads.items()):
        (FONTS / name).write_bytes(data)
        kind = "variable" if "fvar" in woff2_tables(data) else "static"
        print(f"  wrote {name} ({len(data)} bytes, {kind})")

    # A generator that only ever appends leaves the last run's files behind, and
    # service-worker.js precaches this directory whole with an all-or-nothing addAll -- so a
    # subset no face declares any more is bytes every phone downloads before the shell opens.
    for path in sorted(FONTS.glob("*.woff2")):
        if path.name not in downloads:
            path.unlink()
            print(f"  removed {path.name}, which no face declares any more")

    STYLESHEET.write_text(render_css(), encoding="utf-8")
    print(f"  wrote {STYLESHEET.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
