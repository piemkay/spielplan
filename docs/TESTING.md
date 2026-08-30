# Testing

The suite exists to make the spec falsifiable. Every layer answers a different question, and
the coverage map is the contract that says which requirement each milestone owes a test.

## The six layers

| Layer | Command | Needs | Answers |
|---|---|---|---|
| **backend** | `pytest backend/tests` | nothing | pure logic: secrets, auth, validation rules, mapping, mojibake repair |
| **static guards** | (part of pytest) | nothing | rules no runtime can enforce — weights are never filters, the two DNA tiers are never unioned |
| **schema** | (part of pytest) | node | the migrations apply to a real Postgres engine (PGlite, wasm), and produce the structure §4.1 requires |
| **integration** | `TEST_DATABASE_URL=… pytest` | Postgres 16 | anything that only exists against a real server — COPY resolves encoders from destination column types |
| **e2e** | `node e2e/run.mjs` | the compose stack | what actually ships: the real backend serving the real PWA in a real browser, desktop and phone |
| **frontend units** | `npm --prefix frontend test` | nothing | client helpers with real edge cases (query building, error classification) |

Two of those layers exist because of bugs that reached the running app and could not have been
caught anywhere cheaper: SQLite integer booleans hitting Postgres `boolean` columns (integration),
and `json` columns arriving as text and being iterated character by character (e2e).

### Two test doubles that are not mocks

M1 added both, and the distinction matters: each one can *refuse*, which is what makes the
assertions above it capable of failing.

- **`ops/fake_jellyfin.py`** — a real HTTP server implementing the handful of Jellyfin ≥ 10.9
  routes §7.1 names. It runs two ways: mounted in-process through `httpx.ASGITransport` for the
  integration tests, and as a compose service (`ops/compose.e2e.yml`) for the browser tests.
  **It refuses the admin API key on `/UserPlayedItems` on purpose.** A real Jellyfin would
  accept it — the key is admin-equivalent with no read-only variant (§14.3) — so §7.3's
  per-user-token rule is enforced by this app's code and nothing else. Refusing it here is what
  turns that restraint into something a test can break.
- **`backend/tests/fixtures/soft_authenticator.py`** — a software WebAuthn authenticator that
  signs genuine CTAP2 structures with a real P-256 key. It makes the interesting cases
  reachable: an assertion for the wrong origin, one for the wrong rp_id, and a replay whose
  signature verifies perfectly and whose counter has not moved. In the browser, Chromium's
  virtual authenticator (CDP `WebAuthn` domain) plays the same part.

**The e2e origin is load-bearing.** WebAuthn binds a credential to the origin (§2, §14.4), so
the suite runs against `PUBLIC_URL`'s origin — `http://localhost:8080`, not `http://127.0.0.1:8080`.
Same host, same port, different origin, and a passkey registered under one is correctly refused
under the other.

## Running it

```bash
# everything that needs nothing
python -m pytest backend/tests -q
npm --prefix frontend test

# the integration layer
docker compose -f docker-compose.yml -f ops/compose.dev.yml up -d db
docker compose exec db createdb -U spielplan spielplan_test
echo 'TEST_DATABASE_URL=postgresql://spielplan:<pw>@127.0.0.1:5432/spielplan_test' > .env.test
python -m pytest backend/tests -q

# the whole stack, from a cold start — run.mjs brings it up itself, fake Jellyfin included
node e2e/run.mjs
```

`e2e/run.mjs` runs in two phases on purpose. §10's swap sequence ends in "restart backend +
worker", so a bundle imported in phase one is not *loaded* until the services come back. Without
that restart between the phases, every spec that needs an imported bundle skips — which looks
like a pass and proves nothing.

CI runs all of it on every push: `.github/workflows/ci.yml`.

## The coverage map

`backend/tests/spec_coverage.toml` holds one row per testable requirement:

```toml
current_milestone = "M1"

[[requirement]]
id = "data-rules-dna-evidence-required"
spec = "§4.1 rule 1"
milestone = "M0"
kind = "backend"
what = "Bundle validation fails when dna_evidence is absent, or when any dna_tag row has no
        evidence quote, and the report names the orphan count."
why = "a tag without its quote is unfalsifiable."
tests = ["backend/tests/test_bundle_validation.py::test_tag_without_evidence_fails"]
```

`test_spec_coverage.py` enforces two rules:

1. **Every requirement at or before `current_milestone` names at least one test.**
2. **Every named test exists** — checked against the real pytest functions and Playwright titles,
   so a renamed or deleted test breaks the build rather than silently uncovering a requirement.

A row that genuinely should not be tested yet carries `waived = "an honest reason"` and appears
in the report as waived rather than vanishing.

### This is what "updated every milestone" means

The update is mechanical, and it happens *before* the code:

1. **Open the milestone.** Change `current_milestone` in `spec_coverage.toml` to the milestone
   you are starting. Run `pytest backend/tests/test_spec_coverage.py`.
2. **Read the failure.** It lists every requirement that milestone now owes, with its spec
   section, its kind, and what it has to assert. That list is the milestone's test plan, and you
   did not have to write it.
3. **Write the tests first**, at the `kind` the row names. Fill in `tests = [...]` as each lands.
4. **Review it adversarially before closing it.** A green suite proves the tests pass, not that
   the tests are the right ones. M1's review ran six independent lenses — auth, the connector,
   security, the data layer, test quality, the front end — and put every finding through a
   skeptic told to refute it. What survived was real: a duplicate Jellyfin item silently
   erasing a person's explicit `seen`, three inputs rendering white-on-white, a broken link
   reporting itself healthy. None of them would have failed a test that existed.
5. **The milestone is closed when the suite is green** — which is a stronger statement than
   §12's exit criterion, because every requirement behind it is named.

Two things happen on the way that are easy to miss:

- **`e2e/specs/05-milestones.spec.js` asserts that unbuilt surfaces say which milestone owes
  them.** Those assertions are designed to *fail* when the surface ships. That failure is the
  reminder to delete the placeholder test and point the map's rows at the real ones.
- **The `waived` rows come back.** A waiver is scoped to the milestone that wrote it; when you
  open the next one, re-read them, because "no infrastructure for this yet" usually stops being
  true.

### Current state

```
> M0    33/35 covered (2 waived)
> M1    10/10 covered
> M2     1/23 covered   ← open, on branch `m2`
  M3     9
  M4    18
  M5    10
  M6    12
  M7     1
```

M2 is open, so its gate is red on purpose — that is what an in-progress milestone looks like
here, and the 22 remaining rows are the plan. `main` stays green at M1; the work lands on `m2`
and merges when the gate closes.

The two standing M0 waivers are backup rotation (no backup job exists yet) and the title
card's no-bundle model line (unreachable until M5, when a locally acquired title can outlive a
deactivated bundle). M1 closed the third — connector env-seeding, which was an implementation
gap, not a test gap, and now has both.

M1's tenth row was added *during* the milestone, by the review: §3.1's sequence ends "and
passkey registration is prompted afterwards", and the map — written from the spec at M0 — had
no row for that clause. The map is a contract, not a ceiling; a requirement the spec states and
the map missed is added when it is found.

`pytest backend/tests/test_spec_coverage.py -s` prints the live version.

## Writing a test here

- **Assert the behaviour, cite the rule.** Every test in this suite names the spec section it
  defends and, where there is one, the measured fact behind it. A test whose failure message
  does not tell you what broke and why it matters costs more than it saves.
- **Pick the cheapest layer that can actually falsify it.** e2e is slow and flaky; spend it on
  what only a browser can prove. Most rules are cheaper to check in the query layer.
- **A guard needs a self-test.** `test_landmine_guards.py` feeds each of its regexes a synthetic
  violation, because a guard that cannot fail reads as coverage while providing none.
- **Fixtures reproduce the landmines, not the volume.** `tests/fixtures/make_bundle.py` builds a
  bundle with both DNA tiers overlapping, the frozen `rating_source` ids, duplicate `tmdb_id`s,
  NULL alias PK components, CJK/emoji/ZWSP — plus `break_*` helpers that violate one rule each.
  Eight titles that carry every trap beat eleven thousand that carry none.
