"""Extract the *shape* of a real corpus bundle, so the test fixture can be held to it.

Spec v2.1 §10 lists what a bundle contains; it does not pin the shapes, and that gap is what
let `backend/tests/fixtures/make_bundle.py` drift into a bundle the importer could read and the
corpus has never produced. The fixture reproduced every measured landmine and invented every
structure around them, so the whole import layer was verified against the implementation's own
reading of the spec rather than against the artifact.

This script reads a bundle and writes a manifest of shapes only -- table names and their
columns, file names, JSON key sets, TSV headers, npz array names, and the *pattern* of the
feature contract's column names. **No values.** Film titles, people's names and review text
never enter the manifest: what is recorded for a feature column is `p:<s>:<s>` (a `p:` prefix
and three colon-separated segments), never `p:director:Adam Arkin`.

Usage:

    python ops/bundle_shapes.py <bundle-dir> -o backend/tests/fixtures/real_bundle_shapes.json

The output is committed. `test_bundle_shapes.py` holds the fixture to it on every run, and
holds a real bundle to it too when `CORPUS_BUNDLE_DIR` is set -- so a corpus-side change to the
bundle format fails this repo's suite instead of surfacing as a mystery at import time.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# A column name is reduced to its punctuation skeleton: every run of non-separator characters
# becomes `<s>`, every digit run `<n>`. `p:director:Adam Arkin` -> `p:<s>`; `decade:1990` ->
# `decade:<n>`. That keeps the *grammar* of a block's keys -- which is what the vector builder
# has to reproduce -- and discards the vocabulary, which is data.
_SEG = re.compile(r"[^:]+")


def column_pattern(name: str) -> str:
    def one(match: re.Match[str]) -> str:
        return "<n>" if match.group(0).isdigit() else "<s>"

    head, sep, rest = name.partition(":")
    if not sep:
        return _SEG.sub(one, name)
    # Keep the block prefix literal -- `p:`, `genre:`, `kw:` are the contract's own vocabulary
    # of block tags, not data -- and reduce everything after it.
    return f"{head}:{_SEG.sub(one, rest)}"


def _sqlite_shapes(path: Path) -> dict[str, list[str]]:
    """Table -> ordered column names. Row counts are deliberately excluded: they change with
    every crawl, and a manifest that churns is a manifest nobody re-generates."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = sorted(
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not r[0].startswith("sqlite_")
        )
        return {t: [d[1] for d in conn.execute(f"PRAGMA table_info({t})")] for t in tables}
    finally:
        conn.close()


def _json_shape(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(doc, list):
        head = doc[0] if doc else None
        return {
            "type": "list",
            "entry_keys": sorted(head) if isinstance(head, dict) else None,
            "entry_type": type(head).__name__ if head is not None else None,
        }
    shape: dict[str, Any] = {"type": "object", "keys": sorted(doc)}
    for key, value in sorted(doc.items()):
        if isinstance(value, list) and value and isinstance(value[0], str):
            shape[f"{key}.item_patterns"] = sorted(Counter(map(column_pattern, value)))
            shape[f"{key}.len"] = len(value)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            shape[f"{key}.entry_keys"] = sorted(value[0])
            shape[f"{key}.len"] = len(value)
        elif isinstance(value, dict):
            shape[f"{key}.keys"] = sorted(value)
        else:
            shape[f"{key}.type"] = type(value).__name__
    return shape


def _tsv_header(path: Path) -> list[str]:
    with path.open(encoding="utf-8", newline="") as fh:
        return (fh.readline().rstrip("\r\n")).split("\t")


def _npz_keys(path: Path) -> list[str]:
    import numpy as np

    with np.load(path, allow_pickle=False) as z:
        return sorted(z.files)


def _pt_shapes(path: Path) -> dict[str, list[int]]:
    """Checkpoint tensor name -> shape. Names and shapes; never a weight.

    §4.3 calls `cold_tower.pt` "the live model" and says nothing about its interior, and the
    corpus ships `torch.save(model.state_dict())` — a bare OrderedDict with no `arch`, no
    `version` and no `input_dim`. So the tensor NAMES *are* the architecture contract:
    `placement/tower.py` reconstructs the module from `trunk.0.weight` and `head_e.weight`, and
    until this branch existed the manifest recorded the file as a bare filename, leaving those
    three names pinned to a comment rather than to the artifact.

    `weights_only=True` because a checkpoint is data: unpickling a bundle's arbitrary objects
    to read its key names would be a worse defect than the one this manifest exists to catch.
    """
    import torch

    obj = torch.load(path, map_location="cpu", weights_only=True)
    # A wrapper carrying `state_dict` is the shape this app used to demand; a bare state dict is
    # the shape the corpus ships. Both are read, so the manifest records what is there.
    state = obj.get("state_dict", obj) if isinstance(obj, dict) else obj
    return {name: list(tensor.shape) for name, tensor in state.items()}


def extract(root: Path) -> dict[str, Any]:
    """The whole manifest for one bundle directory."""
    shapes: dict[str, Any] = {
        "_note": "Shapes only -- no values. Regenerate with ops/bundle_shapes.py.",
        "files": [],
        "sqlite": {},
        "json": {},
        "tsv": {},
        "npz": {},
        "pt": {},
    }
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        shapes["files"].append(rel)
        try:
            if path.suffix == ".sqlite":
                shapes["sqlite"][rel] = _sqlite_shapes(path)
            elif path.suffix == ".json":
                shapes["json"][rel] = _json_shape(path)
            elif path.suffix == ".tsv":
                shapes["tsv"][rel] = _tsv_header(path)
            elif path.suffix == ".npz":
                shapes["npz"][rel] = _npz_keys(path)
            elif path.suffix == ".pt":
                shapes["pt"][rel] = _pt_shapes(path)
        except Exception as exc:                                  # noqa: BLE001
            # A file this script cannot read is recorded as unreadable rather than skipped:
            # silence here would reproduce the exact failure the manifest exists to prevent.
            shapes.setdefault("unreadable", {})[rel] = f"{type(exc).__name__}: {exc}"
    return shapes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bundle", type=Path, help="a bundle directory (content.sqlite, artifacts/)")
    ap.add_argument("-o", "--out", type=Path, help="write here instead of stdout")
    args = ap.parse_args(argv)

    if not args.bundle.is_dir():
        print(f"not a directory: {args.bundle}", file=sys.stderr)
        return 2
    text = json.dumps(extract(args.bundle), indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
