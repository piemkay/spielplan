import { expect, test } from '@playwright/test';

import { createMember, signInAsMember, signedIn, waitForBoard } from '../helpers.js';

/**
 * §6.3's Rank surface, driven in a browser.
 *
 * Two coverage rows close here and neither can close anywhere cheaper.
 *
 * `tonight-rank-tap-to-tier-and-cancel` — §6.3's "**On phones:** tap a title (it lifts), tap a
 * tier (it drops)" with proposals 74 and 75. The write is asserted at the integration layer;
 * what only a browser can prove is that the *gesture* produces it, and — the half that matters
 * — that the two ways out of the lift produce **nothing**. A Cancel that quietly committed
 * would pass every server-side test in the suite.
 *
 * `tonight-rank-queue-answer-sharpens` — §6.3's "sharpen my ranking". The 70/20/10 selector has
 * a pure test and the stored arm an integration one; nothing else asserts that a person can
 * reach the queue at all, which is what §12's M3 exit criterion is made of.
 *
 * SEEDED THROUGH THE SHARED HELPERS. §4.2's observations are append-only, so a shared account
 * cannot be rewound between runs and this file needs an account of its own — one per project,
 * reused across runs rather than minted anew. What it no longer keeps is a PRIVATE copy of
 * `createMember`/`signInAsMember`: that copy never received the re-login `helpers.js` grew when
 * WebKit's `page.request` stopped carrying the session cookie past §3.1's forced password
 * change, so every seeding write from this file was refused on the phone project while
 * 14-tonight passed on the same browser off the repaired copy. One seeding path (decision 186).
 *
 * Serial, one page: the board is a session's worth of state and Playwright's default
 * context-per-test would throw away the account it was built on.
 */
test.describe.configure({ mode: 'serial' });

/**
 * Proposal 75's standing footnote, as `rank.svelte.js` renders it. Verbatim on purpose: the
 * sentence tells a person how the board works, and the surface has to keep it.
 *
 * It used to promise "each move writes a tier_edit plus a duel against each new neighbour",
 * which M4.10's finding 17 made false on the phone (a tap names no neighbour) and which was the
 * model's vocabulary on a member surface besides. Decision 496 made a tap open the title and
 * gave moving its own control, and the footnote now says exactly that and claims no write
 * (decision 486). The constant lives in `frontend/src/lib/rank.svelte.js`; the two move together.
 */
const FOOTNOTE = 'tap a title to open it · tap Move to pick it up, then tap a tier to drop it';

const board = (page) => page.getByTestId('rank-board');
const moving = (page) => page.getByTestId('rank-moving');

/** Decision 496's Move control on a title, by the title's id. */
const moveOf = (page, titleId) => page.getByTestId(`rank-move-${titleId}`);

/**
 * A board to rank. §6.3's board is "every **rated** title", so it does not exist until the
 * person has rated something — the verdicts are the fixture.
 *
 * Spread across all three classes on purpose: §5.2's measured 5x lever is about class balance,
 * and a board built entirely of "liked" would collapse the cutpoints into one tier and make
 * every assertion below about a degenerate case.
 */
async function rateSome(page, count = 8) {
  const opened = await page.request.post('/api/rate/session', {
    data: { restart: true, kinds: ['movie'] }
  });
  expect(opened.ok(), `seeding needs a rating session: ${opened.status()} ${await opened.text()}`)
    .toBeTruthy();
  const values = [2, 1, 0];
  let rated = 0;
  for (let i = 0; i < count * 4 && rated < count; i++) {
    const res = await page.request.get('/api/rate');
    expect(res.ok(), 'GET /api/rate while seeding').toBeTruthy();
    const { card } = await res.json();
    if (!card) break;
    const [path, data] =
      card.type === 'sweep'
        ? ['/api/rate/verdict', { card_token: card.token, value: values[rated % 3] }]
        : ['/api/rate/duel', { card_token: card.token, outcome: 'A' }];
    const written = await page.request.post(path, { data });
    // Loud, not silent. `rateSome` swallowing failures and `test.skip(rated < 3)` turning a
    // broken seed into a pass is exactly the "looks like a pass and proves nothing" pattern
    // docs/TESTING.md warns about — and both of this file's coverage rows close here.
    expect(written.ok(), `seeding: POST ${path}`).toBeTruthy();
    if (card.type === 'sweep') rated += 1;
  }
  // The STATE this function owes its caller, not the number of writes this run made — the rule
  // `helpers.js::seedFilmLedger` already states over its own loop, and the one assertion this copy
  // never got. `rated` is legitimately 0 on a re-run: `createMember(..., {reuse: true})` hands back
  // the account seeded last time, whose sweep pool is drained, and the `break` above is §6.1's
  // drained state rather than a failure. An account with no rated films at all is a different
  // thing — it has no board to fit, nothing queued a refit for it, and `waitForBoard` would spend
  // 120 s blaming a worker for a seed that never happened.
  //
  // §5.2's live label count, because it is true the instant this loop ends: `rate/balance.py` reads
  // `FROM label l`, the rows the verdict writes in its own transaction, while `ledger_state` — every
  // row of §6.3's board — arrives with the worker's sweep (M4.10 finding 9). [§5.2, §6.1]
  //
  // Written in M4.11, which owns neither §6.1 nor §6.3 and owes the reason: this changes no
  // behaviour and cannot redden a run that would otherwise pass — `waitForBoard(page, {atLeast: 3})`
  // is the next call in the same `beforeAll`, and zero live labels is precisely the state in which
  // it spends 120 s and fails anyway. It converts that slow, misattributed red into an immediate
  // accurate one, which is the same repair M4.11 made to `waitForBoard`'s own message after its
  // two-phase run landed on it.
  const balance = await page.request.get('/api/rate');
  expect(balance.ok(), 'reading the seeded ledger back (§6.1)').toBeTruthy();
  const labels = (await balance.json()).class_balance.total;
  expect(labels, 'this account has no rated films, so §6.3 has no board to fit').toBeGreaterThan(0);
  return rated;
}

async function openRank(page) {
  await page.goto('/rank');
  await expect(page.getByTestId('rank-surface')).toBeVisible();
  await expect(board(page)).toBeVisible();
}

/**
 * Put titles into a tier over HTTP until it holds `count` of them, naming no position.
 *
 * Arrangement, not the claim — every assertion below is made about a gesture. It exists because
 * two of the tests need a tier that is already occupied before the gesture is made: a drop
 * *between* two titles has no meaning in an empty tier, and neither does "a tap names no
 * neighbour however full the tier is". The drag test used to ask the board whether such a tier
 * happened to exist and `test.skip` past it when it did not, which on a fresh account was
 * always — so §6.3's two-duel case, the one the M3 review found unreachable from the app, had
 * never been proved in a browser at all. [M4.10 finding 33]
 *
 * `above` and `below` are deliberately absent: a seeded drop must write the edit and nothing
 * else, or the arrangement would be writing the very duels the tests are counting.
 */
async function seedTier(page, index, count) {
  const before = await (await page.request.get('/api/rank?kind=movie')).json();
  const held = before.tiers.find((tier) => tier.index === index)?.entries ?? [];
  const elsewhere = before.tiers
    .filter((tier) => tier.index !== index)
    .flatMap((tier) => tier.entries)
    .map((entry) => entry.title_id);
  for (const title_id of elsewhere.slice(0, Math.max(0, count - held.length))) {
    const written = await page.request.post('/api/rank/drop?kind=movie', {
      data: { title_id, tier: index }
    });
    expect(
      written.ok(),
      `seeding tier ${index}: ${written.status()} ${await written.text()}`
    ).toBeTruthy();
  }
  const after = await (await page.request.get('/api/rank?kind=movie')).json();
  const seeded = after.tiers.find((tier) => tier.index === index)?.entries ?? [];
  expect(seeded.length, `tier ${index} needs ${count} titles in it`).toBeGreaterThanOrEqual(count);
}

/** A rated title this tier does not hold, so a move into it is a move. */
async function titleOutside(page, index) {
  const payload = await (await page.request.get('/api/rank?kind=movie')).json();
  const outside = payload.tiers
    .filter((tier) => tier.index !== index)
    .flatMap((tier) => tier.entries)
    .map((entry) => entry.title_id);
  expect(outside.length, `every rated title is already in tier ${index}`).toBeGreaterThan(0);
  return outside[0];
}

/** The index of a tier by its label — §4.2 fixes the count, not the letters. */
async function tierIndexOf(page, label) {
  const payload = await (await page.request.get('/api/rank?kind=movie')).json();
  const index = payload.tier_set.indexOf(label);
  expect(index, `${label} is not in this account's tier set`).toBeGreaterThanOrEqual(0);
  return index;
}

/** Every key anywhere in a payload. A gated key one level deeper is still on the wire. */
function keysOf(value, into = new Set()) {
  if (Array.isArray(value)) {
    for (const item of value) keysOf(item, into);
    return into;
  }
  if (value && typeof value === 'object') {
    for (const [key, nested] of Object.entries(value)) {
      into.add(key);
      keysOf(nested, into);
    }
  }
  return into;
}

/** Every tier_edit this account has, read over HTTP. The absence of one is the assertion. */
async function tierEditCount(page) {
  const res = await page.request.get('/api/rank?kind=movie');
  const payload = await res.json();
  return payload.tiers
    .flatMap((tier) => tier.entries)
    .filter((entry) => entry.assigned_tier !== null).length;
}

test.describe('rank', () => {
  /** @type {import('@playwright/test').Page} */
  let page;

  test.beforeAll(async ({ browser, baseURL }, testInfo) => {
    page = await browser.newPage({ baseURL });
    await signedIn(page);
    const config = await (await page.request.get('/api/config')).json();
    test.skip(!config.has_bundle, 'needs an imported bundle — run 01-first-boot first');
    const member = await createMember(page, `rank-e2e-${testInfo.project.name}`, { reuse: true });
    await signInAsMember(page, member);
    await rateSome(page);
    // The board, not this run's writes. The account is reused across runs (M4.8, finding 8), so
    // a re-run against a stack `reset.mjs` never touched finds every film already rated and
    // legitimately writes nothing above. What this file needs is a board with something on it,
    // however it got there.
    //
    // Awaited rather than read once, and the wait is not this file's to explain: since M4.10's
    // finding 9 a verdict queues §5.3's full fit instead of running it on the request path, so
    // `ledger_state` — which is every row of §6.3's board — arrives with the `tier-set-refit`
    // sweep. `waitForBoard` carries the arithmetic, beside `waitForPool`, which waits out the
    // other worker tick for the same kind of reason. [M4.10 finding 9]
    await waitForBoard(page, { atLeast: 3 });
  });

  test.afterAll(async () => {
    await page?.close();
  });

  test('the board carries its tiers, a why-line and the standing footnote', async () => {
    // §6.3 lists F, D, C, B, A, A+, S; proposal 82 renders them best-first and keeps the empty
    // ones as drop targets; proposal 81 makes the seven letters say where they came from.
    await openRank(page);

    const labels = await board(page).locator('[data-tier]').evaluateAll((rows) =>
      rows.map((row) => row.getAttribute('data-tier'))
    );
    expect(labels).toEqual(['S', 'A+', 'A', 'B', 'C', 'D', 'F']);
    // The why-line in the member register (decision 486): what the person has told the board,
    // never "learned cutpoints, refit nightly", which was false while nobody had moved a title.
    await expect(page.getByTestId('rank-why')).toContainText('rated');
    await expect(page.getByTestId('rank-why')).toContainText('compared');
    await expect(page.getByTestId('rank-why')).not.toContainText('cutpoints');
    await expect(page.getByText(FOOTNOTE)).toBeVisible();
  });

  test('a title lifts on a tap and puts itself down again, writing nothing', async () => {
    // Proposal 74: "a modeless lift with an undiscoverable exit is the classic tap-to-move
    // failure". Both exits are asserted, and both have to leave the Ledger alone. The tap is on
    // the title's Move control since decision 496; a tap on the title itself opens it.
    await openRank(page);
    const before = await tierEditCount(page);

    const first = board(page).locator('[data-title]').first();
    const titleId = await first.getAttribute('data-title');
    const move = moveOf(page, titleId);
    await move.click();
    await expect(moving(page)).toBeVisible();
    await expect(moving(page)).toContainText('tap a tier to drop it');

    // Exit one: tapping Move again on the lifted title.
    await move.click();
    await expect(moving(page)).toHaveCount(0);

    // Exit two: the banner's Cancel.
    await move.click();
    await expect(moving(page)).toBeVisible();
    await page.getByTestId('rank-cancel-lift').click();
    await expect(moving(page)).toHaveCount(0);

    expect(titleId, 'the board has at least one title to lift').toBeTruthy();
    expect(await tierEditCount(page), 'a cancelled lift writes no observation').toBe(before);
  });

  test('tapping a tier drops the lifted title into it, and it stays there', async () => {
    // §6.3: tap-to-tier carries "the same `tier_edit` semantics" as the drag — and §6.3's
    // other half, "shows the tension rather than snapping back", means the title is still
    // there after the refit the drop triggers.
    await openRank(page);

    const poster = board(page).locator('[data-title]').first();
    const titleId = await poster.getAttribute('data-title');
    await moveOf(page, titleId).click();
    await expect(moving(page)).toBeVisible();

    const written = page.waitForResponse(
      (res) => res.url().includes('/api/rank/drop') && res.request().method() === 'POST'
    );
    await page.getByTestId('rank-tier-S').click();
    await written;

    await expect(moving(page)).toHaveCount(0);
    await expect(board(page).locator(`[data-tier="S"] [data-title="${titleId}"]`)).toHaveCount(1);

    // And it survives a reload, because it is a row and not a client-side sort.
    await openRank(page);
    await expect(board(page).locator(`[data-tier="S"] [data-title="${titleId}"]`)).toHaveCount(1);
  });

  test('a tap into an occupied tier writes the edit and no neighbour duel', async () => {
    // §6.3 gives the phone one input path — "tap a title (it lifts), tap a tier (it drops)" —
    // and it is the case §6.3 spells as "dropping a title into a tier emits a `tier_edit`", full
    // stop. The client used to name the tier's current last entry as a neighbour, so every
    // promotion into a non-empty tier also wrote `duel(last, dropped, outcome='A')`: a
    // comparison the person never made, in the same direction every time, permanent under §4.2
    // and weighed like an answered duel by §5.2. On the primary form factor that was every move.
    //
    // The body is half the proof and the rail is the other half. What the client posts is
    // observable here; what the server wrote is not, except through §6.7's line — which names
    // its neighbour duels or says nothing about them, and which decision 117 gates. So the
    // toggle goes on for the length of this test and comes off again, because every other test
    // in this file is about a member who has not opened the rail. [M4.10 finding 17]
    const tier = await tierIndexOf(page, 'S');
    await seedTier(page, tier, 1);
    const gate = await page.request.post('/api/auth/preferences', { data: { show_model: true } });
    expect(gate.ok(), 'decision 117 is a per-user preference, and the rail is behind it')
      .toBeTruthy();
    try {
      const titleId = await titleOutside(page, tier);
      await openRank(page);
      await expect(board(page).locator('[data-tier="S"] [data-title]')).not.toHaveCount(0);
      await moveOf(page, titleId).click();
      await expect(moving(page)).toBeVisible();

      const written = page.waitForResponse(
        (res) => res.url().includes('/api/rank/drop') && res.request().method() === 'POST'
      );
      await page.getByTestId('rank-tier-S').click();
      const response = await written;
      expect(response.ok(), `tap-to-tier: ${response.status()}`).toBeTruthy();

      const body = JSON.parse(response.request().postData() ?? '{}');
      expect(body.title_id).toBe(titleId);
      expect(body.above, 'a tap names no title above it').toBeNull();
      expect(body.below, 'and none below it either, however full the tier is').toBeNull();

      // §6.7's line for this write. `rail.tier_edit_line` appends "+ N margin-less duels vs new
      // neighbours" only when there were some, so the clause's absence is the count being zero.
      // (`via=drag_drop` on a tap is not a slip: §6.3 gives the two gestures one route and one
      // set of semantics, and `rank/drop.py` marks both with the one word.)
      const payload = await response.json();
      expect(payload.log?.[0], 'the rail is open, so the drop reports its own line').toBeTruthy();
      expect(payload.log[0]).toContain('via=drag_drop');
      expect(payload.log[0], 'a tap writes the edit and nothing else').not.toContain('duels');
    } finally {
      await page.request.post('/api/auth/preferences', { data: { show_model: false } });
    }
  });

  test('dragging a title onto another writes the edit and two neighbour duels', async ({
    browserName
  }, testInfo) => {
    // §6.3: "**Drag-and-drop rearrange** — the owner's requirement … dropping it *between* two
    // titles emits that edit **plus two margin-less duels** against its new neighbours."
    //
    // The M3 review found this case unreachable from the app: both input paths appended to the
    // end of a tier, so the second duel could never be made — while the standing footnote
    // promised it on every move. This is the browser half of the fix; the write shapes are
    // asserted in test_rank_integration.py.
    test.skip(testInfo.project.name === 'phone', 'HTML5 drag is a pointer gesture (§6.3)');

    // The tier is ARRANGED to hold two titles rather than searched for. It used to be searched
    // for, and `test.skip(target === null)` when the search failed — which on a board nobody has
    // rearranged is every time, so this assertion had never run once. A skip is not a pass.
    // [M4.10 finding 33]
    const tier = await tierIndexOf(page, 'A');
    await seedTier(page, tier, 2);
    await openRank(page);

    const target = board(page).locator('[data-tier="A"]');
    const posters = target.locator('[data-title]');
    expect(await posters.count(), 'the arranged tier renders what the board says it holds')
      .toBeGreaterThanOrEqual(2);
    const above = await posters.nth(0).getAttribute('data-title');
    const settled = await posters.nth(1).getAttribute('data-title');
    // From OUTSIDE the target tier, so the landing really is *between* two titles: a source
    // already in A would be filtered out of its own tier's neighbours and name only one.
    const moving = await titleOutside(page, tier);
    const source = board(page).locator(`[data-title="${moving}"]`);

    const written = page.waitForResponse(
      (res) => res.url().includes('/api/rank/drop') && res.request().method() === 'POST'
    );
    await source.dragTo(posters.nth(1));
    const response = await written;
    expect(response.ok(), `drag-and-drop in ${browserName}`).toBeTruthy();

    const body = JSON.parse(response.request().postData() ?? '{}');
    expect(body.title_id).toBe(moving);
    expect(body.below, 'a drop onto a poster names the title it landed above').toBe(
      Number(settled)
    );
    expect(body.above, "and the one it landed below — §6.3's two margin-less duels").toBe(
      Number(above)
    );
    // Both named titles were read out of the tier being dropped into, which is exactly what
    // `rank/drop.py` now refuses a drop for getting wrong — so the 200 above is also that
    // refusal not misfiring on the one gesture that legitimately names two neighbours.
    // [M4.10 finding 18]
    await expect(board(page).locator(`[data-tier="A"] [data-title="${moving}"]`)).toHaveCount(1);
  });

  test('sharpen my ranking serves a pair, and answering it moves the board', async () => {
    // §6.3's control by its own name, and §12's M3 exit criterion in miniature: the queue is
    // reachable, an answer is one `tier_queue` duel, and the board behind it re-reads.
    await openRank(page);
    await page.getByTestId('rank-sharpen').click();
    await expect(page.getByTestId('rank-queue')).toBeVisible();

    const pairA = page.getByTestId('rank-pair-a');
    await expect(pairA).toBeVisible();
    // The token the client is holding, so the replay assertion below names the same pair.
    const answeredToken = (
      await (await page.request.get('/api/rank/queue?kind=movie')).json()
    ).pair.token;
    await expect(page.getByTestId('rank-pair-reason')).not.toBeEmpty();
    // The mirrored strip §6.1's battle uses, which §6.3's queue reuses.
    await expect(page.getByTestId('rank-pair-tie')).toHaveText('about the same');

    const answered = page.waitForResponse(
      (res) => res.url().includes('/api/rank/queue/answer') && res.request().method() === 'POST'
    );
    const redrawn = page.waitForResponse(
      (res) => res.url().includes('/api/rank?') && res.request().method() === 'GET'
    );
    await pairA.click();
    const written = await answered;
    expect(written.ok(), 'the answer is accepted').toBeTruthy();
    await redrawn;

    // "exactly one duel" is enforced by the seal being single-use, and that is observable:
    // replaying the token the client just used has to be refused, and the pair on the table
    // has to have moved on. Without it a retry — or a double submit — wrote another
    // comparison, which for a held-out pair weights one judgement twice in §13's only figure.
    const served = await (await page.request.get('/api/rank/queue?kind=movie')).json();
    const replayed = await page.request.post('/api/rank/queue/answer', {
      data: { pair: answeredToken, outcome: 'A' }
    });
    expect(replayed.status(), 'a replayed seal is a stale card').toBe(409);
    expect(served.pair.token).not.toBe(answeredToken);

    await page.getByTestId('rank-queue-close').click();
    await expect(page.getByTestId('rank-queue')).toHaveCount(0);
  });

  test('the queue answer never lets the client name its own selection arm', async () => {
    // §13's guard: "the 10% uniform-random comparison stream is the *only* data used to
    // evaluate the tier model". A client that could name the arm could put an adaptively
    // chosen pair into the evaluation stream, which is the inflation the guard exists to stop.
    //
    // Both halves now. The pair the member is *served* used to carry `arm` and the arm's own
    // sentence — on the held-out tenth, "uniform-random, held out — this pair never tunes the
    // model" — so §13's only figure was being collected from people who had been told in words
    // which of their answers it ignores. The arm travels sealed in the token and, for a member
    // who has opened the rail, under the gated `model` key; this member has not, so there is
    // nothing on the wire to name. [M4.10 finding 16; decision 117]
    //
    // Decision 117's default is restated rather than assumed, because the claim is about a
    // member who has NOT opened the rail and the tap test above turns the toggle on for one
    // gesture.
    await page.request.post('/api/auth/preferences', { data: { show_model: false } });
    const served = await (await page.request.get('/api/rank/queue?kind=movie')).json();
    expect(served.pair, 'the queue must have a pair to be gated about').toBeTruthy();
    expect([...keysOf(served)]).not.toContain('arm');
    expect([...keysOf(served)]).not.toContain('model');
    expect(JSON.stringify(served)).not.toContain('never tunes the model');
    expect(served.pair.reason, '§6.8 still owes a why-line, and it says the same on every arm')
      .not.toBe('');

    await openRank(page);
    await page.getByTestId('rank-sharpen').click();
    await expect(page.getByTestId('rank-pair-a')).toBeVisible();

    const request = page.waitForRequest(
      (req) => req.url().includes('/api/rank/queue/answer') && req.method() === 'POST'
    );
    await page.getByTestId('rank-pair-a').click();
    const body = JSON.parse((await request).postData() ?? '{}');

    expect(Object.keys(body).sort()).toEqual(['decisive', 'outcome', 'pair']);
    expect(body.pair, 'the pair travels sealed, not as two ids').not.toContain('title');
    await page.getByTestId('rank-queue-close').click();
  });

  test('a tap on a title opens its card and writes nothing', async () => {
    // Decision 496. The first household tapped a title expecting its card and found themselves
    // moving it, and the board had no road to the card at all. The tap opens the card now, and
    // opening it is a read: the Ledger is left exactly as it was.
    await openRank(page);
    const before = await tierEditCount(page);
    const titleId = await board(page).locator('[data-title]').first().getAttribute('data-title');

    await page.getByTestId(`rank-open-${titleId}`).click();
    const card = page.locator('aside[aria-label="Title detail"]');
    await expect(card).toBeVisible();
    await expect(moving(page), 'a tap opens; it does not lift').toHaveCount(0);

    await page.keyboard.press('Escape');
    await expect(card).toHaveCount(0);
    expect(await tierEditCount(page), 'opening a title writes no observation').toBe(before);
  });

  test('the sharpen sheet shows the pair as posters and counts the round', async () => {
    // Decisions 483 and 495. The sheet showed two names in grey boxes under one fixed line,
    // "one more comparison sharpens your board", with no count and no end; both first-household
    // sittings stopped at ten or eleven answers. The pair is two posters carrying their titles'
    // ids, the header says how far through the round of fifteen the person is, and each answer
    // says where its two titles now sit.
    await openRank(page);
    await page.getByTestId('rank-sharpen').click();
    const sheet = page.getByTestId('rank-queue');
    await expect(sheet).toBeVisible();
    await expect(page.getByTestId('rank-round')).toHaveText('1 of 15 this round');

    const served = (await (await page.request.get('/api/rank/queue?kind=movie')).json()).pair;
    expect(served, 'the queue must have a pair to show').toBeTruthy();
    for (const id of [served.title_a, served.title_b]) {
      await expect(sheet.locator(`[data-testid="rate-poster"][data-title-id="${id}"]`))
        .toHaveCount(1);
    }
    await expect(page.getByTestId('rank-pair-reason')).not.toContainText('one more comparison');

    await page.getByTestId('rank-pair-a').click();
    await expect(page.getByTestId('rank-round')).toHaveText('2 of 15 this round');
    await expect(page.getByTestId(`rank-placed-${served.title_a}`)).toBeVisible();
    await expect(page.getByTestId(`rank-placed-${served.title_b}`)).toBeVisible();
    await page.getByTestId('rank-queue-close').click();
  });

  test('every tier letter sits at the top of its row', async () => {
    // The gutter is a button the height of its tier, and a button centres what it holds: in a
    // sixteen-title tier on a phone the letter sat ~500 px down the row, so the first household
    // saw letters on the empty tiers only. The row stays the drop target; the letter sits at
    // its top.
    await openRank(page);
    for (const row of await board(page).locator('[data-tier]').all()) {
      const label = await row.getAttribute('data-tier');
      const rowBox = await row.boundingBox();
      const letterBox = await page.getByTestId(`rank-letter-${label}`).boundingBox();
      expect(rowBox && letterBox, `tier ${label} is not laid out`).toBeTruthy();
      expect(letterBox.y - rowBox.y, `tier ${label}'s letter is not at the top of its row`)
        .toBeLessThan(48);
    }
  });

  test('all six filter dimensions in section 6.3 have a control', async () => {
    // "**Filters:** genre, kind (movie/series — separate by default), decade, runtime,
    // seen-state, DNA facet/term predicates". The review found genre and decade wired through
    // the query builder with nothing bound to them — proposal 152's anti-pattern, by name.
    await openRank(page);
    for (const id of ['rank-genre', 'rank-decade', 'rank-runtime', 'rank-seen', 'rank-dna']) {
      await expect(page.getByTestId(id)).toBeVisible();
    }
    // The kind pills live in the header, not inside the board — §6.3 lists `kind` among the
    // six but it is the partition, not a filter control, so it is a different element.
    await expect(page.locator('[data-kind="movie"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('[data-kind="series"]')).toHaveAttribute('aria-pressed', 'false');
  });

  test('the title box filters as it is typed in, and every picker says what it picks', async () => {
    // Round-2 finding R5: Rank's box waited for Enter while Home's is live, the pickers read
    // "1990s" with nothing saying it was a decade, and "max minutes" clipped to "max minute".
    await openRank(page);
    const first = board(page).locator('[data-title]').first();
    const name =
      (await first.locator('[data-testid^="rank-open-"] .name').textContent())?.trim() ?? '';
    expect(name, 'the seeded board has a title to look for').not.toBe('');
    const read = page.waitForResponse(
      (res) => res.url().includes('/api/rank?') && res.url().includes('q=')
    );
    await page.getByTestId('rank-filter').pressSequentially(name.slice(0, 6));
    await read;
    await expect(board(page).locator('[data-title]').first()).toBeVisible();
    for (const [id, caption] of [
      ['rank-genre', 'genre'],
      ['rank-decade', 'decade'],
      ['rank-runtime', 'max minutes'],
      ['rank-seen', 'seen']
    ]) {
      await expect(page.locator('label', { has: page.getByTestId(id) })).toContainText(caption);
    }
    // Armed before the fill, as `read` is before the typing: WebKit's fill took 1.4 s on the
    // phone project, longer than the 220 ms pause and the read, so a waiter armed after it
    // missed the answer it was waiting for and ran the test out.
    const cleared = page.waitForResponse(
      (res) => res.url().includes('/api/rank?') && !res.url().includes('q=')
    );
    await page.getByTestId('rank-filter').fill('');
    await cleared;
  });

  test('every control on the board meets the 48 px touch floor', async ({}, testInfo) => {
    // §6 preamble: "responsive PWA, phone-first (48 px targets, one-handed, swipe)". The review
    // found nine controls at 32-36 px because a scoped rule outranks design.css's global
    // coarse-pointer floor — invisible to a suite that never measured one.
    test.skip(testInfo.project.name !== 'phone', 'the 48 px rule is about touch');
    await openRank(page);
    const first = board(page).locator('[data-title]').first();
    const firstId = await first.getAttribute('data-title');
    await moveOf(page, firstId).click();

    // 48, and BOTH dimensions. The title has promised 48 since M3 while the assertion admitted
    // 44, and `design.css` has never defined a 44: `--touch` is 48px and no 44-47 px value
    // exists anywhere in `frontend/src`, so the number under the title was checking against a
    // constant the app does not have. The width is the half that was missing rather than merely
    // low - the coarse block raises `min-height` alone, so a control 48 px tall and 32 px wide
    // passed every 48 px assertion in this suite, which is the shape two overlay exits shipped
    // in. [tq4-48px-rule-asserted-at-44-against-a-48px-token]
    // Decision 496's Move is a control of its own on every title, so it takes the floor too.
    for (const id of [
      'rank-sharpen',
      'rank-seen',
      'rank-genre',
      'rank-decade',
      'rank-cancel-lift',
      `rank-move-${firstId}`
    ]) {
      const box = await page.getByTestId(id).boundingBox();
      expect(box, `${id} is not on screen`).not.toBeNull();
      expect(
        box.height,
        `${id} is ${box.height}px tall, under --touch (48px)`
      ).toBeGreaterThanOrEqual(48);
      expect(
        box.width,
        `${id} is ${box.width}px wide, under --touch (48px)`
      ).toBeGreaterThanOrEqual(48);
    }
    await page.getByTestId('rank-cancel-lift').click();
  });

  test('the tier set is a per-user preference on the account page', async () => {
    // Decision 11: "The control belongs on the per-user settings page … not on §6.6 Admin",
    // and "warn on save that it discards that user's learned cutpoints and queues a refit" -
    // said in the member register since decision 486, the same two facts without the nouns.
    await page.goto('/account');
    const section = page.getByTestId('tier-set');
    await expect(section).toBeVisible();
    await expect(section).toContainText('throws away where your tier lines were learned to fall');
    await expect(section).toContainText('works them out again shortly');
    await expect(page.getByTestId('tier-set-current')).toContainText('S');

    await page.getByTestId('tier-set-input').fill('bad ok good');
    await page.getByRole('button', { name: 'Save tier set' }).click();
    await expect(page.getByTestId('tier-set-current')).toHaveText('bad · ok · good');

    // §4.2 fixes length = |tier set| - 1, so the board now renders three rows and no letters.
    await openRank(page);
    const labels = await board(page).locator('[data-tier]').evaluateAll((rows) =>
      rows.map((row) => row.getAttribute('data-tier'))
    );
    expect(labels).toEqual(['good', 'ok', 'bad']);

    // Decision 11: tier EDITS survive; only the boundaries do not.
    expect(await tierEditCount(page)).toBeGreaterThan(0);
  });
});
