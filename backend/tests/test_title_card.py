"""Asserted at the payload over HTTP: a client that hid a number it was sent would pass a render
test while breaking decision 486's promise."""

from __future__ import annotations

import json

import pytest

from spielplan.api import auth as auth_api

VOCAB = "v1"
ADMIN_PASSWORD = "an-admin-password"
MEMBER_PASSWORD = "a-member-password"


async def _member(app):
    """An admin to create the household, and a signed-in member past §3.1's password lock."""
    admin = app()
    created = await admin.post("/api/setup/admin", json={"name": "patrick", "password": ADMIN_PASSWORD})
    assert created.status_code == 201, created.text
    made = await admin.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    assert made.status_code == 201, made.text
    otp = made.json()["one_time_password"]
    member = app()
    signed_in = await member.post("/api/auth/login", json={"name": "jenny", "password": otp})
    assert signed_in.status_code == 200, signed_in.text
    changed = await member.post(
        "/api/auth/password", json={"current_password": otp, "new_password": MEMBER_PASSWORD}
    )
    assert changed.status_code == 200, changed.text
    return member


@pytest.fixture
async def card(db, app):
    """`era.wwii` ships a label that is not its leaf and `mood.gritty` none, so both halves of
    `labels_for` are on one card."""
    await db.executemany(
        "INSERT INTO title (id, kind, name, year, is_owned, jellyfin_id) VALUES ($1, $2, $3, $4, $5, $6)",
        [
            (1, "movie", "Heat", 1995, True, "jf-1"),
            (2, "movie", "Unowned", 2001, False, None),
            (3, "movie", "Rated From Its Card", 2010, True, None),
            (4, "movie", "Parked", 2011, True, None),
        ],
    )
    await db.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, 2, 2)", VOCAB
    )
    await db.executemany(
        "INSERT INTO dna_facet (version, facet, ord) VALUES ($1, $2, $3)",
        [(VOCAB, "era", 0), (VOCAB, "mood", 1)],
    )
    await db.executemany(
        "INSERT INTO dna_term (version, term, facet, gloss, label) VALUES ($1, $2, $3, $4, $5)",
        [
            (VOCAB, "era.wwii", "era", "set during the Second World War", "World War II"),
            (VOCAB, "mood.gritty", "mood", "rough, dirty, unvarnished", None),
        ],
    )
    tag_id = await db.fetchval(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, n_sources, "
        "provider) VALUES (1, $1, 'era.wwii', 'era', 3, 0.9, 2, '') RETURNING id",
        VOCAB,
    )
    await db.execute(
        "INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, 'a war film', 'trakt:1')",
        tag_id,
    )
    await db.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
        "VALUES (1, $1, 'mood.gritty', 'mood', 1, 'movielens_tags')",
        VOCAB,
    )
    return await _member(app)


async def test_a_card_read_with_the_switch_off_carries_no_model_number(card):
    """Decision 486 clause 3 is kept at the payload, not the render."""
    body = (await card.get("/api/titles/1")).json()
    assert "model_line" not in body, "the model line reached a member with Show the model off"
    (tag,) = body["dna"]["extracted"]
    for number in ("salience", "confidence", "n_sources"):
        assert number not in tag, f"the tag's {number} reached a member with the switch off"
    wire = json.dumps(body)
    assert "b(t)" not in wire and "gate" not in wire


async def test_the_switch_brings_the_model_line_and_the_weights_back(card):
    """Behind the switch, not deleted."""
    assert (await card.post("/api/auth/preferences", json={"show_model": True})).status_code == 200
    body = (await card.get("/api/titles/1")).json()
    assert body["model_line"]["available"] is False, "no bundle is imported in this fixture"
    assert body["model_line"]["reason"] == "no artifact bundle imported"
    (tag,) = body["dna"]["extracted"]
    assert (tag["salience"], tag["n_sources"]) == (3, 2)


async def test_the_card_names_every_term_by_its_shipped_label(card):
    """Decision 486 clause 4: the shipped label, or the leaf in words; never the id alone."""
    body = (await card.get("/api/titles/1")).json()
    (extracted,) = body["dna"]["extracted"]
    (projected,) = body["dna"]["projected"]
    assert extracted["term"] == "era.wwii", "the id stays: it is the key the tiers are joined on"
    assert extracted["label"] == "World War II"
    assert extracted["gloss"] == "set during the Second World War"
    assert projected["label"] == "gritty"
    # §4.1 rule 2: the projected weight is how many sources suggested the term.
    assert projected["weight"] == 1


async def test_the_card_and_the_catalog_carry_the_original_title_and_its_language(db, card):
    """Decision 516: the client leads with the original title only where the payload names its language."""
    await db.execute(
        "INSERT INTO title (id, kind, name, original_name, original_language, year, is_owned) "
        "VALUES (5, 'movie', 'Wonderfully Beautiful', 'Wunderschön', 'de', 2022, true)"
    )
    title = (await card.get("/api/titles/5")).json()["title"]
    assert (title["original_name"], title["original_language"]) == ("Wunderschön", "de")

    listed = (await card.get("/api/titles", params={"kind": "movie", "q": "wonderfully"})).json()
    (hit,) = listed["items"]
    assert (hit["name"], hit["original_name"], hit["original_language"]) == (
        "Wonderfully Beautiful", "Wunderschön", "de"
    )
    # No recorded original is nulls, never a missing key.
    heat = (await card.get("/api/titles", params={"kind": "movie", "q": "heat"})).json()["items"][0]
    assert heat["original_name"] is None and heat["original_language"] is None


async def test_the_platform_caption_is_plain_and_the_flag_still_travels(card):
    """§4.1 rule 3 rests on `display_only`, which stays."""
    ratings = (await card.get("/api/titles/1")).json()["platform_ratings"]
    assert ratings["display_only"] is True
    assert "never affect your suggestions" in ratings["note"]
    assert "model" not in ratings["note"] and "conduit" not in ratings["note"]


async def test_play_names_which_of_its_two_reasons_it_is(db, card):
    """The route says which reason: not in the library, or no server."""
    await db.execute(
        """INSERT INTO connector_config (name, config)
           VALUES ('jellyfin', '{"url": "http://jellyfin.test/"}'::jsonb)
           ON CONFLICT (name) DO UPDATE SET config = EXCLUDED.config"""
    )
    unowned = (await card.get("/api/titles/2")).json()["actions"]
    assert unowned["play_on_jellyfin"] is None
    assert unowned["play_reason"] == "not_in_library"

    owned = (await card.get("/api/titles/1")).json()["actions"]
    assert owned["play_on_jellyfin"] == "http://jellyfin.test/web/#/details?id=jf-1"
    assert owned["play_reason"] is None

    # With no server, the server is the reason for every title, owned or not.
    await db.execute("DELETE FROM connector_config WHERE name = 'jellyfin'")
    for title_id in (1, 2):
        serverless = (await card.get(f"/api/titles/{title_id}")).json()["actions"]
        assert serverless["play_on_jellyfin"] is None
        assert serverless["play_reason"] == "no_server"


async def test_show_on_map_is_absent_until_the_map_ships(card, monkeypatch):
    """Decision 488: follows the flag navigation follows."""
    assert (await card.get("/api/titles/1")).json()["actions"]["show_on_map"] is None

    shipped = tuple({**s, "built": True} if s["key"] == "map" else s for s in auth_api.SURFACES)
    monkeypatch.setattr(auth_api, "SURFACES", shipped)
    assert (await card.get("/api/titles/1")).json()["actions"]["show_on_map"] == {"title_id": 1}


async def _journal(db) -> list[dict]:
    rows = await db.fetch(
        "SELECT kind_of, title_ids, card, undone_at FROM rate_observation ORDER BY seq"
    )
    return [dict(r) for r in rows]


async def test_a_verdict_from_the_card_is_a_sweep_answer_in_the_rate_session(db, card):
    """Written through the Rate session (decision 212): a journal row Undo reverses, the counter
    moves, and the reveal rides on this response only."""
    answered = await card.post("/api/rate/title/3", json={"answer": "liked"})
    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body["session"]["block"]["slot"] == 2, "the answer did not move §6.1's counter"
    assert body["undo"] == {"available": True, "kind": "verdict", "reason": None}
    assert "reveal" in body

    uid = await db.fetchval("SELECT id FROM app_user WHERE name = 'jenny'")
    assert await db.fetchval(
        "SELECT value FROM verdict WHERE user_id = $1 AND title_id = 3", uid
    ) == 2
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = 3", uid
    ) == "seen", "§6.1: a verdict implies seen"
    (row,) = await _journal(db)
    assert row["kind_of"] == "verdict" and list(row["title_ids"]) == [3]
    assert row["card"]["source"] == "title_card"

    detail = (await card.get("/api/titles/3")).json()
    assert detail["my_verdict"] == {"value": 2, "label": "liked"}

    undone = await card.post("/api/rate/undo")
    assert undone.status_code == 200, undone.text
    assert await db.fetchval("SELECT count(*) FROM verdict WHERE title_id = 3") == 0
    assert (await card.get("/api/titles/3")).json()["my_verdict"] is None


async def test_the_card_answers_a_title_the_queue_would_never_serve(db, card):
    """A second verdict supersedes the first (§4.2)."""
    assert (await card.post("/api/rate/title/3", json={"answer": "liked"})).status_code == 200
    again = await card.post("/api/rate/title/3", json={"answer": "fine"})
    assert again.status_code == 200, again.text
    assert (await card.get("/api/titles/3")).json()["my_verdict"] == {"value": 1, "label": "fine"}
    live = await db.fetchval(
        "SELECT count(*) FROM verdict WHERE title_id = 3 AND superseded_by IS NULL"
    )
    assert live == 1, "a re-rating left two live verdicts"


async def test_not_seen_from_the_card_flips_the_state_and_keeps_the_verdict(db, card):
    """Not seen is a state; the verdict survives the flip."""
    assert (await card.post("/api/rate/title/3", json={"answer": "disliked"})).status_code == 200
    flipped = await card.post("/api/rate/title/3", json={"answer": "not_seen"})
    assert flipped.status_code == 200, flipped.text
    uid = await db.fetchval("SELECT id FROM app_user WHERE name = 'jenny'")
    assert await db.fetchval(
        "SELECT state FROM user_title WHERE user_id = $1 AND title_id = 3", uid
    ) == "unseen"
    detail = (await card.get("/api/titles/3")).json()
    assert detail["title"]["seen_state"] == "unseen"
    assert detail["my_verdict"] == {"value": 0, "label": "disliked"}
    assert [r["kind_of"] for r in await _journal(db)] == ["verdict", "not_seen"]


async def test_the_card_answer_takes_the_table_and_the_old_token_goes_stale(card):
    """A device holding the parked card's token meets the ordinary stale-card refusal."""
    parked = (await card.get("/api/rate")).json()["card"]
    assert parked is not None and parked["type"] == "sweep"
    target = 4 if parked["title"]["id"] != 4 else 3
    assert (await card.post(f"/api/rate/title/{target}", json={"answer": "fine"})).status_code == 200
    stale = await card.post("/api/rate/verdict", json={"card_token": parked["token"], "value": 2})
    assert stale.status_code == 409
    assert stale.json()["detail"]["reason"] == "stale_card"


async def test_the_card_answer_route_refuses_what_it_cannot_answer(app, card):
    """An unknown title is a 404, an answer §6.1 does not offer is a 422, and a stranger is a 401."""
    assert (await card.post("/api/rate/title/999", json={"answer": "liked"})).status_code == 404
    assert (await card.post("/api/rate/title/3", json={"answer": "loved"})).status_code == 422
    stranger = app()
    assert (await stranger.post("/api/rate/title/3", json={"answer": "liked"})).status_code == 401
