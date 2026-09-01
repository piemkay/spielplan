import { expect, test } from '@playwright/test';

import { openAccountMenu, signedIn } from '../helpers.js';

/** §6 (surface names), §3.2 (the account chip), §6.7 + decision 117 (show the model). */

test.beforeEach(async ({ page }) => {
  await signedIn(page);
});

test('the nav carries the six spec surface names', async ({ page }) => {
  // §6: "Surface names (prototype, normative): Home / Rate / Tonight / Rank / Map / Taste".
  // The prototype called Map "Explore" and hid Taste in the account menu; the spec wins.
  const nav = page.getByRole('navigation', { name: 'Surfaces' });
  for (const name of ['Home', 'Rate', 'Tonight', 'Rank', 'Map', 'Taste']) {
    await expect(nav.getByRole('link', { name, exact: true })).toBeVisible();
  }
  await expect(nav.getByRole('link')).toHaveCount(6);
});

test('every nav destination resolves — no dead links', async ({ page }) => {
  const nav = page.getByRole('navigation', { name: 'Surfaces' });
  const hrefs = await nav.getByRole('link').evaluateAll((els) => els.map((e) => e.getAttribute('href')));
  for (const href of hrefs) {
    const response = await page.goto(href);
    expect(response?.status(), `${href} must not 404`).toBeLessThan(400);
    await expect(page.locator('main')).not.toBeEmpty();
  }
});

test('the account chip states the role and the auth method', async ({ page }) => {
  // §3.2: 'the chip reads "member · passkey + PIN"'.
  const menu = await openAccountMenu(page);
  await expect(menu.locator('.data').first()).toContainText(/admin · (passkey \+ PIN|PIN)/);
});

test('show the model is off by default, toggles, and persists', async ({ page }) => {
  // Decision 117: one global per-user preference, in the account dropdown, default off.
  const menu = await openAccountMenu(page);
  const toggle = menu.getByRole('switch', { name: /Show the model/ });

  await expect(toggle).toHaveAttribute('aria-checked', 'false');
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-checked', 'true');

  // It is a preference on the account, not a page-local flag: the server has it. Read with a
  // retry, as the way back down already is: `aria-checked` flips on the click and the PATCH
  // lands after it, so a bare read here is a race that fails about one run in twenty.
  await expect(async () => {
    const me = await (await page.request.get('/api/auth/me')).json();
    expect(me.show_model).toBe(true);
  }).toPass();

  // …and it survives a reload.
  await page.reload();
  const again = await openAccountMenu(page);
  await expect(again.getByRole('switch', { name: /Show the model/ })).toHaveAttribute(
    'aria-checked',
    'true'
  );

  // Put it back — default off is part of the contract.
  await again.getByRole('switch', { name: /Show the model/ }).click();
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

test('a deep link while signed out lands on sign-in, not a broken shell', async ({
  page,
  context,
}) => {
  await context.clearCookies();
  await page.goto('/rank');
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
});
