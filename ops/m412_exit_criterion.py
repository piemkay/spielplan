"""M4.12's exit criterion: a real evening resolves on the shipped 696-title owned pool.

M4.12 is in §12 (decision 224), and its row's criterion is a *household* sentence rather than a
suite: two members and one guest seat on one phone reach 54e's blind reveal, the pairs arrive
inside §6's 1.5 s per battle, and nothing one phone does stops the other phone. That sentence is
the one thing this repository's own fixtures structurally cannot say. Every candidate pool in
`backend/tests` is four to twelve titles; the shipped owned pool is 696, and the three faults
that made an evening unplayable -- the pair search at 36 s for a member and 126 s for a guest
seat, the `voting -> ballot` transition living only inside a POST handler, and a pool of two or
three candidates converging at zero answers with no route able to close the room -- are all
invisible below a few hundred candidates.

Twelve checks. **Pass is 12/12**; the parenthesised value is what each read before this milestone.

   1. two members and one guest seat on one phone play through to the reveal: a winner card,
      `participants == 3`, one `session_outcome` row     (was: the room never revealed at all --
      `submitted_count` counts guest seats and Submit was bound to the viewer's own seat)
   2. the first pair for a GUEST seat (no profile, so every candidate straddles)
                                                         (was 125.9 s against a 1.5 s budget)
   3. the first pair AND the first answer for a zero-label MEMBER seat
                                                         (was 36.2 s and 24.1 s)
   4. an unrelated `GET /api/tonight/rooms` from a second cookie while an answer is in flight,
      and the largest pause between two wakeups of a 10 ms timer during it
                                                         (was: the read did not return at all
                                                          until the answer had finished)
   5. `play.finish` made to raise once on the last answer, then a plain GET of the session and
      of the ballot                                      (was: 'voting' for ever, slate empty)
   6. `asyncio.gather` of a member's join and the host's start
                                                         (was: both 200, the seat outside the
                                                          frozen snapshot, the room unclosable)
   7. `asyncio.gather` of an undo and an answer on one seat, then one more answer
                                                         (was: UniqueViolationError -> 500, and
                                                          every later tap a 500 for ever)
   8. a pool of exactly two candidates, and one of exactly three
                                                         (was: `converged` at zero answers with
                                                          `ended_by` NULL and nothing to end it)
   9. no route, spec file, coverage row or normative sentence funds a TV client (decision 165)
  10. no direct `play.finish` call in `backend/spielplan/api/tonight.py` -- the lifecycle has one
      owner, `play.settle`
  11. `SELECT DISTINCT latency_ms FROM session_answer` over the evening just played
                                                         (was `{0}` for every row ever written)
  12. the coverage contract: `current_milestone`, M4.12's rows, and the waiver count

WHY THE BUNDLE IS REQUIRED AND THE FIXTURE IS REFUSED. Checks 2, 3 and 4 are the three the suite
cannot hold: a six-title pool serves its first pair in milliseconds whatever the selector costs,
so a green suite is silent about all three. `MIN_REAL_TITLES` is `ops/m49_exit_criterion.py`'s
constant and refuses for its reason -- M4.8's `make_bundle(pool_titles=700)` tops out at 708
titles against the export's 19,071, so the one fixture that could reach this *pool* size cannot
reach this *bundle* size, and the refusal is on the number that separates them. `MIN_REAL_POOL`
is then checked against the owned, scored library the app's own fold-in produced -- the upper
bound on `pool.build`'s candidate set, before its kind, budget and rewatch filters -- because a
real bundle imported into a household that owns nothing is the fixture again wearing the
corpus's name.

WHAT THIS SCRIPT IS NOT ALLOWED TO DO, and the reason is M4.5's close-out: three of that
harness's four failures were the harness talking to a different database than the app. So the
evening below is played through the real ASGI app over HTTP with three real cookie jars, the pool
is built by `play.start` and never by a query written here, and every number is read back out of
the database, out of a response body, or off a clock -- never out of a variable this script set.
The one measurement genuinely this script's own is `latency_ms`, and that is the point of check
11: the client is what holds that clock, so the harness holds it the way `tonight.svelte.js`
does -- armed when the card arrives, read when the answer is posted.

CHECKS 9, 10 AND 12 ARE SOURCE READS, AND ARE NAMED AS SUCH RATHER THAN DRESSED UP. Nothing in a
running stack can tell you that a coverage row was deleted rather than waived, and the criterion
asks for exactly that. Check 12's other half -- `pytest backend/tests -q` green and
`npm --prefix e2e run fresh` green -- is two commands this script deliberately does not shell out
to: a harness that ran the suite would report its own subprocess's opinion of a tree it had just
changed, and both commands are in `docs/TESTING.md` where an operator reads the output
themselves. What is checked here is the part a green suite cannot show afterwards: that the
milestone was closed at `current_milestone = "M4.12"`, that every M4.12 row names a test, and
that no waiver was added to the one M0 row carrying the project's only one.

Run it against a live Postgres, with the bundle reachable. No Docker, no server, no browser:

    CORPUS_BUNDLE_DIR=/path/to/export_bundle/v20260828 \\
    TEST_DATABASE_URL=postgresql://... \\
      backend/.venv/Scripts/python ops/m412_exit_criterion.py

It creates and drops its own DATABASE, so it never runs against a household's data by accident --
0003 creates schemas of its own, so a search_path would not have isolated it. Output is ASCII:
Windows consoles crash on decorative glyphs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import tomllib
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# Set before the first `spielplan` import, because `settings()` is `lru_cache`d and decision 181
# made §2's required config a refusal at construction: a process with no SESSION_SECRET raises
# rather than signing cookies with a constant from the public repository. `setdefault` would read
# a developer's `.env` *after* this and pydantic-settings ranks the environment above that file,
# so these two are set outright: this script must never seal a household's real SECRETS_KEY into
# a scratch database, and a throwaway key is all the encryption here means.
os.environ["SESSION_SECRET"] = "m412-exit-criterion-session-secret-not-a-real-one"
os.environ["SECRETS_KEY"] = "m412-exit-criterion-secrets-key-not-a-real-one"
os.environ.setdefault("PUBLIC_URL", "http://localhost:8080")

import asyncpg  # noqa: E402
import httpx  # noqa: E402
from spielplan.api import tonight as tonight_api  # noqa: E402
from spielplan.core import config as core_config  # noqa: E402
from spielplan.db import migrate  # noqa: E402
from spielplan.db import pool as db_pool  # noqa: E402
from spielplan.importer import bundle as bundle_import  # noqa: E402
from spielplan.models.artifacts import ArtifactStore  # noqa: E402
from spielplan.scoring import backbone as bb  # noqa: E402
from spielplan.scoring import foldin  # noqa: E402
from spielplan.tonight import play as play_rules  # noqa: E402
from spielplan.tonight import round as round_rules  # noqa: E402

# `ops/m49_exit_criterion.py`'s constant, restated rather than imported: the two scripts are read
# one at a time by an operator, and a floor that lives in the other file is a floor nobody reading
# this one can check. The export ships 19,071 titles; `make_bundle(pool_titles=700)` tops out at
# 708, which is above `MIN_REAL_POOL` and nowhere near this.
MIN_REAL_TITLES = 5_000

# The candidate pool the app builds for two seated members, below which checks 2, 3 and 4 are
# measuring a fixture. The shipped owned pool is 696 movies and this floor sits deliberately
# under it: a household that has un-owned a few films since the export is still the household
# this row is about, and a floor tracking the exact figure would refuse them.
MIN_REAL_POOL = 600

# §6's preamble: "under 1.5 s per battle" on §2's 4 vCPU reference box.
BATTLE_BUDGET_S = 1.5

# The criterion's own row 4. Not a latency budget -- a *blocking* budget: the loop is shared by
# every phone in the household, the hub's fan-out and `/api/health`, so this is the longest any
# one handler may own it.
LOOP_BUDGET_MS = 200.0

# The controls the evening is played under. The budget is the slider's ceiling and rewatches are
# included, both for one reason: §6.2 step 3's filters must not be what makes the pool small,
# because the pool's size is the thing checks 2, 3 and 4 are measuring.
BUDGET_MIN = 200
INCLUDE_REWATCHES = True

ADMIN_PASSWORD = "an-exit-criterion-admin-password"
MEMBER_PASSWORD = "an-exit-criterion-member-password"

# Check 9's exclusions, each with the reason it is not evidence. A grep reporting these would be
# unfalsifiable: decision 165 is *written* in the proposals register, the milestone plans are the
# record of the work that deleted the route, and v1.1 is the superseded spec this project keeps
# precisely so that what it used to say can still be read.
TV_GREP_SKIP = (
    "docs/milestones/",
    "docs/spec-v2.2-proposals.md",
    "docs/media-graph-spec_v1.1.md",
    "backend/tests/pglite/",
    "node_modules/",
)

# `/tv` as a route, never `imdb/tmdb/tvdb`. §7.1's ProviderIds line is in the spec and in the
# coverage map and says nothing about a kiosk; a raw `grep -rn "/tv"` reports it, which is the
# first of the two reasons this check is a rule rather than a shell one-liner.
TV_ROUTE = re.compile(r"/tv(?![a-z])")

# The second reason, and the sharper one: the phrases are also how this codebase RECORDS the
# retirement. The coverage map's note where the deleted row stood quotes §12's struck cell
# verbatim, and §12's own M4.12 paragraph says the M4 row loses it -- a check reporting those
# would be reading the evidence of the deletion as evidence against it. So prose is measured
# in exactly two places where a sentence can only be funding: the normative spec's kiosk by
# name, and §12's M4 cell itself, which is a table row and not a sentence about one.
NORMATIVE_SPEC = "docs/spielplan-spec_v2.1.md"
COVERAGE_MAP = "backend/tests/spec_coverage.toml"
TV_KIOSK = re.compile(r"TV kiosk", re.IGNORECASE)
M4_ROW = re.compile(r"^\|\s*\*\*M4\*\*\s*\|")
TV_COVERAGE_ROW = 'id = "tonight-rank-tv-kiosk-route"'

# The suffixes check 9 reads. Narrow on purpose: `pglite.data` is a binary blob carrying the two
# letters in some compressed span, and a check that reported it would be reporting an artefact of
# its own reading rather than a sentence anybody wrote.
TV_GREP_SUFFIXES = {".js", ".svelte", ".toml", ".md", ".py", ".ts"}

# The eighteen rows §12's M4.12 cell is measured by: seventeen written before the code, plus
# the vectorised pair search that shipped ahead of the milestone under `ROADMAP-to-M5.md`'s
# "Start here" table. A floor rather than an equality, because a later milestone amending one
# of these rows must not make this check fail for a reason that is not M4.12's.
M412_ROWS = 18

# Every row carrying a `waived` reason when M4.12 opened. The criterion says zero waivers
# ADDED, which is a claim about a difference and not about a count: `library-rate-model-line-
# no-bundle` carries a waiver AND names tests, so it is invisible to the ledger's "(1 waived)"
# line while still being a waiver a milestone could hide behind. Naming the set rather than
# counting it means a waiver swapped for another one is still reported.
WAIVED_AT_M411 = frozenset(
    {"data-rules-platform-rating-display-only", "library-rate-model-line-no-bundle"}
)

results: list[tuple[bool, str]] = []


def console(text: str) -> str:
    """`text` rendered in the encoding stdout actually has, escaping what it cannot carry.

    The same guard the three scripts before this one carry, and for their reason: the measured
    values below interpolate the app's own refusal strings, and §6.8's register writes them with
    em dashes. On a cp850 console, printing one raises UnicodeEncodeError from inside `print` --
    a traceback where the diagnosis belongs, in the one script whose whole job is diagnosis.
    """
    encoding = sys.stdout.encoding or "ascii"
    return text.encode(encoding, "backslashreplace").decode(encoding)


def check(ok: bool, label: str, measured: str, detail: str = "") -> None:
    """One line per numbered check, carrying the numbers and not only the verdict.

    The verdict is the FIRST argument, as in the three scripts before this one:
    `test_static_contracts.py::test_no_milestone_exit_check_has_a_constant_predicate` reads
    `check`'s first positional argument and reports a truthy literal there as a verdict settled
    before the run, so a signature taking the number first would make all twelve calls look
    exactly like the defect that guard exists to catch.

    Exactly twelve of these are recorded, which is what makes a published "12/12" mean the
    criterion's twelve rather than however many assertions the harness happened to make. Flushed
    per line: an evening on a real pool takes minutes, and a block-buffered stdout on a redirected
    pipe holds everything until the process ends -- a harness whose whole purpose is to report
    cannot report from a buffer.
    """
    results.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {console(label)}", flush=True)
    print(f"         {console(measured)}", flush=True)
    if detail:
        for line in detail.splitlines():
            print(f"         {console(line)}", flush=True)


class LoopGaps:
    """The largest pause between two wakeups of a 10 ms timer, for as long as the block runs.

    The criterion's row 4 has two halves, and this is the one an HTTP timing cannot show: a
    response that came back quickly proves the handler did not block *that* request, while what
    M4.12 repaired is that one seat's pure-CPU pair search owned the only event loop this process
    has. A timer that should fire every 10 ms and fires once in 36 s is the whole defect, read off
    the loop itself rather than off any request.
    """

    def __init__(self, tick: float = 0.01) -> None:
        self._tick = tick
        self._task: asyncio.Task[None] | None = None
        self.worst_ms = 0.0

    async def _sample(self) -> None:
        last = time.perf_counter()
        while True:
            await asyncio.sleep(self._tick)
            now = time.perf_counter()
            self.worst_ms = max(self.worst_ms, (now - last - self._tick) * 1000.0)
            last = now

    async def __aenter__(self) -> LoopGaps:
        self._task = asyncio.create_task(self._sample())
        # One tick before the caller's work starts, then the reading is reset: otherwise the
        # first interval measured is this task's own scheduling rather than a handler's.
        await asyncio.sleep(self._tick)
        self.worst_ms = 0.0
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


class Evening:
    """Three real accounts, the imported corpus, and the app under ASGI.

    A class for the reason `ops/m411_exit_criterion.py`'s `Household` is one: twelve scenarios
    share one database and one app, and a check that inherits the previous one's room is a check
    whose failure names the wrong milestone. Each check opens a room of its own -- which is also
    what a household does, since `rooms.open_session` abandons the host's previous *open* room by
    itself.
    """

    def __init__(self, conn: asyncpg.Connection, make_client: Any) -> None:
        self.conn = conn
        self.host = make_client()
        self.member = make_client()
        self.spare = make_client()
        self.host_id = 0
        self.member_id = 0
        self.spare_id = 0
        self.version = ""
        self.pool_size = 0
        # Every payload the app hands the push sender, and the reason it is recorded rather than
        # sent: `rooms.invite` runs on a detached task and would otherwise open an httpx client
        # against a push endpoint that does not exist, from a script with no VAPID keypair.
        self.pushes: list[dict[str, Any]] = []

    # --- setting the household up ----------------------------------------------------------

    async def open(self, version: str) -> None:
        from spielplan.push import send as push_send

        async def record(_conn: Any, user_id: int, payload: dict[str, Any], **_kw: Any) -> list:
            self.pushes.append({"user_id": user_id, **payload})
            return []

        push_send.send_to_user = record  # type: ignore[assignment]
        self.version = version

        created = await self.host.post(
            "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
        )
        if created.status_code != 201:
            raise RuntimeError(f"the first-boot admin was refused: {created.text[:200]}")
        self.host_id = await self._user_id("patrick")
        self.member_id = await self._invite("jenny", self.member)
        self.spare_id = await self._invite("mika", self.spare)

    async def _invite(self, name: str, client: httpx.AsyncClient) -> int:
        """One member account, created the way §6.6 Users creates one (decision 164).

        Through the real routes and not by an INSERT, because `POST /api/setup/admin` is
        first-boot only and there is no other way to obtain a cookie for a second account -- and
        two real cookie jars are precisely what checks 4 and 6 are about.
        """
        invited = await self.host.post("/api/admin/users", json={"name": name, "role": "member"})
        if invited.status_code != 201:
            raise RuntimeError(f"{name} could not be invited: {invited.text[:200]}")
        otp = invited.json()["one_time_password"]
        signed_in = await client.post("/api/auth/login", json={"name": name, "password": otp})
        if not signed_in.is_success:
            raise RuntimeError(f"{name} could not sign in: {signed_in.text[:200]}")
        # §3.1's first-login change, which `deps.active_user` enforces on every route below.
        changed = await client.post(
            "/api/auth/password",
            json={"current_password": otp, "new_password": MEMBER_PASSWORD},
        )
        if not changed.is_success:
            raise RuntimeError(f"{name} could not set a password: {changed.text[:200]}")
        return await self._user_id(name)

    async def _user_id(self, name: str) -> int:
        return int(await self.conn.fetchval("SELECT id FROM app_user WHERE name = $1", name))

    async def fit_everyone(self, store: ArtifactStore) -> None:
        """§5.3's fold-in, over accounts that arrived after the bundle did.

        The app's own `foldin.run`, never a hand-written INSERT into `user_score`: §6.2 step 3's
        pool is exactly that table, and a harness that seeded it would be measuring its own idea
        of a score. Both members are fitted to ZERO labels here, which is the state check 3 is
        about -- "fitted to zero labels" and "never fitted" are different states (§6.0), and the
        first is what a household has on the evening it adds someone.
        """
        await foldin.run(
            self.conn, bb.load_for(store), bundle_version=self.version, only_stale=False
        )

    # --- reads, all of them out of the database or a response -------------------------------

    async def seats(self, session_id: int) -> list[dict[str, Any]]:
        rows = await self.conn.fetch(
            "SELECT id, user_id, role, seat, ended_by, answered_count FROM session_participant "
            " WHERE session_id = $1 ORDER BY seat",
            session_id,
        )
        return [dict(r) for r in rows]

    async def state(self, session_id: int) -> str:
        return str(await self.conn.fetchval("SELECT state FROM session WHERE id = $1", session_id))

    async def settle_dispatches(self) -> None:
        """Join the detached invite and fan-out tasks before a check reads what they wrote.

        `api/tonight.py` holds both in module-level sets (`_INVITES`, `_FRAMES`) precisely so they
        are joinable: the invitation and the lobby frame are off the request path now (findings 17
        and 42), so a check asserting on either without joining would be asserting on a race
        rather than on a rule. A dispatch that raises is the sender's business and is swallowed
        here for the same reason the app swallows it -- §6's preamble makes push best effort.
        """
        for pending in (tonight_api._INVITES, tonight_api._FRAMES):
            for task in list(pending):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(task), timeout=10)

    # --- the room, opened and played the way a phone does -----------------------------------

    async def room(
        self, *, guests: int = 0, joiners: tuple[int, ...] = (), start: bool = True
    ) -> int:
        """Open a room, seat the joiners, start it, and return its id."""
        opened = await self.host.post(
            "/api/tonight/sessions",
            json={
                "kind": "movie",
                "runtime_budget_min": BUDGET_MIN,
                "include_rewatches": INCLUDE_REWATCHES,
                "guests": guests,
            },
        )
        if opened.status_code != 201:
            raise RuntimeError(f"the room was refused: {opened.text[:300]}")
        session_id = int(opened.json()["session_id"])
        for who in joiners:
            joined = await self.client_for(who).post(
                "/api/tonight/sessions/join", json={"session_id": session_id}
            )
            if joined.status_code != 200:
                raise RuntimeError(f"the join was refused: {joined.text[:300]}")
        if start:
            began = await self.host.post(f"/api/tonight/sessions/{session_id}/start")
            if began.status_code != 200:
                raise RuntimeError(f"the start was refused: {began.text[:300]}")
        await self.settle_dispatches()
        return session_id

    def client_for(self, user_id: int | None) -> httpx.AsyncClient:
        """The cookie jar a seat is played on. A guest seat has no account, so it is the host's
        phone -- §6.2 step 2's hand-the-phone, and the only pairing `_seat_for` admits."""
        return {
            self.host_id: self.host,
            self.member_id: self.member,
            self.spare_id: self.spare,
        }.get(user_id, self.host) if user_id is not None else self.host

    async def card(self, client: httpx.AsyncClient, seat_id: int) -> dict[str, Any]:
        res = await client.get(f"/api/tonight/seats/{seat_id}/round")
        if res.status_code != 200:
            raise RuntimeError(f"seat {seat_id} could not read its round: {res.text[:300]}")
        card = res.json()
        card["_shown_at"] = time.perf_counter()
        return card

    async def tap(
        self, client: httpx.AsyncClient, seat_id: int, card: dict[str, Any], answer: str = "A"
    ) -> httpx.Response:
        """One answer, posted the way `tonight.svelte.js` posts it: the sealed card and a clock.

        `latency_ms` is measured here and nowhere else, because the client is the only thing that
        knows when the pair reached the screen -- which is precisely why every row ever written
        before this milestone held 0, and why check 11 reads the column back. Floored at 1 ms so
        that an ASGI round trip faster than the clock's resolution is reported as the fast tap it
        was rather than as the defect; a stored 0 then means the app dropped the measurement,
        which is the only thing check 11 is asking.
        """
        shown = card.get("_shown_at") or time.perf_counter()
        return await client.post(
            f"/api/tonight/seats/{seat_id}/answer",
            json={
                "card_token": card["card_token"],
                "answer": answer,
                "latency_ms": max(1, round((time.perf_counter() - shown) * 1000)),
            },
        )

    def carry(self, response: httpx.Response) -> dict[str, Any]:
        """The next card, off the answer's own payload -- the "next card preloaded" the §6
        preamble asks for, and what the phone actually renders."""
        card = response.json()
        card["_shown_at"] = time.perf_counter()
        return card

    async def play_out(self, client: httpx.AsyncClient, seat_id: int) -> int:
        """Answer until the round has nothing left to ask. Returns the number of answers given."""
        card = await self.card(client, seat_id)
        given = 0
        while card.get("pair") is not None:
            answered = await self.tap(client, seat_id, card, "A" if given % 2 == 0 else "B")
            if answered.status_code != 200:
                raise RuntimeError(
                    f"seat {seat_id} was refused at pair {given + 1}: "
                    f"{answered.status_code} {answered.text[:200]}"
                )
            given += 1
            card = self.carry(answered)
            if given > round_rules.CAP_PAIRS + 2:
                raise RuntimeError(f"seat {seat_id} passed the cap of {round_rules.CAP_PAIRS}")
        return given

    async def vote(self, client: httpx.AsyncClient, seat_id: int, approved: list[int]) -> None:
        cast = await client.post(
            f"/api/tonight/seats/{seat_id}/ballot", json={"approved": approved}
        )
        if cast.status_code != 200:
            raise RuntimeError(f"seat {seat_id} could not vote: {cast.text[:300]}")


# --- 1. the evening itself ---------------------------------------------------------------------


async def check_one(e: Evening) -> tuple[bool, str, str]:
    """Two members and one guest seat, on one phone, through to 54e's blind reveal."""
    session_id = await e.room(guests=1, joiners=(e.member_id,))
    seats = await e.seats(session_id)
    host_seat = next(s for s in seats if s["user_id"] == e.host_id)
    member_seat = next(s for s in seats if s["user_id"] == e.member_id)
    guest_seat = next(s for s in seats if s["role"] == "guest")

    # The guest plays on the HOST's cookie, which is the whole reason `_seat_for` admits a guest
    # seat in a session the caller hosts.
    answers = {
        "host": await e.play_out(e.host, host_seat["id"]),
        "guest": await e.play_out(e.host, guest_seat["id"]),
        "member": await e.play_out(e.member, member_seat["id"]),
    }
    await e.settle_dispatches()

    ballot = await e.host.get(f"/api/tonight/sessions/{session_id}/ballot")
    slate = [row["title_id"] for row in (ballot.json().get("slate") or [])]
    for client, seat in ((e.host, host_seat), (e.host, guest_seat), (e.member, member_seat)):
        await e.vote(client, seat["id"], slate[:2])
    await e.settle_dispatches()

    revealed = await e.host.get(f"/api/tonight/sessions/{session_id}/result")
    payload = revealed.json() if revealed.status_code == 200 else {}
    winner = payload.get("winner") or {}
    outcomes = int(await e.conn.fetchval(
        "SELECT count(*) FROM session_outcome WHERE session_id = $1", session_id
    ) or 0)
    final = await e.state(session_id)
    ok = (
        revealed.status_code == 200
        and bool(winner.get("title_id"))
        and payload.get("participants") == 3
        and float(payload.get("approval_share") or 0.0) > 0.0
        and outcomes == 1
        and final == "resolved"
    )
    return ok, (
        f"reveal {revealed.status_code}, winner={winner.get('name')!r}, "
        f"share={payload.get('approval_share')} over {payload.get('participants')} seats, "
        f"session_outcome rows={outcomes}, state={final!r}"
    ), f"answers per seat: {answers}; the ballot carried {len(slate)} titles"


# --- 2 and 3. the two budgets ------------------------------------------------------------------


async def check_two(e: Evening) -> tuple[bool, str, str]:
    """A guest seat's first pair: no profile, so every candidate in the pool straddles."""
    session_id = await e.room(guests=1, joiners=(e.member_id,))
    guest_seat = next(s for s in await e.seats(session_id) if s["role"] == "guest")

    began = time.perf_counter()
    card = await e.card(e.host, guest_seat["id"])
    elapsed = time.perf_counter() - began

    pair = card.get("pair")
    ok = pair is not None and elapsed < BATTLE_BUDGET_S
    return ok, (
        f"the guest seat's first pair arrived in {elapsed:.2f}s "
        f"(budget {BATTLE_BUDGET_S}s) over a pool of {e.pool_size} scored owned movies"
    ), f"pair={'served' if pair else 'none'}, selection={(pair or {}).get('selection')}"


async def check_three(e: Evening) -> tuple[bool, str, str]:
    """A zero-label member's first pair and first answer. Both are budgeted, not just the read."""
    session_id = await e.room(joiners=(e.member_id,))
    member_seat = next(s for s in await e.seats(session_id) if s["user_id"] == e.member_id)

    began = time.perf_counter()
    card = await e.card(e.member, member_seat["id"])
    read_s = time.perf_counter() - began
    if card.get("pair") is None:
        return False, "the member's round served no pair at all", ""

    began = time.perf_counter()
    answered = await e.tap(e.member, member_seat["id"], card)
    answer_s = time.perf_counter() - began

    labels = await e.conn.fetchval(
        "SELECT label_count FROM user_vector WHERE user_id = $1 AND kind = 'movie' "
        "   AND purpose = 'foldin'",
        e.member_id,
    )
    ok = answered.status_code == 200 and read_s < BATTLE_BUDGET_S and answer_s < BATTLE_BUDGET_S
    return ok, (
        f"first pair {read_s:.2f}s, first answer {answer_s:.2f}s "
        f"(budget {BATTLE_BUDGET_S}s each) for a member fitted to {labels} labels"
    ), f"the answer returned {answered.status_code}"


# --- 4. the household, while one phone answers -------------------------------------------------


async def check_four(e: Evening) -> tuple[bool, str, str]:
    """The other phone's read, and the loop, during one seat's answer.

    Two readings, because they fail differently: a slow response says the request queued, and a
    long loop gap says the handler owned the process. M4.12's repair is `asyncio.to_thread` around
    the pure replay, which moves both -- and only the second stays a real measurement if a later
    change makes the search fast enough to hide the blocking.
    """
    session_id = await e.room(joiners=(e.member_id,))
    host_seat = next(s for s in await e.seats(session_id) if s["user_id"] == e.host_id)
    card = await e.card(e.host, host_seat["id"])
    if card.get("pair") is None:
        return False, "the host's round served no pair, so nothing was in flight", ""

    async with LoopGaps() as gaps:
        answering = asyncio.create_task(e.tap(e.host, host_seat["id"], card))
        began = time.perf_counter()
        other = await e.member.get("/api/tonight/rooms")
        read_s = time.perf_counter() - began
        answered = await answering

    ok = (
        other.status_code == 200
        and answered.status_code == 200
        and read_s < BATTLE_BUDGET_S
        and gaps.worst_ms < LOOP_BUDGET_MS
    )
    return ok, (
        f"the second cookie's GET /api/tonight/rooms answered {other.status_code} in "
        f"{read_s:.2f}s while the answer ran; longest loop pause {gaps.worst_ms:.0f}ms "
        f"(budget {LOOP_BUDGET_MS:.0f}ms)"
    ), f"the answer itself returned {answered.status_code}"


# --- 5. the lifecycle, when the combine fails once ---------------------------------------------


async def check_five(e: Evening) -> tuple[bool, str, str]:
    """`play.finish` raises on the last answer. A plain read must still finish the evening."""
    session_id = await e.room(joiners=(e.member_id,))
    seats = await e.seats(session_id)
    host_seat = next(s for s in seats if s["user_id"] == e.host_id)
    member_seat = next(s for s in seats if s["user_id"] == e.member_id)

    await e.play_out(e.host, host_seat["id"])

    real_finish = play_rules.finish
    broken = {"calls": 0}

    async def raises_once(*args: Any, **kwargs: Any) -> Any:
        broken["calls"] += 1
        if broken["calls"] == 1:
            raise asyncpg.ConnectionDoesNotExistError("the pool dropped the connection")
        return await real_finish(*args, **kwargs)

    play_rules.finish = raises_once  # type: ignore[assignment]
    try:
        # The room-finishing answer 500s, which is the defect's own shape: `settle` does not
        # swallow, so the transition's failure reaches the phone rather than being logged and
        # lost -- `play_out` raises `RuntimeError` on it. What this check is about is the read
        # AFTER it, which is why the 500 is suppressed here rather than asserted on.
        with contextlib.suppress(RuntimeError):
            await e.play_out(e.member, member_seat["id"])
        stuck = await e.state(session_id)
        read = await e.host.get(f"/api/tonight/sessions/{session_id}")
        ballot = await e.host.get(f"/api/tonight/sessions/{session_id}/ballot")
    finally:
        play_rules.finish = real_finish  # type: ignore[assignment]

    slate = (ballot.json().get("slate") or []) if ballot.status_code == 200 else []
    healed = await e.state(session_id)
    ok = (
        broken["calls"] >= 1
        and read.status_code == 200
        and ballot.status_code == 200
        and healed == "ballot"
        and len(slate) >= 3
    )
    return ok, (
        f"the combine raised {broken['calls']} time(s); the room read {stuck!r} at the failure "
        f"and {healed!r} after a plain GET, with {len(slate)} titles on the slate"
    ), f"GET session {read.status_code}, GET ballot {ballot.status_code}"


# --- 6 and 7. the two gathers ------------------------------------------------------------------


async def check_six(e: Evening) -> tuple[bool, str, str]:
    """A member's join and the host's start, submitted together."""
    session_id = await e.room(start=False)
    joined, began = await asyncio.gather(
        e.member.post("/api/tonight/sessions/join", json={"session_id": session_id}),
        e.host.post(f"/api/tonight/sessions/{session_id}/start"),
        return_exceptions=True,
    )
    await e.settle_dispatches()

    join_status = getattr(joined, "status_code", None)
    start_status = getattr(began, "status_code", None)
    if start_status != 200:
        # No claim, so no frozen pool: `snapshot_of` would raise `no_room` and the run would
        # report a traceback where the sentence naming the failure belongs. The verdict is
        # already settled here -- the criterion's row says the start is the one that stands.
        return False, (
            f"join {join_status}, start {start_status}: the host's start did not take the room, "
            "so there is no frozen snapshot to read"
        ), getattr(began, "text", repr(began))[:200]
    snapshot = await play_rules.snapshot_of(e.conn, session_id)
    seated = {s["id"] for s in await e.seats(session_id) if s["user_id"] == e.member_id}
    scored = {p for seats in snapshot.scores.values() for p in seats}

    admitted = join_status == 200 and bool(seated) and seated <= scored
    refused = join_status == 409 and not seated
    ok = start_status == 200 and (admitted or refused)
    outcome = (
        "seated and inside the frozen snapshot" if admitted
        else "refused as started" if refused else "NEITHER"
    )
    return ok, (
        f"join {join_status}, start {start_status}; the member holds {len(seated)} seat(s) and "
        f"the snapshot scores {len(scored)} seat(s) over {len(snapshot.candidates)} titles"
    ), f"outcome: {outcome}"


async def check_seven(e: Evening) -> tuple[bool, str, str]:
    """An undo and an answer on one seat, submitted together, and then one more answer."""
    session_id = await e.room(joiners=(e.member_id,))
    host_seat = next(s for s in await e.seats(session_id) if s["user_id"] == e.host_id)

    card = await e.card(e.host, host_seat["id"])
    if card.get("pair") is None:
        return False, "the round served no pair, so there was nothing to gather", ""
    first = await e.tap(e.host, host_seat["id"], card)
    if first.status_code != 200:
        return False, f"the first answer was refused: {first.status_code}", first.text[:200]

    second = e.carry(first)
    if second.get("card_token") is None:
        return False, (
            "the round ended on its first answer, so there was no second card to race an undo "
            "against"
        ), f"stop_reason={second.get('stop_reason')!r}"
    gathered = await asyncio.gather(
        e.host.post(f"/api/tonight/seats/{host_seat['id']}/undo"),
        e.tap(e.host, host_seat["id"], second, "B"),
        return_exceptions=True,
    )
    statuses = [getattr(r, "status_code", repr(r)) for r in gathered]

    after = await e.card(e.host, host_seat["id"])
    counters = await e.conn.fetchrow(
        "SELECT p.answered_count, coalesce(max(a.seq), 0) AS top "
        "  FROM session_participant p LEFT JOIN session_answer a ON a.participant_id = p.id "
        " WHERE p.id = $1 GROUP BY p.answered_count",
        host_seat["id"],
    )
    again = None
    if after.get("pair") is not None:
        again = await e.tap(e.host, host_seat["id"], after, "A")

    ok = (
        all(isinstance(s, int) and s < 500 for s in statuses)
        and after.get("ended_by") is None
        and counters is not None
        and int(counters["top"]) >= int(counters["answered_count"])
        and (again is None or again.status_code < 500)
    )
    return ok, (
        f"undo/answer gathered as {statuses}; the seat reads ended_by={after.get('ended_by')!r} "
        f"with max(seq)={counters and counters['top']} against "
        f"answered_count={counters and counters['answered_count']}"
    ), (
        "the next answer returned "
        f"{again.status_code if again is not None else 'no pair left to answer'}"
    )


# --- 8. the pools too small for a round --------------------------------------------------------


async def check_eight(e: Evening) -> tuple[bool, str, str]:
    """Two candidates, then three. Both must reach the ballot rather than waiting for ever.

    The pool is narrowed the way §6.2 step 3 narrows it -- `pool.build` keeps only titles EVERY
    seated member has scored, so a member whose scores cover two titles makes the pool two titles.
    Nothing here writes a session, a seat or a snapshot: the app builds all three, and the check
    would be worthless if it did not.

    AND THE SEAT READS ARE THE LAST THING IT DOES, which is the whole of what it measures. This
    check used to GET the session once more after the loop below, and that GET is a settling read
    (`play.settle` runs on every read of the session, the ballot and the result): with it there,
    the room reached 'ballot' whether or not the round read had settled anything, so the clause
    the detail line names -- the silent wait -- was the one thing the check could not see. No
    client produces that order either. `tonight.svelte.js`'s `refresh()` GETs the session and
    only THEN calls `loadRound`, so the session read belongs before each seat's round read and
    is written that way below; on a pool this size it settles nothing when it runs, because
    `play.state_for`'s belt-and-braces is the only thing that can end such a seat at all.
    [M4.12 review cycle 2: M412-GATE-8; decision 215]
    """
    kept = [
        int(r["title_id"])
        for r in await e.conn.fetch(
            "SELECT us.title_id FROM user_score us JOIN title t ON t.id = us.title_id "
            " WHERE us.user_id = $1 AND us.kind = 'movie' AND us.bundle_version = $2 "
            "   AND t.is_owned AND t.runtime_min IS NOT NULL AND t.runtime_min <= $3 "
            " ORDER BY us.title_id LIMIT 3",
            e.spare_id, e.version, BUDGET_MIN,
        )
    ]
    if len(kept) < 3:
        return False, f"the corpus offered only {len(kept)} candidates to narrow to", ""

    outcomes: dict[int, str] = {}
    sizes: dict[int, int] = {}
    # DESCENDING, and that is not a style choice: the narrowing is a DELETE, so each pass can only
    # take more away. Run 2 before 3 and the three-candidate pool is measured on the two rows the
    # first pass left -- a check that reports two numbers and measures one.
    for size in (3, 2):
        await e.conn.execute(
            "DELETE FROM user_score WHERE user_id = $1 AND NOT (title_id = ANY($2::integer[]))",
            e.spare_id, kept[:size],
        )
        session_id = await e.room(joiners=(e.spare_id,))
        for seat in await e.seats(session_id):
            client = e.client_for(seat["user_id"])
            # `refresh()` in order: the session, and then the round. Never the other way round
            # and never again afterwards, so nothing but the round read can be what moves this
            # room -- which is the only reason the verdict below means anything (M412-GATE-8).
            await client.get(f"/api/tonight/sessions/{session_id}")
            # The read is what ends a seat whose round has nothing to ask it (finding 6), and it
            # is exactly what the phone does when the screen opens.
            await e.card(client, seat["id"])
        snapshot = await play_rules.snapshot_of(e.conn, session_id)
        sizes[size] = len(snapshot.candidates)
        outcomes[size] = await e.state(session_id)

    ok = outcomes[2] == "ballot" and outcomes[3] == "ballot"
    return ok, (
        f"a pool of {sizes[2]} reached {outcomes[2]!r}; a pool of {sizes[3]} reached "
        f"{outcomes[3]!r}"
    ), "both must reach 'ballot'; 'voting' is the silent wait this milestone closed"


# --- 9, 10 and 12. the three source reads ------------------------------------------------------


def _tv_offenders() -> list[str]:
    """Every place a TV client is still funded -- decision 165's four categories, each its own rule.

    "No route, no spec, no coverage row, no spec sentence" is four different questions, and the
    one-line grep the criterion writes cannot separate them: it reports §7.1's `imdb/tmdb/tvdb`
    identity line, the coverage map's note explaining why the row was deleted, and §12's own
    paragraph saying the M4 row loses its TV clause. Each of those is evidence that the retirement
    happened. So the route literal is searched for as a route, and prose only where a sentence can
    be nothing but funding.
    """
    offenders: list[str] = []
    roots = (
        ROOT / "frontend" / "src",
        ROOT / "e2e" / "specs",
        ROOT / "backend" / "tests",
        ROOT / "docs",
    )
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in TV_GREP_SUFFIXES:
                continue
            relative = path.relative_to(ROOT).as_posix()
            if any(skip in relative for skip in TV_GREP_SKIP):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if TV_ROUTE.search(line):
                    offenders.append(f"{relative}:{number}: a /tv route: {line.strip()[:80]}")

    spec = (ROOT / NORMATIVE_SPEC).read_text(encoding="utf-8")
    for number, line in enumerate(spec.splitlines(), start=1):
        if TV_KIOSK.search(line):
            offenders.append(f"{NORMATIVE_SPEC}:{number}: the kiosk, named: {line.strip()[:70]}")
        if M4_ROW.match(line) and "TV route" in line:
            offenders.append(f"{NORMATIVE_SPEC}:{number}: §12's M4 row still funds it")

    covered = [
        f"{COVERAGE_MAP}:{number}: the deleted row is back"
        for number, line in enumerate(
            (ROOT / COVERAGE_MAP).read_text(encoding="utf-8").splitlines(), start=1
        )
        if line.strip() == TV_COVERAGE_ROW
    ]
    return offenders + covered


async def check_nine(_e: Evening) -> tuple[bool, str, str]:
    """Decision 165: no route, no spec, no coverage row, no sentence funding a TV client."""
    route = ROOT / "frontend" / "src" / "routes" / "tv"
    spec = ROOT / "e2e" / "specs" / "16-tonight-tv.spec.js"
    offenders = _tv_offenders()
    ok = not route.exists() and not spec.exists() and not offenders
    return ok, (
        f"frontend/src/routes/tv exists={route.exists()}, 16-tonight-tv.spec.js "
        f"exists={spec.exists()}, surviving mentions={len(offenders)}"
    ), "\n".join(offenders[:10])


async def check_ten(_e: Evening) -> tuple[bool, str, str]:
    """The lifecycle has one owner: no route calls the combine directly."""
    source = (ROOT / "backend" / "spielplan" / "api" / "tonight.py").read_text(encoding="utf-8")
    hits = [
        f"line {number}: {line.strip()[:80]}"
        for number, line in enumerate(source.splitlines(), start=1)
        if "play.finish" in line
    ]
    settles = source.count("play.settle(")
    ok = not hits and settles >= 3
    return ok, (
        f"{len(hits)} direct play.finish call(s) in api/tonight.py, and {settles} play.settle "
        "call(s) -- the three reads that move a stuck room on"
    ), "\n".join(hits)


async def check_eleven(e: Evening) -> tuple[bool, str, str]:
    """§14 risk 6's instrument: the stored latency is a measurement, not a constant."""
    rows = await e.conn.fetch("SELECT DISTINCT latency_ms FROM session_answer ORDER BY latency_ms")
    values = [r["latency_ms"] for r in rows]
    non_zero = [v for v in values if v]
    ok = bool(values) and set(values) != {0} and bool(non_zero)
    return ok, (
        f"{len(values)} distinct latency_ms value(s) over the evening just played; "
        f"{len(non_zero)} of them non-zero, range "
        f"{min(non_zero) if non_zero else None}-{max(non_zero) if non_zero else None} ms"
    ), f"distinct values (first twelve): {values[:12]}"


async def check_twelve(_e: Evening) -> tuple[bool, str, str]:
    """The coverage contract, read off the map rather than off a suite run.

    `pytest backend/tests -q` and `npm --prefix e2e run fresh` are the other half of this row and
    are deliberately not shelled out to here; `docs/TESTING.md` carries both commands. What a
    green suite cannot show afterwards is the shape of the close: that the milestone was raised
    rather than left low, that every M4.12 row names a test, and that nothing was silenced.
    """
    data = tomllib.loads(
        (ROOT / "backend" / "tests" / "spec_coverage.toml").read_text(encoding="utf-8")
    )
    rows = data.get("requirement", [])
    ours = [r for r in rows if r.get("milestone") == "M4.12"]
    uncovered = [r["id"] for r in ours if not r.get("tests")]
    waived = {r["id"] for r in rows if r.get("waived")}
    ok = (
        data.get("current_milestone") == "M4.12"
        and len(ours) >= M412_ROWS
        and not uncovered
        and waived == WAIVED_AT_M411
    )
    return ok, (
        f"current_milestone={data.get('current_milestone')!r}, {len(ours)} M4.12 rows of which "
        f"{len(uncovered)} name no test, {len(waived)} waived row(s) in the whole map "
        f"against the {len(WAIVED_AT_M411)} this milestone inherited"
    ), (
        f"uncovered: {uncovered}\n"
        f"waivers added: {sorted(waived - WAIVED_AT_M411)}\n"
        "the suite itself: backend/.venv/Scripts/python -m pytest backend/tests -q\n"
        "the browser:      npm --prefix e2e run fresh"
    )


CHECKS = [
    (1, "two members and a guest seat on one phone reach the blind reveal", check_one),
    (2, "a guest seat's first pair is served inside the battle budget", check_two),
    (3, "a zero-label member's first pair and first answer are both inside it", check_three),
    (4, "an unrelated read is served while an answer is in flight", check_four),
    (5, "a combine that failed once is finished by the next read", check_five),
    (6, "a gathered join and start leave exactly one outcome standing", check_six),
    (7, "a gathered undo and answer leave the seat playing", check_seven),
    (8, "pools of two and three candidates reach the ballot", check_eight),
    (9, "nothing in the repository funds a TV client", check_nine),
    (10, "no route calls the combine directly", check_ten),
    (11, "the answer latency is a measurement", check_eleven),
    (12, "the coverage contract closed at M4.12 with no waiver added", check_twelve),
]


def discard_staged_artifacts(data_dir: Path | None) -> None:
    """Remove the staged bundle, and say so when it survives.

    `ops/m49_exit_criterion.py`'s note, for its reason: on Windows a handle held open by a loaded
    npz makes `rmtree` a silent no-op, and a harness reporting success while leaving a gigabyte
    behind is how M4.5's close-out found its own temp directories. Nothing here holds an
    `ArtifactStore` open past `main`, so this is expected to work -- and it says so when it does
    not, rather than being believed.
    """
    if data_dir is None:
        return
    shutil.rmtree(data_dir, ignore_errors=True)
    if data_dir.exists():
        print(f"  NOTE      staged artifacts survived cleanup at {data_dir}", flush=True)


def _dsn_from_env_test() -> str | None:
    """`.env.test`'s TEST_DATABASE_URL, read the way `backend/tests/conftest.py` reads it.

    The same convenience and the same limit: an untracked file names the server, so this is the
    only place this script decides which host it is allowed to create a database on.
    """
    path = ROOT / ".env.test"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "TEST_DATABASE_URL":
            return value.strip()
    return None


def _neutralise_connector_env() -> None:
    """Take the operator's own connector credentials away before the app boots.

    `backend/tests/conftest.py` records the incident this repeats: the genuine lifespan calls
    `registry.seed_from_env`, so a household that followed its own README boots this scratch
    install with their real Jellyfin configured -- and their encrypted credentials are then
    written into a throwaway database under a throwaway key. Derived from `Settings` rather than
    listed, so a seventh connector variable cannot be forgotten here.
    """
    from spielplan.core.config import Settings

    for name in Settings.model_fields:
        if name.startswith(("jellyfin_", "tmdb_", "omdb_", "trakt_")):
            os.environ.pop(name.upper(), None)


async def main() -> int:
    bundle_dir = os.environ.get("CORPUS_BUNDLE_DIR")
    if not bundle_dir:
        print("CORPUS_BUNDLE_DIR is unset. This script measures a REAL bundle on purpose --")
        print("checks 2, 3 and 4 are the three no fixture in this repository can falsify.")
        return 2
    root = Path(bundle_dir)
    dsn = os.environ.get("TEST_DATABASE_URL") or _dsn_from_env_test()
    if not dsn:
        print("TEST_DATABASE_URL is unset and .env.test does not supply it.")
        return 2

    content = root / "content.sqlite"
    if not content.is_file():
        print(f"no content.sqlite under {root} -- that is not an export bundle.")
        return 2
    with sqlite3.connect(f"file:{content}?mode=ro", uri=True) as probe:
        shipped_titles = probe.execute("SELECT count(*) FROM title").fetchone()[0]
    if shipped_titles < MIN_REAL_TITLES:
        print(f"{root} ships {shipped_titles:,} titles -- that is the fixture, not the corpus.")
        print("Refusing on purpose: an evening on a fixture pool resolves whatever a round costs.")
        return 2

    print(f"\nM4.12 exit criterion -- bundle {root.name} ({shipped_titles:,} titles)\n", flush=True)

    # A dedicated DATABASE, not a schema: 0003 creates `display` and `review_store`, which are
    # database-global, so a search_path could not isolate this run from a household's data. Named
    # with this run's pid, so two concurrent runs cannot drop each other's database.
    admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
    scratch = f"spielplan_m412_exit_p{os.getpid()}"
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {scratch}")
    finally:
        await admin.close()

    scratch_dsn = dsn.rsplit("/", 1)[0] + f"/{scratch}"
    conn: asyncpg.Connection | None = None
    data_dir: Path | None = None
    lifespan: Any = None
    clients: list[httpx.AsyncClient] = []
    try:
        conn = await asyncpg.connect(scratch_dsn)
        # The app's own connection setup, not a bare connect: `db/pool.py` registers the
        # json/jsonb codec every caller depends on, and `session.context` -- the frozen pool this
        # whole milestone is about -- is jsonb. M4.5's harness died three times here, with a
        # defect that read exactly like a defect in the importer.
        await db_pool._init_connection(conn)
        data_dir = Path(tempfile.mkdtemp(prefix="spielplan-m412-exit-"))
        artifacts_root = data_dir / "artifacts"
        artifacts_root.mkdir(parents=True, exist_ok=True)
        await migrate.apply_all(conn)

        print("0. The bundle, then the household", flush=True)
        began = time.perf_counter()
        report = await bundle_import.import_bundle(
            conn, bundle_import.Bundle.open(root), artifacts_root
        )
        if not report.ok:
            failures = "\n".join(
                f"FAIL {f.rule}: {f.message[:150]}"
                for f in report.findings if f.severity == "fail"
            )
            print(f"  the bundle did not import:\n{console(failures[:800])}", flush=True)
            return 2
        version = str(
            await conn.fetchval("SELECT version FROM artifact_bundle WHERE state = 'active'")
        )
        print(f"  imported {version} in {time.perf_counter() - began:.0f}s", flush=True)

        # The app, pointed at the scratch database and the staged artifacts, booted through its
        # genuine lifespan so §2's env seeding and §3.1's gates are the ones that ship.
        os.environ["DATABASE_URL"] = scratch_dsn
        os.environ["DATA_DIR"] = str(data_dir)
        _neutralise_connector_env()
        neutral = data_dir / "no-dot-env"
        neutral.mkdir(exist_ok=True)
        os.chdir(neutral)
        core_config.settings.cache_clear()

        from spielplan.app import create_app

        application = create_app()

        def make_client() -> httpx.AsyncClient:
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test"
            )
            clients.append(client)
            return client

        lifespan = application.router.lifespan_context(application)
        await lifespan.__aenter__()

        evening = Evening(conn, make_client)
        await evening.open(version)
        store = await ArtifactStore.load_active(conn, artifacts_root)
        await evening.fit_everyone(store)

        # The pool the app itself scores, read before any check runs: a real bundle imported into
        # a household that owns nothing is the fixture again under the corpus's name, and checks
        # 2, 3 and 4 would then report a budget nobody was ever near.
        evening.pool_size = int(await conn.fetchval(
            "SELECT count(*) FROM title t WHERE t.is_owned AND t.kind = 'movie' "
            "   AND EXISTS (SELECT 1 FROM user_score us WHERE us.title_id = t.id "
            "                AND us.user_id = $1 AND us.bundle_version = $2)",
            evening.host_id, version,
        ) or 0)
        print(f"  the household owns {evening.pool_size:,} scored movies", flush=True)
        if evening.pool_size < MIN_REAL_POOL:
            print(f"  a pool of {evening.pool_size} is below the floor of {MIN_REAL_POOL}.")
            print("  Refusing on purpose: the three timing checks cannot be measured on it.")
            return 2

        print("", flush=True)
        for number, title, runner in CHECKS:
            label = f"{number:2d}. {title}"
            try:
                ok, measured, detail = await runner(evening)
            except Exception as exc:
                # One check's crash fails that check and no other. The score's denominator is the
                # criterion's twelve, not however far the harness got: a run that stops at six and
                # prints "6/6 checks passed" is the failure mode an exit criterion exists to rule
                # out, and it is the shape `ops/m45_exit_criterion.py` still has.
                check(
                    False, label, f"the check stopped on {type(exc).__name__}: {exc}",
                    traceback.format_exc(),
                )
                continue
            check(ok, label, measured, detail)
    except Exception as exc:
        # Everything outside a check: the connect, the migration, the import, the household seed.
        # Reported as the failures they are, so the exit code stays non-zero and the run still
        # ends in a score rather than in a traceback where the sentence naming the cause belongs.
        stopped = f"the run stopped on {type(exc).__name__}: {exc}"
        trace = traceback.format_exc()
        for index, (number, title, _runner) in enumerate(CHECKS[len(results):]):
            check(False, f"{number:2d}. {title}", stopped, trace if index == 0 else "")
    finally:
        for client in clients:
            with contextlib.suppress(Exception):
                await client.aclose()
        if lifespan is not None:
            with contextlib.suppress(Exception):
                await lifespan.__aexit__(None, None, None)
        if conn is not None:
            await conn.close()
        os.chdir(ROOT)
        discard_staged_artifacts(data_dir)
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


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
