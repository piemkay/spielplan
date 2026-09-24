import { expect, test } from '@playwright/test';

import { signedIn } from '../helpers.js';

/**
 * Admin > Connectors, the half M5.7 built, end to end. Spec v2.1 §6.6, §7.2, §9, §14.3;
 * decisions 339, 343, 450-455.
 *
 * THE THESIS IS A SEQUENCE, AND ONLY A BROWSER SEES ALL OF IT. §6.6's spend guard is a "per-title
 * cost estimate before enabling": the number appears before the setting persists, and saying no
 * costs nothing (plan §7 checks 1-3). `test_spend_guard.py` proves the route's half - a preview
 * writes no row, a confirm without its figure stores nothing - and the store's vitest proves the
 * page's order against a fetch double; neither can say that the panel an admin reads is on screen
 * while no PUT has left the page, that Cancel sends nothing at all, and that the one PUT a Confirm
 * sends carries the figure the panel rendered. That is the third test here, and the file is built
 * round it: the cap is set before it so the panel has a remaining cap to show, and a provider is
 * keyed before that so there is something to enable.
 *
 * WRITE-ONLY, CHECKED WHERE A KEY WOULD LEAK. A provider key bills the household - §14.3's
 * argument for the media-server key, made about money - so the Gemini card is handed an obviously
 * fake key and the page's markup, every /api/admin answer it receives and every URL it requests
 * are searched for it. That fixture is the only credential this file stores. No source key is
 * saved and no Test button is pressed on any card: the e2e stack has no double for a provider,
 * TMDB, OMDb or Trakt, so either would reach the internet from the stack under test, and the
 * probes are proved against hosts the backend chose (`test_source_probes.py`, `test_llm_api.py`).
 *
 * IT RUNS TWICE ON ONE STACK. The phone project takes this file because plan §7 check 14 - every
 * control on both admin pages at least 48 px - is a phone measurement, and Playwright runs the
 * whole desktop pass first. So every test reads the state it starts from rather than assuming a
 * fresh one, and puts back what it changed: the extraction assignment through the same preview
 * and confirm the thesis proves, the library pick through the route that stores it. Three writes
 * cannot be put back, and each is harmless twice. The Gemini fixture key, because no route deletes
 * a key and no provider is ever called on this stack: stage 6 asks for the stored pack before it
 * builds a client, and stage 5, which stores packs, is not wired (decision 432). The cap, because
 * decision 452 offers no way back to "no cap", so the test alternates it between two figures it
 * derives from the one it finds. And the webhook token, because decision 418 mints it once and
 * never rotates: the desktop run generates it, and the phone run asserts nothing can show it again.
 *
 * NOTHING IS ACQUIRED. The one webhook delivery names an ItemId `ops/fake_jellyfin.py` does not
 * hold, so the intake records it - which is what decision 455's status reads - and the sweep files
 * nothing, and the phone project's 03-library meets the library the rest of the suite built.
 *
 * NO SERVICE WORKER IN THIS CONTEXT, for 19-phone-shell's reason (decision 284): the cap test hands
 * the page a meter at the cap with `page.route`, which a worker in the middle hides from WebKit, and
 * the request logs below are a record of what the page sent only if nothing answered in between.
 */
test.use({ serviceWorkers: 'block' });

test.beforeEach(async ({ page }) => {
  await signedIn(page);
});

/** Strings no provider issues, so finding one anywhere is a leak and never a coincidence. */
const FIXTURE_KEY = {
  gemini: 'e2e-not-a-real-gemini-key',
  openai: 'e2e-not-a-real-openai-key'
};

/** An id the fake Jellyfin does not hold: recorded by the intake, acquired by nobody. */
const NOT_HELD = 'jf-e2e-not-held';

/** A model no price table names, so decision 343 has no figure for it. */
const UNPRICED = 'e2e-unpriced-model';

/** `llm/client.PROVIDERS`, with each adapter's structured-output mode as its card captions it. */
const PROVIDERS = [
  { name: 'anthropic', title: 'Anthropic', caps: 'ANTHROPIC', caption: 'forced tool-use' },
  { name: 'openai', title: 'OpenAI', caps: 'OPENAI', caption: 'strict schema' },
  { name: 'gemini', title: 'Gemini', caps: 'GEMINI', caption: 'responseSchema' }
];

/** §6.6's "TMDB / OMDb / Trakt keys", with the fields each card carries. */
const SOURCES = {
  tmdb: { title: 'TMDB', fields: ['TMDB KEY'] },
  omdb: { title: 'OMDb', fields: ['OMDB KEY'] },
  trakt: { title: 'Trakt', fields: ['TRAKT CLIENT ID', 'TRAKT CLIENT SECRET'] }
};

/** Stage 2's five keyless sources, as the page names them (`GET /admin/connectors`' `keyless`). */
const KEYLESS = ['Wikidata', 'Wikipedia', 'TVmaze', 'Rotten Tomatoes', 'Metacritic'];

const pathOf = (url) => new URL(url).pathname;
const answers = (method, path) => (response) =>
  pathOf(response.url()) === path && response.request().method() === method;
const isPreview = answers('POST', '/api/admin/llm/preview');

/**
 * Load Connectors and return the three reads it rendered from - the provider cards and the meter,
 * the source cards, the Jellyfin card - and the library list when asked for it. Each wait is made
 * before the navigation it races, and each is the page's own response rather than a second call
 * beside it, so an assertion about the screen is an assertion about what the screen was given.
 */
async function openConnectors(page, { libraries = false } = {}) {
  const [llm, sources, jellyfin, listed] = await Promise.all([
    page.waitForResponse(answers('GET', '/api/admin/llm')),
    page.waitForResponse(answers('GET', '/api/admin/connectors')),
    page.waitForResponse(answers('GET', '/api/admin/connectors/jellyfin')),
    libraries ? page.waitForResponse(answers('GET', '/api/admin/connectors/jellyfin/libraries')) : null,
    page.goto('/admin/connectors')
  ]);
  return {
    llm: await llm.json(),
    sources: await sources.json(),
    jellyfin: await jellyfin.json(),
    libraries: listed ? await listed.json() : null
  };
}

/** `GET /api/admin/llm` asked beside the page, for what is STORED rather than what is shown. */
async function llmNow(page) {
  const res = await page.request.get('/api/admin/llm');
  expect(res.ok(), 'GET /api/admin/llm').toBeTruthy();
  return res.json();
}

/** Everything the page sends under /api, in order. `page.request` is not the page's network. */
function recordRequests(page) {
  const log = [];
  page.on('request', (request) => {
    const url = request.url();
    if (!pathOf(url).startsWith('/api/')) return;
    log.push({ method: request.method(), path: pathOf(url), url, body: request.postData() });
  });
  return log;
}

/** What the page sent that could have changed something: every write but a preview (decision 450). */
const writesBesidePreviews = (log) =>
  log.filter((r) => r.method !== 'GET' && !(r.method === 'POST' && r.path === '/api/admin/llm/preview'));

/**
 * A usable key on `name`, so its option can be chosen. Through the key route rather than the card,
 * because the card's own save is the first test's subject and this is a precondition of two others
 * that must not depend on that one having passed.
 */
async function ensureFixtureKey(page, name) {
  const card = (await llmNow(page)).providers.find((provider) => provider.name === name);
  if (card.has_api_key) return;
  const res = await page.request.put(`/api/admin/connectors/${name}`, {
    data: { api_key: FIXTURE_KEY[name] }
  });
  expect(res.ok(), `the ${name} key route refused the fixture key`).toBeTruthy();
}

/**
 * Put the extraction assignment back as the test found it, through the preview and the confirm the
 * thesis proves - the confirm carries the figure the preview answered (decision 450), and an
 * assignment of nobody previews "unknown", which is the figure it is confirmed at.
 */
async function restoreExtraction(page, original) {
  const now = await llmNow(page);
  if ((now.settings.extraction_provider ?? null) === original) return;
  const change = { extraction_provider: original };
  const preview = await page.request.post('/api/admin/llm/preview', { data: change });
  expect(preview.ok(), 'the restoring preview').toBeTruthy();
  const { estimate } = await preview.json();
  const put = await page.request.put('/api/admin/llm', {
    data: { ...change, accepted_estimate: estimate.per_title_usd }
  });
  expect(put.ok(), `restoring extraction_provider to ${original} was refused`).toBeTruthy();
}

/**
 * A dollar figure as the cards print one: every digit the server sent and at least the cents, an
 * exponent written out - `Decimal` spells a product of zero titles `0E-5`.
 */
function dollars(amount) {
  const text = String(amount);
  const plain = /e/i.test(text) ? Number(text).toFixed(10) : text;
  const [whole, fraction = ''] = plain.split('.');
  return `$${whole}.${fraction.replace(/0+$/, '').padEnd(2, '0')}`;
}

/** 19-phone-shell's rule for reading a box: only once nothing is still moving it. */
async function hasStoppedMoving(locator) {
  await locator.evaluate(async (el) => {
    const moving = [];
    for (let node = el; node; node = node.parentElement) moving.push(...node.getAnimations());
    await Promise.all(moving.map((animation) => animation.finished.catch(() => {})));
  });
}

/**
 * Every control inside the page's `main` narrower or shorter than 48 px, named in ASCII.
 *
 * Both axes, for 19-phone-shell's `meetsTheTouchFloor` reason: design.css's coarse block raises
 * `min-height` and never `min-width`, so the narrow axis is where this app's failures were. A
 * checkbox is measured by the label it sits in, because design.css's coarse block leaves checkboxes
 * out and each card makes its label the 48 px target instead. `main` and not the document: the
 * header and the rail are the shell's, measured by the specs that own them.
 */
async function underTheFloor(page) {
  const controls = page
    .locator('main')
    .locator(
      'button, select, input:not([type=checkbox]):not([type=radio]):not([type=hidden]), label.check'
    );
  const short = [];
  const count = await controls.count();
  for (let i = 0; i < count; i++) {
    const control = controls.nth(i);
    if (!(await control.isVisible())) continue;
    await hasStoppedMoving(control);
    const box = await control.boundingBox();
    if (box && box.width >= 48 && box.height >= 48) continue;
    const name = await control.evaluate((el) =>
      (el.getAttribute('aria-label') || el.textContent || el.getAttribute('placeholder') || el.tagName)
        .replace(/\s+/g, ' ')
        .trim()
        .slice(0, 60)
    );
    const size = box ? `${box.width}x${box.height}` : 'no box';
    short.push(`${name.replace(/[^\x20-\x7e]/g, '?')}: ${size}`);
  }
  return short;
}

test('each provider card says what it is and never shows a stored key', async ({ page }) => {
  const key = FIXTURE_KEY.gemini;
  const requests = recordRequests(page);
  const bodies = [];
  page.on('response', (response) => {
    const path = pathOf(response.url());
    if (!path.startsWith('/api/admin')) return;
    bodies.push(response.text().then((body) => ({ path, body }), () => ({ path, body: null })));
  });

  const { llm } = await openConnectors(page);
  expect(llm.providers.map((p) => p.name).sort()).toEqual(PROVIDERS.map((p) => p.name).sort());
  // Nothing in this suite keys Anthropic, so an un-configured card is on the page on both runs.
  expect(llm.providers.some((p) => !p.configured), 'every provider reads configured').toBe(true);

  for (const provider of PROVIDERS) {
    const read = llm.providers.find((p) => p.name === provider.name);
    const card = page.locator(`[data-provider="${provider.name}"]`);
    // §6.6's caption, in the adapter's own word for its mode (decision 338 keeps Gemini off batch).
    await expect(card.locator('[data-structured-output]')).toContainText(provider.caption);
    // Configured exactly when the read says so - a key this SECRETS_KEY opens and a price in force -
    // and an un-configured card says which half is missing instead of looking like one that works.
    await expect(card).toHaveAttribute('data-configured', String(read.configured));
    await expect(card.locator('[data-unconfigured]')).toHaveCount(read.configured ? 0 : 1);
    const field = card.getByLabel(`${provider.caps} KEY`);
    await expect(field).toHaveAttribute('type', 'password');
    await expect(field).toHaveValue('');
    await expect(field).toHaveAttribute('placeholder', read.has_api_key ? /stored/ : /paste a key/);
    await expect(card.getByLabel(`${provider.caps} MODEL`)).toHaveValue(read.model);
    await expect(card.getByRole('button', { name: `Test ${provider.title}`, exact: true })).toBeVisible();
  }

  const gemini = page.locator('[data-provider="gemini"]');
  const field = gemini.getByLabel('GEMINI KEY');
  const save = gemini.getByRole('button', { name: 'Save Gemini key', exact: true });
  // An empty field is not a save at all: the half of "empty keeps the stored key" a page can hold
  // by itself (decision 452).
  await expect(save).toBeDisabled();
  await field.fill(key);
  const stored = page.waitForResponse(answers('PUT', '/api/admin/connectors/gemini'));
  await save.click();
  const answer = await stored;
  expect(answer.status()).toBe(200);
  expect(answer.request().postDataJSON()).toEqual({ api_key: key });
  // Booleans back, never a value, a prefix or a length (§14.3).
  expect(await answer.json()).toEqual({ name: 'gemini', has_api_key: true, secrets_unreadable: false });
  // Emptied, so the next person at the screen finds no pasted key, and the placeholder is the only
  // thing on the card that says a key exists.
  await expect(field).toHaveValue('');
  await expect(field).toHaveAttribute('placeholder', /stored/);
  await expect(gemini).toHaveAttribute('data-configured', 'true');

  await openConnectors(page);
  await expect(gemini.getByLabel('GEMINI KEY')).toHaveAttribute('placeholder', /stored/);
  await expect(gemini.getByLabel('GEMINI KEY')).toHaveValue('');
  expect(await page.content()).not.toContain(key);

  // Saved with the field empty, the key stays. The card cannot send that form, so the route is
  // asked as a client that sent it would ask - the Jellyfin card's idiom, which a card that cleared
  // on an empty save would break by disconnecting a provider mid-acquisition.
  const kept = await page.request.put('/api/admin/connectors/gemini', { data: { api_key: '' } });
  expect(kept.status()).toBe(200);
  expect(await kept.json()).toEqual({ name: 'gemini', has_api_key: true, secrets_unreadable: false });
  const after = await llmNow(page);
  expect(after.providers.find((p) => p.name === 'gemini').has_api_key).toBe(true);
  expect(JSON.stringify(after)).not.toContain(key);

  const seen = await Promise.all(bodies);
  expect(seen.some(({ body }) => body !== null), 'no /api/admin answer was read at all').toBe(true);
  for (const { path, body } of seen) {
    if (body !== null) expect(body, `${path} answered with the key in it`).not.toContain(key);
  }
  // A key belongs in a body or a header and never in a URL, where every proxy log keeps it.
  expect(requests.filter((r) => r.url.includes(key)).map((r) => r.path)).toEqual([]);
});

test('the cap is edited in place and the meter reads against it', async ({ page }) => {
  const { llm: before } = await openConnectors(page);
  const meter = page.getByTestId('spend-meter');
  const capped = before.meter.cap_usd !== null;
  const overAt = (cap) => Number(before.meter.spent_usd) >= Number(cap);
  await expect(meter).toHaveAttribute(
    'data-meter-state',
    !capped ? 'no-cap' : overAt(before.meter.cap_usd) ? 'over-cap' : 'under-cap'
  );
  // §9's fivefold undercount is invisible to an operator anywhere else, so it is on every reading.
  await expect(meter.locator('[data-meter-caption]')).toContainText('thinking tokens');

  // Two figures exact in binary, alternated: decision 452 offers no way back to "no cap", so each
  // run moves the cap to the figure it did not find, and the move is what proves the write.
  const next = Number(before.meter.cap_usd) === 5 ? 6.25 : 5;
  await meter.getByLabel('MONTHLY CAP').fill(String(next));
  const written = page.waitForResponse(answers('PUT', '/api/admin/llm/cap'));
  await meter.getByRole('button', { name: 'Set cap', exact: true }).click();
  const answer = await written;
  expect(answer.status()).toBe(200);
  expect(answer.request().postDataJSON()).toEqual({ cap_usd: next });
  expect(Number((await answer.json()).meter.cap_usd)).toBe(next);

  // In force at once and in place: the reading moves on this page, with no reload and no preview,
  // because the cap is the guard itself and enables no provider (decision 452).
  await expect(meter.locator('[data-meter-reading]')).toContainText(`of ${dollars(next)} this month`);
  if (overAt(next)) {
    await expect(meter).toHaveAttribute('data-meter-state', 'over-cap');
  } else {
    await expect(meter).toHaveAttribute('data-meter-state', 'under-cap');
    await expect(meter).toContainText(/\$\d+\.\d{2,} left/);
  }
  expect(Number((await llmNow(page)).meter.cap_usd)).toBe(next);

  // At the cap, which no gesture on this stack can reach: nothing here is ever billed, so the seam
  // is the response - the live read with the month spent to the cap, as 18-system hands its card an
  // unreadable custody. The park itself and the retry's refusal are the backend's to prove
  // (`test_llm_stage.py`); what only this layer can say is that the card says so.
  const live = await llmNow(page);
  const atCap = { ...live, meter: { ...live.meter, spent_usd: live.meter.cap_usd, remaining_usd: '0' } };
  await page.route('**/api/admin/llm', (route) =>
    route.request().method() === 'GET' ? route.fulfill({ json: atCap }) : route.continue()
  );
  try {
    const [read] = await Promise.all([
      page.waitForResponse(answers('GET', '/api/admin/llm')),
      page.reload()
    ]);
    expect((await read.json()).meter.spent_usd).toBe(live.meter.cap_usd);
    await expect(meter).toHaveAttribute('data-meter-state', 'over-cap');
    const over = meter.locator('[data-meter-over-cap]');
    // The board's own word for the park (§8), and what an admin retry meets: the refusal is M5.6's
    // route, and this card is where an admin learns it is coming.
    await expect(over).toContainText('over spend cap');
    await expect(over).toContainText(/admin\s+retry\s+that\s+would\s+breach\s+it\s+is\s+refused/);
    await expect(meter.locator('[data-meter-caption]')).toContainText('thinking tokens');
  } finally {
    await page.unroute('**/api/admin/llm');
  }
});

test('enabling extraction shows the estimate before anything is saved', async ({ page }) => {
  const found = await llmNow(page);
  const original = found.settings.extraction_provider ?? null;
  // A keyed provider that is not the one stored, so choosing it is a change. Gemini is the one this
  // suite keys; OpenAI is keyed only when a run left Gemini assigned, which the restore prevents.
  const target = original === 'gemini' ? 'openai' : 'gemini';
  await ensureFixtureKey(page, target);

  const requests = recordRequests(page);
  const { llm: before } = await openConnectors(page);
  expect(before.settings.extraction_provider ?? null).toBe(original);
  const puts = () => requests.filter((r) => r.method === 'PUT' && r.path === '/api/admin/llm');
  const extraction = page.getByTestId('llm-extraction');
  const select = extraction.getByLabel('EXTRACTION PROVIDER');
  await expect(select.locator(`option[value="${target}"]`)).toBeEnabled();

  try {
    const asked = page.waitForResponse(isPreview);
    await select.selectOption(target);
    const previewed = await asked;
    expect(previewed.request().postDataJSON()).toEqual({ extraction_provider: target });
    const preview = await previewed.json();

    // Plan §7 check 1's three figures, on screen. The per-title estimate as a figure...
    const panel = page.getByTestId('spend-estimate');
    await expect(panel).toHaveAttribute('data-estimate-state', 'pending');
    const perTitle = panel.locator('[data-per-title]');
    expect(preview.estimate.per_title_usd).toMatch(/^\d+(\.\d+)?$/);
    await expect(perTitle).toHaveAttribute('data-per-title', preview.estimate.per_title_usd);
    await expect(perTitle).toContainText(dollars(preview.estimate.per_title_usd));
    // ...the projected month, or the sentence for an install that has filed nothing (decision 451)...
    const month = panel.locator('[data-projected]');
    const monthly = preview.projected.monthly_usd;
    if (monthly === null) {
      await expect(month).toHaveAttribute('data-projected', 'no-history');
      await expect(month).toContainText('no acquisition history yet');
    } else if (monthly === 'unknown') {
      await expect(month).toHaveAttribute('data-projected', 'unknown');
    } else {
      await expect(month).toHaveAttribute('data-projected', 'figure');
      await expect(month).toContainText(`${dollars(monthly)} a month`);
    }
    // ...and what is left of the cap, which the test before this one set.
    expect(preview.meter).toHaveProperty('remaining_usd');
    if (preview.projected.remaining_usd !== null) {
      await expect(month).toContainText(`against ${dollars(preview.projected.remaining_usd)} left`);
    } else {
      await expect(month).toContainText('with no cap set');
    }

    // Check 2: the figure is on screen and nothing is stored, nor asked to be.
    expect(puts(), 'a PUT left the page before Confirm').toEqual([]);
    expect(writesBesidePreviews(requests)).toEqual([]);
    expect((await llmNow(page)).settings).toEqual(before.settings);

    // Saying no costs nothing: Cancel sends no request and the stored plan is back on screen.
    const sentBeforeCancel = requests.length;
    await panel.getByRole('button', { name: 'Cancel', exact: true }).click();
    await expect(page.getByTestId('spend-estimate')).toHaveCount(0);
    await expect(extraction.locator('[data-plan="stored"]')).toBeVisible();
    await expect(select).toHaveValue(original ?? '');
    expect((await llmNow(page)).settings).toEqual(before.settings);
    expect(requests.slice(sentBeforeCancel).filter((r) => r.method !== 'GET')).toEqual([]);

    // Check 3: the confirm stores the change, carries the figure the panel rendered, and bills
    // nothing.
    const again = page.waitForResponse(isPreview);
    await select.selectOption(target);
    await again;
    await expect(panel).toHaveAttribute('data-estimate-state', 'pending');
    const shown = await panel.locator('[data-per-title]').getAttribute('data-per-title');
    const storing = page.waitForResponse(answers('PUT', '/api/admin/llm'));
    await panel.getByRole('button', { name: 'Confirm', exact: true }).click();
    const confirmed = await storing;
    expect(confirmed.status()).toBe(200);
    expect(puts()).toHaveLength(1);
    expect(confirmed.request().postDataJSON()).toEqual({
      extraction_provider: target,
      accepted_estimate: shown
    });
    // The estimate came first: a preview answered for this change before the one PUT was sent.
    const order = requests.map((r) => `${r.method} ${r.path}`);
    expect(order.indexOf('POST /api/admin/llm/preview')).toBeLessThan(order.indexOf('PUT /api/admin/llm'));
    await expect(page.getByTestId('spend-estimate')).toHaveCount(0);
    await expect(extraction.locator('[data-plan="stored"] [data-per-title]')).toHaveAttribute(
      'data-per-title',
      shown
    );

    const after = await llmNow(page);
    expect(after.settings).toEqual({ ...before.settings, extraction_provider: target });
    expect(after.meter.spent_usd, 'a confirm billed something').toBe(before.meter.spent_usd);
    expect(after.meter.unsettled_usd).toBe(before.meter.unsettled_usd);
  } finally {
    await restoreExtraction(page, original);
  }
});

test('the estimate names the model and the price it used', async ({ page }) => {
  await ensureFixtureKey(page, 'gemini');
  const requests = recordRequests(page);
  const { llm } = await openConnectors(page);
  const gemini = llm.providers.find((p) => p.name === 'gemini');
  // Nothing in this suite stores another Gemini model: this file proposes one and cancels it.
  expect(gemini.model).toBe('gemini-3.7-flash');
  const basis = gemini.price_basis;
  expect(basis, 'the shipped table prices gemini-3.7-flash').not.toBe('unknown');
  expect(basis).toMatchObject({ provider: 'gemini', model: 'gemini-3.7-flash', source: 'table' });
  // Decision 343's dated price: introductory until 2027-01-01, and doubled from that day.
  if (Date.now() < Date.UTC(2027, 0, 1)) {
    expect(basis.valid_until).toBe('2027-01-01');
    expect(basis.then).not.toBeNull();
  }
  const later = basis.then && `${dollars(basis.then.input)} / ${dollars(basis.then.output)}`;
  const caption = [
    basis.model,
    `${dollars(basis.input)} in / ${dollars(basis.output)} out per 1M tokens`,
    'shipped table',
    ...(basis.valid_until ? [`valid until ${basis.valid_until}, then ${later}`] : [])
  ];

  // On the card, before anything is proposed.
  const card = page.locator('[data-provider="gemini"]');
  await expect(card.locator('[data-price-basis]')).toHaveAttribute('data-price-basis', 'table');
  for (const part of caption) await expect(card.locator('[data-price-basis]')).toContainText(part);

  // And beside the estimate itself, which is where a figure is accepted.
  const extraction = page.getByTestId('llm-extraction');
  const panel = page.getByTestId('spend-estimate');
  if ((llm.settings.extraction_provider ?? null) !== 'gemini') {
    const asked = page.waitForResponse(isPreview);
    await extraction.getByLabel('EXTRACTION PROVIDER').selectOption('gemini');
    await asked;
    await expect(panel).toHaveAttribute('data-estimate-state', 'pending');
  }
  const named = extraction.locator('[data-basis="gemini"]');
  for (const part of caption) await expect(named).toContainText(part);
  await expect(extraction).toContainText('23,500 tokens in');

  // A model nobody priced is "unknown", never a number (decision 343): the plan is refused and
  // names the model it refused, and no dollar figure stands for it. Committed with Enter, so the
  // `change` the card proposes on is the engine's own and the field is still focused when Cancel is
  // pressed. Not a dispatched `change`: that one leaves the engine still owing its own for the typed
  // value, which it pays when focus leaves - on Cancel's mousedown, where the card re-proposed the
  // same plan, the figure dropped for its re-ask, and the panel shrank under the pointer before the
  // mouseup, so the click never reached Cancel. No user can put that second `change` there.
  const model = card.getByLabel('GEMINI MODEL');
  const asked = page.waitForResponse(isPreview);
  await model.fill(UNPRICED);
  await model.press('Enter');
  const previewed = await asked;
  expect(previewed.request().postDataJSON().providers).toEqual({ gemini: { model: UNPRICED } });
  const preview = await previewed.json();
  expect(preview.estimate.per_title_usd).toBe('unknown');
  await expect(panel).toHaveAttribute('data-estimate-state', 'pending');
  const perTitle = panel.locator('[data-per-title]');
  await expect(perTitle).toHaveAttribute('data-per-title', 'unknown');
  await expect(perTitle).toContainText('unknown');
  await expect(perTitle).not.toContainText('$');
  await expect(panel.locator('[data-unknown-reason]')).toContainText(UNPRICED);
  await expect(panel.locator('[data-basis]')).toHaveCount(0);
  await expect(panel.locator('[data-projected]')).not.toHaveAttribute('data-projected', 'figure');

  // Nothing was confirmed, so nothing is put back - asserted rather than assumed.
  await panel.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByTestId('spend-estimate')).toHaveCount(0);
  await expect(model).toHaveValue(gemini.model);
  expect(writesBesidePreviews(requests)).toEqual([]);
  const after = await llmNow(page);
  expect(after.settings).toEqual(llm.settings);
  expect(after.providers.find((p) => p.name === 'gemini').model).toBe(gemini.model);
});

test('the source cards take a key and say which stage needs it', async ({ page }) => {
  const requests = recordRequests(page);
  const { sources } = await openConnectors(page);
  expect(sources.sources.map((s) => s.name)).toEqual(Object.keys(SOURCES));
  // Booleans and the two facts a stage-2 failure needs, never a credential (decision 452).
  const allowed = [
    'name',
    'has_api_key',
    'has_client_id',
    'has_client_secret',
    'secrets_unreadable',
    'required',
    'used_by'
  ];

  for (const source of sources.sources) {
    expect(Object.keys(source).filter((field) => !allowed.includes(field))).toEqual([]);
    const { title, fields } = SOURCES[source.name];
    const card = page.locator(`[data-source="${source.name}"]`);
    // Decision 334's rule on the card: TMDB is the one source whose missing key parks a title at
    // stage 2, so a stage-2 failure points at the card that can fix it (plan C2).
    expect(source.required).toBe(source.name === 'tmdb');
    await expect(card).toHaveAttribute('data-required', String(source.required));
    await expect(card).toContainText(source.required ? 'Required' : 'Best-effort');
    await expect(card).toContainText('stage 2');
    if (source.used_by) await expect(card).toContainText(source.used_by);
    const held =
      source.name === 'trakt' ? source.has_client_id && source.has_client_secret : source.has_api_key;
    await expect(card).toHaveAttribute('data-has-key', String(Boolean(held)));
    for (const label of fields) {
      await expect(card.getByLabel(label)).toHaveAttribute('type', 'password');
      await expect(card.getByLabel(label)).toHaveValue('');
    }
    await expect(card.getByRole('button', { name: `Test ${title}`, exact: true })).toBeVisible();

    // It takes a key: typing one arms Save and emptying the field disarms it. Nothing is saved - a
    // stored source key would have this stack's own stage 2 ask the real host - so the write is the
    // provider card's above, through the same `saveKey`, and the route's (`test_spend_guard.py`).
    const save = card.getByRole('button', { name: `Save ${title} key`, exact: true });
    await expect(save).toBeDisabled();
    const first = card.getByLabel(fields[0]);
    await first.fill(`e2e-not-a-real-${source.name}-key`);
    await expect(save).toBeEnabled();
    await first.fill('');
    await expect(save).toBeDisabled();
  }
  await expect(page.locator('[data-source="omdb"] [data-quota]')).toContainText('daily quota');

  // The five that need no key are named once, so a failure from one of them is not chased here.
  expect(sources.keyless).toHaveLength(KEYLESS.length);
  for (const name of KEYLESS) await expect(page.locator('[data-keyless]')).toContainText(name);
  expect(requests.filter((r) => r.method !== 'GET')).toEqual([]);
});

test('the jellyfin card picks libraries and keeps the pick', async ({ page }) => {
  const { jellyfin, libraries } = await openConnectors(page, { libraries: true });
  const original = [...jellyfin.library_ids];
  const card = page.getByTestId('connector-jellyfin');
  const pick = card.locator('[data-library-pick]');
  await expect(pick).toHaveAttribute('data-library-pick', 'ready');
  expect(libraries.ok, 'the fake Jellyfin lists its libraries').toBe(true);
  const byName = Object.fromEntries(libraries.libraries.map((lib) => [lib.name, lib.id]));
  // `ops/fake_jellyfin.py`'s three, listed as the server names them.
  expect(Object.keys(byName)).toEqual(expect.arrayContaining(['Films', 'Shows', 'Home Videos']));
  const box = (name) => pick.getByRole('checkbox', { name, exact: true });
  for (const lib of libraries.libraries) {
    await expect(box(lib.name)).toBeChecked({ checked: original.includes(lib.id) });
  }
  const save = card.getByRole('button', { name: 'Save library pick', exact: true });
  // Sent only when it changed (decision 455): nothing has, so nothing can be sent.
  await expect(save).toBeDisabled();

  const target = original.length === 1 && original[0] === byName.Shows ? [byName.Films] : [byName.Shows];
  try {
    for (const lib of libraries.libraries) {
      if (target.includes(lib.id)) await box(lib.name).check();
      else await box(lib.name).uncheck();
    }
    await expect(save).toBeEnabled();
    const stored = page.waitForResponse(answers('PUT', '/api/admin/connectors/jellyfin'));
    await save.click();
    const answer = await stored;
    expect(answer.status()).toBe(200);
    // Its own write, and nothing else in it: no URL, no key, no mint (decisions 418, 455).
    expect(answer.request().postDataJSON()).toEqual({ library_ids: target });
    expect((await answer.json()).library_ids).toEqual(target);
    const read = await (await page.request.get('/api/admin/connectors/jellyfin')).json();
    expect(read.library_ids).toEqual(target);

    await openConnectors(page, { libraries: true });
    await expect(pick).toHaveAttribute('data-library-pick', 'ready');
    for (const lib of libraries.libraries) {
      await expect(box(lib.name)).toBeChecked({ checked: target.includes(lib.id) });
    }
    await expect(save).toBeDisabled();
  } finally {
    // Put back through the route the pick is stored by. `[]` is the whole server (decision 364),
    // which is what an empty pick already meant before this test.
    const res = await page.request.put('/api/admin/connectors/jellyfin', {
      data: { library_ids: original }
    });
    expect(res.ok(), 'restoring the library pick').toBeTruthy();
    expect((await res.json()).library_ids).toEqual(original);
  }
});

test('the jellyfin card says when the last ItemAdded arrived', async ({ page }) => {
  const { jellyfin: before } = await openConnectors(page);
  const card = page.getByTestId('connector-jellyfin');
  expect(before.trigger, 'the Jellyfin card is read with its trigger status').toBeTruthy();
  const generate = card.getByRole('button', { name: 'Generate webhook token', exact: true });
  const state = card.locator('[data-webhook-token-state]');
  let current = before;

  if (before.has_webhook_token === false) {
    // Decision 418: minted only by a press that asks for it, shown once, kept in the component.
    await expect(state).toHaveAttribute('data-webhook-token-state', 'none');
    const minted = page.waitForResponse(answers('PUT', '/api/admin/connectors/jellyfin'));
    await generate.click();
    const answer = await minted;
    expect(answer.status()).toBe(200);
    expect(answer.request().postDataJSON()).toEqual({ mint_webhook_token: true });
    const token = (await answer.json()).webhook_token;
    expect(typeof token === 'string' && token.length > 0, 'the minting save answered no token').toBe(
      true
    );
    await expect(state).toHaveAttribute('data-webhook-token-state', 'revealed');
    await expect(card.locator('[data-webhook-token]')).toHaveText(token);
    // Everything the plugin needs beside it: the header and the path (§7.2).
    await expect(state).toContainText('X-Spielplan-Token');
    await expect(state).toContainText('/events/jellyfin');
    await expect(generate).toHaveCount(0);

    // One ItemAdded, built from `ops/jellyfin-webhook-template.json`'s fields, for an item the fake
    // does not hold: recorded and answered 202, and acquired by nobody.
    const delivered = await page.request.post('/events/jellyfin', {
      headers: { 'X-Spielplan-Token': token },
      data: {
        Name: 'e2e webhook probe',
        SeriesName: '',
        Year: '',
        NotificationType: 'ItemAdded',
        ItemId: NOT_HELD,
        ItemType: 'Movie',
        SeriesId: ''
      }
    });
    expect(delivered.status()).toBe(202);
    expect((await delivered.json()).state).toBe('pending');

    // Once: a reload has no way to show it again, and the card says one exists.
    ({ jellyfin: current } = await openConnectors(page));
    expect(current.has_webhook_token).toBe(true);
    await expect(state).toHaveAttribute('data-webhook-token-state', 'exists');
    await expect(card.locator('[data-webhook-token]')).toHaveCount(0);
    expect(await page.content()).not.toContain(token);
    expect(JSON.stringify(current)).not.toContain(token);
    const was = before.trigger.webhook.last_item_added_at;
    expect(current.trigger.webhook.last_item_added_at).not.toBeNull();
    if (was) {
      expect(Date.parse(current.trigger.webhook.last_item_added_at)).toBeGreaterThan(Date.parse(was));
    }
    expect(current.trigger.webhook.deliveries_7d).toBeGreaterThan(before.trigger.webhook.deliveries_7d);
  } else {
    // The phone run, after the desktop run minted it: nothing on the card can show it or mint
    // another, and the status still names the add that token delivered.
    await expect(generate).toHaveCount(0);
    await expect(state).toHaveAttribute('data-webhook-token-state', 'exists');
    expect(
      before.trigger.webhook.last_item_added_at,
      'a token exists that this run did not mint and no ItemAdded ever arrived with it - run the ' +
        'suite on a reset stack (e2e/run.mjs), where the desktop pass mints it and delivers one'
    ).not.toBeNull();
  }

  // §6.6's "webhook status" as a fact about what arrived, not a mode flag (decision 455).
  const webhook = card.locator('[data-trigger="webhook"]');
  await expect(webhook).toHaveAttribute('data-item-added', 'received');
  await expect(webhook).toContainText('last ItemAdded');
  await expect(webhook).not.toContainText('none received yet');
  await expect(card.locator('[data-trigger="delta-poll"]')).toBeVisible();
  expect(current.trigger.webhook.last_item_added_at).not.toBeNull();
});

test('every control on both admin pages is at least 48 px on the phone', async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== 'phone',
    'the 48 px floor is a statement about a finger (section 6 preamble), measured on the phone'
  );
  await openConnectors(page, { libraries: true });
  // Everything the page draws once its reads have answered, so no control is measured by absence.
  await expect(page.getByTestId('spend-meter')).not.toHaveAttribute('data-meter-state', 'unread');
  await expect(page.locator('[data-provider]')).toHaveCount(PROVIDERS.length);
  await expect(page.locator('[data-source]')).toHaveCount(Object.keys(SOURCES).length);
  await expect(page.locator('[data-library-pick]')).toHaveAttribute('data-library-pick', 'ready');
  await expect(page.locator('tr[data-user]').first()).toBeVisible();
  expect(await underTheFloor(page), 'on /admin/connectors').toEqual([]);

  const [system] = await Promise.all([
    page.waitForResponse(answers('GET', '/api/admin/system')),
    page.goto('/admin/system')
  ]);
  expect(system.ok()).toBeTruthy();
  await expect(page.getByTestId('system-logs')).toBeVisible();
  expect(await underTheFloor(page), 'on /admin/system').toEqual([]);
});
