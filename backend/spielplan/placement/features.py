"""The placement feature vector (§8 stage 9): layout from the contract, values from the database.

Each block's query mirrors the corpus exporter, since the tower was trained on its output.
`build_vector` is pure so §5.3's per-title budget is measurable without Postgres.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from spielplan.placement.contract import TEXT_BLOCK, Block, ContractError, FeatureContract

# Blocks no §8 stage 2 re-fetch can fill (no genome for new titles, an award nobody gave, review text
# from the bundle), so their absence does not make a title thin.
UNENRICHABLE_BLOCKS: tuple[str, ...] = ("genome", "award", TEXT_BLOCK)

# block name -> feature key -> value, for one title.
TitleRows = dict[str, dict[str, float]]
BlockSource = Callable[[Any, Sequence[int], str], Awaitable[dict[int, dict[str, float]]]]


@dataclass(frozen=True)
class BuiltVector:
    """One title's tower input, plus the block bookkeeping §5.3's badge and §8.4's flywheel read."""

    title_id: int
    vec: np.ndarray                       # float32, contract.input_dim
    blocks_present: tuple[str, ...]
    blocks_dropped: tuple[str, ...]       # §5.3 "absent blocks dropped … rather than defaulted"
    blocks_imputed: tuple[str, ...]       # §4.3 "genome zero-imputation"
    blocks_empty: tuple[str, ...] = ()    # rows arrived; none of their keys is a declared column
    unmapped: dict[str, int] = field(default_factory=dict, repr=False)
    nnz: int = 0
    build_ms: int = 0

    @property
    def is_thin(self) -> bool:
        """§5.3's thin title: an enrichable block dropped, or any block that hit no declared column."""
        missing = [b for b in self.blocks_dropped if b not in UNENRICHABLE_BLOCKS]
        return bool(missing or self.blocks_empty)


def build_vector(
    contract: FeatureContract,
    title_id: int,
    rows: Mapping[str, Mapping[str, float]],
    text_emb: np.ndarray | None,
) -> BuiltVector:
    """Assemble one title's vector. Pure.

    Dropped and zero-imputed blocks are identical zeros (the tower trained on block dropout); only
    the bookkeeping differs. Nothing is ever filled with a mean or prior.
    """
    started = time.perf_counter()
    vec = np.zeros(contract.input_dim, dtype=np.float32)
    present: list[str] = []
    dropped: list[str] = []
    imputed: list[str] = []
    empty: list[str] = []
    unmapped: dict[str, int] = {}

    for block in contract.blocks:
        pairs = rows.get(block.name)
        if not pairs:
            (imputed if block.impute == "zero" else dropped).append(block.name)
            continue
        hits = 0
        misses = 0
        for key, value in pairs.items():
            column = block.column(key)
            if column is None:
                misses += 1        # a key this contract does not declare — counted, never grown
                continue
            hits += 1
            vec[column] = 1.0 if block.encoding == "multi_hot" else float(value)
        if misses:
            unmapped[block.name] = misses
        # Present only if it hit a declared column; otherwise the tower got zeros.
        (present if hits else empty).append(block.name)
        _normalise(vec, block)

    if text_emb is None:
        dropped.append(TEXT_BLOCK)
    else:
        if text_emb.shape[-1] < contract.text_used:
            raise ContractError(
                f"review-text embedding for title {title_id} has {text_emb.shape[-1]} columns; "
                f"the contract takes the first {contract.text_used}"
            )
        present.append(TEXT_BLOCK)
        vec[contract.text_offset:] = (
            text_emb[: contract.text_used].astype(np.float32) * contract.text_scale
        )

    return BuiltVector(
        title_id=title_id,
        vec=vec,
        blocks_present=tuple(present),
        blocks_dropped=tuple(dropped),
        blocks_imputed=tuple(imputed),
        blocks_empty=tuple(empty),
        unmapped=unmapped,
        nnz=int(np.count_nonzero(vec)),
        build_ms=int(round((time.perf_counter() - started) * 1000)),
    )


def _normalise(vec: np.ndarray, block: Block) -> None:
    if block.normalise == "none":
        return
    span = vec[block.offset:block.stop]
    if block.normalise == "l2":
        scale = float(np.linalg.norm(span))
    elif block.normalise == "sum1":
        scale = float(np.abs(span).sum())
    else:                                   # max1
        scale = float(np.abs(span).max(initial=0.0))
    if scale > 0.0:
        span /= scale


# --- the review-text block's own file --------------------------------------------------------


def text_embeddings(store: Any, title_ids: Sequence[int]) -> dict[int, np.ndarray]:
    """The rows of `review_text_emb.npz` for these titles. A `covered = False` row is no row."""
    if getattr(store, "is_empty", True) or not store.present.get("review_text_emb.npz"):
        return {}
    npz = store.npz("review_text_emb.npz")
    # `title_ids`, plural: the exporter's name.
    ids = np.asarray(npz["title_ids"]).astype(np.int64)
    emb = npz["emb"]
    # Only an unvalidated store lacks `covered`; then every row counts.
    covered = npz["covered"] if "covered" in npz.files else None
    wanted = {int(t) for t in title_ids}
    return {
        int(t): np.asarray(emb[i])
        for i, t in enumerate(ids)
        if int(t) in wanted and (covered is None or bool(covered[i]))
    }


# --- the nine block sources ------------------------------------------------------------------
# Each returns {title_id: {feature key: value}}; no entry for a title means the block is absent.


async def _dna_x(conn: Any, ids: Sequence[int], vocab_version: str) -> dict[int, dict[str, float]]:
    """The extracted tier (§4.1 rule 1), as presence; DISTINCT across §6.6's per-provider rows."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT title_id, 'dna:' || term AS key, 1.0::float8 AS value
          FROM dna_tag
         WHERE title_id = ANY($1::int[]) AND version = $2
        """,
        list(ids), vocab_version,
    )
    return _group(rows)


async def _dna_p(conn: Any, ids: Sequence[int], vocab_version: str) -> dict[int, dict[str, float]]:
    """The projected tier — a separate table, a separate statement, never a union (rule 1)."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT title_id, 'dna:' || term AS key, 1.0::float8 AS value
          FROM dna_projected
         WHERE title_id = ANY($1::int[]) AND version = $2
        """,
        list(ids), vocab_version,
    )
    return _group(rows)


# The corpus exporter's own genome cut: which rows the tower's columns were built from. Older
# installs still hold genome rows (decision 311).
_GENOME_MIN_RELEVANCE = 0.5


async def _genome(conn: Any, ids: Sequence[int], _vocab: str) -> dict[int, dict[str, float]]:
    """MovieLens genome relevance. Not dead code: pre-291 installs still hold it (decision 311)."""
    rows = await conn.fetch(
        """
        SELECT l.title_id, 'g:' || g.tag AS key, s.relevance::float8 AS value
          FROM ml_link l
          JOIN ml_genome_score s ON s.ml_movie_id = l.ml_movie_id
          JOIN ml_genome_tag g ON g.tag_id = s.tag_id
         WHERE l.title_id = ANY($1::int[]) AND s.relevance >= $2
        """,
        list(ids), _GENOME_MIN_RELEVANCE,
    )
    return _group(rows)


async def _genre(conn: Any, ids: Sequence[int], _vocab: str) -> dict[int, dict[str, float]]:
    """The cell is a COUNT of the source rows that said it, as the exporter summed them."""
    rows = await conn.fetch(
        "SELECT title_id, 'genre:' || lower(genre) AS key, count(*)::float8 AS value"
        " FROM title_genre WHERE title_id = ANY($1::int[])"
        " GROUP BY title_id, lower(genre)",
        list(ids),
    )
    return _group(rows)


async def _keyword(conn: Any, ids: Sequence[int], _vocab: str) -> dict[int, dict[str, float]]:
    """`lower(trim(...))`: the expression the exporter named the columns from."""
    rows = await conn.fetch(
        "SELECT title_id, 'kw:' || lower(trim(keyword)) AS key, count(*)::float8 AS value"
        " FROM title_keyword WHERE title_id = ANY($1::int[])"
        " GROUP BY title_id, lower(trim(keyword))",
        list(ids),
    )
    return _group(rows)


async def _credit(conn: Any, ids: Sequence[int], _vocab: str) -> dict[int, dict[str, float]]:
    """Keyed `p:<role_class>:<name>` (the contract is name-keyed); dedupe at read time (§4.1).

    The role predicate is the exporter's, verbatim: which rows the columns were built from.
    """
    rows = await conn.fetch(
        "SELECT c.title_id, 'p:' || c.role_class || ':' || p.name AS key,"
        " count(*)::float8 AS value"
        "  FROM credit c JOIN person p ON p.id = c.person_id"
        " WHERE c.title_id = ANY($1::int[]) AND p.name <> ''"
        "   AND (c.role_class IN ('director', 'writer', 'composer', 'dp')"
        "        OR (c.role_class = 'cast' AND c.billing_order <= 3))"
        " GROUP BY c.title_id, c.role_class, p.name",
        list(ids),
    )
    return _group(rows)


async def _country(conn: Any, ids: Sequence[int], _vocab: str) -> dict[int, dict[str, float]]:
    """The cell is a COUNT of the source rows that said it, as the exporter summed them."""
    rows = await conn.fetch(
        "SELECT title_id, 'country:' || country AS key, count(*)::float8 AS value"
        " FROM title_country WHERE title_id = ANY($1::int[])"
        " GROUP BY title_id, country",
        list(ids),
    )
    return _group(rows)


async def _award(conn: Any, ids: Sequence[int], _vocab: str) -> dict[int, dict[str, float]]:
    """Two counts. `won IS NOT TRUE`: an unknown outcome is still a nomination."""
    rows = await conn.fetch(
        """
        SELECT title_id,
               count(*) FILTER (WHERE won IS NOT TRUE)::float8 AS nominated,
               count(*) FILTER (WHERE won)::float8            AS won
          FROM award
         WHERE title_id = ANY($1::int[])
         GROUP BY title_id
        """,
        list(ids),
    )
    return {
        int(r["title_id"]): {
            "award:nominated": float(r["nominated"]),
            "award:won": float(r["won"]),
        }
        for r in rows
    }


_META_SQL = """
SELECT t.id, t.kind, t.year, t.runtime_min, t.original_language,
       (t.overview    IS NOT NULL AND t.overview <> '') AS has_overview,
       (t.tagline     IS NOT NULL AND t.tagline  <> '') AS has_tagline,
       (t.trailer_key IS NOT NULL)                      AS has_trailer,
       (t.poster_path IS NOT NULL)                      AS has_poster,
       (t.imdb_id     IS NOT NULL)                      AS has_imdb_id,
       (SELECT count(*) FROM credit        c WHERE c.title_id = t.id) AS n_credits,
       (SELECT count(*) FROM title_genre   g WHERE g.title_id = t.id) AS n_genres,
       (SELECT count(*) FROM title_keyword k WHERE k.title_id = t.id) AS n_keywords,
       (SELECT count(*) FROM title_country o WHERE o.title_id = t.id) AS n_countries,
       (SELECT count(*) FROM title_alias   a WHERE a.title_id = t.id) AS n_aliases,
       (SELECT count(*) FROM title_company p WHERE p.title_id = t.id) AS n_companies,
       (SELECT count(*) FROM award         w WHERE w.title_id = t.id) AS n_awards,
       (SELECT count(*) FROM review_store.review r WHERE r.title_id = t.id) AS n_reviews,
       (SELECT count(*) FROM dna_tag       d
         WHERE d.title_id = t.id AND d.version = $2) AS n_dna_x,
       (SELECT count(*) FROM dna_projected j
         WHERE j.title_id = t.id AND j.version = $2) AS n_dna_p,
       (SELECT count(*) FROM ml_link ml
          JOIN ml_genome_score gs ON gs.ml_movie_id = ml.ml_movie_id
         WHERE ml.title_id = t.id) AS n_genome
  FROM title t
 WHERE t.id = ANY($1::int[])
"""

_COUNT_KEYS = (
    "credits", "genres", "keywords", "countries", "reviews",
    "dna_x", "dna_p", "aliases", "companies", "awards",
)


# The corpus's runtime bins, boundaries included: exactly 160 belongs in `>160`.
_RUNTIME_EDGES = ((80, "<80"), (105, "80-105"), (130, "105-130"), (160, "130-160"))


def _runtime_bucket(minutes: Any) -> str | None:
    if minutes is None:
        return None
    value = int(minutes)
    for edge, label in _RUNTIME_EDGES:
        if value < edge:
            return label
    return ">160"


async def _meta(conn: Any, ids: Sequence[int], vocab_version: str) -> dict[int, dict[str, float]]:
    """The one block produced by code (grammar in `contract.META_PRODUCTIONS`); every title has it.

    `lang:` is `title.original_language` as the exporter wrote it, not the `title_language` list.
    """
    rows = await conn.fetch(_META_SQL, list(ids), vocab_version)

    out: dict[int, dict[str, float]] = {}
    for r in rows:
        title_id = int(r["id"])
        counts = {k: int(r[f"n_{k}"]) for k in _COUNT_KEYS}
        values: dict[str, float] = {f"kind:{r['kind']}": 1.0}
        if r["year"] is not None:
            values[f"decade:{(int(r['year']) // 10) * 10}"] = 1.0
        bucket = _runtime_bucket(r["runtime_min"])
        if bucket is not None:
            values[f"runtime:{bucket}"] = 1.0
        if r["original_language"]:
            values[f"lang:{r['original_language']}"] = 1.0
        for flag in ("overview", "tagline", "trailer", "poster", "imdb_id"):
            if r[f"has_{flag}"]:
                values[f"has:{flag}"] = 1.0
        for flag, count in (("genome", int(r["n_genome"])), ("award", counts["awards"]),
                            ("review", counts["reviews"]), ("keyword", counts["keywords"]),
                            ("credit", counts["credits"])):
            if count:
                values[f"has:{flag}"] = 1.0
        values["_year"] = float(r["year"]) if r["year"] is not None else math.nan
        values["_runtime"] = (
            float(r["runtime_min"]) if r["runtime_min"] is not None else math.nan
        )
        for key, count in counts.items():
            values[f"_n_{key}"] = float(count)
        out[title_id] = values
    return out


def _finish_meta(contract: FeatureContract, values: dict[str, float]) -> dict[str, float]:
    """Apply the three continuous productions, with the contract's constants or the defaults."""
    year = values.pop("_year", math.nan)
    runtime = values.pop("_runtime", math.nan)
    counts = {k[3:]: values.pop(k) for k in list(values) if k.startswith("_n_")}

    if not math.isnan(year):
        t = contract.meta_transform("year_norm")
        values["year_norm"] = _clip01((year - t["offset"]) / t["scale"])
    if not math.isnan(runtime):
        t = contract.meta_transform("runtime_norm")
        values["runtime_norm"] = _clip01((runtime - t["offset"]) / t["scale"])
    t = contract.meta_transform("count_log")
    for key, count in counts.items():
        if count:
            values[f"n_{key}_log"] = _clip01((math.log1p(count) - t["offset"]) / t["scale"])
    return values


def _clip01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


BLOCK_SOURCES: dict[str, BlockSource] = {
    "dna_x": _dna_x, "dna_p": _dna_p, "genome": _genome, "genre": _genre,
    "keyword": _keyword, "credit": _credit, "country": _country, "award": _award,
    "meta": _meta,
}


def _group(rows: Sequence[Any]) -> dict[int, dict[str, float]]:
    out: dict[int, dict[str, float]] = {}
    for r in rows:
        out.setdefault(int(r["title_id"]), {})[str(r["key"])] = float(r["value"])
    return out


async def fetch_blocks(
    conn: Any,
    title_ids: Sequence[int],
    contract: FeatureContract,
    *,
    vocab_version: str,
) -> dict[int, TitleRows]:
    """Read every block the contract declares, for a chunk of titles."""
    rows: dict[int, TitleRows] = {int(t): {} for t in title_ids}
    for block in contract.blocks:
        source = BLOCK_SOURCES.get(block.name)
        if source is None:
            continue          # a block §4.3 does not name; reported by contract.notes
        produced = await source(conn, title_ids, vocab_version)
        for title_id, values in produced.items():
            if title_id not in rows:
                continue
            rows[title_id][block.name] = (
                _finish_meta(contract, values) if block.name == "meta" else values
            )
    return rows


def unproducible_blocks(contract: FeatureContract) -> tuple[str, ...]:
    """Declared blocks this app has no source for — always dropped, so always reported."""
    return tuple(b.name for b in contract.blocks if b.name not in BLOCK_SOURCES)


async def build_vectors(
    conn: Any,
    store: Any,
    contract: FeatureContract,
    title_ids: Sequence[int],
    *,
    vocab_version: str,
) -> list[BuiltVector]:
    """§8 stage 9 for a chunk of titles: database rows in, tower inputs out."""
    rows = await fetch_blocks(conn, title_ids, contract, vocab_version=vocab_version)
    emb = text_embeddings(store, title_ids)
    return [
        build_vector(contract, int(t), rows.get(int(t), {}), emb.get(int(t)))
        for t in title_ids
    ]
