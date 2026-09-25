"""`GET /api/art/{title_id}/poster` over HTTP. Spec v2.1 §6.8, §3.1, §3.2; decisions 483 and 484.

The real app, its lifespan and its pool against the test database; the image host is an
`httpx.MockTransport` swapped into `app.state.art`, and the household's Jellyfin is
`ops/fake_jellyfin.py` behind `registry.make_client`. What these assert is what a phone receives:
the status, the cache headers, which source the bytes came from, who is refused, and that the
request holds no pooled connection while it waits on a host. Skipped without TEST_DATABASE_URL.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from spielplan.art.poster import ArtService
from spielplan.connectors import registry
from spielplan.connectors.jellyfin import JellyfinClient
from spielplan.db import pool

PASSWORD = "an-admin-password"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
W500 = "https://image.tmdb.org/t/p/w500/heat.jpg"
W342 = "https://image.tmdb.org/t/p/w342/heat.jpg"
JELLYFIN_URL = "http://jellyfin.test"


def poster_url(title_id: int) -> str:
    return f"/api/art/{title_id}/poster"


class Host:
    def __init__(self, answer=None) -> None:
        self.asked: list[str] = []
        self.answer = answer

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(str(request.url))
        if self.answer is not None:
            return await self.answer(request)
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})


async def _admin(app) -> httpx.AsyncClient:
    client = app()
    made = await client.post("/api/setup/admin", json={"name": "patrick", "password": PASSWORD})
    assert made.status_code == 201
    return client


async def _host(client: httpx.AsyncClient, tmp_path, host: Host) -> ArtService:
    """The app's own service, re-opened over the mock host. The lifespan closes whichever is on
    `app.state.art` when it exits, so the replaced one is closed here."""
    application = client._transport.app
    await application.state.art.close()
    application.state.art = await ArtService(
        tmp_path / "art-under-test", transport=httpx.MockTransport(host)
    ).open()
    return application.state.art


async def _title(db, title_id: int, **columns) -> None:
    names = ["id", "kind", "name", "year", *columns]
    values = [title_id, "movie", f"Title {title_id}", 1995, *columns.values()]
    marks = ", ".join(f"${i}" for i in range(1, len(values) + 1))
    await db.execute(f"INSERT INTO title ({', '.join(names)}) VALUES ({marks})", *values)


async def test_a_stranger_is_refused_before_any_source_is_asked(app, db, tmp_path):
    admin = await _admin(app)
    host = Host()
    await _host(admin, tmp_path, host)
    await _title(db, 1, poster_path=W500)
    refused = await app().get(poster_url(1))
    assert refused.status_code == 401
    assert host.asked == []


async def test_a_locked_account_is_refused_by_the_poster_route(app, db, tmp_path):
    """§3.1 and decision 179: the route is behind the first-login lock like every other one, and
    a gate that let a locked account through here would be a hole the sweep could not see."""
    admin = await _admin(app)
    host = Host()
    await _host(admin, tmp_path, host)
    await _title(db, 1, poster_path=W500)
    otp = (await admin.post("/api/admin/users", json={"name": "jenny", "role": "member"})).json()[
        "one_time_password"
    ]
    locked = app()
    signed_in = await locked.post("/api/auth/login", json={"name": "jenny", "password": otp})
    assert signed_in.json()["must_change_password"] is True
    assert (await locked.get(poster_url(1))).status_code == 403
    assert host.asked == []


async def test_a_tmdb_poster_is_served_private_for_180_days_and_never_immutable(app, db, tmp_path):
    admin = await _admin(app)
    host = Host()
    await _host(admin, tmp_path, host)
    await _title(db, 1, poster_path=W500)
    served = await admin.get(poster_url(1))
    assert served.status_code == 200
    assert served.content == JPEG
    assert served.headers["content-type"] == "image/jpeg"
    assert served.headers["cache-control"] == "private, max-age=15552000"
    assert "immutable" not in served.headers["cache-control"]
    assert served.headers["x-content-type-options"] == "nosniff"
    assert host.asked == [W342]

    again = await admin.get(poster_url(1), headers={"if-none-match": served.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    assert host.asked == [W342], "a revalidation is answered from the cache"


async def test_an_owned_title_is_served_from_the_households_jellyfin(
    app, db, tmp_path, secrets_key, fake_jellyfin, monkeypatch
):
    module, transport = fake_jellyfin
    admin = await _admin(app)
    host = Host()
    await _host(admin, tmp_path, host)
    await registry.save_jellyfin(db, url=JELLYFIN_URL, api_key=module.API_KEY)
    monkeypatch.setattr(
        registry, "make_client",
        lambda cfg: JellyfinClient(cfg.url, cfg.api_key, transport=transport),
    )
    await _title(db, 1, poster_path=W500, jellyfin_id="jf-1", is_owned=True)
    served = await admin.get(poster_url(1))
    assert served.status_code == 200 and served.headers["content-type"] == "image/png"
    assert module.IMAGES_ASKED == ["jf-1"]
    assert host.asked == [], "the household's own art wins and TMDB is not asked"


async def test_a_posterless_title_is_filed_for_the_worker_and_drawn_as_the_tinted_panel(
    app, db, tmp_path
):
    """Decision 484: the web process files the title and asks TMDB nothing; the 404 is cacheable
    for one `art-lookup` interval, so the card asks again after the worker has had its turn."""
    admin = await _admin(app)
    host = Host()
    await _host(admin, tmp_path, host)
    await _title(db, 1, imdb_id="tt0368447",
                 poster_path="https://m.media-amazon.com/images/M/village.jpg")
    before = await db.fetchval("SELECT row_to_json(t)::text FROM title t WHERE id = 1")
    answer = await admin.get(poster_url(1))
    assert answer.status_code == 404
    assert answer.headers["cache-control"] == "private, max-age=1800"
    assert host.asked == [], "neither IMDb's host nor TMDB's API is asked from the web process"
    assert await db.fetchval("SELECT outcome IS NULL FROM art_lookup WHERE title_id = 1") is True
    await admin.get(poster_url(1))
    assert await db.fetchval("SELECT count(*) FROM art_lookup") == 1
    assert await db.fetchval("SELECT row_to_json(t)::text FROM title t WHERE id = 1") == before

    await db.execute(
        "UPDATE art_lookup SET outcome = 'found', looked_up_at = now(), "
        "poster_url = 'https://image.tmdb.org/t/p/w342/village.jpg' WHERE title_id = 1"
    )
    found = await admin.get(poster_url(1))
    assert found.status_code == 200
    assert host.asked == ["https://image.tmdb.org/t/p/w342/village.jpg"]


async def test_an_unknown_title_is_a_cacheable_404(app, tmp_path):
    admin = await _admin(app)
    await _host(admin, tmp_path, Host())
    answer = await admin.get(poster_url(999999))
    assert answer.status_code == 404
    assert answer.headers["cache-control"] == "private, max-age=86400"


async def test_no_pooled_connection_is_held_while_the_host_is_asked(app, db, tmp_path):
    """The pool holds ten and a cold Home asks for sixty posters: a route that held its connection
    across the upstream wait would answer every other surface 503 while a shelf filled."""
    admin = await _admin(app)
    asked, release = asyncio.Event(), asyncio.Event()

    async def stall(request):
        asked.set()
        await release.wait()
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    await _host(admin, tmp_path, Host(stall))
    await _title(db, 1, poster_path=W500)
    pending = asyncio.create_task(admin.get(poster_url(1)))
    try:
        await asyncio.wait_for(asked.wait(), 10)
        connections = pool.pool()
        assert connections.get_size() - connections.get_idle_size() == 0, (
            "a pooled connection is checked out while the image host is being asked"
        )
    finally:
        release.set()
    assert (await pending).status_code == 200


@pytest.mark.parametrize("title_id", ["x", "1.5"])
async def test_a_title_id_that_is_not_a_number_is_refused_by_the_route(app, title_id):
    admin = await _admin(app)
    assert (await admin.get(f"/api/art/{title_id}/poster")).status_code == 422
