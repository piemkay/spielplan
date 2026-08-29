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
  // `exact`, because M1 put "Sign in with a passkey" on the same page (§3.2 keeps password
  // login always available alongside it) and a substring match now resolves to two buttons.
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
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

/**
 * The fake Jellyfin from `ops/compose.e2e.yml`. Two addresses for one server, because the
 * backend and the test are in different networks: the app reaches it by service name, the test
 * reaches its published port.
 */
export const JELLYFIN = {
  // as the backend container sees it
  url: process.env.FAKE_JELLYFIN_URL ?? 'http://jellyfin-fake:8096',
  // as this test process sees it
  control: process.env.FAKE_JELLYFIN_CONTROL ?? 'http://127.0.0.1:8096',
  apiKey: process.env.FAKE_JELLYFIN_API_KEY ?? 'e2e-jellyfin-key',
  password: process.env.FAKE_JELLYFIN_PASSWORD ?? 'e2e-jellyfin-password',
  // The fake's own users, fixed in ops/fake_jellyfin.py.
  user: { patrick: 'jf-user-patrick', jenny: 'jf-user-jenny' },
  item: { heat: 'jf-1', severance: 'jf-6' },
};

/** Reset the fake to a clean library: nothing played, no sessions, no tokens. */
export async function resetJellyfin(request) {
  const res = await request.post(`${JELLYFIN.control}/_test/reset`);
  expect(res.ok(), 'the fake Jellyfin must be running — see ops/compose.e2e.yml').toBeTruthy();
}

export async function jellyfinState(request) {
  const res = await request.get(`${JELLYFIN.control}/_test/state`);
  expect(res.ok()).toBeTruthy();
  return res.json();
}

/** Simulate someone marking a title watched *in Jellyfin* — the other direction. */
export async function markPlayedInJellyfin(request, itemId, played = true) {
  const res = await request.post(`${JELLYFIN.control}/_test/played`, {
    data: { user_id: JELLYFIN.user.patrick, item_id: itemId, played },
  });
  expect(res.ok()).toBeTruthy();
}

/** Put a session on the fake at a given fraction of the runtime. */
export async function playInJellyfin(request, itemId, fraction = 0.96, sessionId = 'sess-e2e') {
  const res = await request.post(`${JELLYFIN.control}/_test/session`, {
    data: {
      user_id: JELLYFIN.user.patrick,
      item_id: itemId,
      fraction,
      session_id: sessionId,
    },
  });
  expect(res.ok()).toBeTruthy();
}

/**
 * Open a title's detail panel from the catalog.
 *
 * Two details that are the difference between this working and this being a coin flip:
 *
 *  - the card is matched by NAME, not by position. The search box is debounced, so clicking
 *    `.card-wrap` first opens whatever the *unfiltered* grid happened to show — which is
 *    Paddington 2, because the catalog is ordered by year descending.
 *  - the kinds it needs go on. Home opens with Films only (owner decision 18), so a series
 *    like Severance is not in the grid at all until Series is switched on. A caller testing
 *    the partition itself passes `['Films']` to leave the default alone.
 */
export async function openTitle(page, name, { ensureKinds = ['Films', 'Series'] } = {}) {
  await page.goto('/');
  for (const kind of ensureKinds) {
    if (!(await kindIsOn(page, kind))) await kindToggle(page, kind).click();
  }
  await page.getByRole('searchbox', { name: 'Search titles' }).fill(name);
  const card = page.locator('.card-wrap', { hasText: name }).first();
  await card.click();
  const panel = page.getByRole('complementary', { name: 'Title detail' });
  await expect(panel.getByRole('heading', { name })).toBeVisible();
  return panel;
}
