"""The ten stages of §8's per-title pipeline, and the contract every one of them answers to.

Stages return `advance`, `park` (with `until` a defer, without one a skip) or `fail`, never raising
for an ordinary outcome. Stage 1 only ever INSERTs a new title (decision 162). No transport imports.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from spielplan.acquire import queue
from spielplan.connectors import resolve
from spielplan.connectors.jellyfin import TICKS_PER_SECOND
from spielplan.core.config import settings
from spielplan.db import dna_terms
from spielplan.derive import gate, rebuild
from spielplan.dna import craft, packs
from spielplan.dna.project import project_title
from spielplan.llm import extract
from spielplan.models import artifacts
from spielplan.models.artifacts import ArtifactStore
from spielplan.placement import reconcile
from spielplan.sources import base as sources
from spielplan.sources import credentials

log = logging.getLogger("spielplan.acquire.pipeline")

ADVANCE = "advance"
PARK = "park"
FAIL = "fail"

# `title_id_seq`'s MINVALUE, asserted after every mint.
APP_ID_MIN = 1_000_000_000

# §8 stage 1 (decision 323), shown verbatim. The park has no deadline, so the key is closed and the
# next sweep cannot re-enqueue it: the sentence names the real lever.
NO_PROVIDER_ID = (
    "no provider id: Jellyfin supplies no imdb, tmdb or tvdb id for this item, so there is "
    "nothing to mint a title against. Add the id in Jellyfin, then revive this task from the "
    "acquisition board - the queue is keyed on the item and will not re-enqueue a closed one "
    "(decision 323)"
)

# A provider id whose value is malformed; a person must edit it, so a skip.
MALFORMED_PROVIDER_ID = (
    "malformed provider id: Jellyfin reports {} for this item, which is not an id this app can "
    "mint a title against - an imdb id is tt plus at least seven digits, and a tmdb or tvdb id is "
    "a positive whole number the content spine's 32-bit columns can hold. Correct it in Jellyfin, "
    "then revive this task from the acquisition board (decision 323)"
)

# A value no content-spine column can hold (resolver binds integer ids and a smallint year); the
# database's sentence is carried inside one naming the lever.
UNUSABLE_METADATA = (
    "unusable item metadata: this item carries a value the content spine cannot hold - a tmdb or "
    "tvdb id is a 32-bit whole number and the production year is 16-bit - and the lookup refused "
    "it with \"{}\". Correct the item in Jellyfin, then revive this task from the acquisition "
    "board (decision 323)"
)

# `title.kind` admits movie or series only (§4.1 rule 5).
UNSUPPORTED_KIND = (
    "unsupported item type: this app holds movies and series only, and Jellyfin reports this "
    "item as something else (spec v2.1 §4.1 rule 5)"
)

# §3.1 allows a bundle-less install, but a placement needs a bundle; a wait, not a failure.
NO_ACTIVE_BUNDLE = (
    "no artifact bundle is active, so there is no basis to place a coordinate in. Import a "
    "bundle from Admin and this title is placed on the next drain (spec v2.1 §3.1, §8 stage 9)"
)

# The forward pass left this title without a coordinate; the nightly reconciliation may place it,
# so the park has a deadline and says it comes back by itself.
NOT_PLACED = (
    "the Cold Tower produced no coordinate for this title; §6.6's placement report says why. "
    "§5.3's nightly reconciliation places what is still unplaced, so this title is re-asked once "
    "a day and needs nothing from an operator unless that report names something"
)

# Stage 10 checks the two columns Home's shelf reads before stamping `ready`.
NOT_BADGEABLE = (
    "this title is not ready to be shown: Home's \"New in the library\" shelf needs an owned "
    "title carrying a Cold Tower placement, and this one carries {}"
)

# `_mint` writes the item id stripped but the resolver looks it up unstripped, so a padded id would
# mint a second title. Refused, not normalised: one identity rule, the resolver's.
UNCANONICAL_ITEM_ID = (
    "unusable Jellyfin item id: this item's Id carries leading or trailing whitespace, so the "
    "value this app would write as the title's deep link is not the value it looks an item up by "
    "- and a second run of this task would mint a second title for the same film rather than find "
    "the first. Correct the item in Jellyfin, then revive this task from the acquisition board "
    "(decision 323)"
)

# The resolver's None can mean "several and I won't guess"; decision 360 refuses to mint a third row.
AMBIGUOUS_IDENTITY = (
    "this film's name or original title already matches {} from {} in the library, and its "
    "provider ids match none of them - so the app cannot tell whether this item is a title the "
    "spine already holds, and a second row for one film can never be merged away (decision 162). "
    "Give the item a provider id in Jellyfin that the title it belongs to already carries, then "
    "revive this task from the acquisition board (decision 360)"
)

# Decision 411: a bundle title already placed exits at stage 1, or a library re-scan would re-walk
# (and bill) every title. Unplaced bundle titles and this pipeline's own mints still walk.
ALREADY_PLACED = (
    "this item resolves to title {}, which the corpus bundle supplied and the app has already "
    "placed, so there is nothing for the pipeline to acquire: a Jellyfin re-scan re-stamps items "
    "the household has had for years, and this was one of them (decision 411)"
)

# Two tasks for one film reached stage 1 at once; the loser waits, nothing charged.
FILM_IN_FLIGHT = (
    "another worker is already identifying this film, so this task waited rather than minting "
    "beside it. It is due again immediately and has spent no attempt (decision 322)"
)

# Advisory-lock namespace for the film-identity claim; `pipeline._TITLE_LOCK = 8001` is its sibling.
# Declared here because `pipeline` imports this module.
_MINT_LOCK = 8002

# The stub marker, one spelling.
NOT_IMPLEMENTED = "not implemented at M5.1 - owned by {}"

# §8's name for stage 2, and the `phase` every adapter registers under.
ENRICH_PHASE = "enrich"

# Decision 334's one required source; `derive/rebuild.REQUIRED_DOCUMENTS` is its stored-bytes twin.
REQUIRED_KIND = "tmdb:detail"

# Derived so the two cannot drift apart.
REQUIRED_SOURCE = REQUIRED_KIND.split(":", 1)[0]

# A failure, not a park: the driver builds the fetcher (decision 373), and a stage must never build
# its own (a second set of token buckets).
NO_FETCHER = (
    "stage 2 was handed no fetcher, so no source could be asked. Every request this app makes "
    "goes through the one rate-limited fetcher `pipeline.drain` builds per drain (spec v2.1 §8, "
    "decision 373); this is a defect in the driver rather than anything about this title"
)

# The same refusal for stage 6. ASCII: shown verbatim and printed by exit scripts.
NO_EXTRACTION_FETCHER = (
    "stage 6 was handed no fetcher, so no extraction provider could be asked. Every provider call "
    "goes through the one rate-limited fetcher `pipeline.drain` builds per drain (spec v2.1 section "
    "8, section 9, decision 373); this is a defect in the driver rather than anything about this "
    "title"
)

# Decision 334's park: the required source ran and failed; names both levers and carries the note.
ENRICH_REQUIRED_FAILED = (
    "enrichment stopped: {} is the one source §8 stage 2 requires and it answered \"{}\". The "
    "other sources were still asked and whatever they returned is in this job's detail. Check "
    "the TMDB connector in Admin, then retry this job from the acquisition board - it resumes "
    "here and re-reads what is already in the raw store (decision 334)"
)

# The required kind never asked because a sibling kind of the same source failed first.
ENRICH_REQUIRED_UNASKED = (
    "enrichment stopped: {} is the one source §8 stage 2 requires and it could not be asked, "
    "because {} answered \"{}\". The other sources were still asked and whatever they returned "
    "is in this job's detail. Check the TMDB connector in Admin, then retry this job from the "
    "acquisition board - it resumes here and re-reads what is already in the raw store "
    "(decision 334)"
)

# The shape a mint accepts: `resolve.identity` is for lookup and would let junk (`Tmdb: "0"`,
# `"tt..."` under Tmdb) become a permanent identity.
_IMDB_ID = re.compile(r"tt\d{7,}")

# Column ceilings (`integer` ids, `smallint` year) for the values `_mint` binds, so junk parks or
# drops instead of raising `DataError`.
_INT32_MAX = 2**31 - 1
_SMALLINT_MAX = 2**15 - 1

# A daily re-ask for parks that wait on a person; `queue.defer` spends no attempt.
OPERATOR_WAIT = timedelta(days=1)


def waiting_on_the_world() -> datetime:
    """The instant a park that waits on a person or a sweep comes back by itself.

    A park with no time becomes `queue.skip`, which nothing revives; use this unless that is intended.
    """
    return datetime.now(UTC) + OPERATOR_WAIT


@dataclass(frozen=True)
class Outcome:
    """What a stage did, in the three verbs the driver knows. Frozen: board and queue read one record."""

    verb: str
    reason: str = ""
    until: datetime | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    # Stage 1 only: the one fact a stage hands forward out of band.
    title_id: int | None = None
    # A `fail` only: a retry cannot change the answer (decision 431), so `queue.fail` closes the task.
    permanent: bool = False


def advance(detail: dict[str, Any] | None = None, *, title_id: int | None = None) -> Outcome:
    return Outcome(ADVANCE, detail=detail or {}, title_id=title_id)


def park(
    reason: str, *, until: datetime | None = None, detail: dict[str, Any] | None = None
) -> Outcome:
    """Waiting on something that may change (decision 336). Never auto-fails.

    With `until` the task is deferred; without it, skipped until an operator acts.
    """
    return Outcome(PARK, reason=reason, until=until, detail=detail or {})


def fail(
    reason: str, *, detail: dict[str, Any] | None = None, permanent: bool = False
) -> Outcome:
    """This stage raised and will raise again (decision 336). The only plain-retry state.

    `permanent=True` closes the task; only the admin retry runs it again (decision 431).
    """
    return Outcome(FAIL, reason=reason, detail=detail or {}, permanent=permanent)


@dataclass
class StageContext:
    """What one stage is handed: the corpus's `Ctx`, with the title added and the fetcher back.

    `title_id` is set by the driver after stage 1. `fetcher` is `Any` so this module never imports a
    transport; a stage handed none fails rather than building one.
    """

    conn: asyncpg.Connection
    task: queue.Task
    title_id: int | None = None
    run_id: int | None = None
    fetcher: Any = None
    # The drain's supply of the fetcher, handed to a paid stage so it opens only when a request is next.
    open_fetcher: Any = None

    @property
    def item(self) -> dict[str, Any]:
        """The Jellyfin item this task was enqueued for, or `{}` for a task keyed on a title."""
        payload = self.task.payload or {}
        return payload.get("item") or {}


# --- stage 1: identify --------------------------------------------------------------------------


async def identify(ctx: StageContext) -> Outcome:
    """§8 stage 1: "Jellyfin ProviderIds -> title row (fill-never-clobber); a row is MINTED only
    on a provider id (imdb/tmdb/tvdb)".

    Resolve first via `connectors/resolve` (the one identity implementation); the mint sets
    `jellyfin_id`, so a re-run resolves instead. Mints run under `_MINT_LOCK` on the film's identity
    (tried, never waited for) and refuse when the spine holds titles this item cannot be told from.
    """
    item = ctx.item
    if ctx.title_id is not None:
        # A task enqueued against an existing title; nothing to identify.
        return advance({"identified": "the task names the title"}, title_id=ctx.title_id)
    if not item:
        return fail("the task payload carries neither a Jellyfin item nor a title id")

    claims = _mint_claims(item)
    if not claims:
        # No mintable identity, so no claim: every exit below is a read or a park, in autocommit.
        return await _resolve_or_mint(ctx, item)
    async with ctx.conn.transaction():
        for claim in claims:
            if not await ctx.conn.fetchval(
                "SELECT pg_try_advisory_xact_lock($1, hashtext($2))", _MINT_LOCK, claim
            ):
                log.info("acquisition stage 1: %s is claimed by another worker; this task waits",
                         claim)
                return park(FILM_IN_FLIGHT, until=datetime.now(UTC),
                            detail={"claim": claim, "name": str(item.get("Name") or "")})
        return await _resolve_or_mint(ctx, item)


def _mint_claims(item: dict[str, Any]) -> list[str]:
    """Every identity a mint of this item would make permanent, named so two tasks agree.

    Built from provider ids (never the task key), all of them, plus kind+year+name when there is at
    least one. Alias matches are not claimed; `_indistinguishable_titles` covers that side.
    """
    kind = resolve.kind_of(item)
    if kind is None:
        return []
    mintable = _mintable_ids(item)
    claims = []
    if mintable["imdb_id"] is not None:
        claims.append(f"imdb:{mintable['imdb_id']}")
    claims += [
        f"{kind}:{column}:{mintable[column]}"
        for column in ("tmdb_id", "tvdb_id")
        if mintable[column] is not None
    ]
    if not claims:
        return []
    name = str(item.get("Name") or "").strip()
    year = _year(item)
    if name and year is not None:
        claims.append(f"{kind}:name:{name.lower()}:{year}")
    return claims


async def _resolve_or_mint(ctx: StageContext, item: dict[str, Any]) -> Outcome:
    """Stage 1's body, run under the claim `identify` takes on this item's identity."""
    try:
        found = await resolve.resolve_title_id(ctx.conn, item)
    except (asyncpg.DataError, ValueError) as exc:
        # The resolver binds unvalidated values before `_mintable_ids` runs, so a runaway or unparseable
        # id raises here; park it (a person must fix it) rather than fail four times.
        offered = resolve.provider_ids(item)
        log.info("acquisition stage 1: %s refused an item's metadata: %s",
                 type(exc).__name__, exc)
        return park(
            UNUSABLE_METADATA.format(exc),
            detail={"name": str(item.get("Name") or ""), "offered": _present(offered),
                    "refused_by": type(exc).__name__},
        )
    if found is not None:
        if await _nothing_left_to_acquire(ctx.conn, int(found)):
            # `ALREADY_PLACED`: no `title_id` on the outcome, so no board row is written over the existing
            # one.
            return park(
                ALREADY_PLACED.format(int(found)),
                detail={"name": str(item.get("Name") or ""), "title_id": int(found)},
            )
        return advance({"identified": "resolved to an existing title"}, title_id=int(found))

    kind = resolve.kind_of(item)
    if kind is None:
        return park(UNSUPPORTED_KIND, detail={"type": str(item.get("Type") or "")})

    ids = resolve.identity(item)
    if not any(v is not None for v in ids.values()):
        return park(NO_PROVIDER_ID, detail={"name": str(item.get("Name") or "")})

    mintable = _mintable_ids(item)
    if not any(v is not None for v in mintable.values()):
        offered = resolve.provider_ids(item)
        return park(
            MALFORMED_PROVIDER_ID.format(_present(offered) or "nothing usable"),
            detail={"name": str(item.get("Name") or ""), "offered": _present(offered)},
        )

    raw_id = str(item.get("Id") or "")
    if raw_id != raw_id.strip():
        # Last: a reason naming a fixable field beats one about the library's shape.
        return park(
            UNCANONICAL_ITEM_ID,
            detail={"name": str(item.get("Name") or ""), "item_id": raw_id},
        )

    collides = await _indistinguishable_titles(ctx.conn, item, kind=kind)
    if collides:
        # The last question before the one-way door (decision 360).
        return park(
            AMBIGUOUS_IDENTITY.format(
                "1 title" if collides == 1 else f"{collides} titles", _year(item)
            ),
            detail={"name": str(item.get("Name") or ""),
                    "original_title": str(item.get("OriginalTitle") or ""),
                    "year": _year(item), "indistinguishable_from": collides},
        )

    minted = await _mint(ctx.conn, item, kind=kind, ids=mintable)
    log.info(
        "acquisition stage 1: minted title %d (%s) for Jellyfin item %s",
        minted, item.get("Name") or "?", item.get("Id") or "-",
    )
    return advance({"identified": "minted", "provider_ids": _present(mintable)}, title_id=minted)


async def _nothing_left_to_acquire(conn: asyncpg.Connection, title_id: int) -> bool:
    """Decision 411's test: a bundle title that already carries a placement."""
    return bool(await conn.fetchval(
        "SELECT origin = 'bundle' AND placement <> 'unplaced' FROM title WHERE id = $1",
        int(title_id),
    ))


async def re_offered_title(conn: asyncpg.Connection, item: dict[str, Any]) -> int | None:
    """The title an item resolves to, when stage 1 would exit on it; None otherwise.

    For §7.2's feeders, which file such items below genuine adds. Same resolver and test as stage 1.
    Called outside any transaction, since a refusal is a Postgres error.
    """
    try:
        found = await resolve.resolve_title_id(conn, item)
    except (asyncpg.DataError, ValueError):
        return None
    if found is None or not await _nothing_left_to_acquire(conn, int(found)):
        return None
    return int(found)


async def _indistinguishable_titles(
    conn: asyncpg.Connection, item: dict[str, Any], *, kind: str
) -> int:
    """How many titles this mint could not be told apart from, counted to a ceiling of two.

    The resolver's None may mean "ambiguous". Probes both names this mint would write, lowered by
    Postgres like the resolver. Returns a count only: it can refuse, never resolve (decision 360).
    """
    year = _year(item)
    if year is None:
        # The arm this mirrors needs a year too.
        return 0
    names = [
        name for name in (
            str(item.get("Name") or "").strip(), str(item.get("OriginalTitle") or "").strip()
        ) if name
    ]
    if not names:
        return 0
    return int(await conn.fetchval(
        """
        WITH probe AS (SELECT lower(n) AS n FROM unnest($2::text[]) AS n)
        SELECT count(*) FROM (
            SELECT 1 FROM title t
             WHERE t.kind = $1
               AND t.year = $3
               AND (lower(t.name) IN (SELECT n FROM probe)
                    OR lower(coalesce(t.original_name, '')) IN (SELECT n FROM probe)
                    OR EXISTS (SELECT 1 FROM title_alias a
                                WHERE a.title_id = t.id AND lower(a.alias) IN (SELECT n FROM probe)))
             LIMIT 2
        ) candidates
        """,
        kind, names, year,
    ) or 0)


def _present(ids: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in ids.items() if v is not None}


def _mintable_ids(item: dict[str, Any]) -> dict[str, Any]:
    """The provider ids this item may be MINTED against: the same shape as `resolve.identity`,
    with every value that is not a well-formed id of its kind dropped.

    Reads the raw strings, not `identity`'s digits-only output, so a wrong-shaped id is refused rather
    than reinterpreted. `"0"` is refused (an unscraped NFO).
    """
    offered = resolve.provider_ids(item)
    # Not stripped: the resolver looks up the raw string, so a stripped write could never be found
    # again. Padded values fail `_IMDB_ID` into `MALFORMED_PROVIDER_ID`.
    imdb = offered.get("imdb") or ""
    mintable: dict[str, Any] = {
        "imdb_id": imdb if _IMDB_ID.fullmatch(imdb) else None,
        "tmdb_id": None,
        "tvdb_id": None,
    }
    for column, key in (("tmdb_id", "tmdb"), ("tvdb_id", "tvdb")):
        digits = (offered.get(key) or "").strip()
        # Bounded by the `integer` column. `isdecimal`, not `isdigit`: `int()` rejects superscripts.
        if digits.isdecimal() and 0 < int(digits) <= _INT32_MAX:
            mintable[column] = int(digits)
    return mintable


def _year(item: dict[str, Any]) -> int | None:
    """Jellyfin's `ProductionYear` as an int, digits only."""
    raw = item.get("ProductionYear")
    if raw is None:
        return None
    # `isdecimal` so a superscript is dropped rather than raising out of `int()`.
    digits = "".join(c for c in str(raw) if c.isdecimal())
    if not digits:
        return None
    # `smallint` bound; an out-of-range year is dropped (nullable, not an identity).
    year = int(digits)
    return year if 0 < year <= _SMALLINT_MAX else None


def _runtime_min(item: dict[str, Any]) -> int | None:
    """Jellyfin's `RunTimeTicks` in whole minutes, or None.

    Rounded (a 160.6-minute film belongs above §4.3's `runtime:>160`). Positive and bounded by the
    `integer` column; anything unparseable or out of range is dropped, never raised.
    """
    try:
        # `OverflowError` too: `int()` raises it for the `Infinity` JSON literal.
        ticks = int(item.get("RunTimeTicks") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    minutes = int(round(ticks / (TICKS_PER_SECOND * 60)))
    return minutes if 0 < minutes <= _INT32_MAX else None


async def _mint(
    conn: asyncpg.Connection, item: dict[str, Any], *, kind: str, ids: dict[str, Any]
) -> int:
    """Insert the one new `title` row §8 stage 1 is allowed to create. Returns its id.

    No `id` in the column list: the sequence default mints above `APP_ID_MIN`, asserted before commit.
    `is_owned` derives from the item carrying an `Id` (a provider-only item is not owned).
    `title_jellyfin_item` is the sweep's, not written here.
    """
    jellyfin_id = str(item.get("Id") or "").strip() or None
    async with conn.transaction():
        minted = await conn.fetchval(
            """
            INSERT INTO title (kind, name, original_name, year, runtime_min,
                               imdb_id, tmdb_id, tvdb_id, jellyfin_id,
                               is_owned, owned_checked_at, origin)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    CASE WHEN $10 THEN now() END, 'acquired')
            RETURNING id
            """,
            kind,
            str(item.get("Name") or "").strip() or "(untitled)",
            (str(item.get("OriginalTitle") or "").strip() or None),
            _year(item),
            _runtime_min(item),
            ids["imdb_id"], ids["tmdb_id"], ids["tvdb_id"],
            jellyfin_id,
            # The stamp goes with the flag.
            jellyfin_id is not None,
        )
        if int(minted) < APP_ID_MIN:
            raise RuntimeError(
                f"title {minted} was minted below the app's id range ({APP_ID_MIN}): "
                "title_id_seq has been repositioned or the column default is gone, and §4.1's "
                "partition is the only thing keeping an acquired title from colliding with a "
                "corpus one (0015_seed.sql:20-25, decision 162)"
            )
    return int(minted)


# --- stages 2, 3 and 4: the crawl, the derive and the gate ---------------------------------------


async def _capabilities(conn: asyncpg.Connection) -> dict[str, bool]:
    """Which of the three keyed sources this install has configured. Decision 377's narrow read.

    Unconfigured keyed sources are filtered out, so a half-configured install is legal (§3.1).
    """
    return {
        credentials.TMDB: await credentials.tmdb_auth(conn) is not None,
        credentials.OMDB: await credentials.omdb_key(conn) is not None,
        credentials.TRAKT: await credentials.trakt_headers(conn) is not None,
    }


async def enrich(ctx: StageContext) -> Outcome:
    """§8 stage 2: "tmdb:resolve -> tmdb:detail ... wikidata:resolve ... rt:page,
    metacritic:page->reviews" (`spec:365-368`), each through the one polite fetcher.

    Order is the registry's `default_priority`. Only a failure of the required source that actually
    ran parks (decision 334), with a deadline; every other failure, a raise or a paused host
    included, is that source's note (decision 422).
    """
    if ctx.title_id is None:
        return fail("stage 2 reached with no title id; stage 1 did not establish one")
    if ctx.fetcher is None:
        return fail(NO_FETCHER)

    # Loaded here, not at import: the adapters import `acquire.fetch`, which would close a cycle.
    sources.load_all()
    capabilities = await _capabilities(ctx.conn)
    # Stage 2 is not a paid stage, so no paid kind may run inside it.
    wanted = sources.available_kinds(capabilities, ENRICH_PHASE, include_paid=False)

    answered: list[str] = []
    notes: dict[str, str] = {}
    # Kinds that returned without a request (`SourceResult.ran`). An adapter that raised is in neither
    # set and so counts as having run.
    unasked: set[str] = set()
    documents = 0
    for kind in wanted:
        spec = sources.REGISTRY[kind]
        try:
            result = await spec.fn(ctx)
        except Exception as exc:                                         # noqa: BLE001
            log.warning("acquisition source %s raised for title %s", kind, ctx.title_id,
                        exc_info=True)
            notes[kind] = f"{type(exc).__name__}: {exc}"
            continue
        if result.doc_id is not None:
            documents += 1
        if not result.ran:
            unasked.add(kind)
        if result.ok:
            answered.append(kind)
            if result.note:
                notes[kind] = result.note
        else:
            notes[kind] = result.note or "no answer"

    # Unrunnable kinds are reported by name, based on `requires` alone.
    for kind, spec in sorted(sources.REGISTRY.items()):
        if spec.phase != ENRICH_PHASE or kind in wanted:
            continue
        if spec.requires and not capabilities.get(spec.requires, False):
            notes[kind] = f"{spec.requires} is not configured, so this source was not asked"

    detail: dict[str, Any] = {"answered": answered, "documents": documents}
    if notes:
        detail["notes"] = notes
    # Park only when a kind of the required source ran and failed; name the earliest that failed.
    failed = [kind for kind in wanted if kind not in answered and kind not in unasked]
    required = [kind for kind in failed if kind.split(":", 1)[0] == REQUIRED_SOURCE]
    if REQUIRED_KIND in wanted and REQUIRED_KIND not in answered and required:
        blame = REQUIRED_KIND if REQUIRED_KIND in required else required[0]
        note = notes.get(blame, "no answer")
        return park(
            ENRICH_REQUIRED_FAILED.format(REQUIRED_KIND, note) if blame == REQUIRED_KIND
            else ENRICH_REQUIRED_UNASKED.format(REQUIRED_KIND, blame, note),
            until=waiting_on_the_world(),
            detail=detail,
        )
    return advance(detail)


async def derive(ctx: StageContext) -> Outcome:
    """§8 stage 3: "per-title parse of raw docs -> title_meta/credit/review/...", ending by
    applying both curated ledgers, corrections last (`spec:375-379`, §14.5).

    Reads the raw store, never fetches. The ledger counts go on the board. A missing title raises.
    """
    if ctx.title_id is None:
        return fail("stage 3 reached with no title id; stage 1 did not establish one")
    report = await rebuild.derive_title(ctx.conn, ctx.title_id)
    detail: dict[str, Any] = {
        "documents": len(report.documents),
        "sources": list(report.sources),
        "rows": dict(report.rows),
        "people": report.people,
        "adjudications": dict(report.adjudications),
        "corrections": dict(report.corrections),
    }
    if report.refused:
        detail["refused"] = list(report.refused)
    return advance(detail)


async def reviews_gate(ctx: StageContext) -> Outcome:
    """§8 stage 4: "pack requires plot + multi-source reviews >=50 words; if thin, retry window 30
    days (new releases accrue reviews over weeks)" (`spec:380-381`).

    The predicate is `derive/gate.py`'s. The park's deadline is the window, and the expired park
    re-enters at stage 2 (`reask_from`, decision 421); the driver writes that one instant to both tables.
    """
    if ctx.title_id is None:
        return fail("stage 4 reached with no title id; stage 1 did not establish one")
    counts = await gate.measure(ctx.conn, ctx.title_id)
    detail: dict[str, Any] = {
        "plot": counts.has_plot, "sources": counts.sources, "words": counts.words,
    }
    if gate.passes(counts):
        return advance(detail)
    return park(gate.reason(counts), until=gate.window_deadline(), detail=detail)


# --- stage 5: the pack, with its craft supplement ------------------------------------------------

# A bundle-less install at stage 5: no vocabulary version to store a pack under. ASCII.
NO_PACK_VOCABULARY = (
    "no DNA vocabulary is active on this install, so there is no version to store this title's "
    "pack under and nothing that could verify an extraction from it (section 3.1: a bundle-less "
    "install is a legal state). Import the bundle, and this title resumes here (decision 461)"
)


async def dna_pack(ctx: StageContext) -> Outcome:
    """§8 stage 5: "ported packs.py (interleaving, caps, norm()) + craft supplement".

    Stores the augmented pack stage 6 reads, with `chars`/`sha` recomputed from the augmented text (or
    `store_pack` refuses it). One transaction, filed under `ctx.task.key`. No vocabulary parks with a
    deadline; a missing title fails. Neither paid nor fetching.
    """
    if ctx.title_id is None:
        return fail("stage 5 reached with no title id; stage 1 did not establish one")
    version = await dna_terms.active_version(ctx.conn)
    if version is None:
        return park(NO_PACK_VOCABULARY, until=waiting_on_the_world(), detail={"version": None})
    built = await packs.build_pack(ctx.conn, ctx.title_id)
    if built is None:
        return fail(f"title {ctx.title_id} no longer exists")
    text, base = built
    augmented, craft_info = await craft.augment(ctx.conn, ctx.title_id, text)
    # The augmented text's own length and digest.
    info = replace(base, chars=len(augmented), sha=packs.sha(augmented))
    async with ctx.conn.transaction():
        document = await packs.store_pack(
            ctx.conn, ctx.title_id, version, augmented, info,
            entity_key=ctx.task.key, run_id=ctx.run_id,
        )
    return advance({
        "version": version, "pack_sha": info.sha, "chars": info.chars,
        "base_chars": craft_info.base_chars, "n_reviews": info.n_reviews,
        "n_sources": info.n_sources, "raw_document_id": document,
        "wiki_chars": craft_info.wiki_chars, "n_sections": craft_info.n_sections,
        "n_rt": craft_info.n_rt,
    })


# Read off the payload mark `pipeline._record_stop` writes, not off attempt counts: a permanent
# failure on the last attempt looks like exhaustion.
FAILED_FOR_GOOD_MARK = "failed_for_good"
_FAILED_FOR_GOOD = (
    "SELECT key FROM acquisition_task"
    " WHERE kind = $1 AND state = $2 AND id <> $3 AND payload ->> 'title_id' = $4"
    f"   AND (attempts < max_attempts OR payload ->> '{FAILED_FOR_GOOD_MARK}' = 'true')"
    " ORDER BY updated_at DESC LIMIT 1"
)
FAILED_FOR_GOOD = (
    "stage 6's extraction failed for good for this title under task {key}, and decision 431 makes an "
    "admin retry of that task the only way back. This task waits rather than paying for the same "
    "extraction again, and resumes by itself once that task is retried"
)

# The payload keys a flywheel launch writes (decision 443); the plan has three readers that must
# agree (decision 442).
PLAN_KEY = "plan"
BATCH_KEY = "flywheel_batch"


def task_plan(task: queue.Task | None) -> Any:
    """The batch plan a task carries, or None when it carries none - the one reader of `PLAN_KEY`.

    A malformed plan is passed on for `llm/spend` to refuse, never read as None.
    """
    if task is None:
        return None
    return (task.payload or {}).get(PLAN_KEY)


async def dna_extract(ctx: StageContext) -> Outcome:
    """§8 stage 6: the LLM structured call(s).

    The cap is checked by the driver's gate before this runs. `llm/extract` reaches the verdict; this
    maps its status to a verb (decision 431): written advances; no pack, no vocabulary, plan and
    account refusals park with a deadline; violations and final refusals fail permanently; transient
    failures stay on the curve; a breaker pause parks.
    """
    if ctx.title_id is None:
        return fail("stage 6 reached with no title id; stage 1 did not establish one")
    closed = await ctx.conn.fetchval(_FAILED_FOR_GOOD, ctx.task.kind, queue.FAILED, ctx.task.id,
                                     str(ctx.title_id))
    if closed is not None:
        # Permanence is the title's (decision 431): another key of a title that failed for good parks
        # instead of buying the attempts again.
        return park(FAILED_FOR_GOOD.format(key=closed), until=waiting_on_the_world(),
                    detail={"failed_for_good_under": closed})
    if ctx.fetcher is None and ctx.open_fetcher is None:
        return fail(NO_EXTRACTION_FETCHER)
    # The same plan reader the gate used (decision 442).
    extraction = await extract.extract_title(
        ctx.conn, title_id=ctx.title_id, fetcher=ctx.fetcher, task_key=ctx.task.key,
        run_id=ctx.run_id, open_fetcher=ctx.open_fetcher, batch=task_plan(ctx.task),
    )
    status = extraction.status
    if status == extract.WRITTEN:
        return advance({**extraction.detail, "tags": extraction.n_tags, "calls": extraction.calls})
    # Account refusals lift by themselves, so park rather than fail.
    if status in (extract.NO_PACK, extract.NO_VOCABULARY, extract.PLAN, extract.ACCOUNT):
        return park(extraction.reason, until=waiting_on_the_world(), detail=extraction.detail)
    # A pause met before anything was billed parks until it ends, attempt refunded.
    if status == extract.PAUSED:
        paused = float(extraction.detail.get("paused_for_s") or 0.0)
        return park(extraction.reason, until=datetime.now(UTC) + timedelta(seconds=max(paused, 1.0)),
                    detail=extraction.detail)
    if status in (extract.VIOLATED, extract.REFUSED):
        return fail(extraction.reason, detail=extraction.detail, permanent=True)
    if status == extract.TRANSIENT:
        return fail(extraction.reason, detail=extraction.detail)
    return fail(f"stage 6's extraction answered {status!r}, which this stage maps to no verb")


# --- stages 7 and 8: the verdict recorded, and the projection -------------------------------------


async def verify(ctx: StageContext) -> Outcome:
    """§8 stage 7: "ported trust boundary verbatim ... Failures drop, never repaired" - reached
    inside stage 6 and recorded here (decision 462).

    Records the extracted-tier count and this run's refusals by rule (`run_id = $2`, deliberately not
    null-safe) into the board's detail. Decides nothing, calls nothing, writes no row.
    """
    if ctx.title_id is None:
        return fail("stage 7 reached with no title id; stage 1 did not establish one")
    version = await dna_terms.active_version(ctx.conn)
    tags = await ctx.conn.fetchval(
        "SELECT count(*) FROM dna_tag WHERE title_id = $1 AND version = $2", ctx.title_id, version
    )
    rejected = await ctx.conn.fetch(
        "SELECT rule_violated, count(*) AS n FROM dna_reject"
        " WHERE title_id = $1 AND run_id = $2 GROUP BY rule_violated ORDER BY rule_violated",
        ctx.title_id, ctx.run_id,
    )
    return advance({
        "version": version, "tags": int(tags),
        "rejected": {row["rule_violated"]: int(row["n"]) for row in rejected},
    })


# A bundle title's projected rows are seeded content (decision 162); not re-derived.
BUNDLE_PROJECTION_KEPT = (
    "a bundle title's projected rows are seeded once by the bundle import (decision 162), so this "
    "stage leaves them as they are and projects nothing (decision 463)"
)


async def project(ctx: StageContext) -> Outcome:
    """§8 stage 8: "per-title alias-map projection of its keywords (incremental - new code, same
    alias map) for an acquired title".

    Bundle titles also reach this stage; they advance without projecting (decision 162). The thin-facet
    observation is the driver's, on both branches.
    """
    if ctx.title_id is None:
        return fail("stage 8 reached with no title id; stage 1 did not establish one")
    origin = await ctx.conn.fetchval("SELECT origin FROM title WHERE id = $1", ctx.title_id)
    if origin is None:
        return fail(f"title {ctx.title_id} no longer exists")
    if origin != "acquired":
        return advance({"origin": origin, "kept": BUNDLE_PROJECTION_KEPT})
    return advance({"origin": origin, "projected": await project_title(ctx.conn, ctx.title_id)})


# --- stage 9: place -----------------------------------------------------------------------------


async def active_store(conn: asyncpg.Connection) -> ArtifactStore | None:
    """The basis stage 9 places against, via `worker.py`'s three steps in the same order.

    A broken install raises; a bundle-less one returns None. The drain job is a model job because of this.
    """
    store = await ArtifactStore.load_active(conn, settings().artifacts_dir)
    store.assert_not_broken()
    store.assert_matches(await artifacts.active_bundle_version(conn))
    return None if store.is_empty else store


async def place(ctx: StageContext) -> Outcome:
    """§8 stage 9: "feature vector per the feature contract -> Cold Tower -> e(t), b(t)".

    Calls `reconcile` with scope `app_acquired` (every acquired title; idempotent upserts), or the
    sweep's scope for a bundle title. Reads the title back: a report count does not say this one placed.
    """
    if ctx.title_id is None:
        return fail("stage 9 reached with no title id; stage 1 did not establish one")
    store = await active_store(ctx.conn)
    if store is None:
        # With a deadline; the bundle-less re-ask is the cheapest walk there is.
        return park(NO_ACTIVE_BUNDLE, until=waiting_on_the_world())

    # A bundle title is not in `app_acquired`'s list, so use the nightly sweep's scope now rather than
    # at 03:00.
    origin = await ctx.conn.fetchval("SELECT origin FROM title WHERE id = $1", ctx.title_id)
    scope = "app_acquired" if origin == "acquired" else "owned_missing"
    report = await reconcile.reconcile(ctx.conn, store, scope=scope)
    placement = await ctx.conn.fetchval(
        "SELECT placement FROM title WHERE id = $1", ctx.title_id
    )
    detail = {
        "scope": report.scope,
        "considered": report.considered,
        "placed": report.placed,
        "bundle_version": store.version,
    }
    if placement not in ("cold_tower", "warm"):
        # With a deadline: the condition can clear itself overnight.
        return park(NOT_PLACED, until=waiting_on_the_world(),
                    detail=detail | {"notes": report.notes[:4]})
    return advance(detail | {"placement": placement})


# --- stage 10: ready ----------------------------------------------------------------------------


async def ready(ctx: StageContext) -> Outcome:
    """§8 stage 10: "appears in ranking/search/explore with a 'new - model placement, no crowd
    data' badge until ratings accrue".

    Checks the two columns Home's shelf reads before `ready` is stamped; the board write is the driver's.
    """
    if ctx.title_id is None:
        return fail("stage 10 reached with no title id; stage 1 did not establish one")
    row = await ctx.conn.fetchrow(
        "SELECT is_owned, placement FROM title WHERE id = $1", ctx.title_id
    )
    if row is None:
        return fail(f"title {ctx.title_id} no longer exists")
    if not row["is_owned"] or row["placement"] not in ("cold_tower", "warm"):
        missing = []
        if not row["is_owned"]:
            missing.append("no ownership flag")
        if row["placement"] not in ("cold_tower", "warm"):
            missing.append(f"placement {row['placement']!r}")
        # With a deadline: both columns are set by timed jobs.
        return park(NOT_BADGEABLE.format(" and ".join(missing)), until=waiting_on_the_world())
    return advance({"badge": "new - model placement, no crowd data", "placement": row["placement"]})
