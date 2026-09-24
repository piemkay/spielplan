"""M5.3's exit criterion: the sources and the derive, measured on a real install.

§12's M5.3 row names this script, and the eleven checks below are `docs/milestones/M5.3-plan.md`
§7's own pass table, numbered as it numbers them. The row's sentence is what they add up to:

    a title carrying one of the bundle's `credit_correction` rows is re-derived from its raw
    documents and still carries the correction afterwards, with the DNA verdicts applied at
    ingest and the credit corrections applied last; deriving it a second time changes nothing. A
    title with a plot but fewer than two review sources parks at `reviews gate` with both counts
    in its reason and a 30-day window, and the admin retry resumes from that stage rather than
    from stage 1, so nothing before the park runs again: no request leaves the process, no HTTP
    client is constructed, and no derived row is duplicated. A source that 404s is a note on the
    job while a failure of the required source parks with that source named; a scraped page that
    belongs to a different film is refused with nothing written; and a models-only re-import
    afterwards leaves a household-authored correction in place.

      script  plan  what it measures
        1       1   a title with a shipped correction, derived: the corrected credit is there
        2       2   the same title derived again: every derived row identical
        3       3   the adjudication applier disabled: a curated verdict visibly reverts
        4       4   stage 4 parks with both counts in the reason and a 30-day retry_after
        5       5   the retry resumes at stage 4, opens no socket, duplicates no derived row
        6       6   stage 2 over eight sources: a raw_document row per response, before any parse
        7       7   Metacritic 404 with TMDB ok: a note under that source, and stage 2 advances
        8       8   the required source failing: a park naming `tmdb:detail`
        9       9   a scraped page for another film: refused, and nothing derived from it
       10      10   a household correction survives the ledger reload a models-only import runs
       11      11   `wikidata:resolve` runs before `rt:page`, and the RT slug is read, not guessed

THE SCAR IS INVISIBLE IN A HAPPY PATH, so checks 1, 2 and 10 run against titles the bundle has
already ruled on. §14.5's sentence is that a derive which regenerates rows without re-applying
the curated ledgers silently reverts them, and a freshly acquired title carries neither an
adjudication nor a correction - so "applies both ledgers, corrections last" passes vacuously over
code that does nothing at all. Check 1 therefore picks a title the shipped `corrections_v1.tsv`
names, stages a TMDB document whose crew credits the WRONG composer, and asserts that the name
standing at the end of the derive is the ledger's. Check 3 does the same for the other ledger from
the other end: it shows the revert happening.

CHECK 3 IS THE NEGATIVE CONTROL AND IT IS OFF BY DEFAULT (decision 378). A run with no
`--negative-control` reports it as NOT MEASURED HERE and exits 3 - not a pass, not a failure, in
`ops/m51_exit_criterion.py`'s own idiom and for decision 184's reason: ten checks measured is ten
checks measured, and a green 11/11 from a run that never disabled anything is the certificate this
family of scripts exists to refuse. With the flag, the applier is replaced for the length of one
derive, on the scratch database every check here runs against, and put back inside a `finally`.
The plan's words are "never leave the applier disabled".

IT REFUSES TO RUN ON THE FIXTURE, for the reason `ops/m45_exit_criterion.py`,
`ops/m412_exit_criterion.py` and `ops/m51_exit_criterion.py` do, and the reason is specific to
what M5.3 claims. Three of the eleven checks are about curated ledgers, and the fixture ships
neither: the real bundle carries six `credit_correction` rows and 828 `dna_adjudication` rows, 817
of them title-scoped, and against a fixture with none of them checks 1, 2, 3 and 10 would each
print a verdict about a ledger that was empty. Check 9's population argument is the same one
`resolve.py` makes for stage 1: a page refused for belonging to another film is only interesting
where two films share a name, and 2,438 of the corpus's titles share `(kind, lower(name))`.

NOTHING LEAVES THE BOX. Every one of §8 stage 2's eight sources is driven against an
`httpx.MockTransport` serving a canned web declared at the top of this file, with the fetcher's
clock, sleeper and jitter injected so that Rotten Tomatoes' and Metacritic's real policy - seven
tenths of a request a second, one at a time (`acquire/hosts.py`) - is honoured without the run
taking a quarter of an hour. That is the rule `test_acquire_fetch.py` states and this script
keeps: make the harness tolerant, never the policy faster. The hostnames in the route table are
the real ones because the adapters build them, and no socket is opened to any of them.

It connects through `db/pool._init_connection` and never a bare `asyncpg.connect`: without the
json/jsonb codec `title_meta.payload` dies with "expected str, got dict", which is the defect
M4.5's harness spent three runs believing was in the importer.

It creates and drops its own DATABASE, so it never runs against a household's data by accident,
and it stages into a temporary DATA_DIR - which is the raw store's root as well as the artifact
root - that it removes. It takes the operator's connector credentials away before anything reads
them and seeds three throwaway ones of its own, so a household running this with a real TMDB key
configured does not write that key into a scratch database under a throwaway DEK.

Output is ASCII: a Windows console crashes on a decorative glyph, and every string this script did
not author goes through `console()` on the way out - the park reasons `acquire/stages.py` and
`derive/gate.py` write for an operator carry typographic dashes, and both reach the console here.

Run it against a live Postgres, with the bundle reachable:

    CORPUS_BUNDLE_DIR=/path/to/export_bundle/v20260828 \\
    TEST_DATABASE_URL=postgresql://... \\
      backend/.venv/Scripts/python ops/m53_exit_criterion.py [--negative-control]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
# `ops/` on the path so `m45_exit_criterion`'s reporting helpers can be imported rather than
# copied, which is what `ops/m414_exit_criterion.py` and `ops/m51_exit_criterion.py` both do and
# why: M4.5's recorded 18/18 has to stay reproducible unchanged, so that file is read, never
# edited.
sys.path.insert(0, str(ROOT / "ops"))

# Set before the first `spielplan` import, because `settings()` is `lru_cache`d and decision 181
# made §2's required config a refusal at construction. `setdefault` would read a developer's
# `.env` *after* this and pydantic-settings ranks the environment above that file, so these two
# are set outright: this script must never seal a household's real SECRETS_KEY into a scratch
# database, and a throwaway key is all the encryption here means.
os.environ["SESSION_SECRET"] = "m53-exit-criterion-session-secret-not-a-real-one"
os.environ["SECRETS_KEY"] = "m53-exit-criterion-secrets-key-not-a-real-one"
os.environ.setdefault("PUBLIC_URL", "http://localhost:8080")

import asyncpg  # noqa: E402
import httpx  # noqa: E402
from m45_exit_criterion import check, console, discard_staged_artifacts, results  # noqa: E402
from spielplan.acquire import fetch, pipeline, queue, rawstore, stages  # noqa: E402
from spielplan.connectors import registry  # noqa: E402
from spielplan.core import config as core_config  # noqa: E402
from spielplan.db import dna_terms, migrate  # noqa: E402
from spielplan.db import pool as db_pool  # noqa: E402
from spielplan.derive import gate, ledgers, parse, rebuild  # noqa: E402
from spielplan.importer import bundle as bundle_import  # noqa: E402
from spielplan.importer import dna as dna_import  # noqa: E402
from spielplan.importer.report import ImportReport  # noqa: E402

# One of the eleven adapters `sources.load_all()` discovers, imported by name for one reason:
# check 11 refuses the url a GUESS would have produced, and the only honest source of that string
# is the adapter's own `candidate_paths`. A literal here would be this file's idea of a guess.
from spielplan.sources import rottentomatoes  # noqa: E402

CHECKS: tuple[tuple[int, str], ...] = (
    (1, "a title carrying a shipped credit correction keeps it through a derive"),
    (2, "deriving the same title a second time changes no derived row"),
    (3, "with the adjudication applier disabled, a curated DNA verdict reverts"),
    (4, "stage 4 parks with both counts in the reason and a 30-day retry_after"),
    (5, "the retry resumes at stage 4, opens no socket and duplicates no row"),
    (6, "every stage-2 response has a raw_document row, and nothing is parsed yet"),
    (7, "a Metacritic 404 with TMDB answering is a note and not a park"),
    (8, "the required source failing parks stage 2 with that source named"),
    (9, "a scraped page for a different film is refused and derives nothing"),
    (10, "a household correction survives the ledger reload of a models-only import"),
    (11, "wikidata:resolve runs before rt:page and the RT slug is read, not guessed"),
)

# The floor under which CORPUS_BUNDLE_DIR is the fixture and not the corpus. Read off BUNDLE.json's
# own `total_bytes` rather than off the directory, because a bundle that has been half-copied is
# exactly what this script must be able to tell apart from one that is small by design.
# `ops/m414_exit_criterion.py` and `ops/m51_exit_criterion.py` use the same number.
MIN_REAL_BYTES = 500_000_000

# §8 stage 4's window, as the board must show it. Not thirty: `derive/gate.REVIEW_WINDOW` is the
# one place the number lives (decision 335) and check 4 compares against it, so a milestone that
# changed the window would move this check with it instead of failing it. The tolerance is what a
# run takes end to end plus a comfortable margin.
WINDOW_TOLERANCE_HOURS = 6.0

# The curated ledger this run writes a household row into, for check 10. `origin` is decision 326's
# column and `'household'` is the value no bundle can produce, which is the whole of why the check
# means anything: a models-only re-import replaces every `bundle` row and must leave this one.
HOUSEHOLD_COMPOSER = "An Exit Criterion Composer"
HOUSEHOLD_EVIDENCE = "typed into the ledger editor by the household, for this measurement"

# The composer a source gets wrong, staged into check 1's TMDB document so that the derive has
# something to overrule. It is not a real person and is not in any bundle: the point is that the
# derive WRITES it from the raw store and the ledger then replaces it, which is the sequence
# §14.5's scar is about.
WRONG_COMPOSER = "A Composer This Source Got Wrong"


# --- the canned web -------------------------------------------------------------------------------
#
# Five titles, one route table. Each item carries an IMDb id in a block no distributor has used and
# every route dispatches on it, so one handler serves the walk, the eight-source stage 2, the two
# refusals and the wrong page without five copies of the same JSON.
#
# THE SHAPES ARE THE ADAPTERS' AND NOT A CONVENIENCE. TMDB's `/find` answers under
# `movie_results`/`tv_results`; Wikidata's SPARQL answers under `results.bindings` with one binding
# per row; Wikipedia's action API answers `query.pages[0].extract`; Metacritic's scores are in a
# `title=` attribute on the hero block. A double that answered a shape the adapter does not read
# would measure this script's idea of each host rather than the parser that has to live with it.

ITEM_WALK = {
    "Id": "jf-m53-exit-walk", "Name": "An Exit Criterion Walk", "Type": "Movie",
    "ProductionYear": 2087, "RunTimeTicks": 101 * 60 * 10_000_000,
    "ProviderIds": {"Imdb": "tt93000001"},
}
ITEM_EIGHT = {
    "Id": "jf-m53-exit-eight", "Name": "An Exit Criterion Series", "Type": "Series",
    "ProductionYear": 2088, "RunTimeTicks": 42 * 60 * 10_000_000,
    "ProviderIds": {"Imdb": "tt93000002"},
}
ITEM_NOTE = {
    "Id": "jf-m53-exit-note", "Name": "An Exit Criterion Note", "Type": "Movie",
    "ProductionYear": 2089, "RunTimeTicks": 103 * 60 * 10_000_000,
    "ProviderIds": {"Imdb": "tt93000003"},
}
ITEM_REQUIRED = {
    "Id": "jf-m53-exit-required", "Name": "An Exit Criterion Refusal", "Type": "Movie",
    "ProductionYear": 2090, "RunTimeTicks": 104 * 60 * 10_000_000,
    "ProviderIds": {"Imdb": "tt93000004"},
}
ITEM_WRONG = {
    "Id": "jf-m53-exit-wrong", "Name": "An Exit Criterion Page", "Type": "Movie",
    "ProductionYear": 2091, "RunTimeTicks": 105 * 60 * 10_000_000,
    "ProviderIds": {"Imdb": "tt93000005"},
}

# imdb id -> (tmdb id, rt slug, metacritic slug, wikipedia article title). The slugs are what
# `wikidata:resolve` hands over, and check 11 is the assertion that `rt:page` asked for THIS string
# rather than for one built out of the name.
IDENTITY = {
    "tt93000001": (9301, "m/exit-criterion-walk", "movie/exit-criterion-walk",
                   "An Exit Criterion Walk"),
    "tt93000002": (9302, "tv/exit-criterion-series", "tv/exit-criterion-series",
                   "An Exit Criterion Series"),
    "tt93000003": (9303, "m/exit-criterion-note", "movie/exit-criterion-note",
                   "An Exit Criterion Note"),
    "tt93000004": (9304, "m/exit-criterion-refusal", "movie/exit-criterion-refusal",
                   "An Exit Criterion Refusal"),
    "tt93000005": (9305, "m/exit-criterion-page", "movie/exit-criterion-page",
                   "An Exit Criterion Page"),
}
TMDB_TO_IMDB = {tmdb: imdb for imdb, (tmdb, _rt, _mc, _page) in IDENTITY.items()}

DIRECTOR = "An Exit Criterion Director"
LEAD = "An Exit Criterion Lead"

# A plot, so that §8 stage 4's gate measures the half it is about. Decision 335 makes the plot
# `title.overview`, resolved by `importer/meta.resolve_title_fields` from the derive's own
# `title_meta` rows, so this string reaching the card is itself part of what check 4 asserts.
OVERVIEW = (
    "A synthetic plot, written for an exit criterion so that the reviews gate has a title with a "
    "plot and not enough reviews - which is the one state stage 4 exists to name."
)

# Two critic excerpts, deliberately ONE source and deliberately over fifty words. Decision 335's
# predicate is a conjunction, and a fixture that failed both halves would leave the check unable to
# say which one the park was about: this one passes the word count and fails the source count, so
# the reason printed on the board has to carry a figure that passed beside one that did not.
CRITIC_ONE = (
    "The staging is patient and the performances are exact, and the film earns its final "
    "twenty minutes by refusing to explain itself for the first ninety."
)
CRITIC_TWO = (
    "A confident picture that trusts an audience to keep up, carried by a lead performance of "
    "unusual stillness and by a score that never once tells you how to feel."
)


def _tmdb_credits(composer: str | None) -> dict[str, Any]:
    """TMDB's `credits` block: one billed cast member, a director, and optionally a composer.

    The composer is the row check 1's correction overrules. `derive/ledgers._MUSIC_CREDITS` matches
    on the JOB and not on `role_class`, deliberately and for the reason stated there, so the job
    string is the one TMDB actually writes.
    """
    crew = [{"id": 93_001, "name": DIRECTOR, "department": "Directing", "job": "Director"}]
    if composer:
        crew.append({"id": 93_002, "name": composer, "department": "Sound",
                     "job": "Original Music Composer"})
    return {
        "cast": [{"id": 93_003, "name": LEAD, "character": "The Lead", "order": 0}],
        "crew": crew,
    }


def tmdb_detail_body(*, tmdb_id: int, name: str, year: int, imdb_id: str,
                     composer: str | None = None, is_movie: bool = True) -> bytes:
    """One TMDB detail payload, in the shape `derive/parse.parse_tmdb_detail` reads.

    `reviews.results` is empty on purpose: the walk title has to reach stage 4 with exactly one
    review source, and TMDB's own reviews would be a second.
    """
    key = "title" if is_movie else "name"
    date = "release_date" if is_movie else "first_air_date"
    body: dict[str, Any] = {
        "id": tmdb_id, key: name, f"original_{key}": name,
        "overview": OVERVIEW, date: f"{year}-06-01", "runtime": 101,
        "original_language": "en", "popularity": 1.0,
        "external_ids": {"imdb_id": imdb_id},
        "keywords": {"keywords": [{"id": 93_004, "name": "exit criterion"}]},
        "reviews": {"page": 1, "results": [], "total_results": 0, "total_pages": 0},
        "videos": {"results": []},
        "alternative_titles": {"titles": []},
    }
    body["credits" if is_movie else "aggregate_credits"] = _tmdb_credits(composer)
    return json.dumps(body).encode("utf-8")


def _binding(value: str, *, uri: bool = False) -> dict[str, str]:
    return {"type": "uri" if uri else "literal", "value": value}


def wikidata_body(imdb_id: str) -> bytes:
    """The SPARQL answer that halves guessing: the Q-id and the two scrape slugs, in one row.

    §8's own words for `wikidata:resolve` are that it "yields MC/RT/Letterboxd slugs", and check 11
    is the measurement of what that is worth: with this answer in hand `rt:page` asks for an
    identifier, and without it the same adapter would build a url out of a name that cannot tell
    two films apart.
    """
    _tmdb, rt_slug, mc_slug, article = IDENTITY[imdb_id]
    row = {
        "item": _binding(f"http://www.wikidata.org/entity/Q93{imdb_id[-6:]}", uri=True),
        "imdb": _binding(imdb_id),
        "rt": _binding(rt_slug),
        "mc": _binding(mc_slug),
        "lb": _binding(f"exit-criterion-{imdb_id[-4:]}"),
        "article": _binding(
            "https://en.wikipedia.org/wiki/" + article.replace(" ", "_"), uri=True
        ),
    }
    return json.dumps({
        "head": {"vars": ["item", "imdb", "rt", "mc", "lb", "tmdb", "article"]},
        "results": {"bindings": [row]},
    }).encode("utf-8")


def omdb_body(imdb_id: str, name: str, year: int) -> bytes:
    return json.dumps({
        "Response": "True", "Title": name, "Year": str(year), "imdbID": imdb_id,
        "Plot": OVERVIEW, "Language": "English", "Country": "United States",
        "imdbRating": "7.4", "imdbVotes": "1,234", "Metascore": "71",
        "Awards": "1 win.", "Ratings": [{"Source": "Internet Movie Database", "Value": "7.4/10"}],
    }).encode("utf-8")


def wikipedia_article_body(article: str) -> bytes:
    """An extract with no Reception section, so Wikipedia contributes no review source.

    `derive/parse.split_sections` is what decides that, and it is why the body is plain text with
    the section headings Wikipedia's `explaintext` extract really uses.
    """
    text = (
        f"{article} is a synthetic film used by an exit criterion.\n\n\n"
        "== Plot ==\n" + OVERVIEW + "\n\n\n"
        "== Production ==\nIt was produced for a measurement and released nowhere.\n"
    )
    return json.dumps({
        "batchcomplete": True,
        "query": {"pages": [{"pageid": 93_005, "title": article, "extract": text}]},
    }).encode("utf-8")


# Every scraped body is padded past `_views.MIN_HTML_BYTES`, which is 512. A page under it is
# stored as a FAILURE by `_views.capture` - the corpus's own rule for a stub or an interstitial -
# so a fixture that fell short would make check 6 measure the length gate instead of the raw store,
# and it would do it silently, because a short page and a 404 look the same from the store's side.
PADDING = (
    "  <p>A synthetic page, written for an exit criterion and padded past the scraped-document\n"
    "  floor on purpose: a page under that floor is stored as a refusal rather than as a\n"
    "  document, so a fixture short of it would leave every check below measuring the length\n"
    "  gate instead of the thing it names. Nothing here is a real page and nothing here was\n"
    "  fetched from a real host; the route table at the top of this file served it.</p>\n"
)


def rt_page_body(name: str, year: int, *, director: str, cast: tuple[str, ...]) -> bytes:
    """A Rotten Tomatoes page, scorecard and `ld+json` identity block.

    THE SLOT NAMES AND THE `ld+json` KEYS ARE THE PARSER'S, and both were got wrong on the first
    attempt at this fixture in ways that were invisible from the outside. `derive/parse._RT_SLOT`
    matches `slot="critics-score"`, hyphenated, and `page_people` reads the singular schema.org
    keys `director` and `actor` - so a page written with `criticsScore` and `actors` yields zero
    score rows and an empty cast, which would have left check 9 refusing a page that had written
    nothing to begin with and reporting it as a refusal that saved something.

    `dateCreated` is the second identity signal `rt_page_year` reads, and the scorecard is scoped
    inside `<media-scorecard>` because the parser scopes to it: the related-titles carousel reuses
    the same markup, and an unscoped scan takes a neighbouring film's percentage.
    """
    people = ",".join(f'{{"@type":"Person","name":"{person}"}}' for person in cast)
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        f"  <title>{name} ({year}) | Rotten Tomatoes</title>\n"
        f"  <meta name=\"description\" content=\"A synthetic page for an exit criterion.\">\n"
        "  <script type=\"application/ld+json\">\n"
        f'  {{"@context":"http://schema.org","@type":"Movie","name":"{name}",'
        f'"dateCreated":"{year}-06-01",'
        f'"director":[{{"@type":"Person","name":"{director}"}}],"actor":[{people}]}}\n'
        "  </script>\n</head>\n<body>\n"
        "  <media-scorecard>\n"
        "    <rt-text slot=\"critics-score\">81%</rt-text>\n"
        "    <rt-link slot=\"critics-reviews\">44 Reviews</rt-link>\n"
        "    <rt-text slot=\"audience-score\">72%</rt-text>\n"
        "    <rt-link slot=\"audience-reviews\">900 Ratings</rt-link>\n"
        "  </media-scorecard>\n"
        "  <section id=\"movie-info\">\n" + PADDING + "  </section>\n"
        "</body>\n</html>\n"
    ).encode("utf-8")


def metacritic_page_body(name: str, year: int) -> bytes:
    """A Metacritic title page: the hero scores, the `__NEXT_DATA__` block, and an identity block.

    THE `ld+json` IS NOT DECORATION. `derive/parse.metacritic_page_year` and `page_people` both
    read it and nothing else, so a page without one is judged by `page_belongs_to_title`'s last
    line - "nothing on the page contradicts us: absence of evidence is not evidence" - and is
    accepted for having said nothing. Check 6 would then be measuring that fallback rather than
    the identity check every real page goes through.
    """
    next_data = json.dumps({"props": {"pageProps": {"component": {"props": {"item": {
        "title": name, "releaseYear": year,
        "production": {"companies": [{"name": "An Exit Criterion Company"}]},
        "credits": [{"name": DIRECTOR, "role": "Director"}, {"name": LEAD, "role": "Cast"}],
    }}}}}})
    identity = json.dumps({
        "@context": "https://schema.org", "@type": "Movie", "name": name,
        "datePublished": f"{year}-06-01",
        "director": [{"@type": "Person", "name": DIRECTOR}],
        "actor": [{"@type": "Person", "name": LEAD}],
    })
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        f"  <title>{name} Reviews - Metacritic</title>\n"
        f"  <meta name=\"description\" content=\"Metascore and user score for {name}.\">\n"
        "  <script type=\"application/ld+json\">\n"
        f"  {identity}\n"
        "  </script>\n</head>\n<body>\n"
        "  <div class=\"c-productHero_scoreInfo\">\n"
        "    <div title=\"Metascore 71 out of 100\"><span>71</span></div>\n"
        "    <div title=\"User score 7.8 out of 10\"><span>7.8</span></div>\n"
        "  </div>\n"
        "  <script id=\"__NEXT_DATA__\" type=\"application/json\">\n"
        f"  {next_data}\n"
        "  </script>\n" + PADDING +
        "</body>\n</html>\n"
    ).encode("utf-8")


def metacritic_critics_body() -> bytes:
    """The one review source the walk title gets, and the reason check 4's park is about sources.

    Two excerpts and comfortably over fifty words between them: decision 335's gate is a
    conjunction, so a body that also failed the word count would leave the park's reason unable to
    show a figure that passed beside one that did not.

    THE SELECTORS ARE `derive/reviews._mc_from_html`'s AND NOT A GUESS AT METACRITIC'S MARKUP.
    That parser's own note is why - "the Tailwind class names on those cards change constantly;
    the `data-testid` attributes have been stable" - so a fixture written against the class names
    would be a fixture about the fallback path, and the primary one would go unmeasured.
    """
    cards = "".join(
        "    <div data-testid=\"review-card\">\n"
        f"      <a data-testid=\"review-card-header\" href=\"/publication/publication-{n}\">"
        f"Publication {n}</a>\n"
        f"      <div title=\"Metascore {80 + n} out of 100\"><span>{80 + n}</span></div>\n"
        f"      <div data-testid=\"review-quote-text\">{quote}</div>\n"
        "    </div>\n"
        for n, quote in enumerate((CRITIC_ONE, CRITIC_TWO), start=1)
    )
    return (
        "<!DOCTYPE html>\n<html lang=\"en\"><head><title>Critic Reviews - Metacritic</title></head>\n"
        "<body>\n<section data-testid=\"critic-reviews\">\n" + cards + "</section>\n" + PADDING +
        "</body></html>\n"
    ).encode("utf-8")


JSON_TYPE = {"content-type": "application/json"}
HTML_TYPE = {"content-type": "text/html; charset=utf-8"}
SPARQL_TYPE = {"content-type": "application/sparql-results+json"}


class Web:
    """A canned web: every request this run makes, answered without opening a socket.

    The log is the assertion surface for three of the eleven checks. Check 11 reads the ORDER two
    requests were made in, check 5 reads that the list did not grow at all, and check 7 reads that
    a 404 from one host did not stop the others being asked. So the handler records first and
    decides afterwards, which is the only order in which a refused request is still a request that
    was made.

    `robots.txt` is served permissively, because two of the eight hosts are crawled with
    `respect_robots=True` (`acquire/hosts.py`) and a measurement about a scorecard must not become
    a measurement about a missing robots file. `acquire/fetch` reads RFC 9309's unreachable case as
    complete disallow, so a double that 404ed it would refuse both scraped sources and every check
    below would be about that instead.
    """

    ROBOTS = b"User-agent: *\nAllow: /\n"

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        # Every non-robots request this web actually ANSWERED, as opposed to 404ed. Check 6's
        # equality is between these and the `raw_document` rows that carry bytes, and the two
        # lists have to be kept apart: a refused request is still a request that was made, and a
        # stored failure row is still not a stored response.
        self.answered: list[str] = []
        # The three routes this run deliberately breaks, set per check rather than declared once:
        # check 7 needs Metacritic absent for ONE title while it answers for the others, check 8
        # needs TMDB's detail absent for one, and check 9 needs one Rotten Tomatoes slug to serve
        # another film. A route table with the holes already in it would make every check
        # downstream of them measure a different web.
        self.missing_metacritic: set[str] = set()
        self.missing_tmdb_detail: set[int] = set()
        self.wrong_page: set[str] = set()

    @property
    def fetched(self) -> list[httpx.Request]:
        """Every request that was not a robots.txt read."""
        return [r for r in self.seen if r.url.path != "/robots.txt"]

    def paths(self) -> list[str]:
        return [f"{r.url.host}{r.url.path}" for r in self.fetched]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=self.ROBOTS,
                                  headers={"content-type": "text/plain"})
        answer = self._route(request)
        if answer is None:
            return httpx.Response(404, content=b"not found", headers=HTML_TYPE)
        status, body, headers = answer
        self.answered.append(f"{request.url.host}{request.url.path}")
        return httpx.Response(status, content=body, headers=headers)

    def _route(self, request: httpx.Request):
        host, path = request.url.host, request.url.path
        if host == "api.themoviedb.org":
            return self._tmdb(path)
        if host == "query.wikidata.org":
            return self._wikidata(request)
        if host == "www.omdbapi.com":
            return self._omdb(request)
        if host == "api.trakt.tv":
            return self._trakt(path)
        if host == "en.wikipedia.org":
            return self._wikipedia(request)
        if host == "api.tvmaze.com":
            return self._tvmaze(request, path)
        if host == "www.rottentomatoes.com":
            return self._rottentomatoes(path)
        if host == "www.metacritic.com":
            return self._metacritic(path)
        return None

    def _tmdb(self, path: str):
        if path.startswith("/3/find/"):
            imdb_id = path.rsplit("/", 1)[-1]
            entry = IDENTITY.get(imdb_id)
            if entry is None:
                return None
            hits = {"id": entry[0], "title": "An Exit Criterion Title"}
            key = "tv_results" if entry[1].startswith("tv/") else "movie_results"
            body = {"movie_results": [], "tv_results": [], "person_results": []}
            body[key] = [hits]
            return 200, json.dumps(body).encode("utf-8"), JSON_TYPE
        for prefix, is_movie in (("/3/movie/", True), ("/3/tv/", False)):
            if not path.startswith(prefix):
                continue
            tmdb_id = int(path.rsplit("/", 1)[-1])
            if tmdb_id in self.missing_tmdb_detail:
                return None
            imdb_id = TMDB_TO_IMDB.get(tmdb_id)
            if imdb_id is None:
                return None
            item = _item_for(imdb_id)
            composer = WRONG_COMPOSER if imdb_id == "tt93000001" else None
            return 200, tmdb_detail_body(
                tmdb_id=tmdb_id, name=item["Name"], year=item["ProductionYear"],
                imdb_id=imdb_id, composer=composer, is_movie=is_movie,
            ), JSON_TYPE
        return None

    def _wikidata(self, request: httpx.Request):
        query = request.url.params.get("query") or ""
        for imdb_id in IDENTITY:
            if imdb_id in query:
                return 200, wikidata_body(imdb_id), SPARQL_TYPE
        return None

    def _omdb(self, request: httpx.Request):
        imdb_id = request.url.params.get("i") or ""
        if imdb_id not in IDENTITY:
            return None
        item = _item_for(imdb_id)
        return 200, omdb_body(imdb_id, item["Name"], item["ProductionYear"]), JSON_TYPE

    def _trakt(self, path: str):
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2 or parts[1] not in IDENTITY:
            return None
        if "comments" in parts:
            # An empty page, which `sources/trakt.py` reads as the last one: three sorts, three
            # requests, no review source. The walk title must reach stage 4 with exactly one.
            return 200, b"[]", JSON_TYPE
        if parts[-1] in ("ratings", "stats"):
            return 200, json.dumps({"rating": 7.4, "votes": 120}).encode("utf-8"), JSON_TYPE
        return 200, json.dumps({
            "title": "An Exit Criterion Title", "year": 2087,
            "ids": {"trakt": 930_000, "slug": "an-exit-criterion-title", "imdb": parts[1]},
        }).encode("utf-8"), JSON_TYPE

    def _wikipedia(self, request: httpx.Request):
        titles = request.url.params.get("titles") or ""
        if titles:
            return 200, wikipedia_article_body(titles), JSON_TYPE
        # The search arm. Unreached on this run - `wikidata:resolve` has already written
        # `title.wikipedia_title` by the time `wikipedia:article` runs, which is priority 25
        # before priority 50 doing its job - and answered anyway, because a route that 404ed
        # would make the ordering silently load-bearing for a check that is not about it.
        search = request.url.params.get("srsearch") or ""
        return 200, json.dumps({
            "query": {"search": [{"title": search.split(" 2")[0]}]},
        }).encode("utf-8"), JSON_TYPE

    def _tvmaze(self, request: httpx.Request, path: str):
        if path == "/lookup/shows":
            imdb_id = request.url.params.get("imdb") or ""
            entry = IDENTITY.get(imdb_id)
            if entry is None:
                return None
            return 200, json.dumps({"id": entry[0], "name": "An Exit Criterion Series"}).encode(
                "utf-8"
            ), JSON_TYPE
        if path.startswith("/shows/"):
            return 200, json.dumps({
                "id": int(path.rsplit("/", 1)[-1]), "name": "An Exit Criterion Series",
                "premiered": "2088-06-01", "summary": OVERVIEW,
                "_embedded": {"cast": [], "crew": [], "seasons": []},
            }).encode("utf-8"), JSON_TYPE
        return None

    def _rottentomatoes(self, path: str):
        slug = path.strip("/")
        imdb_id = _imdb_for_slug(slug, index=1)
        if imdb_id is None:
            return None
        item = _item_for(imdb_id)
        if slug in self.wrong_page:
            # Another film entirely: a different year AND a disjoint cast. Everything else about
            # the page is unchanged - the same scorecard, the same markup - so the only thing
            # `page_belongs_to_title` can be refusing it for is its identity. Both signals are
            # changed because `people_decide=False` for this source: a Rotten Tomatoes page
            # carries an English-dub cast list, so either signal may vouch for it and only a page
            # that fails both is dropped.
            return 200, rt_page_body("A Different Exit Criterion Film", 1974,
                                     director="Another Director",
                                     cast=("Another Lead", "A Second Other Lead")), HTML_TYPE
        return 200, rt_page_body(item["Name"], item["ProductionYear"],
                                 director=DIRECTOR, cast=(LEAD,)), HTML_TYPE

    def _metacritic(self, path: str):
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) < 2:
            return None
        slug = "/".join(parts[:2])
        if slug in self.missing_metacritic:
            return None
        imdb_id = _imdb_for_slug(slug, index=2)
        if imdb_id is None:
            return None
        item = _item_for(imdb_id)
        if parts[-1] == "critic-reviews":
            return 200, metacritic_critics_body(), HTML_TYPE
        if parts[-1] == "user-reviews":
            return None
        return 200, metacritic_page_body(item["Name"], item["ProductionYear"]), HTML_TYPE


ITEMS = (ITEM_WALK, ITEM_EIGHT, ITEM_NOTE, ITEM_REQUIRED, ITEM_WRONG)


def _item_for(imdb_id: str) -> dict[str, Any]:
    return next(item for item in ITEMS if item["ProviderIds"]["Imdb"] == imdb_id)


def _imdb_for_slug(slug: str, *, index: int) -> str | None:
    return next((imdb for imdb, entry in IDENTITY.items() if entry[index] == slug), None)


class Clock:
    """A clock the run advances itself, so a real rate limit costs no real time.

    `acquire/hosts.py` gives Rotten Tomatoes and Metacritic seven tenths of a request a second with
    a burst of one, which is what the corpus crawled them at without being blocked. Those numbers
    are not the thing to change for a harness: the `Fetcher` takes `clock`, `sleep` and `jitter`
    precisely so a measurement can be tolerant while the policy stays honest. The spin limit is the
    other half - a bucket that never hands out a token would otherwise hang this script forever
    instead of reporting a failed check.
    """

    _SPIN_LIMIT = 4000

    def __init__(self) -> None:
        self.now = 0.0
        self.waits = 0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits += 1
        if self.waits > self._SPIN_LIMIT:
            raise PreconditionFailed(
                f"the fetcher paced {self._SPIN_LIMIT} times without finishing; last wait "
                f"{seconds:.2f}s"
            )
        self.now += seconds

    @staticmethod
    def jitter(low: float, _high: float) -> float:
        """Pinned to the bottom of the band, so a wait is a number and not a distribution."""
        return low


class PreconditionFailed(RuntimeError):
    """What a check needed and did not get, said as a sentence rather than as a traceback."""


@dataclass
class Install:
    """The one install every check below measures, and what the checks hand each other."""

    conn: asyncpg.Connection
    work: Path
    bundle_root: Path
    web: Web
    clock: Clock
    negative_control: bool = False
    version: str = ""
    # The corpus title the shipped corrections ledger names, and the composer it settles on.
    corrected_id: int = 0
    corrected_name: str = ""
    # The corpus title the shipped adjudications ledger rules on, and the tag the verdict removes.
    ruled_id: int = 0
    ruled_term: str = ""
    # Stage 1's output per item, so a later check measures the row an earlier one produced rather
    # than whatever `max(id)` happens to be by the time it runs.
    minted: dict[str, int] = field(default_factory=dict)
    # How many times a `fetch.Fetcher` was built. Check 5's whole assertion: a drain that never
    # reaches a stage declaring `fetches` constructs no HTTP client at all (decision 373).
    fetchers_built: int = 0
    walk_park: dict[str, Any] = field(default_factory=dict)
    walk_paths: list[str] = field(default_factory=list)


# --- reporting ------------------------------------------------------------------------------------


UNMEASURED: list[tuple[str, str]] = []


def not_measured(number: int, reason: str) -> tuple[None, str]:
    """Record a check this run could not measure. Not a pass, not a failure, and never silent.

    Decision 184's rule applied to a harness: a published figure is one a run produced. Check 3
    disables a curated-ledger applier to show a verdict revert, which decision 378 puts behind an
    explicit flag, and a run without the flag must not report the absence as a verdict in either
    direction - a green 11/11 from a run that disabled nothing is exactly the certificate this
    family of guards exists to refuse.

    The number is carried rather than the label so the wording lives in `CHECKS` alone, which is
    the list the tally and the failure summary both read.
    """
    label = next(f"{n}. {title}" for n, title in CHECKS if n == number)
    UNMEASURED.append((label, reason))
    print(f"  [----] {console(label)}", flush=True)
    print(f"         NOT MEASURED HERE: {console(reason)}", flush=True)
    return None, ""


# --- the install ----------------------------------------------------------------------------------


def _dsn_from_env_test() -> str | None:
    """`.env.test`'s TEST_DATABASE_URL, read the way `backend/tests/conftest.py` reads it.

    The same convenience and the same limit as the other exit scripts: an untracked file names the
    server, so the line below is the only place this script decides which host it is allowed to
    create a database on.
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
    """Take the operator's own connector credentials away before anything reads them.

    `backend/tests/conftest.py` records the incident this repeats: a household that followed its
    own README runs this script with their real Jellyfin and TMDB configured, and their encrypted
    credentials would be written into a throwaway database under a throwaway key. Derived from the
    connectors `registry.CONNECTORS` seeds, so a connector added there is neutralised here without
    anyone remembering this list. It used to say "derived from `Settings`" over four M0-era
    prefixes, and the three provider keys M5.5 seeds (§2, M5.5 plan A3) passed straight through it
    into the `registry.seed_from_env` call `build_install` makes. [M5.5 review cycle 1, KEYS-C1-03,
    M55-DOC-07]

    It matters more in this script than in any other. `acquire/hosts.policy_for` takes a
    `jellyfin_host` and exempts it from the throttle and the robots check, and a run that inherited
    a real one could exempt a host the pacing assertions believe they are measuring. And these are
    the three keys §8 stage 2 actually spends: a run that kept the household's TMDB key would put
    it in `raw_document.url`, which is where TMDB's API insists it goes.

    BOTH HALVES, because a name is only one of the two ways those credentials arrive.
    `Settings.model_config` declares `env_file=".env"`, resolved from the working directory at
    construction, and `.env.example` documents JELLYFIN_URL and JELLYFIN_API_KEY as exactly what
    an operator writes into that file - so popping a name that was never in `os.environ` does
    nothing to the file this script is most likely to be run beside, and only the three keys
    `_seed_throwaway_credentials` overwrites outright are safe from it. The move is into a
    directory of this run's own DATA_DIR, set by `main` on the line above this call and removed
    in its `finally` - which chdirs back out first, because Windows will not delete the tree a
    process is standing in. `backend/tests/conftest.py:481-490` is this pair of statements for
    this reason, and its own docstring records the second site that got one half only; this was
    the third. [M5.3 review cycle 1, M53-EXIT-04]
    """
    seeded = tuple(f"{name}_" for name, spec in registry.CONNECTORS.items() if spec.seeded)
    # Matched case-insensitively, as `Settings` matches it: pydantic-settings reads the environment
    # with `case_sensitive` False, and a POSIX environment keeps `openai_api_key` apart from
    # `OPENAI_API_KEY`, so popping the upper-case spelling alone left a lower-case key for the seed.
    # [M5.5 review cycle 2, M55-KEYS-C2-04]
    wanted = {name.upper() for name in core_config.Settings.model_fields if name.startswith(seeded)}
    for variable in list(os.environ):
        if variable.upper() in wanted:
            os.environ.pop(variable, None)
    neutral = Path(os.environ["DATA_DIR"]) / "no-dot-env"
    neutral.mkdir(parents=True, exist_ok=True)
    os.chdir(neutral)


def _seed_throwaway_credentials() -> None:
    """Three keys of this run's own, so all eight of §8 stage 2's sources are asked.

    Decision 377 filters a source whose credential is absent out of `available_kinds` entirely, and
    §3.1 makes that a legal half-configured install rather than a broken one - so a run with no
    keys would measure five sources and check 6 would be about the word "eight" rather than about
    the raw store. These three are set AFTER `_neutralise_connector_env` and never leave the
    process except into the `MockTransport` and into a scratch database that is dropped.
    """
    os.environ["TMDB_API_KEY"] = "m53-exit-criterion-tmdb-key-not-a-real-one"
    os.environ["OMDB_API_KEY"] = "m53-exit-criterion-omdb-key-not-a-real-one"
    os.environ["TRAKT_CLIENT_ID"] = "m53-exit-criterion-trakt-client-id-not-a-real-one"


async def build_install(conn: asyncpg.Connection, bundle_root: Path, work: Path,
                        *, negative_control: bool) -> Install:
    """Import the corpus bundle and make the install this run measures. Raises on a refusal.

    Everything the eleven checks need and nothing they do not: the content, the artifacts staged
    under the temporary DATA_DIR, the active `artifact_bundle` row, the curated ledgers the import
    carries, and the three connector rows decision 377's reader asks for. No admin account and no
    HTTP client, because no check here goes through a route.
    """
    bundle = bundle_import.Bundle.open(bundle_root)
    began = time.perf_counter()
    report = await bundle_import.import_bundle(conn, bundle, work / "artifacts")
    fails = [f"{f.rule}: {f.message[:200]}" for f in report.findings if f.severity == "fail"]
    if not report.ok or fails:
        raise PreconditionFailed(
            "the bundle did not import, so there is no install to measure: " + "; ".join(fails[:3])
        )
    version = await conn.fetchval("SELECT version FROM artifact_bundle WHERE state = 'active'")
    if not version:
        raise PreconditionFailed("the import left no active artifact_bundle row")
    seeded = await registry.seed_from_env(conn)
    titles = await conn.fetchval("SELECT count(*) FROM title")
    corrections = await conn.fetchval("SELECT count(*) FROM credit_correction")
    adjudications = await conn.fetchval("SELECT count(*) FROM dna_adjudication")
    print(
        f"  imported {console(str(version))} in {time.perf_counter() - began:.0f}s: "
        f"{titles:,} titles, {corrections} credit correction(s), "
        f"{adjudications} adjudication(s), connectors {', '.join(sorted(seeded)) or 'none'}",
        flush=True,
    )
    return Install(
        conn=conn, work=work, bundle_root=bundle_root, web=Web(), clock=Clock(),
        negative_control=negative_control, version=str(version),
    )


async def choose_subjects(ctx: Install) -> None:
    """The two corpus titles the curated ledgers actually rule on, chosen from the ledgers.

    NOT NAMED AS CONSTANTS, and that is the difference between a measurement and an anecdote. The
    shipped `corrections_v1.tsv` names six titles and `adjudications_v1.tsv` 817; which ids those
    are is the bundle's business and changes with every export, so a script carrying two hard-coded
    numbers would start silently measuring a title with no curated row the first time the corpus
    re-exported - and a derive that applies nothing passes "the correction is still there" without
    a word.
    """
    row = await ctx.conn.fetchrow(
        "SELECT c.title_id, c.new_value FROM credit_correction c"
        "  JOIN title t ON t.id = c.title_id"
        " WHERE c.field = 'composer' AND c.origin = 'bundle' AND btrim(coalesce(c.evidence,'')) <> ''"
        " ORDER BY c.title_id LIMIT 1"
    )
    if row is None:
        raise PreconditionFailed(
            "this install carries no bundle-authored composer correction over a title it holds, so "
            "there is nothing for checks 1 and 2 to measure: the derive would apply an empty "
            "ledger and pass"
        )
    ctx.corrected_id, ctx.corrected_name = int(row["title_id"]), str(row["new_value"])

    # `drop` alone, and not every verdict the ledger spells. `derive/ledgers._VERDICTS` folds three
    # more - `repoint`, `rename`, `drop_evidence` - and only a drop makes the tag ROW disappear,
    # which is the one revert a count can show. A re-point leaves a row behind under another term
    # and `drop_evidence` leaves the tag standing by design, so a subject chosen off either would
    # make the negative control report a difference it could not attribute.
    ruled = await ctx.conn.fetchrow(
        "SELECT a.title_id, a.term FROM dna_adjudication a"
        "  JOIN dna_tag g ON g.title_id = a.title_id AND g.term = a.term AND g.version = a.version"
        " WHERE a.scope = 'title' AND lower(btrim(a.verdict)) = 'drop'"
        " ORDER BY a.title_id, a.term LIMIT 1"
    )
    if ruled is None:
        raise PreconditionFailed(
            "no shipped per-title adjudication names a tag this install actually carries, so the "
            "negative control would show nothing reverting"
        )
    ctx.ruled_id, ctx.ruled_term = int(ruled["title_id"]), str(ruled["term"])
    print(
        f"  subjects: title {ctx.corrected_id} (corrections ledger says the composer is "
        f"{console(ctx.corrected_name)}), title {ctx.ruled_id} (adjudications ledger rules on "
        f"{console(ctx.ruled_term)})",
        flush=True,
    )


# --- the raw store, the stages and the derived snapshot -------------------------------------------


# Every table §8 stage 3 writes, plus the two the ledgers touch. A snapshot is the whole of these
# rows and not a count: check 2's sentence is "row counts and values identical", and two derives
# that wrote the same NUMBER of different credits is precisely the failure a count cannot see.
DERIVED_TABLES = (
    "title_meta", "credit", "title_alias", "title_genre", "title_keyword",
    "title_language", "title_country", "title_company", "title_video", "award",
)


async def derived_snapshot(conn: asyncpg.Connection, title_id: int) -> dict[str, list[tuple]]:
    """Every derived row this title carries, ordered so that two runs are comparable.

    The primary key is deliberately NOT read. `credit` and `review` carry `bigserial` ids, so a
    re-derive gives every row a new one and a snapshot including them could never be equal;
    `derive/rebuild.py`'s own docstring says what must not move instead - the values, and the order
    a card reads them in, which it writes into `credit.billing_order` from the document's billing.

    `display.platform_rating`'s score column is `score` and not `value`: `value` is the corpus
    BUNDLE's name for it, mapped on the way in by `importer/load.py:283`, and the app's own column
    has been `score` since `0003_content.sql:177-184`. The bundle's spelling is the one that comes
    to hand while reading a snapshot, and the statement it makes cannot execute at all.
    [M5.3 review cycle 1, M53-EXIT-01]
    """
    out: dict[str, list[tuple]] = {}
    for table in DERIVED_TABLES:
        columns = [
            r["column_name"] for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = $1 AND column_name <> 'id'"
                " ORDER BY ordinal_position",
                table,
            )
        ]
        if not columns:
            continue
        names = ", ".join(f'"{c}"' for c in columns)
        rows = await conn.fetch(
            f"SELECT {names} FROM {table} WHERE title_id = $1 ORDER BY {names}", title_id
        )
        out[table] = [tuple(str(value) for value in row) for row in rows]
    out["review"] = [
        tuple(str(v) for v in row) for row in await conn.fetch(
            "SELECT source, author, url, rating, is_critic, body, word_count"
            "  FROM review_store.review WHERE title_id = $1"
            " ORDER BY source, author, body",
            title_id,
        )
    ]
    out["platform_rating"] = [
        tuple(str(v) for v in row) for row in await conn.fetch(
            "SELECT platform, metric, score, scale FROM display.platform_rating"
            " WHERE title_id = $1 ORDER BY platform, metric",
            title_id,
        )
    ]
    return out


def snapshot_differences(before: dict[str, list[tuple]],
                         after: dict[str, list[tuple]]) -> list[str]:
    """Which tables moved between two derives, as lines an operator can act on."""
    moved: list[str] = []
    for table in sorted(set(before) | set(after)):
        first, second = before.get(table, []), after.get(table, [])
        if first != second:
            moved.append(f"{table}: {len(first)} row(s) -> {len(second)} row(s)")
    return moved


async def stage_document(ctx: Install, title_id: int, *, source: str, kind: str, url: str,
                         content: bytes) -> int:
    """Put one document into the raw store for a title the corpus shipped.

    THE CORPUS BUNDLE SHIPS NO `data/raw/`, which the plan states in its own "does not do" section:
    the nineteen thousand titles' bytes do not exist and cannot be re-parsed, so this app's raw
    store starts empty and a derive of a corpus title has nothing to read. The two ledger checks
    need one anyway - a derive that regenerates NOTHING cannot bury a curated fix - so the document
    is written through `rawstore.store`, which is the app's own writer and not a hand-built INSERT,
    and filed under the key `pipeline.enqueue_title` produces because `derive/rebuild._DOCUMENTS`
    joins exactly that.

    THE TASK IS CLOSED THE MOMENT IT EXISTS, and that is not tidying. It is here to KEY the
    documents - decision 322's seam, a queue row that can exist before its title does - and not to
    be walked: left pending, the next drain in this run would lease it, put a corpus title through
    stage 1 to 10 and fill the request log with a walk no check asked for. Check 11 reads that log.
    """
    await pipeline.enqueue_title(ctx.conn, title_id)
    task_id = await ctx.conn.fetchval(
        "SELECT id FROM acquisition_task WHERE kind = $1 AND key = $2",
        pipeline.TASK_KIND, f"title:{title_id}",
    )
    if task_id is None:
        raise PreconditionFailed(f"no acquisition task was created for title {title_id}")
    await queue.complete(
        ctx.conn, int(task_id),
        "staged by the exit criterion: this title is derived directly, not walked",
    )
    return await rawstore.store(
        ctx.conn, source=source, kind=kind, url=url, content=content,
        entity_key=f"title:{title_id}", content_type="application/json",
    )


def build_fetcher(ctx: Install):
    """`pipeline.drain`'s `fetcher_factory`, wired to the canned web. Decision 373's seam.

    It counts as well as builds, because check 5's assertion is about the call that never happens:
    a drain whose one task resumes at stage 4 reaches no stage declaring `fetches`, so `_OneFetcher`
    is never awaited and no `httpx.AsyncClient` is constructed at all. That is measurable from
    outside the driver, which is what makes it a check rather than an argument.
    """

    async def factory(conn: asyncpg.Connection) -> fetch.Fetcher:
        ctx.fetchers_built += 1
        return fetch.Fetcher(
            conn=conn, transport=httpx.MockTransport(ctx.web.handler),
            clock=ctx.clock, sleep=ctx.clock.sleep, jitter=ctx.clock.jitter,
        )

    return factory


async def walk_item(ctx: Install, item: dict[str, Any]) -> pipeline.TaskReport:
    """Enqueue one Jellyfin item, drain, and hand back that item's own report.

    By key and not by position: `drain` leases up to eight and the batch may carry a task an
    earlier check left pending, so a report picked off the front of the list is a report about
    whatever the queue's ORDER BY chose.
    """
    key = pipeline.key_for_item(item)
    if not await pipeline.enqueue_item(ctx.conn, item):
        raise PreconditionFailed(f"the queue already held a task keyed {key!r}")
    report = await pipeline.drain(ctx.conn, fetcher_factory=build_fetcher(ctx))
    found = next((task for task in report.tasks if task.key == key), None)
    if found is None:
        raise PreconditionFailed(
            f"the drain leased {report.leased} task(s) and none of them was {key!r}"
        )
    if found.title_id is None:
        raise PreconditionFailed(f"stage 1 established no title for {key!r}")
    ctx.minted[str(item["Id"])] = int(found.title_id)
    return found


async def run_enrich(ctx: Install, item: dict[str, Any]) -> tuple[int, stages.Outcome]:
    """Stage 1 then stage 2 alone, through the two calls `run_task` makes, and no further.

    THE DRIVER OFFERS NO SEAM THAT STOPS AFTER STAGE 2, and three checks need exactly that state:
    check 6's sentence is "before any parse", check 7's is that a 404 leaves stage 2 ADVANCING, and
    check 8's is that stage 2 PARKS. Running a whole drain and reading the board afterwards would
    answer none of the three - by then stage 3 has parsed everything stage 2 stored, and an advance
    is indistinguishable from a walk that went further. So the two stages are called directly, in
    the order and with the context `pipeline.run_task` builds, which is the same manoeuvre
    `ops/m51_exit_criterion.py` makes for its killed-worker check and for the same reason.
    """
    key = pipeline.key_for_item(item)
    if not await pipeline.enqueue_item(ctx.conn, item):
        raise PreconditionFailed(f"the queue already held a task keyed {key!r}")
    leased = [t for t in await queue.lease(ctx.conn, [pipeline.TASK_KIND], limit=8) if t.key == key]
    if not leased:
        raise PreconditionFailed(f"the queue would not lease {key!r}")
    task = leased[0]
    identified = await stages.identify(stages.StageContext(conn=ctx.conn, task=task))
    if identified.title_id is None:
        raise PreconditionFailed(f"stage 1 established no title for {key!r}: {identified.reason}")
    title_id = int(identified.title_id)
    # `run_task`'s own second call, and it is not optional. `enqueue_item`'s payload carries the
    # ITEM and not a title id, because there may be no title yet; `_remember_title` is what records
    # which one stage 1 turned out to mean, and `derive/rebuild._DOCUMENTS` finds a title's
    # documents through exactly that payload key. Skip it and stage 2 stores bytes that stage 3
    # cannot see - which would make check 9's derive read nothing and pass for the wrong reason.
    await pipeline._remember_title(ctx.conn, task, title_id)
    ctx.minted[str(item["Id"])] = title_id
    factory = build_fetcher(ctx)
    async with await factory(ctx.conn) as fetcher:
        stage_ctx = stages.StageContext(conn=ctx.conn, task=task, title_id=title_id)
        stage_ctx.fetcher = fetcher
        outcome = await stages.enrich(stage_ctx)
    # Closed here for `stage_document`'s reason: a task left leased is one `queue.reclaim_expired`
    # hands to the next drain, and the next drain is check 5's, whose whole assertion is that it
    # opened no socket.
    await queue.complete(ctx.conn, task.id, "stage 2 measured directly by the exit criterion")
    return title_id, outcome


# --- 1: the corrected credit ----------------------------------------------------------------------


async def check_one(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 1: a title carrying a shipped `credit_correction` row is re-derived from its raw
    documents and the corrected credit is still there afterwards.

    THIS IS §14.5's SCAR, AND IT IS INVISIBLE UNLESS THE DERIVE HAS SOMETHING TO BURY. The staged
    TMDB document credits `WRONG_COMPOSER`, so the derive genuinely writes the wrong music credit
    out of the raw store and `apply_corrections` genuinely has to replace it. A run against a title
    with no document, or against a document with no composer, would report "the corrected credit is
    present" over a derive that wrote nothing at all - which is the vacuous pass the milestone's own
    risks section warns about in its first sentence.

    BOTH DIRECTIONS ARE ASSERTED. The corrected name being present is half the claim; the other
    half is that the name the source got wrong is GONE, because a correction that added a row
    beside the bad one would satisfy the first half and leave a card naming two composers.

    IT OVERWRITES THE BUNDLE'S OWN TMDB CREDITS FOR THIS TITLE, on purpose. Decision 375 scopes the
    derive's delete to `(title_id, source)` and the importer files the corpus's credits under the
    same source names, so the staged document really does bury what the import wrote - which is
    exactly the sequence §14.5 describes and the one an acquired title's every re-derive performs.
    A harness that arranged for the derive not to touch anything would be measuring a derive that
    could not have reverted the fix in the first place.
    """
    await stage_document(
        ctx, ctx.corrected_id, source="tmdb", kind="movie_detail",
        url=f"https://api.themoviedb.org/3/movie/93{ctx.corrected_id}",
        content=tmdb_detail_body(tmdb_id=93_000, name="A Corpus Title This Run Re-derives",
                                 year=2001, imdb_id="tt9300001", composer=WRONG_COMPOSER),
    )
    report = await rebuild.derive_title(ctx.conn, ctx.corrected_id)
    music = await ctx.conn.fetch(
        "SELECT p.name, c.source FROM credit c JOIN person p ON p.id = c.person_id"
        " WHERE c.title_id = $1 AND (lower(c.job) LIKE '%composer%' OR lower(c.job) LIKE '%music%')"
        " ORDER BY p.name",
        ctx.corrected_id,
    )
    names = [str(row["name"]) for row in music]
    ok = (
        names == [ctx.corrected_name]
        and any(str(row["source"]) == ledgers.CORRECTION_SOURCE for row in music)
        and report.corrections.get("rows", 0) >= 1
        and report.rows.get("credit", 0) >= 1
    )
    detail = (
        f"title {ctx.corrected_id}: the derive wrote {report.rows.get('credit', 0)} credit(s) from "
        f"{len(report.documents)} document(s); music credits afterwards "
        f"{console(', '.join(names) or '(none)')}; adjudications {report.adjudications}; "
        f"corrections {report.corrections}"
    )
    return ok, detail


# --- 2: the second derive -------------------------------------------------------------------------


async def check_two(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 2: deriving the same title again changes nothing.

    Decision 375's idempotence mechanism, measured rather than argued: the derive deletes by
    `(title_id, source)` for the sources this run re-derived and re-inserts, so a second pass must
    leave every derived table byte for byte where it was. The snapshot is over VALUES and not over
    counts, because two derives that wrote the same number of different credits is the failure a
    count cannot see, and it excludes `bigserial` ids, which move by design.

    THE SECOND DERIVE REPORTS `replaced` AND NOT `already_correct`, WHICH IS THE MECHANISM WORKING
    AND NOT A DEFECT - and it is worth stating because the opposite is the obvious expectation.
    The delete scope is `(title_id, source)` for the sources THIS RUN re-derived, which is `tmdb`;
    the corrected row's source is `correction`, so it survives the delete and the wrong composer is
    re-inserted from the raw store beside it. `apply_corrections` then sees two music credits, not
    one, takes its `replaced` arm and leaves exactly the ledger's name standing. `already_correct`
    is reachable only when the derive read no document naming a composer at all. What must be
    identical is the END STATE, and that is what the snapshot compares.
    """
    before = await derived_snapshot(ctx.conn, ctx.corrected_id)
    report = await rebuild.derive_title(ctx.conn, ctx.corrected_id)
    after = await derived_snapshot(ctx.conn, ctx.corrected_id)
    moved = snapshot_differences(before, after)
    ok = (
        not moved
        and report.corrections.get("rows", 0) >= 1
        and report.rows.get("credit", 0) >= 1
    )
    detail = (
        f"{sum(len(rows) for rows in after.values())} derived row(s) across "
        f"{len(after)} table(s); the derive rewrote {report.rows.get('credit', 0)} credit(s); "
        f"corrections {report.corrections}; "
        + (f"MOVED: {'; '.join(moved)}" if moved else "nothing moved")
    )
    return ok, detail


# --- 3: the negative control ----------------------------------------------------------------------


async def check_three(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 3: with the adjudication applier disabled, a curated DNA verdict visibly reverts.

    THE ONLY CHECK HERE THAT SHOWS THE FAILURE RATHER THAN THE FIX, and the plan says why in one
    sentence: "§14.5's scar is invisible unless you can show the failure once". Every other check on
    this list passes against a derive that applies the ledgers and would also pass against one that
    happened not to need to; this one demonstrates the afternoon §14.5 records, where 787 rows were
    reverted twice by a rebuild nobody thought was doing anything.

    HOW THE REVERT IS STAGED. The tag the shipped ledger drops for this title is put back exactly
    as the tier's OWN WRITER wrote it, and the derive is then run with `ledgers.apply_adjudications`
    replaced by a function that returns an empty result. The tag is still there afterwards, with no
    failure, no count on §6.6's board and nothing in the log: that silence is the finding.

    WHICH WRITER, SAID PRECISELY, because this paragraph used to name the importer's recurring path
    and `importer/bundle.py` forbids exactly that: `load_tags` sits inside the seed-only
    `if db is not None:` branch, whose `else:` names it among the content tiers decision 162 will
    not let a bundle load twice, and `seed-once` refuses a second content bundle. The extracted
    tier therefore has two writers: the ONE seed import, before any derive, and M5.4's stage 8
    after it. The INSERT below stands in for stage 8, which is the event this control rehearses,
    and decision 376 is what makes the applier rule over those rows whatever wrote them. Check 10
    is where a models-only import belongs, and it is exact about the two curated ledgers one
    touches. [M5.3 review cycle 1, M53-C1-SCAR-02]

    OFF BY DEFAULT, ON A SCRATCH DATABASE, AND PUT BACK IN A `finally` (decision 378). The whole
    run already owns a database it created and drops, so "a scratch database" costs nothing extra;
    what the `finally` buys is that a crash inside the derive cannot leave a household's process
    holding an applier that does nothing.
    """
    if not ctx.negative_control:
        return not_measured(
            3,
            "the negative control is off by default (decision 378): re-run with "
            "--negative-control to disable the adjudication applier for one derive",
        )
    version = await ctx.conn.fetchval(
        "SELECT version FROM dna_adjudication WHERE title_id = $1 AND term = $2 LIMIT 1",
        ctx.ruled_id, ctx.ruled_term,
    )
    # The row as the SEED IMPORT wrote it, taken whole before anything rules on it. Re-inserting a
    # tag this script invented would be a different row - `dna_tag.facet` is NOT NULL and
    # `salience` is CHECKed to 1, 2 or 3 - and, more to the point, the sentence under test is
    # "the ledger rules on the tier every derive, whatever wrote the rows". What an extractor puts
    # there is this.
    original = await ctx.conn.fetchrow(
        "SELECT facet, salience, confidence, n_sources, provider FROM dna_tag"
        " WHERE title_id = $1 AND term = $2 AND version = $3 ORDER BY id LIMIT 1",
        ctx.ruled_id, ctx.ruled_term, version,
    )
    if original is None:
        raise PreconditionFailed(
            f"title {ctx.ruled_id} no longer carries the tag {ctx.ruled_term!r} the ledger rules "
            "on, so there is nothing for the control to show reverting"
        )
    await rebuild.derive_title(ctx.conn, ctx.ruled_id)
    after_applier = await ctx.conn.fetchval(
        "SELECT count(*) FROM dna_tag WHERE title_id = $1 AND term = $2 AND version = $3",
        ctx.ruled_id, ctx.ruled_term, version,
    )

    # The extractor, in the one statement that matters here: the tag the ledger just removed is
    # back on the row, exactly as the seed import wrote it, which is what makes the ledger's job a
    # job at all - and what M5.4's stage 8 will do to this tier on every acquisition.
    await ctx.conn.execute(
        "INSERT INTO dna_tag (title_id, version, term, facet, salience, confidence, n_sources,"
        "                     provider)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
        " ON CONFLICT (title_id, version, term, provider) DO NOTHING",
        ctx.ruled_id, version, ctx.ruled_term, original["facet"], original["salience"],
        original["confidence"], original["n_sources"], original["provider"],
    )
    real = ledgers.apply_adjudications

    async def disabled(_conn: asyncpg.Connection, _title_id: int) -> dict[str, int]:
        return {}

    try:
        ledgers.apply_adjudications = disabled
        await rebuild.derive_title(ctx.conn, ctx.ruled_id)
    finally:
        ledgers.apply_adjudications = real
    reverted = await ctx.conn.fetchval(
        "SELECT count(*) FROM dna_tag WHERE title_id = $1 AND term = $2 AND version = $3",
        ctx.ruled_id, ctx.ruled_term, version,
    )
    restored = ledgers.apply_adjudications is real
    ok = int(after_applier) == 0 and int(reverted) >= 1 and restored
    detail = (
        f"title {ctx.ruled_id}, term {console(ctx.ruled_term)} (facet "
        f"{console(str(original['facet']))}, salience {original['salience']}, provider "
        f"{console(str(original['provider']) or '(none)')}) under vocabulary "
        f"{console(str(version))}: the ledger removed it ({after_applier} left), and with the "
        f"applier disabled a re-extracted copy survived the derive ({reverted} left) with no "
        f"failure, no count and nothing on the board. The applier is "
        f"{'back in place' if restored else 'STILL DISABLED'}"
    )
    return ok, detail


# --- 4: the reviews gate's park -------------------------------------------------------------------


async def check_four(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 4: `acquisition_job` at stage 4, `reason` carrying both counts, `retry_after`
    about thirty days out.

    DECISION 336's PARK WITH A DEADLINE, WHICH IS THE ONE THIS WHOLE MILESTONE TURNS ON. `parked`
    never auto-fails and a park with no deadline is terminal - `queue.skip` closes the task for
    good - so stage 4's thirty days is a defer, not a skip and not a failure. The two writes are
    asserted against EACH OTHER rather than each against thirty days: `pipeline._record_stop` passes
    one instant to `queue.defer` and to `write_board`, so a milestone that put two different dates
    in front of an operator is what this compares for.

    THE REASON IS A PRODUCT SURFACE. `0005_ledger.sql:138` says `acquisition_job.reason` is "shown
    verbatim on the admin board", and decision 335 requires both counts in it, so the check reads
    the figures out of the sentence rather than trusting that the sentence exists.
    """
    before = len(ctx.web.fetched)
    report = await walk_item(ctx, ITEM_WALK)
    # The walk's OWN request log, sliced here rather than read globally in check 11. By the time
    # that check runs, four more titles have been through stage 2 and the log holds their Rotten
    # Tomatoes pages too - so "the only RT url this run asked for was the slug Wikidata gave" is a
    # sentence about one walk or it is a sentence about nothing.
    ctx.walk_paths = ctx.web.paths()[before:]
    title_id = int(report.title_id or 0)
    job = await ctx.conn.fetchrow(
        "SELECT stage, status, reason, retry_after FROM acquisition_job WHERE title_id = $1",
        title_id,
    )
    if job is None:
        raise PreconditionFailed(f"the walk left no board row for title {title_id}")
    task_due = await ctx.conn.fetchval(
        "SELECT next_attempt_at FROM acquisition_task WHERE kind = $1 AND key = $2",
        pipeline.TASK_KIND, report.key,
    )
    counts = await gate.measure(ctx.conn, title_id)
    reason = str(job["reason"] or "")
    retry_after = job["retry_after"]
    days = (retry_after - datetime.now(UTC)).total_seconds() / 86400.0 if retry_after else 0.0
    ctx.walk_park = {"title_id": title_id, "key": report.key, "retry_after": retry_after}
    ok = (
        int(job["stage"]) == 4
        and str(job["status"]) == pipeline.PARKED
        and counts.has_plot
        and f"{counts.sources} source" in reason
        and f"{counts.words} word" in reason
        and retry_after is not None
        and abs(days - gate.REVIEW_WINDOW.days) <= WINDOW_TOLERANCE_HOURS / 24.0
        and task_due == retry_after
    )
    detail = (
        f"title {title_id}: stage {job['stage']} {job['status']}; plot {counts.has_plot}, "
        f"{counts.sources} source(s), {counts.words} word(s); retry_after {days:.2f} days out "
        f"(window {gate.REVIEW_WINDOW.days}); queue agrees: {task_due == retry_after}; "
        f"reason {console(reason)}"
    )
    return ok, detail


# --- 5: the retry ---------------------------------------------------------------------------------


async def check_five(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 5: the admin retry resumes at stage 4, makes no outbound request, and duplicates
    no derived row.

    WHAT AN ADMIN RETRY IS, TODAY. §6.6's retry control is M5.6's and `acquire/board.py` says so in
    its first paragraph - "a read module that also knew how to retry" is the thing it refuses to be
    - so what this check drives is the one state change that control will make: the deferred task
    is made due now. Everything after that is the shipped driver, which is the half the criterion is
    about: `_resume_index` reads the BOARD, finds stage 4, and re-enters there rather than at stage
    1.

    THE ZERO IS MEASURED TWICE, from inside and outside. `Web.fetched` is every request that left
    this process, and `Install.fetchers_built` is how many times the drain asked for a fetcher at
    all - and the second is the stronger statement, because decision 373 makes a drain that reaches
    no fetching stage construct no `httpx.AsyncClient` in the first place. A check that only counted
    requests would pass on a driver that built a client, opened a connection pool and happened not
    to use it.
    """
    park = ctx.walk_park
    if not park:
        raise PreconditionFailed("check 4 established no parked walk, so there is nothing to retry")
    title_id = int(park["title_id"])
    before = await derived_snapshot(ctx.conn, title_id)
    requests_before = len(ctx.web.fetched)
    fetchers_before = ctx.fetchers_built
    await ctx.conn.execute(
        "UPDATE acquisition_task SET next_attempt_at = now() WHERE kind = $1 AND key = $2",
        pipeline.TASK_KIND, park["key"],
    )
    report = await pipeline.drain(ctx.conn, fetcher_factory=build_fetcher(ctx))
    task = next((t for t in report.tasks if t.key == park["key"]), None)
    if task is None:
        raise PreconditionFailed(
            f"the retry drain leased {report.leased} task(s) and none of them was {park['key']!r}"
        )
    after = await derived_snapshot(ctx.conn, title_id)
    moved = snapshot_differences(before, after)
    walked = list(task.stages_run)
    ok = (
        walked[:1] == ["reviews gate"]
        and len(ctx.web.fetched) == requests_before
        and ctx.fetchers_built == fetchers_before
        and not moved
    )
    detail = (
        f"title {title_id} resumed at {console(', '.join(walked) or '(nothing)')}; "
        f"{len(ctx.web.fetched) - requests_before} request(s) left the process and "
        f"{ctx.fetchers_built - fetchers_before} fetcher(s) were built; "
        + (f"MOVED: {'; '.join(moved)}" if moved else "no derived row moved")
    )
    return ok, detail


# --- 6: the raw store before the parse ------------------------------------------------------------


async def check_six(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 6: every stage-2 response has a `raw_document` row before anything is parsed.

    §8's preamble is the clause - "All fetched bytes land in the app's own raw store, so re-parsing
    is free forever" - and it is the sentence the whole milestone rests on: a parser that was wrong
    is repaired by re-running stage 3, never by asking eight hosts again. What makes it measurable
    is the state between the two stages, which is why this runs `enrich` alone and reads the
    database before `derive` is called at all.

    A SERIES AND NOT A FILM, because §8 stage 2 names eight sources and TVmaze is one of them.
    `sources/tvmaze.py` reports a movie as "not applicable" and spends nothing, faithfully - the
    corpus's own note is that "movies only exist on tvmaze as specials" - so a movie here would
    leave this check asserting the word eight over seven sources that ran.

    TWO ROW COUNTS AND NOT ONE. The store's rows are compared against what the transport actually
    served, so a source whose bytes went nowhere is caught; and the derived tables are asserted
    EMPTY, so a stage 2 that had quietly parsed on the way past is caught too.
    """
    before = len(ctx.web.answered)
    title_id, outcome = await run_enrich(ctx, ITEM_EIGHT)
    served = ctx.web.answered[before:]
    documents = await ctx.conn.fetch(
        "SELECT source, kind, ok, http_status FROM raw_document WHERE entity_key = $1"
        " ORDER BY source, kind, page",
        pipeline.key_for_item(ITEM_EIGHT),
    )
    sources = sorted({str(row["source"]) for row in documents})
    good = [row for row in documents if row["ok"]]
    derived = await derived_snapshot(ctx.conn, title_id)
    parsed = {table: len(rows) for table, rows in derived.items() if rows}
    answered = list((outcome.detail or {}).get("answered") or [])
    ok = (
        outcome.verb == stages.ADVANCE
        and len(good) == len(served)
        and len(sources) >= 8
        and "tvmaze" in sources
        and not parsed
    )
    detail = (
        f"title {title_id}: {len(served)} response(s) served, {len(good)} stored ok of "
        f"{len(documents)} row(s) across {len(sources)} source(s) "
        f"({console(', '.join(sources))}); {len(answered)} kind(s) answered; derived rows before "
        f"stage 3: {parsed or 'none'}"
    )
    return ok, detail


# --- 7: the 404 that is a note --------------------------------------------------------------------


async def check_seven(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 7: Metacritic 404s while TMDB answers, and the result is a note, not a park.

    DECISION 334 IS THE WHOLE OF THIS CHECK. Only `tmdb:detail` is required; every other source's
    404, timeout, refused slug or missing credential is recorded in `acquisition_job.detail` under
    that source's name and stage 2 still advances. That is not leniency - it is what makes the raw
    store worth having. Seven sources answered and their bytes are on disk; a stage that parked on
    the first 404 would throw all seven away over one host that has never heard of this film.

    THE NOTE IS READ BY NAME. A check that only asserted the advance would pass on a stage that
    swallowed the 404 in silence, and an operator chasing a thin title on §6.6's board would have
    nothing to read. Decision 334's sentence is that the failure is recorded, so the recording is
    what is asserted.
    """
    slug = IDENTITY[ITEM_NOTE["ProviderIds"]["Imdb"]][2]
    ctx.web.missing_metacritic.add(slug)
    try:
        title_id, outcome = await run_enrich(ctx, ITEM_NOTE)
    finally:
        ctx.web.missing_metacritic.discard(slug)
    detail_payload = outcome.detail or {}
    notes = dict(detail_payload.get("notes") or {})
    answered = list(detail_payload.get("answered") or [])
    metacritic = [kind for kind in notes if kind.startswith("metacritic:")]
    ok = (
        outcome.verb == stages.ADVANCE
        and bool(metacritic)
        and "tmdb:detail" in answered
        and "metacritic:page" not in answered
    )
    detail = (
        f"title {title_id}: stage 2 said {console(outcome.verb)}; "
        f"{len(answered)} kind(s) answered including tmdb:detail={'tmdb:detail' in answered}; "
        f"notes under {console(', '.join(sorted(metacritic)) or '(no metacritic note)')}: "
        + console("; ".join(notes[kind][:90] for kind in sorted(metacritic)) or "(none)")
    )
    return ok, detail


# --- 8: the required source ------------------------------------------------------------------------


async def check_eight(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 8: a failure of the required source parks stage 2 with that source named.

    THE OTHER HALF OF DECISION 334, and the two are only both true because two states are read
    apart. A TMDB that is not configured is filtered out by `available_kinds` and never runs, which
    §3.1 makes a legal half-configured install; a TMDB that was asked and did not answer is the one
    failure §8 stage 2 cannot shrug off. This check is the second state: the key is configured, the
    request is made, and the detail route is not there.

    THE PARK CARRIES A DEADLINE AND THE REASON NAMES THE SOURCE. A park with no `until` is
    `queue.skip`, which closes the task for good, and what this one waits on - a network that heals,
    a host that comes back - is decision 336's "something that may change" in its plainest form. An
    operator reading `acquisition_job.reason` has to be able to tell which of eight sources stopped
    the title, so the string is searched for the kind rather than merely checked for length.
    """
    tmdb_id = IDENTITY[ITEM_REQUIRED["ProviderIds"]["Imdb"]][0]
    ctx.web.missing_tmdb_detail.add(tmdb_id)
    try:
        title_id, outcome = await run_enrich(ctx, ITEM_REQUIRED)
    finally:
        ctx.web.missing_tmdb_detail.discard(tmdb_id)
    reason = str(outcome.reason or "")
    answered = list((outcome.detail or {}).get("answered") or [])
    ok = (
        outcome.verb == stages.PARK
        and stages.REQUIRED_KIND in reason
        and outcome.until is not None
        and stages.REQUIRED_KIND not in answered
        and "wikidata:resolve" in answered
    )
    detail = (
        f"title {title_id}: stage 2 said {console(outcome.verb)} until "
        f"{console(str(outcome.until))}; {len(answered)} kind(s) still answered; "
        f"reason {console(reason[:180])}"
    )
    return ok, detail


# --- 9: the page for another film -----------------------------------------------------------------


async def check_nine(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 9: a scraped page that belongs to a different film is refused, and nothing is
    written from it.

    THE PAGE IS REACHED BY AN IDENTIFIER AND STILL REFUSED, which is the case worth measuring. When
    the slug is a GUESS the adapter itself refuses the page before writing a slug
    (`sources/rottentomatoes.py`); when Wikidata supplied it, the adapter takes it as given and the
    refusal has to happen in the derive - `derive/rebuild._refuses`, judging the page against the
    cast THIS RUN parsed out of the sources that are not reached by a guess. So this drives the half
    that no adapter can catch, and the half that decides whether another film's reviews reach a
    pack.

    WHY IT MATTERS MORE THAN IT LOOKS. §8 stage 7 verifies that a quote is a substring of the pack,
    and a quote from the wrong film is a genuine substring of the wrong film's pack - so a page
    accepted here is a page M5.4's verification will pass. That is the one failure mode in this
    pipeline that gets quieter the further downstream it travels.

    THE BYTES STAY, AND THAT IS NOT A LOOPHOLE. The raw store is append-only and a page that turned
    out to be another film is the honest record of what the slug returned; what it does not get is a
    derived row. So the check asserts a `raw_document` row AND no derived row from that source.

    THE REFUSED SOURCE IS STILL IN `DeriveReport.sources`, AND THAT IS THE MECHANISM RATHER THAN A
    LEAK. `derive/rebuild.derive_title` keeps a refused source's label in the delete scope on
    purpose - it is how rows a PREVIOUS derive wrote from a page that has since turned out to be
    another film get deleted and not replaced - so "nothing written" is a claim about the rows, not
    about the label, and that is what is counted below.
    """
    slug = IDENTITY[ITEM_WRONG["ProviderIds"]["Imdb"]][1]
    ctx.web.wrong_page.add(slug)
    try:
        title_id, _outcome = await run_enrich(ctx, ITEM_WRONG)
    finally:
        ctx.web.wrong_page.discard(slug)
    report = await rebuild.derive_title(ctx.conn, title_id)
    key = pipeline.key_for_item(ITEM_WRONG)
    documents = await ctx.conn.fetch(
        "SELECT id FROM raw_document WHERE entity_key = $1 AND source = $2 AND ok ORDER BY id",
        key, "rottentomatoes",
    )
    # WHAT THE PAGE WOULD HAVE WRITTEN, read off the bytes the store holds. Without this the check
    # is "no rt rows exist" over a page that might have produced none anyway - and `parse_rt_page`
    # returning nothing for a fixture whose scorecard markup was a character out is precisely how
    # the first draft of this file passed while measuring an empty refusal. The count below is the
    # damage the refusal prevented, not an assumption about it.
    content = await rawstore.read(ctx.conn, int(documents[-1]["id"])) if documents else b""
    would_have = len(parse.parse_document("rottentomatoes", "page:main", content)
                     .rows.get("platform_rating", []))
    from_page = await ctx.conn.fetchval(
        "SELECT count(*) FROM display.platform_rating WHERE title_id = $1 AND platform = 'rt'",
        title_id,
    )
    refused = [line for line in report.refused if "rottentomatoes" in line]
    ok = (
        bool(refused)
        and len(documents) >= 1
        and would_have >= 1
        and int(from_page) == 0
        and "rottentomatoes" in report.sources
    )
    detail = (
        f"title {title_id}: {len(documents)} rottentomatoes document(s) in the store, carrying "
        f"{would_have} score row(s) a derive that accepted the page would have written; refused "
        f"{console(', '.join(refused) or '(nothing was refused)')}; rt platform_rating rows in the "
        f"database {from_page}; the source stayed in the delete scope: "
        f"{'rottentomatoes' in report.sources}; derived sources "
        f"{console(', '.join(report.sources))}"
    )
    return ok, detail


# --- 10: the household's own correction -----------------------------------------------------------


async def check_ten(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 10: a household-authored correction survives the ledger reload a models-only
    re-import runs.

    DECISION 326, AND DECISION 171 PROBED THE COST IN ADVANCE: "`DELETE FROM credit_correction` is
    unscoped, so when §6.6's ledger editors land, an in-app-authored correction absent from the next
    bundle's TSV is wiped". Decision 162 makes a models-only import the only import a household runs
    twice and decision 171 makes it reload all four curated ledgers every time, so the wipe is not
    an exotic sequence - it is the next routine re-import after somebody types a fix into the
    editor.

    WHAT IS DRIVEN, PRECISELY. `importer/dna.load_corrections` and `load_adjudications` are the
    whole of a models-only import's effect on these two tables, and they are what this calls, with
    the bundle's own TSVs. `import_bundle` itself is NOT re-run: decision 162 refuses a second
    import of a bundle that carries content, and assembling a models-only copy of a half-gigabyte
    bundle to exercise the caller of two functions would measure the copy. The `CHECKS` entry says
    "the ledger reload" for that reason rather than saying more than the run did.

    THE BUNDLE'S HALF IS ASSERTED IN THE SAME PASS. `origin` scoping is only correct if decision
    247's replacement still happens for the rows the bundle DOES own, so a check that read the
    household row alone would pass over a DELETE that had stopped working altogether.
    """
    await ctx.conn.execute(
        "INSERT INTO credit_correction (title_id, field, new_value, evidence, note, origin)"
        " VALUES ($1, 'composer', $2, $3, 'written by this exit criterion', 'household')",
        ctx.corrected_id, HOUSEHOLD_COMPOSER, HOUSEHOLD_EVIDENCE,
    )
    artifacts = ctx.bundle_root / "artifacts"
    corrections_tsv = artifacts / "corrections_v1.tsv"
    if not corrections_tsv.is_file():
        raise PreconditionFailed(
            f"{corrections_tsv} is not in the bundle, so there is no ledger to re-load"
        )
    report = ImportReport()
    before = await ctx.conn.fetchval(
        "SELECT count(*) FROM credit_correction WHERE origin = 'bundle'"
    )
    # `importer/bundle.py`'s own models-only branch, in the two calls it makes into these tables
    # and in its order: the corrections ledger from `artifacts/`, then the adjudications ledger
    # from the vocabulary tree the INSTALL is on. `db/dna_terms.active_version` is how the applier
    # asks the same question, so a bundle naming a vocabulary this install has no row for is the
    # state decision 265 records - and the run says so rather than loading a ledger with no parent.
    await dna_import.load_corrections(ctx.conn, corrections_tsv, report)
    vocabulary = await dna_terms.active_version(ctx.conn)
    vocab_dir = artifacts / "dna_vocab" / str(vocabulary)
    if vocabulary is not None and vocab_dir.is_dir():
        await dna_import.load_adjudications(ctx.conn, vocab_dir, vocabulary, report)
    rows = await ctx.conn.fetch(
        "SELECT new_value, evidence, note FROM credit_correction"
        " WHERE title_id = $1 AND origin = 'household'",
        ctx.corrected_id,
    )
    after = await ctx.conn.fetchval(
        "SELECT count(*) FROM credit_correction WHERE origin = 'bundle'"
    )
    survived = [str(row["new_value"]) for row in rows]
    intact = [str(row["evidence"]) for row in rows] == [HOUSEHOLD_EVIDENCE]
    ok = survived == [HOUSEHOLD_COMPOSER] and intact and int(after) == int(before) and int(after) > 0
    detail = (
        f"the household row is {console(', '.join(survived) or '(gone)')} with its evidence "
        f"{'intact' if intact else 'REWRITTEN'}; the bundle's rows went {before} -> {after}; "
        f"adjudications re-loaded under vocabulary {console(str(vocabulary))}"
    )
    return ok, detail


# --- 11: the slug that was read rather than guessed -----------------------------------------------


async def check_eleven(ctx: Install) -> tuple[bool | None, str]:
    """Plan check 11: `wikidata:resolve` runs before `rt:page`, so the RT slug is read and not
    guessed.

    §8's ORDER IS LOAD-BEARING AND THIS IS THE MEASUREMENT OF IT. The spec's own words for Wikidata
    are that it "halves guessing", because it yields the Rotten Tomatoes, Metacritic and Letterboxd
    slugs; `sources/base.available_kinds` sorts on `default_priority` and gives `wikidata:resolve`
    25 against `rt:page`'s 76 for exactly that. A driver that ran them the other way round would
    build a Rotten Tomatoes url out of a name, and `sources/rottentomatoes.candidate_paths` says
    what that costs: a slug derived from a title cannot tell two films of one name apart.

    BOTH HALVES, because either alone is satisfiable by the wrong code. The ORDER is read off the
    request log this run's transport kept, and the SLUG is read off the request itself: a run that
    asked Wikidata first and then guessed anyway would pass an ordering assertion, and a run that
    happened to guess the right string would pass a slug assertion. The name-derived candidate is
    computed here from the adapter's own function, so what the check refuses is the exact url a
    guess would have produced rather than a string this file made up.
    """
    imdb_id = str(ITEM_WALK["ProviderIds"]["Imdb"])
    slug = IDENTITY[imdb_id][1]
    paths = ctx.walk_paths
    if not paths:
        raise PreconditionFailed("check 4 recorded no request log for the walk, so there is no "
                                 "order to read")
    sparql = next((i for i, p in enumerate(paths) if p == "query.wikidata.org/sparql"), -1)
    rt = next((i for i, p in enumerate(paths) if p.startswith("www.rottentomatoes.com/")), -1)
    title_id = ctx.minted.get(str(ITEM_WALK["Id"]), 0)
    row = await ctx.conn.fetchrow(
        "SELECT id, kind, name, original_name, year, rt_slug FROM title WHERE id = $1", title_id
    )
    if row is None:
        raise PreconditionFailed(f"the walk minted no title row to read a slug off ({title_id})")
    guesses = [
        f"www.rottentomatoes.com/{candidate}"
        for candidate in rottentomatoes.candidate_paths({**dict(row), "rt_slug": None})
    ]
    asked = [p for p in paths if p.startswith("www.rottentomatoes.com/")]
    ok = (
        sparql >= 0
        and rt > sparql
        and asked == [f"www.rottentomatoes.com/{slug}"]
        and not set(asked) & set(guesses)
        and str(row["rt_slug"]) == slug
    )
    detail = (
        f"request {sparql} was the SPARQL and request {rt} the first Rotten Tomatoes page; "
        f"asked {console(', '.join(asked) or '(nothing)')}; the guess this run did NOT make was "
        f"{console(', '.join(guesses) or '(none)')}; title.rt_slug is {console(str(row['rt_slug']))}"
    )
    return ok, detail


# --- the run ---------------------------------------------------------------------------------------


RUNNERS = {
    1: check_one, 2: check_two, 3: check_three, 4: check_four, 5: check_five, 6: check_six,
    7: check_seven, 8: check_eight, 9: check_nine, 10: check_ten, 11: check_eleven,
}
RECORDED: set[int] = set()


async def measure(number: int, ctx: Install) -> None:
    """Run one numbered check, reporting a crash inside it as that check's failure.

    One check's crash fails that check and no other, and the denominator stays the criterion's
    eleven: a run that stops at three and prints "3/3 checks passed" is the failure mode an exit
    criterion exists to rule out. A refused precondition is reported without its traceback - the
    sentence IS the diagnosis, and a stack trace over it is what `ops/m4_exit_criterion.py`'s
    seeding taught this project to stop printing.

    A runner that answers None has already reported itself as not measured, and nothing is appended
    to `results` for it: it belongs in neither half of a tally of verdicts.
    """
    label = next(f"{n}. {title}" for n, title in CHECKS if n == number)
    RECORDED.add(number)
    try:
        verdict, detail = await RUNNERS[number](ctx)
    except PreconditionFailed as exc:
        check(False, label, f"PRECONDITION FAILED: {exc}")
        return
    except Exception as exc:                       # reported, not propagated
        check(
            False, label,
            f"the check stopped on {type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        )
        return
    if verdict is None:
        return
    check(verdict, label, detail)


async def main() -> int:
    negative_control = "--negative-control" in sys.argv[1:]
    bundle_dir = os.environ.get("CORPUS_BUNDLE_DIR")
    if not bundle_dir:
        print("CORPUS_BUNDLE_DIR is unset. This script measures a REAL export bundle on purpose:")
        print("three of its eleven checks are about curated ledgers, and the real bundle ships six")
        print("credit corrections and 828 adjudications while the fixture ships none. A derive")
        print("that applies an empty ledger passes 'the correction is still there' in silence.")
        return 2
    # Resolved before anything moves this process: `_neutralise_connector_env` chdirs out of the
    # directory the operator ran the script from, and CORPUS_BUNDLE_DIR is theirs to write
    # relative to it. [M5.3 review cycle 1, M53-EXIT-04]
    root = Path(bundle_dir).resolve()
    if not (root / "BUNDLE.json").is_file() or not (root / "artifacts").is_dir():
        print(f"CORPUS_BUNDLE_DIR={console(str(root))} is not an export bundle: it must carry")
        print("BUNDLE.json and an artifacts/ directory. It refuses to run on the fixture on")
        print("purpose.")
        return 2
    inventoried = int(
        (json.loads((root / "BUNDLE.json").read_text(encoding="utf-8")).get("total_bytes")) or 0
    )
    if inventoried < MIN_REAL_BYTES:
        print(
            f"{console(str(root))} inventories {inventoried:,} bytes -- the fixture, not the "
            "corpus."
        )
        print("Refusing on purpose: every check below would still print, and none of them would")
        print("mean what its sentence says.")
        return 2
    dsn = os.environ.get("TEST_DATABASE_URL") or _dsn_from_env_test()
    if not dsn:
        print("TEST_DATABASE_URL is unset and .env.test does not supply it.")
        return 2

    print(
        f"\nM5.3 exit criterion -- bundle {console(root.name)} ({inventoried:,} bytes); "
        f"negative control {'ON' if negative_control else 'off'}\n",
        flush=True,
    )

    # A dedicated DATABASE, not a schema: 0003 creates `display` and `review_store`, which are
    # database-global, so a search_path could not isolate this run from a household's data. Named
    # with this run's pid so two concurrent runs cannot drop each other's database.
    scratch = f"spielplan_m53_exit_p{os.getpid()}"
    scratch_dsn = dsn.rsplit("/", 1)[0] + f"/{scratch}"
    admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        await admin.execute(f"CREATE DATABASE {scratch}")
    finally:
        await admin.close()

    # The block that creates the database is the block that drops it. Everything that can fail
    # after the CREATE happens inside the `try`, because the scratch name carries this run's pid:
    # pid-suffixing it removed the accidental second chance the next run's `DROP DATABASE IF
    # EXISTS` used to be, and an orphan nothing will ever name again is a leak on the household's
    # own server. [M4.8 dd22-m45-exit-script-harness-hygiene]
    conn: asyncpg.Connection | None = None
    work: Path | None = None
    started_in = Path.cwd()
    try:
        work = Path(tempfile.mkdtemp(prefix="spielplan-m53-exit-"))
        # A temporary DATA_DIR, removed in the `finally`. It is the artifacts root AND the raw
        # root: `settings.artifacts_dir` is `data_dir / "artifacts"` and `settings.raw_dir` is
        # `data_dir / "raw"`, and every document this run stores lands under the second exactly as
        # `rawstore.resolve` puts it there in production.
        os.environ["DATA_DIR"] = str(work)
        _neutralise_connector_env()
        _seed_throwaway_credentials()
        core_config.settings.cache_clear()
        conn = await asyncpg.connect(scratch_dsn)
        # The app's own connection setup, not a bare connect: `db/pool.py` registers the json/jsonb
        # codec every caller depends on, and a harness that skips it measures a database the app
        # never talks to. Without it `title_meta.payload` fails with "expected str, got dict".
        await db_pool._init_connection(conn)
        await migrate.apply_all(conn)

        print("0. The install this run measures (spec section 10; decision 162)", flush=True)
        ctx = await build_install(conn, root, work, negative_control=negative_control)
        await choose_subjects(ctx)

        print("\n1. The curated credit: a derive that had something to bury (plan check 1)",
              flush=True)
        await measure(1, ctx)
        print("\n2. The second derive: decision 375's idempotence (plan check 2)", flush=True)
        await measure(2, ctx)
        print("\n3. The negative control: the ledger switched off (plan check 3)", flush=True)
        await measure(3, ctx)
        print("\n4. The reviews gate: a park with a deadline (plan check 4)", flush=True)
        await measure(4, ctx)
        print("\n5. The retry: resumed, and no socket opened (plan check 5)", flush=True)
        await measure(5, ctx)
        print("\n6. The raw store: eight sources, stored before parsed (plan check 6)", flush=True)
        await measure(6, ctx)
        print("\n7. The note: one host 404s and stage 2 advances (plan check 7)", flush=True)
        await measure(7, ctx)
        print("\n8. The park: the required source did not answer (plan check 8)", flush=True)
        await measure(8, ctx)
        print("\n9. The refusal: a page about another film (plan check 9)", flush=True)
        await measure(9, ctx)
        print("\n10. The re-import: the household's own correction (plan check 10)", flush=True)
        await measure(10, ctx)
        print("\n11. The slug: read from Wikidata, not guessed (plan check 11)", flush=True)
        await measure(11, ctx)
    except Exception as exc:
        # Everything outside a check: the connect, the migration, the import, the subject choice.
        # Reported as the failures they are, so the exit code stays non-zero and the run still ends
        # in a score rather than in a traceback where the sentence naming the cause belongs. The
        # handler catches everything on purpose, for the reason `ops/m51_exit_criterion.py` states:
        # narrowing it to `asyncpg.PostgresError` would leave an `OSError` out of the staging copy
        # reverting this run to the bare traceback that loses every section below it and the tally.
        # [M4.8 review cycle 2: m48-rev2-m45-raises-where-m4-was-taught-to-report]
        stopped = f"the run stopped on {type(exc).__name__}: {exc}"
        trace = traceback.format_exc()
        first = True
        for number, title in CHECKS:
            if number in RECORDED:
                continue
            check(False, f"{number}. {title}", stopped + ("\n" + trace if first else ""))
            first = False
    finally:
        if conn is not None:
            await conn.close()
        # Out of the neutral directory before the tree holding it is removed: Windows refuses to
        # delete the directory a process stands in, `shutil.rmtree(..., ignore_errors=True)`
        # swallows the refusal, and the run would end on M4.5's NOTE about a staged tree that
        # survived cleanup. [M5.3 review cycle 1, M53-EXIT-04]
        os.chdir(started_in)
        discard_staged_artifacts(work, None)
        admin = await asyncpg.connect(dsn.rsplit("/", 1)[0] + "/postgres")
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)")
        finally:
            await admin.close()

    passed = sum(1 for ok, _ in results if ok)
    failed = len(results) - passed
    unmeasured = len(UNMEASURED)
    print(
        f"\n{passed}/{len(CHECKS)} checks passed, {failed} failed, "
        f"{unmeasured} not measured here",
        flush=True,
    )
    for ok, label in results:
        if not ok:
            print(f"  FAILED: {console(label)}", flush=True)
    for label, reason in UNMEASURED:
        print(f"  NOT MEASURED: {console(label)} -- {console(reason)}", flush=True)
    # Three codes and not two. 1 is a criterion that was measured and came out no; 3 is a run that
    # measured everything it could and could not measure all eleven, which is neither a pass nor a
    # failure and must not be reported as either (decision 184). A default run is a 3, because the
    # negative control is off by default and decision 378 keeps it that way.
    return 1 if failed else (0 if not unmeasured else 3)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
