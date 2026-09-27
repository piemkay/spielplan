import { expect, test } from '@playwright/test';

import { createMember, login, openAccountMenu, signInAsMember, signedIn } from '../helpers.js';

/**
 * The shell on the phone (§6 preamble, §6.8, §3.1, §3.2): the 16 px and 48 px rules, dismissal,
 * the 401 seam, request deadlines, the offline card, and a clean hand-over on a shared device.
 *
 * The filename must end in `shell.spec.js`: that is how the phone project selects it (decision
 * 267). Playwright runs the whole desktop pass first, so this file's desktop run is followed by
 * 20-admin-data and 21-connectors, then the phone runs of 02-shell, 03-library, 06-responsive,
 * 13-rank and 14-tonight before this file, and 20-admin-data and 21-connectors after it. The
 * durable footprint is one reused member, `shell-switch`; a test that writes more must be argued
 * safe for those specs.
 *
 * Not measurable here: the status-bar inset in standalone mode, Safari's toolbar, and focus zoom
 * on a real device are owed as manual device checks in docs/TESTING.md (decision 281).
 */

// No service worker except in the shell-cache block: `page.route` does not see requests a
// service worker mediates (microsoft/playwright#1090), so on WebKit a route would never fire.
test.use({ serviceWorkers: 'block' });

test.beforeEach(async ({ page }) => {
  await signedIn(page);
});

/** Decision 117's switch, through the API. Read at boot, so the caller reloads after it. */
async function showModel(page, on) {
  const res = await page.request.post('/api/auth/preferences', { data: { show_model: on } });
  expect(res.ok(), `setting show_model=${on}: ${res.status()}`).toBeTruthy();
}

/** Tonight, at the door: a seated device is restored into its room, so step `Back` out first. */
async function atTonightDoor(page) {
  await page.goto('/tonight');
  await expect(page.getByTestId('tonight-surface')).toBeVisible();
  await expect(page.getByTestId('tonight-booting')).toHaveCount(0, { timeout: 20_000 });
  const back = page.getByTestId('tonight-back');
  if (await back.count()) await back.click();
  await expect(page.getByTestId('tonight-controls')).toBeVisible();
}

const FINGERS = 'a finger is what this measures, and the desktop project has none';

// ---------------------------------------------------------------------------------------------
// 1. The 16 px rule
// ---------------------------------------------------------------------------------------------

// Input types with no caret, which iOS Safari never zooms for.
const NO_CARET = [
  'checkbox',
  'radio',
  'range',
  'file',
  'submit',
  'button',
  'reset',
  'image',
  'hidden',
  'color'
];

/** Every text-taking control computing under 16 px, described well enough to find in a stylesheet. */
async function controlsThatWouldZoom(page) {
  return page.evaluate((skip) => {
    const describe = (el) => {
      const type = (el.getAttribute('type') || '').toLowerCase();
      const id = el.dataset.testid ? `[data-testid=${el.dataset.testid}]` : '';
      const label =
        el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || '';
      const tag = el.tagName.toLowerCase() + (type ? `[type=${type}]` : '');
      return `${tag}${id}${label ? ` (${label})` : ''}`;
    };
    const out = [];
    for (const el of document.querySelectorAll('input, select, textarea')) {
      const type = (el.getAttribute('type') || '').toLowerCase();
      if (el.tagName === 'INPUT' && skip.includes(type)) continue;
      const rect = el.getBoundingClientRect();
      if (!rect.width && !rect.height) continue;
      const size = parseFloat(getComputedStyle(el).fontSize);
      if (!(size >= 16)) out.push(`${describe(el)} computes ${size}px`);
    }
    return out;
  }, NO_CARET);
}

test('no form control on the member path zooms on focus', async ({ page, context, isMobile }) => {
  test.skip(!isMobile, FINGERS);

  // iOS Safari zooms on focus below 16 px and does not zoom back. The COMPUTED size, because
  // whether the global rule wins is cascade arithmetic. /login only renders signed out.
  await context.clearCookies();
  await page.goto('/login');
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  expect(await controlsThatWouldZoom(page), 'on /login, before anyone is signed in').toEqual([]);

  await login(page);
  await expect(page.getByTestId('home-greeting')).toBeVisible();
  expect(await controlsThatWouldZoom(page), 'on / (Home)').toEqual([]);

  await page.goto('/account');
  await expect(page.getByRole('heading', { name: 'Account', exact: true })).toBeVisible();
  expect(await controlsThatWouldZoom(page), 'on /account').toEqual([]);

  await page.goto('/rank');
  await expect(page.getByTestId('rank-surface')).toBeVisible();
  expect(await controlsThatWouldZoom(page), 'on /rank').toEqual([]);

  await atTonightDoor(page);
  expect(await controlsThatWouldZoom(page), 'on /tonight').toEqual([]);
});

// ---------------------------------------------------------------------------------------------
// 2. The 48 px rule, on the controls no sweep had reached
// ---------------------------------------------------------------------------------------------

/**
 * Wait until no ancestor is animating. `boundingBox()` maps through transforms in single
 * precision, so mid-`fadeIn` a 48 px box can measure 47.99999; at rest it is exact.
 */
async function hasStoppedMoving(locator) {
  await locator.evaluate(async (el) => {
    const moving = [];
    for (let node = el; node; node = node.parentElement) moving.push(...node.getAnimations());
    await Promise.all(moving.map((a) => a.finished.catch(() => {})));
  });
}

/** Both dimensions: `design.css`'s coarse block raises `min-height` and never `min-width`. */
async function meetsTheTouchFloor(locator, what) {
  await hasStoppedMoving(locator);
  const box = await locator.boundingBox();
  expect(box, `${what} is not on screen`).not.toBeNull();
  expect(
    box.height,
    `${what} is ${box.height}px tall, under --touch (48px)`
  ).toBeGreaterThanOrEqual(48);
  expect(
    box.width,
    `${what} is ${box.width}px wide, under --touch (48px)`
  ).toBeGreaterThanOrEqual(48);
}

test("the account menu's entries and the overlay exits meet the touch floor", async ({
  page,
  isMobile
}) => {
  test.skip(!isMobile, FINGERS);

  // The controls `06-responsive`'s sweep cannot reach: the menu entries and the overlay exits.
  const menu = await openAccountMenu(page);
  const entries = menu.locator('[data-nav]');
  expect(await entries.count(), 'the account menu carries no entries at all').toBeGreaterThan(0);
  for (const entry of await entries.all()) {
    await meetsTheTouchFloor(entry, `the account menu's "${(await entry.textContent())?.trim()}"`);
  }
  await page.keyboard.press('Escape');

  await page.goto('/');
  await page.locator('.card-wrap').first().click();
  const panel = page.getByLabel('Title detail');
  await expect(panel).toBeVisible();
  await meetsTheTouchFloor(panel.locator('.close'), "the title panel's close button");
  await page.keyboard.press('Escape');

  await showModel(page, true);
  try {
    await page.goto('/');
    await (await openAccountMenu(page)).getByTestId('model-rail-open').click();
    await expect(page.getByTestId('model-rail')).toBeVisible();
    await meetsTheTouchFloor(page.getByTestId('model-rail-close'), "the model rail's close button");
    // The kind filters render only with more than one kind.
    const all = page.getByTestId('model-rail-filter-all');
    if (await all.isVisible()) {
      await meetsTheTouchFloor(all, "the rail's \"all\" filter");
      for (const chip of await page.getByTestId('model-rail-filter').all()) {
        await meetsTheTouchFloor(chip, `the rail's "${(await chip.textContent())?.trim()}" filter`);
      }
    }
  } finally {
    await showModel(page, false);
  }

  // The one named exemption: the wizard's step indicator is a 3 px hairline (decision 280).
  await page.goto('/setup');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await expect(page.getByTestId('setup-step').first()).toBeVisible();
  const wizard = page.locator('button:not([data-testid="setup-step"])');
  expect(await wizard.count(), 'the wizard shows no controls to measure').toBeGreaterThan(0);
  for (const control of await wizard.all()) {
    await meetsTheTouchFloor(control, `the wizard's "${(await control.textContent())?.trim()}"`);
  }
});

// ---------------------------------------------------------------------------------------------
// 3. Dismissal
// ---------------------------------------------------------------------------------------------

test('every menu and overlay dismisses by outside tap and by Escape', async ({ page }) => {
  // Proposal 131: "Every popover, menu and sheet dismisses on outside click and on Escape".
  // The top-left corner is outside every sheet and drawer: the scrim or the page lies there.
  const outside = { click: () => page.mouse.click(4, 4) };
  const you = page.getByRole('dialog', { name: 'You' });

  // --- You
  await openAccountMenu(page);
  await outside.click();
  await expect(you, 'You has no outside-tap dismissal').toHaveCount(0);

  await openAccountMenu(page);
  await page.keyboard.press('Escape');
  await expect(you, 'You does not close on Escape').toHaveCount(0);

  // A sheet is a history entry (decision 527): Back closes it and stays on the page.
  await openAccountMenu(page);
  await page.goBack();
  await expect(you, 'Back does not close You').toHaveCount(0);
  await expect(page).not.toHaveURL(/\/login$/);

  // --- the title detail panel
  await page.goto('/');
  await page.locator('.card-wrap').first().click();
  const panel = page.getByLabel('Title detail');
  await expect(panel).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(panel, 'the title panel does not close on Escape').toHaveCount(0);

  await page.locator('.card-wrap').first().click();
  await expect(panel).toBeVisible();
  await outside.click();
  await expect(panel, 'the title panel has no outside-tap dismissal').toHaveCount(0);

  // --- the model rail
  await showModel(page, true);
  try {
    await page.goto('/');
    const rail = page.getByTestId('model-rail');

    await (await openAccountMenu(page)).getByTestId('model-rail-open').click();
    await expect(rail).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(rail, 'the model rail does not close on Escape').toHaveCount(0);

    await (await openAccountMenu(page)).getByTestId('model-rail-open').click();
    await expect(rail).toBeVisible();
    await outside.click();
    await expect(rail, 'the model rail has no outside-tap dismissal').toHaveCount(0);
  } finally {
    await showModel(page, false);
  }

  // --- and the menu's own links, which navigate inside the same document.
  const again = await openAccountMenu(page);
  await again.locator('[data-nav="account"]').click();
  await expect(page).toHaveURL(/\/account$/);
  await expect(you, 'You rode a client-side navigation onto the next surface').toHaveCount(0);
});

// ---------------------------------------------------------------------------------------------
// 4-6. What the wire says, and what the household is told
// ---------------------------------------------------------------------------------------------

test('a 401 returns the member to the sign-in page', async ({ page, context }) => {
  // §3.2: the session is a server fact. A nav TAP, not a `goto`: the shell reads `/auth/me` only
  // at boot, so only the surface's own 401 can know the session ended.
  await page.goto('/');
  await expect(page.getByTestId('home-greeting')).toBeVisible();
  // Home's posters are session-gated and arrive after the greeting; cut the cookie after them.
  await page.waitForLoadState('networkidle');

  const unauthorized = [];
  const watch = (res) => {
    if (res.status() === 401) unauthorized.push(new URL(res.url()).pathname);
  };
  page.on('response', watch);

  await context.clearCookies();
  await page
    .getByRole('navigation', { name: 'Main' })
    .getByRole('link', { name: 'Rank', exact: true })
    .click();

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  page.off('response', watch);

  // Every 401 belongs to the surface tapped: Rank reads `/api/rank` and `/api/facets`.
  const rankReads = (path) => path.startsWith('/api/rank') || path === '/api/facets';
  expect(
    unauthorized.filter((path) => !rankReads(path)),
    'the member met a 401 on a surface they never asked for'
  ).toEqual([]);

  // No backend sentence on the way out (§6.8).
  await expect(page.locator('[role=alert]')).toHaveCount(0);
  await expect(page.locator('.err')).toHaveCount(0);
  await expect(page.getByTestId('account-chip')).toHaveCount(0);

  // --- and the two 401s that are NOT a lost session.
  await login(page);

  // A route that VERIFIES a credential: a wrong password costs a sentence, not the session.
  // Routed rather than real, so no lockout attempt is spent (§3.2).
  await page.route('**/api/auth/pin', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'wrong current password' })
    })
  );
  try {
    await page.goto('/account');
    const card = page.getByTestId('pin-card');
    await expect(card).toBeVisible();
    await card.locator('input[autocomplete="current-password"]').fill('not-the-password');
    await card.locator('input[inputmode="numeric"]').fill('1234');
    await card.getByRole('button', { name: 'Save PIN' }).click();

    await expect(page.locator('.err')).toHaveText('wrong current password');
    expect(new URL(page.url()).pathname, 'a mistyped PIN password signed the admin out').toBe(
      '/account'
    );
    await expect(page.getByTestId('account-chip')).toBeVisible();
  } finally {
    await page.unroute('**/api/auth/pin');
  }

  // And §3.2's 24-hour admin re-prompt: a 401 with a header meaning "prove it again".
  await page.route('**/api/admin/users', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      headers: { 'x-spielplan-reauth': 'admin' },
      body: JSON.stringify({ detail: 'admin re-prompt' })
    })
  );
  try {
    await page.goto('/admin/people');
    await expect(page.getByTestId('admin-reauth')).toBeVisible();
    expect(new URL(page.url()).pathname, 'the re-prompt threw away a live admin session').toBe(
      '/admin/people'
    );
  } finally {
    await page.unroute('**/api/admin/users');
  }
});

// `api.js`'s deadline sentence. The em dash as an escape keeps console output ASCII.
const NO_ANSWER = 'the network did not answer \u2014 try again';

test('a request that never answers ends with a sentence, not a dead surface', async ({ page }) => {
  // `fetch` has no timeout of its own; `api.js` gives most routes 10 s (decision 269).
  const OPEN_ROOM = /\/api\/tonight\/sessions$/;
  let held = null;
  await page.route(OPEN_ROOM, (route) => {
    held = route;
  });
  try {
    await atTonightDoor(page);
    await page.getByTestId('tonight-open').click();

    // Longer than the 10 s deadline being measured.
    await expect(
      page.getByTestId('tonight-error'),
      'the request never ended in anything a person could read'
    ).toHaveText(NO_ANSWER, { timeout: 25_000 });

    // And the controls are back, to try again.
    await expect(page.getByTestId('tonight-controls')).toBeVisible();
    await expect(page.getByTestId('tonight-open')).toBeEnabled();
  } finally {
    await page.unroute(OPEN_ROOM);
    await held?.abort().catch(() => {});
  }
});

test('a refused field says which field and why', async ({ page }) => {
  // A pydantic 422 `detail` is a LIST of `{type, loc, msg}`. The guests stepper cannot send a
  // `null`, so the request is rewritten on its way out and the real server refuses it.
  const OPEN_ROOM = /\/api\/tonight\/sessions$/;
  await page.route(OPEN_ROOM, (route) =>
    route.continue({ postData: JSON.stringify({ ...route.request().postDataJSON(), guests: null }) })
  );
  try {
    await atTonightDoor(page);
    await page.getByTestId('tonight-open').click();

    const error = page.getByTestId('tonight-error');
    await expect(error).toBeVisible();
    // The field, then pydantic's sentence, which is left unpinned.
    await expect(error).toHaveText(/^guests: .+/);
    await expect(error).not.toHaveText('Unprocessable Entity');
  } finally {
    await page.unroute(OPEN_ROOM);
  }
});

// ---------------------------------------------------------------------------------------------
// 7-8. The two states a shared device gets into
// ---------------------------------------------------------------------------------------------

test.describe('the shell cache', () => {
  // Decision 284's exception: an offline boot IS the cache serving the document.
  test.use({ serviceWorkers: 'allow' });

  test('an offline boot keeps the session and says the appliance is unreachable', async ({
    page,
    context,
    browserName
  }) => {
    // Playwright's WebKit refuses any navigation while offline, so this is a device check there.
    test.skip(
      browserName === 'webkit',
      'Playwright/WebKit cannot load a document while offline; owed as a device check'
    );
    // §3.1: "an explicit state instead of erroring". A failed read is not a sign-out.
    await page.goto('/');
    await expect(page.getByTestId('home-greeting')).toBeVisible();

    // The worker must CONTROL the page before going offline, or the reload is a browser error.
    const controlled = await page.evaluate(async () => {
      if (!('serviceWorker' in navigator)) return false;
      const timeout = new Promise((resolve) => setTimeout(() => resolve(false), 20000));
      const ready = navigator.serviceWorker.ready.then(() => {
        if (navigator.serviceWorker.controller) return true;
        return new Promise((resolve) => {
          navigator.serviceWorker.addEventListener('controllerchange', () => resolve(true), {
            once: true
          });
        });
      });
      return Promise.race([ready, timeout]);
    });
    expect(
      controlled,
      'the shell cache never took control of the page, so there is no offline boot to test'
    ).toBeTruthy();

    await context.setOffline(true);
    try {
      await page.reload();

      const card = page.getByTestId('appliance-unreachable');
      await expect(card).toBeVisible();
      await expect(card).toContainText('Spielplan is not answering');
      expect(new URL(page.url()).pathname, 'an unreachable appliance is not a sign-out').toBe('/');
      await expect(page.getByRole('heading', { name: 'Sign in' })).toHaveCount(0);

      const cookies = await context.cookies();
      expect(
        cookies.some((c) => c.name === 'spielplan_session'),
        'the shell threw away the session over a read that never arrived'
      ).toBeTruthy();

      // Decision 271: a shell that read nothing claims nothing.
      await expect(page.getByText('no bundle imported')).toHaveCount(0);
    } finally {
      await context.setOffline(false);
    }

    // Back without a reload, which a standalone web view has no gesture for: the shell's own
    // retry (decision 283), since Chromium's emulation dispatches no `online` event.
    await expect(
      page.getByTestId('home-greeting'),
      'the shell stayed on the unreachable card after the network came back'
    ).toBeVisible({ timeout: 30_000 });
  });
});

test("the next person to sign in sees none of the previous one's surfaces", async ({ page }) => {
  // Surface stores are module-level singletons that outlive a client-side navigation, so the
  // previous member's state must not reach the next one on a shared device.

  const member = await createMember(page, 'shell-switch', { reuse: true });
  await signInAsMember(page, member);
  // Back to the admin through the form.
  await page.request.post('/api/auth/logout');
  await login(page);

  // Traces on two surfaces, in ONE document: Rate by a nav tap, since a `goto` resets the stores.
  const nav = page.getByRole('navigation', { name: 'Main' });
  await atTonightDoor(page);
  // The controls live in a sheet behind the door's summary row (decision 527).
  await page.getByTestId('tonight-settings').click();
  await page.getByTestId('tonight-kind-series').click();
  for (let i = 0; i < 3; i++) await page.getByTestId('tonight-guests-more').click();
  await expect(page.getByTestId('tonight-kind-series')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('tonight-guests')).toHaveText('3');
  await page.getByTestId('tonight-settings-done').click();
  await expect(page.getByRole('dialog', { name: "Tonight's settings" })).toHaveCount(0);

  await nav.getByRole('link', { name: 'Rate', exact: true }).click();
  await expect(page.getByTestId('rate-surface')).toBeVisible();
  const theirCard = page.getByTestId('rate-card-title');
  await expect(
    theirCard,
    'the admin was served no card, so this test would prove nothing about clearing one'
  ).toBeVisible({ timeout: 20_000 });
  const theirTitle = (await theirCard.textContent())?.trim();
  expect(theirTitle, 'the card on screen carries no title to recognise it by').toBeTruthy();

  // Logout leaves the document (`location.assign('/login')`, decisions 272 and 285), which
  // clears every store at once.
  const reloaded = page.waitForEvent('load', { timeout: 20_000 }).then(
    () => true,
    () => false
  );
  const menu = await openAccountMenu(page);
  await menu.getByRole('button', { name: 'Log out' }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect(
    await reloaded,
    'logging out never left the document (decisions 272, 285), so the next person would sign in ' +
      "inside the previous person's page"
  ).toBeTruthy();

  // On the form already there, not `loginAsMember`, whose `goto` would clear the stores itself.
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  await page.locator('input[type=text]').first().fill(member.name);
  await page.locator('input[type=password]').fill(member.password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByTestId('home-greeting')).toBeVisible();

  const RATE = /\/api\/rate(\?|$)/;
  const RANK = /\/api\/rank(\?|$)/;
  let heldRate = null;
  let heldRank = null;
  await page.route(RATE, (route) => {
    heldRate = route;
  });
  await page.route(RANK, (route) => {
    heldRank = route;
  });

  try {
    // With the new person's first read held open, anything on screen belongs to somebody else.
    await nav.getByRole('link', { name: 'Rate', exact: true }).click();
    await expect(page.getByTestId('rate-surface')).toBeVisible();
    await expect(page.getByTestId('rate-loading')).toBeVisible();
    await expect(page.getByTestId('rate-sweep-card')).toHaveCount(0);
    await expect(page.getByTestId('rate-battle-card')).toHaveCount(0);
    await expect(
      page.getByText(theirTitle, { exact: true }),
      "the previous person's card is on the new person's screen"
    ).toHaveCount(0);

    // The board element is always drawn (proposal 82), so count its rows.
    await nav.getByRole('link', { name: 'Rank', exact: true }).click();
    await expect(page.getByTestId('rank-surface')).toBeVisible();
    await expect(
      page.getByTestId('rank-board').locator('[data-tier]'),
      "the previous person's tiers are on the new person's board"
    ).toHaveCount(0);
  } finally {
    await page.unroute(RATE);
    await page.unroute(RANK);
    await heldRate?.abort().catch(() => {});
    await heldRank?.abort().catch(() => {});
  }

  // Tonight's controls render from module state before any read lands.
  await nav.getByRole('link', { name: 'Tonight', exact: true }).click();
  await expect(page.getByTestId('tonight-surface')).toBeVisible();
  await expect(page.getByTestId('tonight-booting')).toHaveCount(0, { timeout: 20_000 });
  await expect(
    page.getByTestId('tonight-controls'),
    "the new person landed inside the previous one's evening"
  ).toBeVisible();
  await page.getByTestId('tonight-settings').click();
  await expect(
    page.getByTestId('tonight-kind-movie'),
    "the previous person's series night carried over"
  ).toHaveAttribute('aria-pressed', 'true');
  await expect(
    page.getByTestId('tonight-guests'),
    "the previous person's guests carried over"
  ).toHaveText('0');
  await page.getByTestId('tonight-settings-done').click();
  // `Back` renders only past the door.
  await expect(page.getByTestId('tonight-back')).toHaveCount(0);
});

// ---------------------------------------------------------------------------------------------
// The notices the displayed data's own terms require (M4.16, decisions 293 and 298)
// ---------------------------------------------------------------------------------------------

// Used only as an ABSENCE: the licences want a notice in the product, not a credit per tile
// (decision 293).
const SOURCE_NAMES = /IMDb|TMDB|TVmaze|Wikipedia|OMDb/i;

test('the data sources are attributed once, on /account, and on no card', async ({ page }) => {
  // Here, on the phone project: decision 293's notice is reachable "by every signed-in member on
  // a phone". It writes nothing.
  await page.goto('/account');
  // One tap away under "Technical details" (decision 518).
  const technical = page.getByTestId('account-technical');
  await expect(technical).toBeVisible();
  await technical.locator('summary').click();
  const block = page.getByTestId('data-sources');
  await expect(block.getByRole('heading', { name: 'Data sources' })).toBeVisible();

  // Verbatim: the words ARE the permission (decision 298).
  for (const notice of [
    'Information courtesy of IMDb (https://www.imdb.com). Used with permission.',
    'This product uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise ' +
      'approved by TMDB.',
    'Plot summaries and overviews from Wikipedia, by its contributors, under CC BY-SA 4.0.',
    'Series data from TVmaze, under CC BY-SA 4.0.',
    'Ratings and plot text from OMDb, under CC BY-NC 4.0.'
  ]) {
    await expect(block.getByText(notice, { exact: true }), notice).toBeVisible();
  }

  // No logo is asserted either way: the TMDB logo is an owed asset (decision 298).

  // And no source names on the tiles. Cards always render; shelves only in shelf mode.
  await page.goto('/');
  const cards = page.locator('.card-wrap');
  // Web-first: a count snapshot right after `goto` can precede `/api/home` entirely.
  await expect(
    cards.first(),
    'Home drew no poster card, so nothing was checked for a source name'
  ).toBeVisible();
  await expect(
    cards.filter({ hasText: SOURCE_NAMES }),
    'a poster card names the source its data came from'
  ).toHaveCount(0);
  await expect(
    page.locator('[data-testid="shelf"]').filter({ hasText: SOURCE_NAMES }),
    'a shelf names the source its data came from'
  ).toHaveCount(0);
});
