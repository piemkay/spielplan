"""§8 stage 2's source adapters, and the scaffolding every one of them registers with.

Spec v2.1 §8 stage 2 (`spec:365-368`), §8's preamble (`spec:359`); decisions 334, 340, 372,
373, 374, 377.

§8 stage 2 names EIGHT sources and no more: `tmdb:resolve` then `tmdb:detail`,
`wikidata:resolve`, `omdb:detail`, `trakt:summary` then `comments`, `wikipedia:article`,
`tvmaze:show` for series, `rt:page`, and `metacritic:page` then `reviews`. One module per
source, landing in this milestone's adapter phase; `base` is the registry they declare
themselves in, `_htmlutil` is what the PARSERS read scraped bytes with, and `credentials` is the
narrow read the three keyed sources use (decision 377).

**This package fetches and it does not parse.** An adapter writes the bytes into M5.1's raw
store and the identity columns the next source needs in order to be fetched at all, and nothing
else (decision 372); every derived row is §8 stage 3's, written from the raw store under
`derive/`. That split is what "All fetched bytes land in the app's own raw store, so re-parsing
is free forever" (`spec:398`) means in code - a bad parse is re-read and never re-crawled - and
it is why `_htmlutil` lives beside the fetchers while importing no transport at all.

**Every request goes through `acquire/fetch.Fetcher`.** Decision 340 makes that layer the one
place robots.txt, the per-host token bucket, the concurrency cap and the circuit breaker exist,
and decision 373 hands one instance per drain to every stage that reaches the network. This is
the milestone that crawls the open web on a household's own IP address: an adapter that opened a
socket any other way would be polite to nobody, and the exemption the household's own Jellyfin
has - it is not a third party and keeps its own client - is not one to widen.

**Letterboxd is absent by decision and not by oversight (decision 374).**
`mdc/sources/letterboxd.py` (63 lines) exists in the corpus and §8 stage 2 does not name it, so
it is not ported and this app never crawls that host. `title.letterboxd_slug` is still filled,
by `wikidata:resolve`, which yields it - the column is written, the source is not fetched. Of
the corpus's three Wikidata handlers only `resolve` is ported, and per-title: `wikidata:entity`
and `wikidata:labels` are batch shapes of a wholesale crawl over nineteen thousand titles, and
§8's unit is one title.
"""
