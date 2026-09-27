import { expect, test } from '@playwright/test';

import { signedIn } from '../helpers.js';

/**
 * Admin > Services and Admin > Budget & AI (§6.6, §7.2, §9, §14.3; decisions 339, 343, 450-455,
 * 527). The thesis is §6.6's spend guard as a sequence: the estimate is on screen while no PUT has
 * left, Cancel sends nothing, and Confirm's one PUT carries the figure shown. Keys are write-only
 * and searched for in every answer and URL; no source key is saved and no Test pressed, since
 * nothing here may reach the internet.
 *
 * It runs on both projects against one stack, so each test reads its starting state and puts back
 * what it changed. Three writes cannot be undone and are harmless twice: the fake Gemini key (no
 * provider is ever called), the cap (no way back to "no cap", decision 452, so it alternates), and
 * the webhook token (minted once, decision 418). The one webhook delivery names an item the fake
 * does not hold, so nothing is acquired. No service worker, for decision 284's reason.
 */
test.use({ serviceWorkers: 'block' });

test.beforeEach(async ({ page }) => {
  await signedIn(page);
});

// Strings no provider issues, so finding one anywhere is a leak.
const FIXTURE_KEY = {
  gemini: 'e2e-not-a-real-gemini-key',
  openai: 'e2e-not-a-real-openai-key'
};

const NOT_HELD = 'jf-e2e-not-held';

// A model no price table names (decision 343).
const UNPRICED = 'e2e-unpriced-model';

const PROVIDERS = [
  { name: 'anthropic', caption: 'forced tool-use' },
  { name: 'openai', caption: 'strict schema' },
  { name: 'gemini', caption: 'responseSchema' }
];

const SOURCES = {
  tmdb: ['API key'],
  omdb: ['API key'],
  trakt: ['Client ID', 'Client secret']
};

const KEYLESS = ['Wikidata', 'Wikipedia', 'TVmaze', 'Rotten Tomatoes', 'Metacritic'];

const pathOf = (url) => new URL(url).pathname;
const answers = (method, path) => (response) =>
  pathOf(response.url()) === path && response.request().method() === method;
const isPreview = answers('POST', '/api/admin/llm/preview');

/** Load Services and return the page's own reads, each waited for before the navigation. */
async function openServices(page, { libraries = false } = {}) {
  const [sources, jellyfin, listed] = await Promise.all([
    page.waitForResponse(answers('GET', '/api/admin/connectors')),
    page.waitForResponse(answers('GET', '/api/admin/connectors/jellyfin')),
    libraries ? page.waitForResponse(answers('GET', '/api/admin/connectors/jellyfin/libraries')) : null,
    page.goto('/admin/services')
  ]);
  return {
    sources: await sources.json(),
    jellyfin: await jellyfin.json(),
    libraries: listed ? await listed.json() : null
  };
}

/** Load Budget & AI and return its spend read. */
async function openBudget(page) {
  const [llm] = await Promise.all([
    page.waitForResponse(answers('GET', '/api/admin/llm')),
    page.goto('/admin/budget')
  ]);
  const read = await llm.json();
  await expect(page.getByTestId('spend-meter')).not.toHaveAttribute('data-meter-state', 'unread');
  return { llm: read };
}

/** The open sheet, and its Done. Done is Back, so the sheet leaves the page. */
const sheet = (page) => page.getByRole('dialog');
async function done(page) {
  await sheet(page).getByRole('button', { name: 'Done', exact: true }).click();
  await expect(sheet(page)).toHaveCount(0);
}

/** What is STORED, asked beside the page. */
async function llmNow(page) {
  const res = await page.request.get('/api/admin/llm');
  expect(res.ok(), 'GET /api/admin/llm').toBeTruthy();
  return res.json();
}

/** Everything the page sends under /api, in order; `page.request` is not the page's network. */
function recordRequests(page) {
  const log = [];
  page.on('request', (request) => {
    const url = request.url();
    if (!pathOf(url).startsWith('/api/')) return;
    log.push({ method: request.method(), path: pathOf(url), url, body: request.postData() });
  });
  return log;
}

// Every write but a preview (decision 450).
const writesBesidePreviews = (log) =>
  log.filter((r) => r.method !== 'GET' && !(r.method === 'POST' && r.path === '/api/admin/llm/preview'));

/** A usable key on `name`, through the route, so no test depends on the card test passing. */
async function ensureFixtureKey(page, name) {
  const card = (await llmNow(page)).providers.find((provider) => provider.name === name);
  if (card.has_api_key) return;
  const res = await page.request.put(`/api/admin/connectors/${name}`, {
    data: { api_key: FIXTURE_KEY[name] }
  });
  expect(res.ok(), `the ${name} key route refused the fixture key`).toBeTruthy();
}

/** Restore the extraction assignment through preview and confirm (decision 450). */
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

/** A dollar figure as the cards print one. `Decimal` can spell zero as `0E-5`. */
function dollars(amount) {
  const text = String(amount);
  const plain = /e/i.test(text) ? Number(text).toFixed(10) : text;
  const [whole, fraction = ''] = plain.split('.');
  return `$${whole}.${fraction.replace(/0+$/, '').padEnd(2, '0')}`;
}

// 19-phone-shell's rule: measure a box only once nothing is moving it.
async function hasStoppedMoving(locator) {
  await locator.evaluate(async (el) => {
    const moving = [];
    for (let node = el; node; node = node.parentElement) moving.push(...node.getAnimations());
    await Promise.all(moving.map((animation) => animation.finished.catch(() => {})));
  });
}

/**
 * Every control in `main` under 48 px in either axis, named in ASCII. A checkbox is measured by
 * its label, which each card makes the 48 px target.
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

test('each provider says what it is and never shows a stored key', async ({ page }) => {
  const key = FIXTURE_KEY.gemini;
  const requests = recordRequests(page);
  const bodies = [];
  page.on('response', (response) => {
    const path = pathOf(response.url());
    if (!path.startsWith('/api/admin')) return;
    bodies.push(response.text().then((body) => ({ path, body }), () => ({ path, body: null })));
  });

  const { llm } = await openBudget(page);
  expect(llm.providers.map((p) => p.name).sort()).toEqual(PROVIDERS.map((p) => p.name).sort());
  expect(llm.providers.some((p) => !p.configured), 'every provider reads configured').toBe(true);

  for (const provider of PROVIDERS) {
    const read = llm.providers.find((p) => p.name === provider.name);
    await page.locator(`[data-provider-row="${provider.name}"]`).click();
    const card = sheet(page).locator(`[data-provider="${provider.name}"]`);
    await expect(card.locator('[data-structured-output]')).toContainText(provider.caption);
    // An un-configured provider says which half is missing.
    await expect(card).toHaveAttribute('data-configured', String(read.configured));
    await expect(card.locator('[data-unconfigured]')).toHaveCount(read.configured ? 0 : 1);
    const field = card.getByLabel('API key', { exact: true });
    await expect(field).toHaveAttribute('type', 'password');
    await expect(field).toHaveValue('');
    await expect(field).toHaveAttribute(
      'placeholder',
      read.has_api_key ? 'Saved' : /Paste a key|paste it again/
    );
    await expect(card.getByLabel('Model', { exact: true })).toHaveValue(read.model);
    await expect(card.getByRole('button', { name: 'Test key', exact: true })).toBeVisible();
    await done(page);
  }

  await page.locator('[data-provider-row="gemini"]').click();
  const gemini = sheet(page).locator('[data-provider="gemini"]');
  const field = gemini.getByLabel('API key', { exact: true });
  const save = gemini.getByRole('button', { name: 'Save key', exact: true });
  // An empty field is not a save (decision 452).
  await expect(save).toBeDisabled();
  await field.fill(key);
  const stored = page.waitForResponse(answers('PUT', '/api/admin/connectors/gemini'));
  await save.click();
  const answer = await stored;
  expect(answer.status()).toBe(200);
  expect(answer.request().postDataJSON()).toEqual({ api_key: key });
  // Booleans back, never a value, a prefix or a length (§14.3).
  expect(await answer.json()).toEqual({ name: 'gemini', has_api_key: true, secrets_unreadable: false });
  // Emptied: only the placeholder says a key exists.
  await expect(field).toHaveValue('');
  await expect(field).toHaveAttribute('placeholder', 'Saved');
  await expect(gemini).toHaveAttribute('data-configured', 'true');
  await expect(page.locator('[data-provider-row="gemini"]')).not.toContainText('No key');

  await openBudget(page);
  await page.locator('[data-provider-row="gemini"]').click();
  await expect(gemini.getByLabel('API key', { exact: true })).toHaveAttribute('placeholder', 'Saved');
  await expect(gemini.getByLabel('API key', { exact: true })).toHaveValue('');
  expect(await page.content()).not.toContain(key);

  // An empty save keeps the key; the card cannot send one, so the route is asked directly.
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
  // Never in a URL, where every proxy log keeps it.
  expect(requests.filter((r) => r.url.includes(key)).map((r) => r.path)).toEqual([]);
});

test('the cap is edited in place and the meter reads against it', async ({ page }) => {
  const { llm: before } = await openBudget(page);
  const meter = page.getByTestId('spend-meter');
  const capped = before.meter.cap_usd !== null;
  const overAt = (cap) => Number(before.meter.spent_usd) >= Number(cap);
  await expect(meter).toHaveAttribute(
    'data-meter-state',
    !capped ? 'no-cap' : overAt(before.meter.cap_usd) ? 'over-cap' : 'under-cap'
  );
  // §9's thinking-token undercount, on every reading.
  await expect(meter.locator('[data-meter-caption]')).toContainText('thinking tokens');

  // Two figures exact in binary, alternated: there is no way back to "no cap" (decision 452).
  const next = Number(before.meter.cap_usd) === 5 ? 6.25 : 5;
  await meter.getByTestId('cap-open').click();
  await meter.getByLabel('Monthly cap').fill(String(next));
  const written = page.waitForResponse(answers('PUT', '/api/admin/llm/cap'));
  await meter.getByRole('button', { name: 'Set cap', exact: true }).click();
  const answer = await written;
  expect(answer.status()).toBe(200);
  expect(answer.request().postDataJSON()).toEqual({ cap_usd: next });
  expect(Number((await answer.json()).meter.cap_usd)).toBe(next);

  // In force at once, with no preview: the cap enables nothing (decision 452).
  await expect(meter.locator('[data-meter-reading]')).toContainText(`of ${dollars(next)} this month`);
  if (overAt(next)) {
    await expect(meter).toHaveAttribute('data-meter-state', 'over-cap');
  } else {
    await expect(meter).toHaveAttribute('data-meter-state', 'under-cap');
    await expect(meter).toContainText(/\$\d+\.\d{2,} left/);
  }
  expect(Number((await llmNow(page)).meter.cap_usd)).toBe(next);

  // At the cap, unreachable here since nothing is billed: the live read, spent to the cap.
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
    // The board's word for the park (§8), and the admin retry's refusal.
    await expect(over).toContainText('over spend cap');
    await expect(over).toContainText(/admin\s+retry\s+that\s+would\s+pass\s+the\s+cap\s+is\s+refused/);
    // The one primary action on the page is the way out.
    await expect(meter.getByRole('button', { name: 'Raise the cap', exact: true })).toBeVisible();
    await expect(meter.locator('[data-meter-caption]')).toContainText('thinking tokens');
  } finally {
    await page.unroute('**/api/admin/llm');
  }
});

test('enabling extraction shows the estimate before anything is saved', async ({ page }) => {
  const found = await llmNow(page);
  const original = found.settings.extraction_provider ?? null;
  // A keyed provider that is not the one stored, so choosing it is a change.
  const target = original === 'gemini' ? 'openai' : 'gemini';
  await ensureFixtureKey(page, target);

  const requests = recordRequests(page);
  const { llm: before } = await openBudget(page);
  expect(before.settings.extraction_provider ?? null).toBe(original);
  const puts = () => requests.filter((r) => r.method === 'PUT' && r.path === '/api/admin/llm');
  const extraction = page.getByTestId('llm-extraction');
  const select = extraction.getByLabel('Provider', { exact: true });
  await expect(select.locator(`option[value="${target}"]`)).toBeEnabled();

  try {
    const asked = page.waitForResponse(isPreview);
    await select.selectOption(target);
    const previewed = await asked;
    expect(previewed.request().postDataJSON()).toEqual({ extraction_provider: target });
    const preview = await previewed.json();

    // The per-title estimate...
    const panel = page.getByTestId('spend-estimate');
    await expect(panel).toHaveAttribute('data-estimate-state', 'pending');
    const perTitle = panel.locator('[data-per-title]');
    expect(preview.estimate.per_title_usd).toMatch(/^\d+(\.\d+)?$/);
    await expect(perTitle).toHaveAttribute('data-per-title', preview.estimate.per_title_usd);
    await expect(perTitle).toContainText(dollars(preview.estimate.per_title_usd));
    // ...the projected month, or the no-history sentence (decision 451)...
    const month = panel.locator('[data-projected]');
    const monthly = preview.projected.monthly_usd;
    if (monthly === null) {
      await expect(month).toHaveAttribute('data-projected', 'no-history');
      await expect(month).toContainText('No titles have arrived yet');
    } else if (monthly === 'unknown') {
      await expect(month).toHaveAttribute('data-projected', 'unknown');
    } else {
      await expect(month).toHaveAttribute('data-projected', 'figure');
      await expect(month).toContainText(`${dollars(monthly)} a month`);
    }
    // ...and what is left of the cap.
    expect(preview.meter).toHaveProperty('remaining_usd');
    if (preview.projected.remaining_usd !== null) {
      await expect(month).toContainText(`${dollars(preview.projected.remaining_usd)} left this month`);
    } else {
      await expect(month).toContainText('No cap is set');
    }

    // The figure is on screen and nothing is stored, nor asked to be.
    expect(puts(), 'a PUT left the page before Confirm').toEqual([]);
    expect(writesBesidePreviews(requests)).toEqual([]);
    expect((await llmNow(page)).settings).toEqual(before.settings);

    // Cancel sends nothing and the stored plan is back.
    const sentBeforeCancel = requests.length;
    await panel.getByRole('button', { name: 'Cancel', exact: true }).click();
    await expect(page.getByTestId('spend-estimate')).toHaveCount(0);
    await expect(extraction.locator('[data-plan="stored"]')).toBeVisible();
    await expect(select).toHaveValue(original ?? '');
    expect((await llmNow(page)).settings).toEqual(before.settings);
    expect(requests.slice(sentBeforeCancel).filter((r) => r.method !== 'GET')).toEqual([]);

    // Confirm stores the change with the figure shown, and bills nothing.
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
  const { llm } = await openBudget(page);
  const gemini = llm.providers.find((p) => p.name === 'gemini');
  expect(gemini.model).toBe('gemini-3.7-flash');
  const basis = gemini.price_basis;
  expect(basis, 'the shipped table prices gemini-3.7-flash').not.toBe('unknown');
  expect(basis).toMatchObject({ provider: 'gemini', model: 'gemini-3.7-flash', source: 'table' });
  // Decision 343's dated price: introductory until 2027-01-01.
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

  const row = page.locator('[data-provider-row="gemini"]');
  const card = sheet(page).locator('[data-provider="gemini"]');
  await row.click();
  await expect(card.locator('[data-price-basis]')).toHaveAttribute('data-price-basis', 'table');
  for (const part of caption) await expect(card.locator('[data-price-basis]')).toContainText(part);
  await done(page);

  // And beside the estimate, where a figure is accepted.
  const extraction = page.getByTestId('llm-extraction');
  const panel = page.getByTestId('spend-estimate');
  if ((llm.settings.extraction_provider ?? null) !== 'gemini') {
    const asked = page.waitForResponse(isPreview);
    await extraction.getByLabel('Provider', { exact: true }).selectOption('gemini');
    await asked;
    await expect(panel).toHaveAttribute('data-estimate-state', 'pending');
  }
  const named = extraction.locator('[data-basis="gemini"]');
  for (const part of caption) await expect(named).toContainText(part);
  await expect(extraction).toContainText('23,500 tokens in');

  // An unpriced model is "Unknown", never a number (decision 343). Committed with Enter: a
  // dispatched `change` leaves the engine owing its own on blur, which re-proposes under Cancel.
  await row.click();
  const model = card.getByLabel('Model', { exact: true });
  const asked = page.waitForResponse(isPreview);
  await model.fill(UNPRICED);
  await model.press('Enter');
  const previewed = await asked;
  expect(previewed.request().postDataJSON().providers).toEqual({ gemini: { model: UNPRICED } });
  const preview = await previewed.json();
  expect(preview.estimate.per_title_usd).toBe('unknown');
  await expect(card.locator('[data-provider-pending]')).toBeVisible();
  await done(page);
  await expect(panel).toHaveAttribute('data-estimate-state', 'pending');
  const perTitle = panel.locator('[data-per-title]');
  await expect(perTitle).toHaveAttribute('data-per-title', 'unknown');
  await expect(perTitle).toContainText('Unknown');
  await expect(perTitle).not.toContainText('$');
  await expect(panel.locator('[data-unknown-reason]')).toContainText(UNPRICED);
  await expect(panel.locator('[data-basis]')).toHaveCount(0);
  await expect(panel.locator('[data-projected]')).not.toHaveAttribute('data-projected', 'figure');

  // Nothing was confirmed, so nothing changed.
  await panel.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByTestId('spend-estimate')).toHaveCount(0);
  await row.click();
  await expect(model).toHaveValue(gemini.model);
  await done(page);
  expect(writesBesidePreviews(requests)).toEqual([]);
  const after = await llmNow(page);
  expect(after.settings).toEqual(llm.settings);
  expect(after.providers.find((p) => p.name === 'gemini').model).toBe(gemini.model);
});

test('the film-information sources take a key and say whether they are needed', async ({ page }) => {
  const requests = recordRequests(page);
  const { sources } = await openServices(page);
  expect(sources.sources.map((s) => s.name)).toEqual(Object.keys(SOURCES));
  // Booleans and the facts a stage-2 failure needs, never a credential (decision 452).
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
    const fields = SOURCES[source.name];
    // Decision 334: only a missing TMDB key parks a title at stage 2.
    expect(source.required).toBe(source.name === 'tmdb');
    await page.locator(`[data-source-row="${source.name}"]`).click();
    const card = sheet(page).locator(`[data-source="${source.name}"]`);
    await expect(card).toHaveAttribute('data-required', String(source.required));
    await expect(card).toContainText(source.required ? 'Required' : 'Optional');
    // The stage and the reader stay one tap down, verbatim.
    await expect(card.locator('details')).toContainText('stage 2');
    if (source.used_by) await expect(card.locator('details')).toContainText(source.used_by);
    const held =
      source.name === 'trakt' ? source.has_client_id && source.has_client_secret : source.has_api_key;
    await expect(card).toHaveAttribute('data-has-key', String(Boolean(held)));
    for (const label of fields) {
      await expect(card.getByLabel(label, { exact: true })).toHaveAttribute('type', 'password');
      await expect(card.getByLabel(label, { exact: true })).toHaveValue('');
    }
    await expect(card.getByRole('button', { name: 'Test', exact: true })).toBeVisible();
    if (source.name === 'omdb') await expect(card.locator('[data-quota]')).toContainText('daily quota');

    // Typing arms Save and emptying disarms it. Nothing is saved: stage 2 would ask the real host.
    const save = card.getByRole('button', { name: 'Save', exact: true });
    await expect(save).toBeDisabled();
    const first = card.getByLabel(fields[0], { exact: true });
    await first.fill(`e2e-not-a-real-${source.name}-key`);
    await expect(save).toBeEnabled();
    await first.fill('');
    await expect(save).toBeDisabled();
    await done(page);
  }

  expect(sources.keyless).toHaveLength(KEYLESS.length);
  for (const name of KEYLESS) await expect(page.locator('[data-keyless]')).toContainText(name);
  expect(requests.filter((r) => r.method !== 'GET')).toEqual([]);
});

test('the jellyfin libraries are picked and the pick is kept', async ({ page }) => {
  const { jellyfin, libraries } = await openServices(page, { libraries: true });
  const original = [...jellyfin.library_ids];
  const card = page.getByTestId('connector-jellyfin');
  const row = card.getByRole('button', { name: /^Libraries/ });
  await row.click();
  const pick = card.locator('[data-library-pick]');
  await expect(pick).toHaveAttribute('data-library-pick', 'ready');
  expect(libraries.ok, 'the fake Jellyfin lists its libraries').toBe(true);
  const byName = Object.fromEntries(libraries.libraries.map((lib) => [lib.name, lib.id]));
  expect(Object.keys(byName)).toEqual(expect.arrayContaining(['Films', 'Shows', 'Home Videos']));
  const box = (name) => pick.getByRole('checkbox', { name, exact: true });
  for (const lib of libraries.libraries) {
    await expect(box(lib.name)).toBeChecked({ checked: original.includes(lib.id) });
  }
  const save = card.getByRole('button', { name: 'Save libraries', exact: true });
  // Sent only when it changed (decision 455).
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
    // Its own write and nothing else: no URL, no key, no mint (decisions 418, 455).
    expect(answer.request().postDataJSON()).toEqual({ library_ids: target });
    expect((await answer.json()).library_ids).toEqual(target);
    const read = await (await page.request.get('/api/admin/connectors/jellyfin')).json();
    expect(read.library_ids).toEqual(target);
    // Saved, the sheet closes and the row names the pick.
    await expect(sheet(page)).toHaveCount(0);
    const names = libraries.libraries.filter((lib) => target.includes(lib.id)).map((lib) => lib.name);
    await expect(row).toContainText(names.join(', '));

    await openServices(page, { libraries: true });
    await row.click();
    await expect(pick).toHaveAttribute('data-library-pick', 'ready');
    for (const lib of libraries.libraries) {
      await expect(box(lib.name)).toBeChecked({ checked: target.includes(lib.id) });
    }
    await expect(save).toBeDisabled();
  } finally {
    // Put back. `[]` is the whole server (decision 364).
    const res = await page.request.put('/api/admin/connectors/jellyfin', {
      data: { library_ids: original }
    });
    expect(res.ok(), 'restoring the library pick').toBeTruthy();
    expect((await res.json()).library_ids).toEqual(original);
  }
});

test('the new-title alerts say when the last ItemAdded arrived', async ({ page }) => {
  const { jellyfin: before } = await openServices(page);
  const card = page.getByTestId('connector-jellyfin');
  const alerts = card.getByRole('button', { name: /^New-title alerts/ });
  expect(before.trigger, 'the Jellyfin read carries its trigger status').toBeTruthy();
  const generate = card.getByRole('button', { name: 'Create token', exact: true });
  const state = card.locator('[data-webhook-token-state]');
  let current = before;
  await alerts.click();

  if (before.has_webhook_token === false) {
    // Decision 418: minted only on request, shown once, where it was issued, with Copy.
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
    await expect(state.getByRole('button', { name: 'Copy', exact: true })).toBeVisible();
    await expect(state).toContainText('X-Spielplan-Token');
    await expect(state).toContainText('/events/jellyfin');
    await expect(generate).toHaveCount(0);

    // One ItemAdded in the template's shape, for an item the fake does not hold.
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

    ({ jellyfin: current } = await openServices(page));
    expect(current.has_webhook_token).toBe(true);
    await expect(alerts).not.toContainText('None yet');
    await alerts.click();
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
    // The phone run: nothing can show the token again or mint another.
    await expect(generate).toHaveCount(0);
    await expect(state).toHaveAttribute('data-webhook-token-state', 'exists');
    expect(
      before.trigger.webhook.last_item_added_at,
      'a token exists that this run did not mint and no ItemAdded ever arrived with it - run the ' +
        'suite on a reset stack (e2e/run.mjs), where the desktop pass mints it and delivers one'
    ).not.toBeNull();
  }
  await done(page);

  // §6.6's "webhook status": what arrived, not a mode flag (decision 455), verbatim one tap down.
  await card.getByText('Technical details', { exact: true }).click();
  const webhook = card.locator('[data-trigger="webhook"]');
  await expect(webhook).toHaveAttribute('data-item-added', 'received');
  await expect(webhook).toContainText('last ItemAdded');
  await expect(webhook).not.toContainText('none received yet');
  await expect(card.locator('[data-trigger="delta-poll"]')).toBeVisible();
  expect(current.trigger.webhook.last_item_added_at).not.toBeNull();
});

test('every control on the admin money pages is at least 48 px on the phone', async ({
  page
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'phone',
    'the 48 px floor is a statement about a finger (section 6 preamble), measured on the phone'
  );
  await openServices(page, { libraries: true });
  // Everything drawn first, so no control is missed by absence.
  await expect(page.locator('[data-source-row]')).toHaveCount(Object.keys(SOURCES).length);
  await expect(page.getByRole('button', { name: /^Libraries/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /^People linked/ })).toContainText(/\d+ of [1-9]/);
  expect(await underTheFloor(page), 'on /admin/services').toEqual([]);

  await openBudget(page);
  await expect(page.locator('[data-provider-row]')).toHaveCount(PROVIDERS.length);
  await expect(page.getByTestId('flywheel-launch')).toBeVisible();
  expect(await underTheFloor(page), 'on /admin/budget').toEqual([]);

  const [system] = await Promise.all([
    page.waitForResponse(answers('GET', '/api/admin/system')),
    page.goto('/admin/system')
  ]);
  expect(system.ok()).toBeTruthy();
  await expect(page.getByTestId('system-logs')).toBeVisible();
  expect(await underTheFloor(page), 'on /admin/system').toEqual([]);
});

test('the old Connectors address lands on Services', async ({ page }) => {
  await page.goto('/admin/connectors');
  await expect(page).toHaveURL(/\/admin\/services$/);
  await expect(page.getByRole('heading', { name: 'Services', level: 1 })).toBeVisible();
});
