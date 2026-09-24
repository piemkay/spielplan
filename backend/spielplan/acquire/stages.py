"""The ten stages of §8's per-title pipeline, and the contract every one of them answers to.

Spec v2.1 §8 (the ten-stage pipeline and "Failure at any stage parks the job with a reason,
retryable from admin"), §4.1 (the id partition), §5.3 (placement reconciliation), §6.0 row 6.

PORT VERDICT: **new code, shaped by the corpus and matching none of its units.** The corpus has
no per-title stage machine at all. `mdc probe` (`mdc/cli.py:813-897`) enqueues one task per
SOURCE kind and drains them in any order, because in a wholesale crawl the order between two
fetchers does not matter; §8's pipeline is a SEQUENCE - "per-title parse of raw docs", then a
gate, then a pack, then an extraction, then a placement - where stage n+1 reads what stage n
wrote. What IS ported, with named changes, is the corpus's handler contract:

  * `mdc/sources/base.py:41-54`'s `HandlerSpec` - a callable, plus the metadata the driver needs
    to decide whether to run it at all, including `paid`. Changed: the unit of registration is a
    STAGE of one pipeline rather than a task KIND of many, so the registry is an ordered tuple
    in `pipeline.py` and not a dict keyed by name.
  * `mdc/sources/base.py:23-34`'s `Ctx` - the per-run handle a stage is given. Changed: the
    `fetcher` is a HANDLE THIS MODULE NEVER NAMES THE TYPE OF, and `title_id` is on it, because
    a stage of a per-title pipeline always has a title while a corpus handler only has a key.
    M5.1 left the fetcher off entirely - "nothing fetches at M5.1, and `acquire/fetch.py` is
    deliberately not imported here so this module does not depend on a layer it does not use" -
    and decision 373 puts it back as a field the DRIVER fills: `pipeline.drain` builds one per
    drain and hands it down, so §8 stage 2 drives eleven source adapters without this file
    importing a transport, an HTTP client or one of the fetcher's exception types. That is why
    `StageContext.fetcher` is annotated `Any` and why every failure an adapter can suffer
    arrives here as a `sources.base.SourceResult` rather than as an exception to catch.
  * `mdc/runner.py:161-205`'s `_execute` - the mapping from what a handler did to what the queue
    is told. Changed from EXCEPTIONS to RETURN VALUES: the corpus raises `Skip`, `Permanent`,
    `HostPaused`, `RobotsDisallowed` and `FetchError` and catches five arms in the runner, which
    works when every handler is one HTTP fetch and its failure modes are the fetcher's. §8's
    stages fail in ways that are not exceptional at all - "if thin, retry window 30 days" is the
    normal outcome of stage 4 on a film released last week - and a normal outcome raised as an
    exception is one a later `except Exception` swallows into a failure. So a stage RETURNS
    `advance` / `park` / `fail` and the driver keeps one `except Exception` arm for the case a
    return value cannot describe: a stage that genuinely broke.

THE THREE VERBS, and decision 336 is the line between the last two.

  * `advance` - this stage is done; the next one may run. Optionally carries the `title_id`
    stage 1 established, which is the only fact a stage hands forward out of band.
  * `park(reason, until=None)` - "waiting on something that may change": the 30-day review
    window, the spend cap, thin-block enrichment, a bundle-less install. It NEVER auto-fails.
    With a time it is a `defer` on the task; without one it is a `skip`, because nothing will
    change without an operator. `reason` is shown VERBATIM on §6.6's board, so it is written for
    a person and not for a log.
  * `fail(reason)` - "this stage raised and will raise again". The only state offering a plain
    retry, and the driver also writes it for an unhandled exception, because an exception is
    exactly that sentence.

WHAT MAKES EVERY WRITE IN THIS FILE SAFE, stated because decision 162 makes it unrecoverable if
it is not: the corpus is no longer somewhere the content can be fetched from again, so a
corrupted write into `title` and its derived tables can only be undone by dropping the database.
Three properties, each of which a later reader must keep:

  1. **A mint is an INSERT of a NEW row and never an UPDATE of an existing one.** `_mint` writes
     no `id`, so `title_id_seq`'s DEFAULT applies (`0015_seed.sql:24-27`, `MINVALUE 1000000000`),
     and the row lands in the app's half of the namespace where no bundle can ever collide with
     it. The insert asserts that before its transaction commits: a sequence that was reset by
     hand, or a column default someone dropped, would otherwise mint quietly into the corpus's
     half and overwrite a bundle title on the next import. `origin = 'acquired'`
     (`0008_placement.sql:46-48`) is what makes the row distinguishable afterwards.
  2. **No stage in this file UPDATES a title the bundle imported.** `identify` READS one
     through `connectors/resolve.resolve_title_id` and never writes it: that function is
     fill-never-clobber by construction and this module does not extend it, because §7.1's rule is
     that "the bundle is derived from a curated corpus and Jellyfin's ProviderIds are whatever a
     scraper guessed; when they disagree the corpus wins" (`connectors/resolve.py:15-17`). And it
     never re-implements identity either, which is the same property from the other side: a second
     resolver here would disagree with the nightly sweep about which title a library item is.
     THIS PARAGRAPH USED TO SAY THE NARROWER AND FALSE THING - that `identify` is the only stage
     that touches a title it did not mint, and that every other write here is an UPDATE of a row
     this pipeline owns. `place` calls `reconcile(scope="app_acquired")`, whose work list is
     `SELECT id FROM title WHERE origin = 'acquired'` - EVERY acquired title in the install - and
     which UPSERTs a `title_placement` row and runs an unguarded
     `UPDATE title SET placement, placement_bundle, placement_at` over all of them
     (`placement/reconcile.py:233-247`, `:334-338`). So one task's stage 9 rewrites rows this walk
     did not mint and does not hold `_TITLE_LOCK` for. What makes THAT safe is stated where it is
     done, in `place`'s own docstring, and it is a different argument: the write is derived rather
     than curated, it is idempotent under `ON CONFLICT (title_id, bundle_version) DO UPDATE`, it is
     scoped by `origin = 'acquired'` so no corpus title is reachable, and it is computed against a
     basis `active_store` has already asserted matches the active bundle. The list a stage author
     for M5.2-M5.7 reads before adding a write has to name it.
     [M5.1 review cycle 3, d323-C3-SPINE-03]
  3. **A minted row is written in ONE transaction with its assertion**, so a refusal leaves no
     half-minted title - and that transaction also holds the claim on the film's identity, so two
     workers cannot both discover there is no such title and both create one. Every other write
     this file makes ITSELF is an UPDATE of a row this pipeline owns.
     WHICH IDENTITIES THAT CLAIM COVERS IS `_mint_claims`' OWN DOCSTRING AND NOT THIS LINE. The
     sentence above was true of the resolver's provider arms and false of its fourth, and two
     items for one film with disjoint provider ids both minted through the gap; the claim set now
     mirrors the name arm too and names the arms it still does not mirror. A property asserted
     here and enforced somewhere else is a property worth reading in the place that enforces it.
     [M5.1 review cycle 4, d322-C4-MINT-01]

WHY STAGES 5-8 ARE DECLARED NO-OPS AND NOT MISSING. Each advances and records
`not implemented at M5.1 - owned by M5.<n>` in the board's `detail`, and names its owner in its
own docstring. That is what makes the spine testable end to end before any lane opens - a task
can walk 1 to 9 to 10 today and prove the driver, the queue and the two shipped stages agree -
and it is what makes a stub that survives into M5.6 visible to a `grep` rather than invisible in
a registry table. The owners are the roadmap's, not guesses: `:298-312` puts the pack, the trust
boundary and the projection in M5.4; `:314-329` puts the LLM extraction in M5.5.
THIS PARAGRAPH SAID "2-8" UNTIL M5.3 GAVE THREE OF THEM BODIES (`ROADMAP-M5.md:279-296`, the
eight source adapters, the parsers and the reviews gate). The count is restated rather than left
to be read off the tuple because `pipeline.STAGES`' `implemented` flag is a hand-written literal
and this sentence is the prose half of the same claim - and because four is now the number
`test_every_stage_declared_a_no_op_returns_its_stub_marker` asserts.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from spielplan.acquire import queue
from spielplan.connectors import resolve
from spielplan.connectors.jellyfin import TICKS_PER_SECOND
from spielplan.core.config import settings
from spielplan.derive import gate, rebuild
from spielplan.models import artifacts
from spielplan.models.artifacts import ArtifactStore
from spielplan.placement import reconcile
from spielplan.sources import base as sources
from spielplan.sources import credentials

log = logging.getLogger("spielplan.acquire.pipeline")

ADVANCE = "advance"
PARK = "park"
FAIL = "fail"

# `title_id_seq`'s MINVALUE, restated here as an assertion rather than imported from a migration
# nothing loads at runtime. `0015_seed.sql:20-25` states what it buys: "A disjoint range makes the
# collision arithmetically impossible instead of contingent on the corpus standing still."
APP_ID_MIN = 1_000_000_000

# §8 stage 1's own clause, as the spec file now carries it (decision 323): an item Jellyfin
# supplies no provider id for "parks here with the reason 'no provider id' and mints nothing,
# because a name-and-year mint is the wrong match the resolver already refuses". The reason OPENS
# with the spec's three words because §6.6 shows it verbatim and an operator scanning a board
# column reads the first clause; the rest is there so the one person who can act on it knows
# what acting would mean. `ops/fake_jellyfin.py:52-53`'s "Tampopo" is this fixture.
# THE SECOND SENTENCE IS WHAT THE MACHINE ACTUALLY DOES, and it used to say "the next sweep will
# enqueue it again". It cannot: this park carries no deadline, so the task is `skipped`, and
# `queue.enqueue` is `ON CONFLICT (kind, key) DO NOTHING` - the sweep's next pass over the same
# Jellyfin item is a no-op against the closed row. A reason shown verbatim to an operator has to
# name the lever that exists. [M5.1 review cycle 1, M51-CRASH-01]
NO_PROVIDER_ID = (
    "no provider id: Jellyfin supplies no imdb, tmdb or tvdb id for this item, so there is "
    "nothing to mint a title against. Add the id in Jellyfin, then revive this task from the "
    "acquisition board - the queue is keyed on the item and will not re-enqueue a closed one "
    "(decision 323)"
)

# An item that carries a provider id Jellyfin's own field names promise and the value denies.
# A skip and not a deferral: nothing changes here without a person editing the id, which is the
# same shape as `NO_PROVIDER_ID` and is what `park`'s docstring calls the honest outcome.
MALFORMED_PROVIDER_ID = (
    "malformed provider id: Jellyfin reports {} for this item, which is not an id this app can "
    "mint a title against - an imdb id is tt plus at least seven digits, and a tmdb or tvdb id is "
    "a positive whole number the content spine's 32-bit columns can hold. Correct it in Jellyfin, "
    "then revive this task from the acquisition board (decision 323)"
)

# An item carrying a value no column in the content spine can hold. Distinct from
# `MALFORMED_PROVIDER_ID` because the cause is not always a provider id: `resolve.resolve_title_id`
# binds `tmdb_id`/`tvdb_id` (`integer`) AND the name-and-year fallback's `t.year` (`smallint`), so
# the same refusal arrives for an item whose `ProductionYear` is junk, and a reason shown verbatim
# on §6.6's board must not name the wrong field. The database's own sentence is carried in `{}`
# rather than paraphrased, because it is the only thing that says which value was refused - and
# it is carried INSIDE a sentence that names the lever rather than being the whole reason, which
# is what this park used to be. [M5.1 review cycle 2, d323-int32-04]
UNUSABLE_METADATA = (
    "unusable item metadata: this item carries a value the content spine cannot hold - a tmdb or "
    "tvdb id is a 32-bit whole number and the production year is 16-bit - and the lookup refused "
    "it with \"{}\". Correct the item in Jellyfin, then revive this task from the acquisition "
    "board (decision 323)"
)

# An item that is neither a Movie nor a Series. `title.kind` is `NOT NULL CHECK (kind IN
# ('movie','series'))` (`0003_content.sql:29`) and §4.1 rule 5 makes that column the partition
# every ranking surface uses, so there is no honest value to write for a Box Set, a Playlist or a
# music video. A park rather than a failure because nothing raised and no retry can help.
UNSUPPORTED_KIND = (
    "unsupported item type: this app holds movies and series only, and Jellyfin reports this "
    "item as something else (spec v2.1 §4.1 rule 5)"
)

# §3.1 makes a bundle-less install legal, and `title_placement.bundle_version` is `NOT NULL
# REFERENCES artifact_bundle(version)` (`0008_placement.sql:14`), so an acquired title on such an
# install genuinely cannot be placed. That is a wait and not a failure: importing a bundle is the
# thing that may change, and it is a thing the household does.
NO_ACTIVE_BUNDLE = (
    "no artifact bundle is active, so there is no basis to place a coordinate in. Import a "
    "bundle from Admin and this title is placed on the next drain (spec v2.1 §3.1, §8 stage 9)"
)

# The stage got its forward pass and the title still has no coordinate. `place_titles` reports a
# non-finite placement per title rather than raising (`placement/reconcile.py:298-301`), so the
# only way to learn that THIS title was the one that failed is to read the row back.
#
# NAMES THE LEVER, like `NO_ACTIVE_BUNDLE` above and for the same reason: this park carries a
# deadline (see `place`), so what an operator reads has to say that the title comes back by itself.
# §5.3's nightly reconciliation runs `scope="owned_missing"`, whose work list is origin-blind and
# is exactly the owned titles still reading `placement = 'unplaced'`, so the condition this park
# describes is one that clears itself. [M5.1 review cycle 2, d323-park-02]
NOT_PLACED = (
    "the Cold Tower produced no coordinate for this title; §6.6's placement report says why. "
    "§5.3's nightly reconciliation places what is still unplaced, so this title is re-asked once "
    "a day and needs nothing from an operator unless that report names something"
)

# Stage 10 badges a title on the strength of two columns, and if either is missing the badge is a
# claim about a title that has none. `home/shelves.py:1015-1053`'s shelf reads `t.is_owned` and
# `t.placement = 'cold_tower'`, so a title missing either is stamped `ready` into a board an
# operator then cannot reconcile with a Home page that does not show it.
NOT_BADGEABLE = (
    "this title is not ready to be shown: Home's \"New in the library\" shelf needs an owned "
    "title carrying a Cold Tower placement, and this one carries {}"
)

# An item whose identity would be WRITTEN in a form the resolver cannot LOOK UP again. `_mint`
# writes `str(item["Id"]).strip()`; `connectors/resolve.resolve_title_id` looks the same item up
# with `str(item["Id"])`, unstripped. So an item id carrying surrounding whitespace mints a title
# the next walk of this very task cannot find, and `_remember_title` is a separate autocommit
# statement after the mint's transaction commits - a window `_run_stage`'s own `asyncio.wait_for`
# budget can land in with no worker dying at all. The second walk mints a second title for one
# film, and decision 162 makes it permanent.
#
# REFUSED AND NOT NORMALISED, which is decision 323's side of the door. Canonicalising the item
# here and handing THAT to the resolver would make this module and the nightly sweep resolve one
# item differently, which is the second identity implementation property 2 of this file's header
# forbids. The same rule covers the imdb id one function below, where it needs no code: dropping
# the `.strip()` lets `_IMDB_ID.fullmatch` refuse a padded value into `MALFORMED_PROVIDER_ID`.
# `tmdb`/`tvdb` need no rule and get none - `resolve._as_int` reduces the LOOKUP side to digits
# exactly as `_mintable_ids` reduces the WRITE side, so those two agree already.
# [M5.1 review cycle 3, d323-C3-MINT-01]
UNCANONICAL_ITEM_ID = (
    "unusable Jellyfin item id: this item's Id carries leading or trailing whitespace, so the "
    "value this app would write as the title's deep link is not the value it looks an item up by "
    "- and a second run of this task would mint a second title for the same film rather than find "
    "the first. Correct the item in Jellyfin, then revive this task from the acquisition board "
    "(decision 323)"
)

# The resolver returned "no match" and this walk asked WHY before writing. `resolve_title_id`'s
# fourth arm takes `LIMIT 2` and answers only for exactly one candidate (`connectors/resolve.py:
# 161-179`), so its None means one of two different things - "the spine holds nothing like this"
# and "the spine holds several and I will not guess" - and stage 1 read both as licence to mint.
# Decision 360 is the refusal: when the app cannot tell an item from titles it already holds, it
# writes no third row. A park is recoverable and a mint is not, and that asymmetry is decision
# 162's whole content. Names the lever, because a provider id is a thing an operator can add in
# Jellyfin and is exactly what makes the resolver's stronger arms answer.
# [M5.1 review cycle 4 second pass, M51-C4-MINT-AMBIG-01, M51-C4-MINT-ORIGINAL-03]
AMBIGUOUS_IDENTITY = (
    "this film's name or original title already matches {} from {} in the library, and its "
    "provider ids match none of them - so the app cannot tell whether this item is a title the "
    "spine already holds, and a second row for one film can never be merged away (decision 162). "
    "Give the item a provider id in Jellyfin that the title it belongs to already carries, then "
    "revive this task from the acquisition board (decision 360)"
)

# An item that resolves to a title the bundle supplied and the app has already PLACED, which is
# M5.2-plan §9's risk made into an exit: "A library re-scan can re-stamp items the household has
# had for years ... the job must then find the title already owned and exit at stage 1 rather
# than re-acquiring. Assert that, or a re-scan bills the household for its whole library."
# Nothing exited. A resolved title advanced and `pipeline.run_task` resumed it from its board
# row, so a re-stamped owned title walked stages 2 to 10 -- stubs today, which stamped §8's "new
# - model placement, no crowd data" badge over warm corpus titles and wrote `ready` over the
# `(2, parked)` row `placement/reconcile._park_thin` leaves for a thin title, a row `_PARK`'s
# `ON CONFLICT DO NOTHING` can never put back; and the day M5.3 and M5.5 give those stages bodies,
# eight-source enrichment and the paid extract for every title a re-scan touched.
#
# A BUNDLE TITLE ALREADY PLACED IS THE TEST, and not "owned", which is decision 411's reading of
# the plan's word. `is_owned` is the full sweep's column and moves on the sweep's schedule, so it
# cannot say whether §8 has anything left to do. Two columns can: `origin = 'bundle'` says the
# curated corpus already did this title's acquisition, and a placement says stages 2 to 9 have
# nothing left to produce. Each half keeps a walk M5.1 built on purpose. A bundle title still
# `unplaced` -- a Jellyfin add the nightly reconciliation has not reached -- walks to stage 9 and
# waits there exactly as `NOT_PLACED` argues. A title THIS PIPELINE minted resumes from its own
# board row, because that row is unfinished work of the pipeline's: a reclaim after a worker died
# between the mint and `_remember_title`, and the loser of a copy race resolving onto the winner's
# row, both finish that way. A thin title's `(2, parked)` inbox row is left for the `title:` task
# whose job it is. A skip with no board row, because the title's row belongs to whichever walk
# holds it and this task established no title of its own. [M5.2 review cycle 3: m52-c3-own-01]
ALREADY_PLACED = (
    "this item resolves to title {}, which the corpus bundle supplied and the app has already "
    "placed, so there is nothing for the pipeline to acquire: a Jellyfin re-scan re-stamps items "
    "the household has had for years, and this was one of them (decision 411)"
)

# Two tasks for one film reached stage 1 at once, and the one that lost the claim waited. Decision
# 336's `parked`, with a deadline of now: the thing that may change is the other walk committing,
# which it will, and nothing was attempted so nothing is charged. `pipeline.TITLE_IN_FLIGHT` is
# the same sentence one step later, for a title that already exists.
FILM_IN_FLIGHT = (
    "another worker is already identifying this film, so this task waited rather than minting "
    "beside it. It is due again immediately and has spent no attempt (decision 322)"
)

# The namespace half of the advisory lock stage 1 mints under, and a DIFFERENT one from
# `pipeline._TITLE_LOCK = 8001` because the thing being claimed is different: that one is a title
# id, this one is the identity of a film that may not have a row yet. The two must not share a
# namespace or a title id could collide with a hashed provider id on the same integer, which is
# the hazard `sync/seen.py:101-102` records for the whole scheme. §8 has no subsection, so 80 and
# the second serial. Declared here rather than in `pipeline.py` beside its sibling because
# `pipeline` imports this module and not the other way round; the pair is registered in both
# comments so neither can be renumbered alone. [M5.1 review cycle 3, d322-MINT-RACE-01]
_MINT_LOCK = 8002

# D3's sentence, one spelling. The milestone named is the one `ROADMAP-M5.md` gives the work.
NOT_IMPLEMENTED = "not implemented at M5.1 - owned by {}"

# §8's own name for stage 2, which is also the `phase` every adapter in `sources/` registers
# under (`sources/base.HandlerSpec.phase`). Spelled once because it is the filter stage 2 selects
# its kinds by: a driver that asked for a phase the registry does not use would run nothing and
# report a clean walk, which is the failure mode a registry exists to prevent.
ENRICH_PHASE = "enrich"

# Decision 334's one required source, as the registry spells it. `derive/rebuild.REQUIRED_DOCUMENTS`
# is the same fact on the other side of the raw store - the two KINDS that document arrives under,
# `tmdb:movie_detail` and `tmdb:tv_detail` - and the two cannot be one constant because one names a
# handler and the other names stored bytes. They are registered in both comments so a rename of the
# handler cannot leave the derive looking for a document nothing produces.
REQUIRED_KIND = "tmdb:detail"

# The SOURCE that kind belongs to, which is what decision 334's park is a rule about: "a required
# source that RAN and did not answer parks with that source named". Derived from the kind rather
# than written out, because the two must not be able to drift apart.
REQUIRED_SOURCE = REQUIRED_KIND.split(":", 1)[0]

# A stage 2 that was handed no fetcher. A FAILURE and not a park, which is the one place in this
# file where that is the easy call: decision 336 gives `failed` to "this stage raised and will
# raise again", and a driver that did not build a fetcher will not build one on the next drain
# either - nothing an operator does to this title changes it. Decision 373 puts the construction
# in `pipeline.drain` precisely so that there is one per drain; a stage that quietly made its own
# would be a second set of per-host token buckets and a second circuit breaker pacing the same
# hosts at twice their declared rate, which is the defect `fetch.Fetcher._runtime` already guards
# against INSIDE one instance. The sentence names the driver because the repair is a code change
# and the person reading §6.6's board needs to know that it is not theirs.
NO_FETCHER = (
    "stage 2 was handed no fetcher, so no source could be asked. Every request this app makes "
    "goes through the one rate-limited fetcher `pipeline.drain` builds per drain (spec v2.1 §8, "
    "decision 373); this is a defect in the driver rather than anything about this title"
)

# Decision 334's park: the one source §8 stage 2 requires answered and did not answer well.
# Written for §6.6's board, which `0005_ledger.sql:138` says shows this string verbatim, and it
# names the lever twice over - the TMDB card in Admin for the configuration case, and the retry
# for the transient one - because `_run_stage`'s own rule is that a reason an operator reads has
# to name a lever that exists. It carries the source's note inside the sentence for
# `UNUSABLE_METADATA`'s reason: the note is the only thing that says WHICH way it failed, and a
# reason that is nothing but a note is a log line on a product surface.
ENRICH_REQUIRED_FAILED = (
    "enrichment stopped: {} is the one source §8 stage 2 requires and it answered \"{}\". The "
    "other sources were still asked and whatever they returned is in this job's detail. Check "
    "the TMDB connector in Admin, then retry this job from the acquisition board - it resumes "
    "here and re-reads what is already in the raw store (decision 334)"
)

# The same park, for the state where the required KIND never got to ask because a sibling kind of
# the required SOURCE failed first. `tmdb:detail` has no request to make until `tmdb:resolve` has
# filled `title.tmdb_id`, so a refused key or a 500 on the resolve kind leaves the required kind
# unasked. A separate sentence rather than a second `format` of the one above because "it answered"
# is untrue here - the required kind was never asked at all - and a reason an operator reads has to
# describe the state it was written for. [M5.3 review cycle 2, m53-c2-334-01]
ENRICH_REQUIRED_UNASKED = (
    "enrichment stopped: {} is the one source §8 stage 2 requires and it could not be asked, "
    "because {} answered \"{}\". The other sources were still asked and whatever they returned "
    "is in this job's detail. Check the TMDB connector in Admin, then retry this job from the "
    "acquisition board - it resumes here and re-reads what is already in the raw store "
    "(decision 334)"
)

# A provider id this pipeline is allowed to MINT against. Decision 323 says a row is minted "only
# on a provider id (imdb/tmdb/tvdb)", and `resolve.identity` answers a different question: it was
# built for LOOKUP, where a junk key merely fails to match, and `_mint` reuses its output as the
# value it WRITES and as the join key every later lookup resolves against. The two measured
# consequences of trusting it are both permanent under decision 162: `{"Tmdb": "0"}`, which Kodi
# and Emby NFO writers emit on a mis-scraped file, mints a title with `tmdb_id = 0` that every
# later zero-id item then resolves onto; and `{"Tmdb": "tt0113277"}` mints `tmdb_id = 113277`,
# which a legitimate tmdb 113277 then resolves onto. `title` carries no UNIQUE on these columns
# (0003 rule 6), so nothing below this line refuses the collision.
# [M5.1 review cycle 1, M51-REV-05]
_IMDB_ID = re.compile(r"tt\d{7,}")

# The ceilings the COLUMNS have, restated here because `_mintable_ids`, `_year` and `_runtime_min`
# are the three functions that decide what this pipeline writes into them - it said TWO while
# `_mint` bound three computed values against bounded columns, and the one the rule was never
# applied to is the one that raised out of the mint. [M5.1 review cycle 4 second pass,
# M51-C4-MINT-RUNTIME-02]
# `title.tmdb_id` and `title.tvdb_id`
# are `integer` and `title.year` is `smallint` (`0003_content.sql:35-36`, `:32`), and asyncpg binds
# a Python int against the column's own type - so a runaway numeric id out of a mis-scraped NFO
# does not park with an operator's sentence, it raises `DataError: value out of int32 range` from
# inside the driver's bookkeeping and puts a database's internal message on §6.6's board, which
# `0005_ledger.sql:138` says is "shown verbatim". A shape check that does not ask the one question
# the column actually asks is a validator that passes the value it exists to refuse.
# [M5.1 review cycle 2, d323-int32-04]
_INT32_MAX = 2**31 - 1
_SMALLINT_MAX = 2**15 - 1

# What a park waits when the thing that may change is a person doing something: a bundle import,
# a spend cap, a library repair. Decision 336 gives a park with a deadline back to the pool
# WITHOUT spending an attempt (`queue.defer`), so this is a re-ask and never a retry budget.
#
# ONE DAY, AND THE ARITHMETIC IS THE REASON. The drain runs every 1800 s and takes
# `DRAIN_LIMIT = 8` tasks a tick, which is 384 walks a day; a household re-asking once a day pays
# one no-op walk per parked title against that ceiling, and a bundle-less install's walk stops at
# stage 9's first statement because `active_store` returns None before any placement runs. An
# acquired set large enough to crowd that ceiling is the same measurement `place` below already
# defers to M5.6 or M5.7, with §6.6's board in front of them. Shorter would ask an operator's
# unchanged install more often than they could possibly act; longer would make "import a bundle
# and this title is placed" a promise about the day after tomorrow.
OPERATOR_WAIT = timedelta(days=1)


def waiting_on_the_world() -> datetime:
    """The instant a park that waits on a person or a sweep comes back by itself.

    THE PARK THAT CARRIES NO TIME IS TERMINAL, and that is what this function exists to avoid.
    `pipeline._record_stop` turns a park with no `until` into `queue.skip`, `queue.lease` claims
    `state = 'pending'` only, and nothing in the tree moves a row out of `skipped` - `retry_failed`
    was deliberately not ported (decision 330 is M5.6's), and `queue.enqueue` is
    `ON CONFLICT (kind, key) DO NOTHING`, so even a later sweep enqueueing the same key is a
    no-op. So a stage that parked "waiting on something that may change" with no deadline was
    waiting for a drain that could never come, while its reason told an operator on §6.6's board
    that importing a bundle would place the title "on the next drain". `park`'s own docstring
    names that state a bug for everything except "no provider id". [M5.1 review cycle 1,
    M51-CRASH-01, M51-REV-03]
    """
    return datetime.now(UTC) + OPERATOR_WAIT


@dataclass(frozen=True)
class Outcome:
    """What a stage did, in the three verbs the driver knows.

    Frozen because the driver writes the board from it and then hands the same object to the
    queue: two writers reading one mutable record is how a reason shown on a board stops being
    the reason a task was deferred with.
    """

    verb: str
    reason: str = ""
    until: datetime | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    # Stage 1 only. The task is keyed on a Jellyfin item or a provider id and can exist before
    # its title does (decision 322), so the title id is established rather than known, and this
    # is the one fact a stage hands forward out of band.
    title_id: int | None = None


def advance(detail: dict[str, Any] | None = None, *, title_id: int | None = None) -> Outcome:
    return Outcome(ADVANCE, detail=detail or {}, title_id=title_id)


def park(
    reason: str, *, until: datetime | None = None, detail: dict[str, Any] | None = None
) -> Outcome:
    """Waiting on something that may change (decision 336). Never auto-fails.

    `until` is the whole difference between the two shapes of waiting: with a time the task is
    deferred and comes back by itself, without one it is skipped and comes back when an operator
    does something. A park with no time and no operator action is a title that sits for ever,
    which is the honest outcome for "no provider id" and a bug for anything else.
    """
    return Outcome(PARK, reason=reason, until=until, detail=detail or {})


def fail(reason: str, *, detail: dict[str, Any] | None = None) -> Outcome:
    """This stage raised and will raise again (decision 336). The only plain-retry state."""
    return Outcome(FAIL, reason=reason, detail=detail or {})


@dataclass
class StageContext:
    """What one stage is handed. The corpus's `Ctx`, with the title added and the fetcher back.

    `title_id` is None until stage 1 has run, and is the reason this is a mutable dataclass where
    `Outcome` is frozen: the driver sets it once, between stage 1 and stage 2, and every later
    stage reads it. `fetcher` is set the same way and for a second reason of its own, below.

    `fetcher: Any` IS THE WHOLE OF HOW THIS MODULE STAYS FREE OF TRANSPORT (decision 373). It
    holds the fetcher, the one door §8's politeness clause (`spec:404`) is enforced at, and
    naming that type here would mean importing it - under `TYPE_CHECKING`, which costs nothing at
    runtime, but which is still an import node and still repeals the property
    `test_the_stage_machine_and_the_derive_do_not_reach_for_the_fetcher` states. The annotation is
    not laziness about a type: an adapter calls `ctx.fetcher.get(...)` and this module never calls it
    at all, so what stage 2 needs to know about the object is that it has one, which is exactly
    what `None` and not-`None` say. `pipeline.drain` builds it, so a stage handed none FAILS with
    a reason naming the driver rather than constructing one for itself - a stage that built its
    own would be a second answer to "how many Fetchers does one drain have", and the per-host
    token buckets and the circuit breaker are per instance (`fetch.Fetcher`'s own docstring).
    """

    conn: asyncpg.Connection
    task: queue.Task
    title_id: int | None = None
    run_id: int | None = None
    fetcher: Any = None

    @property
    def item(self) -> dict[str, Any]:
        """The Jellyfin item this task was enqueued for, or `{}` for a task keyed on a title."""
        payload = self.task.payload or {}
        return payload.get("item") or {}


# --- stage 1: identify --------------------------------------------------------------------------


async def identify(ctx: StageContext) -> Outcome:
    """§8 stage 1: "Jellyfin ProviderIds -> title row (fill-never-clobber); a row is MINTED only
    on a provider id (imdb/tmdb/tvdb)".

    RESOLVE FIRST, ALWAYS. `connectors/resolve.resolve_title_id` is shipped and is the one
    implementation of §7.1's identity rules - jellyfin_id, then imdb, then tmdb/tvdb qualified by
    kind, then a name-and-year fallback that refuses when it is ambiguous. A second implementation
    here would disagree with the nightly sweep about which title a library item is, and the two
    disagreeing is how a household ends up with two rows for one film in a spine that cannot be
    rewritten (decision 162).

    Resolving is also what makes this stage IDEMPOTENT, which is the property the whole driver
    rests on: the mint below sets `jellyfin_id`, so a second run of this task finds the row the
    first run minted on the resolver's first branch and mints nothing. A worker killed between
    the mint and the board write leaves exactly that state, and the reclaim is a no-op rather
    than a duplicate.

    THAT IDEMPOTENCE IS NOT FREE AND THIS DOCSTRING USED TO ASSERT IT AS IF IT WERE. It holds only
    because `_mintable_ids` and `UNCANONICAL_ITEM_ID` now refuse to mint an identity whose WRITTEN
    form is not the form the resolver LOOKS UP: `_mint` wrote stripped values while
    `resolve.resolve_title_id` reads the raw ones, so one space around a provider id turned "the
    reclaim is a no-op" into "the reclaim mints a second title for the same film".
    [M5.1 review cycle 3, d323-C3-MINT-01]

    AND IT IS NOT ENOUGH ON ITS OWN, because it is a claim about ONE task run twice and decision
    322 gives one film several tasks. `key_for_item` keys on the Jellyfin item, so a household
    whose libraries ship "Movies" and "Movies 4K" - `resolve.upsert_item`'s own example - has two
    tasks for one film that `UNIQUE (kind, key)` deliberately does not relate, and `queue.lease`'s
    `FOR UPDATE SKIP LOCKED` hands them to two workers under the rolling restart that module calls
    "the ordinary state". Under READ COMMITTED neither sees the other's uncommitted INSERT, so both
    resolve to nothing and both mint; `title` carries no UNIQUE on these columns (§4.1 rule 6), so
    nothing below refuses it. `_MINT_LOCK` is taken on the film's IDENTITY - not on a title id,
    which does not exist yet - and held to the end of the transaction the mint commits in, which is
    the only release a `kill -9` cannot skip. TRIED AND NEVER WAITED FOR, for `_claim_title`'s
    reason: §5.3's loop is sequential, so a drain that blocked would hold the whole tick behind
    another worker, and a task handed back is what the queue is for.
    [M5.1 review cycle 3, d322-MINT-RACE-01, M51-C3-CRASH-01]

    THE MINT IS THE ONE WRITE THIS PIPELINE CANNOT TAKE BACK, so it happens only on a provider id
    (decision 323). A name-and-year mint is the silent wrong match the resolver already refuses,
    measured on this corpus at 2,438 titles sharing `(kind, lower(name))` and 573 groups still
    colliding with the year applied (`connectors/resolve.py:189-194`).

    A PROVIDER ID IS NECESSARY AND IT IS NOT SUFFICIENT, which decision 360 is the fourth reading
    of after `UNSUPPORTED_KIND`, `MALFORMED_PROVIDER_ID` and `UNCANONICAL_ITEM_ID`. The measurement
    quoted one line up is not only an argument against a name-and-year MINT; it is also the state
    in which the resolver's name arm refuses, and stage 1 read that refusal as "there is no such
    title" when it can equally mean "there are several and I will not guess". So the last thing
    asked before the door is whether the spine already holds a title this item cannot be told from
    - `_indistinguishable_titles` - and a walk that cannot tell parks instead of writing. A park is
    recoverable and a mint is not; under decision 162 that is the whole argument.
    [M5.1 review cycle 4 second pass, M51-C4-MINT-AMBIG-01, M51-C4-MINT-ORIGINAL-03]
    """
    item = ctx.item
    if ctx.title_id is not None:
        # A task enqueued against a title that already exists - the inbox `_park_thin` has been
        # writing since M4.13, or §8.4's flywheel. There is nothing to identify.
        return advance({"identified": "the task names the title"}, title_id=ctx.title_id)
    if not item:
        return fail("the task payload carries neither a Jellyfin item nor a title id")

    claims = _mint_claims(item)
    if not claims:
        # Nothing this item offers could become a title, so this walk cannot create a row and
        # there is nothing to serialise: every exit below it is a read or a park. Running it
        # outside a transaction keeps the stage in the autocommit `rawstore.store` documents for
        # every other stage in this file.
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

    The name has to be one two DIFFERENT Jellyfin items for one film both produce, which is why it
    is built from the provider ids and never from the task key: decision 322's whole point is that
    `jellyfin:jf-4k` and `jellyfin:jf-hd` are unrelated keys for one film.

    ALL OF THEM AND NOT THE STRONGEST, because the resolver matches on ANY of them. An item
    offering imdb and tmdb and an item offering only tmdb are one film to
    `resolve.resolve_title_id`, so claiming only the first item's imdb id would leave exactly that
    pair racing. Taken in a fixed order, which costs nothing here - `pg_try_advisory_xact_lock`
    never waits, so there is no lock-ordering deadlock to avoid - and a claim this walk did take
    before failing on a later one is released with the transaction.

    The imdb claim carries no kind and the other two do, which is `resolve_title_id`'s own shape:
    "`imdb_id` is globally unique across kinds when present; tmdb and tvdb ids are only unique
    *within* a kind (§4.1 rule 6)". A claim narrower than the lookup it protects is a claim two
    walks can both hold. [M5.1 review cycle 3, d322-MINT-RACE-01]

    AND THE FOURTH BRANCH IS CLAIMED TOO, because the rule in the paragraph above was stated and
    then applied to three of the resolver's four matching arms. `resolve.resolve_title_id` also
    matches on kind + year + name (`connectors/resolve.py:195-213`), and that arm is precisely
    what merges two walks whose PROVIDER ids are disjoint: a household shipping "Movies" and
    "Movies 4K" where one copy's NFO carries only an imdb id and the other only a tmdb id, or a
    series scraped by Sonarr in one library (tvdb) and by the TMDb plugin in another. Their claim
    sets did not intersect, both walks took every claim they asked for, both read nothing under
    READ COMMITTED, and both minted - measured 1 run in 20 against a scratch database with no
    monkeypatching and no injected delay, and 5 in 5 with the window widened. `_mint` writes the
    stripped `Name` and `_year(item)`, so the row one walk mints is reachable by the other's
    name-and-year lookup WHEN THAT LOOKUP CAN ANSWER, which is why the same pair run sequentially
    produced one title and run concurrently produced two. [M5.1 review cycle 4, d322-C4-MINT-01]

    THAT CONDITION IS NOT A DETAIL AND THIS PARAGRAPH USED TO STATE IT UNCONDITIONALLY. The arm
    the claim mirrors answers only for exactly ONE candidate, so the loser's second walk resolves
    onto the winner's row only where the spine holds no other title of that name and year - and
    the winner's own mint has just added a candidate to it. Where two already collided, the loser
    came back and minted, with the lock working perfectly: serialising a pair does not reconcile
    it. What closes that is a refusal rather than a wider claim, and it is
    `_indistinguishable_titles` under decision 360, which asks the ambiguity question directly
    before the mint. [M5.1 review cycle 4 second pass, M51-C4-MINT-AMBIG-01]

    CLAIMED ONLY WHERE THIS WALK COULD MINT. The name key is appended after the provider claims
    and only when there is at least one, so an item that offers no mintable id still returns the
    empty list `identify` reads as "nothing to serialise" - a lock taken for a walk that is about
    to park `NO_PROVIDER_ID` would buy nothing and would cost `identify`'s documented autocommit.
    It mints nothing by itself either: decision 323 still says a title is minted only on a
    provider id, and what a claim decides is which walk may ASK, never what may be written.

    WHAT THIS STILL DOES NOT COVER, said plainly because the sentence above is the one a later
    reader will rely on. The resolver's fourth arm also matches `original_name` and any
    `title_alias`, and it compares with Postgres's `lower()` rather than Python's; this claim
    mirrors the `lower(t.name)` arm alone. So two items for one film that agree on NOTHING but an
    alias are still two claims.

    THE RESIDUE THAT LEAVES IS NOT A RACE, which is the correction review cycle 4's second pass
    made here. This paragraph filed it under the claim discipline and justified the gap by saying
    the remaining arms "are lookups into rows that already exist", so a walk that would match one
    resolves rather than mints. That is sound for `title_alias`, which nothing in M5.1 writes, and
    it is FALSE for `original_name`, because `_mint` writes that column from the item's
    `OriginalTitle` - and the arm is directional: the resolver probes with the item's `Name` only.
    A German and an English copy of one film therefore mint twice in one ordering and once in the
    other, with no concurrency, no claim overlap and nothing for a wider claim set to serialise. A
    lock is the wrong instrument for a lookup that genuinely does not match, so the repair is the
    refusal in `_indistinguishable_titles` (decision 360), which probes with BOTH names this mint
    would write and declines to write a row the spine cannot be told apart from. What is left
    here is the alias arm alone, and it is left because a claim built from a column this item does
    not carry is a claim the other walk cannot compute - while the refusal covers it from the
    other side, since an alias belongs to a title that already exists.
    [M5.1 review cycle 4 second pass, M51-C4-MINT-ORIGINAL-03]
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
    """Stage 1's body, run under the claim `identify` takes on this item's identity.

    Split out rather than nested so the claim and the work it protects are one `async with` and
    not an indented hundred lines; every paragraph arguing what happens here is on `identify`.
    """
    try:
        found = await resolve.resolve_title_id(ctx.conn, item)
    except (asyncpg.DataError, ValueError) as exc:
        # THE RESOLVER RUNS BEFORE THE MINT VALIDATOR AND BINDS THE SAME UNVALIDATED VALUES.
        # `resolve.resolve_title_id` looks up on `tmdb_id`/`tvdb_id` (`integer`) and on the
        # name-and-year fallback's `t.year` (`smallint`), so an item carrying a runaway numeric id
        # raises `DataError: value out of int32 range` thirteen lines before `_mintable_ids` gets
        # to refuse it - and the driver turns a raised stage into decision 336's `failed`, which
        # burns every attempt re-learning the same thing and writes the DATABASE's sentence onto a
        # board column `0005_ledger.sql:138` says is "shown verbatim". Caught rather than
        # pre-validated, because resolving FIRST is decision 323's rule and this branch must not
        # refuse an item that would have resolved on its Jellyfin id.
        #
        # A SKIP and not a failure, by `MALFORMED_PROVIDER_ID`'s argument: nothing about the value
        # changes without a person editing it, so a retry today learns the same thing four times
        # and then closes the task with a database's internal message as its epitaph.
        # [M5.1 review cycle 2, d323-int32-04]
        #
        # `ValueError` TOO, AND IT IS THE SAME SENTENCE RATHER THAN A SECOND ONE. `resolve._as_int`
        # keeps every character `str.isdigit()` accepts and then calls `int()`, which accepts fewer
        # - so a tmdb id of `"12345\u00b2"` raises out of the resolver on the line above, in
        # PYTHON, before any value reaches a column. `_mintable_ids` refuses that value on this
        # side of the door, but refusing it here would break decision 323's order (resolve first,
        # so an item that would have resolved on its Jellyfin id still does), and the fix on the
        # lookup side is in `connectors/resolve.py`, which this milestone does not touch. The
        # cause, the remedy and the person who has to act are identical to the `DataError` case,
        # so the park is identical: the alternative is `_run_stage` turning it into decision 336's
        # `failed` and four attempts spent re-learning a value only an editor can change.
        # [M5.1 review cycle 4, d323-C4-MINT-02]
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
            # `ALREADY_PLACED` argues the exit. No `title_id` on the outcome, so the driver
            # establishes no title for this walk and writes no board row over the one that exists.
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
        # The last thing checked, because it is the least likely and the most confusing to be told
        # about while a provider id is also wrong: `MALFORMED_PROVIDER_ID` above names the field an
        # operator can actually fix. [M5.1 review cycle 3, d323-C3-MINT-01]
        return park(
            UNCANONICAL_ITEM_ID,
            detail={"name": str(item.get("Name") or ""), "item_id": raw_id},
        )

    collides = await _indistinguishable_titles(ctx.conn, item, kind=kind)
    if collides:
        # THE LAST QUESTION BEFORE THE ONE-WAY DOOR, and it is asked here rather than in the
        # resolver for decision 323's order: an item that would have resolved on its Jellyfin id
        # or a provider id has already done so, so this branch only ever sees a walk that is about
        # to WRITE. Last for the reason `UNCANONICAL_ITEM_ID` is second-to-last: a reason naming a
        # field an operator can fix beats a reason about the shape of the library.
        # [M5.1 review cycle 4 second pass, M51-C4-MINT-AMBIG-01, M51-C4-MINT-ORIGINAL-03]
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
    """Decision 411's test: a title the bundle supplied that already carries a placement.
    `ALREADY_PLACED` argues both halves; `title_placement_has_basis` ties `placement <>
    'unplaced'` to a basis bundle, so it is the one column that already says a coordinate
    exists."""
    return bool(await conn.fetchval(
        "SELECT origin = 'bundle' AND placement <> 'unplaced' FROM title WHERE id = $1",
        int(title_id),
    ))


async def re_offered_title(conn: asyncpg.Connection, item: dict[str, Any]) -> int | None:
    """The title an item resolves to, when stage 1 would exit on it; None otherwise.

    Published for §7.2's two feeders, which file such an item below every genuine add (decision
    411): a task that exits at stage 1 still takes one of the drain's `DRAIN_LIMIT` slots, and a
    re-scan's re-stamps filed at the default priority queued ahead of a film the household had
    really just added. The SAME resolver and the same test as `_resolve_or_mint`, so the feeders
    and the stage cannot disagree about which item is a re-offer.

    A value the resolver's columns refuse is not a re-offer, for `_resolve_or_mint`'s reason: that
    item is stage 1's to park with the database's sentence, and filing it at the default priority
    is what gets it there. Called OUTSIDE any transaction by both feeders, because the refusal is
    a Postgres error and one inside a transaction would abort the enqueue it was deciding.
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

    WHY A REFUSAL IS NEEDED AT ALL, since `resolve_title_id` has already answered None.
    `connectors/resolve.py:195-213` takes `LIMIT 2` and returns a match only for exactly one
    candidate - deliberately, because "an arbitrary match is strictly worse than no match" for a
    LOOKUP, where a refusal is merely reported. Stage 1 is not a lookup: it reads the same None as
    "there is no such title" and WRITES. Measured on the corpus this resolves against, 2,438 titles
    share `(kind, lower(name))` and 573 groups still collide with the year applied
    (`connectors/resolve.py:189-194`), and `pipeline.enqueue_item`'s documented input is
    `ResolveReport.unmatched` - which `resolve.py:233` appends to on exactly that refusal - so the
    items reaching this stage are enriched for ambiguity by construction. Two library copies of one
    film with disjoint provider ids then mint TWO rows: the winner's mint makes the arm MORE
    ambiguous rather than less, so the loser of `_MINT_LOCK` comes back and mints beside it. A lock
    that serialises but does not reconcile is not a fix for a one-way door.

    AND THE FOURTH ARM IS DIRECTIONAL, which is the second half and needs no concurrency at all.
    It binds the ITEM's `Name` and compares it against the candidate's `name`, `original_name` and
    aliases; the item's `OriginalTitle` is never a probe, while `_mint` WRITES `original_name` from
    it. So a German and an English copy of one film resolve in one order and mint twice in the
    other, deterministically, on one connection. Both names this mint would write are probed here
    for that reason, which is also what makes the answer symmetric: whatever a later walk could
    match this row on, this walk asks about first.

    THIS IS NOT A SECOND RESOLVER, which the header's property 2 forbids and would be the wrong
    repair. It returns a COUNT and never an id, it never decides which title an item is, and it is
    incapable of resolving anything: the only thing it can do is refuse. Widening the resolver so
    that stage 1 matches rows the nightly sweep would not is the disagreement property 2 names;
    declining to write when the two cannot agree is the opposite of it.

    `LIMIT 2` inside the count because the answer is only ever used as 0, 1 or "more than one", and
    the wording of the park is the only thing that reads the number. Lowered by POSTGRES on both
    sides, as `resolve_title_id` does it: Python's `str.lower` and Postgres's `lower()` do not
    agree on every alphabet, and a probe that disagreed with the arm it mirrors would refuse and
    permit different rows. [M5.1 review cycle 4 second pass, M51-C4-MINT-AMBIG-01,
    M51-C4-MINT-ORIGINAL-03; decision 360]
    """
    year = _year(item)
    if year is None:
        # The arm this mirrors does not run without a year either ("a name with no year is not an
        # identity"), so there is nothing here to be ambiguous WITH.
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

    THE VALIDATION IS HERE AND NOT IN `resolve.py`, which is M5.2's file and which this milestone
    is told not to edit - and which is also the right place for it not to be. `resolve.identity`
    serves LOOKUP: a junk key there merely fails to match a row, which is harmless. The mint is
    the one write decision 162 makes unrecoverable, so the question it asks is a stricter one -
    "is this a value I am willing to make a title's permanent identity" - and only the caller that
    mints can ask it.

    WHY THE RAW STRINGS AND NOT `identity`'s OUTPUT. `resolve._as_int` keeps the digits and drops
    everything else, so `"tt0113277"` filed under a `Tmdb` key becomes the perfectly plausible
    integer 113277 and `"-5"` becomes 5. Reading `provider_ids` - which only lowercases Jellyfin's
    own keys, so this is not a second implementation of identity - lets an id that is the wrong
    SHAPE for its field be refused rather than silently reinterpreted. `"0"` is refused as well:
    it is the value an NFO writer emits for a file it failed to scrape, and the first item
    carrying it would mint the row every later one resolves onto.

    WHAT THIS DOES NOT CLOSE, said here because a later reader will ask. `resolve.resolve_title_id`
    still LOOKS UP on the unvalidated ids and `resolve.upsert_item`'s fill path can still write one
    onto a corpus title from the nightly sweep; both are in M5.2's file. This closes the pipeline's
    own door - the only one M5.1 owns and the only one that mints.
    [M5.1 review cycle 1, M51-REV-05]
    """
    offered = resolve.provider_ids(item)
    # NOT STRIPPED, AND THAT IS THE RULE RATHER THAN AN OVERSIGHT. `resolve.resolve_title_id` looks
    # up `WHERE imdb_id = $1` on `provider_ids`' raw string, so a value this function trimmed on
    # the way in is a value the resolver can never find on the way back - `identify`'s idempotence
    # paragraph rests on it finding exactly the row the mint wrote. `_IMDB_ID.fullmatch` already
    # refuses anything that is not canonical, so the whole repair is the absent `.strip()` and the
    # existing `MALFORMED_PROVIDER_ID` park, which says what to correct in Jellyfin. The two
    # columns below keep theirs: `resolve._as_int` reduces the lookup side to digits exactly as
    # `.strip().isdigit()` reduces this one, so those two agree already and refusing a padded tmdb
    # id would cost a household a title for nothing. [M5.1 review cycle 3, d323-C3-MINT-01]
    imdb = offered.get("imdb") or ""
    mintable: dict[str, Any] = {
        "imdb_id": imdb if _IMDB_ID.fullmatch(imdb) else None,
        "tmdb_id": None,
        "tvdb_id": None,
    }
    for column, key in (("tmdb_id", "tmdb"), ("tvdb_id", "tvdb")):
        digits = (offered.get(key) or "").strip()
        # BOUNDED BY THE COLUMN, which is the one question this function's own docstring says it
        # exists to ask - "is this a value I am willing to make a title's permanent identity" - and
        # the one it did not ask. Shape and sign alone passed `99999999999999999999`, which the
        # `integer` column cannot hold. [M5.1 review cycle 2, d323-int32-04]
        #
        # `isdecimal` AND NOT `isdigit`, because `str.isdigit()` is TRUE for characters `int()`
        # cannot parse - the superscripts and subscripts, so `"12345\u00b2"` out of a page with a
        # footnote marker in it. The guard then let the value through and `int()` raised, from a
        # function whose whole job is to REFUSE what it will not make permanent, and from
        # `_mint_claims` - before `identify` has taken a claim. `_run_stage` turns that into
        # decision 336's `failed`, which burns four attempts re-learning it and writes a Python
        # exception message onto a board column `0005_ledger.sql:138` shows verbatim, where
        # `MALFORMED_PROVIDER_ID` already names the field and the lever. `isdecimal` is exactly
        # what `int()` accepts on a digits-only string, so nothing that parses today changes: the
        # Arabic-Indic id `"\u0669\u0664\u0669"` still mints 949 and still re-resolves onto itself.
        # [M5.1 review cycle 4, d323-C4-MINT-02]
        if digits.isdecimal() and 0 < int(digits) <= _INT32_MAX:
            mintable[column] = int(digits)
    return mintable


def _year(item: dict[str, Any]) -> int | None:
    """Jellyfin's `ProductionYear` as an int, digits only.

    Spelled here rather than reaching into `connectors/resolve.py`'s private helper: that module
    parses the same field for the name-and-year fallback this stage is forbidden to use
    (decision 323), and a public function of this shape is not something to add to a file M5.2
    owns while M5.1 is open.
    """
    raw = item.get("ProductionYear")
    if raw is None:
        return None
    # `isdecimal` for `_mintable_ids`' reason one function up, and it matters MORE here now that
    # `_mint_claims` reads this value: a `ProductionYear` of `"1995\u00b2"` kept the superscript
    # through an `isdigit` filter and raised out of `int()` before any claim had been taken.
    # Dropping the character rather than raising is what "digits only" already said this does.
    # [M5.1 review cycle 4, d323-C4-MINT-02, d322-C4-MINT-01]
    digits = "".join(c for c in str(raw) if c.isdecimal())
    if not digits:
        return None
    # `title.year` is `smallint`, so a runaway value out of a mis-scraped NFO is the same defect
    # `_mintable_ids` refuses one function above: bound by the column, or the mint raises a
    # `DataError` from inside a transaction the driver then reports as a stage that failed.
    # Dropped rather than parked, because unlike a provider id the year is not an identity - the
    # column is nullable and 21% of the corpus already carries no imdb id either.
    # [M5.1 review cycle 2, d323-int32-04]
    year = int(digits)
    return year if 0 < year <= _SMALLINT_MAX else None


def _runtime_min(item: dict[str, Any]) -> int | None:
    """Jellyfin's `RunTimeTicks` in whole minutes, or None.

    `TICKS_PER_SECOND` is imported rather than restated: `connectors/jellyfin.py:80` already owns
    that constant and two spellings of it is one of them going stale. Rounded rather than
    truncated because §4.3's `runtime:>160` meta column is a threshold and a 160.6-minute film
    truncated to 160 falls out of a bucket it belongs in.

    BOUNDED BY ITS COLUMN AND TOLERANT OF A VALUE THAT WILL NOT PARSE, which is `_year`'s rule one
    function up and the doctrine at the head of this file: `_mintable_ids` and `_year` are named
    there as "the two functions that decide what this pipeline writes into" a bounded column, and
    this is the THIRD value `_mint` binds against one - `runtime_min integer`
    (`0003_content.sql:33`), at `$5`. Jellyfin declares `RunTimeTicks` as an int64 and M5.2's
    `/events` webhook will carry the same dict out of a Handlebars-rendered body where a number
    commonly arrives as a string, so both shapes are real: an absurd tick count raised
    `DataError: value out of int32 range` and a non-numeric one raised `ValueError`, both from
    inside `_mint`'s own transaction and both landing as decision 336's `failed` with a database's
    or Python's internal sentence on a board column `0005_ledger.sql:138` says is shown verbatim.
    Four attempts then re-learn a value only a re-encode can change and `ON CONFLICT (kind, key)
    DO NOTHING` makes the sweep's next pass a no-op, so the film is unacquirable for good - in a
    file where `MALFORMED_PROVIDER_ID` and `UNUSABLE_METADATA` exist so that never happens.

    DROPPED RATHER THAN PARKED, which is `_year`'s disposal and for `_year`'s reason: the column is
    nullable and a runtime is not an identity, so the honest outcome is to lose the value and keep
    the title. Positive AND bounded, because a negative tick count yields a negative runtime that
    is truthy and fits in `integer` - stored rather than refused, which is the same validator
    passing the value it exists to reject. [M5.1 review cycle 4 second pass,
    M51-C4-MINT-RUNTIME-02; M5.1 review cycle 2, d323-int32-04]
    """
    try:
        # `OverflowError` beside the other two because `int()` raises THAT one for a float
        # infinity, and Python's `json` decoder accepts the `Infinity` literal by default - which
        # is the decoder M5.2's webhook body goes through. A guard that names two of the three
        # exceptions its own conversion raises is the shape this paragraph is about.
        ticks = int(item.get("RunTimeTicks") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    minutes = int(round(ticks / (TICKS_PER_SECOND * 60)))
    return minutes if 0 < minutes <= _INT32_MAX else None


async def _mint(
    conn: asyncpg.Connection, item: dict[str, Any], *, kind: str, ids: dict[str, Any]
) -> int:
    """Insert the one new `title` row §8 stage 1 is allowed to create. Returns its id.

    NO `id` IN THE COLUMN LIST. That is the whole mechanism: `0015_seed.sql:27` sets
    `nextval('title_id_seq')` as the column default and the sequence is `MINVALUE 1000000000`, so
    the row lands above the corpus's half of the namespace without this code knowing a number.
    `0015_seed.sql:39-44` names this write path as the beneficiary - "this is the backstop for
    every other write path, including §8 stage 1" - and the assertion below is what makes a
    defeated backstop loud instead of silent: a sequence someone reset by hand, or a default
    someone dropped in a repair, would otherwise mint into the corpus's half and the next bundle
    import would find a collision it cannot resolve. Inside the transaction, so a refusal leaves
    no row (decision 162).

    `is_owned` and `owned_checked_at` are DERIVED FROM THE ITEM and not asserted. §7.2 says the
    flag is "re-derived from Jellyfin, never trusted stale", and seeing the item in the library IS
    the derivation - `connectors/resolve.py:250-253` says exactly that about the same two columns.
    The derivation is `Id`: an item that carries one is an item Jellyfin showed us, and an item
    that carries none is not.

    IT USED TO BE THE SQL LITERAL `true`, WHICH THAT ARGUMENT DOES NOT REACH. `pipeline.key_for_item`
    falls back to a provider id precisely "so §8.4's flywheel can enqueue work for something
    Jellyfin has never shown us", and `test_acquire_pipeline.py` pins that path with an item that
    has no `Id` at all - which mints `jellyfin_id = NULL`. `sync/seen._falsify_ownership` is the
    ONE statement in the codebase that can un-own a title and it is scoped
    `WHERE is_owned AND jellyfin_id IS NOT NULL` (`seen.py:1168-1169`), so such a row is invisible to
    every nightly sweep for ever: under decision 162 the household's spine would permanently claim
    ownership of a film it does not have, and Home's "New in the library" shelf, §6.2's candidate
    pool and Tonight's pool all read that flag. `resolve.py:179-183` names this exact harm as the
    thing its own refusals exist to avoid.

    Nothing is lost by deriving it: stage 10 already parks a title that is not badgeable with "no
    ownership flag" and a deadline, which is the correct state for a title the household has not
    acquired yet, and the flag flips the moment a sweep resolves the item onto this row.
    [M5.1 review cycle 2, d323-owned-03]

    `jellyfin_id` is set here and only here. §7.1 keeps one item id per title as the deep link,
    elected once per sweep by `resolve._elect_representatives` from the whole page-set; that
    election keeps the current id while it is still a copy of this title, so the item that caused
    the mint is kept and not flipped. `title_jellyfin_item` is deliberately NOT written: that map
    is the sweep's, it is pruned against a completed library read, and a row inserted here would
    be a copy nobody verified against the library.
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
            # The stamp goes with the flag: §7.2's column records WHEN the derivation was made,
            # and a timestamp beside `is_owned = false` would date a derivation that said no.
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

    `sources/base.available_kinds` takes this map and drops every kind whose `requires` is not in
    it, so this is what makes §3.1's half-configured boot a legal state for stage 2 rather than a
    stage full of identical "no credential" notes: a household that has set up TMDB and not OMDb
    asks seven sources and never builds a request for the eighth. The five keyless sources carry
    `requires = None` and are not in this map at all, which is why the map is asked for by name
    rather than defaulted - a keyless source filtered out by a missing key would be §8's own
    source list quietly shortened.

    Three reads and not one. `credentials` deliberately exposes no generic loader (decision 377),
    because the day M5.5's `ConnectorSpec` lands it deletes this file rather than reconciling a
    second design with it. The cost is three indexed `connector_config` lookups per title, which
    is the same order as the two `SELECT 1 FROM title` the driver already pays per stop.
    """
    return {
        credentials.TMDB: await credentials.tmdb_auth(conn) is not None,
        credentials.OMDB: await credentials.omdb_key(conn) is not None,
        credentials.TRAKT: await credentials.trakt_headers(conn) is not None,
    }


async def enrich(ctx: StageContext) -> Outcome:
    """§8 stage 2: "tmdb:resolve -> tmdb:detail ... wikidata:resolve ... rt:page,
    metacritic:page->reviews" (`spec:365-368`), each through the one polite fetcher.

    THE ORDER IS THE REGISTRY'S AND NOT A LIST HERE. `sources/base.available_kinds` sorts on
    `default_priority`, and §8's sequence is load-bearing rather than cosmetic: `wikidata:resolve`
    "halves guessing" because it yields the MC/RT/Letterboxd slugs, so running it before `rt:page`
    and `metacritic:page` is the difference between reading a slug and building a url out of a
    name that cannot tell two films apart. A driver that spelled the eleven kinds out would be the
    second place that order lives, and the day a source moved, the two would disagree with nothing
    to say so. This function names exactly one kind - the required one, below - and that one is a
    decision rather than an ordering.

    DECISION 334 IS THE WHOLE OF THE MAPPING FROM ELEVEN ANSWERS TO ONE VERB. Only `tmdb:detail`
    is required; every other source's 404, timeout, refused slug or missing credential is a note
    in `acquisition_job.detail` under that source's name and the stage still advances. That is not
    leniency - it is what makes the raw store worth having. Seven sources answered, their bytes
    are on disk, and §8 stage 4's gate is the quality bar that decides whether what they said is
    enough. A stage that parked on the first 404 would throw away eight good documents over one
    host that had never heard of this film.

    AND THE REQUIRED SOURCE PARKS ONLY WHEN IT RAN - THE SOURCE, WHICH THIS USED TO TEST AS ONE
    KIND. Decision 377 says in terms that "a source
    whose credential is absent is a stage-2 note under decision 334, never a park and never an
    exception", and decision 334 says a FAILURE of `tmdb:detail` parks. Both are true of the code
    below because the two states are different: a TMDB that is not configured is filtered out by
    `available_kinds` and never runs, which §3.1 makes a legal install rather than a broken one,
    while a TMDB that was asked and did not answer is the one failure §8 stage 2 cannot shrug off.
    Read the other way round this stage would park every task on every install that has not yet
    typed a key into §6.6's TMDB card, including the installs `ops/m51_exit_criterion.py` measures.
    The unconfigured title is not lost: it reaches stage 4 with no plot and no reviews and parks
    THERE, with the counts in its reason, which is the honest sentence for it.

    THERE IS A THIRD STATE AND `available_kinds` CANNOT SEE IT, because it filters on capability
    and this one is a fact about the title's data. `tmdb:detail` has no request to make when the
    row carries no `tmdb_id` - a file Jellyfin identified by an IMDb id TMDB has no record of, or
    files under the other `kind` - and no later kind supplies one, so that title would park here
    on every drain for ever. `SourceResult.ran` is what tells this loop the difference, and the
    outcome is the paragraph above's: not lost, parked at stage 4 with the counts.
    [M5.3 review cycle 1, M53-334-01]

    AND THAT THIRD STATE HAS TWO CAUSES THAT LOOK IDENTICAL FROM HERE, which is why the park below
    reads the SOURCE and not the kind. An empty `tmdb_id` means either "TMDB holds no record of
    this film", which is an answer, or "`tmdb:resolve` could not ask" - a refused key, a 500, a
    host this drain could not reach. The second is decision 334's park condition exactly, and
    testing the required KIND walked past it: the stage advanced, the title parked at stage 4 for
    thirty days with zero counts, and the `reason` column §6.6 renders named no connector at all,
    while the SAME broken credential on a title that already carried a `tmdb_id` produced a
    one-day park naming TMDB. One key, two opposite operator experiences, and the wrong one went
    to every newly acquired IMDb-only title. `sources/tmdb.resolve` now answers the no-record case
    ok - it did what it exists to do and spent one request - so `ok=False` on a tmdb kind means a
    request that failed, and a failure at any kind of the required source parks.
    [M5.3 review cycle 2, m53-c2-334-01]

    EVERY FAILURE IS A NOTE, INCLUDING ONE THIS MODULE CANNOT NAME. An adapter returns a
    `SourceResult`; `sources/_views.capture` turns a 404, a robots refusal, a short body and the
    circuit breaker into one. So the `except Exception` below catches a provider's malformed JSON
    and an outright bug in one adapter, and records both as that source's note - which is decision
    334's own reading ("a source raising anything else is that source's note, not the stage's
    failure") and is also the only shape available, because naming the transport's exceptions
    here would import the layer decision 373 keeps out of this file. A paused host that is TMDB's
    parks below with a deadline, which is decision 336's shape for it.

    A PAUSED HOST THAT IS ANY OTHER SOURCE'S IS A NOTE AND THE STAGE ADVANCES, AND THAT CAN COST
    THE TITLE THAT SOURCE. This paragraph used to end "a source that said nothing this drain and
    will be asked again on the next", which was false: `pipeline._resume_index` answers the
    BOARD's stage, so a title past stage 2 does not reach it again on the next drain, and stage 2
    is the only stage that fetches. Decision 422 keeps the behaviour - decision 334 already rules
    that a source which did not answer is a note, a breaker pause is eight of its timeouts in a
    row, and parking every title drained inside a 900-second cooldown would re-walk the seven
    sources that DID answer every quarter of an hour for as long as one host is down, which one
    blocked host would turn into a stalled pipeline - and states the price. A title the missing
    source leaves short parks at stage 4, and decision 421 re-enters it at this stage when that
    window closes, so it IS asked again, thirty days on. A title that clears stage 4 without it
    keeps what it has until an operator can re-run stage 2 (decision 330, M5.6).
    `sources/_views.capture` writes the note as a sentence naming the host and the cooldown, so
    the board says which it was. [M5.3 review cycle 2, M53-C2-NET-01; decision 422]

    THE PARK CARRIES A DEADLINE. `OPERATOR_WAIT`'s arithmetic is the argument and it is the same
    one `place` makes: a park with no `until` is `queue.skip`, which closes the task for good, and
    what this park waits on - a network that heals, a key an operator types - is decision 336's
    "something that may change" in its plainest form. The daily re-ask costs one walk that stops
    at this stage.
    """
    if ctx.title_id is None:
        return fail("stage 2 reached with no title id; stage 1 did not establish one")
    if ctx.fetcher is None:
        return fail(NO_FETCHER)

    # Idempotent and cheap after the first call - `importlib.import_module` hands back what is
    # already in `sys.modules` - and called here rather than at import time because the adapters
    # import `sources/_views`, which imports `acquire.fetch`, which would close a cycle through a
    # module this file is forbidden to name. A registry populated by the first drain rather than
    # by the first import is also what lets `load_all`'s discovery stay discovery.
    sources.load_all()
    capabilities = await _capabilities(ctx.conn)
    # `include_paid=False`, which is `available_kinds`' own argument applied one layer up: the
    # driver's spend gate reads `Stage.paid` and stage 2 is not a paid STAGE, so a paid KIND run
    # from inside it would bill the household behind the refusal §8 requires. None of §8 stage 2's
    # eight sources is paid today; the flag exists so that the first one that is cannot arrive
    # through this loop by default (`sources/base.py`'s `paid_kinds`).
    wanted = sources.available_kinds(capabilities, ENRICH_PHASE, include_paid=False)

    answered: list[str] = []
    notes: dict[str, str] = {}
    # The kinds that returned without putting a request on the wire, which is the distinction
    # decision 334 draws with the word RAN and `SourceResult.ran` carries. Collected for every
    # kind although only the required one is read, because a set built for one member is a set
    # the day a second source becomes required. An adapter that RAISED is in neither collection
    # and parks the required source, which is the conservative reading: nothing survived the
    # exception to say whether a request went out.
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

    # A kind the registry holds and this install cannot run. Reported under its own name rather
    # than omitted, because §6.6's board showing eight sources on one install and eleven on
    # another with nothing saying why is the state decision 377's note exists to prevent. Computed
    # from `requires` alone so that a kind filtered for any OTHER reason - `include_paid` above is
    # the live one - is not described to an operator as a missing credential.
    for kind, spec in sorted(sources.REGISTRY.items()):
        if spec.phase != ENRICH_PHASE or kind in wanted:
            continue
        if spec.requires and not capabilities.get(spec.requires, False):
            notes[kind] = f"{spec.requires} is not configured, so this source was not asked"

    detail: dict[str, Any] = {"answered": answered, "documents": documents}
    if notes:
        detail["notes"] = notes
    # AND THE PARK FIRES ONLY WHEN THE REQUIRED SOURCE RAN, which these lines now test rather than
    # assert. `available_kinds` closes one half of decision 334's distinction - a TMDB nobody has
    # configured never reaches the loop - and `SourceResult.ran` closes the other: a title whose
    # `tmdb_id` column is empty because TMDB holds no record of it is one TMDB was never asked
    # about, and parking it here spends a full eight-source re-crawl a day, for ever, under a
    # reason naming a connector that is working. It advances instead, and stage 4 parks it with
    # the counts and a thirty-day window. [M5.3 review cycle 1, M53-334-01]
    #
    # `failed` IS THE KINDS THAT RAN AND DID NOT ANSWER - in neither collection, which includes an
    # adapter that RAISED, for the reason `unasked`'s own comment gives: nothing survived the
    # exception to say whether a request went out. Decision 334's park is about the required
    # SOURCE, so a failure at any of its kinds is the condition and the reason names the kind that
    # actually failed. `wanted` is priority-ordered, so `required[0]` is the earliest one -
    # `tmdb:resolve` before `tmdb:detail`, which is the order the failure propagated in.
    # [M5.3 review cycle 2, m53-c2-334-01]
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

    IT WRITES AND IT DOES NOT FETCH, which is the property the whole milestone is named for. Every
    byte this stage reads came out of the content-addressed raw store stage 2 filled, so a parser
    that was wrong is repaired by re-running this stage and never by asking a host again - §8's
    "All fetched bytes land in the app's own raw store, so re-parsing is free forever"
    (`spec:398`). `derive/rebuild.py` imports no transport at all and a test holds it to that, so
    the property is a fact about the module rather than a promise about this call.

    THE COUNTS GO ON THE BOARD BECAUSE NOTHING ELSE CAN SAY THEM. `DeriveReport` carries the two
    ledgers' outcomes apart - §14.5 names "two distinct ledgers" and a board line reading "3
    curated rows applied" could not tell an operator which one applied them, or whether the other
    ran at all - and it carries `refused`, which is the scraped page that turned out to be another
    film. A derive that logged these instead would put the only account of what it did somewhere
    decision 345 says §6.6 cannot reach.

    A MISSING TITLE RAISES AND IS MEANT TO. `derive_title` answers `LookupError` rather than an
    empty report, `_run_stage` turns a raise into `fail`, and `_record_stop` checks the title
    exists before writing the board - so the one state this stage cannot describe is handled by
    the driver that already handles it, rather than by a second guard here that would disagree
    with `ready`'s.
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

    THE PREDICATE IS `derive/gate.py`'s AND NOT A SECOND ONE HERE. Decision 335 fixes it as one
    query - a non-whitespace `title.overview`, two distinct `review_store.review` sources, fifty
    words across them off the generated stored column - and this stage asks it, reads the answer
    and decides a verb. That split is what lets the boundary be asserted at 49 against 50 without
    a database walk of the whole pipeline, and it is why `measure` and `passes` are two calls: the
    counts go on the board whether the gate passed or not, because a title that cleared with two
    sources and fifty-one words is one an operator may want to look at.

    THIS IS THE PARK DECISION 336 WAS WRITTEN FOR, and it is the only one in this file whose
    deadline is not `waiting_on_the_world()`. The thing §8 says may change is not an operator - it
    is the world writing reviews of a film that came out last week - so the window is §8's own
    thirty days, counted from now by `gate.window_deadline()` and spelled in exactly one place.
    Nothing is asked of anybody, no attempt is spent (`queue.defer`), and the task comes back by
    itself. A park that auto-failed here would close a title for the crime of being new.

    WHAT COMES BACK WHEN THE WINDOW CLOSES IS A CRAWL, NOT A RE-COUNT, and for one review cycle it
    was a re-count. The two stages that can move these counts are stage 2, which fetches the
    reviews, and stage 3, which writes them; `pipeline._resume_index` answered the BOARD's stage,
    so the task that came back after thirty days re-entered HERE, re-ran `gate.measure` over the
    rows day one wrote, opened no socket and parked again with the same sentence, for ever - and
    the accrual §8 wrote the window for could not be seen by construction. The stage now declares
    `reask_from=2` in `pipeline.STAGES`, and a park here whose deadline has PASSED re-enters at
    stage 2. One made due BEFORE its deadline is an operator's retry, which still re-enters here
    and asks nothing of anyone - check 5 of this milestone's exit criterion and
    `test_a_retry_of_a_parked_gate_resumes_at_stage_four_and_makes_no_request` measure that - so
    the clock against the board's `retry_after` is what tells the two events apart.
    [M5.3 review cycle 2, m53-c2-gate-01, M53-C2-NET-03; decision 421]

    TWO WRITES, ONE TRUTH, ONE CONSTANT - AND THE SECOND WRITE IS ALREADY THE DRIVER'S. The queue
    decides when the task leases again and `acquisition_job.retry_after` is what §6.6 renders;
    `pipeline._record_stop` passes `outcome.until` to BOTH - `queue.defer` and `write_board` - so
    this stage returns one instant and cannot put two different dates in front of an operator.
    `write_board`'s own docstring records that the column "was added for §8 stage 4's thirty-day
    review-accrual window", written at M5.1 for this call; a stage that wrote the board itself
    would be the second writer of `acquisition_job` that `ready`'s docstring refuses.

    A MISSING TITLE RAISES, for `derive`'s reason one function up: `gate.measure` answers
    `LookupError` rather than zeros, because "0 sources, 0 words" on §6.6's board for a row that
    is absent is a sentence about a title that does not exist.
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


# --- stages 5-8: declared no-ops -----------------------------------------------------------------


async def dna_pack(_ctx: StageContext) -> Outcome:
    """§8 stage 5: the ported `packs.py` plus the craft supplement.
    **Owned by M5.4** (`ROADMAP-M5.md:298-312`).

    A declared no-op at M5.1.
    """
    return advance({"stub": NOT_IMPLEMENTED.format("M5.4")})


async def dna_extract(_ctx: StageContext) -> Outcome:
    """§8 stage 6: the LLM structured call(s). **Owned by M5.5** (`ROADMAP-M5.md:314-329`).

    A declared no-op at M5.1, AND THE ONLY PAID STAGE. It is marked `paid=True` in `pipeline.
    STAGES` today although it spends nothing, because the flag is what the driver's spend gate
    reads and a flag first set by the milestone that starts billing is a flag nobody tested. The
    gate lets a declared no-op through and refuses an implemented paid stage with no cap
    configured, so the day M5.5 gives this function a body is the day the pipeline parks here
    until M5.5 supplies the cap - which is §8's "paid stages (6) never auto-retry past the spend
    cap". THE MILESTONE IN THAT SENTENCE USED TO BE M5.7, which is decision 348's own title read
    backwards: "M5.1 owns the refusal, M5.5 owns the cap", and `ROADMAP-M5.md:314-329` puts both
    the cap and `0028` in M5.5 while M5.7 writes no migration at all. M5.7 owns the SURFACE for
    setting a spend guard (`ROADMAP-M5.md:348`), not the cap's existence - and this is the
    docstring an M5.5 author reads first, so it told them the correct outcome of their own commit
    was a pipeline parked at stage 6 for two further milestones.
    [M5.1 review cycle 4 second pass, M51-C4-PAID-06]

    ONE CLAIM THAT USED TO BE MADE HERE IS WITHDRAWN: "enforced by construction rather than by
    remembering". It is not. `implemented` is a hand-written literal in `pipeline.STAGES`, and
    nothing ties it to whether this function has a body - a milestone that writes the billing call
    and leaves the flag reads as a declared no-op to the gate and runs. What now holds the pair
    together is a test rather than a construction:
    `test_acquire_pipeline.py::test_every_stage_declared_a_no_op_returns_its_stub_marker` calls
    every `implemented=False` stage and asserts the marker above, so giving one a body reddens the
    build at the stage that got it. [M5.1 review cycle 1, M51-REV-04, M51-REV-PAID-01]
    """
    return advance({"stub": NOT_IMPLEMENTED.format("M5.5")})


async def verify(_ctx: StageContext) -> Outcome:
    """§8 stage 7: the ported trust boundary. Failures drop, never repaired.
    **Owned by M5.4** (`ROADMAP-M5.md:298-312`).

    A declared no-op at M5.1.
    """
    return advance({"stub": NOT_IMPLEMENTED.format("M5.4")})


async def project(_ctx: StageContext) -> Outcome:
    """§8 stage 8: the per-title alias-map projection.
    **Owned by M5.4** (`ROADMAP-M5.md:298-312`; `worker.py:1052`'s `dna-projection` row is its
    registry entry, already present with no `run` callable).

    A declared no-op at M5.1.
    """
    return advance({"stub": NOT_IMPLEMENTED.format("M5.4")})


# --- stage 9: place -----------------------------------------------------------------------------


async def active_store(conn: asyncpg.Connection) -> ArtifactStore | None:
    """The basis stage 9 places against, acquired the one way this app allows.

    `worker.py:363-393`'s `_active_store` is the argument and this is the same three lines in the
    same order: load the active row, `assert_not_broken`, `assert_matches`. It is repeated rather
    than imported because `worker.py` is the process loop and a domain package importing it would
    invert the direction every other module in `spielplan/` keeps - and repeated in FULL rather
    than partially, because the order is the whole of it: an `artifact_bundle` row that is active
    while its directory is gone loads with that version and `broken = True`, so the §10 comparison
    passes for an install that cannot produce one coordinate.

    Both guards RAISE, deliberately, and the driver turns a raised stage into `failed` - decision
    336's "this stage raised and will raise again", which is exactly what a broken install is. The
    bundle-LESS install is the other case and is not a failure at all: None comes back, the stage
    parks with `NO_ACTIVE_BUNDLE`, and §3.1 says that household is legal.

    THIS FUNCTION IS WHY THE DRAIN JOB IS A MODEL JOB. `worker.py`'s `MODEL_JOBS` names every job
    that fits against the active bundle, and `test_every_job_that_fits_against_the_active_bundle_
    is_named_in_model_jobs` derives that set by walking the call graph - so the job that calls
    `pipeline.drain` reaches `ArtifactStore.load_active` through here and must be named there.
    That is also correct behaviour rather than a guard to satisfy: a placement started against v1
    and committed after §10's flip stamps `title_placement` with a basis the app no longer serves.
    """
    store = await ArtifactStore.load_active(conn, settings().artifacts_dir)
    store.assert_not_broken()
    store.assert_matches(await artifacts.active_bundle_version(conn))
    return None if store.is_empty else store


async def place(ctx: StageContext) -> Outcome:
    """§8 stage 9: "feature vector per the feature contract -> Cold Tower -> e(t), b(t)".

    THIS STAGE IS SHIPPED AND THIS FUNCTION IS ITS FIRST CALLER. `placement/reconcile.py:208-212`
    is the `app_acquired` branch - `SELECT id FROM title WHERE origin = 'acquired' ORDER BY id` -
    and until now `app_acquired` appeared nowhere in the tree outside `reconcile.py` itself. M5.1
    supplies the caller and builds no part of stage 9 or 10.

    `app_acquired` IS A SCOPE-WIDE LIST AND NOT A PER-TITLE CALL. One task's stage 9 re-places
    every acquired title, which is cheap while that set is small - it is empty on every install
    today - and is a forward pass of one chunk (`CHUNK = 512`) for a household that has acquired
    a few hundred. A per-title scope is NOT added to `reconcile.py`: that module is §5.3's job and
    a fifth scope whose only caller is this one would put a second definition of "who needs
    placing" in the file whose two definitions disagreeing is what M4.13 records as ml05. If the
    acquired set grows enough to matter it is M5.6's or M5.7's measurement to take, with the
    board in front of them.

    Re-placing is idempotent by construction: `_UPSERT` is `ON CONFLICT (title_id, bundle_version)
    DO UPDATE` (`reconcile.py:233-247`), so a second run rewrites the same coordinate rather than
    adding a row. That is what makes D5's "running the same task twice produces the same rows"
    hold across a stage that touches more titles than its own.

    READ THE TITLE BACK. `place_titles` reports a non-finite placement per title and continues
    (`reconcile.py:298-301`), so a report with `placed > 0` does not say THIS title was placed.
    Stage 10 badges on the strength of a coordinate, so the stage that produces one has to be the
    one that checks it exists.
    """
    if ctx.title_id is None:
        return fail("stage 9 reached with no title id; stage 1 did not establish one")
    store = await active_store(ctx.conn)
    if store is None:
        # WITH A DEADLINE, because importing a bundle is the thing that may change and a park with
        # no deadline is a task no drain can ever lease again. The re-ask costs a walk of eight
        # declared no-ops and one `artifact_bundle` read: `active_store` refuses here, before any
        # placement runs, so the bundle-less household's daily re-ask is the cheapest walk this
        # pipeline has. [M5.1 review cycle 1, M51-CRASH-01, M51-REV-03]
        return park(NO_ACTIVE_BUNDLE, until=waiting_on_the_world())

    report = await reconcile.reconcile(ctx.conn, store, scope="app_acquired")
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
        # WITH A DEADLINE, and this was the one park in the file that carried none. A park with no
        # `until` is `queue.skip`, `queue.lease` claims `state = 'pending'` only, nothing in the
        # tree moves a row out of `skipped`, and `queue.enqueue` is `ON CONFLICT DO NOTHING` - so
        # the task was CLOSED FOR EVER on a condition that clears itself, while §6.6's board froze
        # at "stage 9, parked" for a title the nightly sweep may well place an hour later. That is
        # `waiting_on_the_world`'s own rule broken in the branch it was written for, and stage 10
        # one call later reads the IDENTICAL predicate - `placement not in ('cold_tower','warm')` -
        # and parks it WITH a deadline, so the two stages disagreed about one fact.
        # [M5.1 review cycle 2, d323-park-02; M5.1 review cycle 1, M51-CRASH-01, M51-REV-03]
        return park(NOT_PLACED, until=waiting_on_the_world(),
                    detail=detail | {"notes": report.notes[:4]})
    return advance(detail | {"placement": placement})


# --- stage 10: ready ----------------------------------------------------------------------------


async def ready(ctx: StageContext) -> Outcome:
    """§8 stage 10: "appears in ranking/search/explore with a 'new - model placement, no crowd
    data' badge until ratings accrue".

    THE BADGE AND THE SHELF ARE ALREADY BUILT AND THIS STAGE ADDS NO UI. `home/shelves.py:1015-
    1053` is the "New in the library" shelf, `why = "placed by the Cold Tower - no crowd data
    yet"`, and `shelves.py:474-484` computes the card's badge fields OUTSIDE `model` on purpose so
    §6.7's gating cannot make them vanish. This stage produces the state they read.

    So what is there to do? CHECK THE TWO COLUMNS THE SHELF READS, and refuse to stamp `ready`
    over a title that will not appear. `ready` is the board's terminal state; an operator reading
    it is reading a claim that the household can now see this title. A title stamped `ready`
    while Home cannot show it is a board that lies, and the two ways to get there are both real:
    a title that lost `is_owned` to a sweep between stage 1 and here, and one whose placement the
    Cold Tower could not produce.

    The board write itself is the DRIVER's, not this function's, for the same reason every park
    and every failure is: one writer of `acquisition_job` inside this pipeline means the stage
    number, the status and the reason are always written together and always from one place. A
    stage that wrote its own terminal status would be a second writer racing the first on the
    only row §6.6 reads.

    A GAP LEFT ON PURPOSE. `home/rail.py:395-397` has `placement_line` for stage 10 and no builder
    for an acquisition-stage line, so §6.8's rail says nothing about a title moving through this
    pipeline. Adding one here is not this milestone's: `rail.py` is M5.6's hotspot file
    (`ROADMAP-M5.md:331-346`) and a line added by M5.1 would be a second author in the file whose
    single ownership is the reason the milestones were split the way they were.
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
        # With a deadline: both columns are written by things that run on a timer - §7.2's sweep
        # re-derives `is_owned` from Jellyfin, and §5.3's nightly reconciliation places what is
        # unplaced - so this is decision 336's "waiting on something that may change" in its
        # plainest form, and the re-ask is two column reads. [M5.1 review cycle 1, M51-REV-03]
        return park(NOT_BADGEABLE.format(" and ".join(missing)), until=waiting_on_the_world())
    return advance({"badge": "new - model placement, no crowd data", "placement": row["placement"]})
