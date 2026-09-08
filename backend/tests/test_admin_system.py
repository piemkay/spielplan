"""§6.6's System card, at the three facts decision 182 gives it.

Spec v2.1 §6.6 (System), §2 (Backups, Configuration), §3.1, §14.3; decisions 181, 182;
docs/milestones/M4.7-plan.md §2 findings 1 and 10.

M4.7 gives `job_run` its rows and secrets custody a readable state, and before this route
nothing read either: `last_run` was an in-process dict, every report was logged once and
dropped, and the only signal that a month of nightly dumps had failed was one ERROR a day in a
container log with no timestamp. `ops-11`'s finding is not that the dump can fail — it is that
the household cannot find out.

Two properties are worth more than the rest and are asserted hardest here:

  * **the fingerprint is a fingerprint.** §14.3 calls a Jellyfin API key "unscoped and
    admin-equivalent", and SECRETS_KEY is what opens the stored one. A card that answered "which
    key is this" with the key would be a worse disclosure than the one it exists to report.
  * **an unreadable key is reported, not raised.** The whole of dd03 is a custody problem that
    500s the routes that would describe it. This route is the description, so of all the routes
    in the app it is the one that must not.

The e2e half is `e2e/specs/18-system.spec.js`: this file proves the payload against a key it
chose, that one proves an operator can find it. Skipped without TEST_DATABASE_URL; see
tests/conftest.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spielplan import worker
from spielplan.api import admin as admin_api
from spielplan.core import secrets as sec
from spielplan.core.config import settings

ADMIN_PASSWORD = "an-admin-password"
MEMBER_PASSWORD = "a-member-password"
BACKUP = admin_api.BACKUP_JOB


async def _admin(app):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD}
    )
    assert created.status_code == 201, created.text
    return client


async def _member(app, admin):
    """A created account past §3.1's forced first-login change, so a 403 means the role."""
    made = await admin.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    assert made.status_code == 201, made.text
    otp = made.json()["one_time_password"]
    client = app()
    assert (
        await client.post("/api/auth/login", json={"name": "jenny", "password": otp})
    ).status_code == 200
    changed = await client.post(
        "/api/auth/password",
        json={"current_password": otp, "new_password": MEMBER_PASSWORD},
    )
    assert changed.status_code == 200, changed.text
    return client


async def _run(db, name: str, *, ok: bool | None, ago: timedelta, detail=None) -> None:
    """One `job_run` row, written the way `worker._record_start`/`_record_finish` leave it."""
    started = datetime.now(UTC) - ago
    await db.execute(
        "INSERT INTO job_run (name, started_at, finished_at, ok, detail) "
        "VALUES ($1, $2, $3, $4, $5)",
        name,
        started,
        None if ok is None else started + timedelta(seconds=2),
        ok,
        detail,
    )


async def _card(admin) -> dict:
    got = await admin.get("/api/admin/system")
    assert got.status_code == 200, got.text
    return got.json()


# --- the fingerprint, with no database in the way -------------------------------------


def test_the_fingerprint_is_short_stable_and_not_the_key():
    """§14.3: this route may prove *which* key is loaded and never what it is.

    Twelve lowercase hex characters, the same twelve every time — an operator comparing this
    install against the `.env` beside their dumps is doing a string comparison by eye, and a
    value that changed between two reads would answer no question at all.
    """
    key = "an-operators-secrets-key-not-a-real-one"
    fingerprint = sec.key_fingerprint(key)

    assert len(fingerprint) == 12
    assert all(c in "0123456789abcdef" for c in fingerprint)
    assert sec.key_fingerprint(key) == fingerprint
    assert key not in fingerprint
    # Nor any window of the key: a truncated digest that happened to leak four characters of a
    # short key would still be a leak, and this is the cheap way to say so.
    assert not any(key[i : i + 4] in fingerprint for i in range(len(key) - 3))


def test_two_keys_have_two_fingerprints():
    """The one thing the card claims: that this value distinguishes one `.env` from another."""
    assert sec.key_fingerprint("the-key-that-was-current-for-the-dump") != sec.key_fingerprint(
        "the-key-the-operator-regenerated-after"
    )


# --- the three facts, over HTTP -------------------------------------------------------


async def test_the_card_reports_exactly_three_facts_and_no_more(secrets_key, db, app):
    """Decision 182: last successful backup, custody, newest `job_run` row per job.

    Asserted as an equality on the key set rather than as three presence checks, because the
    decision is as much about what stays at M5 — §6.6's queue depth, last syncs and logs — and
    a fourth key would arrive here before it arrived on any screen.
    """
    admin = await _admin(app)
    card = await _card(admin)

    assert sorted(card) == ["backup", "jobs", "secrets"]
    assert sorted(card["secrets"]) == ["configured", "fingerprint", "key_id", "unreadable"]
    assert sorted(card["backup"]) == ["at", "bytes", "stale", "stale_after_hours"]


async def test_the_card_never_hands_the_secrets_key_to_the_browser(secrets_key, db, app):
    """The assertion is over the whole serialised response, not over the field we meant.

    A field added later — a config echo, a debug dump of `Settings` — would be caught by this
    and by nothing else, and §14.3 is why that is worth a test rather than a code review.
    """
    admin = await _admin(app)
    got = await admin.get("/api/admin/system")

    assert got.status_code == 200
    assert secrets_key not in got.text
    assert got.json()["secrets"]["fingerprint"] == sec.key_fingerprint(secrets_key)
    assert got.json()["secrets"]["configured"] is True


async def test_the_card_reports_the_newest_run_of_each_job(secrets_key, db, app):
    """§6.6's "job health": one line per job, and it is the latest line.

    The older rows stay in the table — they are the history a later milestone's chart reads —
    so the card's contract is a projection, not a delete.
    """
    admin = await _admin(app)
    await _run(db, "fold-in-tick", ok=True, ago=timedelta(hours=3), detail={"users": 1})
    await _run(db, "fold-in-tick", ok=False, ago=timedelta(minutes=2), detail={"error": "boom"})
    await _run(db, "session-prune", ok=True, ago=timedelta(minutes=30), detail=None)

    jobs = {job["name"]: job for job in (await _card(admin))["jobs"]}

    assert sorted(jobs) == ["fold-in-tick", "session-prune"]
    assert jobs["fold-in-tick"]["ok"] is False
    assert jobs["fold-in-tick"]["detail"] == {"error": "boom"}
    # A prune reports nothing beyond having run, and a null `detail` is still job health.
    assert jobs["session-prune"]["ok"] is True
    assert jobs["session-prune"]["detail"] is None


async def test_a_job_that_started_and_never_finished_is_reported_as_unfinished(
    secrets_key, db, app
):
    """`worker._record_start` opens the row before the call, so this row is a real state.

    A SIGKILL past the grace period, an OOM or a power cut leaves exactly this, and it is a
    more useful fact than no row at all — but only if the payload carries it rather than
    collapsing `ok IS NULL` into `ok = false`, which would report a crash as a job failure and
    send the operator looking for an exception that was never raised.
    """
    admin = await _admin(app)
    await _run(db, "nightly-backup", ok=None, ago=timedelta(minutes=5))

    job = (await _card(admin))["jobs"][0]

    assert job["name"] == "nightly-backup"
    assert job["finished_at"] is None
    assert job["ok"] is None


# --- the backup fact ------------------------------------------------------------------


async def test_the_backup_fact_is_the_newest_successful_dump_not_the_newest_attempt(
    secrets_key, db, app
):
    """The two keys are two different rows on precisely the install that needs this page.

    `jobs` says the last attempt failed; `backup` says when a dump last actually succeeded.
    Collapsing them would leave the household reading "nightly-backup: failed 5 minutes ago"
    with no way to learn that the last good dump is from Tuesday — or that there is none.
    """
    admin = await _admin(app)
    await _run(db, BACKUP, ok=True, ago=timedelta(hours=10), detail={"bytes": 4096, "kept": 14})
    await _run(db, BACKUP, ok=False, ago=timedelta(minutes=5), detail={"error": "no pg_dump"})

    card = await _card(admin)

    assert card["jobs"][0]["ok"] is False
    assert card["backup"]["bytes"] == 4096
    assert card["backup"]["stale"] is False
    assert (datetime.now(UTC) - datetime.fromisoformat(card["backup"]["at"])) < timedelta(
        hours=11
    )


@pytest.mark.parametrize(
    ("hours", "stale"),
    [(9, False), (35, False), (37, True)],
)
async def test_a_dump_is_stale_once_it_is_older_than_a_night_and_a_half(
    secrets_key, db, app, hours, stale
):
    """36 hours: it cannot fire on a household whose dump ran at last night's anchor hour, and
    it does fire before a second night has been missed. The boundary is asserted from both
    sides, because a threshold tested only where it is obviously true is a constant."""
    admin = await _admin(app)
    await _run(db, BACKUP, ok=True, ago=timedelta(hours=hours), detail={"bytes": 1})

    card = await _card(admin)

    assert card["backup"]["stale"] is stale
    assert card["backup"]["stale_after_hours"] == 36


async def test_an_install_that_has_never_completed_a_dump_is_stale(secrets_key, db, app):
    """"No backup yet" and "no backup since Tuesday" are one problem to whoever needs one."""
    admin = await _admin(app)
    await _run(db, BACKUP, ok=False, ago=timedelta(minutes=1), detail={"error": "no pg_dump"})

    card = await _card(admin)

    assert card["backup"]["at"] is None
    assert card["backup"]["bytes"] is None
    assert card["backup"]["stale"] is True


async def test_a_fortnight_of_failures_does_not_turn_the_last_good_dump_into_never(
    secrets_key, db, app
):
    """The card and the retention, on the one install that needs both.

    `job-run-prune` keeps a fortnight, which is §2's own rotation; the household whose dumps
    started failing is the household whose last successful row ages out of it. Deleted by age
    alone, that row's disappearance flips this card from "last successful backup: the 24th, 15
    days ago" plus the stale warning — the true and actionable answer — to the categorical
    "no dump has ever completed on this install", which the frontend renders as a sentence
    (`admin/system/+page.svelte`). The operator is then told backups were never set up, on a box
    whose `/data/backups` still holds fourteen restorable dumps, because `nightly.prune` rotates
    only after a dump that *worked*. Told the wrong problem, they go looking for a
    misconfiguration rather than for the disk that filled on the 24th.
    [M4.7 cycle 2 finding 7]
    """
    admin = await _admin(app)
    await _run(db, BACKUP, ok=True, ago=timedelta(days=15), detail={"bytes": 41235968})
    for night in range(13, 0, -1):
        await _run(db, BACKUP, ok=False, ago=timedelta(days=night), detail={"error": "no space"})

    await worker._prune_job_runs()
    card = await _card(admin)

    assert card["backup"]["at"] is not None, (
        "the prune deleted the last successful dump, so the card says none has ever completed"
    )
    assert card["backup"]["bytes"] == 41235968
    assert card["backup"]["stale"] is True, "fifteen days is stale by any reading"
    assert card["jobs"][0]["ok"] is False, "the newest attempt is still the failure it was"


async def test_a_household_whose_worker_has_not_run_yet_is_named_no_jobs(secrets_key, db, app):
    """The empty install, which is the one an operator meets first.

    The card asks for the newest row of each *registered* job rather than for the distinct names
    in the table, which is what keeps the read proportional to the twelve jobs instead of to
    every row the loop has ever written. That makes the join's kind load-bearing: an outer one
    would render twelve rows of nulls on a fresh boot and call it job health. And a row left by a
    job this build no longer has is history for a later milestone's chart, not health for now.
    """
    admin = await _admin(app)
    await _run(db, "a-job-this-build-does-not-have", ok=True, ago=timedelta(minutes=1))

    card = await _card(admin)

    assert card["jobs"] == []
    assert card["backup"]["at"] is None and card["backup"]["stale"] is True


async def test_the_data_tab_payload_does_not_carry_this_cards_facts(secrets_key, db, app):
    """Decision 182 chose this card; the Data tab's payload kept carrying `jobs` and `backup`.

    That spread was written while owner decision 2 was still open and its option (A) — "Data tab
    + Connectors card only" — was live. Decision 182 took option (B), and nothing on
    `frontend/src/routes/admin/data/+page.svelte` ever read either key: it renders `bundles`,
    `active`, `loaded`, `restart_required` and `rebuild_set`. A second copy of the 36-hour rule,
    on a route polled on a timer, is how two screens start telling one household different things
    about one dump — and the copy nobody reads is the one that drifts. [M4.7 ops-11; decision 182]
    """
    admin = await _admin(app)
    await _run(db, BACKUP, ok=True, ago=timedelta(hours=40), detail={"bytes": 77})

    state = await admin.get("/api/admin/bundle/state")

    assert state.status_code == 200, state.text
    assert "jobs" not in state.json() and "backup" not in state.json()
    assert (await _card(admin))["backup"]["bytes"] == 77, "this card is where it is reported"


# --- custody --------------------------------------------------------------------------


async def test_a_wrong_secrets_key_is_reported_rather_than_raised(
    secrets_key, db, app, monkeypatch
):
    """dd03, at the one route whose job is to describe it.

    The scenario is a restore whose `.env` did not come with it: the DEK row is the one the
    first boot minted, and the key in the environment is not the key that wrapped it. Every
    other admin route degrades; this one has to *name* the state, because it is the screen the
    operator is on when they find out.
    """
    admin = await _admin(app)
    before = await _card(admin)
    assert before["secrets"]["unreadable"] is False
    key_id = before["secrets"]["key_id"]
    assert key_id is not None

    monkeypatch.setenv("SECRETS_KEY", "a-different-secrets-key-not-a-real-one")
    settings.cache_clear()
    after = await _card(admin)

    assert after["secrets"]["unreadable"] is True
    # The row did not move — that is the point. The ciphertexts still name this key_id, so an
    # operator who finds the right `.env` has lost nothing.
    assert after["secrets"]["key_id"] == key_id
    assert after["secrets"]["fingerprint"] != before["secrets"]["fingerprint"]


async def test_the_connectors_repair_does_not_heal_this_card_while_a_secret_is_still_sealed(
    secrets_key, db, app, monkeypatch
):
    """The card answers "is anything unopenable", not "does the active DEK row unwrap".

    Those were one question until M4.7 gave the Connectors card its repair. Pasting the API key
    over an unreadable DEK retires that row and seals under a fresh one, so the *active* row
    opens again — and a card that asked only about the active row flipped to `unreadable: false`
    at the exact moment the household's other secrets became unreachable: the VAPID pair here,
    and any second connector on a real install. The one surface built to report custody would
    have reported it healed. [M4.7 ops-11, dd03; decision 182]
    """
    admin = await _admin(app)
    saved = await admin.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test", "api_key": "JF-ADMIN-KEY-UNSCOPED"},
    )
    assert saved.status_code == 200, saved.text
    retired = (await _card(admin))["secrets"]["key_id"]

    monkeypatch.setenv("SECRETS_KEY", "a-different-secrets-key-not-a-real-one")
    settings.cache_clear()
    assert (await _card(admin))["secrets"]["unreadable"] is True

    repaired = await admin.put(
        "/api/admin/connectors/jellyfin",
        json={"url": "http://jellyfin.test", "api_key": "a-freshly-issued-jellyfin-key"},
    )
    assert repaired.status_code == 200, repaired.text
    # The shortcut worked, for Jellyfin: that is what makes the card's answer load-bearing
    # rather than pedantic, because this is the screen the admin is standing on.
    assert (await admin.get("/api/admin/connectors/jellyfin")).json()["secrets_unreadable"] is False

    card = await _card(admin)
    assert card["secrets"]["key_id"] != retired, "the repair minted a fresh DEK"
    assert card["secrets"]["unreadable"] is True, (
        "app_setting/push.vapid is still sealed under the retired key"
    )
    assert await db.fetchval(
        "SELECT secret_key_id FROM app_setting WHERE key = 'push.vapid'"
    ) == retired


async def test_an_install_with_no_secrets_key_reports_that_rather_than_a_fingerprint(
    no_secrets_key, db, app
):
    """§3.1 makes a half-configured boot a legal state, so the card describes it as one.

    Not an error and not an empty string: `configured: false` is what lets the surface say the
    key is unset rather than render a fingerprint of nothing and imply custody exists.
    """
    admin = await _admin(app)

    card = await _card(admin)

    assert card["secrets"]["configured"] is False
    assert card["secrets"]["fingerprint"] is None
    # Nothing minted a DEK, because nothing could: `ensure_dek` refuses without the key (§2).
    assert card["secrets"]["key_id"] is None
    assert card["secrets"]["unreadable"] is False


# --- who may read it ------------------------------------------------------------------


async def test_the_card_is_refused_to_a_signed_out_caller_and_to_a_member(secrets_key, db, app):
    """§6.6 is an admin surface, and this one names the custody state of the whole install.

    `test_api_gating.py` walks the dependency graph and would catch a missing `AdminUser` here
    as an arithmetic complaint about a route count; this says the same thing in the terms the
    route is written in, and it is cheap.
    """
    admin = await _admin(app)
    member = await _member(app, admin)
    anonymous = app()

    assert (await anonymous.get("/api/admin/system")).status_code == 401
    assert (await member.get("/api/admin/system")).status_code == 403
