"""M4.11's exit criterion, measured against a two-member household and a real (fake) Jellyfin.

M4.11 is not in §12, so it measures its own stated goal:

    seen states flow both ways for both members without a sweep silently reverting the person,
    and the finish prompt arms once and stays answered

against the household §12's M1 row describes and nobody had ever assembled: **two members, a
library with a duplicated copy and a running series, two phones each.** Every one of the eleven
faults below was invisible to the single-member, single-copy, movies-only world the tests had,
and four of them are reverts -- the app writing over what the person just said, fifteen minutes
later, with nothing logged.

Eleven checks. **Pass is 11/11**; the parenthesised value is what each read before this milestone.

   1. explicit "not seen" on a duplicated title, then two sweeps: `unseen` after both,
      `adopted == 0`, and BOTH copies clear in Jellyfin          (was 'seen', adopted=1)
   2. a series marked seen, then a new episode recomputes the folder flag: still `seen`, and no
      DELETE against the Series id in the fake's write log       (was adopted=1, 'unseen')
   3. a member linked with no token, one owed row, two Played flags: adopted=2,
      owed_no_token=1, the owed row still owed, the member in `completed`
                                                                 (was adopted=0, completed=[])
   4. a revoked token with nothing owed, then an owed title that left the library: the badge
      stays `needs_relink` across both sweeps                     (was 'linked' after the first)
   5. a title removed from Jellyfin, then a completed sweep: `is_owned = false`, a fresh
      `owned_checked_at`, and gone from §6.2's candidate pool     (was still owned, still offered)
   6. two members on two different copies, three sweeps: `title.jellyfin_id` identical after all
      six `sync_user` calls, `resolve['relinked'] == 0` from the second
                                      (was jf-1, jf-1b, jf-1, jf-1b, jf-1, jf-1b; relinked climbing)
   7. a 404 on every Played write: `push_failed == the owed count`, `completed == []`, no
      promotion                                     (was completed=[user], promoted, no counter)
   8. an Episode session at 96%: `armed == 1` for the SERIES title, `unresolved == []`
                                                                 (was armed=0, unresolved=[episode])
   9. arm, decline, three further polls: one `playback_event` row, one push   (was 2 rows, 2 pushes)
  10. a member with one registered device, read from a context holding no local subscription
                                                                 (was 'on', offering only disable)
  11. the finish-prompt and Tonight payloads: a distinct `tag` and a non-null `url` on each
                                                                 (was neither field present)

WHAT THIS SCRIPT IS NOT ALLOWED TO DO, and the reason is M4.5's close-out: three of that
harness's four failures were the harness talking to a different database than the app. So every
number below is read back out of the database, out of `ops/fake_jellyfin.py`'s own write log, or
out of the payload the app handed the push sender -- never out of a variable this script set --
and the sweep figures come from the real `seen.sync_all` / `seen.sync_user`, never from a
re-implementation of them. The connection is set up through the app's own `db/pool.py`, for the
same reason.

CHECK 10 IS HALF A BROWSER FACT, AND THE HALF THIS SCRIPT CANNOT SEE IS NAMED RATHER THAN FAKED.
"the onboarding section reads `off`" is a rendering of `Onboarding.svelte`'s `pushState`, which
depends on whether *this* browser holds a PushSubscription -- something no server-side harness can
observe and nothing here pretends to. What is asserted is the server fact underneath it: this
member's second context is handed exactly the same list as the first, which is precisely why
`pushState` cannot be read off that list; plus the handle-never-the-endpoint rule and the
per-member scoping of the off switch. Measured against the pre-M4.11 tree, check 10 is the ONE of
the eleven that passes -- finding 21's defect is entirely in `Onboarding.svelte:57-65` and
`push.js:222-232`, so its server half is M2's contract unchanged and this is a regression guard
rather than a falsifier. The browser half is asserted in a real second context by
`e2e/specs/12-onboarding.spec.js`. Every other check here fails against that tree, which is the
only evidence that the eleven can fail at all.

Run it against a live Postgres. No bundle, no Docker, no server:

    TEST_DATABASE_URL=postgresql://... backend/.venv/Scripts/python ops/m411_exit_criterion.py

It creates and drops its own DATABASE, so it never runs against a household's data by accident --
0003 creates schemas of its own, so a search_path would not have isolated it. Output is ASCII:
Windows consoles crash on decorative glyphs.
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# Set before the first `spielplan` import, because `settings()` is `lru_cache`d and decision 181
# made §2's required config a refusal at construction: a process with no SESSION_SECRET raises
# rather than signing cookies with a constant from the public repository. `backend/tests/conftest.py`
# supplies the same two for the same reason. `setdefault` would read a developer's `.env` *after*
# this, and pydantic-settings ranks the environment above that file -- so these are set outright, on
# purpose: this script must never seal a household's real SECRETS_KEY into a scratch database, and a
# throwaway key is all the encryption here means. DATABASE_URL is deliberately left alone and
# `db/pool.open_pool` is never called: every connection below names the scratch database explicitly.
os.environ["SESSION_SECRET"] = "m411-exit-criterion-session-secret-not-a-real-one"
os.environ["SECRETS_KEY"] = "m411-exit-criterion-secrets-key-not-a-real-one"
os.environ.setdefault("PUBLIC_URL", "http://localhost:8080")

import asyncpg  # noqa: E402
import httpx  # noqa: E402
from spielplan.api import push as push_api  # noqa: E402
from spielplan.api import tonight as tonight_api  # noqa: E402
from spielplan.connectors.jellyfin import JellyfinClient  # noqa: E402
from spielplan.connectors.registry import JellyfinConfig, load_jellyfin, save_jellyfin  # noqa: E402
from spielplan.core import auth  # noqa: E402
from spielplan.db import migrate  # noqa: E402
from spielplan.db import pool as db_pool  # noqa: E402
from spielplan.push.send import device_handle  # noqa: E402
from spielplan.sync import playback, seen  # noqa: E402
from spielplan.tonight import pool as tonight_pool  # noqa: E402
from spielplan.tonight import rooms  # noqa: E402

JELLYFIN_URL = "http://jellyfin.test"
PATRICK_JF = "jf-user-patrick"
JENNY_JF = "jf-user-jenny"

# Mirrors `ops/fake_jellyfin.py`'s ITEMS, so every fake item resolves to a real title row -- the
# same table `backend/tests/test_seen_sync.py` keeps, with the runtime added because §6.2's pool
# filters on it and check 5 reads the pool. `jf-x` ("Christmas 2019") deliberately has no title:
# §4.2 carries `title.id` over from the corpus verbatim, so this connector never mints one.
TITLES = [
    (1, "movie", "Heat", 1995, "tt0113277", 949, 170),
    (2, "movie", "Prisoners", 2013, "tt1392214", 146233, 153),
    (3, "movie", "Paddington 2", 2017, None, 346648, 103),
    (6, "series", "Severance", 2022, "tt11280740", 95396, 48),
    (7, "series", "The Bear", 2022, "tt14452776", 136315, 30),
    (8, "movie", "Tampopo", 1985, "tt0092048", 11081, 114),
]

# §10 binds every §5.1 score to the basis it was computed in, and `session.bundle_version` is a
# foreign key, so check 5's pool and check 11's room both need one row to exist.
BUNDLE = "m411-exit"

# A push endpoint that survives sec-13's validator: https, a public name, not this household's own
# Jellyfin host and not `PUBLIC_URL`'s. A loopback or a private address is refused by design.
PHONE_ENDPOINT = "https://push.example.test/f/patrick-phone-1"

results: list[tuple[bool, str]] = []


def console(text: str) -> str:
    """`text` rendered in the encoding stdout actually has, escaping what it cannot carry.

    The same guard `ops/m45_exit_criterion.py` and `ops/m49_exit_criterion.py` carry, and for the
    same reason: the measured values below interpolate the app's own refusal strings, and
    `sync/seen.py`'s "no per-user Jellyfin token -- re-link required" carries an em dash. Under a
    cp850 console, printing one raises UnicodeEncodeError from inside `print` -- a traceback where
    the diagnosis belongs, in the one script whose whole job is diagnosis.
    """
    encoding = sys.stdout.encoding or "ascii"
    return text.encode(encoding, "backslashreplace").decode(encoding)


def check(ok: bool, label: str, measured: str, detail: str = "") -> None:
    """One line per numbered check, carrying the numbers and not only the verdict.

    The verdict is the FIRST argument, as in `ops/m45_exit_criterion.py` and
    `ops/m49_exit_criterion.py`, and the check's number is carried inside `label`. Not a stylistic
    echo: `test_static_contracts.py::test_no_milestone_exit_check_has_a_constant_predicate` reads
    `check`'s first positional argument and reports a truthy literal there as a verdict settled
    before the run. A signature taking the number first makes all eleven calls look exactly like
    the defect that guard exists to catch -- and a guard made unreadable by its subject is the same
    loss as a guard that is missing.

    Exactly eleven of these are recorded, which is what makes the published score mean "11/11" --
    `ops/m45_exit_criterion.py` counts every assertion it happens to make, and its denominator
    therefore drifts with the harness rather than with the criterion. Flushed per line, because a
    sweep takes seconds and a block-buffered stdout on a redirected pipe holds everything until the
    process ends: a harness whose whole purpose is to report cannot report from a buffer.
    """
    results.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {console(label)}", flush=True)
    print(f"         {console(measured)}", flush=True)
    if detail:
        for line in detail.splitlines():
            print(f"         {console(line)}", flush=True)


class PlayedWriteFails(httpx.AsyncBaseTransport):
    """The fake Jellyfin with §7.3's one write broken in a way that is not a credential problem.

    404 is the case §7.1's pin is about -- a server below 10.9 has no `/UserPlayedItems` route at
    all -- and it is not a 401, so it may never produce a re-link prompt: sending the household to
    re-type a password cannot add a route to their media server.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, status: int) -> None:
        self._inner = inner
        self._status = status

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if "UserPlayedItems" in request.url.path:
            return httpx.Response(self._status, json={"error": "no"})
        return await self._inner.handle_async_request(request)


class Household:
    """Two members, two phones each, one fake Jellyfin, and a way to put it all back.

    `fresh()` is the reason this is a class. Eleven scenarios share one database and one in-process
    fake, and a check that inherits the previous one's state is a check whose failure names the
    wrong milestone -- so each starts from the same household: the app tables emptied, the titles
    re-seeded, the fake reset, both members re-authenticated and the connector re-saved.
    """

    def __init__(self, conn: asyncpg.Connection, module: Any, transport: Any) -> None:
        self.conn = conn
        self.module = module
        self.transport = transport
        # The library and the episode lists as the fake ships them. Checks 1, 2, 5 and 6 change
        # them; a deep copy taken before anything runs is what lets `fresh()` undo that without a
        # pytest `monkeypatch` to unwind it.
        self.items = copy.deepcopy(module.ITEMS)
        self.episodes = copy.deepcopy(module.EPISODES)
        self.patrick = 0
        self.jenny = 0
        self.tokens: dict[str, str] = {}
        self.client: JellyfinClient | None = None
        # Every payload the app hands the push sender. Patched at `spielplan.push.send.send_to_user`
        # because both producers (`sync/playback.notify` and `api/tonight._invite`) import the
        # module and call the attribute -- which is what makes checks 9 and 11 measurable without a
        # VAPID keypair or a socket, and what keeps them reading the app's payload rather than this
        # script's idea of one.
        self.pushes: list[dict[str, Any]] = []

    async def open(self) -> None:
        from spielplan.push import send as push_send

        async def record(_conn, user_id, payload, **_kw):
            self.pushes.append({"user_id": user_id, **payload})
            return []

        push_send.send_to_user = record  # type: ignore[assignment]

        await self.conn.execute(
            "INSERT INTO artifact_bundle (version, manifest, state) VALUES ($1, $2::jsonb, 'active')",
            BUNDLE, {},
        )
        self.patrick = await self.conn.fetchval(
            "INSERT INTO app_user (name, role, jellyfin_user_id, jellyfin_link_state) "
            "VALUES ('patrick', 'admin', $1, 'linked') RETURNING id", PATRICK_JF
        )
        self.jenny = await self.conn.fetchval(
            "INSERT INTO app_user (name, role, jellyfin_user_id, jellyfin_link_state) "
            "VALUES ('jenny', 'member', $1, 'linked') RETURNING id", JENNY_JF
        )

    def with_duplicate(self, item_id: str, new_id: str) -> list[dict[str, Any]]:
        """A second Jellyfin item for the same film -- a "Movies 4K" library, or a second rip.

        §7.1 attaches one `jellyfin_id` per title, so this is the shape the whole duplicate half of
        the milestone is about, and the one the single-copy fixture could not express.
        """
        original = next(i for i in self.items if i["Id"] == item_id)
        return [*copy.deepcopy(self.items), {**copy.deepcopy(original), "Id": new_id}]

    async def fresh(
        self,
        *,
        items: list[dict[str, Any]] | None = None,
        tokens: set[str] | None = None,
    ) -> None:
        conn = self.conn
        # `title` cascades to user_score and title_jellyfin_item; the rest is named because nothing
        # cascades to it. `user_title` moved to that second list with 0022_model_basis: §10's
        # "Ledger observations always survive re-import" is now a RESTRICT, so the sweep's own
        # seen/unseen rows refuse the reset they used to be swept away by -- which would have made
        # every check after the first fail on a fixture, not on the milestone. [M4.13 plan §5 item 2]
        await conn.execute("DELETE FROM playback_event")
        await conn.execute("DELETE FROM push_subscription")
        await conn.execute("DELETE FROM session_participant")
        await conn.execute("DELETE FROM session")
        await conn.execute("DELETE FROM user_title")
        await conn.execute("DELETE FROM title")
        for title_id, kind, name, year, imdb, tmdb, runtime in TITLES:
            await conn.execute(
                "INSERT INTO title (id, kind, name, year, imdb_id, tmdb_id, runtime_min) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7)",
                title_id, kind, name, year, imdb, tmdb, runtime,
            )
        await conn.execute("UPDATE app_user SET jellyfin_link_state = 'linked'")

        self.module.ITEMS = copy.deepcopy(self.items) if items is None else items
        self.module.EPISODES = copy.deepcopy(self.episodes)
        self.module.state.reset()
        self.module.EPISODES_ASKED.clear()
        self.pushes.clear()

        # A new client per scenario: `JellyfinClient._episodes` caches each series' episode list for
        # the client's lifetime (decision 210(c) reads it once per series, not once per session row),
        # and check 2 adds an episode. A cache carried across scenarios would answer check 8 from
        # check 2's library.
        self.client = JellyfinClient(JELLYFIN_URL, self.module.API_KEY, transport=self.transport)
        self.tokens = {}
        for name in ("patrick", "jenny"):
            _jf_id, token = await self.client.authenticate_by_name(name, self.module.PASSWORD)
            self.tokens[name] = token
        wanted = {"patrick", "jenny"} if tokens is None else tokens
        ids = {"patrick": self.patrick, "jenny": self.jenny}
        await save_jellyfin(
            conn,
            url=JELLYFIN_URL,
            api_key=self.module.API_KEY,
            user_tokens={str(ids[n]): self.tokens[n] for n in sorted(wanted)},
        )

    # --- reads, all of them out of the database or the fake ---------------------------------

    async def state_of(self, user_id: int, title_id: int) -> tuple[str | None, bool]:
        row = await self.conn.fetchrow(
            "SELECT state, jf_synced_at FROM user_title WHERE user_id = $1 AND title_id = $2",
            user_id, title_id,
        )
        return (None, False) if row is None else (row["state"], row["jf_synced_at"] is not None)

    async def link_state(self, user_id: int) -> str | None:
        return await self.conn.fetchval(
            "SELECT jellyfin_link_state FROM app_user WHERE id = $1", user_id
        )

    async def pointer(self, title_id: int) -> str | None:
        return await self.conn.fetchval("SELECT jellyfin_id FROM title WHERE id = $1", title_id)

    async def copies_of(self, title_id: int) -> list[str]:
        return sorted(
            r["jellyfin_id"] for r in await self.conn.fetch(
                "SELECT jellyfin_id FROM title_jellyfin_item WHERE title_id = $1", title_id
            )
        )

    async def score(self, title_ids: list[int]) -> None:
        """A §5.1 score per member per title, which is what §6.2 step 3's pool reads.

        Both members, because `pool.build` admits only titles every seated member has scored: the
        mean of one score is that score, and a title only one person can see would otherwise
        outrank the household's actual agreement.
        """
        for title_id in title_ids:
            for user_id in (self.patrick, self.jenny):
                await self.conn.execute(
                    "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
                    "VALUES ($1, $2, 'movie', $3, 0.5, 0) "
                    "ON CONFLICT (user_id, title_id) DO UPDATE SET score = excluded.score",
                    user_id, title_id, BUNDLE,
                )

    async def pool_ids(self) -> list[int]:
        """§6.2 step 3's candidate pool, through the app's own query.

        `include_rewatches=True` on purpose: check 5 is about the **owned** filter, and leaving the
        rewatch filter in the way would let a `seen` row explain an absence that is supposed to be
        explained by ownership.
        """
        seats = [
            tonight_pool.Seat(participant_id=1, user_id=self.patrick, is_member=True),
            tonight_pool.Seat(participant_id=2, user_id=self.jenny, is_member=True),
        ]
        candidates = await tonight_pool.build(
            self.conn, seats=seats, kind="movie", budget_min=200,
            include_rewatches=True, bundle_version=BUNDLE,
        )
        return sorted(c.title_id for c in candidates)

    def linked(self, name: str) -> seen.LinkedUser:
        ids = {"patrick": self.patrick, "jenny": self.jenny}
        jf = {"patrick": PATRICK_JF, "jenny": JENNY_JF}
        return seen.LinkedUser(ids[name], name, jf[name], self.tokens[name], "linked")


# --- 1. the revert a duplicate copy used to guarantee -------------------------------------------


async def check_one(h: Household) -> tuple[bool, str, str]:
    """§7.3: "App is authoritative for explicit user actions", on the library `_collapse`'s own
    docstring is written for -- "Movies" and "Movies 4K".

    The push went to `title.jellyfin_id` alone, so the other copy kept `Played = true`; the next
    sweep OR-collapsed the copies, read "present + stamped + disagrees" and adopted `seen` straight
    back over the person. Two sweeps, because the first is where it happened and the second is where
    a household would notice it had happened twice.
    """
    module = h.module
    await h.fresh(items=h.with_duplicate("jf-1", "jf-1b"))
    module.state.played[PATRICK_JF].update({"jf-1", "jf-1b"})

    first = await seen.sync_all(h.conn, h.client)
    copies = await h.copies_of(1)
    answer = await seen.set_state(
        h.conn, h.client, await load_jellyfin(h.conn),
        user_id=h.patrick, title_id=1, state="unseen",
    )

    adopted, states = [], []
    for _ in range(2):
        report = await seen.sync_all(h.conn, h.client)
        adopted.append(report.adopted)
        states.append((await h.state_of(h.patrick, 1))[0])
    still_played = sorted(i for i in module.state.played[PATRICK_JF] if i.startswith("jf-1"))

    ok = (
        copies == ["jf-1", "jf-1b"]
        and answer["synced"] is True
        and adopted == [0, 0]
        and states == ["unseen", "unseen"]
        and still_played == []
    )
    return ok, (
        f"copies mapped={copies} first sweep adopted={first.adopted} unseen pushed={answer} "
        f"adopted per later sweep={adopted} state after each={states} "
        f"still Played in Jellyfin={still_played}"
    ), ""


# --- 2. a series is asymmetric (decision 210) --------------------------------------------------


async def check_two(h: Household) -> tuple[bool, str, str]:
    """Decision 210(4): a computed folder flag may mark but never un-mark.

    Jellyfin does not store Played on a Series; `Folder.FillUserDataDtoValues` computes
    `playedCount >= totalCount`. So the flag turns false the day a new episode lands with nobody
    having acted -- and the adopt branch's whole premise is that a disagreement is a human change
    made since the last agreement. An explicit "seen" became "unseen" on the day a show continued.

    The new episode is added to the fake's own episode list as well as dropping the folder flag,
    because that is what actually happens on the server: the list grows, and the computed flag is
    false as a consequence. Decision 210(a)'s other half is asserted in the same breath -- no
    DELETE may reach the Series id, because `Folder.MarkUnplayed` resets `Played`, `PlayCount`,
    `PlaybackPositionTicks` and `LastPlayedDate` on every recursive child, and §4.1 rule 5 leaves
    the app no episode identity with which to restore any of it.
    """
    module = h.module
    await h.fresh()
    await seen.sync_all(h.conn, h.client)
    marked = await seen.set_state(
        h.conn, h.client, await load_jellyfin(h.conn), user_id=h.patrick, title_id=6, state="seen"
    )
    posts = list(module.state.write_log)

    severance = next(i for i in module.ITEMS if i["Id"] == "jf-6")
    module.EPISODES["jf-6"] = [
        *module.EPISODES["jf-6"],
        {
            "Id": "jf-6-e3", "Name": "Episode 3", "Type": "Episode", "SeriesId": "jf-6",
            "SeriesName": "Severance", "ParentIndexNumber": 1, "IndexNumber": 3,
            "RunTimeTicks": int(severance["RunTimeTicks"]),
        },
    ]
    module.state.played[PATRICK_JF].discard("jf-6")

    report = await seen.sync_all(h.conn, h.client)
    state, _synced = await h.state_of(h.patrick, 6)
    deletes = [w for w in module.state.write_log if not w["played"]]

    ok = (
        marked["synced"] is True
        and posts == [{"user": PATRICK_JF, "item": "jf-6", "played": True}]
        and state == "seen"
        and report.adopted == 0
        and deletes == []
    )
    return ok, (
        f"seen pushed as={posts} episodes now={len(module.EPISODES['jf-6'])} "
        f"folder flag recomputed to false; adopted={report.adopted} state={state!r} "
        f"DELETEs in the write log={deletes}"
    ), ""


# --- 3. a half-made link is incomplete, not broken ---------------------------------------------


async def check_three(h: Household) -> tuple[bool, str, str]:
    """§7.3 + §3.3. The owed row is the FIRST title in `/Items` order on purpose: that is where the
    sweep used to stop, so everything after it in the page-set is the evidence.

    What must be true at once: the member's Jellyfin history arrives (that is what the link is for),
    the owed write stays owed rather than being abandoned or faked, the count is reported where
    §6.6's card can print it, the admin key is never tried (§14 risk 3), and the sweep counts as
    completed -- a half-made link is not an outage.
    """
    module = h.module
    await h.fresh(tokens={"jenny"})
    await h.conn.execute("UPDATE title SET jellyfin_id = 'jf-1' WHERE id = 1")
    await seen.set_state(h.conn, None, JellyfinConfig(), user_id=h.patrick, title_id=1, state="seen")
    module.state.played[PATRICK_JF].update({"jf-2", "jf-6"})

    report = await seen.sync_all(h.conn, h.client)
    owed_state, owed_synced = await h.state_of(h.patrick, 1)
    adopted_states = [(await h.state_of(h.patrick, t))[0] for t in (2, 6)]
    badge = await h.link_state(h.patrick)

    ok = (
        report.adopted == 2
        and report.owed_no_token == 1
        and "patrick" in report.completed
        and (owed_state, owed_synced) == ("seen", False)
        and adopted_states == ["seen", "seen"]
        and module.state.write_log == []
    )
    return ok, (
        f"adopted={report.adopted} owed_no_token={report.owed_no_token} "
        f"completed={report.completed} owed row={(owed_state, owed_synced)} "
        f"adopted titles 2 and 6={adopted_states} admin-key writes={module.state.write_log}"
    ), f"badge={badge!r} needs_relink={report.needs_relink}"


# --- 4. the badge follows the token, not the silence --------------------------------------------


async def check_four(h: Household) -> tuple[bool, str, str]:
    """Finding 2: a sweep sends the token only for rows with `jf_synced_at IS NULL`, so a quiet
    sweep exercises a revoked token NEVER -- and the promotion read `report.completed`, which is
    also what a household with nothing to say looks like. The badge then said green for a link that
    could write nothing, under a comment claiming the opposite.

    Both evidence-free paths in one scenario: a sweep with nothing owed at all, and a sweep whose
    only owed row is a title Jellyfin no longer lists. The token is genuinely revoked at the fake,
    so the only thing that could clear the badge is a write neither sweep makes.
    """
    await h.fresh()
    revoked = h.tokens["patrick"]
    h.module.state.tokens.pop(revoked, None)
    await h.conn.execute(
        "UPDATE app_user SET jellyfin_link_state = 'needs_relink' WHERE id = $1", h.patrick
    )

    first = await seen.sync_all(h.conn, h.client)
    after_first = await h.link_state(h.patrick)

    await h.conn.execute(
        "INSERT INTO title (id, kind, name, year, jellyfin_id, runtime_min) "
        "VALUES (4, 'movie', 'Chungking Express', 1994, 'jf-gone', 102)"
    )
    await seen.set_state(h.conn, None, JellyfinConfig(), user_id=h.patrick, title_id=4, state="seen")
    second = await seen.sync_all(h.conn, h.client)
    after_second = await h.link_state(h.patrick)

    ok = (
        after_first == "needs_relink"
        and after_second == "needs_relink"
        and second.owed_unreachable == 1
        and first.wrote == set()
        and second.wrote == set()
    )
    return ok, (
        f"badge after the quiet sweep={after_first!r} after the unreachable-debt sweep="
        f"{after_second!r} owed_unreachable={second.owed_unreachable} "
        f"members whose token actually wrote={sorted(first.wrote | second.wrote)}"
    ), ""


# --- 5. ownership is re-derived in both directions ---------------------------------------------


async def check_five(h: Household) -> tuple[bool, str, str]:
    """§7.2: "mark removed titles `is_owned = false` ... re-derived from Jellyfin, never trusted
    stale". Nothing in this codebase wrote false (cs-11), so a film deleted from the library kept
    ownership, stayed in §6.2's candidate pool and on the owned Home shelves, and its winner card
    deep-linked to an item id the server no longer has.

    The pool is read before and after the sweep with the same query, because the claim is that one
    title leaves it -- an "after" list on its own is satisfied by a pool that was always empty, and
    this statement is the most destructive in the milestone.
    """
    await h.fresh()
    await seen.sync_all(h.conn, h.client)
    await h.conn.execute(
        "INSERT INTO title (id, kind, name, year, jellyfin_id, runtime_min, is_owned, "
        "owned_checked_at) VALUES (4, 'movie', 'Chungking Express', 1994, 'jf-gone', 102, true, "
        "now() - interval '2 days')"
    )
    await h.score([1, 4])
    before = await h.pool_ids()

    report = await seen.sync_all(h.conn, h.client)
    after = await h.pool_ids()
    row = await h.conn.fetchrow("SELECT is_owned, owned_checked_at FROM title WHERE id = 4")
    dated = row["owned_checked_at"] > await h.conn.fetchval("SELECT now() - interval '1 hour'")
    kept = await h.conn.fetchval("SELECT count(*) FROM title WHERE is_owned")

    ok = (
        4 in before
        and report.unowned == 1
        and row["is_owned"] is False
        and dated
        and 4 not in after
        and 1 in after
        and kept == len(TITLES)
    )
    return ok, (
        f"pool before={before} unowned={report.unowned} is_owned={row['is_owned']} "
        f"owned_checked_at re-dated={dated} pool after={after} titles still owned={kept}"
    ), ""


# --- 6. the representative pointer does not move -----------------------------------------------


async def check_six(h: Household) -> tuple[bool, str, str]:
    """§7.1's single `jellyfin_id` is the deep link and the single-write representative, and
    `playback.observe` resolves a `/Sessions` row against that one column -- so a pointer that moves
    loses §7.3's finish prompt for whoever is not on the copy it moved to, permanently, because the
    user order is fixed.

    It moved every user sweep, because it was written to the copy *this* user had played. Sampled
    per `sync_user` call rather than per sweep, because a sweep ends on whichever member went last
    and the flip is invisible from outside it. `sync_user` is called the way a caller driving one
    member does -- resolving for itself -- which is what puts a `resolve` report on each of the six.
    """
    module = h.module
    await h.fresh(items=h.with_duplicate("jf-1", "jf-1b"))
    module.state.played[PATRICK_JF].add("jf-1")
    module.state.played[JENNY_JF].add("jf-1b")
    users = await seen.linked_users(h.conn, await load_jellyfin(h.conn))

    pointers, relinked = [], []
    for _sweep in range(3):
        for user in users:
            report = seen.SyncReport()
            await seen.sync_user(h.conn, h.client, user, report)
            pointers.append(await h.pointer(1))
            relinked.append(report.resolve["relinked"])

    ok = (
        len(users) == 2
        and len(pointers) == 6
        and len(set(pointers)) == 1
        and relinked[1:] == [0, 0, 0, 0, 0]
    )
    return ok, (
        f"members={[u.name for u in users]} pointer after each sync_user={pointers} "
        f"relinked per call={relinked}"
    ), ""


# --- 7. a failed write is a counted fact, not silence ------------------------------------------


async def check_seven(h: Household) -> tuple[bool, str, str]:
    """cs-05. Before `push_failed` existed, `SyncReport` had no way to say the app->Jellyfin
    direction was dead: the non-auth branch logged one warning and `sync_user` carried on, so §6.6's
    card printed "pushed 0 - adopted 0 - unchanged N" -- which is also what a healthy quiet sweep
    prints -- and the promotion at the foot of `sync_all` restored a flagged link to healthy.

    One owed row per member, so `completed` has to be empty rather than naming whichever member
    happened to owe nothing: the whole write direction is broken, which is what a 404 on
    `/UserPlayedItems` means, and a sweep that could not write is not a complete sweep for anybody.
    """
    await h.fresh()
    await seen.set_state(h.conn, None, JellyfinConfig(), user_id=h.patrick, title_id=1, state="seen")
    await seen.set_state(h.conn, None, JellyfinConfig(), user_id=h.jenny, title_id=2, state="seen")
    owed = await h.conn.fetchval("SELECT count(*) FROM user_title WHERE jf_synced_at IS NULL")

    broken = JellyfinClient(
        JELLYFIN_URL, h.module.API_KEY, transport=PlayedWriteFails(h.transport, 404)
    )
    report = await seen.sync_all(h.conn, broken)
    badges = [await h.link_state(h.patrick), await h.link_state(h.jenny)]
    still_owed = await h.conn.fetchval("SELECT count(*) FROM user_title WHERE jf_synced_at IS NULL")

    ok = (
        owed == 2
        and report.push_failed == owed
        and report.completed == []
        and report.pushed == 0
        and badges == ["linked", "linked"]
        and still_owed == owed
        and bool(report.push_errors)
    )
    return ok, (
        f"owed={owed} push_failed={report.push_failed} completed={report.completed} "
        f"pushed={report.pushed} badges={badges} still owed={still_owed}"
    ), f"reasons={report.push_errors}"


# --- 8. an episode arms its series' prompt -----------------------------------------------------


async def check_eight(h: Household) -> tuple[bool, str, str]:
    """Decision 210: Jellyfin never plays a Series. The `/Sessions` row carries the **episode's**
    own `Id` and a `SeriesId` for the folder, so resolving `item_id` put every television session in
    `report.unresolved` -- which nothing logged and no surface showed -- and the whole Series
    partition sat outside §13's capture loop while "rating capture > 70%" read healthy.

    End to end through the fake rather than off a constructed session row, because the shape is half
    the bug: the double used to hand back the Series id itself and emit no `Type`, so both the M1
    exit criterion and e2e 08 certified a behaviour that existed only against the double.
    """
    await h.fresh()
    await seen.sync_all(h.conn, h.client)
    await h.module.force_session(
        h.module.SessionControl(user_id=PATRICK_JF, item_id="jf-6", fraction=0.96)
    )

    report = await playback.poll(h.conn, h.client)
    rows = await h.conn.fetch(
        "SELECT title_id, prompt_state, progress FROM playback_event WHERE user_id = $1 ORDER BY id",
        h.patrick,
    )

    ok = (
        report.armed == 1
        and report.unresolved == []
        and [r["title_id"] for r in rows] == [6]
        and [r["prompt_state"] for r in rows] == ["armed"]
    )
    return ok, (
        f"armed={report.armed} unresolved={report.unresolved} watching={report.watching} "
        f"prompt rows={[(r['title_id'], r['prompt_state'], round(r['progress'], 2)) for r in rows]} "
        f"episode lists read={h.module.EPISODES_ASKED}"
    ), ""


# --- 9. a declined viewing stays declined ------------------------------------------------------


async def check_nine(h: Household) -> tuple[bool, str, str]:
    """Finding 11, and `answer`'s own rule at last enforced: "a card that comes back after being
    dismissed teaches people to ignore the banner".

    A "no" produced neither an open prompt nor a `seen` row, the film sat above the threshold for
    another ten minutes, and every one of those polls armed a fresh prompt and sent a fresh push
    (measured: two rows, two pushes). Decision 211 makes the decline an explicit `unseen` -- so the
    sweep cannot adopt Jellyfin's Played flag into the absence the dismissal used to leave either.
    """
    await h.fresh()
    await seen.sync_all(h.conn, h.client)
    h.pushes.clear()
    await h.module.force_session(
        h.module.SessionControl(
            user_id=PATRICK_JF, item_id="jf-1", fraction=0.95, session_id="living-room-tv"
        )
    )
    await playback.poll(h.conn, h.client)
    event = await h.conn.fetchval(
        "SELECT id FROM playback_event WHERE user_id = $1 ORDER BY id", h.patrick
    )
    answered = await playback.answer(
        h.conn, user_id=h.patrick, event_id=event, finished=False, client=h.client
    )

    for _ in range(3):
        await playback.poll(h.conn, h.client)

    rows = await h.conn.fetch(
        "SELECT prompt_state FROM playback_event WHERE user_id = $1 ORDER BY id", h.patrick
    )
    prompts = [p for p in h.pushes if p.get("kind") == "playback.finished"]
    declined, _synced = await h.state_of(h.patrick, 1)

    ok = (
        len(rows) == 1
        and [r["prompt_state"] for r in rows] == ["dismissed"]
        and len(prompts) == 1
        and answered["seen"] is False
        and declined == "unseen"
    )
    return ok, (
        f"playback_event rows={len(rows)} states={[r['prompt_state'] for r in rows]} "
        f"pushes={len(prompts)} the decline wrote={declined!r}"
    ), ""


# --- 10. one member, one device, and a context that holds none ---------------------------------


async def check_ten(h: Household) -> tuple[bool, str, str]:
    """§4.2 and §6's preamble, as far as a server-side harness can honestly reach.

    "The onboarding section reads `off` and offers the enable control" is a rendering of
    `Onboarding.svelte`'s `pushState`, which depends on whether THIS browser holds a
    PushSubscription -- a fact no process without a browser can observe, and one this script
    deliberately does not fake.

    What it measures instead is the server fact that makes the client rule necessary, and it is
    finding 21's own sentence: **this member's second context gets exactly the same answer as the
    first.** `GET /api/push/state` returns "this member's devices, never the household's" -- all of
    them, not this one -- so the two contexts below are indistinguishable in the payload, and
    `pushState` therefore cannot be derived from `subscriptions.length` no matter how the component
    is written. The other two halves are the ones that keep the list safe to render at all: a device
    is named by the endpoint's HANDLE and never by the endpoint (a push endpoint is a bearer
    capability), and the off switch is scoped to the member who owns the row, so the other member's
    context can neither see it nor silence it.

    This check is therefore a GUARD and not a falsifier, and measurement says so: it is the one of
    the eleven that also passes against the pre-M4.11 tree, because finding 21's defect is entirely
    in `Onboarding.svelte:57-65` and `push.js:222-232` and the server half is M2's contract
    unchanged. The behaviour the criterion's row names is asserted in a real second browser context
    by `e2e/specs/12-onboarding.spec.js`.

    The route functions are called directly with the app's own `SessionUser`: FastAPI resolves the
    two dependencies per request and there is no request here, but the bodies are the product rules
    and a second copy of them in this file would be the thing the harness is supposed to measure.
    """
    await h.fresh()
    me = auth.SessionUser(
        id=h.patrick, name="patrick", role="admin", must_change_password=False,
        session_id="m411-exit", auth_method="password", admin_verified_at=None,
    )
    # The same member, on the phone that holds no local subscription: a different session, the same
    # account. This is the context the criterion's row is about.
    second_context = auth.SessionUser(
        id=h.patrick, name="patrick", role="admin", must_change_password=False,
        session_id="m411-exit-second-phone", auth_method="password", admin_verified_at=None,
    )
    other = auth.SessionUser(
        id=h.jenny, name="jenny", role="member", must_change_password=False,
        session_id="m411-exit-jenny", auth_method="password", admin_verified_at=None,
    )
    body = push_api.SubscriptionIn(
        endpoint=PHONE_ENDPOINT,
        keys={"p256dh": "BJ-m411-public-key", "auth": "m411-auth-secret"},
        device_label="Patrick's iPhone",
    )
    await push_api.subscribe(body, me, h.conn)

    state = await push_api.state(me, h.conn)
    listed = state["subscriptions"]
    rows = await h.conn.fetchval(
        "SELECT count(*) FROM push_subscription WHERE user_id = $1", h.patrick
    )
    elsewhere = await push_api.state(second_context, h.conn)
    theirs = await push_api.state(other, h.conn)

    refused: object = None
    try:
        await push_api.unsubscribe(push_api.EndpointIn(endpoint=PHONE_ENDPOINT), other, h.conn)
    except Exception as exc:  # fastapi.HTTPException, reported by its status code
        refused = getattr(exc, "status_code", type(exc).__name__)
    survived = await h.conn.fetchval("SELECT count(*) FROM push_subscription")
    labels = [d["device_label"] for d in listed]
    second = elsewhere["subscriptions"]

    ok = (
        rows == 1
        and len(listed) == 1
        and listed[0]["device"] == device_handle(PHONE_ENDPOINT)
        and "endpoint" not in listed[0]
        and "auth" not in listed[0]
        and [d["device"] for d in elsewhere["subscriptions"]] == [d["device"] for d in listed]
        and theirs["subscriptions"] == []
        and refused == 404
        and survived == 1
    )
    return ok, (
        f"rows for this member={rows} state lists={[d['device'] for d in listed]} "
        f"label={labels} this member's second context is handed={[d['device'] for d in second]} "
        f"the other member's context sees={theirs['subscriptions']} "
        f"their delete answered={refused} rows surviving={survived}"
    ), (
        "a guard, not a falsifier: the server answers both of this member's contexts identically, "
        "which is WHY data-push-state must come from the browser's own subscription. That half is "
        "asserted in a real second context by e2e/specs/12-onboarding.spec.js; no browser is "
        "faked here"
    )


# --- 11. two notifications, two tags, two destinations -----------------------------------------


async def check_eleven(h: Household) -> tuple[bool, str, str]:
    """syncpush-10. `tag` and `url` are the two fields the service worker cannot invent, and neither
    sender set them: its fallbacks are the literal `'spielplan'` and `'/'`, so every notification
    this app sent shared one replacement key. A §6.2 invitation silently replaced an unread §7.3
    finish prompt -- on the phone, where the in-app banner is not the thing the member is looking at.

    Both payloads are taken from the real producers (`sync/playback.notify` and
    `api/tonight._invite`), as they were handed to the sender.
    """
    await h.fresh()
    await seen.sync_all(h.conn, h.client)
    h.pushes.clear()

    await h.module.force_session(
        h.module.SessionControl(user_id=PATRICK_JF, item_id="jf-6", fraction=0.96)
    )
    await playback.poll(h.conn, h.client)

    room = await rooms.open_session(
        h.conn, host_user_id=h.patrick, kind="movie", budget_min=130,
        include_rewatches=False, bundle_version=BUNDLE,
    )
    # `_invite`'s first parameter is the FastAPI `Request`, which its body never reads; the payload
    # is built from the session and the room code alone.
    await tonight_api._invite(
        None, h.conn, session_id=room["session_id"], host_user_id=h.patrick,
        room_code=room["room_code"],
    )

    prompt = next((p for p in h.pushes if p.get("kind") == "playback.finished"), None)
    invite = next((p for p in h.pushes if p.get("kind") == "tonight.invite"), None)

    ok = (
        prompt is not None
        and invite is not None
        and bool(prompt.get("tag"))
        and bool(invite.get("tag"))
        and prompt["tag"] != invite["tag"]
        and bool(prompt.get("url"))
        and bool(invite.get("url"))
    )
    return ok, (
        f"finish prompt tag={(prompt or {}).get('tag')!r} url={(prompt or {}).get('url')!r} "
        f"tonight invite tag={(invite or {}).get('tag')!r} url={(invite or {}).get('url')!r}"
    ), f"kinds delivered={[p.get('kind') for p in h.pushes]}"


CHECKS = [
    (1, 'an explicit "not seen" on a duplicated title survives two sweeps', check_one),
    (2, "a series stays seen when a new episode recomputes the folder flag", check_two),
    (3, "a tokenless link adopts the library and keeps its owed write owed", check_three),
    (4, "a revoked token stays needs_relink across two evidence-free sweeps", check_four),
    (5, "a title removed from Jellyfin is un-owned and leaves Tonight's pool", check_five),
    (6, "the representative pointer holds across six sync_user calls", check_six),
    (7, "a 404 on every Played write is counted and blocks promotion", check_seven),
    (8, "an Episode session at 96% arms its series' prompt", check_eight),
    (9, "a declined viewing leaves one row and one push", check_nine),
    (10, "one device is listed by handle and is the owner's alone", check_ten),
    (11, "the two notifications carry a distinct tag and a destination", check_eleven),
]


async def main() -> int:
    dsn = os.environ.get("TEST_DATABASE_URL") or _dsn_from_env_test()
    if not dsn:
        print("TEST_DATABASE_URL is unset and .env.test does not supply it.")
        return 2

    print("\nM4.11 exit criterion -- two members, a duplicated copy, a running series\n", flush=True)

    # A dedicated DATABASE, not a schema: 0003 creates `display` and `review_store`, which are
    # database-global, so a search_path could not isolate this run from a household's data. Named
    # with this run's pid so two concurrent runs cannot drop each other's database.
    admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
    scratch = f"spielplan_m411_exit_p{os.getpid()}"
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {scratch}")
    finally:
        await admin.close()

    conn: asyncpg.Connection | None = None
    try:
        conn = await asyncpg.connect(dsn.rsplit("/", 1)[0] + f"/{scratch}")
        # The app's own connection setup, not a bare connect: `db/pool.py` registers the json/jsonb
        # codec every caller depends on, and a harness that skips it measures a database the app
        # never talks to. M4.5's close-out found three of its four failures there.
        await db_pool._init_connection(conn)
        await migrate.apply_all(conn)

        module, transport = _fake_jellyfin()
        household = Household(conn, module, transport)
        await household.open()

        for number, title, runner in CHECKS:
            label = f"{number:2d}. {title}"
            try:
                ok, measured, detail = await runner(household)
            except Exception as exc:
                # One check's crash fails that check and no other. The score's denominator is the
                # criterion's eleven, not however far the harness got: a run that stops at six and
                # prints "6/6 checks passed" is the failure mode an exit criterion exists to rule
                # out, and it is the shape `ops/m45_exit_criterion.py` still has.
                check(
                    False, label, f"the check stopped on {type(exc).__name__}: {exc}",
                    traceback.format_exc(),
                )
                continue
            check(ok, label, measured, detail)
    except Exception as exc:
        # Everything outside a check: the connect, the migration, the fake, the household seed.
        # Reported as the failures they are, so the exit code stays non-zero and the run still ends
        # in a score rather than in a traceback where the sentence naming the cause belongs.
        stopped = f"the run stopped on {type(exc).__name__}: {exc}"
        trace = traceback.format_exc()
        for index, (number, title, _runner) in enumerate(CHECKS[len(results):]):
            check(False, f"{number:2d}. {title}", stopped, trace if index == 0 else "")
    finally:
        if conn is not None:
            await conn.close()
        admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        finally:
            await admin.close()

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed}/{len(CHECKS)} checks passed", flush=True)
    for ok, label in results:
        if not ok:
            print(f"  FAILED: {console(label)}", flush=True)
    return 0 if passed == len(CHECKS) else 1


def _dsn_from_env_test() -> str | None:
    """`.env.test`'s TEST_DATABASE_URL, read the way `backend/tests/conftest.py` reads it.

    The same convenience and the same limit: an untracked file names the server, so the line below
    is the only place this script decides which host it is allowed to create a database on.
    """
    path = ROOT / ".env.test"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "TEST_DATABASE_URL":
            return value.strip()
    return None


def _fake_jellyfin() -> tuple[Any, httpx.ASGITransport]:
    """`ops/fake_jellyfin.py` mounted in-process, exactly as `tests/conftest.py` mounts it.

    Registered in `sys.modules` before it is executed, which is what `import` itself does: the
    module carries `from __future__ import annotations`, so Pydantic resolves its models'
    annotations through `sys.modules[cls.__module__]` and a missing entry surfaces as a
    `class-not-fully-defined` error naming a model rather than this function.

    A real HTTP server over ASGI rather than a mock, because §7.3's claims are HTTP facts -- and
    because this fake is a refuser: it rejects the admin API key on `/UserPlayedItems` on purpose,
    which is the only thing that turns "we use per-user tokens" from a comment into a measurement.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fake_jellyfin", ROOT / "ops" / "fake_jellyfin.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.state.reset()
    return module, httpx.ASGITransport(app=module.app)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
