import { expect } from '@playwright/test';

/** Credentials the suite creates and reuses. Never a real account. */
export const ADMIN = { name: 'e2e-admin', password: 'e2e-first-boot-pw' };

/**
 * Read the app's own view of where it is in the first-boot sequence (§3.1).
 *
 * Every helper takes `page.request`, not the bare `request` fixture: the fixture is a separate
 * API context with no cookies, so it answers 401 for anything authenticated. `page.request`
 * shares the browser context, which is what the app actually sees.
 *
 * A caller with no session gets `{required, note}` and nothing else (sec-14: the full payload
 * fingerprints the install to anyone who can reach the origin), so read `required` rather than
 * `has_admin` — the same bit, and the only one present before anyone signs in.
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
  // The operator now walks the rest of §3.1's sequence instead of being thrown off it. The shell
  // used to bounce /setup to Home the instant the admin row existed, which made the last two
  // steps unreachable; the guard now bounces only a caller who is not a signed-in admin, and the
  // wizard ends at the bundle import (decision 164). Both remaining steps are skippable — a
  // bundle-less app is a legal state — so this walks them and finishes.
  await expect(page.getByRole('heading', { name: 'Connectors' })).toBeVisible();
  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page.getByRole('heading', { name: 'Import the bundle' })).toBeVisible();
  await page.getByRole('button', { name: 'Finish' }).click();
  // The wizard signs the new admin in and its last step lands on Home (§3.1).
  await expect(page.getByTestId('home-greeting')).toBeVisible();
}

// Landing on Home is the assertion, not the sentence Home happens to open with. These
// waited on /Good (morning|afternoon|evening)/ until M2 moved the greeting server-side, where
// proposal 22 gives it FOUR bands against §2's TZ — the fourth is "Up late". Every e2e run
// between 00:00 and 05:00 Europe/Berlin would have timed out here, in a helper, with a failure
// pointing at whichever spec happened to run first.
export async function login(page, admin = ADMIN) {
  await page.goto('/login');
  await page.locator('input[type=text]').first().fill(admin.name);
  await page.locator('input[type=password]').fill(admin.password);
  // `exact`, because M1 put "Sign in with a passkey" on the same page (§3.2 keeps password
  // login always available alongside it) and a substring match now resolves to two buttons.
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByTestId('home-greeting')).toBeVisible();
}

/** Ensure we are signed in, creating the admin on a first-boot app. */
export async function signedIn(page, admin = ADMIN) {
  const state = await setupState(page.request);
  if (state.required) {
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

/**
 * Create a household member and sign this page in as them. Spec v2.1 §6.6, §3.1.
 *
 * Lifted out of `13-rank.spec.js`, which had it first: §4.2's observations are append-only, so
 * a shared account cannot be rewound between runs and every spec that needs a ledger of its own
 * needs an account of its own. The sequence is §3.1's: a one-time password, a forced change,
 * then the member is usable.
 *
 * The route is §6.6's Users card, not the wizard's fourth step: decision 164 makes that card the
 * only place accounts are made, so seeding through it is the same path an operator walks.
 *
 * `reuse` gives a spec ONE account per (spec, project) instead of a fresh one on every run. The
 * names carried a timestamp and nothing ever removed the accounts, so a household box that has
 * run this suite a hundred times has a hundred members in §6.6's roster — and `reset.mjs` is not
 * always run before it. The account is looked up on the roster and its credential reissued
 * through §6.6's password reset, which is the only way back to a one-time password an admin may
 * hand over ("an admin never sees, sets or types a member's password", decision 166); creating
 * the same name twice is a 409 on `app_user_name_key`, not a second account. A caller that needs
 * a member with NO history — a first passkey, a first PIN — leaves it off and gets a new one.
 * [M4.8, finding 8]
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
      return { name: label, otp: (await reset.json()).one_time_password, password };
    }
  }
  const name = reuse ? label : `${label}-${Date.now()}-${Math.floor(Math.random() * 1000)}`;
  const res = await page.request.post('/api/admin/users', { data: { name, role: 'member' } });
  expect(res.status(), 'the admin adds a household member (§6.6)').toBe(201);
  return { name, otp: (await res.json()).one_time_password, password };
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
  // Sign in again with the password just set. The route answers 200 and sends no Set-Cookie -
  // `destroy_other_sessions` keeps the caller's own row, and driving the same flow through the
  // forced-change FORM leaves the browser signed in and on Home. But WebKit's APIRequestContext
  // and the browser context diverge here: `page.request` stops sending the cookie the browser
  // still holds, which left 13-rank and 14-tonight unauthenticated from this point on. Signing
  // in again is what a member holding a password can always do (S3.2), and it is what makes the
  // seeding that follows reach the app at all. 13-rank kept a PRIVATE copy of this pair that
  // never got the re-login and was refused from here on; decision 186 deletes that copy, so the
  // specs that import this file seed through one path. NOT the whole suite: `11-rate.spec.js`
  // declares a third copy of the pair at :209-230 and decision 186's Cost paragraph keeps it
  // deliberately — desktop-only, green, named by no finding — so a repair made here does not
  // reach it, and this milestone's own `reuse` is the standing example of one that did not.
  // [M4.8 review cycle 3: m48-c3-one-path-overclaim] Why the two contexts diverge at all is the
  // app's half of the question, and M4.10 owns it.
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
  await expect(page.getByTestId('home-greeting')).toBeVisible();
}

/**
 * Give this session's member a ledger, through §6.1's own routes.
 *
 * Films, not series: §6.2's pool for a film session is what Tonight needs, and it needs more
 * than three candidates or the round has no shortlist boundary to resolve (three titles ARE the
 * shortlist). `include_rewatches` then keeps the rated titles in the pool, since a verdict
 * implies `seen`.
 */
export async function seedFilmLedger(page, rounds = 8) {
  await page.request.post('/api/rate/session', {
    data: { mode: 'sweep', kinds: ['movie'], restart: true }
  });
  for (let i = 0; i < rounds; i++) {
    const { card } = await (await page.request.get('/api/rate')).json();
    if (!card || card.type !== 'sweep') break;
    const answered = await page.request.post('/api/rate/verdict', {
      data: { card_token: card.token, value: i % 3 },
      failOnStatusCode: false
    });
    // Loud, not silent — the rule `13-rank.spec.js` already states over its own seeding. The
    // `break` this replaces turned a refused write into an empty ledger, and the caller then met
    // `waitForPool`'s poll and failed 150 s later blaming the worker's fold-in tick for a seed
    // that never happened. [M4.8, finding 8]
    expect(answered.ok(), `seeding a verdict (§6.1): ${answered.status()}`).toBeTruthy();
  }
  await page.request.delete('/api/rate/session');
  // The state the caller needs, not the number of writes this run made: `createMember`'s `reuse`
  // hands a re-run the account it seeded last time, whose sweep is already drained and which
  // therefore legitimately answers nothing above. §6.3's board is "every rated title", so an
  // empty one is the seed having failed however it failed.
  const board = await page.request.get('/api/rank?kind=movie');
  expect(board.ok(), 'reading the seeded ledger back (§6.3)').toBeTruthy();
  const rated = (await board.json()).tiers.flatMap((tier) => tier.entries).length;
  expect(rated, 'this member has no rated films, so §6.2 has nothing to build a pool from')
    .toBeGreaterThan(0);
  return rated;
}

/**
 * Wait until §5.1's per-user scores exist for this member.
 *
 * `user_score` is written by the worker's fold-in tick — `every=60` in `worker.py`, which is the
 * worker's own addition and not a row of §5.3's table, where the fold-in has a nightly cadence —
 * and not by the verdict, so a spec that opened a room the instant it finished rating would meet
 * §6.2's empty pool and fail for a reason that has nothing to do with what it is testing.
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
        // Two ticks, named as two ticks. The tick is `every=60` in
        // `backend/spielplan/worker.py`, which says over the registration in as many words that
        // it is not in §5.3's table — §5.3 gives the fold-in a nightly cadence, and the tick is
        // the worker's addition for what a person sees within a sitting. So a verdict written a
        // moment after one tick waits out the rest of it and lands on the next: 120 s is one
        // whole missed tick plus a whole spare one. The old 150 s was a number with no
        // arithmetic behind it, and a ceiling nobody can derive is a ceiling nobody can read a
        // failure against — which is equally true of one derived from the wrong document, since
        // §5.3 read on its own says nightly. [M4.8 review cycle 3: m48-c3-foldin-citation]
        message:
          'no picks after 120s: the fold-in tick runs every 60 s (worker.py; §5.3 gives the ' +
          'fold-in a nightly cadence), so two ticks have passed - suspect the seeded ledger',
        timeout: 120_000,
        intervals: [2000]
      }
    )
    .toBeGreaterThan(0);
}
