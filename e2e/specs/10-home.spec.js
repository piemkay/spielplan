import { expect, test } from '@playwright/test';

import { openTitle, placeThroughRate, setUpLadder, signedIn } from '../helpers.js';

/**
 * §6.0's two Home modes: "Search or an active person-filter switches Home into the catalog grid;
 * clearing it returns the shelves." And §6.7's toggle (decision 117): a server-side deletion, so
 * gated numbers are asserted absent from the payload too, not only from the DOM. The file seeds
 * a ledger first, because a profile with no verdicts has no shelf cards (proposal 20); its first
 * test is the admin's Home before the set-up, so it seeds only after looking.
 */

/** Signed in inside its OWN context: a second tab shares the cookie jar (decision 117, per user). */
const SECOND = { name: 'e2e-home-second', password: 'e2e-home-second-pw' };

/** §6.0's shelf table, in its normative order (`home/shelves.py`'s `SHELF_IDS`). */
const SHELF_IDS = [
  'because_anchor',
  'top_of_ledger',
  'never_watched_term',
  'shared_sweet_spot',
  'school_night',
  'new_in_library',
  'worth_getting'
];

/** `home/rail.py`'s `GATED_KEYS` (decisions 117, 486). */
const GATED_KEYS = ['model', 'rail', 'suppressed', 'log', 'ledger', 'why_numbers'];

const KINDS = ['movie', 'series'];

/** §6.0's Home payload. Ask for the kinds the screen asked for: shelves partition per kind. */
async function homePayload(request, kinds = KINDS) {
  const query = kinds.map((kind) => `kind=${kind}`).join('&');
  const res = await request.get(`/api/home?${query}`);
  expect(res.ok(), '§6.0 Home must answer for a signed-in user').toBeTruthy();
  return res.json();
}

/** Every card on every shelf; only a shelf's kind-scoped sections hold items (§4.1 rule 5). */
function shelfCards(home) {
  return (home.shelves ?? [])
    .flatMap((shelf) => shelf.sections ?? [])
    .flatMap((section) => section.items ?? []);
}

/** Every decision-117 key ANYWHERE in a payload, recursively, as the server's gate deletes them. */
function gatedKeysIn(value, found = new Set()) {
  if (Array.isArray(value)) {
    for (const item of value) gatedKeysIn(item, found);
  } else if (value && typeof value === 'object') {
    for (const [key, nested] of Object.entries(value)) {
      if (GATED_KEYS.includes(key)) found.add(key);
      gatedKeysIn(nested, found);
    }
  }
  return found;
}

/**
 * Give this user enough of a ledger for §6.0's shelves to ship: the set-up's two long films, then
 * the series straight onto the ladder through §6.1's Rate. No short film: placing one would mark
 * seen the three `school_night` needs.
 */
async function seedLedger(request) {
  // New in library ships before any rating, so shelves alone do not say the ladder was seeded.
  const { done } = await (await request.get('/api/ladder/setup')).json();
  if (done && shelfCards(await homePayload(request)).length) return; // this profile already has one

  await setUpLadder(request);
  await placeThroughRate(request, 'series', [4, 0]);

  expect(
    shelfCards(await homePayload(request)).length,
    'no shelf shipped even with a ledger — every shelf-card assertion below would be vacuous'
  ).toBeGreaterThan(0);
}

/** The first shelf card whose title carries a credit; found, since that is the bundle's fact. */
async function creditedShelfCard(request, home) {
  const tried = new Set();
  for (const card of shelfCards(home)) {
    if (tried.has(card.title_id)) continue;
    if (tried.size >= 24) break; // bounded: this is a lookup, not a crawl
    tried.add(card.title_id);
    const res = await request.get(`/api/titles/${card.title_id}`, { failOnStatusCode: false });
    if (!res.ok()) continue;
    if (((await res.json()).credits ?? []).length) return card;
  }
  return null;
}

/** One shelf card's poster; a title can sit on two shelves, and either opens the same card. */
function shelfPoster(page, titleId) {
  return page
    .locator(`[data-testid="shelf-card"][data-title="${titleId}"]`)
    .first()
    .getByRole('button');
}

/** The title card, a sheet since decision 527. */
function titlePanel(page) {
  return page.getByRole('dialog', { name: 'Title detail' });
}

/** Set decision 117's switch in You, a sheet; a no-op if already in position. */
async function setShowModel(page, on) {
  await page.getByTestId('account-chip').click();
  const toggle = page.getByTestId('show-model-toggle');
  await expect(toggle).toBeVisible();
  if ((await toggle.getAttribute('aria-checked')) !== String(on)) await toggle.click();
  await expect(toggle).toHaveAttribute('aria-checked', String(on));
  await page.keyboard.press('Escape');
  await expect(toggle).toHaveCount(0);
}

/** A second household account, created the way §3.1 creates one, in its own context. */
async function secondAccount(page, browser, baseURL) {
  const created = await page.request.post('/api/admin/users', {
    data: { name: SECOND.name, role: 'member' },
    failOnStatusCode: false
  });
  expect([201, 409], 'creating the second member must either work or say it already exists')
    .toContain(created.status());

  const context = await browser.newContext({ baseURL, colorScheme: 'dark' });
  const other = await context.newPage();

  if (created.status() === 201) {
    const otp = (await created.json()).one_time_password;
    await other.request.post('/api/auth/login', { data: { name: SECOND.name, password: otp } });
    await other.request.post('/api/auth/password', {
      data: { current_password: otp, new_password: SECOND.password }
    });
  } else {
    const back = await other.request.post('/api/auth/login', {
      data: { name: SECOND.name, password: SECOND.password },
      failOnStatusCode: false
    });
    expect(
      back.ok(),
      `${SECOND.name} exists with another password — run \`node e2e/reset.mjs\` first`
    ).toBeTruthy();
  }
  return { context, other };
}

const BEFORE_SET_UP = 'Home asks for the set-up over its shelves until it is done';

test.beforeEach(async ({ page }, testInfo) => {
  await signedIn(page);
  // A persisted preference: the starting state is set, not assumed.
  await page.request.post('/api/auth/preferences', { data: { show_model: false } });
  if (testInfo.title !== BEFORE_SET_UP) await seedLedger(page.request);
  await page.goto('/');
});

test.afterEach(async ({ page }) => {
  await page.request
    .post('/api/auth/preferences', { data: { show_model: false }, failOnStatusCode: false })
    .catch(() => {});
});

// --- decision 550's notice -----------------------------------------------------------------

test(BEFORE_SET_UP, async ({ page }) => {
  const { done } = await (await page.request.get('/api/ladder/setup')).json();
  test.skip(done, 'an earlier run set the admin up; `npm --prefix e2e run fresh` starts before it');

  const notice = page.getByTestId('home-setup-notice');
  await expect(notice.getByRole('heading')).toHaveText('Set up your ladder.');
  await expect(notice).toContainText('Rating is one tap now, on six steps of your own.');
  await expect(notice.getByRole('link', { name: 'Set up my ladder' })).toHaveAttribute(
    'href',
    '/rate/setup'
  );

  await seedLedger(page.request);
  expect((await homePayload(page.request)).setup_notice).toBeNull();
  const landed = page.waitForResponse((res) => res.url().includes('/api/home?'));
  await page.reload();
  await landed;
  await expect(page.getByTestId('home-title')).toBeVisible();
  await expect(notice).toHaveCount(0);
});

// --- §6.0's grid switch -------------------------------------------------------------------

test('a shelf card opens its title as a sheet, and Back closes it on Home', async ({ page }) => {
  // Decision 527: the title card is a sheet and a history entry.
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'shelves');
  const film = shelfCards(await homePayload(page.request, ['movie']))[0];
  expect(film, 'no film on any shelf — the fixture bundle owns six').toBeTruthy();

  await shelfPoster(page, film.title_id).click();
  await expect(titlePanel(page).getByRole('heading', { name: film.name })).toBeVisible();
  await page.goBack();
  // Count, not visibility: a hidden card would come back.
  await expect(titlePanel(page)).toHaveCount(0);
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId('shelves')).toBeVisible();
});

test('search replaces the shelves with the catalog grid', async ({ page }) => {
  // The shelves first, since the grid has to REPLACE them.
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'shelves');
  await expect(page.getByTestId('shelves')).toBeVisible();
  await expect(page.getByTestId('shelf-why').first()).not.toBeEmpty();

  // Films only: what Home opens with (decision 18).
  const film = shelfCards(await homePayload(page.request, ['movie']))[0];
  expect(film, 'no film on any shelf — the fixture bundle owns six').toBeTruthy();

  await page.getByTestId('home-search').fill(film.name);

  const mode = page.getByTestId('home-mode');
  await expect(mode).toHaveAttribute('data-mode', 'grid');
  await expect(mode).toHaveAttribute('data-reason', 'search');
  await expect(page.getByTestId('shelves')).toHaveCount(0);
  await expect(page.getByTestId('shelf')).toHaveCount(0);
  await expect(page.locator('.card-wrap', { hasText: film.name }).first()).toBeVisible();
});

test('clearing the search box returns the shelves and turns Only in library back on', async ({
  page
}) => {
  // §6.0: "clearing it returns the shelves".
  const search = page.getByTestId('home-search');
  await search.fill('the');
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'grid');
  await expect(page.getByTestId('shelves')).toHaveCount(0);

  // Decision 558: a search ends on the way beyond the library, which switches it off.
  await page.getByTestId('search-beyond').click();
  await expect(page.getByTestId('owned-filter-chip')).toBeVisible();
  await expect(search).toHaveAttribute('placeholder', 'Search all films');

  await search.fill('');

  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'shelves');
  await expect(page.getByTestId('shelves')).toBeVisible();
  await expect(page.getByTestId('shelf-card').first()).toBeVisible();
  await expect(page.getByTestId('owned-filter-chip')).toHaveCount(0);
  await expect(search).toHaveAttribute('placeholder', /^Search your \d+ films?$/);
});

test('tapping a credit swaps the shelves for a filmography behind a person chip', async ({
  page
}) => {
  // Proposal 30's side effects: close the title card, clear the search box; the tap adds its
  // chip to what is set (decision 557 item 6), and the address carries it.
  const home = await homePayload(page.request, ['movie']);
  const credited = await creditedShelfCard(page.request, home);
  expect(credited, 'no shelf card carries a credit — there is no credit to tap').toBeTruthy();

  await expect(page.getByTestId('shelves')).toBeVisible();
  await shelfPoster(page, credited.title_id).click();

  const panel = titlePanel(page);
  await expect(panel).toBeVisible();
  const person = panel.locator('.person').first();
  const name = (await person.locator('.pname').textContent())?.trim();
  await person.click();

  const mode = page.getByTestId('home-mode');
  await expect(mode).toHaveAttribute('data-mode', 'grid');
  await expect(mode).toHaveAttribute('data-reason', 'person');
  await expect(page.getByTestId('shelves')).toHaveCount(0);
  await expect(panel).toHaveCount(0);
  await expect(page.getByTestId('home-search')).toHaveValue('');

  await expect(page.getByTestId('person-chip')).toHaveCount(1);
  await expect(page.getByTestId('person-chip')).toContainText(name ?? '');
  await expect(page).toHaveURL(/[?&]person=\d+(,\d+)*(&|$)/);
});

test('removing the person chip returns the shelves', async ({ page }) => {
  const home = await homePayload(page.request, ['movie']);
  const credited = await creditedShelfCard(page.request, home);
  expect(credited, 'no shelf card carries a credit — there is no credit to tap').toBeTruthy();

  await shelfPoster(page, credited.title_id).click();
  await titlePanel(page).locator('.person').first().click();

  const chip = page.getByTestId('person-chip');
  await expect(chip).toBeVisible();
  await chip.getByRole('button', { name: /^Remove / }).click();

  await expect(page.getByTestId('person-chip')).toHaveCount(0);
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'shelves');
  await expect(page.getByTestId('shelves')).toBeVisible();
  await expect(page.getByTestId('shelf-card').first()).toBeVisible();
});

// --- decision 474's switch, and what a row says about itself -------------------------------

test('Series switches to series, and Both shows the two kinds as two regions', async ({ page }) => {
  // Decision 474: Series is a switch, not an addition.
  await expect(page.getByTestId('shelves')).toBeVisible();
  await page.getByTestId('kind-series').click();
  await expect(page.getByTestId('kind-series')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('kind-movie')).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('[data-testid="shelf"][data-kind="movie"]')).toHaveCount(0);

  await page.getByTestId('kind-both').click();
  await expect(page.getByTestId('kind-both')).toHaveAttribute('aria-pressed', 'true');
  const home = await homePayload(page.request);
  const kinds = home.kinds.filter((kind) =>
    home.shelves.some((shelf) => shelf.sections.some((s) => s.kind === kind))
  );
  const regions = page.getByTestId('kind-region');
  await expect(regions).toHaveCount(kinds.length);
  for (let i = 0; i < kinds.length; i++) {
    const region = regions.nth(i);
    await expect(region).toHaveAttribute('data-kind', kinds[i]);
    const other = kinds[i] === 'movie' ? 'series' : 'movie';
    await expect(region.locator(`[data-testid="shelf"][data-kind="${other}"]`)).toHaveCount(0);
  }
});

test('Home shows no rank number and no tier letter: those live on Rank', async ({ page }) => {
  // Decision 527. Both kinds, so every shelf the fixture ships is on screen.
  await page.getByTestId('kind-both').click();
  await expect(page.getByTestId('kind-both')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('shelf-card').first()).toBeVisible();
  await expect(page.getByTestId('shelf-rank')).toHaveCount(0);
  await expect(page.getByTestId('shelf-tier')).toHaveCount(0);
  await expect(page.getByTestId('tier-legend')).toHaveCount(0);
  // Under the art, the name and "year · runtime" and nothing else.
  const meta = page.getByTestId('shelf-card').first().locator('.meta > span');
  await expect(meta).toHaveCount(2);
  await expect(meta.nth(1)).toHaveText(/^(\d{4}|—)( · .+)?$/);
});

test('the search counts the library the shelves come from, and New says its reason once', async ({
  page
}) => {
  // §6.0: the count, the search field's placeholder (decision 528), counts the household's
  // library of the shown kind, and "New in the library", whose why-line is the badge's reason,
  // does not repeat it (decision 516).
  await expect(page.getByTestId('shelves')).toBeVisible();
  await expect(page.getByTestId('home-search')).toHaveAttribute('placeholder', /^Search your \d+ films?$/);
  const fresh = page.locator('[data-testid="shelf"][data-shelf="new_in_library"]');
  for (const row of await fresh.all()) {
    await expect(row.getByTestId('shelf-cold-note')).toHaveCount(0);
  }
});

// --- §6.7 / decision 117's toggle ---------------------------------------------------------

test('with the toggle off the rail and every inline number are absent, not merely hidden', async ({
  page
}) => {
  // Decision 117: the toggle "governs the event rail **and** every inline numeric annotation".
  await expect(page.getByTestId('shelves')).toBeVisible();
  await expect(page.getByTestId('shelf-card').first()).toBeVisible();

  await expect(page.getByTestId('model-rail-open')).toHaveCount(0);
  await expect(page.getByTestId('model-rail')).toHaveCount(0);
  await expect(page.locator('[data-model-note]')).toHaveCount(0);

  // Nor is the keyboard shortcut a back door (proposal 118).
  await page.keyboard.press('m');
  await expect(page.getByTestId('model-rail')).toHaveCount(0);

  // And the server never sent them: hidden by CSS would still be on the wire.
  const payload = await homePayload(page.request, ['movie']);
  expect(shelfCards(payload).length, 'a payload with no cards proves nothing').toBeGreaterThan(0);
  expect([...gatedKeysIn(payload)]).toEqual([]);
});

test('the title card model line renders only with the toggle on', async ({ page }) => {
  // Decision 486: the toggle governs the model line too, absent from the payload as well.
  await openTitle(page, 'Heat');
  await expect(titlePanel(page)).toBeVisible();
  await expect(page.getByTestId('title-model-line')).toHaveCount(0);
  const listing = await (await page.request.get('/api/titles?kind=movie&q=Heat')).json();
  const heat = listing.items.find((t) => t.name === 'Heat');
  expect(heat, 'the film this test opens must be in the catalog').toBeTruthy();
  const off = await (await page.request.get(`/api/titles/${heat.id}`)).json();
  expect(off, 'the model line is on the wire with the switch off').not.toHaveProperty('model_line');

  await page.goto('/');
  await setShowModel(page, true);
  try {
    await openTitle(page, 'Heat');
    const line = page.getByTestId('title-model-line');
    await expect(line).toBeVisible();
    expect((await line.textContent())?.trim(), 'the line must say something').toBeTruthy();
  } finally {
    await page.goto('/');
    await setShowModel(page, false);
  }
});

test('turning the toggle on reveals the rail, the inline numbers and what did not ship', async ({
  page
}) => {
  await setShowModel(page, true);
  await page.goto('/');
  await expect(page.getByTestId('shelves')).toBeVisible();
  // Both kinds, so the payload read back below is the one this screen renders.
  await page.getByTestId('kind-both').click();
  await expect(page.getByTestId('kind-both')).toHaveAttribute('aria-pressed', 'true');

  // The model log opens from You (decision 527).
  await page.getByTestId('account-chip').click();
  await expect(page.getByTestId('model-rail-open')).toBeVisible();
  await page.keyboard.press('Escape');

  const notes = page.locator('[data-model-note]');
  expect(await notes.count(), 'no inline annotation with the toggle on').toBeGreaterThan(0);
  // §6.8: "model numbers appear in the data voice next to their name … never bare."
  await expect(notes.first()).toHaveText(/[a-zβ]\S*\s+-?\d/i);

  await page.getByTestId('account-chip').click();
  await page.getByTestId('model-rail-open').click();
  const rail = page.getByTestId('model-rail');
  await expect(rail).toBeVisible();
  await expect(rail).toContainText('last 15 events');

  const payload = await homePayload(page.request);
  expect([...gatedKeysIn(payload)].sort()).toContain('model');

  // Every (shelf, kind) either shipped or is named with its reason: an unnamed absence reads
  // as a bug.
  const shipped = new Set(
    payload.shelves.flatMap((shelf) => shelf.sections.map((s) => `${shelf.id}:${s.kind}`))
  );
  const named = new Set(payload.suppressed.map((s) => `${s.shelf}:${s.kind}`));
  for (const shelf of SHELF_IDS) {
    for (const kind of KINDS) {
      expect(
        shipped.has(`${shelf}:${kind}`) || named.has(`${shelf}:${kind}`),
        `${shelf}/${kind} neither shipped nor said why it did not`
      ).toBeTruthy();
    }
  }
  for (const entry of payload.suppressed) expect(entry.reason.trim()).not.toBe('');
  await expect(rail.getByTestId('model-rail-suppressed')).toHaveCount(payload.suppressed.length);

  await setShowModel(page, false);
  await expect(page.getByTestId('model-rail')).toHaveCount(0);
  await expect(page.getByTestId('model-rail-open')).toHaveCount(0);
  await expect(page.locator('[data-model-note]')).toHaveCount(0);
});

test('the toggle is off by default, and one user turning it on leaves the other unchanged', async ({
  page,
  browser,
  baseURL
}) => {
  // Decision 117: "one global per user … default off".
  const { context, other } = await secondAccount(page, browser, baseURL);
  try {
    await seedLedger(other.request);
    await other.goto('/');
    await expect(other.getByTestId('home-title')).toBeVisible();
    // The title renders before `/api/home` lands; `shelves` means the payload is rendered,
    // which the non-retrying `.count()` calls below need.
    await expect(other.getByTestId('shelves')).toBeVisible();
    // The avatar prints an initial; the name is in its accessible name.
    await expect(other.getByTestId('account-chip')).toHaveAccessibleName(new RegExp(SECOND.name));
    await expect(page.getByTestId('account-chip')).not.toHaveAccessibleName(new RegExp(SECOND.name));

    // "Default off", on the one account whose switch nobody has thrown.
    await other.getByTestId('account-chip').click();
    await expect(other.getByTestId('show-model-toggle')).toHaveAttribute('aria-checked', 'false');
    await other.keyboard.press('Escape');

    const shelfCardCount = await other.getByTestId('shelf-card').count();
    expect(
      shelfCardCount,
      'the second account needs shelves of its own for their bareness to mean anything'
    ).toBeGreaterThan(0);
    await expect(other.getByTestId('model-rail-open')).toHaveCount(0);
    await expect(other.locator('[data-model-note]')).toHaveCount(0);

    // The first account turns it on…
    await setShowModel(page, true);
    await page.goto('/');
    // The same gate: the rail button comes from `/api/auth/me`, the notes from `/api/home`.
    await expect(page.getByTestId('shelves')).toBeVisible();
    await page.getByTestId('account-chip').click();
    await expect(page.getByTestId('model-rail-open')).toBeVisible();
    await page.keyboard.press('Escape');
    expect(await page.locator('[data-model-note]').count()).toBeGreaterThan(0);

    // …and the second account's Home is unchanged, on the screen and in the payload.
    await other.reload();
    await expect(other.getByTestId('home-title')).toBeVisible();
    await expect(other.getByTestId('shelves')).toBeVisible();   // the title renders first
    // Not an equality: the worker keeps adding shelves on its own clock. This only says nothing
    // was lost; the three assertions below are the claim.
    expect(await other.getByTestId('shelf-card').count()).toBeGreaterThanOrEqual(shelfCardCount);
    await expect(other.getByTestId('model-rail-open')).toHaveCount(0);
    await expect(other.locator('[data-model-note]')).toHaveCount(0);
    expect([...gatedKeysIn(await homePayload(other.request))]).toEqual([]);

    // Nor the title card's model line (decision 486).
    await openTitle(other, 'Heat');
    await expect(titlePanel(other)).toBeVisible();
    await expect(other.getByTestId('title-model-line')).toHaveCount(0);
  } finally {
    await context.close();
  }
});

