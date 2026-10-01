import { expect, test } from '@playwright/test';

import { signedIn } from '../helpers.js';

/**
 * Admin · New titles, Movie data, Corrections and the extraction queue (§6.6, §8, §8.4; decisions
 * 336, 441, 444, 527): that the pages show what the routes answer. EVERY TEST ONLY READS: a Retry
 * or a Launch would make real work due. A state the stack lacks is made by adding one row to the
 * real route's answer before the page sees it.
 */

// No service worker (decision 284): on WebKit `page.route` would not see the invented rows' reads.
test.use({ serviceWorkers: 'block' });

// §8's ten stage names, in the section's own words.
const SECTION_8 = [
  'identify',
  'enrich',
  'derive',
  'reviews gate',
  'dna pack',
  'dna extract',
  'verify',
  'project',
  'place',
  'ready'
];

const BOARD = /\/api\/admin\/acquisition(\?.*)?$/;
const FLYWHEEL = /\/api\/admin\/flywheel(\?.*)?$/;
const WEIGHT_NAMED = /confidence|salience|n_sources|threshold/i;

// `19-phone-shell.spec.js`'s pair: measure at rest, in both axes.
async function hasStoppedMoving(locator) {
  await locator.evaluate(async (el) => {
    const moving = [];
    for (let node = el; node; node = node.parentElement) moving.push(...node.getAnimations());
    await Promise.all(moving.map((a) => a.finished.catch(() => {})));
  });
}

async function meetsTheTouchFloor(locator, what) {
  await hasStoppedMoving(locator);
  const box = await locator.boundingBox();
  expect(box, `${what} is not on screen`).not.toBeNull();
  expect(box.height, `${what} is ${box.height}px tall, under --touch (48px)`).toBeGreaterThanOrEqual(48);
  expect(box.width, `${what} is ${box.width}px wide, under --touch (48px)`).toBeGreaterThanOrEqual(48);
}

/** Open a job's sheet from the board, look inside it, and close it again. */
async function inTitle(page, id, look) {
  await page.locator(`[data-testid="board-job"][data-title-id="${id}"]`).click();
  const sheet = page.getByTestId('board-title');
  await expect(sheet).toHaveAttribute('data-title-id', String(id));
  await look(sheet);
  await page.getByRole('dialog').getByRole('button', { name: 'Done', exact: true }).click();
  await expect(sheet).toHaveCount(0);
}

/** Answer the page's read of `pattern` with the real route's body, reshaped by `shape`. */
async function reshape(page, pattern, shape) {
  await page.route(pattern, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({
      status: response.status(),
      contentType: 'application/json',
      body: JSON.stringify(shape(body))
    });
  });
}

test('the board names the ten stages of section 8 in order and shows each reason verbatim', async ({
  page
}) => {
  await signedIn(page);
  // The envelope the page rendered: the worker may move a job between two reads.
  const read = page.waitForResponse(
    (res) => BOARD.test(new URL(res.url()).pathname) && res.request().method() === 'GET'
  );
  await page.goto('/admin/titles');
  const response = await read;
  expect(response.ok(), 'GET /api/admin/acquisition must answer an admin').toBeTruthy();
  const envelope = await response.json();

  expect(envelope.stages.map((s) => s.name), "the route's stages are section 8's").toEqual(SECTION_8);
  expect(envelope.stages.map((s) => s.number)).toEqual(SECTION_8.map((_, i) => i + 1));

  // The import parks thin titles at stage 2, so the board is never empty here.
  const parked = envelope.jobs.filter((job) => job.status === 'parked' && job.reason != null);
  expect(
    parked.length,
    'the board holds no parked job: the bundle import parks thin titles at stage 2, so either it ' +
      'did not run or the sweep stopped parking - there is no reason here to test'
  ).toBeGreaterThan(0);

  // Every job has its row under All.
  const board = page.getByTestId('acquisition-board');
  await board.getByRole('button', { name: /^All · \d+$/ }).click();
  for (const job of envelope.jobs) {
    const row = board.locator(`[data-testid="board-job"][data-title-id="${job.title_id}"]`);
    await expect(row).toHaveCount(1);
  }

  // One job per status is opened: one markup draws the stages and the reason for every job.
  const sampled = new Map();
  for (const job of envelope.jobs) {
    if (job.reason != null && !sampled.has(job.status)) sampled.set(job.status, job);
  }
  for (const job of sampled.values()) {
    await inTitle(page, job.title_id, async (sheet) => {
      await expect(sheet.getByTestId('board-stage')).toHaveCount(SECTION_8.length);
      expect(await sheet.getByTestId('board-stage').allTextContents()).toEqual(SECTION_8);
      expect(
        await sheet.getByTestId('board-reason').textContent(),
        `title ${job.title_id}: the board must print acquisition_job.reason byte for byte`
      ).toBe(job.reason);
    });
  }
});

test(
  'a parked job and a failed job render differently and offer only the actions their state admits',
  async ({ page }) => {
    await signedIn(page);
    const FAILED_ID = 999_999_001;
    let copied = null;
    await reshape(page, BOARD, (body) => {
      const parked = body.jobs.find((job) => job.status === 'parked');
      if (parked) {
        copied = parked;
        body.jobs.push({
          ...parked,
          title_id: FAILED_ID,
          name: 'e2e failed copy',
          status: 'failed',
          reason: 'e2e: a copy of a parked job marked failed in the browser; the server never saw it',
          actions: ['retry', 'retry_from', 'abandon']
        });
      }
      return body;
    });
    try {
      await page.goto('/admin/titles');
      const board = page.getByTestId('acquisition-board');
      await expect(board.getByTestId('board-job').first()).toBeVisible();
      expect(copied, 'the board holds no parked job to copy, so there is nothing to compare').toBeTruthy();

      // Waiting, the filter the page opens on, holds both.
      const parkedRow = board.locator(`[data-testid="board-job"][data-title-id="${copied.title_id}"]`);
      const failedRow = board.locator(`[data-testid="board-job"][data-title-id="${FAILED_ID}"]`);
      await expect(parkedRow).toHaveAttribute('data-status', 'parked');
      await expect(failedRow).toHaveAttribute('data-status', 'failed');
      expect(await parkedRow.getAttribute('class')).not.toBe(await failedRow.getAttribute('class'));

      // Decision 444: a plain retry is failed's alone; abandon is offered on both.
      const says = {};
      for (const [id, status, retries] of [
        [copied.title_id, 'parked', 0],
        [FAILED_ID, 'failed', 1]
      ]) {
        await inTitle(page, id, async (sheet) => {
          says[status] = await sheet.getByTestId('board-status').textContent();
          const named = (name) => sheet.getByRole('button', { name, exact: true });
          await expect(named('Retry now')).toHaveCount(retries);
          await expect(named('Retry from a step…')).toHaveCount(1);
          await expect(named('Stop trying')).toHaveCount(1);
        });
      }
      expect(says.parked, 'decision 336: parked is not broken, and the words say so').not.toBe(says.failed);
    } finally {
      await page.unroute(BOARD);
    }
  }
);

test(
  "the reject review orders a title's tags by confidence ascending and offers no control on a weight",
  async ({ page }) => {
    await signedIn(page);
    const listing = await (
      await page.request.get('/api/titles?kind=movie&kind=series&limit=200')
    ).json();
    let chosen = null;
    for (const row of listing.items) {
      const evidence = await (await page.request.get(`/api/admin/dna/evidence/${row.id}`)).json();
      const measured = evidence.tags.map((tag) => tag.confidence).filter((c) => c !== null);
      if (evidence.tags.length >= 2 && new Set(measured).size >= 2) {
        chosen = evidence;
        break;
      }
    }
    expect(
      chosen,
      'no title in this bundle carries two extracted tags of differing confidence - nothing here is ' +
        'under test'
    ).toBeTruthy();
    const measured = chosen.tags.map((tag) => tag.confidence).filter((c) => c !== null);
    expect(measured, 'the route orders weakest first').toEqual([...measured].sort((a, b) => a - b));

    await page.goto('/admin/corrections');
    const review = page.getByTestId('dna-review');
    await review.getByTestId('evidence-title').fill(String(chosen.title_id));
    await review.getByRole('button', { name: 'Show', exact: true }).click();
    const tags = review.getByTestId('evidence-tag');
    await expect(tags, 'every tag the route returned, none left out').toHaveCount(chosen.tags.length);
    const drawn = await tags.evaluateAll((els) => els.map((el) => el.getAttribute('data-term')));
    const served = chosen.tags.map((tag) => tag.term);
    expect(drawn, 'the screen keeps the ascending-confidence order').toEqual(served);

    // §4.1 rule 2: no control on a weight.
    await expect(review.locator('input[type=range]')).toHaveCount(0);
    for (const role of ['button', 'checkbox', 'switch', 'slider', 'combobox', 'textbox', 'spinbutton']) {
      const named = review.getByRole(role, { name: WEIGHT_NAMED });
      await expect(named, `a ${role} named for a weight`).toHaveCount(0);
    }
    await expect(review.getByRole('button', { name: /accept/i })).toHaveCount(0);
    // §8 stage 7: the one action is a ledger row.
    const rows = review.locator('[data-testid="evidence-tag"], [data-testid="dna-reject"]');
    for (const row of await rows.all()) {
      await expect(row.getByRole('button', { name: 'Write a verdict' })).toHaveCount(1);
    }
  }
);

test('Launch is disabled with its reason and the selection controls meet the touch floor', async ({
  page
}, testInfo) => {
  await signedIn(page);
  const listing = await (await page.request.get('/api/titles?kind=movie&limit=60')).json();
  const [first, second] = listing.items;
  expect(second, 'the catalog holds fewer than two films to build queue rows on').toBeTruthy();
  const row = (id, title) => ({
    id,
    kind: 'thin_facet',
    reason: `e2e: ${title.name} names no extracted term for one declared facet`,
    detail: {},
    est_titles: 1,
    status: 'queued',
    created_at: new Date().toISOString(),
    batch_id: null,
    title_id: title.id,
    title: { name: title.name, year: title.year ?? null },
    board: null
  });
  const injected = [row(999_999_101, first), row(999_999_102, second)];
  await reshape(page, FLYWHEEL, (body) => ({ ...body, items: [...injected, ...body.items] }));
  try {
    // The extraction queue and its Launch sit with the money, under Budget & AI (decision 527).
    await page.goto('/admin/budget');
    const queue = page.getByTestId('flywheel-queue');
    // §8.4: a "queued just now" marker from each row's own creation time.
    for (const item of injected) {
      const card = queue
        .getByTestId('flywheel-row')
        .filter({ has: page.getByLabel(`Select row ${item.id}`) });
      await expect(card.getByTestId('flywheel-queued')).toHaveText('queued just now');
    }
    // The REAL quote route answers; watched from before the ticks.
    const quoted = page.waitForResponse(
      (res) =>
        new URL(res.url()).pathname === '/api/admin/flywheel/quote' &&
        new URL(res.url()).searchParams.get('titles') === '2'
    );
    for (const item of injected) await queue.getByLabel(`Select row ${item.id}`).check();
    await expect(queue.getByTestId('flywheel-titles')).toHaveText('2');
    const quote = await (await quoted).json();
    // Refused on both runs, for a reason that depends on 21-connectors' order; the claim is that
    // the page shows the route's reason, whichever it is.
    expect(quote.launchable, 'launchable: a provider is assigned beside a cap on this stack').toBe(false);
    expect(quote.reason, 'the quote refused Launch without a reason').toMatch(/\S/);

    const launch = queue.getByTestId('flywheel-launch');
    await expect(launch).toBeDisabled();
    const reason = queue.getByTestId('flywheel-launch-reason');
    await expect(reason).toBeVisible();
    // The route's own sentence, whole (decision 441).
    await expect(reason).toHaveText(quote.reason);

    if (testInfo.project.name === 'phone') {
      for (const item of injected) {
        const label = queue.getByLabel(`Select row ${item.id}`).locator('xpath=..');
        await meetsTheTouchFloor(label, `the selection box for queue row ${item.id}`);
      }
      await meetsTheTouchFloor(launch, 'Launch');
      await meetsTheTouchFloor(queue.locator('.passes select'), 'the pass picker');
    }
  } finally {
    await page.unroute(FLYWHEEL);
  }
});

test('two separate editors each export their own artifact', async ({ page }) => {
  await signedIn(page);
  await page.goto('/admin/corrections');
  const editors = page.getByTestId('ledger-editor');
  await expect(editors).toHaveCount(2);
  expect(await editors.evaluateAll((els) => els.map((el) => el.getAttribute('data-ledger')))).toEqual([
    'adjudications',
    'corrections'
  ]);

  const hrefs = [];
  for (const ledger of ['adjudications', 'corrections']) {
    const link = page.locator(`[data-ledger="${ledger}"] a.export`);
    await expect(link).toHaveCount(1);
    await expect(link).toHaveAttribute('download', '');
    hrefs.push(await link.getAttribute('href'));
  }
  expect(hrefs).toEqual([
    '/api/admin/curated/adjudications/export',
    '/api/admin/curated/corrections/export'
  ]);
});

test('the re-import rebuild set is stated where the re-import happens', async ({ page }) => {
  // §10: "everything expressed in the old Backbone's basis is garbage against a new one."
  await signedIn(page);
  // The old Data address lands on Movie data, where the importer is.
  await page.goto('/admin/data');
  await expect(page).toHaveURL(/\/admin\/movie-data$/);
  await page.getByText('What a re-import recomputes').click();
  for (const item of ['fold-in vectors', 'blend weights', 'Ledger MAP refit', 'Cold Tower']) {
    await expect(page.getByText(new RegExp(item))).toBeVisible();
  }
});
