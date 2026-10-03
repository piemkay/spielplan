import { expect } from '@playwright/test';
import { env } from './env.mjs';

/** Credentials the suite creates and reuses. Never a real account. */
export const ADMIN = { name: 'e2e-admin', password: 'e2e-first-boot-pw' };

/**
 * Helpers take `page.request`, not the bare `request` fixture, which has no cookies.
 * Before sign-in the state carries only `{required, note}`, so read `required`.
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

export async function createAdminThroughWizard(page, admin = ADMIN) {
  await page.goto('/setup');
  await expect(page.getByRole('heading', { name: 'Create the admin account' })).toBeVisible();
  await page.getByLabel('NAME').or(page.locator('input[type=text]').first()).fill(admin.name);
  await page.locator('input[type=password]').fill(admin.password);
  await page.getByRole('button', { name: 'Create admin' }).click();
  // The remaining steps are skippable: a bundle-less app is a legal state.
  await expect(page.getByRole('heading', { name: 'Connectors' })).toBeVisible();
  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page.getByRole('heading', { name: 'Import the bundle' })).toBeVisible();
  await page.getByRole('button', { name: 'Finish' }).click();
  await expect(page.getByTestId('home-title')).toBeVisible();
}

export async function login(page, admin = ADMIN) {
  await page.goto('/login');
  await page.locator('input[type=text]').first().fill(admin.name);
  await page.locator('input[type=password]').fill(admin.password);
  // The form can mount before its stylesheet on the phone, and the button then moves from under
  // the click. Playwright's `stable` check cannot see a stylesheet still on the wire.
  await page.waitForFunction(() =>
    [...document.querySelectorAll('link[rel="stylesheet"]')].every((link) => link.sheet !== null)
  );
  // `exact`: "Sign in with a passkey" is on the same page.
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByTestId('home-title')).toBeVisible();
}

export async function signedIn(page, admin = ADMIN) {
  const state = await setupState(page.request);
  if (state.required) {
    await createAdminThroughWizard(page, admin);
  } else {
    await login(page, admin);
  }
}

/** You (decision 527): the avatar opens a sheet holding what the account menu held. */
export async function openAccountMenu(page) {
  await page.getByTestId('account-chip').click();
  const sheet = page.getByRole('dialog', { name: 'You' });
  await expect(sheet).toBeVisible();
  return sheet;
}

/** Home's kind switch (decision 474): Films, Series or Both, one position pressed. */
export function kindToggle(page, label) {
  return page.getByRole('group', { name: 'Kind' }).getByRole('button', { name: label, exact: true });
}

export async function kindIsOn(page, label) {
  return (await kindToggle(page, label).getAttribute('aria-pressed')) === 'true';
}

export function kindPosition(kinds) {
  return kinds.includes('Films') && kinds.includes('Series')
    ? 'Both'
    : kinds.includes('Series')
      ? 'Series'
      : 'Films';
}

/**
 * The fake Jellyfin from `ops/compose.e2e.yml`. The app reaches it by service name, the same in
 * every checkout; the test reaches its published port, which is per checkout.
 */
export const JELLYFIN = {
  url: process.env.FAKE_JELLYFIN_URL ?? 'http://jellyfin-fake:8096',
  control:
    process.env.FAKE_JELLYFIN_CONTROL ??
    `http://127.0.0.1:${env('JELLYFIN_FAKE_PORT') ?? '8096'}`,
  apiKey: process.env.FAKE_JELLYFIN_API_KEY ?? 'e2e-jellyfin-key',
  password: process.env.FAKE_JELLYFIN_PASSWORD ?? 'e2e-jellyfin-password',
  user: { patrick: 'jf-user-patrick', jenny: 'jf-user-jenny' },
  item: { heat: 'jf-1', severance: 'jf-6' },
};

export async function resetJellyfin(request) {
  const res = await request.post(`${JELLYFIN.control}/_test/reset`);
  expect(res.ok(), 'the fake Jellyfin must be running — see ops/compose.e2e.yml').toBeTruthy();
}

export async function jellyfinState(request) {
  const res = await request.get(`${JELLYFIN.control}/_test/state`);
  expect(res.ok()).toBeTruthy();
  return res.json();
}

export async function markPlayedInJellyfin(request, itemId, played = true) {
  const res = await request.post(`${JELLYFIN.control}/_test/played`, {
    data: { user_id: JELLYFIN.user.patrick, item_id: itemId, played },
  });
  expect(res.ok()).toBeTruthy();
}

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

/** Whether the household's library holds a title of this name, among the kinds named. */
async function inLibrary(page, name, kinds) {
  const kind = kinds.map((k) => `kind=${k === 'Series' ? 'series' : 'movie'}`).join('&');
  const res = await page.request.get(
    `/api/titles?${kind}&owned=only&limit=60&q=${encodeURIComponent(name)}`
  );
  expect(res.ok(), 'the catalogue answers a search (§6.0)').toBeTruthy();
  return (await res.json()).items.some((title) => title.name === name);
}

/**
 * Home's catalog grid, for a spec that needs any poster card: Home's shelves are unseen-only
 * (decision 562), and by now the admin has seen nearly every owned film.
 */
export async function catalogGrid(page) {
  await page.goto('/');
  await page.getByRole('searchbox', { name: 'Search titles' }).fill('e');
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'grid');
  return page.locator('.grid .card-wrap');
}

/**
 * Open a title's detail sheet from the catalog. The card is matched by name, because the search
 * is debounced; Home shows Films only, so pass `['Films']` to leave that default alone. A title
 * the library does not hold is searched for beyond it (decision 558).
 */
export async function openTitle(page, name, { ensureKinds = ['Films', 'Series'] } = {}) {
  // The pending row lands with /api/home, above the grid: a tap aimed before it lands can hit the
  // space it pushes the card out of.
  const home = () => page.waitForResponse((res) => res.url().includes('/api/home?'));
  let landed = home();
  await page.goto('/');
  await landed;
  const position = kindPosition(ensureKinds);
  if (!(await kindIsOn(page, position))) {
    landed = home();
    await kindToggle(page, position).click();
    await landed;
  }
  await page.getByRole('searchbox', { name: 'Search titles' }).fill(name);
  if (!(await inLibrary(page, name, ensureKinds))) await page.getByTestId('search-beyond').click();
  const card = page.locator('.card-wrap', { hasText: name }).first();
  await card.click();
  const panel = page.getByRole('dialog', { name: 'Title detail' });
  await expect(panel.getByRole('heading', { name })).toBeVisible();
  // The heading is the tapped poster's at once (decision 530); the pair comes with the read.
  await expect(panel.getByTestId('title-watched')).toBeVisible();
  return panel;
}

/**
 * Create a household member. Observations are append-only (§4.2), so a spec needing a ledger of
 * its own needs an account of its own. `reuse` keeps one account per label across runs by
 * reissuing its one-time password; leave it off for a member with no history.
 */
export async function createMember(page, label, { reuse = false } = {}) {
  const password = `${label}-e2e-password`;
  if (reuse) {
    const roster = await page.request.get('/api/admin/users');
    expect(roster.ok(), 'the admin reads the household roster (§6.6)').toBeTruthy();
    const existing = (await roster.json()).find((user) => user.name === label);
    if (existing) {
      const reset = await page.request.post(`/api/admin/users/${existing.id}/reset-password`);
      expect(reset.ok(), 'reissuing a member one-time password (§6.6)').toBeTruthy();
      return { id: existing.id, name: label, otp: (await reset.json()).one_time_password, password };
    }
  }
  const name = reuse ? label : `${label}-${Date.now()}-${Math.floor(Math.random() * 1000)}`;
  const res = await page.request.post('/api/admin/users', { data: { name, role: 'member' } });
  expect(res.status(), 'the admin adds a household member (§6.6)').toBe(201);
  const created = await res.json();
  return { id: created.id, name, otp: created.one_time_password, password };
}

export async function signInAsMember(page, member) {
  await page.request.post('/api/auth/logout');
  const login = await page.request.post('/api/auth/login', {
    data: { name: member.name, password: member.otp }
  });
  expect(login.ok(), 'the one-time password signs the new member in').toBeTruthy();
  const changed = await page.request.post('/api/auth/password', {
    data: { current_password: member.otp, new_password: member.password }
  });
  expect(changed.ok(), 'setting a password unlocks the rest of the app').toBeTruthy();
  // Sign in again: the change rotates the session, and WebKit's `page.request` then stops sending
  // the browser's cookie. `11-rate.spec.js` keeps its own copy of this pair (decision 186), which
  // a repair here does not reach.
  const back = await page.request.post('/api/auth/login', {
    data: { name: member.name, password: member.password }
  });
  expect(back.ok(), 'the member signs in with the password they just chose').toBeTruthy();
}

/** Sign an already-created member in on a second context, by password. */
export async function loginAsMember(page, member) {
  await page.goto('/login');
  await page.locator('input[type=text]').first().fill(member.name);
  await page.locator('input[type=password]').fill(member.password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByTestId('home-title')).toBeVisible();
}

/**
 * The set-up's picks (§6.1) by the fixture bundle's ids: Heat (1) on "Excellent" and Prisoners (2) on
 * "OK". Both run long, so Home's school-night shelf keeps its three short films unseen,
 * and four films are left for Rate: Paddington 2, Chungking Express, its CJK twin and Tampopo.
 */
export const DEFAULT_SETUP_PICKS = [
  { title_id: 1, tier: 4 },
  { title_id: 2, tier: 1 }
];

/** Finish this session's member's set-up over HTTP; a member already set up counts as done. */
export async function setUpLadder(request, picks = DEFAULT_SETUP_PICKS) {
  const res = await request.post('/api/ladder/setup/finish', {
    data: { picks },
    failOnStatusCode: false
  });
  expect(
    res.ok() || res.status() === 409,
    `finishing the set-up (§6.1): ${res.status()} ${await res.text()}`
  ).toBeTruthy();
}

/**
 * Place what Rate serves of one kind, one tier per card in order (0 = the lowest step), until the
 * tiers or the queue run out. Rate opens only after the set-up. Returns how many were placed.
 */
export async function placeThroughRate(request, kind, tiers) {
  const opened = await request.post('/api/rate/session', {
    data: { kinds: [kind], restart: true },
    failOnStatusCode: false
  });
  expect(opened.ok(), `opening Rate for ${kind} (§6.1): ${opened.status()}`).toBeTruthy();
  let placed = 0;
  for (const tier of tiers) {
    const { card } = await (await request.get('/api/rate')).json();
    // Drained is the one legitimate exit: a `reuse`d account may have placed everything already.
    if (!card) break;
    expect(card.kind, `a ${kind} session served a ${card.kind} card (§6.1)`).toBe(kind);
    const answered = await request.post('/api/rate/place', {
      data: { card_token: card.token, tier },
      failOnStatusCode: false
    });
    // Loud: a refused write must not surface later as an empty pool.
    expect(answered.ok(), `placing ${card.title?.name} (§6.1): ${answered.status()}`).toBeTruthy();
    placed += 1;
  }
  // Closes the live session only, so a later spec does not resume a half-filled block.
  await request.delete('/api/rate/session');
  return placed;
}

/**
 * Give this session's member a film ladder: the set-up, then what Rate serves, across the steps.
 * Tonight needs more than three candidates, or the round has no shortlist boundary.
 */
export async function seedFilmLedger(page, rounds = 8) {
  await setUpLadder(page.request);
  return placeThroughRate(
    page.request,
    'movie',
    Array.from({ length: rounds }, (_, i) => [4, 1, 0][i % 3])
  );
}

/**
 * Wait until §6.3's board holds this member's placements. A placement only queues the full fit;
 * the worker's `tier-set-refit` job (`every=60`) writes the board.
 */
export async function waitForBoard(page, { kind = 'movie', atLeast = 1 } = {}) {
  // `fitting` (decision 209) tells "the worker owes a fit" from "nothing asked for one", which
  // are opposite repairs; kept across the poll so the failure can say which.
  let owed = null;
  try {
    await expect
      .poll(
        async () => {
          const res = await page.request.get(`/api/rank?kind=${kind}`, {
            failOnStatusCode: false
          });
          if (!res.ok()) {
            owed = `the route answered ${res.status()}`;
            return 0;
          }
          const payload = await res.json();
          owed = payload.fitting;
          return payload.tiers.flatMap((tier) => tier.entries).length;
        },
        {
          // Two 60 s ticks: one missed plus a spare.
          message:
            'no board after 120s: §6.3\'s board is every row of `ledger_state`, which a placement ' +
            'does not write - the set-up and each placement queue the full refit and the ' +
            'tier-set-refit sweep runs it every 60 s (worker.py; M4.10 finding 9)',
          timeout: 120_000,
          intervals: [2000]
        }
      )
      .toBeGreaterThanOrEqual(atLeast);
  } catch (err) {
    const why =
      owed === true
        ? 'a fit IS owed for this account (`fitting: true`), so the queue did its half and the ' +
          '60 s tier-set-refit sweep has not written `ledger_state` yet - suspect the worker: ' +
          'stopped, starved behind a longer job in its sequential loop, or abandoned at its own ' +
          '55 s budget and re-armed (`worker.py::_tick`; M4.11 finding 17)'
        : owed === false
          ? 'no fit is owed for this account (`fitting: false`), so NOTHING will arrive however ' +
            'long this waits. Either the seed wrote no observation at all - ' +
            '`createMember(..., {reuse: true})` hands a re-run the account it set up last time, ' +
            'whose Rate queue is drained, and `_queue_full_refit` is only reached by a write - or ' +
            'a fit was attempted and raised, and `_tier_set_refits` clears ' +
            '`refit_requested_at` either way (M4.10 finding 6), so only §5.3\'s nightly pass will ' +
            'fit it now. Check the worker log for `tier-set refit failed`'
          : `GET /api/rank never answered with a board: ${owed ?? 'no reading at all'}`;
    throw new Error(`${err.message}\n\n${why}`);
  }
}

/**
 * Wait until §5.1's per-user scores exist for this member: the worker's 60 s tick writes them,
 * not the verdict.
 */
export async function waitForPool(page, { budget = 200 } = {}) {
  await expect
    .poll(
      async () => {
        const res = await page.request.post('/api/tonight/solo', {
          data: { kind: 'movie', runtime_budget_min: budget, include_rewatches: true },
          failOnStatusCode: false
        });
        if (!res.ok()) return 0;
        return ((await res.json()).picks ?? []).length;
      },
      {
        // Two 60 s ticks: one missed plus a spare.
        message:
          'no picks after 120s: the fold-in tick runs every 60 s (worker.py; §5.3 gives the ' +
          'fold-in a nightly cadence), so two ticks have passed - suspect the seeded ledger',
        timeout: 120_000,
        intervals: [2000]
      }
    )
    .toBeGreaterThan(0);
}
