"""The placement feature vector, built from the contract and the database. Spec v2.1 §8 stage 9.

§8 stage 9: "feature vector per the feature contract → Cold Tower → ê(t), b̂(t); genome block
zero-imputed (unavailable for new titles by construction)."
§5.3: "any owned title lacking a coordinate gets a feature vector built from **DB data** per the
feature contract (absent blocks dropped — the tower's dropout training anticipates this; genome
zero-imputed)."

Those two sentences fix both halves of this module:

  * the **column layout** comes from `contract.py` and from nowhere else — no offset is written
    down here, and `build_vector` never mentions a block by name except `review_text`, whose
    source is a different file (§4.3);
  * the **values** come from the content spine and from nowhere else. Nine queries, one per
    block, each a plain read. §4.1's rules are structural here rather than remembered:

      rule 1 — `dna_tag` and `dna_projected` are two functions and two statements. There is no
               code path in this module that reads them from one query.
      rule 2 — salience, confidence and the projected tier's per-term strength appear in SELECT
               lists and never in a predicate: they are weights, never filters.
      rule 3 — nothing here names the display-only schema. Aggregate platform scores are a
               popularity conduit and are banned as model features.

`build_vector` is pure — no database, no torch, no clock beyond its own stopwatch — because
§5.3 puts a per-title budget on placement and a budget measured through Postgres measures
Postgres.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from spielplan.placement.contract import TEXT_BLOCK, Block, ContractError, FeatureContract

# block name -> feature key -> value, for one title.
TitleRows = dict[str, dict[str, float]]
BlockSource = Callable[[Any, Sequence[int], str], Awaitable[dict[int, dict[str, float]]]]


@dataclass(frozen=True)
class BuiltVector:
    """One title's tower input, and the bookkeeping §5.3's badge and §8.4's flywheel read."""

    title_id: int
    vec: np.ndarray                       # float32, contract.input_dim
    blocks_present: tuple[str, ...]
    blocks_dropped: tuple[str, ...]       # §5.3 "absent blocks dropped … rather than defaulted"
    blocks_imputed: tuple[str, ...]       # §4.3 "genome zero-imputation"
    unmapped: dict[str, int] = field(default_factory=dict, repr=False)
    nnz: int = 0
    build_ms: int = 0

    @property
    def is_thin(self) -> bool:
        """§5.3: "thin ones (2 lack keywords, 3 lack any DNA row) are still placed, badged, and
        parked as acquisition jobs for M5 enrichment."

        Thin = at least one block dropped. A zero-imputed genome does **not** make a title thin:
        §8 stage 9 says the genome is "unavailable for new titles by construction", so no amount
        of §8 stage-2 enrichment would fill it and parking a job for it would be a job that can
        never finish.
        """
        return bool(self.blocks_dropped)


def build_vector(
    contract: FeatureContract,
    title_id: int,
    rows: Mapping[str, Mapping[str, float]],
    text_emb: np.ndarray | None,
) -> BuiltVector:
    """Assemble one title's vector. Pure: everything it knows arrives in its arguments.

    The load-bearing subtlety, stated here so nobody invents a mask channel: a dropped block and
    a zero-imputed block produce *identical bytes*. The tower's block-dropout training saw
    exactly all-zero blocks, so all-zero **is** "dropped". The difference is bookkeeping — and
    the badge, and the flywheel — and no path in this function fills a block with a mean, a
    prior or a global average.
    """
    started = time.perf_counter()
    vec = np.zeros(contract.input_dim, dtype=np.float32)
    present: list[str] = []
    dropped: list[str] = []
    imputed: list[str] = []
    unmapped: dict[str, int] = {}

    for block in contract.blocks:
        pairs = rows.get(block.name)
        if not pairs:
            # Absent. Zeros either way; which of the two facts it is, is recorded.
            (imputed if block.impute == "zero" else dropped).append(block.name)
            continue
        present.append(block.name)
        misses = 0
        for key, value in pairs.items():
            column = block.column(key)
            if column is None:
                misses += 1        # a key this contract does not declare — counted, never grown
                continue
            vec[column] = 1.0 if block.encoding == "multi_hot" else float(value)
        if misses:
            unmapped[block.name] = misses
        _normalise(vec, block)

    # §4.3: "then the review-text block = columns 0..63 of the 256-d SVD embedding
    # (singular-value order) multiplied by a frozen scalar `text_scale`".
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
    """The rows of `review_text_emb.npz` for these titles.

    §4.3 ships the full 256 columns and the contract says how many of them the tower sees, so
    the truncation is a decision this app makes *from the contract* rather than a shape it is
    handed. A title with no row is a title whose review-text block drops — the common case for
    a §8-acquired title before its reviews accrue (§8 stage 4).
    """
    if getattr(store, "is_empty", True) or not store.present.get("review_text_emb.npz"):
        return {}
    npz = store.npz("review_text_emb.npz")
    ids = np.asarray(npz["title_id"]).astype(np.int64)
    emb = npz["emb"]
    wanted = {int(t) for t in title_ids}
    return {
        int(t): np.asarray(emb[i]) for i, t in enumerate(ids) if int(t) in wanted
    }


# --- the nine block sources ------------------------------------------------------------------
#
# One statement per block per chunk. Every one of them returns {title_id: {feature key: value}},
# and a block whose query returns no row for a title yields no entry for that title — which is
# precisely how `build_vector` learns the block is absent. There is no "empty dict" middle
# state, because that is the defaulting §5.3 forbids.


async def _dna_x(conn: Any, ids: Sequence[int], vocab_version: str) -> dict[int, dict[str, float]]:
    """The extracted tier (§4.1 rule 1). Salience is the SELECT list; it is never a predicate.

    `max` over providers rather than a sum: §6.6's parallel mode writes one row per provider and
    salience is the strength of the reading, not its agreement — agreement lives in `confidence`
    and `n_sources`, which this block does not encode.
    """
    rows = await conn.fetch(
        """
        SELECT title_id, facet || '.' || term AS key, max(salience)::float8 AS value
          FROM dna_tag
         WHERE title_id = ANY($1::int[]) AND version = $2
         GROUP BY title_id, facet, term
        """,
        list(ids), vocab_version,
    )
    return _group(rows)


async def _dna_p(conn: Any, ids: Sequence[int], vocab_version: str) -> dict[int, dict[str, float]]:
    """The projected tier — a separate table, a separate statement, never a union (rule 1)."""
    rows = await conn.fetch(
        """
        SELECT title_id, facet || '.' || term AS key, coalesce(weight, 0.0)::float8 AS value
          FROM dna_projected
         WHERE title_id = ANY($1::int[]) AND version = $2
        """,
        list(ids), vocab_version,
    )
    return _group(rows)


async def _genome(conn: Any, ids: Sequence[int], _vocab: str) -> dict[int, dict[str, float]]:
    """MovieLens genome relevance, through the link slice. Absent for every §8-acquired title
    by construction — which is exactly why §4.3 zero-imputes this block and no other."""
    rows = await conn.fetch(
        """
        SELECT l.title_id, g.tag AS key, s.relevance::float8 AS value
          FROM ml_link l
          JOIN ml_genome_score s ON s.ml_movie_id = l.ml_movie_id
          JOIN ml_genome_tag g ON g.tag_id = s.tag_id
         WHERE l.title_id = ANY($1::int[])
        """,
        list(ids),
    )
    return _group(rows)


async def _genre(conn: Any, ids: Sequence[int], _vocab: str) -> dict[int, dict[str, float]]:
    rows = await conn.fetch(
        "SELECT DISTINCT title_id, genre AS key, 1.0::float8 AS value"
        " FROM title_genre WHERE title_id = ANY($1::int[])",
        list(ids),
    )
    return _group(rows)


async def _keyword(conn: Any, ids: Sequence[int], _vocab: str) -> dict[int, dict[str, float]]:
    rows = await conn.fetch(
        "SELECT DISTINCT title_id, keyword AS key, 1.0::float8 AS value"
        " FROM title_keyword WHERE title_id = ANY($1::int[])",
        list(ids),
    )
    return _group(rows)


async def _credit(conn: Any, ids: Sequence[int], _vocab: str) -> dict[int, dict[str, float]]:
    """§4.1: "credit (dedupe at read time, never at import)" — hence DISTINCT here and no
    unique constraint there. The column key is the person id, which is what survives a name
    correction from `corrections_v1.tsv`."""
    rows = await conn.fetch(
        "SELECT DISTINCT title_id, person_id::text AS key, 1.0::float8 AS value"
        " FROM credit WHERE title_id = ANY($1::int[])",
        list(ids),
    )
    return _group(rows)


async def _country(conn: Any, ids: Sequence[int], _vocab: str) -> dict[int, dict[str, float]]:
    rows = await conn.fetch(
        "SELECT title_id, country AS key, 1.0::float8 AS value"
        " FROM title_country WHERE title_id = ANY($1::int[])",
        list(ids),
    )
    return _group(rows)


async def _award(conn: Any, ids: Sequence[int], _vocab: str) -> dict[int, dict[str, float]]:
    """§4.3 gives this block exactly two columns, so it is two counts and not a vocabulary.
    `won IS NOT TRUE` rather than `NOT won`: the column is nullable and an award with an
    unknown outcome is a nomination on the record."""
    rows = await conn.fetch(
        """
        SELECT title_id,
               count(*) FILTER (WHERE won IS NOT TRUE)::float8 AS award_nominations,
               count(*) FILTER (WHERE won)::float8            AS award_wins
          FROM award
         WHERE title_id = ANY($1::int[])
         GROUP BY title_id
        """,
        list(ids),
    )
    return {
        int(r["title_id"]): {
            "award_nominations": float(r["award_nominations"]),
            "award_wins": float(r["award_wins"]),
        }
        for r in rows
    }


_META_SQL = """
SELECT t.id, t.kind, t.year, t.runtime_min,
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


async def _meta(conn: Any, ids: Sequence[int], vocab_version: str) -> dict[int, dict[str, float]]:
    """The one block produced by code rather than read from a vocabulary.

    §4.3 fixes its width (57 columns in the corpus contract) and nothing else, so the columns
    come from the closed grammar in `contract.META_PRODUCTIONS` and a declared name outside it
    is reported rather than silently left at zero. Every title row produces this block, so a
    title with no keywords, no DNA and no reviews still has one block present and is placeable.
    """
    rows = await conn.fetch(_META_SQL, list(ids), vocab_version)
    languages = await conn.fetch(
        "SELECT DISTINCT title_id, lower(language) AS language"
        " FROM title_language WHERE title_id = ANY($1::int[])",
        list(ids),
    )
    by_title: dict[int, list[str]] = {}
    for row in languages:
        by_title.setdefault(int(row["title_id"]), []).append(str(row["language"]))

    out: dict[int, dict[str, float]] = {}
    for r in rows:
        title_id = int(r["id"])
        counts = {k: int(r[f"n_{k}"]) for k in _COUNT_KEYS}
        values: dict[str, float] = {f"kind:{r['kind']}": 1.0}
        if r["year"] is not None:
            values[f"decade:{(int(r['year']) // 10) * 10}"] = 1.0
        for code in by_title.get(title_id, ()):
            values[f"lang:{code}"] = 1.0
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
    """Apply the three continuous productions, whose constants live in the contract (§4.3:
    "records all placement-time preprocessing") and fall back to documented defaults."""
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
    """Read every block the contract declares, for a chunk of titles.

    Only declared blocks are queried: a contract that omits `genome` is a tower that was not
    trained on it, and reading it anyway would be work whose result has nowhere to go.
    """
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
