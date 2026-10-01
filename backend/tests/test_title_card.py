"""Asserted at the payload over HTTP: a client that hid a number it was sent would pass a render
test while breaking decision 486's promise."""

from __future__ import annotations

import json

import pytest

from spielplan.home import why
from spielplan.ledger import observations, refit
from spielplan.ledger.hyperparams import DEFAULTS
from tests.helpers import household

VOCAB = "v1"


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
    _admin, member = await household(app)
    return member


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


async def test_the_card_names_its_genres_as_the_filter_does(db, card):
    """Every source's spelling folds into the facet's names; Wikidata's free text is not a genre."""
    await db.executemany(
        "INSERT INTO title_genre (title_id, genre, source) VALUES (1, $1, $2)",
        [("Thriller", "tmdb"), ("crime", "trakt"), ("Crime", "tmdb"), ("heist film", "wikidata")],
    )
    assert (await card.get("/api/titles/1")).json()["genres"] == ["Crime", "Thriller"]
    assert (await card.get("/api/titles/2")).json()["genres"] == []


async def test_the_platform_caption_is_plain(card):
    """§4.1 rule 3's label, in the member register."""
    ratings = (await card.get("/api/titles/1")).json()["platform_ratings"]
    assert "never change your suggestions" in ratings["note"]
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
    assert set(owned) == {"play_on_jellyfin", "play_reason"}, "decision 548: no Show on map"

    # With no server, the server is the reason for every title, owned or not.
    await db.execute("DELETE FROM connector_config WHERE name = 'jellyfin'")
    for title_id in (1, 2):
        serverless = (await card.get(f"/api/titles/{title_id}")).json()["actions"]
        assert serverless["play_on_jellyfin"] is None
        assert serverless["play_reason"] == "no_server"


async def _journal(db) -> list[dict]:
    rows = await db.fetch(
        "SELECT kind_of, title_ids, card, undone_at FROM rate_observation ORDER BY seq"
    )
    return [dict(r) for r in rows]


async def test_not_seen_from_the_card_is_a_rate_answer_in_the_persons_session(db, card):
    """Written through the Rate session (decision 212): a journal row Undo reverses, and the
    counter moves."""
    uid = await db.fetchval("SELECT id FROM app_user WHERE name = 'jenny'")
    await db.execute("INSERT INTO ladder_setup (user_id) VALUES ($1)", uid)
    await db.execute(
        "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 3, 'seen')", uid
    )
    answered = await card.post("/api/rate/title/3", json={"answer": "not_seen"})
    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body["session"]["block"]["slot"] == 2, "the answer did not move the counter"
    assert body["undo"] == {"available": True, "kind": "not_seen", "name": "Rated From Its Card"}

    state = "SELECT state FROM user_title WHERE user_id = $1 AND title_id = 3"
    assert await db.fetchval(state, uid) == "unseen"
    (row,) = await _journal(db)
    assert row["kind_of"] == "not_seen" and list(row["title_ids"]) == [3]
    assert row["card"]["source"] == "title_card"

    undone = await card.post("/api/rate/undo")
    assert undone.status_code == 200, undone.text
    assert await db.fetchval(state, uid) == "seen"


async def test_not_seen_from_the_card_works_before_the_set_up(db, card):
    """Plan reading 17: journalled through a session that draws no card, since Rate is closed."""
    uid = await db.fetchval("SELECT id FROM app_user WHERE name = 'jenny'")
    await observations.record_verdict(db, user_id=uid, title_id=3, value=0)
    flipped = await card.post("/api/rate/title/3", json={"answer": "not_seen"})
    assert flipped.status_code == 200, flipped.text
    assert flipped.json()["setup"]["done"] is False and flipped.json()["card"] is None
    assert (await card.get("/api/titles/3")).json()["title"]["seen_state"] == "unseen"
    assert await db.fetchval(
        "SELECT value FROM verdict WHERE user_id = $1 AND title_id = 3 AND superseded_by IS NULL", uid
    ) == 0, "Not seen is a state; the verdict survives the flip"
    assert [r["kind_of"] for r in await _journal(db)] == ["not_seen"]


async def test_the_card_answer_takes_the_table_and_the_old_token_goes_stale(db, card):
    """A device holding the parked card's token meets the ordinary stale-card refusal."""
    uid = await db.fetchval("SELECT id FROM app_user WHERE name = 'jenny'")
    await db.execute("INSERT INTO ladder_setup (user_id) VALUES ($1)", uid)
    parked = (await card.get("/api/rate")).json()["card"]
    assert parked is not None
    target = 4 if parked["title"]["id"] != 4 else 3
    taken = await card.post(f"/api/rate/title/{target}", json={"answer": "not_seen"})
    assert taken.status_code == 200, taken.text
    stale = await card.post("/api/rate/place", json={"card_token": parked["token"], "tier": 2})
    assert stale.status_code == 409
    assert stale.json()["detail"]["reason"] == "stale_card"


async def test_the_card_ranks_a_title_with_no_letter_before_the_persons_own_answer(db, card):
    """Decision 531: the rows name the person's own tier, never the model's guess; one tap places."""
    uid = await db.fetchval("SELECT id FROM app_user WHERE name = 'jenny'")
    await db.execute(
        "INSERT INTO ledger_state (user_id, title_id, kind, s, sigma, tier, observed) "
        "VALUES ($1, 4, 'movie', 2.0, 0.3, 6, false)",
        uid,
    )
    ranking = (await card.get("/api/titles/4")).json()["ranking"]
    assert ranking["tier"] is None and ranking["tension"] is None
    assert [(t["label"], t["verdict"], t["count"]) for t in ranking["tiers"]] == [
        ("S", "Liked", 0), ("A+", "Liked", 0), ("A", "Liked", 0), ("B", "Fine", 0),
        ("C", "Disliked", 0), ("D", "Disliked", 0), ("F", "Disliked", 0),
    ]

    placed = await card.post("/api/rank/drop?kind=movie&per_tier=1", json={"title_id": 4, "tier": 5})
    assert placed.status_code == 200, placed.text
    # The first observation of a kind is fitted by the sweep, not in the request.
    await refit.refit_user(db, user_id=uid, kind="movie", hp=DEFAULTS)
    body = (await card.get("/api/titles/4")).json()
    assert body["ranking"]["tier"] == 5
    assert body["title"]["seen_state"] == "seen"
    assert await db.fetchval(
        "SELECT value FROM verdict WHERE user_id = $1 AND title_id = 4 AND superseded_by IS NULL", uid
    ) == 2


async def test_the_card_answer_route_takes_not_seen_alone(db, app, card):
    """Decision 536: the card rates on the ladder, so a verdict here is a 422 and writes nothing; an
    unknown title is a 404, and a stranger is a 401."""
    assert (await card.post("/api/rate/title/999", json={"answer": "not_seen"})).status_code == 404
    for answer in ("disliked", "fine", "liked", "loved"):
        assert (await card.post("/api/rate/title/3", json={"answer": answer})).status_code == 422
    assert await db.fetchval("SELECT count(*) FROM verdict") == 0
    stranger = app()
    assert (await stranger.post("/api/rate/title/3", json={"answer": "not_seen"})).status_code == 401


# --- Shares a lot with (decisions 541 and 550) ---------------------------------------------------

LA, HEIST, TENSE, RARE = "place.los_angeles", "themes.heist", "mood.tense", "themes.rare"


@pytest.fixture
async def shares(db, card):
    """Target 10 carries three terms. 11 shares all three, 12-19 two (eight of them reach the cap,
    19 does not), 20 one. 21 is unowned, 22 a series and 23 animated, each carrying all three, and
    `themes.rare` sits on no owned title."""
    await db.executemany(
        "INSERT INTO dna_facet (version, facet, ord) VALUES ($1, $2, $3)",
        [(VOCAB, "place", 2), (VOCAB, "themes", 3)],
    )
    await db.executemany(
        "INSERT INTO dna_term (version, term, facet, gloss, label) VALUES ($1, $2, $3, '', $4)",
        [(VOCAB, LA, "place", "Los Angeles"), (VOCAB, HEIST, "themes", "Heist"),
         (VOCAB, TENSE, "mood", None), (VOCAB, RARE, "themes", "Rare")],
    )
    titles = [(10, "movie", "Target", True), *[(i, "movie", f"Film {i}", True) for i in range(11, 21)],
              (21, "movie", "Unowned", False), (22, "series", "A Series", True),
              (23, "movie", "A Cartoon", True), (24, "movie", "Also Unowned", False)]
    await db.executemany(
        "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, $2, $3, 2000, $4)", titles
    )
    await db.execute("INSERT INTO title_genre (title_id, genre, source) VALUES (23, 'Animation', 'tmdb')")
    tags = {10: (LA, HEIST, TENSE), 11: (LA, HEIST, TENSE), 20: (HEIST,), 21: (LA, HEIST, TENSE, RARE),
            22: (LA, HEIST, TENSE), 23: (LA, HEIST, TENSE), 24: (HEIST, RARE),
            **{i: (HEIST, TENSE) for i in range(12, 20)}}
    await db.executemany(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
        "VALUES ($1, $2, $3, $4, 1, 'movielens_tags')",
        [(t, VOCAB, term, term.split(".")[0]) for t, terms in tags.items() for term in terms],
    )
    uid = await db.fetchval("SELECT id FROM app_user WHERE name = 'jenny'")
    await db.execute("INSERT INTO user_title (user_id, title_id, state) VALUES ($1, 11, 'seen')", uid)
    return card


async def test_shares_are_the_closest_owned_titles_of_the_kind_seen_ones_included(shares):
    """Owned, the card's kind and form, two terms or more, at most eight, closest first; the seen
    one carries its mark and each its strongest shared term."""
    items = (await shares.get("/api/titles/10")).json()["shares"]
    assert [i["title_id"] for i in items] == [11, 12, 13, 14, 15, 16, 17, 18]
    first, second = items[0], items[1]
    assert first["seen"] is True and second["seen"] is False
    # The rarer the shared term in the owned films, the stronger: three films carry Los Angeles.
    assert first["term"] == {"term": LA, "facet": "place", "label": "Los Angeles"}
    assert second["term"] == {"term": TENSE, "facet": "mood", "label": "tense"}
    assert set(first) == {
        "title_id", "kind", "name", "original_name", "original_language", "year", "runtime_min",
        "poster_path", "seen", "term",
    }
    assert first["kind"] == "movie" and first["name"] == "Film 11"


async def test_an_unowned_card_shares_with_the_library_and_never_with_itself(shares):
    items = (await shares.get("/api/titles/21")).json()["shares"]
    assert [i["title_id"] for i in items][:2] == [10, 11]
    assert 21 not in [i["title_id"] for i in items] and len(items) == 8


async def test_shares_are_absent_under_three(db, shares):
    await db.execute("DELETE FROM dna_projected WHERE title_id BETWEEN 13 AND 19")
    assert (await shares.get("/api/titles/10")).json()["shares"] == []
    await db.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
        "VALUES (13, $1, $2, 'themes', 1, 'x'), (13, $1, $3, 'mood', 1, 'x')",
        VOCAB, HEIST, TENSE,
    )
    assert [i["title_id"] for i in (await shares.get("/api/titles/10")).json()["shares"]] == [11, 12, 13]
    # A title sharing nothing has no row at all.
    assert (await shares.get("/api/titles/3")).json()["shares"] == []


async def test_likeness_reads_any_two_titles_owned_or_not(db, shares):
    """Rate's shelves reuse it (decision 551): no owned, unseen, kind or form filter, one shared term
    is enough, and a term no owned title carries weighs as the rarest."""
    likes = await why.likeness(
        db, title_id=21, candidate_ids=[10, 20, 21, 22, 23, 24, 3], kind="movie", version=VOCAB
    )
    assert set(likes) == {10, 20, 22, 23, 24}
    assert likes[24].shared == 2 and likes[24].term == RARE and likes[24].label == "Rare"
    assert likes[20].shared == 1
    same = await why.likeness(db, title_id=10, candidate_ids=[23, 11], kind="movie", version=VOCAB)
    assert same[23].likeness == pytest.approx(1.0) and same[11].likeness == pytest.approx(1.0)
    assert likes[10].likeness < 1.0, "the rare term is the target's and not the candidate's"
