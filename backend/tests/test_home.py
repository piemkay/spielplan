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
# Decision 475 builds the shelves in a claim order and no title repeats on a second shelf of its
# kind, so every shelf here has a population of its own: "Your top picks" is the twelve SEEN
# titles (rewatches included, and nobody else wants them), shelf 1 is MEMBERS, the frontier is
# FRONTIER, the sweet spot is DECOYS, and "Under 110 minutes" is SHORT - short, unseen, owned and
# on no earlier shelf. FILLER is owned, unseen and lowest-scored: it is what puts DECOYS above the
# sweet spot's 0.70 floor in a library whose top twelve are all seen, and it carries no DNA.
SHORT = (24, 25, 26, 27)
FILLER = tuple(range(24, 50))

# §6.0 shelf 5: strict `<`, and a NULL runtime is excluded because a shelf that claims a
# runtime bound must know the runtime. 1005/1105 and 1028/1128 sit exactly ON the threshold,
# and 1029/1129 have no runtime - the latter two among the titles no earlier shelf claims.
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

# §5.1's blend weight, seeded away from the measured 0.8 so a why-line printing the constant
# rather than this profile's fitted number is visible in the copy.
FITTED_BETA = 0.62


# The unseen titles' score order: DECOYS first, so they are the unseen titles both people rate
# highest - the sweet spot's population once the shelves before it have claimed theirs.
UNSEEN_ORDER = DECOYS + MEMBERS + FRONTIER


def score_of(title_id: int) -> float:
    """EVERY series outscores EVERY film. §4.1 rule 5's landmine, in miniature.

    The seen titles (12-23) are the top twelve, so "Your top picks" - which ranks rewatches too -
    claims only titles no later shelf would show (decision 475).
    """
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
        # `placement_bundle` is not decoration here: `title_placement_has_basis`
        # (0023_import_state.sql) makes "placed" and "names a basis" one fact, so a fixture that
        # stamps only the badge is the state decision 249 exists to forbid. BUNDLE is this
        # world's active row, inserted above, and it is the basis every placement here means.
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

    # §6.0 shelf 1's world: an anchor, four titles carrying BOTH of its terms, and three decoys
    # carrying only one of them plus a term the anchor also has.
    #
    # The projected weights are `n_sources` counts, not confidences: `importer/dna.py` writes the
    # bundle's 1..8 source count into `dna_projected.weight`, which is finding 20's whole subject.
    # Seeded as 0.6 and 0.5 this fixture could not see decision 188 at all — under the expression
    # it replaced they weighed 0.18 and 0.15, far below the extracted floor of 0.733, so every
    # assertion in this file passed with the pre-M4.9 fragment restored and the guard on the
    # milestone's most far-reaching read was a substring compare. The anchor's 8 is the corpus
    # maximum and weighs 2.40 under the old form. [M4.9 review cycle 1: M49-D188-02]
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
        # Decision 476: "you put" is only for a title the person placed on Rank; this anchor
        # carries a live liked verdict and no tier edit, so the headline says what they did.
        assert section["title"] == f"Because you liked Home {'Film' if base == 1000 else 'Series'} " \
                                  f"{base}"


async def test_the_ledger_shelf_names_the_beta_its_own_ranking_used(world):
    """§6.0 row 2 names β. Printing the measured constant 0.8 while this profile's fitted β is
    0.62 is exactly the decorative why-line §6.0 forbids — the number would have had no part in
    the ordering the person is looking at.

    Decision 476 moves the number out of the sentence and decision 486 puts it behind Show the
    model: with the switch off the why-line carries no β at all, and with it on `why_numbers`
    carries the fitted one, never the constant.
    """
    payload = await world.home()
    for kind in ("movie", "series"):
        section = world.section(payload, "top_of_ledger", kind)
        assert section is not None
        assert "β" not in section["why"] and "0.62" not in section["why"], section["why"]
        assert "why_numbers" not in section, "a model number reached a member with the switch off"
        assert "rewatches included" in section["why"]   # proposal 25's stated exception

    await world.client.post("/api/auth/preferences", json={"show_model": True})
    payload = await world.home()
    for kind in ("movie", "series"):
        section = world.section(payload, "top_of_ledger", kind)
        assert section["why_numbers"]["beta"] == pytest.approx(FITTED_BETA, abs=1e-6)
        assert section["why_numbers"]["beta"] != pytest.approx(0.8, abs=1e-6)


async def test_the_optimum_the_ledger_shelf_prints_is_this_apps_own_and_not_the_corpuss(world):
    """Decision 167. §5.1 quotes the corpus's 0.8, but the corpus's table is headed `blend beta
    (1.0 = crowd only)`: that number weighs the CROWD, and β here weighs the personal half. So
    the optimum in these coordinates is 1 − 0.8 = **0.2**, which is also where this app's own
    held-out Spearman peaks over 150 real raters and where the median fitted β already sits.

    The consequence is copy, not ranking — the cross-validation searches and serves in one
    orientation, so every stored number was always right. What was wrong is what a person reads:
    `beta_optimum` travels on every "Top of your ledger" section, and a member fitted at the
    optimum was being measured against a constant that is the complement of it.

    Decision 486 puts the optimum behind Show the model with every other model number, so it is
    read with the switch on; the caption that carried it to members (and quoted §5.1 to them) is
    gone, and the unfitted profile's sentence says in words what the number said.
    """
    await world.client.post("/api/auth/preferences", json={"show_model": True})
    payload = await world.home()
    for kind in ("movie", "series"):
        section = world.section(payload, "top_of_ledger", kind)
        assert section is not None
        assert section["why_numbers"]["beta_optimum"] == pytest.approx(0.2, abs=1e-9)
        assert section["caption"] is None, section["caption"]

    # The first evening of a household, before the first nightly run: never fitted, so ranked by
    # the crowd alone - and said so without a β or a section number.
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
    """Proposal 27: "Under the Series partition this shelf restates itself as 'Episodes under 45
    minutes' … the thresholds (110 min film, 45 min episode) are constants, not copy."

    The prototype applied 110 to per-episode runtimes and swallowed the series catalog. Also
    checks the strict `<` (a title at exactly the threshold is not under it) and the NULL
    exclusion (a shelf claiming a runtime bound must know the runtime).
    """
    # With the switch on, because the threshold also rides `why_numbers` and decision 486 gates
    # that block; the title states it either way.
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
        # SHORT, and not MEMBERS, which are as short: shelf 1 claimed them first (decision 475).
        assert [c["title_id"] for c in section["items"]] == sorted(ids(base, SHORT))
        for card in section["items"]:
            assert card["runtime_min"] is not None
            assert card["runtime_min"] < threshold
        shown = {c["title_id"] for c in section["items"]}
        assert not shown & set(ids(base, MEMBERS)), "a title shelf 1 shows is shown again here"
        assert base + 28 not in shown, "a title at exactly the threshold is not *under* it"
        assert base + 29 not in shown, "a NULL runtime cannot satisfy a runtime claim"


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
    what makes that checkable: `item_n`, the count of crowd ratings behind a title. (Not
    "§5.1's gate input", which is the same number only while every row carries a coordinate --
    a cold-masked row has crowd support and no n_t, and `title_prior.gate` is the column that
    carries the gate. [M4.13 cycle 2, M413-C2-DIM5-01])

    Title 1007 is `warm` with 4,213 crowd ratings (gate 0.998). A shelf that selected on recency
    alone, or on `placement` without asking what the model actually has, would show it under a
    claim of no crowd data.
    """
    payload = await world.home()
    for base in BASES:
        section = world.section(payload, "new_in_library", kind_of(base))
        assert section is not None
        # Decision 476's words for the same claim.
        assert section["why"] == "no outside ratings yet, so we placed them by what they're about"
        shown = [c["title_id"] for c in section["items"]]
        # Ordered by recency, newest first — the one shelf that is not score-ordered.
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
        # Decision 476: the title says what the shelf predicts, over titles neither has seen.
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
    """The floor is a rank within the set the shelf is about, and that set is the owned library.

    `user_score` holds a row for every coordinated title of the kind - on the first household 9.5k
    films, most of them unowned - and the ranking CTE ranked all of them, so the owned library's
    mean rank was 0.42-0.46 and a catalogue nobody could play decided which owned titles cleared
    0.70. Forty unowned titles per kind scored above everything owned would have pushed every
    sweet-spot card below the floor and emptied the shelf; read against the owned library they
    change nothing. One function for WA's and WB's two copies of this fix, derived against
    decision 475's world, where the sweet spot is DECOYS in both kinds.
    [owner instruction of 2026-09-25 after the first household user test, C1.7]
    """
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
        # 1060-1099 and 1160-1199: past each kind's owned ids, so `kind_of` still reads them.
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


# --- library-rate-shelf-anchor-is-a-rated-title-in-the-tier-its-owner-assigned ----------------


async def test_a_synced_seen_but_unobserved_title_cannot_anchor_shelf_one(world):
    """§6.3's "every rated title" is `ledger_state.observed`, and shelf 1's anchor is one.

    Proposal 24 puts the anchor on the top-scoring SEEN title, which the shelf read as "seen and
    carrying a fitted tier". Those are not the same population: `refit_user` writes a
    `ledger_state` row for every owned title of the kind, observed or not
    (`ledger/refit.py:350-374`), so every title §7.2's Jellyfin sync marked watched arrived here
    with an `s`, a `tier` and no observation at all — and won the ORDER BY whenever its prior beat
    the rated titles. Home then said "Because you put Home Film 1021 in A" about a title this
    person has never rated, which is not even on the Rank board the sentence is quoting
    (`rank/read.py:106`). The two predicates stay independent in both directions: a verdict
    implies seen (`ledger/observations.py:640-641`), a duel or a tier edit does not.
    [M4.9 finding 15]
    """
    # The toggle is on throughout so the suppressed list is readable: this test's failure mode is
    # a shelf that anchors on the wrong title, and the reason line is what names which.
    await world.client.post("/api/auth/preferences", json={"show_model": True})
    # 1021 is seen (the sweep marked it watched) and has no live verdict — the banner's own
    # population. Nothing has been observed about it, and its prior beats every rated title's.
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

    # And with nothing rated at all the shelf is absent, with a reason naming BOTH predicates —
    # they fail for different reasons and are repaired by different actions, so a line naming
    # only "seen" sends a person who has rated nothing off to mark titles watched.
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
    """§6.0 row 1's verb is "you PUT", and §6.3 says where a title a person dropped renders.

    `rank/board.py:20-27`: "the most recent `tier_edit` decides where a title renders, and the
    model decides it only when there is no edit." The headline read `ledger_state.tier` and never
    looked at `tier_edit`, so dropping the anchor from A to F on Rank left Home still saying
    "in A" — the same title, the same person, two surfaces disagreeing about the one thing the
    sentence claims they did. Decision 187 keeps the shelf-card BADGE on the model's tier and
    fixes only this sentence, because only this sentence has that verb. [M4.9 finding 16]
    """
    tier_set = shelves.DEFAULT_TIER_SET
    model_tier = await world.db.fetchval(
        "SELECT tier FROM ledger_state WHERE user_id = $1 AND title_id = 1000", world.patrick
    )
    assert tier_set[model_tier] == "A", "the fixture's anchor is fitted into A"
    # Decision 475 anchors on the tier the board shows before `s`, so a title dropped to F loses
    # the anchor to any title still in A. The other rated films are unobserved here to leave 1000
    # the only candidate: what this test is about is the tier the sentence NAMES.
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

    # AND after the nightly refit, which is where the prototype's answer changed a second time:
    # the fit absorbs the drop and moves `ledger_state.tier` part of the way, so a headline
    # reading the model said "in A", then "in B", then "in C" while the person's own last action
    # never changed. `tier_edit` is append-only (§4.2), so the sentence is stable by construction.
    #
    # Only the anchor stays `seen`, so the refit cannot hand shelf 1 to a different film: the
    # drop is the only asymmetric observation this profile has, so it also decides which title
    # ends up top, and without this the test would be measuring where the optimiser moved the
    # anchor rather than which tier the sentence names. Membership is untouched — 1001-1004 are
    # unseen owned films carrying both of the anchor's terms, which is what shelf 1 selects on.
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
    """dd06, across two surfaces. §6.0 row 1's verb is "you PUT", so the headline is a quotation
    of where §6.3 renders the title — and a quotation that names a different tier is a bug the
    person can see without leaving the app.

    Decision 11 keeps the `tier_edit` row across a change in K and §4.2 never rewrites it, so the
    stored index has to be re-read against the set it is being shown in. Both surfaces used to
    CLAMP, which agreed by accident: a drop into tier 6 of 7 read as tier 6 of 12 on Home and on
    Rank alike — mid-board, with the top five tiers empty. M4.13 maps it by cumulative prior mass
    through one helper, and the assertion that matters is that it is the SAME helper: mapping on
    the Rank side alone would have left Home quoting a tier Rank no longer shows, which is ml01's
    measured symptom ("Home showing T4 and Rank T7 for one title") reached from the other side.
    """
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


# --- library-rate-cold-badge-follows-crowd-support-not-placement -------------------------------


def no_crowd_data(card: dict) -> bool:
    """`PosterCard.svelte:34-38`'s expression, in Python, over one shelf card.

    Restated rather than imported because the component is JavaScript and this is the payload
    contract it consumes: what is under test here is whether the SERVER sends the two fields that
    expression prefers. The component's own precedence is pinned by
    `test_static_contracts.py::test_the_cold_badge_expression_reads_e_source_not_placement`.

    A title with crowd ratings behind it is never "new", whatever `e_source` says: the bundle's
    evaluation holdout serves crowd-rated rows from the Cold Tower, and §8 stage 10 names the
    badge by the absence of ratings (the owner instruction of 2026-09-25's user test).
    """
    if (card.get("item_n") or 0) > 0:
        return False
    if card.get("e_source"):
        return card["e_source"] == "cold_tower"
    return card.get("item_n") == 0 or (
        card.get("item_n") is None and card.get("placement") == "cold_tower"
    )


async def test_shelf_cards_carry_e_source_outside_the_model_block(world):
    """§8 stage 10's badge is PRODUCT, so it must not ride decision 117's debugging gate.

    `PosterCard`'s comment is the specification — "Off `e_source`/`item_n`, NOT off
    `title.placement`" — and `shelves.py` put both inside `card["model"]`, which `rail.redact`
    removes wholesale. With the toggle off, which is every account by default, `placement` was
    the only branch the card could reach; and `0008_placement.sql:54-58` stamps `cold_tower` on
    any title with a Backbone row and `item_n < 90`, so on the reference library 111 of the 130
    badges Home drew were false. [M4.9 finding 18]
    """
    # Exactly 0008's case: a real Backbone row, crowd support under §5.1's warm gate, and the
    # Cold Tower stamp that follows from it. The title is one of shelf 1's four members.
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
        # The falsifier, stated rather than trusted: the card as it shipped before this change —
        # `placement` and nothing else — badges the same title.
        assert no_crowd_data({"placement": card["placement"]}), (
            "the placement-only fallback no longer reproduces the defect this test is for"
        )

    # And the honest case still badges: `new_in_library`'s cards have no Backbone row at all.
    cold = world.section(await world.home(), "new_in_library", "movie")["items"][0]
    assert cold["e_source"] == "cold_tower" and no_crowd_data(cold)


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
    # Decision 203: the link carries `head=` and nothing else. It used to lead with
    # `mode=sweep`, which `api/rate.py`'s `current` does not declare and `rate/+page.svelte`
    # does not read, so the banner stated a control neither end has.
    assert "mode" not in query, query
    assert list(query) == ["head"], query
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
    banner = (await world.home())["banner"]
    served = await world.client.get(banner["cta"]["api"])
    assert served.status_code == 200, served.text
    card = served.json()["card"]
    assert card is not None and card["type"] == "sweep"
    assert card["title"]["id"] == banner["head_title_ids"][0], (
        "the queue served a different card than the banner named"
    )


async def test_the_banner_names_only_the_kinds_the_live_session_can_serve(world):
    """§6.0's banner names what §6.1's queue can serve, and nothing else. [M4.10 finding 23]

    The fixture makes this falsifiable rather than plausible: 1023 and 1123 carry the same
    `state_changed_at` and the tie breaks on `t.id DESC`, so the most recently seen pending
    title is a SERIES. With no kind predicate a films-only session was handed a banner whose
    first named title was that series and a CTA that served a film instead — proposal 150's own
    failure mode, "a prompt that names titles and then presents a different one is worse than no
    prompt", reached by the one surface that quotes it.

    The session is opened through §6.1's own control rather than by writing `rate_session`, so
    what is under test is the chain a person walks: the control narrows the queue, and the
    banner reads the narrowing.
    """
    both = (await world.home())["banner"]
    assert both["count"] == 6
    assert "series" in {c["kind"] for c in both["named"]}, both["named"]

    opened = await world.client.post("/api/rate/session", json={"kinds": ["movie"]})
    assert opened.status_code == 200, opened.text
    films = (await world.home())["banner"]
    assert films["count"] == 3, "the count is the population the CTA can serve, not all of it"
    assert {c["kind"] for c in films["named"]} == {"movie"}
    assert "Home Series" not in films["copy"]["wide"], films["copy"]["wide"]

    # The property the copy and the link have to share, asserted against the link the BANNER
    # emitted: following it opens on the FIRST title it named. The stashed card is cleared
    # first, which is the state the session is in the moment after a tap and the state a person
    # reaching Home mid-sitting is in -- `ensure_card` keeps a stashed card that is already one
    # of the named titles, so leaving whatever the control happened to draw on the table would
    # assert the idempotency branch instead of the pin.
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

    # The filter is the live session's rather than a standing narrowing of the banner: widen the
    # session and the series is nameable again.
    widened = await world.client.post(
        "/api/rate/session", json={"kinds": ["movie", "series"]}
    )
    assert widened.status_code == 200, widened.text
    assert (await world.home())["banner"]["count"] == 6


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

    Two orders now, and both are kind-independent. A person filter lists by year. A search lists
    best match first (decision 472): match quality is a property of the text, so an exact match
    of either kind leads, and titles that match equally well still interleave.
    """
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
    household clock rather than the device clock — and so it is assertable without a browser.

    The band the LIVE payload carries is asserted next door, against a zone chosen to move it.
    The set-membership assertion that used to stand here accepted all four bands and therefore
    accepted every possible answer, including the one `_now_local`'s bare `except` produces
    [M4.9 finding 22]; what is left is the part this test is actually for — the four bands and
    the copy — with each boundary named as a number rather than as a range.
    """
    payload = await world.home()
    assert payload["greeting"]["text"].endswith(", patrick")
    assert payload["greeting"]["tz"]

    at = datetime(2026, 8, 30, tzinfo=UTC)
    assert shelves.greeting(at.replace(hour=3), "p")["band"] == "up_late"
    assert shelves.greeting(at.replace(hour=9), "p")["band"] == "morning"
    assert shelves.greeting(at.replace(hour=14), "p")["band"] == "afternoon"
    assert shelves.greeting(at.replace(hour=21), "p")["text"] == "Good evening, p"


# Spread across the dial so at least one of them is in a different greeting band from the
# process's own clock whatever hour the suite runs at. Named rather than computed from an
# offset, because §2's `TZ` is an IANA name and the point is that the app resolves one.
FAR_ZONES = (
    "Pacific/Kiritimati",   # UTC+14
    "Pacific/Midway",       # UTC-11
    "Asia/Tokyo",           # UTC+9
    "America/Anchorage",    # UTC-9
    "Pacific/Auckland",     # UTC+12/+13
)


def _zone_that_moves_the_band() -> str | None:
    """A zone this checkout can resolve whose band differs from the process clock's, or None.

    None has two causes and they are the same case for this test: a checkout with no tz database
    at all (a Windows checkout has neither `/usr/share/zoneinfo` nor, unless someone installed
    it, the `tzdata` wheel — see `test_worker_schedule._resolvable_zone`), or the freak hour at
    which every candidate happens to share a band with the host. In both, `_now_local` takes its
    §3.1 fallback or its answer is not falsifiable, and the test asserts the half that still is.
    """
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
    """§2's `TZ`, proposal 22: "a greeting in four bands against §2's TZ" — the HOUSEHOLD clock.

    `api/home.py:_now_local` converts to that zone behind a bare `except`, and nothing exercised
    it: the assertion next door accepted the complete set of bands, and the four real assertions
    call `shelves.greeting()` with hand-built datetimes, which is the pure function and not the
    route. So the band could have come from the process's clock, from UTC, or from the fallback
    branch, and every test in the file would still have been green. [M4.9 finding 22]

    Three arms, none of them skipped. The payload must NAME the configured zone whichever branch
    `_now_local` took — §6.8's rule that the app does not report a setting it did not honour.
    Where the zone resolves, the band is asserted to be that zone's, against a candidate picked
    so its band differs from the process's own: without that difference "equal to `greeting()` in
    that zone" would be incidentally true and would prove nothing. Where it does not resolve, the
    §3.1 fallback is asserted instead — the process's own naive clock, not UTC and not a constant
    — so a checkout with no tz database still falsifies something rather than skipping.
    """
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
        # Bracketing the request rather than sampling once: the two agree at every instant except
        # a band boundary crossed mid-request, and a one- or two-element set is still an
        # assertion about THIS clock — which is the whole difference from the four-band set.
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


# --- §6.7 / decision 117: the show-the-model gate ----------------------------------------------

# Every key that carries a number about THIS VIEWER's model. Walked recursively, so a builder
# that adds a seventh annotation under a new name is caught by the shape of the test rather
# than by someone remembering to extend a list of call sites.
#
# `e_source` LEFT THIS SET IN M4.9, and it is the one entry that ever should. It is not a number
# about this viewer: it names which half of §5.1 produced the item's prior, it is identical for
# every account, and §8 stage 10 requires the card to badge on it — "a 'new — model placement,
# no crowd data' badge until ratings accrue" — which is product copy, not a debugging
# annotation. Gating it left `title.placement` as the only branch `PosterCard` could reach with
# the toggle off, and 0008 stamps that column on any title with a Backbone row and item_n < 90.
# `test_shelf_cards_carry_e_source_outside_the_model_block` asserts the other half: that it is
# present with the toggle in both positions. [M4.9 finding 18]
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
    """Decision 117: "It governs the rail and every inline annotation."

    ABSENT, not hidden. A number removed by CSS is still on the wire, in the network tab and in
    the service-worker cache, so the promise would be cosmetic. The whole payload is walked.
    """
    assert await world.db.fetchval(
        "SELECT show_model FROM app_user WHERE id = $1", world.patrick
    ) is False, "decision 117: default off"

    payload = await world.home()
    assert model_keys_in(payload) == []
    # `suppressed` alone, and not `rail` beside it: no route emits a `rail` key any more, so
    # asserting its absence here would pass by being unable to fail. The one drawer reads
    # `/api/model-log`, which is gated by `rail.visible_to` at the route rather than by
    # `redact`. [M4.9 review cycle 1: M49-HOME-04]
    assert "suppressed" not in payload
    for shelf in payload["shelves"]:
        for section in shelf["sections"]:
            assert section["items"], shelf["id"]
            for card in section["items"]:
                assert "model" not in card
                # …while proposal 29's chrome survives: rank, the seen dot and the settled tier
                # are what a shelf card IS, not an annotation about the model.
                assert card["rank"] >= 1
                assert "seen" in card and "tier" in card


async def test_the_gated_model_block_reads_the_fold_ins_rho_against_the_bundles_own_figures(
    world,
):
    """§14 risk 1's mitigation is "expectations instrumented, not assumed", and `user_vector.cv_rho`
    was neither: the fold-in stores a held-out Spearman per (user, kind) and nothing in the app knew
    what a good one looked like. The corpus ships the reference - `cold_eval.json`, cold 0.35225
    against a ceiling of 0.39193 - in a file that was in no list and read nowhere.

    Three properties, and the middle one is the reason the other two are not enough:

    * the figures travel WITH the rho, so the number is never printed alone;
    * §0's pipeline variance (0.003-0.008 Spearman) is applied, so 0.355 against the corpus's
      0.35225 reads as a TIE and not as a win - the series row here is 0.00275 ahead, which a
      comparison without the floor would report as better than the corpus;
    * it is inside `model`, which decision 117's gate deletes wholesale. §6.0 mandates β on the
      why-line and on the title card, which is why those two are ungated; a held-out correlation is
      in neither sentence, and `rail.py` warns that a builder inventing a new top-level numeric
      block does not inherit the gate.

    [M4.13 step 35, cs-31]
    """
    from spielplan.models.artifacts import ColdEval

    yardstick = ColdEval(
        cold=0.35225, ceiling=0.39193, hybrid=0.37, delta=0.0191, ci95=(0.0043, 0.0339),
        n_test=1876,
    )
    # One rho clear of the floor, one inside it. Both are real `user_vector` rows: the world seeds
    # a fitted profile per kind and `cv_rho` is the column the nightly pass writes.
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

    # The gate, asserted the way the toggle test asserts it: the whole payload is walked.
    assert model_keys_in(rail.redact(payload, show_model=False)) == []
    assert "fit" not in rail.redact(payload, show_model=False).get("model", {})


async def test_with_no_bundle_reference_the_rho_is_not_printed_at_all(world):
    """The defect was a number with nothing to read it against, so the fallback is silence rather
    than a bare rho. A bundle older than `cold_eval.json` is legal (the file is optional in
    `BUNDLE_FILES`), and on that install the fold-in's quality is in the logs and the table where it
    always was - it is the *comparison* that cannot be made, and a payload that printed one half of
    it would be inviting the reader to supply the other from memory. [M4.13 step 35]
    """
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
    # The events are read from `/api/model-log`, and Home carries no copy of them. This payload
    # used to ship up to RAIL_LIMIT events on every load because the drawer was mounted on Home
    # and decision 117's gate was asked of the response; M4.9 moved the mount into the layout,
    # so `ModelRail` refetches on every open and the key had no reader — one drawer, not one per
    # route, the same rule `api/tonight.py` states. [M4.9 review cycle 1: M49-HOME-04]
    assert "rail" not in payload, "one drawer, not one per route"
    events = (await world.client.get("/api/model-log")).json()["events"]
    assert events[0]["text"].startswith("verdict(patrick, Home Film 1000) = liked")
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


async def test_the_title_card_model_line_is_absent_with_the_toggle_off_and_present_with_it_on(world):
    """Decision 486, amending decision 117: §6.7's toggle governs §6.0's model line too.

    Decision 117 left this one line ungated as the M0 transparency promise. The 2026-09-25 user
    test put it in front of two members with the switch off, and β and σ in it are this viewer's
    own fit, not crowd provenance - so it is ABSENT from the payload with the switch off, as the
    rail is, and present the moment it is on.
    """
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


# --- library-rate-model-log-limit-cannot-exceed-the-buffer -------------------------------------


async def test_the_model_log_refuses_a_limit_above_the_buffer(world):
    """§6.7: "an ephemeral log (last ~15 events, never persisted)". The route's ceiling IS that.

    It declared `le=50` against a 15-deep buffer, so the one number in §6.7's sentence and the
    one number the URL would accept were three and a bit apart. Refused at the edge rather than
    silently truncated: a client that asks for fifty and receives fifteen cannot tell a capped
    answer from an exhausted buffer, and this route is the debugging instrument. [M4.9 finding 26]
    """
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
    """The other half of finding 26, and the half the route's `le` cannot reach.

    `recent` concatenated the caller's deque and the household's and only then sliced, so two
    RAIL_LIMIT-deep buffers answered with up to thirty events — the buffer was bounded and the
    response was not. Called directly here because `rail.recent` is public and `api/tonight.py`
    used to call it with a limit of its own; the property is the function's, not the route's.
    """
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
    # Newest-first survives the earlier slice: ids come from one process-wide counter, so the
    # newest `keep` of each deque contains everything the merged newest `keep` can contain.
    events = rail.recent(user_id=world.patrick, limit=50)
    assert [e["id"] for e in events] == sorted((e["id"] for e in events), reverse=True)
    assert {e["scope"] for e in events} == {"you", "household"}, (
        "capping each deque first must not drop one of them entirely"
    )
    rail.forget()


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


def test_a_title_name_too_long_for_the_rail_is_elided_rather_than_refused():
    """Finding 8's third raise site, closed in the renderer. [M4.10 finding 8]

    `api/rank.py` composes its duel line AFTER `record_duel` has committed and `rank/drop.py`
    composes its tier-edit line after the edit has, so a renderer that can refuse is a route
    that can answer 500 over a durable row: measured, two 260-390 character names made the
    answer route 500 with the duel written and the retry wrote a fifth. `rank/tiers.py:56-62`'s
    `MAX_LABEL` comment records the same failure for tier labels and took the other branch,
    because a label is a choice somebody made and a title name is bundle data.

    The two refusals that survive are the ones that are programming errors rather than data,
    and they survive on purpose: the Rank routes lean on that half of the contract.

    `verdict_line` is asserted here too, because the rule is every renderer that interpolates a
    name and not only the two the Rank routes call. It was written without either `_elide`, and it
    is §6.7's commonest line on the surface this milestone is named for: `rate/session.py`'s
    `payload` records it after the verdict has committed, so at the 64 characters `AccountName`
    allows a 280-character title name was a 500 over a durable row whose retry the nulled card
    token then answered 409. [M4.10 cycle 1, M410-R1-01]
    """
    name = ("The Assassination of Jesse James by the Coward Robert Ford " * 6)[:300]
    assert len(name) == 300, len(name)

    line = rail.duel_line(name, name, "TIE", context="tier_queue", selection="uniform_holdout")
    assert len(line) <= rail.MAX_LINE
    assert rail.record(kind="duel", user_id=-1, line=line) > 0, "the write must not refuse"
    edit = rail.tier_edit_line(name, "A", via="drag_drop", neighbour_duels=2)
    assert len(edit) <= rail.MAX_LINE
    assert rail.record(kind="tier_edit", user_id=-1, line=edit) > 0
    # The longest name `AccountName` (`api/setup.py`) permits, against the same 300 characters the
    # coverage row tests the Rank half with: 64 + 300 and a 67-character chrome is 431 of 400, and
    # at the 33 characters of "Grandma's iPad in the living room" it is exactly 400 — the threshold
    # sits inside the range of names a household actually types.
    member = "Grandma's iPad in the living room and the one in the kitchen :-)"
    assert len(member) == 64, len(member)
    spoken = rail.verdict_line(member, name, "disliked", refit_ms=31.4)
    assert len(spoken) <= rail.MAX_LINE, len(spoken)
    assert rail.record(kind="verdict", user_id=-1, line=spoken) > 0
    rail.forget(user_id=-1)

    # Elided, not emptied: §6.7's line still names the titles it narrates, once per name.
    assert name[:40] in line and line.count("…") == 2
    assert line.endswith("uniform-random, held out")
    short = rail.tier_edit_line("Drive", "A", via="drag_drop", neighbour_duels=2)
    assert "…" not in short and "Drive" in short

    # The bound holds for the longest line either renderer can compose, not only for this name.
    # Every other piece is bounded elsewhere, which is what `MAX_NAME_IN_LINE`'s arithmetic
    # assumes: 0005's CHECKs on `duel.outcome` and `duel.context`, `ARM_PHRASES`, and the tier
    # label's own bound -- imported rather than restated, because it belongs to that module.
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
    # The verdict's chrome is bounded by `VERDICT_LABELS` and by the millisecond count, so both
    # move while the names stay absurd -- a six-figure refit is already a pathology.
    for label in ("liked", "fine", "disliked"):
        for refit_ms in (None, 0.4, 31.4, 123456.7):
            said = rail.verdict_line(absurd, absurd, label, refit_ms=refit_ms)
            assert len(said) <= rail.MAX_LINE, (label, refit_ms, len(said))

    # And the refusals the data argument does not reach stay refusals, because the Rank routes
    # read them as the signal that the CALLER is wrong rather than the bundle.
    with pytest.raises(rail.RailError):
        rail.record(kind="duel", line="")
    with pytest.raises(rail.RailError):
        rail.record(kind="tier_drag", line=line)
    with pytest.raises(rail.RailError):
        rail.duel_line("a", "b", "A", context="tier_queue", selection="clairvoyance")


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
    assert payload["degraded"]["cta"]["route"] == "/rate"  # decision 203
    assert [s["id"] for s in payload["shelves"]] == ["new_in_library"]


async def test_a_bundle_less_app_says_so_instead_of_erroring(app, db):
    """§3.1: a bundle-less app is a legal state and "artifact-dependent surfaces render an
    explicit 'no bundle imported' state instead of erroring"."""
    client = app()
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


async def test_an_inferred_term_never_leads_the_why_lines_term_pool(world):
    """§4.1 rule 1 through the reader Home actually calls, not through the SQL string.

    `terms_for` is where a shelf's terms are chosen and where §6.0's why-line gets the term it
    names first, so decision 188's band is a property of this list — the anchor carries an
    8-source projection, the loudest an import can produce, and it still ranks below a salience-2
    quote. Asserting the fragment's text instead would pass over any future reader that stops
    spending it. [M4.9 review cycle 1: M49-D188-02]
    """
    for base in BASES:
        terms = await why_mod.terms_for(world.db, base + ANCHOR, version=VOCAB)
        assert [t.term for t in terms] == ["obsession", "morally-grey", "period"], terms
        assert terms[0].tier == "extracted", "an inferred term is named first"
        assert [t.tier for t in terms] == ["extracted", "extracted", "projected"]


async def _live_row_count(db) -> int:
    """Rows in every public table, counted exactly.

    This used to read `sum(n_tup_ins) FROM pg_stat_user_tables`, which is a *cumulative* view
    fed asynchronously by the statistics reporter — so on a long suite run the second read
    picks up earlier tests' inserts flushing late and the delta is nonzero with nothing having
    been written in between. A proxy that drifts under load cannot answer "did this call write
    anything"; `count(*)` over the live tables can, and it is the stronger assertion anyway.
    """
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
    """§6.7: "an **ephemeral** log (last ~15 events, **never persisted**)".

    This project shipped the rail as a `model_event` table first, on the argument that a nightly
    refit and a Cold Tower placement are model writes with no row of their own, so a rail derived
    from the observation tables would omit exactly what a person turns the rail on to see. The
    argument is sound; it is also not what the spec says, and "never persisted" is a normative
    sentence about a debugging instrument rather than a gap to be improved on.

    Two assertions, because "we deleted the migration" is not the property. The property is that
    recording an event writes nothing anywhere and that the buffer does not survive the process.
    """
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


# --- library-rate-verdict-rail-line-names-the-person-and-the-title -----------------------------


def _rail_record_kinds() -> set[str]:
    """Every `kind` a `rail.record` call in `backend/spielplan` can actually write.

    AST and not a regex, and with ONE hop of resolution, because five of the thirteen kinds are
    not spelled at a `rail.record` call at all: `rate/session.py:1259` writes
    `rail.record(kind=event_kind, ...)` and `api/rate.py` passes `event_kind="verdict"`,
    `"not_seen"`, `"duel"` and `"undo"` into `session.payload`. A guard that read only the
    literals at the record sites would report those four as unproduced and the tuple below would
    have to absorb them — which would make `AWAITING_PRODUCER` a list of "kinds the guard cannot
    see" instead of decision 189's list of "kinds nothing writes".
    """
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
    """§6.7's rail "narrates every model write", and `ModelRail` colours thirteen kinds.

    Seven of them were written by nothing. `ledger_refit`, `ledger_incremental`, `foldin`,
    `blend_weight`, `placement`, `reconcile` and `bundle_swap` — exactly the writes a person
    cannot otherwise see — had renderers, colour rules and a place in the filter row, and no
    call site, so a household turning the toggle on to find out why Home changed overnight saw
    nothing about the refit that changed it. [M4.9 finding 24]

    Decision 189 answers it in two halves and this guard holds both. Five kinds are worker-side
    — the nightly MAP refit, the incremental refit, the fold-in, the blend-weight fit and the
    placement sweep — and §6.7's "never persisted" makes the buffer per process, so there is no
    channel for them and they are declared pending by name, in one tuple. `bundle_swap` and
    `reconcile` were the other half: they were written inside the WEB process, so they were held
    to "has a producer".

    Decision 263 moves them across, because M4.14 step E2 moved the writer. The hot swap and the
    in-request rebuild sweep now run in the worker's `_bundle_import`, so those two `rail.record`
    calls were writing into a process-local buffer that no web request reads — the one event
    that invalidates every fitted number in the app, narrated to nobody, behind a comment saying
    the opposite. Seven kinds are pending and none of the seven has a call site, which is what
    makes "declared pending" mean something a reader can check.

    What the guard forbids is the third state the rail was actually in: a kind that is neither
    written nor declared, a filter chip for events that cannot arrive.
    [M4.14 cycle 1, m414-c1-dim-lock-02, decision 263]
    """
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


# --- decision 475: how Home's shelves choose their titles ------------------------------------


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
    """Decision 475. The first household's Home drew three score-ordered shelves off the top of
    one list, and three titles each appeared twice in one render. "New in the library" is the
    stated exemption: it reports an arrival rather than ranking one."""
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
    """Its why-line promises "the ones we think you'll enjoy most", so no shelf may thin it: the
    shelf that loses a title to it is the one below. Shelf 1's best member is raised into the top
    twelve here - "Your top picks" shows it and shelf 1 fills from its next candidates.

    Which next candidates is decision 513's: with 1001 gone, the three members left hold less
    specificity-weighted likeness than the three decoys, whose `period` is carried by four owned
    films against `morally-grey`'s five, so the shelf names the decoys' pair. It named the three
    members when likeness was a count of shared terms (decision 475)."""
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
    """Two of shelf 1's four members and one of the three decoys raised into the top twelve leave
    each of the anchor's pairs two titles, under proposal 28's floor of three: the shelf is
    absent, and the reason names the claim rather than reading as a library with nothing like the
    anchor in it. (With the decoys whole, the shelf would rightly fall back to their pair.)"""
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
    """Decision 475. The widest-pair rule named Zootopia's most generic pair and showed whatever
    covered it. Here three titles share FOUR of the anchor's terms and four share two; the widest
    pair is the two-term one, and the shelf must show the three.

    The terms are DOTTED ids with shipped labels that are not their leaf (`era.wwii` is "World
    War II"), because this file's vocabulary is otherwise dotless and could never show a raw id
    in a why-line (decision 486)."""
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
    """Decision 475. An argmax of the Ledger's `s` let one coordinate's scale choose the anchor
    (21.8 against 9.7 on the first household). The tier the board shows comes first."""
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
    """Decision 476, withdrawing decision 187's fallback headline. "you put X in {tier}" was said
    of titles the person had never placed - on the first household nobody had a single
    `tier_edit` row, and one member read that he had put Mission: Impossible in S."""
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
    """The person answers `real`, then §13's silent re-ask answers `reask`, each stamped as
    `record_verdict` stamps it: every write supersedes the row before it, the re-ask included, so
    the only un-superseded row left is the instrument's."""
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
    """Decisions 475/476 read "the latest live verdict", and a re-ask is not one: §13's stream
    measures a judgement and must not be one (`LIVE_LABEL_SQL`). After a re-ask the only
    un-superseded row is the instrument's, so `superseded_by IS NULL` let a "fine" title whose
    re-ask came back "liked" be headlined "Because you liked", and the reverse."""
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
    """The same read, in the tie-break: at one tier a live "liked" outranks a "fine" whatever the
    re-ask said, before the Ledger's `s` is consulted (decision 475)."""
    await _tag(world.db, 1012, "obsession", "themes", 3)
    await _tag(world.db, 1012, "morally-grey", "character", 3)
    # 1000 (s 3.0) and 1012 (s 2.0) share tier A. 1000's own answer is "fine"; its re-ask said
    # "liked", which would tie it with 1012 on the verdict and hand it the anchor on `s`.
    await _reask(world.db, world.patrick, 1000, real=1, reask=2)
    section = world.section(await world.home(), "because_anchor", "movie")
    assert section["anchor"]["title_id"] == 1012, section["anchor"]
    assert section["title"] == "Because you liked Home Film 1012"


async def test_the_anchor_is_the_highest_tier_the_board_shows_after_a_k_change(world):
    """Decision 475 anchors on "the highest tier the board shows", and after a change in K the
    board shows a drop through `rescale_level` (decision 11). Ordered by the raw index, an S drop
    on the 7-level board (6) sorted below a title fitted at T9 of 12, although Rank renders the
    drop at T11 - so the anchor was not the title in the highest tier on the board."""
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
    """Decision 476's "tier B, as on your Rank board" quotes §6.3's board, and decision 187 keeps
    the letter on the fitted tier. The card therefore carries what the board shows beside it, so
    the sentence is said only where it is true (decision 486 clause 7): a title marked watched and
    never rated is on no board (`ls.observed`, as `rank/read.py` reads it), and a title the person
    moved renders at their drop, not the fit."""
    # 1021 is seen with no rating, and `refit_user` still writes it a fitted tier (Dunkirk on the
    # first household). 1015 is fitted B and dropped into A+ on Rank. 1012 is fitted A, no drop.
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
    # Every card on every shelf carries the pair, so no card can fall back to `seen`.
    for shelf in (await world.home())["shelves"]:
        for sec in shelf["sections"]:
            for card in sec["items"]:
                assert "on_board" in card and "board_tier" in card, (shelf["id"], card)


async def test_a_cold_placed_title_with_crowd_ratings_is_not_new(world):
    """§8 stage 10 names the badge by the absence of crowd data. The bundle's evaluation holdout
    serves crowd-rated rows from the Cold Tower, so on the first household Raiders of the Lost
    Ark (192,061 ratings) wore "new" and led "New in the library"."""
    await world.db.execute("UPDATE title_prior SET item_n = 192061 WHERE title_id = 1008")
    payload = await world.home()
    fresh = world.section(payload, "new_in_library", "movie")
    assert 1008 not in {c["title_id"] for c in fresh["items"]}
    frontier = world.section(payload, "never_watched_term", "movie")
    card = next(c for c in frontier["items"] if c["title_id"] == 1008)
    assert card["e_source"] == "cold_tower" and card["item_n"] == 192061
    assert not no_crowd_data(card), "a title with 192,061 crowd ratings is badged 'new'"


# The model's working vocabulary and the spec's references, as decision 486 bars them from a
# member's screen. Checked over every string a shelf renders as a sentence.
_MODEL_WORDS = re.compile(
    r"§|β|σ|\bcos\b|\bcdf\b|\d\.\d\d|\bledger\b|fold-in|\bprior\b|Cold Tower|crowd data|"
    r"\bdecision \d|\bproposal \d|\bM[0-7]\b|\blabels\b",
    re.IGNORECASE,
)


async def test_no_shelf_sentence_carries_a_model_word_with_the_switch_off(world):
    """Decision 476 restates §6.0's table in the member register (decision 486): no β, cosine,
    CDF floor, "ledger", "fold-in", "prior", "Cold Tower" or section sign in a title, why-line or
    caption, and no `why_numbers` block at all with Show the model off."""
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
    """Decision 516: a German viewer's card leads with the original title where it is German, so
    every shelf card - whichever builder's statement produced it - carries both fields, null where
    the corpus recorded none."""
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
    """Home's count line stated the whole catalog - "13,330 films · 5,747 series hidden" - above
    shelves holding only owned titles. The payload names what the shelves draw on."""
    await world.db.execute(
        "INSERT INTO title (id, kind, name, year, is_owned) "
        "VALUES (1099, 'movie', 'Unowned', 2000, false)"
    )
    payload = await world.home(kinds=("movie",))
    assert payload["library"] == {"movie": len(MOVIES), "series": len(SERIES)}


# --- decision 512: what a member has disliked stays off their shelves --------------------------
#
# Patrick's live verdicts are "liked" on the anchor and on offsets 12-20 of each kind, and "fine"
# on 21 (the superseded row `seed` writes is the newest non-re-ask one). Jenny has none. A pattern
# is made here by tagging four of his rated SERIES with one term and turning those verdicts into
# "disliked" - series, because the evidence reads both kinds and his films then keep the ten
# liked verdicts the runtime ceiling needs. The term is a `mood`: the avoid set reads the mood,
# themes and sensibility facets only.

GORE = ("gore", "mood", "gory")
GORE_CARRIERS = (1117, 1118, 1119, 1120)


async def _term(db, term: str, facet: str, label: str | None = None) -> None:
    await db.execute(
        "INSERT INTO dna_term (version, term, facet, label) VALUES ($1, $2, $3, $4)",
        VOCAB, term, facet, label,
    )


async def _verdict(db, user_id: int, title_id: int, value: int) -> None:
    """One live verdict, replacing any earlier one - `seed` wrote exactly one per title."""
    await db.execute("DELETE FROM verdict WHERE user_id = $1 AND title_id = $2", user_id, title_id)
    await db.execute(
        "INSERT INTO verdict (user_id, title_id, value) VALUES ($1, $2, $3)",
        user_id, title_id, value,
    )


def _claiming_ids(payload, kind) -> set[int]:
    return {c["title_id"] for _, s in _claiming_sections(payload, kind) for c in s["items"]}


async def test_a_pattern_the_member_disliked_four_times_leaves_every_ranking_shelf(world):
    """Decision 512. A member who has disliked four titles carrying one term, and liked none, is
    shown no film carrying it on any shelf that ranks for them: not shelf 1 (1001), not the
    frontier (1008), not the sweet spot (1005), not "Under 110 minutes" (1024). "New in the
    library" reports an arrival and keeps 1008. The payload names what is left out by label,
    beside the runtime ceiling his ten liked films of 120 minutes set."""
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
    # Three members left and three decoys, one of them avoided: the members' pair is the only
    # one still carried by three.
    assert [c["title_id"] for c in first["items"]] == [1002, 1003, 1004]
    frontier = world.section(payload, "never_watched_term", "movie")
    assert sorted(c["title_id"] for c in frontier["items"]) == [1009, 1010, 1011]
    fresh = world.section(payload, "new_in_library", "movie")
    assert 1008 in {c["title_id"] for c in fresh["items"]}, "an arrival is reported, not ranked"
    assert world.section(payload, "shared_sweet_spot", "movie") is None, (
        "the two decoys left are under the floor of three"
    )


async def test_a_pattern_the_member_also_liked_is_not_avoided(world):
    """Decision 512: never avoided if any liked title carries it - four dislikes beside one like
    is a taste with an exception, not a pattern to hide."""
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
    # 1001 is back, last: the extra term it now carries makes it a little less like the anchor.
    assert [c["title_id"] for c in first["items"]] == [1002, 1003, 1004, 1001]


async def test_the_shared_shelf_leaves_out_what_either_member_avoids(world):
    """Decision 512: "you would both enjoy these" is false of a title one of them has turned down
    the pattern of four times. Jenny avoids gore; Patrick liked the same four films, so his own
    shelves still carry them and only the shared one leaves 1005 out."""
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
    """Decision 512's runtime ceiling: once a member has liked ten films, a film running more than
    half an hour past the longest of them - and past three hours - is left out. Patrick's ten
    liked films run 120 minutes, so the ceiling is three hours: 1021 at 250 minutes leaves "Your
    top picks", 1022 at exactly 180 stays, and a liked film's own length moves the ceiling."""
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


# --- decision 513: shelf 1's likeness is specificity-weighted and keeps the anchor's form -------


async def test_shelf_one_weighs_a_shared_term_by_how_rare_it_is(world):
    """Decision 513. Every candidate here shares exactly two of the anchor's terms, so a count
    cannot tell them apart and the widest pair wins: seventeen films carry `common` + `obsession`.
    Weighted by rarity in the owned library, the three films carrying `rare-a` + `rare-b` (four
    carriers each, the anchor's included) are the anchor's nearest, and the shelf names them."""
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
    """Decision 513: a live-action anchor draws live-action titles and an animated one animated
    titles ("Because you liked Chernobyl" drew Attack on Titan). Read through decision 473's
    canonical Animation, so trakt's "anime" would answer too."""
    await _animated(world.db, *ids(1000, MEMBERS))
    live = world.section(await world.home(kinds=("movie",)), "because_anchor", "movie")
    assert [c["title_id"] for c in live["items"]] == ids(1000, DECOYS), (
        "the four animated members are not a live-action anchor's nearest"
    )

    await _animated(world.db, 1000)
    drawn = world.section(await world.home(kinds=("movie",)), "because_anchor", "movie")
    assert [c["title_id"] for c in drawn["items"]] == ids(1000, MEMBERS)


# --- decision 514: the frontier's "which you like" is the member's own word ---------------------


async def test_which_you_like_rests_on_three_liked_titles_not_on_the_ledger(world):
    """Decision 514. `cosy` sits on three rated films whose Ledger CDF is 0.90, which is all the
    old reading asked; turn one of the three verdicts into "fine" and only two liked titles
    carry it, so "close to cosy, which you like" is no longer a sentence the person said. The
    film frontier is absent and says why; the series one, untouched, still ships."""
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
    """Decision 514: inside one facet a near term is the same thing under a narrower or broader
    name, and "never watched World War I, close to turn of the 20th century, which you like"
    reads as the contradiction it is. Filed under neon's own facet, cosy is no neighbour."""
    await world.db.execute(
        "UPDATE dna_tag SET facet = 'visual' WHERE term = 'cosy' AND title_id < 1100"
    )
    payload = await world.home()
    assert world.section(payload, "never_watched_term", "movie") is None
    series = world.section(payload, "never_watched_term", "series")
    assert series["why"] == "close to cosy, which you like"


# --- the sweet spot on one scale (§6.2 step 3, decision 477) ------------------------------------


async def test_the_sweet_spot_ranks_the_two_members_on_one_scale(world):
    """§6.0 row 4's "ranked as Tonight's pool is" is §6.2 step 3's plain average of each member's
    scores rank-standardised over the library (decision 477). Jenny's scores are shrunk to a
    hundredth, and she prefers 1007 to the others by a wide rank margin: averaged raw, Patrick's
    units decide ([1005, 1006, 1007]); on one scale her preference counts as much as his."""
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


# --- decision 515: the catalogue for you, and the card's why ----------------------------------


async def _titles(client, **params):
    query = [("kind", k) for k in params.pop("kinds", ("movie", "series"))]
    query += [(k, v) for k, v in params.items()]
    response = await client.get("/api/titles", params=query + [("limit", 200)])
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_catalogue_is_for_you_by_default_and_partitions_by_kind(world):
    """Decision 515. Patrick's fold-in is fitted to his own ratings for both kinds, so the grid
    orders by his score by default - and because that is a ranking, films first and then series
    (§4.1 rule 5, decision 18): every series here outscores every film, so a merged ranking would
    open on a series."""
    listing = await _titles(world.client)
    assert listing["sort"] == "for_you"
    kinds = [item["kind"] for item in listing["items"]]
    assert kinds == ["movie"] * len(MOVIES) + ["series"] * len(SERIES)
    for kind in ("movie", "series"):
        run = [item["id"] for item in listing["items"] if item["kind"] == kind]
        assert run == sorted(run, key=lambda t: -score_of(t)), kind


async def test_the_catalogue_is_newest_first_until_the_members_own_ratings_rank_it(world):
    """Decision 515: "for you" is the default only for a profile fitted to the member's own
    ratings. Jenny has none, so she gets the year order even when she asks, and the response says
    which order it is really in. A fit whose personal half has no weight (β 0) is the crowd's
    order - "what most people rate highest" - and is not "for you" either."""
    jenny = await world.sign_in_jenny()
    listing = await _titles(jenny, sort="for_you")
    assert listing["sort"] == "newest"
    years = [item["year"] for item in listing["items"]]
    assert years == sorted(years, reverse=True)

    await world.db.execute("UPDATE user_vector SET blend_beta = 0 WHERE user_id = $1", world.patrick)
    assert (await _titles(world.client))["sort"] == "newest"


async def test_newest_and_a_search_keep_their_own_orders(world):
    """Asked for, the year order stands; under a search the order is best match first (decision
    472) whichever sort was asked, and `sort` says `match`."""
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
    """Decision 515: one member-register sentence, true of this title for this reader. 1001 shares
    the anchor's two quoted terms, and the anchor is a film Patrick liked; the rarer and more
    prominent of the two is named first. Jenny has liked nothing and ranked nothing: None."""
    assert await _why(world.client, 1001) == (
        "Because you liked Home Film 1000 — they share morally-grey + obsession"
    )
    jenny = await world.sign_in_jenny()
    assert await _why(jenny, 1001) is None


async def test_the_title_card_says_nothing_of_a_title_seen_or_avoided(world):
    """Nothing is suggested about a title the member has seen, and the card does not argue for a
    title their shelves leave out (decision 512)."""
    assert await _why(world.client, 1012) is None
    term, facet, label = GORE
    await _term(world.db, term, facet, label)
    for title_id in GORE_CARRIERS:
        await _tag(world.db, title_id, term, facet, 2)
        await _verdict(world.db, world.patrick, title_id, 0)
    await _tag(world.db, 1001, term, facet, 2)
    assert await _why(world.client, 1001) is None


async def test_the_title_card_names_a_top_pick_that_nothing_liked_explains(world):
    """With no liked title alike enough, a title the member's own ratings put in the top tenth of
    the owned films reads as Your top picks does; 1030 carries no terms at all."""
    assert await _why(world.client, 1030) is None
    await world.db.execute(
        "UPDATE user_score SET score = 5.0 WHERE user_id = $1 AND title_id = 1030", world.patrick
    )
    assert await _why(world.client, 1030) == "One of the ones we think you'll enjoy most"
