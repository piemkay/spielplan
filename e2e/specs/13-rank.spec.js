import { expect, test } from '@playwright/test';

import { createMember, signInAsMember, signedIn, waitForBoard } from '../helpers.js';

/**
 * §6.3's Rank surface: that the gestures write what the integration tests expect, that both
 * exits from a lift write nothing, and that "sharpen my ranking" is reachable (§12's M3 exit).
 * Its own member per project, seeded through the shared helpers (decision 186). Serial, one page.
 */
test.describe.configure({ mode: 'serial' });

// Proposal 75's standing footnote, verbatim; the constant lives in `lib/rank.svelte.js`.
const FOOTNOTE = 'tap a title to open it · tap Move to pick it up, then tap a tier to drop it';

const board = (page) => page.getByTestId('rank-board');
const moving = (page) => page.getByTestId('rank-moving');

const moveOf = (page, titleId) => page.getByTestId(`rank-move-${titleId}`);

/**
 * §6.3's board is "every **rated** title", so rate some. Across all three classes: all "liked"
 * would collapse the cutpoints into one tier.
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
    expect(written.ok(), `seeding: POST ${path}`).toBeTruthy();
    if (card.type === 'sweep') rated += 1;
  }
  // The ledger's state, not this run's writes (a `reuse`d account writes nothing), read from the
  // live labels, so no labels fails here rather than as 120 s in `waitForBoard`.
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
 * Arrangement: fill a tier over HTTP to `count` titles, so a gesture has an occupied tier to
 * drop into. No `above`/`below`, or the seed would write the duels the tests count.
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

async function titleOutside(page, index) {
  const payload = await (await page.request.get('/api/rank?kind=movie')).json();
  const outside = payload.tiers
    .filter((tier) => tier.index !== index)
    .flatMap((tier) => tier.entries)
    .map((entry) => entry.title_id);
  expect(outside.length, `every rated title is already in tier ${index}`).toBeGreaterThan(0);
  return outside[0];
}

async function tierIndexOf(page, label) {
  const payload = await (await page.request.get('/api/rank?kind=movie')).json();
  const index = payload.tier_set.indexOf(label);
  expect(index, `${label} is not in this account's tier set`).toBeGreaterThanOrEqual(0);
  return index;
}

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
    // The board arrives with the worker's refit, however this run got its ratings.
    await waitForBoard(page, { atLeast: 3 });
  });

  test.afterAll(async () => {
    await page?.close();
  });

  test('the board carries its tiers, a why-line and the standing footnote', async () => {
    // Proposal 82: best-first, empty tiers kept as drop targets.
    await openRank(page);

    const labels = await board(page).locator('[data-tier]').evaluateAll((rows) =>
      rows.map((row) => row.getAttribute('data-tier'))
    );
    expect(labels).toEqual(['S', 'A+', 'A', 'B', 'C', 'D', 'F']);
    // Decision 486: what the person has told the board, in their words.
    await expect(page.getByTestId('rank-why')).toContainText('rated');
    await expect(page.getByTestId('rank-why')).toContainText('compared');
    await expect(page.getByTestId('rank-why')).not.toContainText('cutpoints');
    await expect(page.getByText(FOOTNOTE)).toBeVisible();
  });

  test('a title lifts on a tap and puts itself down again, writing nothing', async () => {
    // Proposal 74: "a modeless lift with an undiscoverable exit is the classic tap-to-move
    // failure". Both exits leave the Ledger alone. Lifting is Move's job (decision 496).
    await openRank(page);
    const before = await tierEditCount(page);

    const first = board(page).locator('[data-title]').first();
    const titleId = await first.getAttribute('data-title');
    const move = moveOf(page, titleId);
    await move.click();
    await expect(moving(page)).toBeVisible();
    await expect(moving(page)).toContainText('tap a tier to drop it');

    await move.click();
    await expect(moving(page)).toHaveCount(0);

    await move.click();
    await expect(moving(page)).toBeVisible();
    await page.getByTestId('rank-cancel-lift').click();
    await expect(moving(page)).toHaveCount(0);

    expect(titleId, 'the board has at least one title to lift').toBeTruthy();
    expect(await tierEditCount(page), 'a cancelled lift writes no observation').toBe(before);
  });

  test('tapping a tier drops the lifted title into it, and it stays there', async () => {
    // §6.3 "shows the tension rather than snapping back": still there after the refit.
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

    await openRank(page);
    await expect(board(page).locator(`[data-tier="S"] [data-title="${titleId}"]`)).toHaveCount(1);
  });

  test('a tap into an occupied tier writes the edit and no neighbour duel', async () => {
    // §6.3: "dropping a title into a tier emits a `tier_edit`", and nothing else: a tap names no
    // neighbour. What the server wrote shows only in §6.7's rail line, so Show the model is on
    // for this test alone.
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

      // The line names neighbour duels only when there were some. Both gestures are
      // `via=drag_drop`: one route, one set of semantics.
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
    test.skip(testInfo.project.name === 'phone', 'HTML5 drag is a pointer gesture (§6.3)');

    // ARRANGED, not searched for: a search that skipped when it found nothing never ran.
    const tier = await tierIndexOf(page, 'A');
    await seedTier(page, tier, 2);
    await openRank(page);

    const target = board(page).locator('[data-tier="A"]');
    const posters = target.locator('[data-title]');
    expect(await posters.count(), 'the arranged tier renders what the board says it holds')
      .toBeGreaterThanOrEqual(2);
    const above = await posters.nth(0).getAttribute('data-title');
    const settled = await posters.nth(1).getAttribute('data-title');
    // From OUTSIDE the tier, or it would be filtered out of its own neighbours.
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
    await expect(board(page).locator(`[data-tier="A"] [data-title="${moving}"]`)).toHaveCount(1);
  });

  test('sharpen my ranking serves a pair, and answering it moves the board', async () => {
    // §12's M3 exit in miniature: reachable, one duel per answer, and the board re-reads.
    await openRank(page);
    await page.getByTestId('rank-sharpen').click();
    await expect(page.getByTestId('rank-queue')).toBeVisible();

    const pairA = page.getByTestId('rank-pair-a');
    await expect(pairA).toBeVisible();
    const answeredToken = (
      await (await page.request.get('/api/rank/queue?kind=movie')).json()
    ).pair.token;
    await expect(page.getByTestId('rank-pair-reason')).not.toBeEmpty();
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

    // "exactly one duel": the seal is single-use, so a replay or double submit is refused.
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
    // §13: "the 10% uniform-random comparison stream is the *only* data used to evaluate the tier
    // model". The arm travels sealed: the client can neither name it nor be told it.
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
    // Decision 496: a tap opens the card, which is a read.
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
    // Decisions 483, 495: two posters, a round of fifteen counted, and each answer says where
    // its two titles now sit.
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
    // The gutter is a button the height of its tier, and a button centres what it holds.
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
    // seen-state, DNA facet/term predicates".
    await openRank(page);
    for (const id of ['rank-genre', 'rank-decade', 'rank-runtime', 'rank-seen', 'rank-dna']) {
      await expect(page.getByTestId(id)).toBeVisible();
    }
    // `kind` is the partition, in the header rather than among the filters.
    await expect(page.locator('[data-kind="movie"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('[data-kind="series"]')).toHaveAttribute('aria-pressed', 'false');
  });

  test('the title box filters as it is typed in, and every picker says what it picks', async () => {
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
    // Armed before the fill: WebKit's fill can outlast the debounce and the read.
    const cleared = page.waitForResponse(
      (res) => res.url().includes('/api/rank?') && !res.url().includes('q=')
    );
    await page.getByTestId('rank-filter').fill('');
    await cleared;
  });

  test('every control on the board meets the 48 px touch floor', async ({}, testInfo) => {
    // A scoped rule can outrank design.css's global coarse-pointer floor.
    test.skip(testInfo.project.name !== 'phone', 'the 48 px rule is about touch');
    await openRank(page);
    const first = board(page).locator('[data-title]').first();
    const firstId = await first.getAttribute('data-title');
    await moveOf(page, firstId).click();

    // `--touch: 48px`, in both dimensions: the coarse block raises `min-height` alone.
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
    // Decision 11: "warn on save that it discards that user's learned cutpoints and queues a
    // refit", in a member's words (decision 486).
    await page.goto('/account');
    const section = page.getByTestId('tier-set');
    await expect(section).toBeVisible();
    await expect(section).toContainText('throws away where your tier lines were learned to fall');
    await expect(section).toContainText('works them out again shortly');
    await expect(page.getByTestId('tier-set-current')).toContainText('S');

    await page.getByTestId('tier-set-edit').locator('summary').click();
    await page.getByTestId('tier-set-input').fill('bad ok good');
    await page.getByRole('button', { name: 'Save letters' }).click();
    await expect(page.getByTestId('tier-set-current')).toHaveText('bad · ok · good');

    await openRank(page);
    const labels = await board(page).locator('[data-tier]').evaluateAll((rows) =>
      rows.map((row) => row.getAttribute('data-tier'))
    );
    expect(labels).toEqual(['good', 'ok', 'bad']);

    // Decision 11: tier EDITS survive; only the boundaries do not.
    expect(await tierEditCount(page)).toBeGreaterThan(0);
  });
});
