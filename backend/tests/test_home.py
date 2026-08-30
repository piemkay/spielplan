"""§6.0's M2 Home and §6.7's rail. Spec v2.1 §6.0, §6.7, §4.1 rules 1/2/5, §5.1, §5.2, §7.3;
decisions 18 and 117; proposals 20–33 and 150.

Four claims are under test, and the fixture below is built to break each one rather than to
look plausible:

* **the why-line** — three DECOY titles per kind carry `obsession` + `period` but not
  `morally-grey`. The prototype's rule ("admit on any two shared terms, then name the anchor's
  first two") admits them under a why-line they do not satisfy, so any implementation that
  labels a list instead of filtering by the terms it claims fails
  `test_a_card_carrying_only_one_of_the_two_named_terms_is_not_on_the_shelf`.
* **the banner** — one seen title's only verdict row is marked superseded. §4.2 is append-only
  and "has a verdict" means "has a LIVE verdict", so that title is unrated and belongs in the
  banner; a bare `NOT EXISTS (SELECT 1 FROM verdict …)` drops it and the count comes back 5.
* **the partition** — EVERY series outscores EVERY film. A merged top-12 is therefore all
  series and the Films section comes back empty, which is §4.1 rule 5's measured landmine (the
  unpartitioned crowd top-10 is 8/10 TV series) reproduced in miniature. The catalog grid, by
  contrast, is *supposed* to interleave, and one test asserts exactly that — decision 18's
  point is that the falsifiable property is the ORDERING, not the rendering.
* **the toggle** — β is seeded at 0.62, not 0.8, so a hard-coded constant in the why-line is
  visible; and the whole payload is walked for model keys with the toggle off, so a gate
  applied at four call sites and forgotten at the fifth fails.

Skipped without TEST_DATABASE_URL; see tests/conftest.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from spielplan.api import home as home_api
from spielplan.home import rail, shelves
from spielplan.home import why as why_mod

BUNDLE = "test-home-v1"
VOCAB = "v1"

MOVIES = tuple(range(1000, 1024))
SERIES = tuple(range(1100, 1124))
BASES = (1000, 1100)

# Per kind, by offset from the base id. Every group exists to make one assertion falsifiable.
ANCHOR = 0
MEMBERS = (1, 2, 3, 4)          # carry BOTH named terms — the expected shelf-1 membership
DECOYS = (5, 6, 7)              # carry obsession + period, NOT morally-grey — the falsifier
FRONTIER = (8, 9, 10, 11)       # carry `neon`, which no seen title carries; also the cold ones
LIKED = (12, 13, 14)            # seen, high CDF, carry `cosy` — the frontier's named neighbour
PENDING = (21, 22, 23)          # seen, no live verdict — the banner's population

# §6.0 shelf 5: strict `<`, and a NULL runtime is excluded because a shelf that claims a
# runtime bound must know the runtime. 1005/1105 sit exactly ON the threshold.
RUNTIME = {
    1: 95, 2: 100, 3: 105, 4: 90, 5: 110, 6: None, 7: 130,
    8: 140, 9: 150, 10: 160, 11: 170,
}
SERIES_RUNTIME = {
    1: 30, 2: 35, 3: 40, 4: 25, 5: 45, 6: None, 7: 60,
    8: 50, 9: 55, 10: 50, 11: 55,
}

# §5.1's blend weight, seeded away from the measured 0.8 so a why-line printing the constant
# rather than this profile's fitted number is visible in the copy.
FITTED_BETA = 0.62


def score_of(title_id: int) -> float:
    """EVERY series outscores EVERY film. §4.1 rule 5's landmine, in miniature."""
    if title_id == 1000:
        return 0.11
    if title_id == 1100:
        return 0.61
    if title_id < 1100:
        return 0.40 - 0.01 * (title_id - 1001)
    return 0.95 - 0.01 * (title_id - 1101)


def kind_of(title_id: int) -> str:
    return "movie" if title_id < 1100 else "series"


def ids(base: int, offsets) -> list[int]:
    return [base + o for o in offsets]


# --- the world -------------------------------------------------------------------------------


class World:
    def __init__(self, client, db, patrick, jenny, jenny_otp, app):
        self.client, self.db, self.app = client, db, app
        self.patrick, self.jenny, self.jenny_otp = patrick, jenny, jenny_otp

    async def sign_in_jenny(self):
        """§3.1: a member's account is locked to a password change at first login, so a client
        that only logs in with the OTP is not yet an `ActiveUser`."""
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
    """The extracted tier. §4.1: 'a tag without its quote is unfalsifiable' — so every one of
    these carries evidence, exactly as the importer requires."""
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
        await conn.execute(
            """
            INSERT INTO title (id, kind, name, year, runtime_min, is_owned, placement, placement_at)
            VALUES ($1, $2, $3, $4, $5, true, $6, $7)
            """,
            title_id, kind,
            f"Home {'Film' if kind == 'movie' else 'Series'} {title_id}",
            2000 + offset, runtime, placement,
            now - timedelta(days=100 - offset) if placement == "cold_tower" else None,
        )

    # §6.0 shelf 1's world: an anchor, four titles carrying BOTH of its terms, and three decoys
    # carrying only one of them plus a term the anchor also has.
    for base in BASES:
        await _tag(conn, base + ANCHOR, "obsession", "themes", 3)
        await _tag(conn, base + ANCHOR, "morally-grey", "character", 2)
        await _project(conn, base + ANCHOR, "period", "era", 0.6)
        for offset in MEMBERS:
            await _tag(conn, base + offset, "obsession", "themes", 2)
            await _tag(conn, base + offset, "morally-grey", "character", 2)
        for offset in DECOYS:
            await _tag(conn, base + offset, "obsession", "themes", 2)
            await _project(conn, base + offset, "period", "era", 0.5)
        for offset in FRONTIER:
            await _tag(conn, base + offset, "neon", "visual", 2)
        for offset in FRONTIER[:3]:
            await _tag(conn, base + offset, "cosy", "mood", 2)
        for offset in LIKED:
            await _tag(conn, base + offset, "cosy", "mood", 3)

    # Seen state. Patrick has 13 seen of each kind, which clears FRONTIER_MIN_SEEN = 10; Jenny
    # has 11, leaving 1001–1011 / 1101–1111 unseen by BOTH for the sweet-spot shelf.
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

    # Live verdicts on the anchor and offsets 12..20; 21..23 are seen and unrated — the banner.
    for base in BASES:
        for offset in [ANCHOR] + list(range(12, 21)):
            await conn.execute(
                "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, $2, 2)",
                patrick, base + offset,
            )
    # THE FALSIFIER for the banner's `superseded_by IS NULL`. §4.2 is append-only and a
    # re-rating supersedes rather than mutates, so a row marked superseded is not a live
    # verdict and its title is still unrated. The column is nullable and unconstrained across
    # titles — what the predicate asks is "is this row current", not "where did it go".
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

    # §6.0: "credits, each person tappable → filters the library to their filmography".
    await conn.execute("INSERT INTO person (id, name) VALUES (900, 'Ada Cross-Kind')")
    for title_id in (1001, 1101):
        await conn.execute(
            "INSERT INTO credit (title_id, person_id, department, job, source) "
            "VALUES ($1, 900, 'Directing', 'Director', 'tmdb')",
            title_id,
        )


def register_home(client) -> None:
    """Mount `spielplan.api.home.router` on the app the `app` fixture built.

    THIS IS A STAND-IN FOR ONE LINE IN `spielplan/app.py`:
    `app.include_router(home_api.router)`, next to the existing `library_api` registration.
    That file belongs to another agent this milestone, so the router is mounted here instead —
    on the REAL application object, behind the REAL auth dependencies and the real lifespan, so
    every assertion below is still made against the app rather than against a stub. Delete this
    helper the moment app.py registers the router.
    """
    application = client._transport.app
    if not any(getattr(route, "path", None) == "/api/home" for route in application.routes):
        application.include_router(home_api.router)


@pytest.fixture
async def world(app, db):
    client = app()
    register_home(client)
    created = await client.post(
        "/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"}
    )
    assert created.status_code == 201, created.text
    member = await client.post("/api/setup/members", json={"name": "jenny", "role": "member"})
    assert member.status_code == 201, member.text
    patrick = await db.fetchval("SELECT id FROM app_user WHERE name = 'patrick'")
    jenny = await db.fetchval("SELECT id FROM app_user WHERE name = 'jenny'")
    await seed(db, patrick=patrick, jenny=jenny)
    return World(client, db, patrick, jenny, member.json()["one_time_password"], app)


# --- library-rate-shelf-why-line --------------------------------------------------------------


async def test_every_section_carries_a_why_line_and_every_card_carries_every_named_term(world):
    """§6.0: "a shelf that cannot say why it exists doesn't ship"; proposal 24: "nor does one
    that says the wrong why … The why-line must name terms **every** item on the shelf carries".

    Walks the whole payload: every section has a non-empty why, and every term the why-line
    names with role `member` — plus every term the section reports as shared — is present in
    `dna_tagged` for EVERY card on that section, not only for the anchor.
    """
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
            # Every anchor_side term is the OTHER kind of claim (§6.4's named edge): it
            # describes the user's liked region, not the cards, and must be labelled as such.
            for term in section["why_terms"]:
                assert term["role"] in ("member", "anchor_side")
                assert term["tier"] in ("extracted", "projected")
    assert checked >= 6, f"only {checked} term claims were checkable — the fixture went quiet"


async def test_a_card_carrying_only_one_of_the_two_named_terms_is_not_on_the_shelf(world):
    """The prototype admits on "any two shared terms" and then names the anchor's first two,
    so a card can be shown under a reason it does not satisfy (proposal 24).

    The decoys carry `obsession` + `period`; the anchor carries `obsession`, `morally-grey` and
    `period`. Under the prototype's rule they are admitted. Under proposal 24's — the terms are
    chosen first and membership is "carries both" — they are not.
    """
    payload = await world.home()
    for base in BASES:
        section = world.section(payload, "because_anchor", kind_of(base))
        assert section is not None, f"shelf 1 missing for {kind_of(base)}"
        assert [c["title_id"] for c in section["items"]] == ids(base, MEMBERS)
        assert {t["term"] for t in section["why_terms"]} == {"morally-grey", "obsession"}
        assert not set(ids(base, DECOYS)) & {c["title_id"] for c in section["items"]}
        assert section["why"] == "shares morally-grey + obsession with it"
        assert section["title"] == f"Because you put Home {'Film' if base == 1000 else 'Series'} " \
                                  f"{base} in A"


async def test_the_ledger_shelf_names_the_beta_its_own_ranking_used(world):
    """§6.0 row 2's why-line prints β. Printing the measured constant 0.8 while this profile's
    fitted β is 0.62 is exactly the decorative why-line §6.0 forbids — the number would have
    had no part in the ordering the person is looking at."""
    payload = await world.home()
    for kind in ("movie", "series"):
        section = world.section(payload, "top_of_ledger", kind)
        assert section is not None
        assert f"β {FITTED_BETA:.2f}" in section["why"], section["why"]
        assert "0.80" not in section["why"]
        assert section["why_numbers"]["beta"] == pytest.approx(FITTED_BETA, abs=1e-6)
        assert "rewatches included" in section["why"]   # proposal 25's stated exception


async def test_the_school_night_shelf_names_the_threshold_its_cards_obey(world):
    """Proposal 27: "Under the Series partition this shelf restates itself as 'Episodes under 45
    minutes' … the thresholds (110 min film, 45 min episode) are constants, not copy."

    The prototype applied 110 to per-episode runtimes and swallowed the series catalog. Also
    checks the strict `<` (a title at exactly the threshold is not under it) and the NULL
    exclusion (a shelf claiming a runtime bound must know the runtime).
    """
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
        assert [c["title_id"] for c in section["items"]] == sorted(ids(base, MEMBERS))
        for card in section["items"]:
            assert card["runtime_min"] is not None
            assert card["runtime_min"] < threshold
        shown = {c["title_id"] for c in section["items"]}
        assert base + 5 not in shown, "a title at exactly the threshold is not *under* it"
        assert base + 6 not in shown, "a NULL runtime cannot satisfy a runtime claim"


async def test_a_shelf_that_cannot_justify_itself_is_absent_not_empty(world):
    """§6.0: absent, never present-and-empty. Proposal 28 puts the floor at three.

    Two of shelf 1's four members lose `morally-grey` and one decoy loses `period`, so NO pair
    of the anchor's terms covers three unseen owned films — and the shelf must disappear from
    `shelves` entirely rather than render short. The series half is untouched and still ships,
    which is the second half of the claim: the suppression is per section, not per shelf.

    With "show the model" on, `suppressed` names the count and the floor, so the absence is
    distinguishable from a bug.
    """
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
    """§6.0 row 6's why is "placed by the Cold Tower — no crowd data yet", and proposal 33 says
    what makes that checkable: `item_n`, §5.1's gate input.

    Title 1007 is `warm` with n_t = 4213 (gate 0.998). A shelf that selected on recency alone,
    or on `placement` without asking what the model actually has, would show it under a claim
    of no crowd data.
    """
    payload = await world.home()
    for base in BASES:
        section = world.section(payload, "new_in_library", kind_of(base))
        assert section is not None
        assert section["why"] == "placed by the Cold Tower — no crowd data yet"
        shown = [c["title_id"] for c in section["items"]]
        # Ordered by recency, newest first — the one shelf that is not score-ordered.
        assert shown == sorted(ids(base, FRONTIER), reverse=True)
        assert base + 7 not in shown, "a warm title with 4213 crowd ratings is not 'new'"
        for card in section["items"]:
            assert card["placement"] == "cold_tower"
            support = await world.db.fetchval(
                "SELECT item_n FROM title_prior WHERE title_id = $1", card["title_id"]
            )
            assert support == 0, f"{card['title_id']} claims no crowd data but has n_t={support}"


async def test_the_frontier_shelf_names_a_term_no_seen_title_carries(world):
    """§6.0 row 3 / §6.4: "unvisited region of DNA space next to what you like".

    "Never" is literal — zero coverage, not low coverage — and the neighbour is an
    `anchor_side` term, because the cards are unvisited by definition and cannot carry the term
    that describes the region they sit beside. §6.4: "Every connection is *nameable* — edges are
    DNA terms, never opaque similarity", and the edge is that neighbour, printed.
    """
    payload = await world.home()
    for base in BASES:
        section = world.section(payload, "never_watched_term", kind_of(base))
        assert section is not None
        assert section["title"] == "You've never watched anything neon"
        assert "sits beside cosy" in section["why"], section["why"]
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
    """§6.0 row 4 / §6.5: "the region both like — doubles as the couple's watch-now prior".

    Ordered by the PLAIN AVERAGE of the two scores, which is what §6.2 step 3 ranks the Tonight
    pool by ("nothing dominates averaging; dominance rules cost −0.012") — that shared
    arithmetic is what makes "doubles as the Tonight prior" true rather than decorative.
    """
    payload = await world.home()
    assert payload["partner"]["name"] == "jenny"
    for base in BASES:
        section = world.section(payload, "shared_sweet_spot", kind_of(base))
        assert section is not None
        assert section["title"] == "You and jenny both rate these highly"
        assert section["why"] == "the shared sweet spot — doubles as the Tonight prior"
        shown = [c["title_id"] for c in section["items"]]
        assert shown == sorted(shown, key=lambda t: -score_of(t))
        for title_id in shown:
            for user_id in (world.patrick, world.jenny):
                seen = await world.db.fetchval(
                    "SELECT count(*) FROM user_title WHERE user_id = $1 AND title_id = $2 "
                    "AND state = 'seen'",
                    user_id, title_id,
                )
                assert seen == 0, f"{title_id} is on a 'neither of you has seen these' shelf"


# --- library-rate-pending-verdicts-banner -----------------------------------------------------


async def test_the_banner_is_exactly_the_seen_titles_with_no_live_verdict(world):
    """§6.0 + §7.3. The population is computed independently in SQL and compared as a SET.

    Includes the row that falsifies a bare `NOT EXISTS (SELECT 1 FROM verdict …)`: title 1021's
    only verdict row is marked superseded, so it has no LIVE verdict and is unrated (§4.2 is
    append-only — a re-rating supersedes rather than mutates).
    """
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
    """Proposal 21: "At most three titles are named; beyond that the list reads '{title},
    {title} and N more'." Six pending, so two names and "and 4 more"."""
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
    """The other side of proposal 21's cap: at three, all three are named and none is counted."""
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
    """Proposal 150: "The CTA enters the §6.1 queue **with the named titles at its head** — a
    prompt that names titles and then presents a different one is worse than no prompt."

    The route is built by the SERVER, so the head cannot drift from the copy the server just
    rendered. Parsing the emitted link back is what falsifies the link rather than the copy.
    """
    from urllib.parse import parse_qs, urlsplit

    banner = (await world.home())["banner"]
    named = [c["title_id"] for c in banner["named"]]
    assert banner["head_title_ids"] == named, "the head must be the titles the copy NAMES"

    query = parse_qs(urlsplit(banner["cta"]["route"]).query)
    assert urlsplit(banner["cta"]["route"]).path == "/rate"
    assert query["mode"] == ["sweep"]
    assert [int(t) for t in query["head"]] == named
    assert banner["cta"]["head"] == named
    assert parse_qs(urlsplit(banner["cta"]["api"]).query)["head"] == query["head"]


async def test_following_the_banners_own_link_serves_the_first_named_title(world):
    """The other half of the coverage row: "its CTA opens the §6.1 queue with those titles at
    the head of the queue, not at whatever position the standing queue held".

    Follows the link the BANNER emitted rather than one this test built, so it falsifies the
    link and not only the copy. §6.1's queue is another module; this asserts the boundary
    between them — `GET /api/rate` takes `head` as a repeated integer parameter, and a
    comma-joined `head=1,2` would come back 422 here rather than silently ignored.
    """
    from spielplan.api import rate as rate_api

    register_home(world.client)
    application = world.client._transport.app
    if not any(getattr(r, "path", None) == "/api/rate" for r in application.routes):
        application.include_router(rate_api.router)

    banner = (await world.home())["banner"]
    served = await world.client.get(banner["cta"]["api"])
    assert served.status_code == 200, served.text
    card = served.json()["card"]
    assert card is not None and card["type"] == "sweep"
    assert card["title"]["id"] == banner["head_title_ids"][0], (
        "the queue served a different card than the banner named"
    )


async def test_rendering_home_writes_nothing(world):
    """Proposal 150: the banner "never writes `seen`". Home is a read, all the way down —
    §7.3's finish prompt is the surface that writes, and it is a different one."""
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


# --- library-rate-shelves-partition-both-kinds ------------------------------------------------


async def test_every_shelf_returns_one_section_per_kind_and_no_shelf_has_items(world):
    """§4.1 rule 5 as decision 18 reads it: a surface that RANKS "renders two headed sections
    and never one interleaved ranking".

    Asserted by SHAPE, not by inspection of an ordering: no shelf object carries an `items`
    key, so there is no top-level list a client could render as one row; and every card in a
    section carries that section's own kind.
    """
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
    """The measured landmine, in miniature: EVERY series outscores EVERY film, so a merged
    top-12 is 12/12 series and the Films section comes back empty.

    The film section must be exactly the top twelve films by `user_score.score`, in that order.
    """
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
    """Decision 18: two toggles, either or both active, never neither. `?kind=` is a validation
    error rather than a silent "everything", which is the unpartitioned query rule 5 forbids."""
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
    """The other half of decision 18, and the reason the partition claim is falsifiable at all:
    "A surface that merely **lists** in a kind-independent order — the catalog, sorted by year
    or title — may interleave freely."

    So the property under test is the ORDERING, not the rendering. This asserts the grid DOES
    interleave — an implementation that partitioned everything would fail here, and one that
    merged everything would fail the test above. Both cases are distinguished.
    """
    payload = await world.home(q="home")
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


# --- §6.0's mode switch -----------------------------------------------------------------------


async def test_a_person_filter_switches_home_into_the_grid_and_clearing_it_restores_shelves(world):
    """§6.0: "Search or an active person-filter switches Home into the catalog grid; clearing it
    returns the shelves." The server owns the mode, so the two states are mutually exclusive by
    construction — with one set, the payload carries no shelves to render."""
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
    """Proposal 22's four bands, evaluated server-side against §2's `TZ` so the band is the
    household clock rather than the device clock — and so it is assertable without a browser."""
    payload = await world.home()
    assert payload["greeting"]["text"].endswith(", patrick")
    assert payload["greeting"]["band"] in {"up_late", "morning", "afternoon", "evening"}
    assert payload["greeting"]["tz"]

    at = datetime(2026, 8, 30, tzinfo=UTC)
    assert shelves.greeting(at.replace(hour=3), "p")["band"] == "up_late"
    assert shelves.greeting(at.replace(hour=9), "p")["band"] == "morning"
    assert shelves.greeting(at.replace(hour=14), "p")["band"] == "afternoon"
    assert shelves.greeting(at.replace(hour=21), "p")["text"] == "Good evening, p"


# --- §6.7 / decision 117: the show-the-model gate ----------------------------------------------

# Every key that carries a number about THIS VIEWER's model. Walked recursively, so a builder
# that adds a seventh annotation under a new name is caught by the shape of the test rather
# than by someone remembering to extend a list of call sites.
MODEL_KEYS = frozenset(
    {"model", "rail", "suppressed", "score", "cf", "sigma", "cdf", "s", "e_source",
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
    """Decision 117: "It governs the rail and every inline annotation."

    ABSENT, not hidden. A number removed by CSS is still on the wire, in the network tab and in
    the service-worker cache, so the promise would be cosmetic. The whole payload is walked.
    """
    assert await world.db.fetchval(
        "SELECT show_model FROM app_user WHERE id = $1", world.patrick
    ) is False, "decision 117: default off"

    payload = await world.home()
    assert model_keys_in(payload) == []
    assert "rail" not in payload and "suppressed" not in payload
    for shelf in payload["shelves"]:
        for section in shelf["sections"]:
            assert section["items"], shelf["id"]
            for card in section["items"]:
                assert "model" not in card
                # …while proposal 29's chrome survives: rank, the seen dot and the settled tier
                # are what a shelf card IS, not an annotation about the model.
                assert card["rank"] >= 1
                assert "seen" in card and "tier" in card


async def test_turning_the_toggle_on_reveals_the_numbers_for_that_user_only(world):
    """Decision 117: "one global per user … turning it on reveals them for that user only"."""
    rail.record(
        kind="ledger_incremental", user_id=world.patrick,
        line=rail.verdict_line("patrick", "Home Film 1000", "liked", refit_ms=31.0),
    )
    on = await world.client.post("/api/auth/preferences", json={"show_model": True})
    assert on.json() == {"ok": True, "show_model": True}

    payload = await world.home()
    assert payload["rail"], "the rail is the whole point of the toggle"
    assert payload["rail"][0]["text"].startswith("verdict(patrick, Home Film 1000) = liked")
    card = payload["shelves"][0]["sections"][0]["items"][0]
    assert card["model"]["beta"] == pytest.approx(FITTED_BETA, abs=1e-6)
    assert card["model"]["b"] is not None and card["model"]["gate"] is not None

    # A second account, signed in separately, is unchanged — the preference is per user.
    jenny = await world.sign_in_jenny()
    hers = await jenny.get("/api/home", params=[("kind", "movie"), ("kind", "series")])
    assert hers.status_code == 200, hers.text
    assert model_keys_in(hers.json()) == [], "one user's toggle must not open another's rail"
    assert (await jenny.get("/api/model-log")).json() == {
        "show_model": False,
        "hint": "turn on 'show the model' in the account menu to see the model log",
    }


async def test_the_title_card_model_line_renders_with_the_toggle_off(world):
    """Proposal 19 and decision 117 both say so: "the title card's model line stays ungated".

    It is §6.0's M0 transparency promise and predates the §6.7 rail, so it must survive the
    default-off preference. Gating it would put the app's oldest promise behind a debug flag.
    """
    assert await world.db.fetchval(
        "SELECT show_model FROM app_user WHERE id = $1", world.patrick
    ) is False
    card = await world.client.get("/api/titles/1000")
    assert card.status_code == 200
    assert "model_line" in card.json(), "the model line is not the rail and is not gated"


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


async def test_the_rail_narrates_a_model_write_in_one_human_readable_line(world):
    """§6.7's four example lines, rendered at write time (0012's rule) so the rail shows what
    the model believed when it acted rather than a sentence recomposed from numbers that have
    since moved."""
    assert rail.verdict_line("jenny", "Heat", "liked", refit_ms=31.0) == (
        "verdict(jenny, Heat) = liked → ordered-logit arm, incremental refit 31 ms"
    )
    assert rail.tier_edit_line("Drive", "A", via="drag_drop", neighbour_duels=2) == (
        "tier_edit(Drive → A, via=drag_drop) + 2 margin-less duels vs new neighbours"
    )
    assert rail.session_answer_line("p", 4, "A") == "session_answer(p, pair 4) = A — pool-centred tilt"
    assert rail.parse_line("has(robots)", 0) == "parse → predicate has(robots) · 0 survivors → flywheel"

    # A household-wide write — a nightly refit has no observation row of its own (0012) — is
    # visible to every member, because it is what explains Home changing overnight.
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


def test_the_gate_removes_gated_keys_at_every_depth():
    """`redact` is the one place decision 117 is enforced, so it is tested on its own: a nested
    annotation must not survive because it was three levels down."""
    payload = {"a": 1, "model": {"b": 2}, "rows": [{"model": {"c": 3}, "name": "x"}]}
    assert rail.redact(payload, show_model=True) == payload
    assert rail.redact(payload, show_model=False) == {"a": 1, "rows": [{"name": "x"}]}


# --- degraded states --------------------------------------------------------------------------


async def test_a_profile_with_no_verdicts_gets_the_seed_route_not_a_meaningless_ranking(world):
    """Proposal 20: "Bundle imported, zero verdicts … tier badges, ledger weights and every
    score-ordered shelf are meaningless, so Home falls back to the catalog grid plus a route
    into the §6.1 seed-list queue."

    `new_in_library` is ordered by recency rather than by a ledger nobody has yet, so it
    survives — a reading of the phrase, stated rather than assumed.
    """
    await world.db.execute("DELETE FROM verdict WHERE user_id = $1", world.patrick)
    payload = await world.home()
    assert payload["verdict_count"] == 0
    assert payload["degraded"]["state"] == "zero_verdicts"
    assert payload["degraded"]["cta"]["route"] == "/rate?mode=sweep"
    assert [s["id"] for s in payload["shelves"]] == ["new_in_library"]


async def test_a_bundle_less_app_says_so_instead_of_erroring(app, db):
    """§3.1: a bundle-less app is a legal state and "artifact-dependent surfaces render an
    explicit 'no bundle imported' state instead of erroring"."""
    client = app()
    register_home(client)
    await client.post("/api/setup/admin", json={"name": "patrick", "password": "an-admin-password"})
    response = await client.get("/api/home", params=[("kind", "movie"), ("kind", "series")])
    assert response.status_code == 200
    payload = response.json()
    assert payload["degraded"]["state"] == "no_bundle"
    assert payload["shelves"] == []
    assert payload["banner"] is None


# --- the pieces, on their own ------------------------------------------------------------------


def test_the_name_list_copy_matches_proposal_21():
    assert shelves._name_list(["A"], 1) == "A"
    assert shelves._name_list(["A", "B"], 2) == "A and B"
    assert shelves._name_list(["A", "B", "C"], 3) == "A, B and C"
    assert shelves._name_list(["A", "B"], 7) == "A, B and 5 more"


async def test_the_term_reader_keeps_the_two_tiers_distinguishable(world):
    """§4.1 rule 1: 14,181 (title,term) pairs exist in both tiers and "must stay
    distinguishable"; a term present in both is named ONCE and never upgraded to `extracted`
    by accident — nor downgraded from it."""
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


async def test_the_rail_is_ephemeral_and_reaches_no_table(world):
    """§6.7: "an **ephemeral** log (last ~15 events, **never persisted**)".

    This project shipped the rail as a `model_event` table first, on the argument that a nightly
    refit and a Cold Tower placement are model writes with no row of their own, so a rail derived
    from the observation tables would omit exactly what a person turns the rail on to see. The
    argument is sound; it is also not what the spec says, and "never persisted" is a normative
    sentence about a debugging instrument rather than a gap to be improved on.

    Two assertions, because "we deleted the migration" is not the property. The property is that
    recording an event writes nothing anywhere and that the buffer does not survive the process.
    """
    before = await world.db.fetchval(
        "SELECT sum(n_tup_ins) FROM pg_stat_user_tables WHERE schemaname = 'public'"
    )
    rail.record(
        kind="ledger_refit", user_id=world.patrick,
        line=rail.refit_line("movie", n_titles=900, seconds=0.31, rho=0.42),
    )
    after = await world.db.fetchval(
        "SELECT sum(n_tup_ins) FROM pg_stat_user_tables WHERE schemaname = 'public'"
    )
    assert after == before, "recording a rail event inserted a row somewhere"

    assert await world.db.fetchval("SELECT to_regclass('public.model_event')") is None, (
        "the rail must not have a table; §6.7 says never persisted"
    )

    # And it is genuinely gone on restart — the buffer is process state, not a cache over one.
    assert rail.recent(user_id=world.patrick), "the event is readable while the process lives"
    rail.forget()
    assert rail.recent(user_id=world.patrick) == []


async def test_one_persons_rail_never_shows_another_persons_events(world):
    """Decision 117 scopes the toggle per user, and §6.7's rail "narrates every model write" —
    the reader's own, plus the household's. A shared buffer that leaked across accounts would
    make the toggle reveal someone else's ratings, which is a different feature entirely."""
    rail.forget()
    rail.record(kind="verdict", user_id=world.patrick, line="verdict(patrick, A) = liked")
    rail.record(kind="verdict", user_id=world.patrick + 5000, line="verdict(other, B) = liked")
    rail.record(kind="ledger_refit", line=rail.refit_line("movie", n_titles=9, seconds=0.1))

    mine = rail.recent(user_id=world.patrick)
    assert [e["scope"] for e in mine] == ["household", "you"]
    assert not any("other" in e["text"] for e in mine)


def test_a_noisy_account_cannot_push_another_accounts_events_out_of_its_rail():
    """One deque per user rather than one global deque. With a single shared buffer, a member
    mid-rating-session would evict a quieter member's entire rail inside fifteen taps — and the
    rail would be empty exactly for the person who just turned it on to see why."""
    rail.forget()
    rail.record(kind="verdict", user_id=1, line="verdict(quiet, A) = liked")
    for i in range(rail.RAIL_LIMIT * 3):
        rail.record(kind="verdict", user_id=2, line=f"verdict(noisy, {i}) = liked")

    quiet = rail.recent(user_id=1)
    assert len(quiet) == 1 and "quiet" in quiet[0]["text"]
    assert len(rail.recent(user_id=2)) == rail.RAIL_LIMIT
    rail.forget()


async def test_the_hidden_count_is_what_the_toggle_would_actually_reveal(db, world):
    """§6.0's count line exists so a toggle cannot hide things silently — "6 films · 2 series
    hidden". The number therefore has to be what turning the toggle on would show.

    It was the whole catalog's count of the unselected kind, ignoring every filter the listing
    had applied. With a person filter over a four-title filmography that read "26 series
    hidden", promising twenty-six things the toggle could not produce — a worse answer than no
    number, and precisely the silent-truncation failure inverted.
    """
    # `person.id` comes from the corpus, not a sequence (§4.1 rule 4 keeps upstream ids), so
    # the fixture supplies one.
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

    # And the promise holds: the count is exactly what the other toggle produces.
    both = await world.home(kinds=("movie", "series"), person_id=person_id)
    assert both["catalog"]["total"] == catalog["total"] + catalog["hidden"]["series"]
