import { expect, test } from '@playwright/test';

import { signedIn } from '../helpers.js';

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
 * SELF-CONTAINED SEEDING, for the same reason 11-rate is: §4.2's observations are append-only,
 * so a shared account cannot be rewound between runs. A member per run per project gets an
 * empty board, and the verdicts below are the board.
 *
 * Serial, one page: the board is a session's worth of state and Playwright's default
 * context-per-test would throw away the account it was built on.
 */
test.describe.configure({ mode: 'serial' });

const MEMBER_PASSWORD = 'rank-e2e-member-password';

/**
 * Proposal 75's standing footnote, amended to be true of every move this surface can make.
 * The proposal's own "plus two duels" is right only for a drop between two titles; a drop at
 * the end of a tier, and every tap, has one neighbour.
 */
const FOOTNOTE =
  'tap a poster to pick it up, tap a tier to drop · each move writes a tier_edit plus a duel ' +
  'against each new neighbour';

const board = (page) => page.getByTestId('rank-board');
const moving = (page) => page.getByTestId('rank-moving');

async function createMember(page, project) {
  const name = `rank-e2e-${project}-${Date.now()}`;
  const res = await page.request.post('/api/setup/members', { data: { name, role: 'member' } });
  expect(res.status(), 'the admin adds a household member (§3.1)').toBe(201);
  return { name, otp: (await res.json()).one_time_password };
}

async function signInAsMember(page, member) {
  await page.request.post('/api/auth/logout');
  const login = await page.request.post('/api/auth/login', {
    data: { name: member.name, password: member.otp }
  });
  expect(login.ok(), 'the one-time password signs the new member in').toBeTruthy();
  const changed = await page.request.post('/api/auth/password', {
    data: { current_password: member.otp, new_password: MEMBER_PASSWORD }
  });
  expect(changed.ok(), 'setting a password unlocks the rest of the app').toBeTruthy();
}

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
  expect(opened.ok(), 'seeding needs a rating session').toBeTruthy();
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
  return rated;
}

async function openRank(page) {
  await page.goto('/rank');
  await expect(page.getByTestId('rank-surface')).toBeVisible();
  await expect(board(page)).toBeVisible();
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
    await signInAsMember(page, await createMember(page, testInfo.project.name));
    const rated = await rateSome(page);
    expect(rated, 'the seeded member needs a board to rank').toBeGreaterThanOrEqual(3);
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
    await expect(page.getByTestId('rank-why')).toContainText('learned cutpoints, refit nightly');
    await expect(page.getByText(FOOTNOTE)).toBeVisible();
  });

  test('a title lifts on a tap and puts itself down again, writing nothing', async () => {
    // Proposal 74: "a modeless lift with an undiscoverable exit is the classic tap-to-move
    // failure". Both exits are asserted, and both have to leave the Ledger alone.
    await openRank(page);
    const before = await tierEditCount(page);

    const first = board(page).locator('[data-title]').first();
    const titleId = await first.getAttribute('data-title');
    await first.click();
    await expect(moving(page)).toBeVisible();
    await expect(moving(page)).toContainText('tap a tier to drop it');

    // Exit one: re-tapping the lifted title.
    await first.click();
    await expect(moving(page)).toHaveCount(0);

    // Exit two: the banner's Cancel.
    await first.click();
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
    await poster.click();
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
    await openRank(page);

    // A tier with at least two titles in it, so "between" has a meaning.
    const rows = board(page).locator('[data-tier]');
    let target = null;
    for (const row of await rows.all()) {
      if ((await row.locator('[data-title]').count()) >= 2) {
        target = row;
        break;
      }
    }
    test.skip(target === null, 'no tier holds two titles yet');

    const posters = target.locator('[data-title]');
    const settled = await posters.nth(1).getAttribute('data-title');
    const source = board(page)
      .locator(`[data-title]:not([data-title="${settled}"])`)
      .first();
    const moving = await source.getAttribute('data-title');

    const written = page.waitForResponse(
      (res) => res.url().includes('/api/rank/drop') && res.request().method() === 'POST'
    );
    await source.dragTo(posters.nth(1));
    const response = await written;
    expect(response.ok(), `drag-and-drop in ${browserName}`).toBeTruthy();

    const body = JSON.parse(response.request().postData() ?? '{}');
    expect(body.title_id).toBe(Number(moving));
    expect(body.below, 'a drop onto a poster names the title it landed above').toBe(
      Number(settled)
    );
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

  test('all six filter dimensions in section 6.3 have a control', async () => {
    // "**Filters:** genre, kind (movie/series — separate by default), decade, runtime,
    // seen-state, DNA facet/term predicates". The review found genre and decade wired through
    // the query builder with nothing bound to them — proposal 152's anti-pattern, by name.
    await openRank(page);
    for (const id of ['rank-genre', 'rank-decade', 'rank-runtime', 'rank-seen', 'rank-dna']) {
      await expect(page.getByTestId(id)).toBeVisible();
    }
    await expect(board(page).locator('[data-kind="movie"]')).toHaveAttribute(
      'aria-pressed',
      /true|false/
    );
  });

  test('every control on the board meets the 48 px touch floor', async ({}, testInfo) => {
    // §6 preamble: "responsive PWA, phone-first (48 px targets, one-handed, swipe)". The review
    // found nine controls at 32-36 px because a scoped rule outranks design.css's global
    // coarse-pointer floor — invisible to a suite that never measured one.
    test.skip(testInfo.project.name !== 'phone', 'the 48 px rule is about touch');
    await openRank(page);
    const first = board(page).locator('[data-title]').first();
    await first.click();

    for (const id of [
      'rank-sharpen',
      'rank-seen',
      'rank-genre',
      'rank-decade',
      'rank-cancel-lift'
    ]) {
      const box = await page.getByTestId(id).boundingBox();
      expect(box, `${id} is not on screen`).not.toBeNull();
      expect(box.height, `${id} is ${box.height}px tall`).toBeGreaterThanOrEqual(44);
    }
    await page.getByTestId('rank-cancel-lift').click();
  });

  test('the tier set is a per-user preference on the account page', async () => {
    // Decision 11: "The control belongs on the per-user settings page … not on §6.6 Admin",
    // and "warn on save that it discards that user's learned cutpoints and queues a refit".
    await page.goto('/account');
    const section = page.getByTestId('tier-set');
    await expect(section).toBeVisible();
    await expect(section).toContainText('discards your learned cutpoints and queues a refit');
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
