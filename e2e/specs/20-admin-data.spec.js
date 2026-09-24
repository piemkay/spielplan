import { expect, test } from '@playwright/test';

import { signedIn } from '../helpers.js';

/**
 * Admin · Data as an operator's instrument. Spec v2.1 §6.6 Data, §8, §8.4, §4.1 rule 2, §8 stage 7,
 * §6 preamble; decisions 330, 336, 342, 345, 441, 444, 445 and 446 (M5.6).
 *
 * WHAT A BROWSER HAS TO SAY THAT THE BACKEND SUITE CANNOT. The routes are held by
 * `test_acquire_actions.py`, `test_flywheel_launch.py`, `test_curated_api.py` and
 * `test_dna_review.py`; what only this file can hold is that the page shows what those routes
 * answer - §8's ten names in the board's own legend, each reason byte for byte as the server sent
 * it, a failed job and a parked one told apart with only their admitted actions, the review's order
 * on the screen and no control anywhere on a weight, Launch dark with its reason as text, and the
 * selection controls at 48 px on an iPhone 13.
 *
 * EVERY TEST HERE ONLY READS, AND THAT IS LOAD-BEARING. A retry from the board makes a real task due
 * and the worker would walk it from stage 2 against real hosts; a Launch or a Save writes household
 * state the specs after this one would inherit. So nothing here presses Retry, Abandon, Launch or
 * Save. Where a state the stack does not hold is needed - a failed job, two queued thin-facet rows,
 * a household axis - the page's own read is fetched from the real route and one fixture row is added
 * to it before the page sees it (the `route.fetch` then `fulfill` precedent in `10-home.spec.js`),
 * so the component renders the server's shape and only the rows under test are invented.
 *
 * THE PHONE PROJECT RUNS THIS FILE (`playwright.config.js`'s testMatch), after
 * `19-phone-shell.spec.js`'s phone run; being read-only is what makes that order safe.
 */

/**
 * NO SERVICE WORKER IN THIS CONTEXT, for decision 284's reason. Every row this file invents reaches the
 * page through `page.route`, and on the phone project - iPhone 13, WebKit - a fetch that passes
 * through `src/service-worker.js` never reaches the route at all: the worker refuses to cache `/api`,
 * but it is still what the request goes through. Left registered, the three reshaped reads came back
 * as the server's own on WebKit - no failed copy on the board, no queued rows, no household axis - and
 * only Chromium's interception, which sees past the worker, kept the desktop run green. The shell
 * cache is `19-phone-shell.spec.js`'s subject, not this file's, so blocking it here costs no claim.
 */
test.use({ serviceWorkers: 'block' });

// §8's ten stage names, in order, written out here - and only here and in the spec - so the board's
// legend and the route's `stages` are both compared with the section's own words.
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
const AXES = /\/api\/admin\/curated\/axes(\?.*)?$/;
const WEIGHT_NAMED = /confidence|salience|n_sources|threshold/i;

/** `19-phone-shell.spec.js`'s pair, restated for the reason that file gives: a box measured while
 *  it animates is a box measured mid-flight, and both axes are the floor, not the height alone. */
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
  // The envelope the page itself rendered, not a second read: the worker may move a job between
  // two reads, and "verbatim" is a claim about what this screen was sent.
  const read = page.waitForResponse(
    (res) => BOARD.test(new URL(res.url()).pathname) && res.request().method() === 'GET'
  );
  await page.goto('/admin/data');
  const response = await read;
  expect(response.ok(), 'GET /api/admin/acquisition must answer an admin').toBeTruthy();
  const envelope = await response.json();

  expect(envelope.stages.map((s) => s.name), "the route's stages are section 8's").toEqual(SECTION_8);
  expect(envelope.stages.map((s) => s.number)).toEqual(SECTION_8.map((_, i) => i + 1));

  const board = page.getByTestId('acquisition-board');
  await expect(board.getByTestId('board-stage')).toHaveCount(SECTION_8.length);
  expect(await board.getByTestId('board-stage').allTextContents()).toEqual(SECTION_8);

  // The fixture bundle's import parks its thin-but-placed titles at stage 2
  // (`placement/reconcile._park_thin`), so the board is not empty on this stack by construction.
  const parked = envelope.jobs.filter((job) => job.status === 'parked' && job.reason != null);
  expect(
    parked.length,
    'the board holds no parked job: the bundle import parks thin titles at stage 2, so either it ' +
      'did not run or the sweep stopped parking - there is no reason here to test'
  ).toBeGreaterThan(0);
  for (const job of envelope.jobs) {
    if (job.reason == null) continue;
    const row = board.locator(`[data-testid="board-job"][data-title-id="${job.title_id}"]`);
    await expect(row).toHaveCount(1);
    expect(
      await row.getByTestId('board-reason').textContent(),
      `title ${job.title_id}: the board must print acquisition_job.reason byte for byte`
    ).toBe(job.reason);
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
      await page.goto('/admin/data');
      const board = page.getByTestId('acquisition-board');
      await expect(board.getByTestId('board-job').first()).toBeVisible();
      expect(copied, 'the board holds no parked job to copy, so there is nothing to compare').toBeTruthy();

      const parkedRow = board.locator(`[data-testid="board-job"][data-title-id="${copied.title_id}"]`);
      const failedRow = board.locator(`[data-testid="board-job"][data-title-id="${FAILED_ID}"]`);
      await expect(parkedRow).toHaveAttribute('data-status', 'parked');
      await expect(failedRow).toHaveAttribute('data-status', 'failed');
      const parkedSays = await parkedRow.getByTestId('board-status').textContent();
      const failedSays = await failedRow.getByTestId('board-status').textContent();
      expect(parkedSays, 'decision 336: parked is not broken, and the words say so').not.toBe(failedSays);
      expect(await parkedRow.getAttribute('class')).not.toBe(await failedRow.getAttribute('class'));

      // Decision 444: a plain retry is failed's alone; abandon is offered on both.
      await expect(parkedRow.getByRole('button', { name: 'Retry', exact: true })).toHaveCount(0);
      await expect(failedRow.getByRole('button', { name: 'Retry', exact: true })).toHaveCount(1);
      await expect(parkedRow.getByRole('button', { name: 'Retry from stage', exact: true })).toHaveCount(1);
      await expect(failedRow.getByRole('button', { name: 'Retry from stage', exact: true })).toHaveCount(1);
      await expect(parkedRow.getByRole('button', { name: 'Abandon', exact: true })).toHaveCount(1);
      await expect(failedRow.getByRole('button', { name: 'Abandon', exact: true })).toHaveCount(1);
    } finally {
      await page.unroute(BOARD);
    }
  }
);

test(
  "the reject review orders a title's tags by confidence ascending and offers no control on a weight",
  async ({ page }) => {
    await signedIn(page);
    // FOUND through the real read route, not named: which titles carry two extracted tags is a
    // property of the imported bundle.
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

    await page.goto('/admin/data');
    const review = page.getByTestId('dna-review');
    await review.getByTestId('evidence-title').fill(String(chosen.title_id));
    await review.getByRole('button', { name: 'Show', exact: true }).click();
    const tags = review.getByTestId('evidence-tag');
    await expect(tags, 'every tag the route returned, none left out').toHaveCount(chosen.tags.length);
    const drawn = await tags.evaluateAll((els) => els.map((el) => el.getAttribute('data-term')));
    const served = chosen.tags.map((tag) => tag.term);
    expect(drawn, 'the screen keeps the ascending-confidence order').toEqual(served);

    // Section 4.1 rule 2 one layer up: no slider, no threshold, no toggle, nothing named for a weight.
    await expect(review.locator('input[type=range]')).toHaveCount(0);
    for (const role of ['button', 'checkbox', 'switch', 'slider', 'combobox', 'textbox', 'spinbutton']) {
      const named = review.getByRole(role, { name: WEIGHT_NAMED });
      await expect(named, `a ${role} named for a weight`).toHaveCount(0);
    }
    await expect(review.getByRole('button', { name: /accept/i })).toHaveCount(0);
    // Section 8 stage 7: the one action is a ledger row, and every row offers it.
    const rows = review.locator('[data-testid="evidence-tag"], [data-testid="dna-reject"]');
    for (const row of await rows.all()) {
      await expect(row.getByRole('button', { name: 'Write a ledger row' })).toHaveCount(1);
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
    await page.goto('/admin/data');
    const queue = page.getByTestId('flywheel-queue');
    // Section 8.4 as v2.1.3 amends it: each row carries a "queued just now" marker read off its own
    // creation time, and these two were stamped a moment ago. [M5.6 review cycle 1, M56-DATA-01]
    for (const item of injected) {
      const card = queue
        .getByTestId('flywheel-row')
        .filter({ has: page.getByLabel(`Select row ${item.id}`) });
      await expect(card.getByTestId('flywheel-queued')).toHaveText('queued just now');
    }
    // The REAL quote route answers for the two rows: watched from before the ticks that ask it.
    const quoted = page.waitForResponse(
      (res) =>
        new URL(res.url()).pathname === '/api/admin/flywheel/quote' &&
        new URL(res.url()).searchParams.get('titles') === '2'
    );
    for (const item of injected) await queue.getByLabel(`Select row ${item.id}`).check();
    await expect(queue.getByTestId('flywheel-titles')).toHaveText('2');
    const quote = await (await quoted).json();
    // Refused on both runs, under a sentence this file does not own. The cap and the extraction
    // assignment are 21-connectors': it restores the assignment it found, none on a reset stack, so
    // the page's default batch names no provider (decision 442); but it cannot put back "no cap"
    // (decision 452), so the desktop run here meets no cap and the phone run meets its cap. Which
    // refusal the route gives is the suite's order; that the page shows the one it gave is this test.
    expect(quote.launchable, 'launchable: a provider is assigned beside a cap on this stack').toBe(false);
    expect(quote.reason, 'the quote refused Launch without a reason').toMatch(/\S/);

    const launch = queue.getByTestId('flywheel-launch');
    await expect(launch).toBeDisabled();
    const reason = queue.getByTestId('flywheel-launch-reason');
    await expect(reason).toBeVisible();
    // The route's own sentence, whole and as text (decision 441), not a refusal one stack state gives.
    await expect(reason).toHaveText(quote.reason);

    if (testInfo.project.name === 'phone') {
      for (const item of injected) {
        const label = queue.getByLabel(`Select row ${item.id}`).locator('xpath=..');
        await meetsTheTouchFloor(label, `the selection box for queue row ${item.id}`);
      }
      await meetsTheTouchFloor(launch, 'Launch');
      // The pass picker doubles the reservation from 1 to 2, and one digit is narrower than a
      // thumb. [M5.6 review cycle 1, M56-DATA-04]
      await meetsTheTouchFloor(queue.locator('.passes select'), 'the pass picker');
    }
  } finally {
    await page.unroute(FLYWHEEL);
  }
});

test('three separate editors each export their own artifact', async ({ page }) => {
  await signedIn(page);
  // One household axis added to the page's read, so the axis editor draws the per-facet export it
  // offers a household row (section 6.4's `<facet>.tsv`); the bundle ships none (decision 173).
  let facet = null;
  await reshape(page, AXES, (body) => {
    facet = body.facets?.[0] ?? null;
    if (!facet) return body;
    const axis = { facet, left_pole: 'e2e left', right_pole: 'e2e right', origin: 'household' };
    axis.weights = [];
    return { ...body, axes: [axis, ...body.axes.filter((a) => a.facet !== facet)] };
  });
  try {
    await page.goto('/admin/data');
    const editors = page.getByTestId('ledger-editor');
    await expect(editors).toHaveCount(3);
    expect(await editors.evaluateAll((els) => els.map((el) => el.getAttribute('data-ledger')))).toEqual([
      'adjudications',
      'corrections',
      'axes'
    ]);
    // The axis editor's standing sentence is the route's own (`api/curated.APPLIES_AXES`).
    const axes = page.locator('[data-ledger="axes"]');
    const notice = axes.getByTestId('ledger-applies');
    await expect(notice).toContainText('No axis file has been authored and the bundle ships none');
    await expect(notice).toContainText("Tonight's split surfacing");
    expect(facet, 'the active vocabulary declares no facet, so no axis can be drawn').toBeTruthy();

    const hrefs = [];
    for (const ledger of ['adjudications', 'corrections', 'axes']) {
      const link = page.locator(`[data-ledger="${ledger}"] a.export`);
      await expect(link).toHaveCount(1);
      await expect(link).toHaveAttribute('download', '');
      hrefs.push(await link.getAttribute('href'));
    }
    expect(hrefs).toEqual([
      '/api/admin/curated/adjudications/export',
      '/api/admin/curated/corrections/export',
      `/api/admin/curated/axes/${encodeURIComponent(facet)}/export`
    ]);
  } finally {
    await page.unroute(AXES);
  }
});
