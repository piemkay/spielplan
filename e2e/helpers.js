import { expect } from '@playwright/test';

/** Credentials the suite creates and reuses. Never a real account. */
export const ADMIN = { name: 'e2e-admin', password: 'e2e-first-boot-pw' };

/**
 * Read the app's own view of where it is in the first-boot sequence (§3.1).
 *
 * Every helper takes `page.request`, not the bare `request` fixture: the fixture is a separate
 * API context with no cookies, so it answers 401 for anything authenticated. `page.request`
 * shares the browser context, which is what the app actually sees.
 */
export async function setupState(request) {
  const res = await request.get('/api/setup/state');
  expect(res.ok(), 'the app must answer /api/setup/state before anything else').toBeTruthy();
  return res.json();
}

export async function health(request) {
  const res = await request.get('/api/health');
  expect(res.ok()).toBeTruthy();
  return res.json();
}

/** Create the admin through the wizard UI, as a first-booting operator would. */
export async function createAdminThroughWizard(page, admin = ADMIN) {
  await page.goto('/setup');
  await expect(page.getByRole('heading', { name: 'Create the admin account' })).toBeVisible();
  await page.getByLabel('NAME').or(page.locator('input[type=text]').first()).fill(admin.name);
  await page.locator('input[type=password]').fill(admin.password);
  await page.getByRole('button', { name: 'Create admin' }).click();
  // The wizard signs the new admin in and lands on Home (§3.1).
  await expect(page.getByRole('heading', { name: /Good (morning|afternoon|evening)/ })).toBeVisible();
}

export async function login(page, admin = ADMIN) {
  await page.goto('/login');
  await page.locator('input[type=text]').first().fill(admin.name);
  await page.locator('input[type=password]').fill(admin.password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: /Good (morning|afternoon|evening)/ })).toBeVisible();
}

/** Ensure we are signed in, creating the admin on a first-boot app. */
export async function signedIn(page, admin = ADMIN) {
  const state = await setupState(page.request);
  if (!state.has_admin) {
    await createAdminThroughWizard(page, admin);
  } else {
    await login(page, admin);
  }
}

/** Run the §10 swap sequence through the Data tab: validate, then import. */
export async function importBundle(page) {
  await page.goto('/admin/data');
  await expect(page.getByRole('heading', { name: 'Artifact bundle' })).toBeVisible();

  await page.getByRole('button', { name: 'Validate bundle' }).click();
  await expect(page.locator('.verdict')).toHaveText('valid');

  await page.getByRole('button', { name: 'Import and activate' }).click();
  // The report re-renders with the load-stage notes once the import lands.
  await expect(page.locator('.finding', { hasText: 'artifacts staged to' })).toBeVisible();
}

/** The account dropdown, opened. */
export async function openAccountMenu(page) {
  await page.locator('.chip').click();
  await expect(page.locator('.menu')).toBeVisible();
  return page.locator('.menu');
}

/**
 * The catalog's kind toggles (owner decision 18): two independent toggles, either or both,
 * never neither.
 */
export function kindToggle(page, label) {
  return page.getByRole('group', { name: 'Kind' }).getByRole('button', { name: label });
}

export async function kindIsOn(page, label) {
  return (await kindToggle(page, label).getAttribute('aria-pressed')) === 'true';
}
