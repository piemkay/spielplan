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
    "dna_x": "weighted",      # salience 1..3 — §4.1 rule 2: a weight, never a filter
    "dna_p": "weighted",      # the projected tier's per-term strength
    "genome": "weighted",     # MovieLens relevance ∈ [0, 1]
    "genre": "multi_hot",
    "keyword": "multi_hot",
    "credit": "multi_hot",
    "country": "multi_hot",
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
        blocks_raw = raw.get("blocks")
        if not blocks_raw:
            raise ContractError("feature_contract.json declares no `blocks`")

        notes: list[str] = []
        declared = _declared_blocks(raw, blocks_raw)

        # §4.3: "genome zero-imputation" is contract-recorded preprocessing, so it is read from
        # the file. Anything other than zero-imputation is a contract this app cannot honour.
        imputation = str(raw.get("genome_imputation", "zero"))
        if imputation != "zero":
            raise ContractError(
                f"contract says genome_imputation={imputation!r}; §4.3 records it as "
                "zero-imputation and the app implements no other"
            )
        absent = str(raw.get("absent_blocks", "dropped"))
        if absent != "dropped":
            raise ContractError(
                f"contract says absent_blocks={absent!r}; §5.3 drops them — 'the tower's "
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
        text = raw.get("review_text") or {}
        text_dims = int(text.get("svd_dims", 256))
        text_used = int(text.get("used", 64))
        if not 0 < text_used <= text_dims:
            raise ContractError(
                f"review_text uses {text_used} of {text_dims} SVD columns — §4.3 takes "
                "columns 0..63 of the 256-d embedding"
            )
        order = str(text.get("order", "singular-value"))
        if order != "singular-value":
            raise ContractError(
                f"review_text order is {order!r}; §4.3 takes the first columns in "
                "singular-value order, which is only a truncation if the order holds"
            )
        if "text_scale" not in raw:
            raise ContractError(
                "contract ships no `text_scale`; §4.3 freezes it at export time so placements "
                "stay comparable across runs, and a default would silently move every coordinate"
            )
        text_scale = float(raw["text_scale"])

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


def _declared_blocks(
    raw: Mapping[str, Any], blocks_raw: Any
) -> list[tuple[str, dict[str, Any]]]:
    """Both shapes the exporter can ship, reduced to (name, spec) pairs **in declared order**.

    The bundle in hand ships the flat shape — `blocks: {name: size}` with a separate
    `block_order` and `feature_names` — and the §4.3 sentence reads as a list of block objects.
    Order is load-bearing either way ("the nine content blocks *in order*"), so it is taken from
    the list, or from `block_order`, or from the JSON object's own key order, in that priority.
    """
    if isinstance(blocks_raw, list):
        out = []
        for spec in blocks_raw:
            name = str(spec.get("name") or "")
            if not name:
                raise ContractError("a block in `blocks` has no `name`")
            out.append((name, dict(spec)))
        return out

    if not isinstance(blocks_raw, dict):
        raise ContractError(f"`blocks` is a {type(blocks_raw).__name__}, not a list or an object")

    order = [str(n) for n in (raw.get("block_order") or blocks_raw.keys())]
    unknown = [n for n in order if n not in blocks_raw]
    if unknown:
        raise ContractError(f"block_order names blocks that have no size: {unknown}")
    missing = [n for n in blocks_raw if n not in order]
    if missing:
        raise ContractError(f"blocks {missing} have a size but no place in block_order")

    names = raw.get("feature_names") or {}
    encodings = raw.get("encodings") or {}
    normalisers = raw.get("normalise") or {}
    transforms = raw.get("transforms") or {}
    return [
        (
            name,
            {
                "size": blocks_raw[name],
                "feature_names": names.get(name),
                "encoding": encodings.get(name),
                "normalise": normalisers.get(name),
                "transform": transforms.get(name),
            },
        )
        for name in order
    ]


def unproducible_meta_names(contract: FeatureContract) -> list[str]:
    """Declared meta columns no production in the grammar can fill.

    Not an exception: the corpus export is the authority on its own column names, and a name
    this app cannot produce costs one always-zero column rather than a wrong vector. It is
    reported so the gap is named instead of being invisible.
    """
    if not contract.has("meta"):
        return []
    return [n for n in contract.block("meta").names if not _META_GRAMMAR.match(n)]
