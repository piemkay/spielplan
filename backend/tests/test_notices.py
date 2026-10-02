"""Home's notices put away (decision 554, §6.0, §4.2 `notice_hidden`): a sticky one until the next local
midnight or until something new joins it, the finish prompt for good. Needs TEST_DATABASE_URL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfoNotFoundError

import pytest

from spielplan.core.config import settings
from spielplan.home import notices
from spielplan.sync import playback
from tests.helpers import household, insert_user

HEAT, PRISONERS = 701, 702
CEST = timezone(timedelta(hours=2), "CEST")
# 23:30 in Berlin on 2 October, 21:30 UTC.
HIDDEN_AT = datetime(2026, 10, 2, 21, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def berlin(monkeypatch):
    """No tz database on every test box: Berlin is pinned to its October offset."""

    def berlin_only(name: str):
        if name == "Europe/Berlin":
            return CEST
        raise ZoneInfoNotFoundError(name)

    monkeypatch.setenv("TZ", "Europe/Berlin")
    monkeypatch.setattr(notices, "ZoneInfo", berlin_only)
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
async def house(app, db):
    patrick, jenny = await household(app)
    await db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned) "
        "VALUES ($1, 'movie', 'Heat', 1995, true), ($2, 'movie', 'Prisoners', 2013, false)",
        HEAT, PRISONERS,
    )
    ids = {r["name"]: r["id"] for r in await db.fetch("SELECT id, name FROM app_user")}
    return patrick, jenny, ids["patrick"], ids["jenny"]


async def _home(client) -> dict:
    response = await client.get("/api/home", params={"kind": "movie"})
    assert response.status_code == 200, response.text
    return response.json()


async def _hidden_at(db, user_id: int, notice: str, at: datetime = HIDDEN_AT) -> None:
    await db.execute(
        "INSERT INTO notice_hidden (user_id, notice, hidden_at) VALUES ($1, $2, $3)",
        user_id, notice, at,
    )


async def test_a_sticky_x_hides_the_notice_for_that_member_and_undo_brings_it_back(house, db):
    patrick, jenny, patrick_id, _jenny_id = house
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, $2, 'seen')", patrick_id, HEAT
    )
    assert (await _home(patrick))["banner"]["count"] == 1

    hidden = await patrick.put("/api/home/notices/pending")
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["notice"] == "pending"
    assert datetime.fromisoformat(hidden.json()["hidden_at"]).tzinfo is not None
    assert (await _home(patrick))["banner"] is None

    await patrick.put("/api/home/notices/wish_list")
    assert (await _home(patrick))["wish"]["hidden"] is True
    assert (await _home(jenny))["wish"]["hidden"] is False, "put away for that member alone"

    undone = await patrick.delete("/api/home/notices/pending")
    assert undone.json() == {"notice": "pending", "hidden_at": None}
    assert (await _home(patrick))["banner"]["count"] == 1
    assert (await patrick.put("/api/home/notices/arrival")).status_code == 422


async def test_a_sticky_notice_comes_back_at_the_next_midnight_in_tz(house, db):
    _patrick, _jenny, patrick_id, _jenny_id = house
    await _hidden_at(db, patrick_id, "setup")

    before_midnight = datetime(2026, 10, 2, 21, 59, tzinfo=UTC)
    assert await notices.hidden(db, user_id=patrick_id, now=before_midnight) == {"setup"}
    at_midnight = datetime(2026, 10, 2, 22, 0, tzinfo=UTC)
    assert await notices.hidden(db, user_id=patrick_id, now=at_midnight) == frozenset()


async def test_an_unresolvable_tz_reads_the_day_in_utc(house, db, monkeypatch):
    _patrick, _jenny, patrick_id, _jenny_id = house
    monkeypatch.setenv("TZ", "Europe/Berln")
    settings.cache_clear()
    await _hidden_at(db, patrick_id, "setup")

    assert await notices.hidden(
        db, user_id=patrick_id, now=datetime(2026, 10, 2, 23, 59, tzinfo=UTC)
    ) == {"setup"}
    assert await notices.hidden(
        db, user_id=patrick_id, now=datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
    ) == frozenset()


async def test_the_pending_row_returns_once_a_title_is_written_seen_after_it_was_hidden(house, db):
    _patrick, _jenny, patrick_id, jenny_id = house
    await _hidden_at(db, patrick_id, "pending")
    now = HIDDEN_AT + timedelta(minutes=10)

    async def write(user_id: int, title_id: int, state: str, minutes: int) -> None:
        await db.execute(
            "INSERT INTO user_title (user_id, title_id, state, state_changed_at) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (user_id, title_id) DO UPDATE SET state = $3, state_changed_at = $4",
            user_id, title_id, state, HIDDEN_AT + timedelta(minutes=minutes),
        )

    await write(patrick_id, HEAT, "seen", -1)
    await write(patrick_id, PRISONERS, "unseen", 1)
    await write(jenny_id, PRISONERS, "seen", 1)
    assert await notices.hidden(db, user_id=patrick_id, now=now) == {"pending"}, (
        "an earlier seen, an unseen and another member's seen add nothing to the row"
    )
    await write(patrick_id, PRISONERS, "seen", 2)
    assert await notices.hidden(db, user_id=patrick_id, now=now) == frozenset()


async def test_the_wish_list_returns_once_any_member_wants_an_unowned_title_after_it(house, db):
    _patrick, _jenny, patrick_id, jenny_id = house
    await _hidden_at(db, patrick_id, "wish_list")
    now = HIDDEN_AT + timedelta(minutes=10)
    gone = await insert_user(db, "sam")
    await db.execute("UPDATE app_user SET is_active = false WHERE id = $1", gone)

    async def wish(user_id: int, title_id: int, state: str, minutes: int) -> None:
        await db.execute(
            "INSERT INTO wish (user_id, title_id, state, created_at) VALUES ($1, $2, $3, $4)",
            user_id, title_id, state, HIDDEN_AT + timedelta(minutes=minutes),
        )

    await wish(jenny_id, HEAT, "want", 1)
    await wish(gone, PRISONERS, "want", 1)
    await wish(patrick_id, PRISONERS, "not_for_me", 1)
    assert await notices.hidden(db, user_id=patrick_id, now=now) == {"wish_list"}, (
        "an owned title, a disabled member and a Not for me add nothing to the list"
    )
    await wish(jenny_id, PRISONERS, "want", 2)
    assert await notices.hidden(db, user_id=patrick_id, now=now) == frozenset()


async def test_the_set_up_notice_returns_only_with_the_day(house, db):
    _patrick, _jenny, patrick_id, _jenny_id = house
    await _hidden_at(db, patrick_id, "setup")
    later = HIDDEN_AT + timedelta(minutes=1)
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state, state_changed_at) VALUES ($1, $2, 'seen', $3)",
        patrick_id, PRISONERS, later,
    )
    await db.execute(
        "INSERT INTO wish (user_id, title_id, state, created_at) VALUES ($1, $2, 'want', $3)",
        patrick_id, PRISONERS, later,
    )
    assert await notices.hidden(
        db, user_id=patrick_id, now=HIDDEN_AT + timedelta(minutes=10)
    ) == {"setup"}


async def test_the_finish_prompts_x_closes_it_and_its_undo_reopens_it(house, db):
    patrick, _jenny, patrick_id, _jenny_id = house
    await playback.arm(db, user_id=patrick_id, title_id=HEAT, session_id="tv", progress=0.95)
    event_id = (await patrick.get("/api/prompts/finish")).json()[0]["id"]

    closed = await patrick.post(f"/api/prompts/finish/{event_id}/close")
    assert closed.json() == {"ok": True, "title_id": HEAT}
    assert (await patrick.get("/api/prompts/finish")).json() == []
    assert await db.fetchval("SELECT count(*) FROM user_title") == 0, "the x is no answer"
    assert (await patrick.post(f"/api/prompts/finish/{event_id}/close")).status_code == 404

    reopened = await patrick.post(f"/api/prompts/finish/{event_id}/reopen")
    assert reopened.json() == {"ok": True}
    assert [p["id"] for p in (await patrick.get("/api/prompts/finish")).json()] == [event_id]
    assert (await patrick.post(f"/api/prompts/finish/{event_id}/reopen")).status_code == 404
