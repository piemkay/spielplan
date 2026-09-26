"""§6.0's Home and §6.7's rail. The fixture is built to break each claim: decoys carrying one of shelf 1's
two named terms, a superseded-only verdict in the banner, every series outscoring every film, and β at 0.62.
Needs TEST_DATABASE_URL."""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from spielplan.core.config import settings
from spielplan.home import rail, shelves
from spielplan.home import why as why_mod
from spielplan.ledger import model, refit
from spielplan.ledger.hyperparams import DEFAULTS
from spielplan.rank import read

BUNDLE = "test-home-v1"
VOCAB = "v1"

MOVIES = tuple(range(1000, 1050))
SERIES = tuple(range(1100, 1150))
BASES = (1000, 1100)

# Per kind, by offset from the base id. Every group exists to make one assertion falsifiable.
ANCHOR = 0
MEMBERS = (1, 2, 3, 4)          # carry BOTH named terms — the expected shelf-1 membership
DECOYS = (5, 6, 7)              # carry obsession + period, NOT morally-grey — the falsifier
FRONTIER = (8, 9, 10, 11)       # carry `neon`, which no seen title carries; also the cold ones
LIKED = (12, 13, 14)            # seen, high CDF, carry `cosy` — the frontier's named neighbour
PENDING = (21, 22, 23)          # seen, no live verdict — the banner's population
# No title repeats on a second shelf of its kind (decision 475), so every shelf has its own population.
SHORT = (24, 25, 26, 27)
FILLER = tuple(range(24, 50))

# Strict `<`, NULL runtime excluded; some titles sit exactly ON the threshold and some have none.
RUNTIME = {
    1: 95, 2: 100, 3: 105, 4: 90, 5: 110, 6: None, 7: 130,
    8: 140, 9: 150, 10: 160, 11: 170,
    24: 80, 25: 85, 26: 95, 27: 100, 28: 110, 29: None,
}
SERIES_RUNTIME = {
    1: 30, 2: 35, 3: 40, 4: 25, 5: 45, 6: None, 7: 60,
    8: 50, 9: 55, 10: 50, 11: 55,
    24: 20, 25: 25, 26: 30, 27: 35, 28: 45, 29: None,
}

# β seeded away from 0.8, so a why-line printing the constant is visible.
FITTED_BETA = 0.62


# DECOYS rank highest among the unseen: the sweet spot's population.
UNSEEN_ORDER = DECOYS + MEMBERS + FRONTIER


def score_of(title_id: int) -> float:
    """§4.1 rule 5's landmine in miniature: EVERY series outscores EVERY film."""
    base = 1000 if title_id < 1100 else 1100
    offset = title_id - base
    if offset == ANCHOR:
        film = 0.11
    elif offset in UNSEEN_ORDER:
        film = 0.45 - 0.01 * UNSEEN_ORDER.index(offset)
    elif offset in FILLER:
        film = 0.10 - 0.001 * (offset - FILLER[0])
    else:
        film = 0.60 - 0.01 * (offset - 12)
    return film if base == 1000 else film + 1.0


def kind_of(title_id: int) -> str:
    return "movie" if title_id < 1100 else "series"


def ids(base: int, offsets) -> list[int]:
    return [base + o for o in offsets]


class World:
    def __init__(self, client, db, patrick, jenny, jenny_otp, app):
        self.client, self.db, self.app = client, db, app
        self.patrick, self.jenny, self.jenny_otp = patrick, jenny, jenny_otp

    async def sign_in_jenny(self):
        """§3.1: an OTP-only login is not yet an `ActiveUser`."""
        client = self.app()
        await client.post("/api/auth/login", json={"name": "jenny", "password": self.jenny_otp})
        await client.post(
            "/api/auth/password",
            json={"current_password": self.jenny_otp, "new_password": "a-real-password"},
        )
        return client

    async def home(self, *, kinds=("movie", "series"), **params):
        query = [("kind", k) for k in kinds] + [(k, v) for k, v in params.items() if v is not None]
        response = await self.client.get("/api/home", params=query)
        assert response.status_code == 200, response.text
        return response.json()

    def shelf(self, payload, shelf_id):
        for shelf in payload["shelves"]:
            if shelf["id"] == shelf_id:
                return shelf
        return None

    def section(self, payload, shelf_id, kind):
        shelf = self.shelf(payload, shelf_id)
        if shelf is None:
            return None
        return next((s for s in shelf["sections"] if s["kind"] == kind), None)


async def _seed_vocabulary(conn) -> None:
    await conn.execute(
        "INSERT INTO dna_vocabulary (version, facet_count, term_count) VALUES ($1, 5, 5)", VOCAB
    )
    for ord_, facet in enumerate(("mood", "themes", "character", "visual", "era")):
        await conn.execute(
            "INSERT INTO dna_facet (version, facet, ord) VALUES ($1, $2, $3)", VOCAB, facet, ord_
        )
    for term, facet in (
        ("obsession", "themes"), ("morally-grey", "character"), ("period", "era"),
        ("neon", "visual"), ("cosy", "mood"),
    ):
        await conn.execute(
            "INSERT INTO dna_term (version, term, facet) VALUES ($1, $2, $3)", VOCAB, term, facet
        )


async def _tag(conn, title_id: int, term: str, facet: str, salience: int) -> None:
    """§4.1: every extracted tag carries its evidence, as the importer requires."""
    tag_id = await conn.fetchval(
        """
        INSERT INTO dna_tag (title_id, version, term, facet, salience, provider)
        VALUES ($1, $2, $3, $4, $5, 'test') RETURNING id
        """,
        title_id, VOCAB, term, facet, salience,
    )
    await conn.execute(
        "INSERT INTO dna_evidence (dna_tag_id, quote, source) VALUES ($1, $2, 'test:quote')",
        tag_id, f"evidence for {term} on {title_id}",
    )


async def _project(conn, title_id: int, term: str, facet: str, w: float) -> None:
    await conn.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
        "VALUES ($1, $2, $3, $4, $5, 'keyword')",
        title_id, VOCAB, term, facet, w,
    )


async def seed(conn, *, patrick: int, jenny: int) -> None:
    now = datetime.now(UTC)
    await conn.execute(
        "INSERT INTO artifact_bundle (version, manifest, state) VALUES ($1, '{}'::jsonb, 'active')",
        BUNDLE,
    )
    await _seed_vocabulary(conn)

    cold = {b + o for b in BASES for o in FRONTIER}
    warm_with_support = {1007, 1107}
    for title_id in MOVIES + SERIES:
        base = 1000 if title_id < 1100 else 1100
        offset = title_id - base
        kind = kind_of(title_id)
        table = RUNTIME if kind == "movie" else SERIES_RUNTIME
        runtime = table.get(offset, 120 if kind == "movie" else 50)
        placement = "cold_tower" if title_id in cold else "warm"
        # `title_placement_has_basis` makes "placed" and "names a basis" one fact.
        await conn.execute(
            """
            INSERT INTO title
                (id, kind, name, year, runtime_min, is_owned, placement, placement_at,
                 placement_bundle)
            VALUES ($1, $2, $3, $4, $5, true, $6, $7, $8)
            """,
            title_id, kind,
            f"Home {'Film' if kind == 'movie' else 'Series'} {title_id}",
            2000 + offset, runtime, placement,
            now - timedelta(days=100 - offset) if placement == "cold_tower" else None,
            BUNDLE,
        )

    # Projected weights are `n_sources` counts (the anchor's
    # 8 is the corpus maximum), as the importer writes.
    for base in BASES:
        await _tag(conn, base + ANCHOR, "obsession", "themes", 3)
        await _tag(conn, base + ANCHOR, "morally-grey", "character", 2)
        await _project(conn, base + ANCHOR, "period", "era", 8)
        for offset in MEMBERS:
            await _tag(conn, base + offset, "obsession", "themes", 2)
            await _tag(conn, base + offset, "morally-grey", "character", 2)
        for offset in DECOYS:
            await _tag(conn, base + offset, "obsession", "themes", 2)
            await _project(conn, base + offset, "period", "era", 1)
        for offset in FRONTIER:
            await _tag(conn, base + offset, "neon", "visual", 2)
        for offset in FRONTIER[:3]:
            await _tag(conn, base + offset, "cosy", "mood", 2)
        for offset in LIKED:
            await _tag(conn, base + offset, "cosy", "mood", 3)

    # Patrick has 13 seen per kind (>= FRONTIER_MIN_SEEN); Jenny 11, leaving some unseen by BOTH.
    for base in BASES:
        for offset in [ANCHOR] + list(range(12, 24)):
            await conn.execute(
                "INSERT INTO user_title (user_id, title_id, state, state_changed_at) "
                "VALUES ($1, $2, 'seen', $3)",
                patrick, base + offset, now - timedelta(minutes=100 - offset),
            )
        for offset in [ANCHOR] + list(range(12, 22)):
            await conn.execute(
                "INSERT INTO user_title (user_id, title_id, state) VALUES ($1, $2, 'seen')",
                jenny, base + offset,
            )

    # Live verdicts on the anchor and offsets 12..20; 21..23 are seen and unrated: the banner.
    for base in BASES:
        for offset in [ANCHOR] + list(range(12, 21)):
            await conn.execute(
                "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, $2, 2)",
                patrick, base + offset,
            )
    # The falsifier for `superseded_by IS NULL`: a superseded row is not a live verdict.
    live = await conn.fetchval(
        "SELECT id FROM verdict WHERE user_id = $1 AND title_id = 1000", patrick
    )
    await conn.execute(
        "INSERT INTO verdict (user_id, title_id, value, superseded_by) VALUES ($1, 1021, 1, $2)",
        patrick, live,
    )

    # §4.2 / §5.2: the nightly MAP output, and the learned cutpoints whose set names the tiers.
    for base in BASES:
        kind = kind_of(base)
        await conn.execute(
            "INSERT INTO ledger_cutpoints (user_id, kind, boundaries) "
            "VALUES ($1, $2, ARRAY[-2.0, -1.0, 0.0, 1.0, 2.0, 3.0]::double precision[])",
            patrick, kind,
        )
        rows = [(ANCHOR, 3.0, 0.98, 4)]
        rows += [(o, 2.0, 0.90, 4) for o in LIKED]
        rows += [(o, 1.0, 0.50, 3) for o in range(15, 21)]
        for offset, s, cdf, tier in rows:
            await conn.execute(
                """
                INSERT INTO ledger_state (user_id, title_id, s, sigma, cdf, tier, kind, observed)
                VALUES ($1, $2, $3, 0.2, $4, $5, $6, true)
                """,
                patrick, base + offset, s, cdf, tier, kind,
            )
        await conn.execute(
            """
            INSERT INTO user_vector (user_id, kind, purpose, vec, blend_beta, label_count,
                                     bundle_version)
            VALUES ($1, $2, 'foldin', $3, $4, 10, $5)
            """,
            patrick, kind, b"\x00" * 256, FITTED_BETA, BUNDLE,
        )

    # §5.1's two materialised halves.
    for title_id in MOVIES + SERIES:
        if title_id in cold:
            b, item_n, gate, source = 0.40, 0, 0.0, "cold_tower"
        elif title_id in warm_with_support:
            b, item_n, gate, source = 0.62, 4213, 0.998, "backbone"
        else:
            b, item_n, gate, source = 0.50, 100, 0.909, "backbone"
        await conn.execute(
            """
            INSERT INTO title_prior (title_id, bundle_version, b, b_i, item_n, gate, e_source)
            VALUES ($1, $2, $3, $3, $4, $5, $6)
            """,
            title_id, BUNDLE, b, item_n, gate, source,
        )
        for user_id in (patrick, jenny):
            await conn.execute(
                """
                INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf)
                VALUES ($1, $2, $3, $4, $5, 0.0)
                """,
                user_id, title_id, kind_of(title_id), BUNDLE, score_of(title_id),
            )

    # §6.0: credits filter the library to a filmography.
    await conn.execute("INSERT INTO person (id, name) VALUES (900, 'Ada Cross-Kind')")
    for title_id in (1001, 1101):
        await conn.execute(
            "INSERT INTO credit (title_id, person_id, department, job, source) "
            "VALUES ($1, 900, 'Directing', 'Director', 'tmdb')",
            title_id,
        )


@pytest.fixture
async def world(app, db):
    client = app()
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201, created.text
    member = await client.post("/api/admin/users", json={"name": "jenny", "role": "member"})
    assert member.status_code == 201, member.text
    patrick = await db.fetchval("SELECT id FROM app_user WHERE name = 'patrick'")
    jenny = await db.fetchval("SELECT id FROM app_user WHERE name = 'jenny'")
    await seed(db, patrick=patrick, jenny=jenny)
    return World(client, db, patrick, jenny, member.json()["one_time_password"], app)


async def test_every_section_carries_a_why_line_and_every_card_carries_every_named_term(world):
    """Every term a why-line names, and every shared term, is on EVERY card of that section."""
    payload = await world.home()
    assert payload["shelves"], "no shelves at all — the fixture is not exercising the surface"

    checked = 0
    for shelf in payload["shelves"]:
        for section in shelf["sections"]:
            assert section["why"].strip(), f"{shelf['id']}/{section['kind']} has no why-line"
            named = [t["term"] for t in section["why_terms"] if t["role"] == "member"]
            named += [t["term"] for t in section["shared_terms"]]
            card_ids = [c["title_id"] for c in section["items"]]
            for term in named:
                carriers = await world.db.fetchval(
                    "SELECT count(DISTINCT title_id) FROM dna_tagged "
                    "WHERE version = $1 AND term = $2 AND title_id = ANY($3)",
                    VOCAB, term, card_ids,
                )
                assert carriers == len(card_ids), (
                    f"{shelf['id']}/{section['kind']} names {term!r} but only {carriers} of "
                    f"{len(card_ids)} cards carry it"
                )
                checked += 1
            # An `anchor_side` term describes the liked region, not the cards, and is labelled so.
            for term in section["why_terms"]:
                assert term["role"] in ("member", "anchor_side")
                assert term["tier"] in ("extracted", "projected")
    assert checked >= 6, f"only {checked} term claims were checkable — the fixture went quiet"


async def test_a_card_carrying_only_one_of_the_two_named_terms_is_not_on_the_shelf(world):
    """Decoys carry two of the anchor's terms but not both named ones; they must not be admitted."""
    payload = await world.home()
    for base in BASES:
        section = world.section(payload, "because_anchor", kind_of(base))
        assert section is not None, f"shelf 1 missing for {kind_of(base)}"
        assert [c["title_id"] for c in section["items"]] == ids(base, MEMBERS)
        assert {t["term"] for t in section["why_terms"]} == {"morally-grey", "obsession"}
        assert not set(ids(base, DECOYS)) & {c["title_id"] for c in section["items"]}
        assert section["why"] == "shares morally-grey + obsession with it"
        # Decision 476: "you put" only for a title the person placed on Rank.
        assert section["title"] == f"Because you liked Home {'Film' if base == 1000 else 'Series'} " \
                                  f"{base}"


async def test_the_ledger_shelf_names_the_beta_its_own_ranking_used(world):
    """With the switch off no β; with it on the fitted β, never the constant."""
    payload = await world.home()
    for kind in ("movie", "series"):
        section = world.section(payload, "top_of_ledger", kind)
        assert section is not None
        assert "β" not in section["why"] and "0.62" not in section["why"], section["why"]
        assert "why_numbers" not in section, "a model number reached a member with the switch off"
        assert "rewatches included" in section["why"]

    await world.client.post("/api/auth/preferences", json={"show_model": True})
    payload = await world.home()
    for kind in ("movie", "series"):
        section = world.section(payload, "top_of_ledger", kind)
        assert section["why_numbers"]["beta"] == pytest.approx(FITTED_BETA, abs=1e-6)
        assert section["why_numbers"]["beta"] != pytest.approx(0.8, abs=1e-6)


async def test_the_optimum_the_ledger_shelf_prints_is_this_apps_own_and_not_the_corpuss(world):
    """Decision 167: the corpus's 0.8 weighs the crowd, so
    this app's optimum is 0.2; behind Show the model."""
    await world.client.post("/api/auth/preferences", json={"show_model": True})
    payload = await world.home()
    for kind in ("movie", "series"):
        section = world.section(payload, "top_of_ledger", kind)
        assert section is not None
        assert section["why_numbers"]["beta_optimum"] == pytest.approx(0.2, abs=1e-9)
        assert section["caption"] is None, section["caption"]

    # Never fitted: ranked by the crowd alone, and said so without a β.
    await world.db.execute(
        "DELETE FROM user_vector WHERE user_id = $1 AND purpose = 'foldin'", world.patrick
    )
    await world.client.post("/api/auth/preferences", json={"show_model": False})
    payload = await world.home()
    for kind in ("movie", "series"):
        section = world.section(payload, "top_of_ledger", kind)
        assert section["caption"] is None, section["caption"]
        assert "until your own ratings take over" in section["why"], section["why"]
        assert "§" not in section["why"] and "β" not in section["why"]


async def test_the_school_night_shelf_names_the_threshold_its_cards_obey(world):
    """110 min for films, 45 for episodes; strict `<`, NULL runtimes excluded."""
    # With the switch on, because the threshold also rides `why_numbers`.
    await world.client.post("/api/auth/preferences", json={"show_model": True})
    payload = await world.home()
    expected = {"movie": ("Under 110 minutes", 110), "series": ("Episodes under 45 minutes", 45)}
    for base in BASES:
        kind = kind_of(base)
        title, threshold = expected[kind]
        section = world.section(payload, "school_night", kind)
        assert section is not None
        assert section["title"] == title
        assert section["why"] == "for a school night"
        assert section["why_numbers"]["max_minutes"] == threshold
        # SHORT, and not MEMBERS, which are as short: shelf 1 claimed them first.
        assert [c["title_id"] for c in section["items"]] == sorted(ids(base, SHORT))
        for card in section["items"]:
            assert card["runtime_min"] is not None
            assert card["runtime_min"] < threshold
        shown = {c["title_id"] for c in section["items"]}
        assert not shown & set(ids(base, MEMBERS)), "a title shelf 1 shows is shown again here"
        assert base + 28 not in shown, "a title at exactly the threshold is not *under* it"
        assert base + 29 not in shown, "a NULL runtime cannot satisfy a runtime claim"


async def test_a_shelf_that_cannot_justify_itself_is_absent_not_empty(world):
    """Absent, never present-and-empty (floor of three); suppression is per section."""
    for title_id in (1002, 1003):
        await world.db.execute(
            "DELETE FROM dna_tag WHERE title_id = $1 AND term = 'morally-grey'", title_id
        )
    await world.db.execute("DELETE FROM dna_projected WHERE title_id = 1005 AND term = 'period'")
    payload = await world.home()
    film = world.section(payload, "because_anchor", "movie")
    assert film is None, "shelf 1's film section should be absent, not short"
    assert world.section(payload, "because_anchor", "series") is not None
    for shelf in payload["shelves"]:
        assert shelf["sections"], f"{shelf['id']} shipped with no sections"
        for section in shelf["sections"]:
            assert len(section["items"]) >= shelves.SECTION_FLOOR

    await world.client.post("/api/auth/preferences", json={"show_model": True})
    with_model = await world.home()
    reasons = [
        s for s in with_model["suppressed"]
        if s["shelf"] == "because_anchor" and s["kind"] == "movie"
    ]
    assert reasons, with_model["suppressed"]
    assert "3" in reasons[0]["reason"], reasons[0]["reason"]


async def test_the_new_in_library_shelf_only_carries_titles_with_no_crowd_support(world):
    """"No crowd data" is checked by `item_n`; 1007 is warm with 4,213 ratings and must not appear."""
    payload = await world.home()
    for base in BASES:
        section = world.section(payload, "new_in_library", kind_of(base))
        assert section is not None
        # Decision 476's words for the same claim.
        assert section["why"] == "no outside ratings yet, so we placed them by what they're about"
        shown = [c["title_id"] for c in section["items"]]
        # Ordered by recency, newest first: the one shelf that is not score-ordered.
        assert shown == sorted(ids(base, FRONTIER), reverse=True)
        assert base + 7 not in shown, "a warm title with 4213 crowd ratings is not 'new'"
        for card in section["items"]:
            assert card["placement"] == "cold_tower"
            support = await world.db.fetchval(
                "SELECT item_n FROM title_prior WHERE title_id = $1", card["title_id"]
            )
            assert support == 0, (
                f"{card['title_id']} claims no crowd data but the crowd rated it {support} times"
            )


async def test_the_frontier_shelf_names_a_term_no_seen_title_carries(world):
    """"Never" is literal zero coverage; the named neighbour is an `anchor_side` term."""
    payload = await world.home()
    for base in BASES:
        section = world.section(payload, "never_watched_term", kind_of(base))
        assert section is not None
        assert section["title"] == "You've never watched anything neon"
        assert section["why"] == "close to cosy, which you like", section["why"]
        roles = {t["term"]: t["role"] for t in section["why_terms"]}
        assert roles == {"neon": "member", "cosy": "anchor_side"}
        assert sorted(c["title_id"] for c in section["items"]) == sorted(ids(base, FRONTIER))
        seen_carriers = await world.db.fetchval(
            """
            SELECT count(*) FROM dna_tagged d
              JOIN user_title ut ON ut.title_id = d.title_id AND ut.user_id = $1
                                AND ut.state = 'seen'
              JOIN title t ON t.id = d.title_id AND t.kind = $2
             WHERE d.version = $3 AND d.term = 'neon'
            """,
            world.patrick, kind_of(base), VOCAB,
        )
        assert seen_carriers == 0, "'never watched' must mean zero coverage, not low coverage"


async def test_the_sweet_spot_is_unseen_by_both_and_high_for_both(world):
    """The plain average of the two scores, which is also how Tonight's pool ranks."""
    payload = await world.home()
    assert payload["partner"]["name"] == "jenny"
    for base in BASES:
        section = world.section(payload, "shared_sweet_spot", kind_of(base))
        assert section is not None
        # Decision 476: the title says what the shelf predicts.
        assert section["title"] == "You and jenny would both enjoy these"
        assert section["why"] == "neither of you has seen them — a good pick for a night in together"
        assert section["caption"] is None
        shown = [c["title_id"] for c in section["items"]]
        assert shown == ids(base, DECOYS), "the fixture's sweet spot is DECOYS, in score order"
        assert shown == sorted(shown, key=lambda t: -score_of(t))
        for title_id in shown:
            for user_id in (world.patrick, world.jenny):
                seen = await world.db.fetchval(
                    "SELECT count(*) FROM user_title WHERE user_id = $1 AND title_id = $2 "
                    "AND state = 'seen'",
                    user_id, title_id,
                )
                assert seen == 0, f"{title_id} is on a 'neither of you has seen these' shelf"


async def test_the_sweet_spot_floor_is_read_against_the_owned_library(world):
    """The floor is a rank within the owned library, not the whole scored catalogue."""
    def shelf(payload):
        shown = {}
        for base in BASES:
            section = world.section(payload, "shared_sweet_spot", kind_of(base))
            shown[base] = None if section is None else [c["title_id"] for c in section["items"]]
        return shown

    before = shelf(await world.home())
    assert all(before.values()), before
    for base in BASES:
        kind = kind_of(base)
        # Past each kind's owned ids, so `kind_of` still reads them.
        for title_id in range(base + 60, base + 100):
            await world.db.execute(
                "INSERT INTO title (id, kind, name, year, is_owned) VALUES ($1, $2, $3, 2000, false)",
                title_id, kind, f"Unowned {title_id}",
            )
            await world.db.execute(
                "INSERT INTO title_prior (title_id, bundle_version, b, b_i, item_n, gate, e_source) "
                "VALUES ($1, $2, 0.9, 0.9, 5000, 0.998, 'backbone')",
                title_id, BUNDLE,
            )
            for user_id in (world.patrick, world.jenny):
                await world.db.execute(
                    "INSERT INTO user_score (user_id, title_id, kind, bundle_version, score, cf) "
                    "VALUES ($1, $2, $3, $4, 9.0, 0.0)",
                    user_id, title_id, kind, BUNDLE,
                )
    after = shelf(await world.home())
    assert all(after.values()), f"unowned titles pushed the owned library under the floor: {after}"
    assert after == before


async def test_a_synced_seen_but_unobserved_title_cannot_anchor_shelf_one(world):
    """The anchor must be an observed title: `refit_user`
    writes `ledger_state` rows for unobserved ones too."""
    # The toggle is on so the suppressed reason names which title anchored.
    await world.client.post("/api/auth/preferences", json={"show_model": True})
    # 1021 is seen and unrated, and its prior beats every rated title's.
    await world.db.execute(
        """
        INSERT INTO ledger_state (user_id, title_id, s, sigma, cdf, tier, kind, observed)
        VALUES ($1, 1021, 9.0, 0.2, 0.99, 4, 'movie', false)
        """,
        world.patrick,
    )
    payload = await world.home()
    film = world.section(payload, "because_anchor", "movie")
    assert film is not None, payload["suppressed"]
    assert film["anchor"]["title_id"] == 1000, (
        "the highest-scoring SEEN row anchored the shelf, rated or not"
    )
    assert "Home Film 1021" not in film["title"], film["title"]

    # With nothing rated the reason names both predicates: they are fixed by different actions.
    await world.db.execute(
        "UPDATE ledger_state SET observed = false WHERE user_id = $1 AND kind = 'movie'",
        world.patrick,
    )
    after = await world.home()
    assert world.section(after, "because_anchor", "movie") is None
    reason = next(
        s["reason"] for s in after["suppressed"]
        if s["shelf"] == "because_anchor" and s["kind"] == "movie"
    )
    assert "seen" in reason and "rated" in reason, reason
    assert world.section(after, "because_anchor", "series") is not None, (
        "the series half is untouched — the suppression is per section"
    )


async def test_the_anchor_headline_names_the_tier_the_owner_assigned(world):
    """The latest `tier_edit` decides where a title renders, so "you put X in" must read it."""
    tier_set = shelves.DEFAULT_TIER_SET
    model_tier = await world.db.fetchval(
        "SELECT tier FROM ledger_state WHERE user_id = $1 AND title_id = 1000", world.patrick
    )
    assert tier_set[model_tier] == "A", "the fixture's anchor is fitted into A"
    # The other rated films are unobserved so 1000 stays the only candidate.
    await world.db.execute(
        "UPDATE ledger_state SET observed = false "
        "WHERE user_id = $1 AND kind = 'movie' AND title_id <> 1000",
        world.patrick,
    )

    await world.db.execute(
        "INSERT INTO tier_edit (user_id, title_id, tier, via) VALUES ($1, 1000, 0, 'drag_drop')",
        world.patrick,
    )
    film = world.section(await world.home(), "because_anchor", "movie")
    assert film["anchor"]["tier"] == "F", "Home is still naming the tier the model fitted"
    assert film["title"] == "Because you put Home Film 1000 in F", film["title"]

    # After the refit too: `tier_edit` is append-only, so the sentence is stable by construction.
    await world.db.execute(
        "DELETE FROM user_title WHERE user_id = $1 AND title_id BETWEEN 1001 AND 1099",
        world.patrick,
    )
    report = await refit.refit_user(world.db, user_id=world.patrick, kind="movie", hp=DEFAULTS)
    assert report.fitted, report.as_dict()
    after = world.section(await world.home(), "because_anchor", "movie")
    assert after is not None, "the refit suppressed shelf 1"
    assert after["anchor"]["title_id"] == 1000
    assert after["title"] == "Because you put Home Film 1000 in F", after["title"]

    model_after = await world.db.fetchval(
        "SELECT tier FROM ledger_state WHERE user_id = $1 AND title_id = 1000", world.patrick
    )
    assert model_after != 0, (
        f"the refit fitted the anchor into F itself (tier {model_after}), so the headline would "
        "read F whichever column it took — this assertion has stopped being falsifiable"
    )


async def test_the_anchor_headline_names_the_tier_rank_renders_after_a_k_change(world):
    """Home and Rank must map a drop across a K change through the SAME helper."""
    labels = [f"T{i}" for i in range(12)]
    await world.db.execute(
        "INSERT INTO tier_edit (user_id, title_id, tier, via, n_levels) "
        "VALUES ($1, 1000, 6, 'drag_drop', 7)",
        world.patrick,
    )
    before = world.section(await world.home(), "because_anchor", "movie")
    assert before["anchor"]["tier"] == "S", "the drop is into the top tier of the default set"

    await world.db.execute(
        """
        INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set)
        VALUES ($1, 'movie', $2::float8[], $3::text[])
        ON CONFLICT (user_id, kind) DO UPDATE
            SET boundaries = EXCLUDED.boundaries, tier_set = EXCLUDED.tier_set
        """,
        world.patrick,
        [float(b) for b in model.initial_cutpoints(len(labels))],
        labels,
    )

    film = world.section(await world.home(), "because_anchor", "movie")
    assert film is not None, "growing the tier set suppressed shelf 1"
    assert film["anchor"]["tier"] == "T11", "Home is still reading the raw index of the old board"
    assert film["title"] == "Because you put Home Film 1000 in T11", film["title"]

    # And Rank agrees, which is the half neither surface could assert on its own.
    rows = {
        row.title_id: row
        for row in await read.items(world.db, user_id=world.patrick, kind="movie")
    }
    assert rows[1000].assigned_tier == 11, "the two surfaces disagree about the person's own drop"


def no_crowd_data(card: dict) -> bool:
    """`PosterCard.svelte`'s badge expression over one card; crowd ratings mean never "new"."""
    if (card.get("item_n") or 0) > 0:
        return False
    if card.get("e_source"):
        return card["e_source"] == "cold_tower"
    return card.get("item_n") == 0 or (
        card.get("item_n") is None and card.get("placement") == "cold_tower"
    )


async def test_shelf_cards_carry_e_source_outside_the_model_block(world):
    """`e_source` and `item_n` ship outside the gated `model` block: the badge is product."""
    # A real Backbone row under the warm gate, and the Cold Tower stamp 0008 puts on it.
    await world.db.execute("UPDATE title SET placement = 'cold_tower' WHERE id = 1001")
    await world.db.execute(
        "UPDATE title_prior SET e_source = 'backbone', item_n = 40 WHERE title_id = 1001"
    )

    for show_model in (False, True):
        await world.client.post("/api/auth/preferences", json={"show_model": show_model})
        payload = await world.home()
        card = next(
            c for c in world.section(payload, "because_anchor", "movie")["items"]
            if c["title_id"] == 1001
        )
        assert ("model" in card) is show_model, "the gate is decision 117's, and unchanged"
        assert card["e_source"] == "backbone", f"e_source did not survive show_model={show_model}"
        assert card["item_n"] == 40
        assert card["placement"] == "cold_tower", "the fixture's own premise"
        assert not no_crowd_data(card), (
            "a title with a Backbone row wears 'no crowd data yet' on Home"
        )
        # The falsifier: `placement` alone badges the same title.
        assert no_crowd_data({"placement": card["placement"]}), (
            "the placement-only fallback no longer reproduces the defect this test is for"
        )

    # And the honest case still badges: these cards have no Backbone row at all.
    cold = world.section(await world.home(), "new_in_library", "movie")["items"][0]
    assert cold["e_source"] == "cold_tower" and no_crowd_data(cold)


async def test_the_banner_is_exactly_the_seen_titles_with_no_live_verdict(world):
    """Compared as a SET; 1021's only verdict is superseded, so it is unrated."""
    expected = {
        r["id"] for r in await world.db.fetch(
            """
            SELECT t.id FROM user_title ut JOIN title t ON t.id = ut.title_id
             WHERE ut.user_id = $1 AND ut.state = 'seen'
               AND NOT EXISTS (SELECT 1 FROM verdict v WHERE v.user_id = $1
                                AND v.title_id = t.id AND v.superseded_by IS NULL)
            """,
            world.patrick,
        )
    }
    assert expected == {b + o for b in BASES for o in PENDING}

    payload = await world.home()
    banner = payload["banner"]
    assert banner["count"] == len(expected) == 6
    assert 1021 in expected, "the superseded-only row must count as unrated"
    assert set(banner["head_title_ids"]) <= expected
    # An unseen title never appears, and neither does a title with a live verdict.
    assert not {c["title_id"] for c in banner["named"]} & {1001, 1002, 1000}


async def test_the_banner_that_names_at_most_three_counts_the_rest(world):
    """At most three titles are named; beyond that "and N more"."""
    payload = await world.home()
    banner = payload["banner"]
    assert banner["count"] == 6
    assert len(banner["named"]) == 2
    assert len(banner["head_title_ids"]) == 2
    assert "and 4 more" in banner["copy"]["wide"], banner["copy"]["wide"]
    for card in banner["named"]:
        assert card["name"] in banner["copy"]["wide"]
        assert card["name"] in banner["copy"]["compact"]
    assert banner["copy"]["wide"].startswith("You watched ")
    assert banner["copy"]["compact"].startswith("Watched, not rated: ")


async def test_three_pending_titles_are_all_named(world):
    """At three, all three are named and none is counted."""
    for title_id in (1121, 1122, 1123):
        await world.db.execute(
            "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, $2, 1)",
            world.patrick, title_id,
        )
    banner = (await world.home())["banner"]
    assert banner["count"] == 3
    assert len(banner["named"]) == 3 == len(banner["head_title_ids"])
    assert " more" not in banner["copy"]["wide"]
    assert banner["copy"]["wide"].count(",") == 1 and " and " in banner["copy"]["wide"]


async def test_the_banner_cta_carries_exactly_the_named_titles_as_the_queue_head(world):
    """The server builds the link, so parsing it back falsifies the link, not only the copy."""
    from urllib.parse import parse_qs, urlsplit

    banner = (await world.home())["banner"]
    named = [c["title_id"] for c in banner["named"]]
    assert banner["head_title_ids"] == named, "the head must be the titles the copy NAMES"

    query = parse_qs(urlsplit(banner["cta"]["route"]).query)
    assert urlsplit(banner["cta"]["route"]).path == "/rate"
    # Decision 203: the link carries `head=` and nothing else.
    assert "mode" not in query, query
    assert list(query) == ["head"], query
    assert [int(t) for t in query["head"]] == named
    assert banner["cta"]["head"] == named
    assert parse_qs(urlsplit(banner["cta"]["api"]).query)["head"] == query["head"]


async def test_following_the_banners_own_link_serves_the_first_named_title(world):
    """`head` is a repeated integer parameter; a comma-joined one would be a 422."""
    banner = (await world.home())["banner"]
    served = await world.client.get(banner["cta"]["api"])
    assert served.status_code == 200, served.text
    card = served.json()["card"]
    assert card is not None and card["type"] == "sweep"
    assert card["title"]["id"] == banner["head_title_ids"][0], (
        "the queue served a different card than the banner named"
    )


async def test_the_banner_names_only_the_kinds_the_live_session_can_serve(world):
    """A films-only session must not be named a series the CTA cannot serve."""
    both = (await world.home())["banner"]
    assert both["count"] == 6
    assert "series" in {c["kind"] for c in both["named"]}, both["named"]

    opened = await world.client.post("/api/rate/session", json={"kinds": ["movie"]})
    assert opened.status_code == 200, opened.text
    films = (await world.home())["banner"]
    assert films["count"] == 3, "the count is the population the CTA can serve, not all of it"
    assert {c["kind"] for c in films["named"]} == {"movie"}
    assert "Home Series" not in films["copy"]["wide"], films["copy"]["wide"]

    # The stashed card is cleared first, so the pin is asserted, not `ensure_card`'s idempotency.
    await world.db.execute(
        "UPDATE rate_session SET current_card = NULL, card_token = NULL "
        "WHERE user_id = $1 AND ended_at IS NULL",
        world.patrick,
    )
    served = await world.client.get(films["cta"]["api"])
    assert served.status_code == 200, served.text
    assert served.json()["card"]["title"]["id"] == films["head_title_ids"][0], (
        "the queue served a different card than the banner named"
    )

    # Widen the session and the series is nameable again.
    widened = await world.client.post(
        "/api/rate/session", json={"kinds": ["movie", "series"]}
    )
    assert widened.status_code == 200, widened.text
    assert (await world.home())["banner"]["count"] == 6


async def test_rendering_home_writes_nothing(world):
    """Home is a read all the way down: the banner never writes `seen`."""
    before = (
        await world.db.fetchval("SELECT count(*) FROM user_title"),
        await world.db.fetchval("SELECT count(*) FROM verdict"),
        await world.db.fetchval("SELECT count(*) FROM ledger_state"),
    )
    await world.home()
    await world.home()
    after = (
        await world.db.fetchval("SELECT count(*) FROM user_title"),
        await world.db.fetchval("SELECT count(*) FROM verdict"),
        await world.db.fetchval("SELECT count(*) FROM ledger_state"),
    )
    assert before == after


async def test_every_shelf_returns_one_section_per_kind_and_no_shelf_has_items(world):
    """Asserted by SHAPE: no shelf object carries a top-level `items` list."""
    payload = await world.home()
    assert payload["kinds"] == ["movie", "series"]
    for shelf in payload["shelves"]:
        assert "items" not in shelf, f"{shelf['id']} has a shelf-level list — that is the merge"
        assert [s["kind"] for s in shelf["sections"]] == ["movie", "series"], shelf["id"]
        for section in shelf["sections"]:
            assert section["heading"] == {"movie": "Films", "series": "Series"}[section["kind"]]
            for card in section["items"]:
                assert card["kind"] == section["kind"]


async def test_a_kind_that_loses_a_merged_ranking_still_gets_its_own_section(world):
    """A merged top-12 would be all series; the film section must be the top twelve films."""
    payload = await world.home()
    top = await world.db.fetch(
        "SELECT title_id FROM user_score WHERE user_id = $1 AND kind = 'movie' "
        "AND bundle_version = $2 ORDER BY score DESC, title_id LIMIT 12",
        world.patrick, BUNDLE,
    )
    films = world.section(payload, "top_of_ledger", "movie")
    assert films is not None and films["items"], "a merged ranking returns zero films"
    assert [c["title_id"] for c in films["items"]] == [r["title_id"] for r in top]

    series = world.section(payload, "top_of_ledger", "series")
    assert min(score_of(c["title_id"]) for c in series["items"]) > max(
        score_of(c["title_id"]) for c in films["items"]
    ), "the fixture no longer reproduces the landmine"


async def test_selecting_one_kind_returns_one_section_and_selecting_none_is_a_422(world):
    """`?kind=` empty is a 422, never a silent "everything"."""
    only_films = await world.home(kinds=("movie",))
    assert only_films["kinds"] == ["movie"]
    for shelf in only_films["shelves"]:
        assert [s["kind"] for s in shelf["sections"]] == ["movie"]

    assert (await world.client.get("/api/home", params={"kind": ""})).status_code == 422
    assert (await world.client.get("/api/home")).status_code == 422
    with pytest.raises(ValueError, match="at least one kind"):
        await shelves.build_home(
            world.db, user=_Anon(world.patrick), kinds=[], bundle_version=BUNDLE,
            now_local=datetime.now(UTC),
        )


class _Anon:
    """The two attributes `build_home` reads off the session user, and nothing else."""

    def __init__(self, user_id: int, name: str = "patrick"):
        self.id, self.name, self.show_model = user_id, name, False


async def test_the_catalog_grid_may_interleave_the_two_kinds(world):
    """The catalog grid MAY interleave: the property under test is the ordering."""
    for offset in range(1, 7):
        for base in BASES:
            await world.db.execute(
                "INSERT INTO credit (title_id, person_id, department, job, source) "
                "VALUES ($1, 900, 'Directing', 'Director', 'tmdb') ON CONFLICT DO NOTHING",
                base + offset,
            )
    payload = await world.home(person_id=900)
    assert payload["mode"] == "grid"
    assert payload["shelves"] == []
    items = payload["catalog"]["items"]
    kinds = [item["kind"] for item in items]
    assert set(kinds) == {"movie", "series"}
    assert any(a != b for a, b in zip(kinds, kinds[1:], strict=False)), (
        "the year-ordered catalog listing came back partitioned — decision 18 permits the "
        "interleave here and only forbids it in a RANKING"
    )
    years = [item["year"] for item in items]
    assert years == sorted(years, reverse=True)

    broad = [item["kind"] for item in (await world.home(q="home"))["catalog"]["items"]]
    assert set(broad) == {"movie", "series"}
    assert any(a != b for a, b in zip(broad, broad[1:], strict=False)), (
        "equally good matches of the two kinds came back partitioned"
    )


async def test_a_person_filter_switches_home_into_the_grid_and_clearing_it_restores_shelves(world):
    """The server owns the mode: with a person filter the payload carries no shelves."""
    person = await world.home(person_id=900)
    assert person["mode"] == "grid"
    assert person["shelves"] == []
    assert {item["kind"] for item in person["catalog"]["items"]} == {"movie", "series"}, (
        "decision 18: with both toggles on, a filmography is complete across the partition"
    )

    restored = await world.home()
    assert restored["mode"] == "shelves"
    assert restored["catalog"] is None
    assert restored["shelves"], "clearing both the query and the person chip restores the shelves"


async def test_the_greeting_uses_the_household_clock_and_has_four_bands(world):
    """Four bands with each boundary named as a number."""
    payload = await world.home()
    assert payload["greeting"]["text"].endswith(", patrick")
    assert payload["greeting"]["tz"]

    at = datetime(2026, 8, 30, tzinfo=UTC)
    assert shelves.greeting(at.replace(hour=3), "p")["band"] == "up_late"
    assert shelves.greeting(at.replace(hour=9), "p")["band"] == "morning"
    assert shelves.greeting(at.replace(hour=14), "p")["band"] == "afternoon"
    assert shelves.greeting(at.replace(hour=21), "p")["text"] == "Good evening, p"


# Spread across the dial so one differs from the process's
# band at any hour; IANA names, as §2's `TZ` is one.
FAR_ZONES = (
    "Pacific/Kiritimati",   # UTC+14
    "Pacific/Midway",       # UTC-11
    "Asia/Tokyo",           # UTC+9
    "America/Anchorage",    # UTC-9
    "Pacific/Auckland",     # UTC+12/+13
)


def _zone_that_moves_the_band() -> str | None:
    """None when there is no tz database (Windows) or every candidate shares the host's band."""
    here = shelves.greeting(datetime.now(), "p")["band"]  # noqa: DTZ005 - the naive fallback
    for name in FAR_ZONES:
        try:
            zone = ZoneInfo(name)
        except Exception:  # noqa: BLE001 - no tz database is the case this is detecting
            continue
        if shelves.greeting(datetime.now(zone), "p")["band"] != here:
            return name
    return None


async def test_the_greeting_band_is_computed_in_the_household_zone(world, monkeypatch):
    """The payload names the configured zone; the band is that
    zone's, or the naive fallback where it cannot resolve."""
    zone = _zone_that_moves_the_band()
    resolved, zone = zone is not None, zone or FAR_ZONES[0]
    monkeypatch.setenv("TZ", zone)
    settings.cache_clear()
    try:
        payload = await world.home()
        assert payload["greeting"]["tz"] == zone, (
            "the payload names the zone the greeting was computed in, or the client cannot tell "
            "a household clock from a device clock"
        )
        # Bracketing the request: the two agree except across a band boundary crossed mid-request.
        def band_now() -> str:
            at = datetime.now(ZoneInfo(zone)) if resolved else datetime.now()  # noqa: DTZ005
            return shelves.greeting(at, "p")["band"]

        before = band_now()
        payload = await world.home()
        assert payload["greeting"]["band"] in {before, band_now()}, (
            f"the band is not the one {'TZ=' + zone if resolved else 'the fallback clock'} is in"
        )
        if resolved:
            assert shelves.greeting(datetime.now(), "p")["band"] != before, (  # noqa: DTZ005
                "the chosen zone no longer moves the band away from the process clock, so the "
                "assertion above proves nothing — widen FAR_ZONES"
            )
    finally:
        settings.cache_clear()


# Every key carrying a number about THIS VIEWER's model,
# walked recursively. `e_source` is product, not in the set.
MODEL_KEYS = frozenset(
    {"model", "rail", "suppressed", "score", "cf", "sigma", "cdf", "s",
     "tier_index", "mine_cdf", "theirs_cdf", "pair_score"}
)


def model_keys_in(node, path="") -> list[str]:
    if isinstance(node, dict):
        found = []
        for key, value in node.items():
            if key in MODEL_KEYS:
                found.append(f"{path}.{key}")
            found += model_keys_in(value, f"{path}.{key}")
        return found
    if isinstance(node, list):
        return [k for i, item in enumerate(node) for k in model_keys_in(item, f"{path}[{i}]")]
    return []


async def test_with_the_toggle_off_no_model_annotation_is_in_the_payload(world):
    """ABSENT, not hidden: a number removed by CSS is still on the wire."""
    assert await world.db.fetchval(
        "SELECT show_model FROM app_user WHERE id = $1", world.patrick
    ) is False, "decision 117: default off"

    payload = await world.home()
    assert model_keys_in(payload) == []
    # No route emits `rail` any more; the drawer reads `/api/model-log`, gated at the route.
    assert "suppressed" not in payload
    for shelf in payload["shelves"]:
        for section in shelf["sections"]:
            assert section["items"], shelf["id"]
            for card in section["items"]:
                assert "model" not in card
                # Rank, the seen dot and the settled tier are what a card IS, not model annotations.
                assert card["rank"] >= 1
                assert "seen" in card and "tier" in card


async def test_the_gated_model_block_reads_the_fold_ins_rho_against_the_bundles_own_figures(
    world,
):
    """The rho travels with the bundle's figures, a tie inside
    §0's noise floor reads as a tie, and all of it is gated."""
    from spielplan.models.artifacts import ColdEval

    yardstick = ColdEval(
        cold=0.35225, ceiling=0.39193, hybrid=0.37, delta=0.0191, ci95=(0.0043, 0.0339),
        n_test=1876,
    )
    # One rho clear of the floor, one inside it.
    for kind, rho in (("movie", 0.41), ("series", 0.355)):
        assert await world.db.fetchval(
            "UPDATE user_vector SET cv_rho = $3 WHERE user_id = $1 AND kind = $2 "
            "AND purpose = 'foldin' RETURNING cv_rho",
            world.patrick, kind, rho,
        ) == pytest.approx(rho), "the world no longer seeds a fitted profile for this kind"

    payload = await shelves.build_home(
        world.db, user=_Anon(world.patrick), kinds=["movie", "series"], bundle_version=BUNDLE,
        now_local=datetime.now(UTC), cold_eval=yardstick,
    )
    fit = {row["kind"]: row for row in payload["model"]["fit"]}
    assert set(fit) == {"movie", "series"}
    for row in fit.values():
        assert (row["cold"], row["ceiling"]) == (0.35225, 0.39193)
        assert row["ci95"] == [0.0043, 0.0339] and row["n_test"] == 1876
        assert row["noise_floor"] == DEFAULTS.rho_noise_floor

    assert fit["movie"]["cv_rho"] == pytest.approx(0.41, abs=1e-6)
    assert fit["movie"]["reads"] == "above cold"
    assert fit["movie"]["vs_cold"] == pytest.approx(0.0578, abs=1e-4)
    assert fit["movie"]["vs_ceiling"] == pytest.approx(0.0181, abs=1e-4)

    assert fit["series"]["cv_rho"] == pytest.approx(0.355, abs=1e-6)
    assert fit["series"]["reads"] == "tie", (
        "0.355 is 0.00275 above the corpus's cold path and §0 calls anything under 0.008 a tie; "
        "reporting it as a win is the comparison this whole block exists to make honest"
    )

    # The gate, asserted by walking the whole payload.
    assert model_keys_in(rail.redact(payload, show_model=False)) == []
    assert "fit" not in rail.redact(payload, show_model=False).get("model", {})


async def test_with_no_bundle_reference_the_rho_is_not_printed_at_all(world):
    """With no reference, silence rather than a bare rho."""
    payload = await shelves.build_home(
        world.db, user=_Anon(world.patrick), kinds=["movie"], bundle_version=BUNDLE,
        now_local=datetime.now(UTC),
    )
    assert payload["model"]["fit"] is None
    assert payload["model"]["sections_ms"], "the block's other annotation is untouched"


async def test_turning_the_toggle_on_reveals_the_numbers_for_that_user_only(world):
    """Decision 117: "one global per user … turning it on reveals them for that user only"."""
    rail.record(
        kind="ledger_incremental", user_id=world.patrick,
        line=rail.verdict_line("patrick", "Home Film 1000", "liked", refit_ms=31.0),
    )
    on = await world.client.post("/api/auth/preferences", json={"show_model": True})
    assert on.json() == {"ok": True, "show_model": True}

    payload = await world.home()
    # The events come from `/api/model-log`; Home carries no copy.
    assert "rail" not in payload, "one drawer, not one per route"
    events = (await world.client.get("/api/model-log")).json()["events"]
    assert events[0]["text"].startswith("verdict(patrick, Home Film 1000) = liked")
    card = payload["shelves"][0]["sections"][0]["items"][0]
    assert card["model"]["beta"] == pytest.approx(FITTED_BETA, abs=1e-6)
    assert card["model"]["b"] is not None and card["model"]["gate"] is not None

    # A second account, signed in separately, is unchanged: the preference is per user.
    jenny = await world.sign_in_jenny()
    hers = await jenny.get("/api/home", params=[("kind", "movie"), ("kind", "series")])
    assert hers.status_code == 200, hers.text
    assert model_keys_in(hers.json()) == [], "one user's toggle must not open another's rail"
    assert (await jenny.get("/api/model-log")).json() == {
        "show_model": False,
        "hint": "turn on 'show the model' in the account menu to see the model log",
    }


async def test_the_title_card_model_line_is_absent_with_the_toggle_off_and_present_with_it_on(world):
    """Decision 486: the title card's model line is gated by the toggle too."""
    assert await world.db.fetchval(
        "SELECT show_model FROM app_user WHERE id = $1", world.patrick
    ) is False
    off = await world.client.get("/api/titles/1000")
    assert off.status_code == 200
    assert "model_line" not in off.json(), "the model line reached a member with the switch off"

    await world.client.post("/api/auth/preferences", json={"show_model": True})
    on = await world.client.get("/api/titles/1000")
    assert on.status_code == 200
    assert "model_line" in on.json(), "the switch is on and the model line is still absent"


async def test_the_model_log_route_omits_the_events_key_when_the_toggle_is_off(world):
    """§6.7: "A per-user toggle (default off) reveals an ephemeral log (last ~15 events)"."""
    for i in range(20):
        rail.record(
            kind="verdict", user_id=world.patrick,
            line=rail.verdict_line("patrick", f"Home Film {1000 + i}", "liked", refit_ms=12.0),
        )
    off = (await world.client.get("/api/model-log")).json()
    assert off["show_model"] is False
    assert "events" not in off

    await world.client.post("/api/auth/preferences", json={"show_model": True})
    on = (await world.client.get("/api/model-log")).json()
    assert on["show_model"] is True
    assert len(on["events"]) == rail.RAIL_LIMIT == 15, "§6.7 caps the rail at ~15 events"
    assert on["kinds"] == ["verdict"]
    assert on["events"][0]["at"] >= on["events"][-1]["at"], "newest first"


async def test_the_model_log_refuses_a_limit_above_the_buffer(world):
    """The route's ceiling IS the 15-deep buffer; refused rather than silently truncated."""
    await world.client.post("/api/auth/preferences", json={"show_model": True})
    for i in range(rail.RAIL_LIMIT * 2):
        rail.record(kind="verdict", user_id=world.patrick, line=f"verdict(patrick, {i}) = liked")

    refused = await world.client.get("/api/model-log", params={"limit": rail.RAIL_LIMIT + 1})
    assert refused.status_code == 422, refused.text
    assert (await world.client.get("/api/model-log", params={"limit": 50})).status_code == 422
    assert (await world.client.get("/api/model-log", params={"limit": 0})).status_code == 422

    at_the_edge = await world.client.get(
        "/api/model-log", params={"limit": rail.RAIL_LIMIT}
    )
    assert at_the_edge.status_code == 200
    assert len(at_the_edge.json()["events"]) == rail.RAIL_LIMIT


async def test_recent_caps_before_it_merges_the_two_deques(world):
    """Two deques are capped before merging, or the response exceeds the buffer."""
    rail.forget()
    for i in range(rail.RAIL_LIMIT):
        rail.record(kind="verdict", user_id=world.patrick, line=f"verdict(patrick, {i}) = liked")
        rail.record(kind="ledger_refit", line=rail.refit_line("movie", n_titles=i, seconds=0.1))

    assert len(rail.recent(user_id=world.patrick, limit=50)) == rail.RAIL_LIMIT
    assert len(rail.recent(user_id=world.patrick)) == rail.RAIL_LIMIT
    assert len(rail.recent(user_id=world.patrick, limit=4)) == 4, "a smaller ask is still honoured"
    assert rail.recent(user_id=world.patrick, limit=0) == [], (
        "an empty ask must be empty, not the whole buffer — `[-0:]` is the whole list"
    )
    # Ids come from one counter, so newest-first survives the earlier slice.
    events = rail.recent(user_id=world.patrick, limit=50)
    assert [e["id"] for e in events] == sorted((e["id"] for e in events), reverse=True)
    assert {e["scope"] for e in events} == {"you", "household"}, (
        "capping each deque first must not drop one of them entirely"
    )
    rail.forget()


async def test_the_rail_narrates_a_model_write_in_one_human_readable_line(world):
    """Rendered at write time, so the rail shows what the model believed when it acted."""
    assert rail.verdict_line("jenny", "Heat", "liked", refit_ms=31.0) == (
        "verdict(jenny, Heat) = liked → ordered-logit arm, incremental refit 31 ms"
    )
    assert rail.tier_edit_line("Drive", "A", via="drag_drop", neighbour_duels=2) == (
        "tier_edit(Drive → A, via=drag_drop) + 2 margin-less duels vs new neighbours"
    )
    assert rail.session_answer_line("p", 4, "A") == "session_answer(p, pair 4) = A — pool-centred tilt"
    assert rail.parse_line("has(robots)", 0) == "parse → predicate has(robots) · 0 survivors → flywheel"

    # A household-wide write has no observation row, and explains Home changing overnight.
    rail.record(kind="ledger_refit",
                line=rail.refit_line("movie", n_titles=900, seconds=0.31, rho=0.42))
    await world.client.post("/api/auth/preferences", json={"show_model": True})
    events = (await world.client.get("/api/model-log")).json()["events"]
    assert events[0]["scope"] == "household"
    assert events[0]["text"].startswith("ledger_refit(movie) = 900 titles")

    with pytest.raises(rail.RailError):
        rail.record(kind="not-a-model-write", line="x")
    with pytest.raises(rail.RailError):
        rail.record(kind="verdict", line="   ")


def test_a_title_name_too_long_for_the_rail_is_elided_rather_than_refused():
    """A line composed after a durable write must never raise, so long names are elided."""
    name = ("The Assassination of Jesse James by the Coward Robert Ford " * 6)[:300]
    assert len(name) == 300, len(name)

    line = rail.duel_line(name, name, "TIE", context="tier_queue", selection="uniform_holdout")
    assert len(line) <= rail.MAX_LINE
    assert rail.record(kind="duel", user_id=-1, line=line) > 0, "the write must not refuse"
    edit = rail.tier_edit_line(name, "A", via="drag_drop", neighbour_duels=2)
    assert len(edit) <= rail.MAX_LINE
    assert rail.record(kind="tier_edit", user_id=-1, line=edit) > 0
    # The longest `AccountName` allows puts the threshold inside the names households type.
    member = "Grandma's iPad in the living room and the one in the kitchen :-)"
    assert len(member) == 64, len(member)
    spoken = rail.verdict_line(member, name, "disliked", refit_ms=31.4)
    assert len(spoken) <= rail.MAX_LINE, len(spoken)
    assert rail.record(kind="verdict", user_id=-1, line=spoken) > 0
    rail.forget(user_id=-1)

    # Elided, not emptied: the line still names each title once.
    assert name[:40] in line and line.count("…") == 2
    assert line.endswith("uniform-random, held out")
    short = rail.tier_edit_line("Drive", "A", via="drag_drop", neighbour_duels=2)
    assert "…" not in short and "Drive" in short

    # The bound holds for the longest line either renderer can compose; the tier label bound is imported.
    from spielplan.rank import tiers

    absurd = "x" * 4000
    for context in ("profile_battle", "tier_queue", "tier_insert"):
        for outcome in ("A", "B", "TIE"):
            for arm in rail.ARM_PHRASES:
                built = rail.duel_line(absurd, absurd, outcome, context=context, selection=arm)
                assert len(built) <= rail.MAX_LINE, (context, outcome, arm, len(built))
    widest = rail.tier_edit_line(
        absurd, "L" * tiers.MAX_LABEL, via="drag_drop", neighbour_duels=999
    )
    assert len(widest) <= rail.MAX_LINE, len(widest)
    # The verdict's chrome is bounded by `VERDICT_LABELS` and the millisecond count.
    for label in ("liked", "fine", "disliked"):
        for refit_ms in (None, 0.4, 31.4, 123456.7):
            said = rail.verdict_line(absurd, absurd, label, refit_ms=refit_ms)
            assert len(said) <= rail.MAX_LINE, (label, refit_ms, len(said))

    # Refusals that signal a CALLER error stay refusals.
    with pytest.raises(rail.RailError):
        rail.record(kind="duel", line="")
    with pytest.raises(rail.RailError):
        rail.record(kind="tier_drag", line=line)
    with pytest.raises(rail.RailError):
        rail.duel_line("a", "b", "A", context="tier_queue", selection="clairvoyance")


def test_the_gate_removes_gated_keys_at_every_depth():
    """A nested annotation must not survive because it was three levels down."""
    payload = {"a": 1, "model": {"b": 2}, "rows": [{"model": {"c": 3}, "name": "x"}]}
    assert rail.redact(payload, show_model=True) == payload
    assert rail.redact(payload, show_model=False) == {"a": 1, "rows": [{"name": "x"}]}


async def test_a_profile_with_no_verdicts_gets_the_seed_route_not_a_meaningless_ranking(world):
    """Zero verdicts: the catalog grid plus a route into the seed queue; `new_in_library` survives."""
    await world.db.execute("DELETE FROM verdict WHERE user_id = $1", world.patrick)
    payload = await world.home()
    assert payload["verdict_count"] == 0
    assert payload["degraded"]["state"] == "zero_verdicts"
    assert payload["degraded"]["cta"]["route"] == "/rate"
    assert [s["id"] for s in payload["shelves"]] == ["new_in_library"]


async def test_a_bundle_less_app_says_so_instead_of_erroring(app, db):
    """§3.1: a bundle-less app renders an explicit state, not an error."""
    client = app()
    await client.post("/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"})
    response = await client.get("/api/home", params=[("kind", "movie"), ("kind", "series")])
    assert response.status_code == 200
    payload = response.json()
    assert payload["degraded"]["state"] == "no_bundle"
    assert payload["shelves"] == []
    assert payload["banner"] is None


def test_the_name_list_copy_matches_proposal_21():
    assert shelves._name_list(["A"], 1) == "A"
    assert shelves._name_list(["A", "B"], 2) == "A and B"
    assert shelves._name_list(["A", "B", "C"], 3) == "A, B and C"
    assert shelves._name_list(["A", "B"], 7) == "A, B and 5 more"


async def test_the_term_reader_keeps_the_two_tiers_distinguishable(world):
    """A term in both tiers is named ONCE, neither upgraded nor downgraded."""
    await world.db.execute(
        "INSERT INTO dna_projected (title_id, version, term, facet, weight, via) "
        "VALUES (1001, $1, 'obsession', 'themes', 0.9, 'keyword')",
        VOCAB,
    )
    terms = await why_mod.terms_for(world.db, 1001, version=VOCAB)
    obsession = [t for t in terms if t.term == "obsession"]
    assert len(obsession) == 1, "a term in both tiers must be named once"
    assert obsession[0].tier == "extracted", "the quote-verified tier wins the label"

    await world.db.execute("DELETE FROM dna_tag WHERE title_id = 1001 AND term = 'obsession'")
    again = await why_mod.terms_for(world.db, 1001, version=VOCAB)
    assert [t.tier for t in again if t.term == "obsession"] == ["projected"]


async def test_an_inferred_term_never_leads_the_why_lines_term_pool(world):
    """Through the reader Home calls: an 8-source projection still ranks below a salience-2 quote."""
    for base in BASES:
        terms = await why_mod.terms_for(world.db, base + ANCHOR, version=VOCAB)
        assert [t.term for t in terms] == ["obsession", "morally-grey", "period"], terms
        assert terms[0].tier == "extracted", "an inferred term is named first"
        assert [t.tier for t in terms] == ["extracted", "extracted", "projected"]


async def _live_row_count(db) -> int:
    """`count(*)` over live tables, not `pg_stat_user_tables`, which flushes asynchronously."""
    return int(
        await db.fetchval(
            """
            SELECT coalesce(sum(
                (xpath(
                    '/row/c/text()',
                    query_to_xml(format('SELECT count(*) AS c FROM %I.%I', schemaname, tablename),
                                 false, true, '')
                ))[1]::text::bigint
            ), 0)
            FROM pg_tables WHERE schemaname = 'public'
            """
        )
        or 0
    )


async def test_the_rail_is_ephemeral_and_reaches_no_table(world):
    """§6.7: "never persisted". Recording writes nothing, and the buffer does not survive the process."""
    before = await _live_row_count(world.db)
    rail.record(
        kind="ledger_refit", user_id=world.patrick,
        line=rail.refit_line("movie", n_titles=900, seconds=0.31, rho=0.42),
    )
    after = await _live_row_count(world.db)
    assert after == before, "recording a rail event inserted a row somewhere"

    assert await world.db.fetchval("SELECT to_regclass('public.model_event')") is None, (
        "the rail must not have a table; §6.7 says never persisted"
    )

    # And it is genuinely gone on restart.
    assert rail.recent(user_id=world.patrick), "the event is readable while the process lives"
    rail.forget()
    assert rail.recent(user_id=world.patrick) == []


async def test_one_persons_rail_never_shows_another_persons_events(world):
    """A shared buffer leaking across accounts would reveal someone else's ratings."""
    rail.forget()
    rail.record(kind="verdict", user_id=world.patrick, line="verdict(patrick, A) = liked")
    rail.record(kind="verdict", user_id=world.patrick + 5000, line="verdict(other, B) = liked")
    rail.record(kind="ledger_refit", line=rail.refit_line("movie", n_titles=9, seconds=0.1))

    mine = rail.recent(user_id=world.patrick)
    assert [e["scope"] for e in mine] == ["household", "you"]
    assert not any("other" in e["text"] for e in mine)


def test_a_noisy_account_cannot_push_another_accounts_events_out_of_its_rail():
    """One deque per user, so a busy member cannot evict another's rail."""
    rail.forget()
    rail.record(kind="verdict", user_id=1, line="verdict(quiet, A) = liked")
    for i in range(rail.RAIL_LIMIT * 3):
        rail.record(kind="verdict", user_id=2, line=f"verdict(noisy, {i}) = liked")

    quiet = rail.recent(user_id=1)
    assert len(quiet) == 1 and "quiet" in quiet[0]["text"]
    assert len(rail.recent(user_id=2)) == rail.RAIL_LIMIT
    rail.forget()


def _rail_record_kinds() -> set[str]:
    """AST with one hop of resolution: some kinds reach `rail.record` through a variable."""
    root = Path(rail.__file__).resolve().parent.parent
    trees = [ast.parse(p.read_text(encoding="utf-8")) for p in sorted(root.rglob("*.py"))]

    def is_record(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "record"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "rail"
        )

    literal: set[str] = set()
    forwarded: set[tuple[str, str]] = set()
    for tree in trees:
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            params = {
                a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)
            }
            for call in ast.walk(fn):
                if not is_record(call):
                    continue
                for kw in call.keywords:
                    if kw.arg != "kind":
                        continue
                    if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        literal.add(kw.value.value)
                    elif isinstance(kw.value, ast.Name) and kw.value.id in params:
                        forwarded.add((fn.name, kw.value.id))

    for tree in trees:
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            called = (
                call.func.attr if isinstance(call.func, ast.Attribute)
                else getattr(call.func, "id", None)
            )
            for fn_name, param in forwarded:
                if called != fn_name:
                    continue
                for kw in call.keywords:
                    if kw.arg == param and isinstance(getattr(kw.value, "value", None), str):
                        literal.add(kw.value.value)
    return literal


def test_every_declared_rail_kind_has_a_producer_or_is_declared_pending():
    """Every declared kind has a producer or is in
    `AWAITING_PRODUCER`; a kind that is neither is forbidden."""
    produced = _rail_record_kinds()
    pending = set(rail.AWAITING_PRODUCER)

    assert pending == {
        "ledger_refit", "ledger_incremental", "foldin", "blend_weight", "placement",
        "bundle_swap", "reconcile",
    }, "decision 189 plus decision 263 name seven worker-side kinds; this tuple has drifted"
    assert not produced & pending, (
        f"{sorted(produced & pending)} is written somewhere and still declared pending"
    )
    assert produced | pending == set(rail.EVENT_KINDS), (
        f"unproduced and undeclared: {sorted(set(rail.EVENT_KINDS) - produced - pending)}; "
        f"written but not in EVENT_KINDS: {sorted(produced - set(rail.EVENT_KINDS))}"
    )


async def test_the_hidden_count_is_what_the_toggle_would_actually_reveal(db, world):
    """The count must be what the other toggle would actually reveal under the same filters."""
    # `person.id` comes from the corpus, not a sequence, so the fixture supplies one.
    person_id = 90210
    await db.execute("INSERT INTO person (id, name) VALUES ($1, 'Ada Cross-Kind')", person_id)
    credited = [MOVIES[0], MOVIES[1], SERIES[0]]
    for title_id in credited:
        await db.execute(
            "INSERT INTO credit (title_id, person_id, department, job) "
            "VALUES ($1, $2, 'Directing', 'Director')",
            title_id, person_id,
        )

    payload = await world.home(kinds=("movie",), person_id=person_id)
    catalog = payload["catalog"]
    assert catalog["total"] == 2, "two of this person's titles are films"
    assert catalog["hidden"].get("series", 0) == 1, (
        "turning Series on reveals this person's ONE series, so that is what the line must say"
    )

    # The count is exactly what the other toggle produces.
    both = await world.home(kinds=("movie", "series"), person_id=person_id)
    assert both["catalog"]["total"] == catalog["total"] + catalog["hidden"]["series"]


def _claiming_sections(payload, kind):
    """Every shipped section of one kind whose shelf takes part in the claim."""
    return [
        (shelf["id"], section)
        for shelf in payload["shelves"]
        if shelf["id"] in shelves.CLAIMING_SHELVES
        for section in shelf["sections"]
        if section["kind"] == kind
    ]


async def test_a_title_appears_on_at_most_one_shelf_per_kind(world):
    """Decision 475: no title twice per kind; "New in the library" is the stated exemption."""
    payload = await world.home()
    for kind in ("movie", "series"):
        sections = _claiming_sections(payload, kind)
        assert len(sections) == 5, [s for s, _ in sections]
        seen: dict[int, str] = {}
        for shelf_id, section in sections:
            for card in section["items"]:
                assert card["title_id"] not in seen, (
                    f"{card['title_id']} is on {seen.get(card['title_id'])} and on {shelf_id}"
                )
                seen[card["title_id"]] = shelf_id


async def test_your_top_picks_claims_first_and_keeps_its_whole_list(world):
    """"Your top picks" claims first; shelf 1 then names the decoys' pair by decision 513's weighting."""
    await world.db.execute(
        "UPDATE user_score SET score = 5.0 WHERE title_id = 1001 AND user_id = $1", world.patrick
    )
    payload = await world.home()
    top = world.section(payload, "top_of_ledger", "movie")
    assert [c["title_id"] for c in top["items"]][0] == 1001
    assert len(top["items"]) == shelves.SHELF_CAP
    first = world.section(payload, "because_anchor", "movie")
    assert 1001 not in {c["title_id"] for c in first["items"]}
    assert [c["title_id"] for c in first["items"]] == ids(1000, DECOYS)
    assert first["why"] == "shares obsession + period with it", first["why"]


async def test_the_floor_applies_after_the_claim_and_says_so(world):
    """The floor applies after the claim, and the reason names the claim."""
    await world.db.execute(
        "UPDATE user_score SET score = 5.0 WHERE title_id IN (1001, 1002, 1005) AND user_id = $1",
        world.patrick,
    )
    await world.client.post("/api/auth/preferences", json={"show_model": True})
    payload = await world.home()
    assert world.section(payload, "because_anchor", "movie") is None
    reason = next(
        s["reason"] for s in payload["suppressed"]
        if s["shelf"] == "because_anchor" and s["kind"] == "movie"
    )
    assert "once the shelves before it took theirs" in reason, reason
    assert world.section(payload, "because_anchor", "series") is not None


async def test_the_payload_keeps_the_tables_order_though_the_claim_runs_in_another(world):
    payload = await world.home()
    order = [shelf["id"] for shelf in payload["shelves"]]
    assert order == [s for s in shelves.SHELF_IDS if s in order]
    assert order[:2] == ["because_anchor", "top_of_ledger"], order
    assert shelves.CLAIM_ORDER[0] == "top_of_ledger"


async def test_shelf_one_prefers_titles_most_like_the_anchor_over_the_widest_pair(world):
    """Most-alike titles beat the widest pair; dotted ids with non-leaf labels guard against raw ids."""
    labelled = (
        ("era.wwii", "era", "World War II"),
        ("mood.tense", "mood", "on the edge of your seat"),
        ("themes.loss", "themes", "grief & loss"),
    )
    for term, facet, label in labelled:
        await world.db.execute(
            "INSERT INTO dna_term (version, term, facet, label) VALUES ($1, $2, $3, $4)",
            VOCAB, term, facet, label,
        )
        await _tag(world.db, 1000, term, facet, 3)
        for title_id in (1030, 1031, 1032):
            await _tag(world.db, title_id, term, facet, 2)
    for title_id in (1030, 1031, 1032):
        await _tag(world.db, title_id, "obsession", "themes", 2)

    section = world.section(await world.home(), "because_anchor", "movie")
    assert section is not None
    assert [c["title_id"] for c in section["items"]] == [1030, 1031, 1032]
    named = {t["term"] for t in section["why_terms"]}
    assert named < {"obsession", "era.wwii", "mood.tense", "themes.loss"}, named
    for term in section["why_terms"]:
        assert term["label"] in section["why"], (term, section["why"])
    assert not re.search(r"[a-z]+\.[a-z_]+", section["why"]), (
        f"a vocabulary id reached a member's why-line: {section['why']!r}"
    )
    assert "_" not in section["why"]


async def test_the_anchor_is_the_highest_tier_before_the_highest_s(world):
    """The board's tier comes before `s`, whose scale varies by coordinate."""
    # 1012 carries the anchor's two named terms, sits one tier above it, and has the lower `s`.
    await _tag(world.db, 1012, "obsession", "themes", 3)
    await _tag(world.db, 1012, "morally-grey", "character", 3)
    await world.db.execute(
        "UPDATE ledger_state SET tier = 5, s = 1.0 WHERE user_id = $1 AND title_id = 1012",
        world.patrick,
    )
    await world.db.execute(
        "UPDATE ledger_state SET s = 9.0 WHERE user_id = $1 AND title_id = 1000", world.patrick
    )
    section = world.section(await world.home(), "because_anchor", "movie")
    assert section["anchor"]["title_id"] == 1012, section["anchor"]
    assert section["title"] == "Because you liked Home Film 1012"


async def test_the_anchor_headline_says_what_the_person_did(world):
    """Decision 476: the headline says what the person did, never "you put" for an unplaced title."""
    await world.db.execute(
        "UPDATE ledger_state SET observed = false "
        "WHERE user_id = $1 AND kind = 'movie' AND title_id <> 1000",
        world.patrick,
    )
    liked = world.section(await world.home(), "because_anchor", "movie")
    assert liked["title"] == "Because you liked Home Film 1000"

    await world.db.execute(
        "UPDATE verdict SET value = 1 WHERE user_id = $1 AND title_id = 1000", world.patrick
    )
    neutral = world.section(await world.home(), "because_anchor", "movie")
    assert neutral["title"] == "More like Home Film 1000"

    await world.db.execute(
        "INSERT INTO tier_edit (user_id, title_id, tier, via) VALUES ($1, 1000, 5, 'drag_drop')",
        world.patrick,
    )
    placed = world.section(await world.home(), "because_anchor", "movie")
    assert placed["title"] == "Because you put Home Film 1000 in A+"


async def _reask(db, user_id: int, title_id: int, *, real: int, reask: int) -> None:
    """Every write supersedes the previous one, so only the re-ask's row is un-superseded."""
    async def write(value: int, *, reask_of: int | None) -> int:
        row = await db.fetchval(
            "INSERT INTO verdict (user_id, title_id, value, is_reask, reask_of) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            user_id, title_id, value, reask_of is not None, reask_of,
        )
        await db.execute(
            "UPDATE verdict SET superseded_by = $1 "
            "WHERE user_id = $2 AND title_id = $3 AND id <> $1 AND superseded_by IS NULL",
            row, user_id, title_id,
        )
        return row

    await write(reask, reask_of=await write(real, reask_of=None))


async def test_the_anchor_headline_reads_the_persons_verdict_not_the_reask(world):
    """A re-ask is not the person's verdict, so the headline must not read it."""
    await world.db.execute(
        "UPDATE ledger_state SET observed = false "
        "WHERE user_id = $1 AND kind = 'movie' AND title_id <> 1000",
        world.patrick,
    )
    await _reask(world.db, world.patrick, 1000, real=1, reask=2)
    fine = world.section(await world.home(), "because_anchor", "movie")
    assert fine["title"] == "More like Home Film 1000", fine["title"]

    await _reask(world.db, world.patrick, 1000, real=2, reask=1)
    liked = world.section(await world.home(), "because_anchor", "movie")
    assert liked["title"] == "Because you liked Home Film 1000", liked["title"]


async def test_the_anchor_tie_break_reads_the_persons_verdict_not_the_reask(world):
    """The tie-break reads the person's verdict too."""
    await _tag(world.db, 1012, "obsession", "themes", 3)
    await _tag(world.db, 1012, "morally-grey", "character", 3)
    # Same tier; 1000's own answer is "fine", its re-ask "liked".
    await _reask(world.db, world.patrick, 1000, real=1, reask=2)
    section = world.section(await world.home(), "because_anchor", "movie")
    assert section["anchor"]["title_id"] == 1012, section["anchor"]
    assert section["title"] == "Because you liked Home Film 1012"


async def test_the_anchor_is_the_highest_tier_the_board_shows_after_a_k_change(world):
    """After a K change the board shows drops via `rescale_level`; the anchor follows the board."""
    labels = [f"T{i}" for i in range(12)]
    await _tag(world.db, 1012, "obsession", "themes", 3)
    await _tag(world.db, 1012, "morally-grey", "character", 3)
    await world.db.execute(
        "INSERT INTO tier_edit (user_id, title_id, tier, via, n_levels) "
        "VALUES ($1, 1012, 6, 'drag_drop', 7)",
        world.patrick,
    )
    await world.db.execute(
        """
        INSERT INTO ledger_cutpoints (user_id, kind, boundaries, tier_set)
        VALUES ($1, 'movie', $2::float8[], $3::text[])
        ON CONFLICT (user_id, kind) DO UPDATE
            SET boundaries = EXCLUDED.boundaries, tier_set = EXCLUDED.tier_set
        """,
        world.patrick,
        [float(b) for b in model.initial_cutpoints(len(labels))],
        labels,
    )
    await world.db.execute(
        "UPDATE ledger_state SET tier = 9 WHERE user_id = $1 AND title_id = 1000", world.patrick
    )
    section = world.section(await world.home(), "because_anchor", "movie")
    assert section["anchor"]["title_id"] == 1012, section["anchor"]
    assert section["title"] == "Because you put Home Film 1012 in T11", section["title"]
    rows = {r.title_id: r for r in await read.items(world.db, user_id=world.patrick, kind="movie")}
    assert rows[1012].assigned_tier == 11, "Rank renders the drop somewhere else"


async def test_a_card_says_whether_its_letter_is_the_one_the_rank_board_shows(world):
    """The card says whether its letter is the board's: unrated
    titles are on no board, drops render at the drop."""
    # 1021 seen and unrated; 1015 fitted B and dropped to A+; 1012 fitted A, no drop.
    await world.db.execute(
        """
        INSERT INTO ledger_state (user_id, title_id, s, sigma, cdf, tier, kind, observed)
        VALUES ($1, 1021, 9.0, 0.2, 0.99, 6, 'movie', false)
        """,
        world.patrick,
    )
    await world.db.execute(
        "INSERT INTO tier_edit (user_id, title_id, tier, via) VALUES ($1, 1015, 5, 'drag_drop')",
        world.patrick,
    )
    section = world.section(await world.home(), "top_of_ledger", "movie")
    cards = {c["title_id"]: c for c in section["items"]}
    assert (cards[1021]["tier"], cards[1021]["on_board"], cards[1021]["board_tier"]) == (
        "S", False, None
    ), "a title nobody rated is not on the Rank board, whatever the fit says"
    assert (cards[1015]["tier"], cards[1015]["on_board"], cards[1015]["board_tier"]) == (
        "B", True, "A+"
    ), "the letter stays the fit's (decision 187) and the board's letter travels beside it"
    assert (cards[1012]["tier"], cards[1012]["on_board"], cards[1012]["board_tier"]) == (
        "A", True, "A"
    )
    board = {r.title_id: r for r in await read.items(world.db, user_id=world.patrick, kind="movie")}
    assert 1021 not in board and board[1015].assigned_tier == 5
    # Every card on every shelf carries the pair.
    for shelf in (await world.home())["shelves"]:
        for sec in shelf["sections"]:
            for card in sec["items"]:
                assert "on_board" in card and "board_tier" in card, (shelf["id"], card)


async def test_a_cold_placed_title_with_crowd_ratings_is_not_new(world):
    """The evaluation holdout serves crowd-rated rows from the Cold Tower; those are not "new"."""
    await world.db.execute("UPDATE title_prior SET item_n = 192061 WHERE title_id = 1008")
    payload = await world.home()
    fresh = world.section(payload, "new_in_library", "movie")
    assert 1008 not in {c["title_id"] for c in fresh["items"]}
    frontier = world.section(payload, "never_watched_term", "movie")
    card = next(c for c in frontier["items"] if c["title_id"] == 1008)
    assert card["e_source"] == "cold_tower" and card["item_n"] == 192061
    assert not no_crowd_data(card), "a title with 192,061 crowd ratings is badged 'new'"


# Decision 486's barred vocabulary, checked over every sentence a shelf renders.
_MODEL_WORDS = re.compile(
    r"§|β|σ|\bcos\b|\bcdf\b|\d\.\d\d|\bledger\b|fold-in|\bprior\b|Cold Tower|crowd data|"
    r"\bdecision \d|\bproposal \d|\bM[0-7]\b|\blabels\b",
    re.IGNORECASE,
)


async def test_no_shelf_sentence_carries_a_model_word_with_the_switch_off(world):
    """No model word and no `why_numbers` with Show the model off."""
    await world.db.execute(
        "DELETE FROM user_vector WHERE user_id = $1 AND purpose = 'foldin'", world.patrick
    )
    for payload in (await world.home(), await world.home(kinds=("series",))):
        assert payload["shelves"]
        for shelf in payload["shelves"]:
            for section in shelf["sections"]:
                assert "why_numbers" not in section, shelf["id"]
                for key in ("title", "why", "caption"):
                    text = section.get(key) or ""
                    assert not _MODEL_WORDS.search(text), f"{shelf['id']}.{key}: {text!r}"

    await world.db.execute("DELETE FROM verdict WHERE user_id = $1", world.patrick)
    degraded = (await world.home())["degraded"]
    assert degraded["state"] == "zero_verdicts"
    for key in ("headline", "why"):
        assert not _MODEL_WORDS.search(degraded[key]), degraded[key]


async def test_every_shelf_card_carries_its_original_title_and_language(world):
    """Decision 516: every card carries its original title and language, null where unknown."""
    await world.db.execute(
        "UPDATE title SET original_name = 'Wunderschön', original_language = 'de' WHERE id = ANY($1)",
        list(MOVIES),
    )
    payload = await world.home()
    cards = [c for shelf in payload["shelves"] for s in shelf["sections"] for c in s["items"]]
    assert cards, "no shelf cards at all - the fixture is not exercising the surface"
    for card in cards:
        assert "original_name" in card and "original_language" in card, card["title_id"]
        expected = ("Wunderschön", "de") if card["title_id"] in MOVIES else (None, None)
        assert (card["original_name"], card["original_language"]) == expected, card["title_id"]


async def test_home_counts_the_library_the_shelves_draw_on(world):
    """The count line names the owned library the shelves draw on."""
    await world.db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned) "
        "VALUES (1099, 'movie', 'Unowned', 2000, false)"
    )
    payload = await world.home(kinds=("movie",))
    assert payload["library"] == {"movie": len(MOVIES), "series": len(SERIES)}


# Four of Patrick's rated series share one mood term and become dislikes; his films keep ten likes.

GORE = ("gore", "mood", "gory")
GORE_CARRIERS = (1117, 1118, 1119, 1120)


async def _term(db, term: str, facet: str, label: str | None = None) -> None:
    await db.execute(
        "INSERT INTO dna_term (version, term, facet, label) VALUES ($1, $2, $3, $4)",
        VOCAB, term, facet, label,
    )


async def _verdict(db, user_id: int, title_id: int, value: int) -> None:
    """One live verdict, replacing the one `seed` wrote."""
    await db.execute("DELETE FROM verdict WHERE user_id = $1 AND title_id = $2", user_id, title_id)
    await db.execute(
        "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, $2, $3)",
        user_id, title_id, value,
    )


def _claiming_ids(payload, kind) -> set[int]:
    return {c["title_id"] for _, s in _claiming_sections(payload, kind) for c in s["items"]}


async def test_a_pattern_the_member_disliked_four_times_leaves_every_ranking_shelf(world):
    """Decision 512: four dislikes and no like leave the term off every ranking shelf."""
    term, facet, label = GORE
    await _term(world.db, term, facet, label)
    for title_id in GORE_CARRIERS:
        await _tag(world.db, title_id, term, facet, 2)
        await _verdict(world.db, world.patrick, title_id, 0)
    for title_id in (1001, 1005, 1008, 1024):
        await _tag(world.db, title_id, term, facet, 2)

    payload = await world.home(kinds=("movie",))
    assert payload["avoiding"] == {"labels": ["gory"], "runtime_max": 180}, payload["avoiding"]
    shown = _claiming_ids(payload, "movie")
    assert not shown & {1001, 1005, 1008, 1024}, shown
    first = world.section(payload, "because_anchor", "movie")
    # Three members and three decoys, one avoided: the members' pair is the only one left carried by three.
    assert [c["title_id"] for c in first["items"]] == [1002, 1003, 1004]
    frontier = world.section(payload, "never_watched_term", "movie")
    assert sorted(c["title_id"] for c in frontier["items"]) == [1009, 1010, 1011]
    fresh = world.section(payload, "new_in_library", "movie")
    assert 1008 in {c["title_id"] for c in fresh["items"]}, "an arrival is reported, not ranked"
    assert world.section(payload, "shared_sweet_spot", "movie") is None, (
        "the two decoys left are under the floor of three"
    )


async def test_a_pattern_the_member_also_liked_is_not_avoided(world):
    """One liked title carrying the term means it is not avoided."""
    term, facet, label = GORE
    await _term(world.db, term, facet, label)
    for title_id in (*GORE_CARRIERS, 1116):
        await _tag(world.db, title_id, term, facet, 2)
    for title_id in GORE_CARRIERS:
        await _verdict(world.db, world.patrick, title_id, 0)
    await _tag(world.db, 1001, term, facet, 2)

    payload = await world.home(kinds=("movie",))
    assert payload["avoiding"]["labels"] == [], payload["avoiding"]
    first = world.section(payload, "because_anchor", "movie")
    # 1001 is back, last: its extra term makes it a little less like the anchor.
    assert [c["title_id"] for c in first["items"]] == [1002, 1003, 1004, 1001]


async def test_the_shared_shelf_leaves_out_what_either_member_avoids(world):
    """The shared shelf leaves out what either member avoids."""
    term, facet, label = GORE
    await _term(world.db, term, facet, label)
    for title_id in (1012, 1013, 1014, 1015):
        await _tag(world.db, title_id, term, facet, 2)
        await _verdict(world.db, world.jenny, title_id, 0)
    await _tag(world.db, 1005, term, facet, 2)

    payload = await world.home(kinds=("movie",))
    assert payload["avoiding"]["labels"] == [], "Patrick liked all four; the pattern is Jenny's"
    sweet = world.section(payload, "shared_sweet_spot", "movie")
    assert sweet is None or 1005 not in {c["title_id"] for c in sweet["items"]}
    top = world.section(payload, "top_of_ledger", "movie")
    assert 1012 in {c["title_id"] for c in top["items"]}, "his own shelf keeps what he liked"


async def test_a_film_far_longer_than_anything_the_member_liked_leaves_the_shelves(world):
    """Past 30 minutes over the longest liked film and past three hours is left out; exactly 180 stays."""
    await world.db.execute("UPDATE title SET runtime_min = 250 WHERE id = 1021")
    await world.db.execute("UPDATE title SET runtime_min = 180 WHERE id = 1022")
    payload = await world.home(kinds=("movie",))
    assert payload["avoiding"] == {"labels": [], "runtime_max": 180}, payload["avoiding"]
    top = {c["title_id"] for c in world.section(payload, "top_of_ledger", "movie")["items"]}
    assert 1021 not in top and 1022 in top, top

    await world.db.execute("UPDATE title SET runtime_min = 230 WHERE id = 1012")
    payload = await world.home(kinds=("movie",))
    assert payload["avoiding"]["runtime_max"] == 260
    top = {c["title_id"] for c in world.section(payload, "top_of_ledger", "movie")["items"]}
    assert 1021 in top, top


async def test_shelf_one_weighs_a_shared_term_by_how_rare_it_is(world):
    """Decision 513: rare shared terms outweigh common ones."""
    for term, label in (("common", "common thread"), ("rare-a", "rare one"),
                        ("rare-b", "rare two")):
        await _term(world.db, term, "themes", label)
    for term in ("common", "rare-a", "rare-b"):
        await _tag(world.db, 1000, term, "themes", 2)
    for title_id in (1030, 1031, 1032):
        for term in ("rare-a", "rare-b"):
            await _tag(world.db, title_id, term, "themes", 2)
    for title_id in range(1033, 1050):
        await _tag(world.db, title_id, "common", "themes", 2)
        await _tag(world.db, title_id, "obsession", "themes", 2)

    section = world.section(await world.home(kinds=("movie",)), "because_anchor", "movie")
    assert [c["title_id"] for c in section["items"]] == [1030, 1031, 1032], section["items"]
    assert section["why"] == "shares rare one + rare two with it", section["why"]


async def _animated(db, *title_ids: int) -> None:
    for title_id in title_ids:
        await db.execute(
            "INSERT INTO title_genre (title_id, genre, source) VALUES ($1, 'Animation', 'tmdb')",
            title_id,
        )


async def test_shelf_one_keeps_to_the_anchors_form(world):
    """Decision 513: live-action anchors draw live action; animation is read through canonical Animation."""
    await _animated(world.db, *ids(1000, MEMBERS))
    live = world.section(await world.home(kinds=("movie",)), "because_anchor", "movie")
    assert [c["title_id"] for c in live["items"]] == ids(1000, DECOYS), (
        "the four animated members are not a live-action anchor's nearest"
    )

    await _animated(world.db, 1000)
    drawn = world.section(await world.home(kinds=("movie",)), "because_anchor", "movie")
    assert [c["title_id"] for c in drawn["items"]] == ids(1000, MEMBERS)


async def test_which_you_like_rests_on_three_liked_titles_not_on_the_ledger(world):
    """Decision 514: "which you like" needs three liked titles, not a Ledger CDF."""
    await _verdict(world.db, world.patrick, 1014, 1)
    await world.client.post("/api/auth/preferences", json={"show_model": True})
    payload = await world.home()
    assert world.section(payload, "never_watched_term", "movie") is None
    reason = next(
        s["reason"] for s in payload["suppressed"]
        if s["shelf"] == "never_watched_term" and s["kind"] == "movie"
    )
    assert "liked on 3 titles" in reason, reason
    assert world.section(payload, "never_watched_term", "series") is not None


async def test_the_frontier_names_a_neighbour_from_another_facet(world):
    """Decision 514: the neighbour must come from another facet."""
    await world.db.execute(
        "UPDATE dna_tag SET facet = 'visual' WHERE term = 'cosy' AND title_id < 1100"
    )
    payload = await world.home()
    assert world.section(payload, "never_watched_term", "movie") is None
    series = world.section(payload, "never_watched_term", "series")
    assert series["why"] == "close to cosy, which you like"


async def test_the_sweet_spot_ranks_the_two_members_on_one_scale(world):
    """Decision 477: scores are rank-standardised per member before averaging."""
    await world.db.execute(
        "UPDATE user_score SET score = score * 0.01 WHERE user_id = $1", world.jenny
    )
    for title_id, score in ((1005, 0.0043), (1006, 0.0044), (1007, 0.0061)):
        await world.db.execute(
            "UPDATE user_score SET score = $3 WHERE user_id = $1 AND title_id = $2",
            world.jenny, title_id, score,
        )
    sweet = world.section(await world.home(kinds=("movie",)), "shared_sweet_spot", "movie")
    assert [c["title_id"] for c in sweet["items"]] == [1007, 1005, 1006]


async def _titles(client, **params):
    query = [("kind", k) for k in params.pop("kinds", ("movie", "series"))]
    query += [(k, v) for k, v in params.items()]
    response = await client.get("/api/titles", params=query + [("limit", 200)])
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_catalogue_is_for_you_by_default_and_partitions_by_kind(world):
    """Decision 515: "for you" ranks by the member's own fit, films then series."""
    listing = await _titles(world.client)
    assert listing["sort"] == "for_you" and listing["for_you_available"] is True
    kinds = [item["kind"] for item in listing["items"]]
    assert kinds == ["movie"] * len(MOVIES) + ["series"] * len(SERIES)
    for kind in ("movie", "series"):
        run = [item["id"] for item in listing["items"] if item["kind"] == kind]
        assert run == sorted(run, key=lambda t: -score_of(t)), kind


async def test_the_catalogue_is_newest_first_until_the_members_own_ratings_rank_it(world):
    """"For you" needs the member's own ratings (β > 0);
    otherwise the year order, and the response says so."""
    jenny = await world.sign_in_jenny()
    listing = await _titles(jenny, sort="for_you")
    assert listing["sort"] == "newest"
    years = [item["year"] for item in listing["items"]]
    assert years == sorted(years, reverse=True)
    # So the grid offers no "For you" that answers newest.
    assert listing["for_you_available"] is False

    await world.db.execute("UPDATE user_vector SET blend_beta = 0 WHERE user_id = $1", world.patrick)
    crowd = await _titles(world.client)
    assert crowd["sort"] == "newest" and crowd["for_you_available"] is False


async def test_newest_and_a_search_keep_their_own_orders(world):
    """Year order stands when asked; a search is best match first, and `sort` says `match`."""
    newest = await _titles(world.client, sort="newest")
    assert newest["sort"] == "newest"
    years = [item["year"] for item in newest["items"]]
    assert years == sorted(years, reverse=True)
    searched = await _titles(world.client, q="Home Film 1001", sort="for_you")
    assert searched["sort"] == "match"
    assert searched["items"][0]["id"] == 1001
    refused = await world.client.get(
        "/api/titles", params=[("kind", "movie"), ("sort", "popular")]
    )
    assert refused.status_code == 422


async def _why(client, title_id: int):
    response = await client.get(f"/api/titles/{title_id}")
    assert response.status_code == 200, response.text
    return response.json()["why"]


async def test_the_title_card_says_which_liked_title_it_is_like(world):
    """One member-register sentence naming the liked title this one is like."""
    assert await _why(world.client, 1001) == (
        "Because you liked Home Film 1000 — they share morally-grey + obsession"
    )
    jenny = await world.sign_in_jenny()
    assert await _why(jenny, 1001) is None


async def test_the_title_card_says_nothing_of_a_title_seen_or_avoided(world):
    """Nothing is suggested about a seen or avoided title."""
    assert await _why(world.client, 1012) is None
    term, facet, label = GORE
    await _term(world.db, term, facet, label)
    for title_id in GORE_CARRIERS:
        await _tag(world.db, title_id, term, facet, 2)
        await _verdict(world.db, world.patrick, title_id, 0)
    await _tag(world.db, 1001, term, facet, 2)
    assert await _why(world.client, 1001) is None


async def test_the_title_card_names_a_top_pick_that_nothing_liked_explains(world):
    """With no liked title alike enough, a top-tenth title reads as Your top picks does."""
    assert await _why(world.client, 1030) is None
    await world.db.execute(
        "UPDATE user_score SET score = 5.0 WHERE user_id = $1 AND title_id = 1030", world.patrick
    )
    assert await _why(world.client, 1030) == "One of the ones we think you'll enjoy most"
