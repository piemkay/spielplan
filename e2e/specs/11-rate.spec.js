import { devices, expect, test } from '@playwright/test';

import { setUpLadder, signedIn } from '../helpers.js';

/**
 * §6.1's Rate surface, the ladder (decisions 545, 546, 550 and 551): that the surface obeys rules
 * the integration tests already cover. The admin rates, set up by 10-home (`setUpLadder` is
 * idempotent), on the films the set-up left; each test takes back what it placed until the last
 * one drains the queue. The block of 15 is unit-tested: the fixture cannot fill one.
 * Serial, one page: this is a session.
 */
test.describe.configure({ mode: 'serial' });

/**
 * The model's belief about the title, which §6.1 forbids before the tap. The shelves are exempt:
 * a shelf's `tier` is the person's own step, not a belief.
 */
const BELIEF_KEYS = [
  'cdf',
  'sigma',
  's',
  'tier',
  'score',
  'predicted',
  'predicted_label',
  'prediction',
  'verdict_class',
  'label_count',
  'reask_of',
  'placement',
  'guess_word'
];

// Best first, as the shelves stand (decision 550).
const WORDS = [
  'All-time favourite',
  'Loved it',
  'Liked it',
  'It was fine',
  'Not really for me',
  "Didn't like it",
  'Hated it'
];

// Decision 550: a placement may take a median 3.5 s from film shown to tap, so the app's own share
// of it, the round trip to the next film, stays well inside that.
const PLACE_BUDGET_MS = 3_500;
const DWELL_MS = 400;
const CLOCK_SLOP_MS = 100;

const card = (page) => page.getByTestId('rate-card');
const counter = (page) => page.getByTestId('rate-counter');
const undoButton = (page) => page.getByTestId('rate-undo');
const shelf = (page, tier) => page.locator(`[data-testid="rate-shelf"][data-tier="${tier}"]`);

async function envelope(page) {
  const res = await page.request.get('/api/rate');
  expect(res.ok(), 'the Rate surface must answer GET /api/rate').toBeTruthy();
  return res.json();
}

async function seenState(page, titleId) {
  const res = await page.request.get(`/api/titles/${titleId}/state`);
  expect(res.ok()).toBeTruthy();
  return (await res.json()).state;
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

async function openRate(page) {
  await page.goto('/rate');
  await expect(page.getByTestId('rate-surface')).toBeVisible();
  await expect(card(page)).toBeVisible();
  await expect(counter(page)).toHaveText(/^\d+ of 15$/);
}

/** The write a tap or a key sends, and its reply. */
function written(page, route) {
  return page.waitForResponse((res) => res.url().includes(route) && res.request().method() === 'POST');
}

test.describe('rate', () => {
  /** @type {import('@playwright/test').Page} */
  let page;

  test.beforeAll(async ({ browser, baseURL }) => {
    page = await browser.newPage({ baseURL });
    await signedIn(page);
    await setUpLadder(page.request);
    const opened = await page.request.post('/api/rate/session', {
      data: { kinds: ['movie'], restart: true }
    });
    expect(opened.ok(), 'a fresh films block for the admin (§6.1)').toBeTruthy();
  });

  test.afterAll(async () => {
    await page?.close();
  });

  test('a film sits over seven shelves named by their words, with no letter and no belief', async () => {
    await openRate(page);
    await expect(page.getByTestId('rate-card-title')).not.toBeEmpty();
    await expect(page.getByTestId('rate-kind')).toContainText('Films');
    await expect(page.getByTestId('rate-not-seen')).toBeVisible();
    // Undo is always there, dimmed with nothing to take back.
    await expect(undoButton(page)).toBeDisabled();

    const shelves = page.getByTestId('rate-shelf');
    await expect(shelves).toHaveCount(7);
    // A shelf's posters carry no names, so its text is its word alone: no letter (§6.1).
    await expect(shelves).toHaveText(WORDS);
    for (const [i, tier] of [6, 5, 4, 3, 2, 1, 0].entries()) {
      await expect(shelves.nth(i)).toHaveAttribute('data-tier', String(tier));
    }

    // Nothing the model guesses before the tap, on the wire.
    const before = await envelope(page);
    expect(before.echo, 'no echo before a placement').toBeNull();
    const { shelves: own, ...rest } = before.card;
    expect(own).toHaveLength(7);
    const keys = keysOf(rest);
    for (const key of BELIEF_KEYS) {
      expect(keys.has(key), `the card must not carry '${key}' before the answer`).toBe(false);
    }
    await expect(page.getByTestId('rate-model-log')).toHaveCount(0);
  });

  test('a tap places the film, the next card names it and its word, and Undo brings it back', async () => {
    await openRate(page);
    const title = (await page.getByTestId('rate-card-title').textContent())?.trim();
    const at = await counter(page).textContent();

    const reply = written(page, '/api/rate/place');
    await shelf(page, 4).click();
    const res = await reply;
    expect(res.ok(), `placing through Rate: ${res.status()}`).toBeTruthy();
    expect(res.request().postDataJSON()).toMatchObject({ tier: 4 });
    const body = await res.json();
    expect(body.echo).toMatchObject({ name: title, word: 'Liked it' });
    if (body.card) await expect(page.getByTestId('rate-echo')).toContainText(`${title} · Liked it`);

    await expect(undoButton(page)).toBeEnabled();
    await expect(undoButton(page)).toHaveAttribute('data-undo-kind', 'placement');
    await expect(undoButton(page)).toHaveAttribute('aria-label', `Undo placing ${title}`);

    await undoButton(page).click();
    await expect(page.getByTestId('rate-card-title')).toHaveText(title);
    await expect(counter(page)).toHaveText(at);
    await expect(undoButton(page)).toBeDisabled();
  });

  test('keys place from the top, N says Not seen, and Z takes either back', async () => {
    await openRate(page);
    const title = (await page.getByTestId('rate-card-title').textContent())?.trim();
    const { id } = (await envelope(page)).card.title;
    const was = await seenState(page, id);

    let reply = written(page, '/api/rate/place');
    await page.keyboard.press('2');
    expect((await reply).request().postDataJSON()).toMatchObject({ tier: 5 });
    await expect(undoButton(page)).toHaveAttribute('data-undo-kind', 'placement');
    reply = written(page, '/api/rate/undo');
    await page.keyboard.press('z');
    expect((await reply).ok()).toBeTruthy();
    await expect(page.getByTestId('rate-card-title')).toHaveText(title);

    reply = written(page, '/api/rate/not-seen');
    await page.keyboard.press('n');
    expect((await reply).ok()).toBeTruthy();
    expect(await seenState(page, id), 'Not seen sets unseen (§6.1)').toBe('unseen');
    await expect(page.getByTestId('rate-echo'), 'Not seen echoes nothing').toHaveCount(0);
    await expect(undoButton(page)).toHaveAttribute('aria-label', 'Undo not seen');
    reply = written(page, '/api/rate/undo');
    await page.keyboard.press('z');
    expect((await reply).ok()).toBeTruthy();
    await expect(page.getByTestId('rate-card-title')).toHaveText(title);
    expect(await seenState(page, id), 'Undo puts the seen state back').toBe(was);
  });

  test('Not seen from "About this film" answers as the capsule does; looking answers nothing', async () => {
    await openRate(page);
    const title = (await page.getByTestId('rate-card-title').textContent())?.trim();
    await page.getByRole('button', { name: `About ${title}` }).click();
    const peek = page.getByRole('dialog', { name: 'About this film' });
    await expect(peek).toContainText('Looking never counts as an answer.');
    await peek.getByRole('button', { name: 'Done', exact: true }).click();
    await expect(peek).toHaveCount(0);
    await expect(undoButton(page)).toBeDisabled();

    await page.getByRole('button', { name: `About ${title}` }).click();
    const reply = written(page, '/api/rate/not-seen');
    await page.getByTestId('rate-peek-not-seen').click();
    expect((await reply).ok()).toBeTruthy();
    await expect(undoButton(page)).toHaveAttribute('data-undo-kind', 'not_seen');
    await undoButton(page).click();
    await expect(page.getByTestId('rate-card-title')).toHaveText(title);
  });

  test('the title switches between films and series', async () => {
    await openRate(page);
    await page.getByTestId('rate-kind').click();
    const sheet = page.getByRole('dialog', { name: 'What to rate' });
    await sheet.getByRole('menuitem', { name: 'Series' }).click();
    await expect(page.getByTestId('rate-kind')).toContainText('Series');
    expect((await envelope(page)).session.kind).toBe('series');

    await page.getByTestId('rate-kind').click();
    await sheet.getByRole('menuitem', { name: 'Films' }).click();
    await expect(page.getByTestId('rate-kind')).toContainText('Films');
    expect((await envelope(page)).session.kind).toBe('movie');
  });

  test('on a 390x844 phone every shelf, Undo and Not seen are on screen with nothing scrolled', async ({
    browser,
    baseURL
  }) => {
    // Decision 545. The installed app also pays the status bar and the home indicator, which the
    // layout reserves through env(); this measures the same column on the phone's own screen.
    const context = await browser.newContext({
      ...devices['iPhone 13'],
      viewport: { width: 390, height: 844 },
      baseURL
    });
    const phone = await context.newPage();
    try {
      await signedIn(phone);
      await openRate(phone);
      for (const id of ['rate-undo', 'rate-kind', 'rate-counter', 'rate-not-seen']) {
        await expect(phone.getByTestId(id), `${id} is on the screen`).toBeInViewport({ ratio: 1 });
      }
      const shelves = phone.getByTestId('rate-shelf');
      await expect(shelves).toHaveCount(7);
      for (let i = 0; i < 7; i++) {
        await expect(shelves.nth(i), `shelf ${i + 1} is on the screen`).toBeInViewport({ ratio: 1 });
      }
      const clear = await phone.evaluate(() => {
        const last = [...document.querySelectorAll('[data-testid="rate-shelf"]')].at(-1);
        const bar = document.querySelector('nav[aria-label="Main"]');
        return bar.getBoundingClientRect().top - last.getBoundingClientRect().bottom;
      });
      expect(clear, 'the last shelf sits under the tab bar').toBeGreaterThanOrEqual(0);
      const unscrolled = await phone.evaluate(() => document.documentElement.scrollHeight - innerHeight);
      expect(unscrolled, 'the page does not scroll').toBeLessThanOrEqual(0);
    } finally {
      await context.close();
    }
  });

  test('a placement carries the time it took, and its round trip stays inside the budget', async () => {
    await openRate(page);
    // NOT a wait: the dwell under measurement.
    await page.waitForTimeout(DWELL_MS);
    const reply = written(page, '/api/rate/place');
    await shelf(page, 3).click();
    const res = await reply;
    expect(res.ok(), 'the placement was refused, so the timing below is about nothing').toBeTruthy();
    const sent = res.request().postDataJSON();
    expect(sent.latency_ms, 'the placement carries no measure of its card on the screen')
      .toBeGreaterThanOrEqual(DWELL_MS - CLOCK_SLOP_MS);
    // `responseEnd` is -1 until the body arrives, and `waitForResponse` resolves on the headers.
    await res.finished();
    const trip = res.request().timing().responseEnd;
    expect(trip, 'no resource timing was recorded, so the budget below is vacuous').toBeGreaterThan(0);
    expect(trip, 'decision 550 gives a placement 3.5 s').toBeLessThan(PLACE_BUDGET_MS);

    await undoButton(page).click();
    await expect(undoButton(page)).toBeDisabled();
  });

  test('once every film is placed, the queue says so in a plain line', async () => {
    await openRate(page);
    const drained = page.getByTestId('rate-drained');
    for (let i = 0; i < 8 && (await card(page).count()); i++) {
      const token = await card(page).getAttribute('data-card-token');
      const reply = written(page, '/api/rate/place');
      await page.keyboard.press('4');
      expect((await reply).ok(), 'placing a film through Rate').toBeTruthy();
      // The next film deals in after the lit shelf has played; a key before then is not taken.
      await expect(page.locator(`[data-testid="rate-card"]:not([data-card-token="${token}"])`).or(drained))
        .toBeVisible();
    }
    await expect(drained).toHaveText("There's nothing more to rate right now.");
    await expect(card(page)).toHaveCount(0);
    // The last placement stays undoable on the drained screen (decision 35).
    await expect(undoButton(page)).toBeEnabled();
  });
});
