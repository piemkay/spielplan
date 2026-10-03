import { expect, test } from '@playwright/test';

import {
  createMember,
  placeThroughRate,
  setUpLadder,
  signInAsMember,
  signedIn,
  waitForBoard
} from '../helpers.js';

/**
 * §6.3's Rank surface: that the gestures write what the integration tests expect, that the title
 * card's ladder sheet writes nothing on its way out, that the comparison round is reachable (§12's
 * M3 exit), and that a member not yet set up reads the board and writes nothing (decision 550).
 * Its own member per project, seeded through the shared helpers (decision 186). Serial, one page.
 */
test.describe.configure({ mode: 'serial' });

const board = (page) => page.getByTestId('rank-board');
const card = (page) => page.getByRole('dialog', { name: 'Title detail' });
const ladder = (page) => page.getByTestId('ladder-sheet');
const shelf = (page, index) =>
  ladder(page).locator(`[data-testid="ladder-shelf"][data-tier="${index}"]`);

// Decision 550's reason: the steps, lower first, and at most one shared genre.
const REASON = /^(Both in \S+|One in \S+, one in \S+)( · both [a-z ]+ films)?$/;

/** Decision 550: a title moves from its card, whose "In your ranking" row opens the ladder sheet. */
async function openLadder(page, titleId) {
  await page.getByTestId(`rank-open-${titleId}`).click();
  await card(page).getByTestId('rank-card-tier').click();
  await expect(ladder(page)).toBeVisible();
}

/** A phone keeps the search behind its icon in the top bar. */
async function searchBox(page) {
  const icon = page.getByTestId('rank-search');
  if (await icon.isVisible()) await icon.click();
  return page.getByTestId('rank-filter');
}

/** The tier a title sits in on the board, as `{index, label, word}`. */
async function stepOf(page, titleId) {
  const payload = await (await page.request.get('/api/rank?kind=movie')).json();
  const tier = payload.tiers.find((t) => t.entries.some((e) => e.title_id === Number(titleId)));
  expect(tier, `title ${titleId} is not on the board`).toBeTruthy();
  return tier;
}

/** Every request but a read, while `act` runs: what "writes nothing" means on the wire. */
async function writesDuring(page, act) {
  const writes = [];
  const watch = (req) => req.method() !== 'GET' && writes.push(`${req.method()} ${req.url()}`);
  page.on('request', watch);
  try {
    await act();
  } finally {
    page.off('request', watch);
  }
  return writes;
}

async function openRank(page) {
  await page.goto('/rank');
  await expect(page.getByTestId('rank-surface')).toBeVisible();
  await expect(board(page)).toBeVisible();
}

// Done keeps the sheet open on the end card when the round's settle moved titles (decision 564).
async function closeSharpen(page) {
  await page.getByTestId('rank-queue-close').click();
  const stop = page.getByTestId('rank-round-stop');
  await expect(async () => {
    if (await stop.isVisible()) await stop.click();
    await expect(page.getByTestId('rank-queue')).toHaveCount(0, { timeout: 500 });
  }).toPass();
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

/** The area a finger can hit: the drawn box plus any hit-area extension (§6: a control may draw smaller). */
async function hitBox(locator) {
  await locator.scrollIntoViewIfNeeded();
  return locator.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    const reach = (dx, dy) => {
      let n = 0;
      // Half a pixel in, so an edge that falls on a whole pixel is not lost to rounding.
      const at = (k) => [x + dx * (r.width / 2 + k - 0.5), y + dy * (r.height / 2 + k - 0.5)];
      while (n < 24 && el.contains(document.elementFromPoint(...at(n + 1)))) n += 1;
      return n;
    };
    return {
      width: r.width + reach(-1, 0) + reach(1, 0),
      height: r.height + reach(0, -1) + reach(0, 1)
    };
  });
}

test.describe('rank', () => {
  /** @type {import('@playwright/test').Page} */
  let page;

  test.beforeAll(async ({ browser, baseURL }, testInfo) => {
    // Waits on the worker's minute tick, longer than the config's 60 s.
    test.setTimeout(180_000);
    page = await browser.newPage({ baseURL });
    await signedIn(page);
    const member = await createMember(page, `rank-e2e-${testInfo.project.name}`, { reuse: true });
    await signInAsMember(page, member);
    // Decision 550: Rank writes wait for the set-up, so set up, then place the rest through Rate
    // across the board, so the cutpoints do not collapse into one tier. Both are a no-op on reuse.
    await setUpLadder(page.request);
    await placeThroughRate(page.request, 'movie', [5, 3, 1, 0]);
    // The board arrives with the worker's refit, however this run got its placements.
    await waitForBoard(page, { atLeast: 3 });
  });

  test.afterAll(async () => {
    await page?.close();
  });

  test('the board heads each tier with its letter and its word', async () => {
    // Proposal 82: best-first, empty tiers kept as drop targets. Decision 550: the step's word.
    await openRank(page);

    const labels = await board(page).locator('[data-tier]').evaluateAll((rows) =>
      rows.map((row) => row.getAttribute('data-tier'))
    );
    expect(labels).toEqual(['S', 'A', 'B', 'C', 'D', 'E']);
    await expect(page.getByTestId('rank-tier-S')).toContainText('All-time favourite');
    await expect(page.getByTestId('rank-tier-B')).toContainText('Very good');
    await expect(page.getByTestId('rank-tier-E')).toContainText('Not for me');
    // Decisions 486 and 528: the count in the person's words, in the search field.
    await expect(await searchBox(page)).toHaveAttribute('placeholder', /^Search \d+ rated films?$/);
    await expect(page.getByRole('region', { name: 'Needs a look' })).toContainText(
      /between two tiers|still mostly our guess|Sharpen your list/
    );
    await expect(page.getByTestId('rank-setup-card')).toHaveCount(0);
    await expect(page.getByTestId('rank-surface')).not.toContainText('cutpoints');
  });

  test('the ladder sheet names the current step, and leaving it writes nothing', async () => {
    // Decision 550: a short sheet of the shelves over the card, closed by Done or Back.
    await openRank(page);
    const titleId = await board(page).locator('[data-title]').first().getAttribute('data-title');
    expect(titleId, 'the board has at least one title to move').toBeTruthy();
    const step = await stepOf(page, titleId);

    const writes = await writesDuring(page, async () => {
      await openLadder(page, titleId);
      await expect(ladder(page).getByTestId('ladder-shelf')).toHaveCount(6);
      // Every shelf carries its word, so the current step's is looked for outside them.
      const named = await ladder(page).evaluate((sheet) => {
        const copy = sheet.cloneNode(true);
        copy.querySelectorAll('[data-testid="ladder-shelf"]').forEach((s) => s.remove());
        return copy.textContent;
      });
      expect(named, 'the sheet names the current step by its word').toContain(step.word);
      await ladder(page).getByTestId('ladder-done').click();
      await expect(ladder(page)).toHaveCount(0);
      await expect(card(page)).toBeVisible();

      await card(page).getByTestId('rank-card-tier').click();
      await expect(ladder(page)).toBeVisible();
      await page.goBack();
      await expect(ladder(page)).toHaveCount(0);
      await expect(page, 'Back closes the sheet and stays on Rank').toHaveURL(/\/rank$/);
      await page.keyboard.press('Escape');
      await expect(card(page)).toHaveCount(0);
    });
    expect(writes, 'a sheet left without a tap writes nothing').toEqual([]);
  });

  test('a tap on a shelf drops the title into that tier, and it stays there', async () => {
    // §6.3 "shows the tension rather than snapping back": still there after the refit.
    const tier = await tierIndexOf(page, 'S');
    const titleId = await titleOutside(page, tier);
    await openRank(page);

    await openLadder(page, titleId);
    const written = page.waitForResponse(
      (res) => res.url().includes('/api/rank/drop') && res.request().method() === 'POST'
    );
    await shelf(page, tier).click();
    await written;

    await expect(ladder(page)).toHaveCount(0);
    await expect(board(page).locator(`[data-tier="S"] [data-title="${titleId}"]`)).toHaveCount(1);

    await openRank(page);
    await expect(board(page).locator(`[data-tier="S"] [data-title="${titleId}"]`)).toHaveCount(1);
  });

  test('a move into an occupied tier writes the edit and no neighbour duel', async () => {
    // §6.3: a tap on a shelf "drops the title in that tier, the same `tier_edit` semantics, naming
    // no neighbour". What the server wrote shows only in §6.7's rail line, so Show the model is on
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
      await openLadder(page, titleId);

      const written = page.waitForResponse(
        (res) => res.url().includes('/api/rank/drop') && res.request().method() === 'POST'
      );
      await shelf(page, tier).click();
      const response = await written;
      expect(response.ok(), `move to a tier: ${response.status()}`).toBeTruthy();

      const body = JSON.parse(response.request().postData() ?? '{}');
      expect(body.title_id).toBe(titleId);
      expect(body.above, 'a move names no title above it').toBeNull();
      expect(body.below, 'and none below it either, however full the tier is').toBeNull();

      // The line names neighbour duels only when there were some. The sheet's pick is recorded
      // `via=explicit`; a drag stays `drag_drop` (decision 534).
      expect(body.via).toBe('explicit');
      const payload = await response.json();
      expect(payload.log?.[0], 'the rail is open, so the drop reports its own line').toBeTruthy();
      expect(payload.log[0]).toContain('via=explicit');
      expect(payload.log[0], 'a move writes the edit and nothing else').not.toContain('duels');
    } finally {
      await page.request.post('/api/auth/preferences', { data: { show_model: false } });
    }
  });

  test('dragging a poster onto another writes the edit and two neighbour duels', async ({
    browserName
  }, testInfo) => {
    // §6.3: "**Drag-and-drop rearrange** — the owner's requirement … dropping it *between* two
    // titles emits that edit **plus two margin-less duels** against its new neighbours."
    test.skip(testInfo.project.name === 'phone', 'a long press and a drag is a touch sequence Playwright cannot send');

    // ARRANGED, not searched for: a search that skipped when it found nothing never ran.
    const tier = await tierIndexOf(page, 'A');
    await seedTier(page, tier, 2);
    await openRank(page);

    const target = board(page).locator('[data-tier="A"]');
    const rows = target.locator('[data-title]');
    expect(await rows.count(), 'the arranged tier renders what the board says it holds')
      .toBeGreaterThanOrEqual(2);
    const above = await rows.nth(0).getAttribute('data-title');
    const settled = await rows.nth(1).getAttribute('data-title');
    // From OUTSIDE the tier, or it would be filtered out of its own neighbours.
    const dragged = await titleOutside(page, tier);
    const source = board(page).locator(`[data-title="${dragged}"]`);

    const written = page.waitForResponse(
      (res) => res.url().includes('/api/rank/drop') && res.request().method() === 'POST'
    );
    await source.dragTo(rows.nth(1));
    const response = await written;
    expect(response.ok(), `drag-and-drop in ${browserName}`).toBeTruthy();

    const body = JSON.parse(response.request().postData() ?? '{}');
    expect(body.title_id).toBe(dragged);
    expect(body.below, 'a drop onto a title names the title it landed above').toBe(
      Number(settled)
    );
    expect(body.above, "and the one it landed below — §6.3's two margin-less duels").toBe(
      Number(above)
    );
    await expect(board(page).locator(`[data-tier="A"] [data-title="${dragged}"]`)).toHaveCount(1);
  });

  test('Place with questions asks inside the tier and ends on the new spot', async () => {
    // Decision 528: about log2(n) either-or questions from the card, then where the title sits.
    const tier = await tierIndexOf(page, 'A');
    await seedTier(page, tier, 3);
    await openRank(page);
    const titleId = await board(page)
      .locator('[data-tier="A"] [data-title]')
      .first()
      .getAttribute('data-title');
    await page.getByTestId(`rank-open-${titleId}`).click();
    await card(page).getByTestId('rank-card-place').click();

    await expect(page).toHaveURL(new RegExp(`/rank/place/${titleId}\\?kind=movie$`));
    await expect(page.getByTestId('rank-place-where')).toHaveText(/^Somewhere between #1 and #\d+ of \d+ in A$/);
    // The title being placed has been seen: Not seen sits under the neighbour alone.
    await expect(page.getByTestId('rate-correction-left')).toHaveCount(0);
    await expect(page.getByTestId('rate-correction-right')).toBeVisible();

    const done = page.getByTestId('rank-place-done');
    const ready = page.locator('[data-testid="rate-duel-B"]:not([disabled])');
    for (let asked = 0; asked < 8 && !(await done.isVisible()); asked++) {
      const answered = page.waitForResponse(
        (res) => res.url().includes('/api/rank/place/answer') && res.request().method() === 'POST'
      );
      await ready.click();
      expect((await answered).ok(), 'each answer is one duel the server accepts').toBeTruthy();
      await expect(done.or(ready)).toBeVisible();
    }
    await expect(done).toHaveText(/ sits in A$/);
    // Every answer preferred the neighbour, so the search ends at the foot of the tier.
    await expect(page.getByTestId('rank-place')).toContainText(/At the bottom of A, below .+ — \d questions?/);

    await page.getByTestId('rank-place-finish').click();
    await expect(page).toHaveURL(/\/rank$/);
    await expect(page.getByTestId('rank-surface')).toBeVisible();
  });

  test('Sharpen your list serves a pair, and answering it moves the board', async () => {
    // §12's M3 exit in miniature: reachable, one duel per answer, and the board re-reads.
    await openRank(page);
    await page.getByTestId('rank-sharpen').click();
    await expect(page.getByTestId('rank-queue')).toBeVisible();

    const pairA = page.getByTestId('rate-duel-A');
    await expect(pairA).toBeVisible();
    const answeredToken = (
      await (await page.request.get('/api/rank/queue?kind=movie')).json()
    ).pair.token;
    // Decision 550: the reason names the steps and a genre the two share.
    await expect(page.getByTestId('rate-battle-reason')).toHaveText(REASON);
    // More, Same, More: the round has no decisive answer (decision 201), and a poster never answers.
    await expect(page.getByTestId('rate-duel-TIE')).toHaveAccessibleName('About the same');
    await expect(page.getByTestId('rate-duel-A-much')).toHaveCount(0);

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

    await closeSharpen(page);
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
    expect(served.pair.reason, '§6.8 still owes a why-line, in one form on every arm').toMatch(REASON);

    await openRank(page);
    await page.getByTestId('rank-sharpen').click();
    await expect(page.getByTestId('rate-duel-A')).toBeVisible();

    const request = page.waitForRequest(
      (req) => req.url().includes('/api/rank/queue/answer') && req.method() === 'POST'
    );
    await page.getByTestId('rate-duel-A').click();
    const body = JSON.parse((await request).postData() ?? '{}');

    expect(Object.keys(body).sort()).toEqual(['decisive', 'outcome', 'pair']);
    expect(body.pair, 'the pair travels sealed, not as two ids').not.toContain('title');
    await closeSharpen(page);
  });

  test('a tap on a title opens its card and writes nothing', async () => {
    // Decision 496: a tap opens the card, which is a read.
    await openRank(page);
    const before = await tierEditCount(page);
    const titleId = await board(page).locator('[data-title]').first().getAttribute('data-title');

    await page.getByTestId(`rank-open-${titleId}`).click();
    const card = page.getByLabel('Title detail');
    await expect(card).toBeVisible();
    await expect(ladder(page), 'a tap opens; it does not move').toHaveCount(0);

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

    await page.getByTestId('rate-duel-A').click();
    await expect(page.getByTestId('rank-round')).toHaveText('2 of 15 this round');
    await expect(page.getByTestId(`rank-placed-${served.title_a}`)).toBeVisible();
    await expect(page.getByTestId(`rank-placed-${served.title_b}`)).toBeVisible();
    await closeSharpen(page);
  });

  test('every tier letter sits at the top of its tier', async () => {
    await openRank(page);
    for (const row of await board(page).locator('[data-tier]').all()) {
      const label = await row.getAttribute('data-tier');
      const rowBox = await row.boundingBox();
      const letterBox = await page.getByTestId(`rank-letter-${label}`).boundingBox();
      expect(rowBox && letterBox, `tier ${label} is not laid out`).toBeTruthy();
      expect(letterBox.y - rowBox.y, `tier ${label}'s letter is not at the top of its tier`)
        .toBeLessThan(48);
    }
  });

  test('all six filter dimensions in section 6.3 have a control', async ({}, testInfo) => {
    // "**Filters:** genre, kind (movie/series — separate by default), decade, runtime,
    // seen-state, and What it's like, §6.0's term picker; all but the kind sit behind one
    // Filters control" (decision 557 item 8: the free-text taste tag is gone).
    await openRank(page);
    await expect(page.getByTestId('rank-genre')).toHaveCount(0);
    await page.getByTestId('rank-filters').click();
    const filters = page.getByRole('dialog', { name: 'Filters' });
    await expect(filters).toBeVisible();
    for (const id of ['rank-genre', 'rank-decade', 'rank-runtime', 'rank-seen', 'rank-terms']) {
      await expect(filters.getByTestId(id)).toBeVisible();
    }
    for (const [id, name] of [
      ['rank-genre', 'Genre'],
      ['rank-decade', 'Decade'],
      ['rank-runtime', 'Max length'],
      ['rank-seen', 'Seen']
    ]) {
      await expect(filters.locator('label', { has: page.getByTestId(id) })).toContainText(name);
    }
    // A row that opens the term sheet on a phone; on a desktop the row holds the field (board B8).
    const terms =
      testInfo.project.name === 'phone'
        ? filters.getByRole('button', { name: "What it's like Add" })
        : filters.getByRole('combobox', { name: 'Find a taste term' });
    await expect(terms).toBeVisible();
    await expect(filters.getByTestId('rank-dna')).toHaveCount(0);
    await filters.getByRole('button', { name: 'Done' }).click();
    await expect(filters).toHaveCount(0);
    // `kind` is the partition, beside the title rather than among the filters.
    await expect(page.locator('[data-kind="movie"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('[data-kind="series"]')).toHaveAttribute('aria-pressed', 'false');
  });

  test('the search box filters as it is typed in', async () => {
    await openRank(page);
    const first = board(page).locator('[data-title]').first();
    // Posters carry no name beneath (decision 528); the button's label starts with it.
    const name = (await first.getAttribute('aria-label'))?.split(',')[0].trim() ?? '';
    expect(name, 'the seeded board has a title to look for').not.toBe('');
    const box = await searchBox(page);
    const read = page.waitForResponse(
      (res) => res.url().includes('/api/rank?') && res.url().includes('q=')
    );
    await box.pressSequentially(name.slice(0, 6));
    await read;
    await expect(board(page).locator('[data-title]').first()).toBeVisible();
    // Armed before the fill: WebKit's fill can outlast the debounce and the read.
    const cleared = page.waitForResponse(
      (res) => res.url().includes('/api/rank?') && !res.url().includes('q=')
    );
    await box.fill('');
    await cleared;
  });

  test('every control on the board meets the 48 px touch floor', async ({}, testInfo) => {
    // A control may draw smaller than 48 px; what a finger can hit may not (§6 preamble).
    test.skip(testInfo.project.name !== 'phone', 'the 48 px rule is about touch');
    await openRank(page);
    const firstId = await board(page).locator('[data-title]').first().getAttribute('data-title');

    for (const id of ['rank-sharpen', 'rank-search', 'rank-filters', `rank-open-${firstId}`]) {
      const box = await hitBox(page.getByTestId(id));
      expect(box.height, `${id} is ${box.height}px tall to a finger, under 48`).toBeGreaterThanOrEqual(48);
      expect(box.width, `${id} is ${box.width}px wide to a finger, under 48`).toBeGreaterThanOrEqual(48);
    }
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
    await page.getByTestId('tier-set-input').fill('good ok bad');
    await page.getByRole('button', { name: 'Save letters' }).click();
    await expect(page.getByTestId('tier-set-current')).toHaveText('good ok bad');

    await openRank(page);
    const labels = await board(page).locator('[data-tier]').evaluateAll((rows) =>
      rows.map((row) => row.getAttribute('data-tier'))
    );
    expect(labels).toEqual(['good', 'ok', 'bad']);
    // A custom label is its own word, shown once, and no verdict word stands in for it.
    for (const label of labels) {
      await expect(page.getByTestId(`rank-tier-${label}`).locator('h2')).toHaveText(label);
    }
    await expect(board(page)).not.toContainText(/Liked|Disliked|Fine/);

    // Decision 11: tier EDITS survive; only the boundaries do not.
    expect(await tierEditCount(page)).toBeGreaterThan(0);
  });
});

test.describe('rank before the set-up', () => {
  /** @type {import('@playwright/test').Page} */
  let page;

  test.beforeAll(async ({ browser, baseURL }) => {
    page = await browser.newPage({ baseURL });
    await signedIn(page);
    // Nobody sets this member up, so a reuse on the second project is as fresh as the first.
    const member = await createMember(page, 'rank-before', { reuse: true });
    await signInAsMember(page, member);
  });

  test.afterAll(async () => {
    await page?.close();
  });

  test('a member not yet set up sees the set-up card over a board that takes no writes', async () => {
    // Decision 550: no drag, no Needs a look, no Place with questions; the card is the way on.
    // A long press that lifts nothing is the page test's: this member has no poster to press.
    await openRank(page);
    const setup = page.getByTestId('rank-setup-card');
    await expect(setup).toContainText('Set up your ladder.');
    await expect(setup.getByRole('link', { name: 'Set up my ladder' })).toHaveAttribute(
      'href',
      '/rate/setup'
    );
    await expect(page.getByTestId('rank-sharpen')).toHaveCount(0);
    await expect(page.getByRole('region', { name: 'Needs a look' })).toHaveCount(0);
    await expect(page.getByTestId('rank-empty')).toHaveCount(0);
    // Its empty tiers stay on screen, and neither a head nor a strip letter takes a drop.
    await expect(board(page).locator('[data-tier]')).toHaveCount(6);
    await expect(page.locator('[data-drop-tier]')).toHaveCount(0);

    const film = (await (await page.request.get('/api/titles?kind=movie&limit=1')).json()).items[0];
    const refusals = {
      'a drop': await page.request.post('/api/rank/drop?kind=movie', {
        data: { title_id: film.id, tier: 5, via: 'explicit' }
      }),
      'a pair': await page.request.get('/api/rank/queue?kind=movie'),
      'Place with questions': await page.request.post('/api/rank/place', {
        data: { title_id: film.id, kind: 'movie' }
      })
    };
    for (const [what, response] of Object.entries(refusals)) {
      expect(response.status(), `${what} before the set-up`).toBe(409);
      expect((await response.json()).detail.reason, what).toBe('not_set_up');
    }
    expect((await (await page.request.get('/api/rank?kind=movie')).json()).set_up).toBe(false);
  });
});
