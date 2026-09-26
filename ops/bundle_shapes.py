"""Extract the *shape* of a real corpus bundle -- names, keys, headers, never values.

    python ops/bundle_shapes.py <bundle-dir> -o backend/tests/fixtures/real_bundle_shapes.json
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

# A column name keeps its grammar and drops its vocabulary: `decade:1990` -> `decade:<n>`.
_SEG = re.compile(r"[^:]+")


def column_pattern(name: str) -> str:
    def one(match: re.Match[str]) -> str:
        return "<n>" if match.group(0).isdigit() else "<s>"

    head, sep, rest = name.partition(":")
    if not sep:
        return _SEG.sub(one, name)
    # The block prefix (`p:`, `genre:`) is the contract's vocabulary, not data.
    return f"{head}:{_SEG.sub(one, rest)}"


def _sqlite_shapes(path: Path) -> dict[str, list[str]]:
    """Table -> ordered column names. No row counts: they churn with every crawl."""
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
    """Checkpoint tensor name -> shape, never a weight: the bare state dict's names are the
    architecture contract `placement/tower.py` rebuilds from. `weights_only` because it is data."""
    import torch

    obj = torch.load(path, map_location="cpu", weights_only=True)
    state = obj.get("state_dict", obj) if isinstance(obj, dict) else obj
    return {name: list(tensor.shape) for name, tensor in state.items()}


def extract(root: Path) -> dict[str, Any]:
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
            # Recorded, not skipped: silence is the failure the manifest exists to catch.
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
