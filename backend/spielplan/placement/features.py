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
      rule 2 — salience, confidence, `n_sources` and the projected tier's per-term strength
               appear in no predicate here: they are weights, never filters. They are not in
               the cells either — the corpus's exporter wrote both DNA tiers as presence, and
               §4.3 makes its column set the exhaustive definition of the tower's input.
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
    blocks_empty: tuple[str, ...] = ()    # rows arrived; none of their keys is a declared column
    unmapped: dict[str, int] = field(default_factory=dict, repr=False)
    nnz: int = 0
    build_ms: int = 0

    @property
    def is_thin(self) -> bool:
        """§5.3: "thin ones (2 lack keywords, 3 lack any DNA row) are still placed, badged, and
        parked as acquisition jobs for M5 enrichment."

        Thin = at least one block dropped, or one that hit none of its declared columns. The
        second half is not a second rule: §4.3 makes the contract "the **exhaustive** definition
        of the tower's input", so a block whose keys are all outside it feeds the tower exactly
        the zeros a dropped block does. A title whose keywords are real but name nothing the
        exported vocabulary carries is as thin as one with no keywords at all, and §8 stage 2's
        re-fetch is the same remedy for both.

        A zero-imputed genome does **not** make a title thin: §8 stage 9 says the genome is
        "unavailable for new titles by construction", so no amount of §8 stage-2 enrichment
        would fill it and parking a job for it would be a job that can never finish.
        """
        return bool(self.blocks_dropped or self.blocks_empty)


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
    empty: list[str] = []
    unmapped: dict[str, int] = {}

    for block in contract.blocks:
        pairs = rows.get(block.name)
        if not pairs:
            # Absent. Zeros either way; which of the two facts it is, is recorded.
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
        # §4.3 makes the contract the exhaustive definition of the tower's input, so "present"
        # can only mean "hit a column that definition declares". A block that produced rows and
        # landed none of them writes the same zeros a dropped block writes, and marking it
        # present asserts the tower was fed something it was not — which is how a credit block
        # keyed `person_id::text` against `p:<role>:<name>` columns stayed invisible.
        (present if hits else empty).append(block.name)
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
    """The rows of `review_text_emb.npz` for these titles.

    §4.3 ships the full 256 columns and the contract says how many of them the tower sees, so
    the truncation is a decision this app makes *from the contract* rather than a shape it is
    handed. A title with no row is a title whose review-text block drops — the common case for
    a §8-acquired title before its reviews accrue (§8 stage 4).

    A row whose `covered` flag is False is no row either. The shipped contract records the rule
    itself — `preprocessing.missing_review_text: "zeros when covered=False"` — and the bundle
    sets it False on 6,010 of 14,397 rows, whose `emb` is float noise around 1e-16 rather than
    text. Returning them made the tenth block *present* for 42% of titles while the tower got
    64 zeros: §5.3's badge stayed off, `is_thin` stayed False, and §8 stage 2 never parked the
    acquisition job that is the only thing which can fill it.
    """
    if getattr(store, "is_empty", True) or not store.present.get("review_text_emb.npz"):
        return {}
    npz = store.npz("review_text_emb.npz")
    # `title_ids`, plural: the name the corpus's exporter writes. Reading `title_id` raised a
    # KeyError on every bundle it has ever produced, so no block below this line was reachable
    # (row data-rules-model-artifacts-load-from-the-shipped-bundle).
    ids = np.asarray(npz["title_ids"]).astype(np.int64)
    emb = npz["emb"]
    # A bundle that ships no `covered` array has told this app nothing about coverage, and
    # inventing it from the embedding's magnitude would be a threshold nothing states. Every
    # bundle the corpus has produced carries it, and `validate.py` refuses one that does not —
    # so this branch is the read of a store that was never validated, not of an import.
    covered = npz["covered"] if "covered" in npz.files else None
    wanted = {int(t) for t in title_ids}
    return {
        int(t): np.asarray(emb[i])
        for i, t in enumerate(ids)
        if int(t) in wanted and (covered is None or bool(covered[i]))
    }


# --- the nine block sources ------------------------------------------------------------------
#
# One statement per block per chunk. Every one of them returns {title_id: {feature key: value}},
# and a block whose query returns no row for a title yields no entry for that title — which is
# precisely how `build_vector` learns the block is absent. There is no "empty dict" middle
# state, because that is the defaulting §5.3 forbids.


async def _dna_x(conn: Any, ids: Sequence[int], vocab_version: str) -> dict[int, dict[str, float]]:
    """The extracted tier (§4.1 rule 1), as presence.

    The cell is 1.0 and not salience: the corpus built this block's 433 columns with
    `build("dna_x", "SELECT title_id, 'dna:'||term FROM dna_tag")` and no `weighted=True`
    (`scripts/build_content.py`), so the checkpoint has never seen a 2 or a 3 here — see
    `contract.DEFAULT_ENCODING`. §4.1 rule 2 is still honoured: salience appears in no predicate.

    DISTINCT rather than a GROUP BY: §6.6's parallel mode writes one row per provider, and the
    corpus's `dna_tag` is PRIMARY KEY (title_id, term) — one row per term, whatever read it.
    """
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
    """The projected tier — a separate table, a separate statement, never a union (rule 1).

    Presence again, for the same reason and from the same builder line: the projected weight is
    a weight the corpus's exporter did not carry into the tower's input.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT title_id, 'dna:' || term AS key, 1.0::float8 AS value
          FROM dna_projected
         WHERE title_id = ANY($1::int[]) AND version = $2
        """,
        list(ids), vocab_version,
    )
    return _group(rows)


# The corpus's own genome cut, verbatim: `build("genome", "... WHERE g.relevance >= 0.5",
# weighted=True)` in `scripts/build_content.py`. It is not a filter on a *weight* in §4.1 rule
# 2's sense — it is the definition of which rows the 983 columns were counted from, and the
# shipped `content_X.npz` proves it: the genome block's minimum nonzero is exactly 0.5. Reading
# every row instead feeds 587,502 of the bundle's 888,023 scores into columns the tower was
# trained to see as zero.
_GENOME_MIN_RELEVANCE = 0.5


async def _genome(conn: Any, ids: Sequence[int], _vocab: str) -> dict[int, dict[str, float]]:
    """MovieLens genome relevance, through the link slice. Absent for every §8-acquired title
    by construction — which is exactly why §4.3 zero-imputes this block and no other."""
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
    """The cell is a COUNT of the source rows that said it, not a presence bit.

    Measured in the shipped `content_X.npz` — the corpus's own tower input — the four
    source-multiplied blocks carry values above 1.0 in a large minority of their nonzeros:
    genre 37.7% (max 6), keyword 21.4% (max 7), credit 64.7% (max 5), country 66.2% (max 3).
    The exporter's `build(...)` sums duplicate (title, feature) pairs and every one of these
    tables carries a `source` column, so a genre four sources agreed on is a 4.0. `SELECT
    DISTINCT ... 1.0` fed the checkpoint a presence bit where it was trained on a count.
    """
    rows = await conn.fetch(
        "SELECT title_id, 'genre:' || lower(genre) AS key, count(*)::float8 AS value"
        " FROM title_genre WHERE title_id = ANY($1::int[])"
        " GROUP BY title_id, lower(genre)",
        list(ids),
    )
    return _group(rows)


async def _keyword(conn: Any, ids: Sequence[int], _vocab: str) -> dict[int, dict[str, float]]:
    """`lower(trim(...))` because that is the expression the 3,884 columns were named from —
    `build("keyword", "SELECT title_id, 'kw:'||lower(trim(keyword)) FROM title_keyword")` in
    `scripts/build_content.py`. 15,096 of the shipped bundle's 764,732 keyword rows are not
    already lower-cased, and each of those missed its column outright."""
    rows = await conn.fetch(
        "SELECT title_id, 'kw:' || lower(trim(keyword)) AS key, count(*)::float8 AS value"
        " FROM title_keyword WHERE title_id = ANY($1::int[])"
        " GROUP BY title_id, lower(trim(keyword))",
        list(ids),
    )
    return _group(rows)


async def _credit(conn: Any, ids: Sequence[int], _vocab: str) -> dict[int, dict[str, float]]:
    """§4.1: "credit (dedupe at read time, never at import)" — hence DISTINCT here and no
    unique constraint there.

    The key is `p:<role_class>:<name>`, which is what the shipped contract declares. It used to
    be `person_id::text`, on the reasoning that an id survives a name correction — true, and
    beside the point: the tower was trained on a name-keyed vocabulary, so an id-keyed builder
    misses all 244 columns and the block contributes nothing while reporting itself present.
    `corrections_v1.tsv` is applied at derive time (§8 stage 3), which is what actually keeps
    the name right.

    `role_class` is the corpus's own normalisation (director|writer|dp|composer|cast|…), not a
    re-derivation from `job` strings: re-deriving it here would drift from the vocabulary the
    contract was built against, one job title at a time.

    The role predicate is the corpus's, verbatim (`scripts/build_content.py`): the four
    above-the-line crafts, plus cast only down to third billing. It is not an optimisation — it
    is which rows the 244 columns exist for. Every one of the shipped bundle's 281,655 credit
    rows carries a `role_class`, so "any non-NULL role_class" lit 52,421 rows the corpus left
    dark: an editor, a production designer and a tenth-billed actor all entering columns whose
    training distribution has them at zero."""
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
    """The cell is a COUNT of the source rows that said it, not a presence bit.

    Measured in the shipped `content_X.npz` — the corpus's own tower input — the four
    source-multiplied blocks carry values above 1.0 in a large minority of their nonzeros:
    genre 37.7% (max 6), keyword 21.4% (max 7), credit 64.7% (max 5), country 66.2% (max 3).
    The exporter's `build(...)` sums duplicate (title, feature) pairs and every one of these
    tables carries a `source` column, so a genre four sources agreed on is a 4.0. `SELECT
    DISTINCT ... 1.0` fed the checkpoint a presence bit where it was trained on a count.
    """
    rows = await conn.fetch(
        "SELECT title_id, 'country:' || country AS key, count(*)::float8 AS value"
        " FROM title_country WHERE title_id = ANY($1::int[])"
        " GROUP BY title_id, country",
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


# The corpus's own binning (`mdc/ratings/features.py:89`), boundaries included: `< 80`,
# `< 105`, `< 130`, `< 160`, else `>160`. Copied rather than re-derived because the tower was
# trained on these columns -- a runtime of exactly 160 belongs in `>160`, and an off-by-one
# moves every three-hour film into a column it was never trained in.
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
    """The one block produced by code rather than read from a vocabulary.

    §4.3 fixes its width (57 columns in the corpus contract) and nothing else, so the columns
    come from the closed grammar in `contract.META_PRODUCTIONS` and a declared name outside it
    is reported rather than silently left at zero. Every title row produces this block, so a
    title with no keywords, no DNA and no reviews still has one block present and is placeable.

    One `lang:` column per title, from `title.original_language`. That is the corpus's own
    production — `for tid, kind, year, runtime, lang in q("SELECT id, kind, year, runtime_min,
    original_language FROM title") … if lang: meta.append((tid, f"lang:{lang}"))`
    (`scripts/build_content.py`) — and it is a different fact from `title_language`, which is a
    multi-source list of the languages *spoken* in a title and averages 2.98 distinct entries
    per title across the shipped bundle. Reading it here set two extra language columns on a
    typical film, in a block whose training distribution has exactly one. The code is verbatim
    to the corpus down to the absent `lower()`: the corpus wrote whatever the column held.
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
