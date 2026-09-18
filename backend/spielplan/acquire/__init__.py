"""The acquisition spine — §8's "Ported skeleton", ported. Spec v2.1 §8, §8.4, §5.3, §6.6.

§8 names what this package is, in one sentence (`spec:359`): *"the corpus project's raw store
(content-addressed, immutable — crawl once, re-parse forever), durable `(kind,key)` queue,
per-host rate-limited HTTP layer, and the single-title prototype (`mdc probe`)"*. Three layers
and a driver:

  * `rawstore` — every byte that came off the network, written once, gzipped, named by the
    SHA-256 of its content, under `/data/raw`. `raw_document` rows point at those files, and two
    fetches that return identical bytes share one file and get two rows. §8 states the promise
    this buys: "All fetched bytes land in the app's own raw store, so re-parsing is free
    forever" (`spec:398`) — a bad derive is a re-read, never another crawl.
  * `fetch` — the polite per-host HTTP layer: token bucket, concurrency cap, circuit breaker,
    conditional requests off the previous row's ETag/Last-Modified, and robots.txt honoured per
    host with a declared User-Agent naming the app (decision 340). The household's own Jellyfin
    is not a third party and is not throttled here; it keeps its own client.
  * `queue` — the durable `(kind,key)` task table. Work in flight is recovered by lease expiry
    and not by a shutdown handler, which is the only mechanism that survives `kill -9`; the
    queue is `acquisition_task` and is deliberately not `acquisition_job`, which stays §6.6's
    per-title board (decision 322).
  * `pipeline` — the ten-stage driver, park-and-resume: "Failure at any stage parks the job with
    a reason, retryable from admin" (`spec:398`).

**Why the three layers are one milestone and not three.** In the corpus nothing above them
imports anything else — `mdc/http.py` and `mdc/rawstore.py` reach for `config` and `db`,
`mdc/queue.py` for `db` alone — so they are a narrow waist rather than a stack. Split them and
one of the three becomes a stub the other two are written against, which is a contract nobody
has read. The same shape holds here: this package reads settings and Postgres, and the stages
that will use it (M5.2 through M5.7) plug into the seams rather than into each other.

**The raw store starts empty, on every install.** The export bundle ships `BUNDLE.json`,
`artifacts/`, `content.sqlite` and `reviews.sqlite` and no `data/raw/`
(`docs/milestones/M5.1-plan.md` §2.1), so the promise above is about bytes this app fetches from
M5 onward and never about a head start the bundle supplies. An install that has never run the
pipeline has an empty store, and that is the healthy state rather than a missing import.

**Custody.** `docker-compose.yml:45` mounts `./data/raw:/data/raw` on the worker only, and the
backend's own anchor omits it deliberately: "nothing in the backend reads /data/backups or
/data/raw at all, and a file that is not in the container cannot be served out of it"
(M4.7 sec-08, pinned by `test_static_contracts.py`). So §6.6's board — which the backend serves
— shows the `raw_document` row and never the document: url, http status, sha256, byte size,
fetched_at, all of them in Postgres (decision 345). This package is that mount's first user and
must not move it.

**Nothing here may import from `spielplan.api`.** The rules live in the domain packages and
`api/` decides only HTTP shapes (CLAUDE.md Conventions), enforced over this package by
`test_layering_guards.py::test_no_domain_package_imports_from_the_api_layer`. It matters more
here than elsewhere: the drain runs in the worker, which has no HTTP layer to reach for at all.
(By NAME and not by coordinate, which is what the other fourteen test citations in this tree do
and what this one did not: it said `:553`, and M5.1's own edits to that file pushed the guard to
574, where line 553 is the middle of an unrelated SQL-residue helper. A line number is a
citation that the next commit to the cited file falsifies.
[M5.1 review cycle 4 second pass, M51-C4-CITE-02])

**Every write into the content spine is one-way.** Decision 162: the corpus is no longer a place
the content can be fetched from again, so a corrupted write into `title` and its derived tables
can only be undone by dropping the database. Stage 1 therefore *mints* — a new row taking
`title_id_seq`'s default above 1e9 (`0015_seed.sql:24-25`) with `origin = 'acquired'` — and
never updates a row the bundle imported. A task row is disposable; a title row is not, and that
asymmetry is why the queue is a separate table with no foreign key to `title`.
"""
