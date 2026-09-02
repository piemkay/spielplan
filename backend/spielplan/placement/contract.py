"""`feature_contract.json`, parsed. Spec v2.1 §4.3, §8 stage 9.

§4.3: "`feature_contract.json` — the **exhaustive** definition of the tower's input: the nine
content blocks in order with sizes (dna_x 433, dna_p 556, genome 983, genre 179, keyword 3,884,
credit 244, country 97, award 2, meta 57 = 6,435 columns), per-column `feature_names`, then the
review-text block = columns 0..63 of the 256-d SVD embedding (singular-value order) multiplied
by a frozen scalar `text_scale` … The contract *references* the review-text SVD components …
and records all placement-time preprocessing: genome zero-imputation, text truncation +
scaling. §8 stage 9 builds vectors from this file and nothing else."

"and nothing else" is the whole design of this module. The corpus widths quoted above appear in
that sentence and in this docstring — and nowhere in the code. Every offset is a cumulative sum
of the sizes the loaded file declares, so a contract whose keyword block shrinks by one column
moves every later column by one and there is no constant table to disagree with it.

Two asymmetries §4.3 states and this module keeps apart, because they are identical bytes in
the vector and different facts about the title:

  * **genome is zero-imputed** — "genome block zero-imputed (unavailable for new titles by
    construction)" (§8 stage 9). The block is *there* and it is zeros.
  * **absent blocks are dropped** — "absent blocks dropped — the tower's dropout training
    anticipates this" (§5.3). Nothing is filled with a mean, a prior or a global average, ever;
    a dropped block is zeros too, and it is recorded as dropped so §5.3's badge and the §8.4
    flywheel can tell the two apart.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ContractError(ValueError):
    """The file cannot define a tower input.

    Loud rather than defaulted: §8 stage 9 builds from this file *and nothing else*, so a
    missing width or a missing `text_scale` has no safe fallback — inventing one produces a
    plausible vector, a plausible coordinate, and silently worse placements forever.
    """


# §4.3's nine, in §4.3's order. These are names, not sizes: they key the per-block producers in
# `features.py`. A contract that declares a different set is honoured as written and the
# difference is reported (`unproducible_blocks` / `undeclared_blocks`) — the corpus export is
# the authority on its own column set, exactly as §4.1's shape note says of the content spine.
BLOCK_NAMES: tuple[str, ...] = (
    "dna_x", "dna_p", "genome", "genre", "keyword", "credit", "country", "award", "meta",
)

# The tenth block is not a content block: §4.3 appends it after the nine, out of a different
# file, under a scale frozen at export time.
TEXT_BLOCK = "review_text"

ENCODINGS = ("multi_hot", "weighted", "scalar")
NORMALISERS = ("none", "l2", "sum1", "max1")

# §4.3 fixes the column *names* per block but not how a cell is filled, so the choice is made
# once, here, per block, and a contract may override it per block. `multi_hot` writes 1.0 for a
# key that is present; `weighted` and `scalar` write the value the database gave.
DEFAULT_ENCODING: dict[str, str] = {
    # Both DNA tiers are PRESENCE, not strength. §4.3 makes the corpus's export the exhaustive
    # definition of the tower's input, and the exporter (`scripts/build_content.py`) builds both
    # tiers with `build("dna_x", ...)` / `build("dna_p", ...)` and no `weighted=True`, so every
    # cell it wrote is 1.0; its `dna_tag` / `dna_projected` are keyed PRIMARY KEY (title_id,
    # term), so nothing sums past it either. Measured in the shipped `content_X.npz`: 29,749 +
    # 216,212 nonzeros, min 1.0 and max 1.0 in both blocks. §4.1 rule 2 keeps salience and the
    # projected weight out of the *predicate*; it does not put them in the cell, and writing
    # them here fed the checkpoint a 1..3 scale it was never trained on.
    "dna_x": "multi_hot",
    "dna_p": "multi_hot",
    "genome": "weighted",     # MovieLens relevance, in [0.5, 1] after the corpus's own cut
    # These four are COUNTS, not presence. Measured in the shipped `content_X.npz`, the share of
    # nonzeros above 1.0 is genre 37.7% (max 6), keyword 21.4% (max 7), credit 64.7% (max 5),
    # country 66.2% (max 3): the exporter sums duplicate (title, feature) pairs and all four
    # tables carry a `source`, so a genre four sources agreed on is a 4.0. Encoding them
    # multi_hot fed the checkpoint a distribution it was never trained on in 4,404 of 6,435
    # columns — the keys hit and the values were still wrong.
    "genre": "weighted",
    "keyword": "weighted",
    "credit": "weighted",
    "country": "weighted",
    "award": "scalar",        # two count columns
    "meta": "scalar",         # flags and normalised scalars, produced by the grammar below
}

# §4.3 says nothing about per-block normalisation, so the app does none and says so: a norm the
# corpus applied at training time and the app does not is exactly the silent encoder drift that
# no unit test can see. A contract that declares one is honoured.
DEFAULT_NORMALISE = "none"

# §4.3: "records all placement-time preprocessing: genome zero-imputation". Only this block.
ZERO_IMPUTED = ("genome",)

# The meta block is the one block whose columns are produced by code rather than read from a
# table, so its column names must come from a closed grammar or the app cannot fill them. A
# declared name outside the grammar is a column that would silently stay zero for every title,
# which is a defaulted block by another route — reported by `unproducible_meta_names`.
META_PRODUCTIONS: tuple[str, ...] = (
    r"kind:(movie|series)",
    r"decade:\d{3}0",
    # The corpus's runtime bins, verbatim -- `mdc/ratings/features.py:89`. Declared here as a
    # closed alternation rather than a pattern so a bin the app cannot produce is reported
    # rather than silently left at zero.
    r"runtime:(<80|80-105|105-130|130-160|>160)",
    r"lang:[a-z]{2,3}",
    r"has:(overview|tagline|trailer|poster|imdb_id|genome|award|review|keyword|credit)",
    r"year_norm",
    r"runtime_norm",
    r"n_(credits|genres|keywords|countries|reviews|dna_x|dna_p|aliases|companies|awards)_log",
)
_META_GRAMMAR = re.compile("^(?:" + "|".join(META_PRODUCTIONS) + ")$")

# The three continuous meta productions need constants that must match training-time
# preprocessing. §4.3 puts placement-time preprocessing in the contract, so the block's
# `transform` map overrides these; these are what the app uses when the exporter ships none,
# and `FeatureContract.transforms_defaulted` records that it had to.
META_TRANSFORM_DEFAULTS: dict[str, dict[str, float]] = {
    # 1900..2025 mapped into [0, 1]; film history, not an arbitrary window.
    "year_norm": {"offset": 1900.0, "scale": 125.0},
    # 400 minutes is past every plausible runtime, so the clip is a guard and not a squash.
    "runtime_norm": {"offset": 0.0, "scale": 400.0},
    # log1p(count)/5: log1p(148) ≈ 5, and 148 credits is a big cast.
    "count_log": {"offset": 0.0, "scale": 5.0},
}


@dataclass(frozen=True)
class Block:
    """One content block: a contiguous run of columns with a name per column."""

    name: str
    size: int
    offset: int
    encoding: str
    normalise: str
    impute: str                                    # 'zero' (genome) | 'none' (everything else)
    names: tuple[str, ...]
    index: Mapping[str, int] = field(repr=False)
    transform: Mapping[str, Mapping[str, float]] = field(default_factory=dict, repr=False)

    @property
    def stop(self) -> int:
        return self.offset + self.size

    def column(self, key: str) -> int | None:
        """The absolute column for a feature key, or None if this contract does not declare it."""
        local = self.index.get(key)
        return None if local is None else self.offset + local


@dataclass(frozen=True)
class FeatureContract:
    blocks: tuple[Block, ...]
    content_width: int
    text_offset: int
    text_dims: int          # the SVD's full width (§4.3: 256)
    text_used: int          # the columns the tower is fed (§4.3: 0..63)
    text_scale: float
    input_dim: int
    sha256: str
    notes: tuple[str, ...] = ()

    @property
    def block_names(self) -> tuple[str, ...]:
        return tuple(b.name for b in self.blocks)

    def block(self, name: str) -> Block:
        for b in self.blocks:
            if b.name == name:
                return b
        raise ContractError(f"contract declares no block {name!r}")

    def has(self, name: str) -> bool:
        return any(b.name == name for b in self.blocks)

    @property
    def undeclared_blocks(self) -> tuple[str, ...]:
        """§4.3's nine that this contract leaves out — the tower is simply not fed them."""
        return tuple(n for n in BLOCK_NAMES if not self.has(n))

    @property
    def transforms_defaulted(self) -> tuple[str, ...]:
        """Continuous meta productions the contract shipped no constants for."""
        if not self.has("meta"):
            return ()
        shipped = self.block("meta").transform
        return tuple(k for k in META_TRANSFORM_DEFAULTS if k not in shipped)

    def meta_transform(self, key: str) -> dict[str, float]:
        shipped = self.block("meta").transform.get(key) if self.has("meta") else None
        return dict(shipped) if shipped else dict(META_TRANSFORM_DEFAULTS[key])

    def summary(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "input_dim": self.input_dim,
            "content_width": self.content_width,
            "text": {"offset": self.text_offset, "dims": self.text_dims,
                     "used": self.text_used, "scale": self.text_scale},
            "blocks": [
                {"name": b.name, "size": b.size, "offset": b.offset,
                 "encoding": b.encoding, "normalise": b.normalise, "impute": b.impute}
                for b in self.blocks
            ],
            "undeclared_blocks": list(self.undeclared_blocks),
            "unproducible_meta_names": unproducible_meta_names(self)[:8],
            "transforms_defaulted": list(self.transforms_defaulted),
            "notes": list(self.notes),
        }

    # --- loading ------------------------------------------------------------------------

    @classmethod
    def load(cls, raw: Mapping[str, Any], *, sha256: str = "") -> FeatureContract:
        """Parse a contract document. Every width and every offset comes from `raw`."""
        blocks_raw = raw.get("content_blocks")
        if not blocks_raw:
            raise ContractError(
                "feature_contract.json declares no `content_blocks` — §4.3 makes this file the "
                "exhaustive definition of the tower's input, and the block list is the whole of it"
            )

        notes: list[str] = []
        declared = _declared_blocks(raw, blocks_raw)

        # §4.3 records placement-time preprocessing in the contract, and the corpus writes it as
        # prose rather than as an enum. The guard is on the substance — zero-imputation for
        # genome, dropped-not-defaulted for absent blocks — because those are the two rules the
        # app implements and cannot silently diverge from.
        pre = raw.get("preprocessing") or {}
        imputation = str(pre.get("genome", ""))
        if "zero-imput" not in imputation:
            raise ContractError(
                f"contract records genome preprocessing as {imputation!r}; §4.3 records it as "
                "zero-imputation and the app implements no other"
            )
        absent = str(pre.get("absent_blocks", ""))
        if "dropped" not in absent:
            raise ContractError(
                f"contract records absent_blocks as {absent!r}; §5.3 drops them — 'the tower's "
                "dropout training anticipates this' — and defaulting them is what it forbids"
            )

        blocks: list[Block] = []
        offset = 0
        for name, spec in declared:
            size = spec.get("size")
            if not isinstance(size, int) or size < 0:
                raise ContractError(f"block {name!r} has no usable `size` ({size!r})")
            names = tuple(str(n) for n in spec.get("feature_names") or ())
            if len(names) != size:
                raise ContractError(
                    f"block {name!r} declares size {size} but {len(names)} feature_names; "
                    "§4.3 makes the per-column names part of the exhaustive definition"
                )
            index = {n: i for i, n in enumerate(names)}
            if len(index) != len(names):
                dupes = sorted({n for n in names if names.count(n) > 1})[:5]
                raise ContractError(f"block {name!r} repeats feature_names {dupes}")
            encoding = str(spec.get("encoding") or DEFAULT_ENCODING.get(name, "multi_hot"))
            if encoding not in ENCODINGS:
                raise ContractError(f"block {name!r} declares unknown encoding {encoding!r}")
            normalise = str(spec.get("normalise") or DEFAULT_NORMALISE)
            if normalise not in NORMALISERS:
                raise ContractError(f"block {name!r} declares unknown normalise {normalise!r}")
            if spec.get("encoding") is None and name in DEFAULT_ENCODING:
                notes.append(f"block {name}: encoding defaulted to {encoding}")
            blocks.append(
                Block(
                    name=name, size=size, offset=offset, encoding=encoding,
                    normalise=normalise, impute="zero" if name in ZERO_IMPUTED else "none",
                    names=names, index=index,
                    transform=dict(spec.get("transform") or {}),
                )
            )
            offset += size

        content_width = offset
        # §4.3's tenth block, out of a different file. The corpus writes it as `text_block`, and
        # puts `text_scale` INSIDE it — reading the top level found nothing and every real
        # contract was refused for shipping no scale.
        text = raw.get("text_block") or {}
        if not text:
            raise ContractError(
                "contract declares no `text_block`; §4.3 appends the review-text columns after "
                "the nine content blocks and the app cannot guess their width or their scale"
            )
        # `columns` is a closed range like "0..63". It and `dim` are two statements of the same
        # fact, so disagreement between them is a contract error rather than a preference.
        span = _column_span(text.get("columns"))
        text_used = int(text.get("dim", span or 0)) or span
        if span and text_used != span:
            raise ContractError(
                f"text_block says dim={text_used} and columns={text.get('columns')!r}, which "
                f"spans {span}; §4.3's truncation is one fact and the file states it twice"
            )
        # §4.3: "columns 0..63 of the 256-d SVD embedding". The full width lives with the
        # embedding, not here, so it is recorded rather than re-derived.
        text_dims = int(text.get("svd_dims", 256))
        if not 0 < text_used <= text_dims:
            raise ContractError(
                f"text_block uses {text_used} of {text_dims} SVD columns — §4.3 takes "
                "columns 0..63 of the 256-d embedding"
            )
        order = str(text.get("order", "singular-value"))
        if not order.startswith("singular-value"):
            raise ContractError(
                f"text_block order is {order!r}; §4.3 takes the first columns in "
                "singular-value order, which is only a truncation if the order holds"
            )
        if "text_scale" not in text:
            raise ContractError(
                "text_block ships no `text_scale`; §4.3 freezes it at export time so placements "
                "stay comparable across runs, and a default would silently move every coordinate"
            )
        text_scale = float(text["text_scale"])

        omitted = [n for n in BLOCK_NAMES if n not in {b.name for b in blocks}]
        if omitted:
            notes.append("contract omits §4.3 block(s) " + ", ".join(omitted))
        extra = [b.name for b in blocks if b.name not in BLOCK_NAMES]
        if extra:
            notes.append("contract declares block(s) §4.3 does not name: " + ", ".join(extra))

        return cls(
            blocks=tuple(blocks),
            content_width=content_width,
            text_offset=content_width,
            text_dims=text_dims,
            text_used=text_used,
            text_scale=text_scale,
            input_dim=content_width + text_used,
            sha256=sha256,
            notes=tuple(notes),
        )

    @classmethod
    def load_path(cls, path: Path) -> FeatureContract:
        """Load from disk, carrying the file's sha256 — §8 stage 9's input has an identity, and
        a placement built under a different contract is stale by construction."""
        blob = path.read_bytes()
        try:
            raw = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError(f"{path.name} is not readable JSON: {exc}") from exc
        return cls.load(raw, sha256=hashlib.sha256(blob).hexdigest())

    @classmethod
    def from_store(cls, store: Any) -> FeatureContract:
        """Load the active bundle's contract. Raises ContractError when the bundle ships none —
        §3.1 makes an empty store legal, so callers ask and report rather than assume."""
        if getattr(store, "is_empty", True):
            raise ContractError("no artifact bundle is loaded, so there is no feature contract")
        if not store.present.get("feature_contract.json"):
            raise ContractError(
                f"bundle {store.version} ships no feature_contract.json — §8 stage 9 has no "
                "input definition, so nothing can be placed"
            )
        return cls.load_path(store.path("feature_contract.json"))


def _column_span(columns: Any) -> int:
    """`"0..63"` -> 64. Zero when the contract states no range."""
    if not isinstance(columns, str) or ".." not in columns:
        return 0
    lo, _, hi = columns.partition("..")
    try:
        return int(hi) - int(lo) + 1
    except ValueError as exc:
        raise ContractError(f"text_block columns {columns!r} is not a range") from exc


def _declared_blocks(
    raw: Mapping[str, Any], blocks_raw: Any
) -> list[tuple[str, dict[str, Any]]]:
    """`content_blocks` reduced to (name, spec) pairs **in declared order**.

    The corpus ships one flat `feature_names` list of every column in the whole contract, and
    `content_blocks` as `[{name, size}, ...]`. The names belong to the blocks by POSITION: the
    first `size` names are the first block's, and so on. Order is therefore load-bearing twice
    over -- §4.3's "nine content blocks *in order*" fixes the offsets, and the same order slices
    the names -- so a size that does not add up is a contract error rather than a short block.
    """
    if not isinstance(blocks_raw, list):
        raise ContractError(
            f"`content_blocks` is a {type(blocks_raw).__name__}, not a list"
        )

    flat = raw.get("feature_names")
    if not isinstance(flat, list):
        raise ContractError(
            "`feature_names` is not a flat list of column names; §4.3 makes the per-column "
            "names part of the exhaustive definition and the app slices them by block size"
        )

    encodings = raw.get("encodings") or {}
    normalisers = raw.get("normalise") or {}
    transforms = raw.get("transforms") or {}

    out: list[tuple[str, dict[str, Any]]] = []
    cursor = 0
    for spec in blocks_raw:
        name = str(spec.get("name") or "")
        if not name:
            raise ContractError("a block in `content_blocks` has no `name`")
        size = spec.get("size")
        if not isinstance(size, int) or size < 0:
            raise ContractError(f"block {name!r} has no usable `size` ({size!r})")
        if cursor + size > len(flat):
            raise ContractError(
                f"block {name!r} runs past the end of feature_names: it needs columns "
                f"{cursor}..{cursor + size - 1} of {len(flat)}"
            )
        out.append((
            name,
            {
                "size": size,
                "feature_names": [str(n) for n in flat[cursor:cursor + size]],
                "encoding": spec.get("encoding", encodings.get(name)),
                "normalise": spec.get("normalise", normalisers.get(name)),
                "transform": spec.get("transform", transforms.get(name)),
            },
        ))
        cursor += size

    if cursor != len(flat):
        raise ContractError(
            f"content_blocks account for {cursor} columns but feature_names has {len(flat)}; "
            "§4.3's definition is exhaustive, so the remainder is a column nothing declares"
        )
    declared_dim = raw.get("content_dim")
    if isinstance(declared_dim, int) and declared_dim != cursor:
        raise ContractError(
            f"contract says content_dim={declared_dim} and its blocks sum to {cursor}"
        )
    return out


def unproducible_meta_names(contract: FeatureContract) -> list[str]:
    """Declared meta columns no production in the grammar can fill.

    Not an exception: the corpus export is the authority on its own column names, and a name
    this app cannot produce costs one always-zero column rather than a wrong vector. It is
    reported so the gap is named instead of being invisible.
    """
    if not contract.has("meta"):
        return []
    return [n for n in contract.block("meta").names if not _META_GRAMMAR.match(n)]
