import { expect, test } from '@playwright/test';

import { openAccountMenu, signedIn } from '../helpers.js';

/** §6 (surface names), §3.2 (You), §6.7 + decision 117 (show the model, labelled Show the numbers). */

test.beforeEach(async ({ page }) => {
  await signedIn(page);
});

test('the nav carries the shipped surfaces and no unbuilt one', async ({ page }) => {
  // Decision 488: an unshipped surface (Map, Taste) is absent from navigation.
  const nav = page.getByRole('navigation', { name: 'Main' });
  for (const name of ['Home', 'Rate', 'Tonight', 'Rank']) {
    await expect(nav.getByRole('link', { name, exact: true })).toBeVisible();
  }
  await expect(nav.getByRole('link')).toHaveCount(4);
  for (const name of ['Map', 'Taste']) {
    await expect(nav.getByRole('link', { name, exact: true })).toHaveCount(0);
  }
});

test('the account menu links no unbuilt surface', async ({ page }) => {
  const menu = await openAccountMenu(page);
  await expect(menu.getByRole('link', { name: /Account/ })).toBeVisible();
  await expect(menu.locator('a[href="/taste"], a[href="/map"]')).toHaveCount(0);
  await expect(menu).not.toContainText('My Taste');
});

/**
 * What each surface puts on the screen that no other does. Not a status code: the SPA fallback
 * answers index.html with 200 for every non-API path, typos included.
 */
const MARKER = {
  '/': (page) => page.getByTestId('home-mode'),
  '/rate': (page) => page.getByTestId('rate-surface'),
  '/tonight': (page) => page.getByTestId('tonight-surface'),
  '/rank': (page) => page.getByTestId('rank-surface')
};

test('every nav destination resolves to its own surface', async ({ page }) => {
  const nav = page.getByRole('navigation', { name: 'Main' });
  const hrefs = await nav.getByRole('link').evaluateAll((els) => els.map((e) => e.getAttribute('href')));

  // A surface added or renamed in `api/auth.py`'s SURFACES fails here by name.
  expect(
    [...hrefs].sort(),
    'the nav carries an href this table does not know how to identify'
  ).toEqual(Object.keys(MARKER).sort());

  for (const href of hrefs) {
    await page.goto(href);
    expect(new URL(page.url()).pathname, `${href} did not stay on its own path`).toBe(href);
    await expect(MARKER[href](page), `${href} answered, but it is not that surface`).toBeVisible();
  }
});

test('You states the role and the auth method', async ({ page }) => {
  // §3.2: the line is an inventory of what the account holds, so it is derived from the server:
  // desktop runs before 09-passkeys and the phone after it (wording: decision 518).
  const me = await (await page.request.get('/api/auth/me')).json();
  const method = [
    me.passkeys > 0 ? 'signs in with a passkey' : 'signs in with a password',
    me.has_pin ? 'PIN set for quick switching' : null
  ]
    .filter(Boolean)
    .join(' · ');
  const role = me.role === 'admin' ? 'Admin' : 'Member';
  const menu = await openAccountMenu(page);
  await expect(menu.getByTestId('account-line')).toHaveText(`${role} · ${method}`);
});

test('the account page speaks plainly and folds what a member need not act on', async ({ page }) => {
  // Decision 518: the same controls and facts, in a member's words.
  await page.goto('/account');
  await expect(page.getByRole('heading', { name: 'Account' })).toBeVisible();
  const body = (await page.locator('main').textContent()) ?? '';
  for (const word of ['WebAuthn', 'Switch PIN', 'switch PIN', 'Tier set']) {
    expect(body, `the account page says "${word}"`).not.toContain(word);
  }
  await expect(page.getByRole('heading', { name: 'PIN for switching profiles' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Rank letters' })).toBeVisible();
  await expect(page.getByTestId('tier-set-current')).toBeVisible();
  await expect(page.getByTestId('tier-set-input')).toBeHidden();
  await expect(page.getByTestId('data-sources')).toBeHidden();
  await page.getByTestId('account-technical').locator('summary').click();
  await expect(page.getByTestId('data-sources')).toBeVisible();
});

test('show the model is off by default, toggles, and persists', async ({ page }) => {
  // Decision 117: one global per-user preference, in the account dropdown, default off.
  const menu = await openAccountMenu(page);
  const toggle = menu.getByRole('switch', { name: /Show the numbers/ });

  await expect(toggle).toHaveAttribute('aria-checked', 'false');
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-checked', 'true');

  // The server has it. Retried: `aria-checked` flips on the click and the PATCH lands after it.
  await expect(async () => {
    const me = await (await page.request.get('/api/auth/me')).json();
    expect(me.show_model).toBe(true);
  }).toPass();

  await page.reload();
  const again = await openAccountMenu(page);
  await expect(again.getByRole('switch', { name: /Show the numbers/ })).toHaveAttribute(
    'aria-checked',
    'true'
  );

  // Put it back — default off is part of the contract.
  await again.getByRole('switch', { name: /Show the numbers/ }).click();
  await expect(async () => {
    const after = await (await page.request.get('/api/auth/me')).json();
    expect(after.show_model).toBe(false);
  }).toPass();
});

test('logging out clears the session and returns to the sign-in page', async ({ page }) => {
  // §3.2: "Logout clears the session cookie only — passkeys remain registered."
  const menu = await openAccountMenu(page);
  await menu.getByRole('button', { name: 'Log out' }).click();
  await expect(page).toHaveURL(/\/login$/);

  const me = await page.request.get('/api/auth/me');
  expect(me.status()).toBe(401);
});

/**
 * Service workers blocked: `page.route` does not see a request a service worker mediates, so on
 * WebKit the abort below would never happen (decision 284). Scoped to this test only.
 */
test.describe(() => {
  test.use({ serviceWorkers: 'block' });

  test('a logout the server never answers still ends it on this device', async ({ page }) => {
    // On a LAN or Tailscale origin a request that never lands is ordinary; the local half of
    // logout must happen either way.
    await page.route('**/api/auth/logout', (route) => route.abort());
    const menu = await openAccountMenu(page);
    await menu.getByRole('button', { name: 'Log out' }).click();
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    // The server session is still alive; this device only forgets the person (decision 272).
    expect((await page.request.get('/api/auth/me')).status()).toBe(200);
    await page.unroute('**/api/auth/logout');
  });
});

test("an unknown address renders the app's own error card, not the framework's page", async ({
  page
}) => {
  // The only test that renders `+error.svelte`. An installed standalone view has no address bar,
  // so the error page must offer a way back into the shell.
  await page.goto('/not-a-surface');
  const card = page.getByTestId('app-error');
  await expect(card).toBeVisible();
  await expect(card, 'the card does not say what happened').toContainText('ERROR 404');
  await expect(
    card.getByRole('link', { name: 'Home' }),
    'the error page offers no way back into the shell'
  ).toBeVisible();
  await expect(card.getByRole('button', { name: 'Reload' })).toBeVisible();
});

test('a deep link while signed out lands on sign-in, not a broken shell', async ({
  page,
  context,
}) => {
  await context.clearCookies();
  await page.goto('/rank');
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
});

// --- §6.7's drawer, now that it is shell chrome ----------------------------------------------

/** Decision 117's switch, set through the API: these tests are about the drawer it gates. */
async function showModel(page, on) {
  const res = await page.request.post('/api/auth/preferences', { data: { show_model: on } });
  expect(res.ok(), `setting show_model=${on}: ${res.status()}`).toBeTruthy();
}

test('the model rail opens from every surface, not only Home', async ({ page }) => {
  // Counts, not visibility: a second mount left behind would render every line twice.
  await showModel(page, true);
  try {
    for (const surface of ['/', '/rate', '/rank', '/tonight']) {
      await page.goto(surface);
      // The trigger is absent on `/login` too, for an unrelated reason.
      await expect(page, `${surface} bounced to sign-in`).not.toHaveURL(/\/login$/);

      const menu = await openAccountMenu(page);
      const trigger = menu.getByTestId('model-rail-open');
      await expect(trigger, `no rail control on ${surface}`).toHaveCount(1);
      await trigger.click();

      const rail = page.getByTestId('model-rail');
      await expect(rail, `${surface} mounted the drawer more than once`).toHaveCount(1);
      await expect(rail).toBeVisible();
      await expect(rail).toContainText('last 15 events');

      await page.getByTestId('model-rail-close').click();
      await expect(rail, `${surface} could not close the drawer it opened`).toHaveCount(0);
    }
  } finally {
    await showModel(page, false);
  }
});

test('reopening the rail refills it from the live log, once', async ({ page }) => {
  // The reopened drawer reads the same log back, once. That it drops its payload on close is
  // asserted in `ModelRail.svelte.test.js`, which can hold the reply open.
  await showModel(page, true);
  try {
    await page.goto('/');
    const rail = page.getByTestId('model-rail');
    const body = rail.getByTestId('model-rail-event').or(rail.getByTestId('model-rail-empty'));

    await (await openAccountMenu(page)).getByTestId('model-rail-open').click();
    await expect(rail).toBeVisible();
    await expect(body.first(), 'the first open never finished reading').toBeVisible();
    const read = await body.allTextContents();

    await page.getByTestId('model-rail-close').click();
    await expect(rail).toHaveCount(0);

    await (await openAccountMenu(page)).getByTestId('model-rail-open').click();
    await expect(rail).toBeVisible();
    await expect(body.first(), 'the reopened drawer never finished reading').toBeVisible();
    expect(await body.allTextContents(), 'the reopened drawer is not the log again').toEqual(read);
    await expect(rail).toContainText('last 15 events');
  } finally {
    await showModel(page, false);
  }
});
