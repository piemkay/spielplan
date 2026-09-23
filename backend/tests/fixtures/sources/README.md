# One captured real response per source

These are what §8 stage 2 actually receives, so that `backend/tests/test_derive_parse.py` asserts
the parsers against markup and JSON a server really sent rather than against markup written to
suit them. A parser tested only against its author's idea of the response is a parser that agrees
with itself.

**Provenance.** Every file here is a document from the corpus project's own raw store at
`C:/Users/pmk/Workspace/movie_data_curator/data/raw`, located through that project's
`raw_document` table and decompressed. The corpus captured them on **2026-08-14** (TMDB, Trakt,
OMDb, TVmaze, Wikipedia, Wikidata, Letterboxd, Metacritic, Rotten Tomatoes) and **2026-08-16**
(the MPST bulk record). That repository is read-only to this one and nothing here was written
back into it. This app's own raw store starts empty — the export bundle ships no `data/raw/`
(`M5.3-plan.md`, section 8) — so the crawl these came from is the only one either project has.

**Trimming.** Each file is the real response reduced to what the parser reads. A TMDB detail
response is 300 KB and carries 72 cast and 487 crew entries; a Metacritic title page is 700 KB
and carries forty-two score elements, two of which are about the film. Nothing was rewritten:
lists are truncated, unread top-level keys are dropped, and the surviving values are byte-for-byte
the server's. Where a truncation would have destroyed the property under test it was not made —
`metacritic_page.html` keeps six score elements precisely because "the first occurrence is the
page's own" cannot be asserted against a page with one.

**Line endings.** Every file in this repository is CRLF and these are too, which is the one edit
made to the captured bytes. No parser reads a line ending: `clean_text` collapses runs of
whitespace and the two scraped-page parsers match with `re.S`.

**What is in them.** Film titles, cast and crew names, scores, publication names and review text
are here as the public API or the public page served them. Review bodies are truncated (TMDB and
Trakt to 400 characters, MPST's synopsis to 600) because the test needs a body and not a corpus;
Metacritic's critic excerpts are already short by construction and are kept whole.

| file | source | corpus kind | what it is for |
|---|---|---|---|
| `tmdb_movie_detail.json` | TMDB | `movie_detail` | Arrival (2016), tmdb 329865. 8 cast so the top-6 billing cut has a boundary, 6 classified crew jobs and 2 support jobs so `_SUPPORT`'s demotion runs against real credits |
| `tmdb_reviews.json` | TMDB | (the detail response's `reviews` block) | three long-form user reviews, two rated and one not |
| `omdb_detail.json` | OMDb | `detail` | the whole response, 1.7 KB. Carries `Awards`, the three `Ratings` OMDb relays, and `Language`/`Country` as comma-joined prose |
| `trakt_summary.json` | Trakt | `summary` | `?extended=full` |
| `trakt_ratings.json` | Trakt | `ratings` | the ten-bucket histogram |
| `trakt_comments.json` | Trakt | `comments` | four comments, three rated, bodies truncated |
| `tvmaze_show.json` | TVmaze | `show` | The Expanse, with `_embedded` cast, crew and seasons — the series-only source |
| `wikipedia_article.json` | Wikipedia | `article` | Arrival's plaintext extract, reduced to the lead, Plot, Filming, Visual effects and Critical response. The last one is the fixture's point: `parse_wikipedia` must NOT keep it and `parse_wikipedia_reception` must |
| `wikidata_entity.json` | Wikidata | `entities` + `labels` | Q20382729 with the sixteen properties the parser reads, plus the label lookup its Q-ids resolve through. `wikidata:entity` is a batch kind decision 374 does not port, so the labels ship beside the entity rather than as a second document |
| `mpst_meta.json` | MPST | `meta` | the bulk record: a truncated synopsis and the closed-vocabulary tags |
| `jellyfin_item.json` | Jellyfin | `items` | one library item with its `MediaStreams`, `MediaSources` and `People` — the presentation block §8 stage 3 stores under `jellyfin` |
| `rt_page.html` | Rotten Tomatoes | `page:main` | Arrival's `<media-scorecard>` element, its `ld+json` node and its `<title>` |
| `metacritic_page.html` | Metacritic | `page:main` | Arrival's `ld+json`, its `<title>` and six score elements in page order — the first two are the film's and the rest belong to the carousel |
| `metacritic_page_alpha_2018.html` | Metacritic | `page:main` | **the real collision.** The corpus asked `movie/alpha` for Alpha (2026) and Metacritic served the 2018 film; its `metacritic_slug` is still NULL because the check below refused the page |
| `metacritic_page_black_orpheus_2006.html` | Metacritic | `page:main` | **the real restoration.** The page is dated 2006, the Criterion re-release of a 1959 film, and the corpus accepted it |
| `metacritic_reviews_critics.html` | Metacritic | `reviews:critics` | three `data-testid="review-card"` cards with publication, `/100` score, byline and date |
| `metacritic_reviews_users.html` | Metacritic | `reviews:users` | three user cards with `/10` scores. One body is Turkish, which is what §4.1 rule 8's "never clean non-ASCII" is about |
| `letterboxd_film_page.html` | Letterboxd | `film:page` | Arrival's `ld+json` node, **inside the CDATA wrapper the page really serves**. See below |

**The Letterboxd fixture is a measurement, not a happy path.** Letterboxd wraps its `ld+json`
block in `/* <![CDATA[ */ ... /* ]]> */`, so `sources/_htmlutil.ld_json` cannot decode it and
`parse_letterboxd_page` yields nothing — from this capture and from every other. The corpus's
own database confirms it: zero `platform_rating` rows and zero `title_meta` rows under
`letterboxd`, against 9,909 under `tmdb`. The parser is ported faithfully, defect included,
because decision 374 does not port the crawler either; a fix belongs with whoever decides to
crawl the source, and `test_derive_parse.py` asserts the behaviour that exists rather than the
one that was intended.
