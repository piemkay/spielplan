import { expect, test } from '@playwright/test';

import { createMember, login, openAccountMenu, signInAsMember, signedIn } from '../helpers.js';

/**
 * The box the whole app sits inside. Spec v2.1 §6 preamble, §6.8, §3.1, §3.2.
 *
 * §6's preamble - "responsive PWA, phone-first (48 px targets, one-handed, swipe), desktop as
 * progressive enhancement, installable, service-worker shell cache" - is normative and had no
 * owner: §12 schedules screens, every surface milestone built its own and left the chrome
 * alone, and no coverage row at all named `06-responsive`. The three `02-shell` ids this map
 * held were M0's session-cookie contract and M4.9's model rail, neither of them the preamble.
 * M4.15 decides those rules once, in the six files every other frontend milestone is forbidden
 * to touch, and this is where a browser is asked whether they hold.
 *
 * THE FILENAME IS LOAD-BEARING AND MUST NOT BE "TIDIED". `playwright.config.js`'s phone project
 * selects files with `testMatch: /(shell|library|responsive|13-rank|14-tonight)\.spec\.js/`, and
 * the alternation is anchored on `\.spec\.js` immediately after - so only a name ENDING in
 * `shell.spec.js` runs on the phone at all. The plan's `17-shell-phone.spec.js` would have run
 * on desktop only, which is the one project where none of this can be measured; 17 and 18 are
 * taken by M4.6 and M4.7 besides. Hence `19-phone-shell.spec.js` (decision 267). Renaming it
 * back does not fail: it quietly stops running where it matters.
 *
 * NINETEEN IS THE RIGHT POSITION WITHIN A PROJECT, WHICH IS NOT THE WHOLE ORDER. The specs are
 * stateful and filename-ordered on one worker, so inside either project everything that must not
 * meet this file's state - cleared cookies, the network taken away, a second household member
 * signed in on the same device - has already run. Playwright groups by PROJECT first, though:
 * every desktop test runs before the phone project starts, so the DESKTOP run of this file
 * completes and then five phone specs follow it - 02-shell, 03-library, 06-responsive, 13-rank
 * and 14-tonight.
 *
 * What crosses that boundary is narrow, and is named here rather than covered by the sentence
 * above. Cookies live in per-test contexts and the offline flag with them; decision 117's switch
 * is put back in a `finally`. The durable footprint is one household member - `shell-switch`,
 * created with `reuse: true` and given a password - and whatever a test here writes against a
 * member. Nothing downstream reads the roster as a count (13-rank and 14-tonight seed per
 * project, 17-users is desktop-only), so this costs nothing today; but a test added here that
 * writes an observation, deletes a member or leaves a preference standing has to be argued safe
 * for those five specs rather than assumed safe by position.
 * [decision 267; review cycle 3: M415-C3-E2E-05]
 *
 * WHAT THIS FILE CANNOT SEE, SAID OUT LOUD. `env()` resolves to 0 in every engine the suite
 * runs and Playwright's viewport IS the visible viewport by construction, so three facts - the
 * header clearing the status bar in installed standalone mode, the tab bar staying above
 * Safari's toolbar at every scroll position, and the absence of focus zoom on a real device -
 * cannot be produced here at all. They are owed in `docs/TESTING.md` as an unsigned device
 * check (decision 281), and the source-level guards in `backend/tests/test_static_contracts.py`
 * are their only standing substitute. A green run of this file is not that signature.
 */

/**
 * NO SERVICE WORKER IN THIS CONTEXT, EXCEPT IN THE ONE BLOCK WHERE THE CACHE IS THE SUBJECT.
 *
 * Three tests below hold a request open or answer one themselves, and network interception is a
 * Chromium-only instrument once a service worker is in the middle. `src/service-worker.js`
 * refuses to cache anything under `/api`, but it is still what every request passes through, so
 * on the phone project - iPhone 13, WebKit, the form factor this whole file exists for -
 * `page.route` never saw one: the room opened for real while the test waited for the sentence a
 * held request produces, and the two 401s the seam test fulfils would have been answered by the
 * live server instead. A test that cannot fail is worse than one that does. So this context
 * registers no worker, and what the routes say is what the app gets. Playwright says the same in
 * its own note on `page.route` - it "will not intercept requests intercepted by Service Worker"
 * (microsoft/playwright#1090) - and recommends this option for exactly this reason.
 * `02-shell.spec.js:210` wrote half of it down in M4.9 and read it as universal; it is not, which
 * is why three desktop-only specs route `/api` happily. [decision 284]
 */
test.use({ serviceWorkers: 'block' });

test.beforeEach(async ({ page }) => {
  await signedIn(page);
});

/** Decision 117's switch, set through the route the account dropdown PATCHes, as `02-shell`
 *  does: the rail is not this file's subject anywhere except as chrome that has to dismiss and
 *  be tappable, and re-opening the dropdown to reach the toggle would be another chance for an
 *  unrelated flake in a file that already drives four surfaces. The preference is read at boot,
 *  so the caller reloads after setting it. */
async function showModel(page, on) {
  const res = await page.request.post('/api/auth/preferences', { data: { show_model: on } });
  expect(res.ok(), `setting show_model=${on}: ${res.status()}`).toBeTruthy();
}

/**
 * Tonight, at the door.
 *
 * The restore is a round trip (§6.2 step 4: a reload must not cost somebody their evening), so
 * a device already seated in a live room comes back INTO it rather than to the controls. The
 * `Back` control is the household's own way out and leaves the seat alone, so this is the same
 * gesture a person makes, not a reset the test invented.
 */
async function atTonightDoor(page) {
  await page.goto('/tonight');
  await expect(page.getByTestId('tonight-surface')).toBeVisible();
  await expect(page.getByTestId('tonight-booting')).toHaveCount(0, { timeout: 20_000 });
  const back = page.getByTestId('tonight-back');
  if (await back.count()) await back.click();
  await expect(page.getByTestId('tonight-controls')).toBeVisible();
}

/**
 * The `phone` project is the subject of clauses 3 and 4 of this milestone's exit criterion, and
 * this is the sentence the desktop run prints instead of pretending to have measured them.
 */
const FINGERS = 'a finger is what this measures, and the desktop project has none';

// ---------------------------------------------------------------------------------------------
// 1. The 16 px rule
// ---------------------------------------------------------------------------------------------

/**
 * The input types iOS Safari never zooms for, because none of them takes text: a checkbox, a
 * radio, a slider, a file picker and the button-shaped inputs have no caret to magnify the page
 * around. `design.css`'s coarse rule excludes exactly the first four by selector, which is why
 * they are named here rather than left to fail a rule that was never about them.
 */
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

/** Every text-taking control on the page that computes under 16 px, described well enough to
 *  find in a stylesheet: iOS names neither the element nor the rule when it zooms. */
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
      // A control with no box cannot be focused, so it cannot zoom anything.
      if (!rect.width && !rect.height) continue;
      const size = parseFloat(getComputedStyle(el).fontSize);
      if (!(size >= 16)) out.push(`${describe(el)} computes ${size}px`);
    }
    return out;
  }, NO_CARET);
}

test('no form control on the member path zooms on focus', async ({ page, context, isMobile }) => {
  test.skip(!isMobile, FINGERS);

  // iOS Safari zooms the page on focus for any control whose text is under 16 px and does not
  // zoom back out; there is no gesture that says "undo that". §6's preamble makes the phone the
  // primary form factor and §3.1 puts a new member's very first tap in the /login name field,
  // so this is the app's first impression as well as its rule.
  //
  // THIS IS THE ONLY PLACE THE SPECIFICITY WORK IS VISIBLE. `design.css`'s coarse `input` rule
  // is (0,4,1) and outranks every scoped input rule in the tree, while a bare `select` is
  // (0,0,1) and LOSES to any component's own - so the global rule reaching a control is a claim
  // about cascade arithmetic, not about intent, and reading the COMPUTED size is what turns it
  // into a measurement. A scoped rule that still wins shows up here and nowhere else.
  //
  // /login is measured signed OUT, because that is the only state it renders in: `guard()`
  // sends a live session straight back to Home, so a signed-in visit would measure Home twice.
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

/** Both dimensions, always. `design.css`'s coarse block raises `min-height` and never
 *  `min-width`, so every control it reaches passes a height-only assertion by construction and
 *  the narrow axis is where this app's failures actually were: two overlay exits at 48 by 32,
 *  and a stack of account links at 34 that the block's selector list never named. */
async function meetsTheTouchFloor(locator, what) {
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

/** Phase 2 always has a bundle: `run.mjs` refuses to enter it otherwise, precisely so that a
 *  suite of skips cannot exit 0 and be read as a pass (§10's swap sequence). Asserting rather
 *  than skipping keeps that guarantee visible here instead of turning a broken stack into a
 *  quiet green. */
async function withABundle(page) {
  const config = await page.evaluate(() => fetch('/api/config').then((r) => r.json()));
  expect(
    config.has_bundle,
    'phase 2 runs against an imported bundle - see e2e/run.mjs'
  ).toBeTruthy();
}

test("the account menu's entries and the overlay exits meet the touch floor", async ({
  page,
  isMobile
}) => {
  test.skip(!isMobile, FINGERS);

  // §6 preamble: "phone-first (48 px targets, one-handed)", and `design.css` sets --touch: 48px.
  // These three controls are the ones `06-responsive`'s sweep could not see, and between them
  // they are the only phone path to /account and /admin and the only exit from a panel that
  // covers the screen.
  const menu = await openAccountMenu(page);
  const entries = menu.locator('[data-nav]');
  expect(await entries.count(), 'the account menu carries no entries at all').toBeGreaterThan(0);
  for (const entry of await entries.all()) {
    await meetsTheTouchFloor(entry, `the account menu's "${(await entry.textContent())?.trim()}"`);
  }
  await page.keyboard.press('Escape');

  await withABundle(page);
  await page.goto('/');
  await page.locator('.card-wrap').first().click();
  const panel = page.getByLabel('Title detail');
  await expect(panel).toBeVisible();
  await meetsTheTouchFloor(panel.locator('.close'), "the title panel's close button");
  await page.keyboard.press('Escape');

  await showModel(page, true);
  try {
    await page.goto('/');
    await page.getByTestId('model-rail-open').click();
    await expect(page.getByTestId('model-rail')).toBeVisible();
    await meetsTheTouchFloor(page.getByTestId('model-rail-close'), "the model rail's close button");
    // And the kind filters, eight lines under that close button in the same component and left
    // behind by its repair: `min-width` on one and not the other, so the exit came out 48 by 32
    // and the filters 48 by 36. The row is `{#if kinds.length > 1}`, so it is measured where it
    // renders and named where it does not - a loop over zero locators asserts nothing, which is
    // the vacuity this file exists to stop rather than to reproduce. The static guard holds the
    // rule on every tree; this holds it on the device.
    const all = page.getByTestId('model-rail-filter-all');
    if (await all.isVisible()) {
      await meetsTheTouchFloor(all, "the rail's \"all\" filter");
      for (const chip of await page.getByTestId('model-rail-filter').all()) {
        await meetsTheTouchFloor(chip, `the rail's "${(await chip.textContent())?.trim()}" filter`);
      }
    }
  } finally {
    // Default off is part of decision 117's contract, and every spec after this one opens on it.
    await showModel(page, false);
  }

  // THE ONE NAMED EXEMPTION, named here rather than absent from a selector.
  //
  // §3.1's wizard sequence is walkable - a step already done can be gone back to - so its
  // progress indicator is a 3 px hairline drawn as buttons, and `design.css`'s coarse
  // `button { min-height: var(--touch) }` rendered it as three 48 px grey blocks above the
  // heading on the first screen a household ever meets. Decision 280 exempts the indicator and
  // refuses the wide fix: no blanket `button { min-height: auto }` in design.css, which would
  // spare this hairline by giving up the floor for everything else. The skip is by testid so
  // the exemption is legible in a failure message, and the hairline's own rule is held by
  // `test_static_contracts.py::test_the_wizard_step_indicator_stays_a_hairline` - a control
  // this sweep steps over is a control this sweep cannot hold.
  //
  // Step 0 and no further: the last step mounts `BundleImport`, which another milestone is
  // rewriting this wave, and nothing here needs to walk the wizard to measure its chrome.
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
  // Proposal 131: "Every popover, menu and sheet dismisses on outside click and on Escape - the
  // prototype has neither, and an implementation copying it ships a menu you cannot click
  // away." All three of this app's overlays copied it. The menu was the worst of them:
  // navigation is client-side inside a persistent layout, so an undismissed dropdown followed
  // the person onto the next surface and sat over it.
  //
  // The brand in the header is the outside tap for all three, and it is outside all three by
  // construction rather than by luck: the title panel and the rail are both anchored BELOW the
  // header (`top: calc(54px + env(safe-area-inset-top))`, and the rail's phone form is a bottom
  // sheet), and the account menu hangs under the chip. It is a plain span with no handler of
  // its own, so what dismisses is the document-level listener and nothing else.
  //
  // `.click()` rather than `.tap()`: tap needs a touch-enabled context and would throw on the
  // desktop project, and the dismissal listens for `pointerdown`, which the mouse path emits on
  // both. That the listener is `pointerdown` at all is the phone's doing - a tap that drifts
  // produces no `click` - and is argued in `src/lib/dismiss.js`.
  await withABundle(page);
  const outside = page.getByText('SPIELPLAN', { exact: true });

  // --- the account menu. `openAccountMenu` clicks the chip and waits for `.menu`; the dismiss
  // action sits on `.wrap`, with the chip INSIDE it, so the helper is unchanged and still
  // correct - an outside-handler scoped to `.menu` would have fired on the chip's own
  // pointerdown, closed, and let the click reopen it.
  const menu = await openAccountMenu(page);
  await outside.click();
  await expect(menu, 'the account menu has no outside-tap dismissal').toHaveCount(0);

  await openAccountMenu(page);
  await page.keyboard.press('Escape');
  await expect(page.locator('.menu'), 'the account menu does not close on Escape').toHaveCount(0);

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

  // --- the model rail. Proposal 118 makes it a bottom sheet on compact layouts, which is the
  // shape that most needs a way out: 62vh of screen whose only exit was the control in its
  // corner.
  await showModel(page, true);
  try {
    await page.goto('/');
    const rail = page.getByTestId('model-rail');

    await page.getByTestId('model-rail-open').click();
    await expect(rail).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(rail, 'the model rail does not close on Escape').toHaveCount(0);

    await page.getByTestId('model-rail-open').click();
    await expect(rail).toBeVisible();
    await outside.click();
    await expect(rail, 'the model rail has no outside-tap dismissal').toHaveCount(0);

    // The trigger is OUTSIDE the drawer and it toggles, so a plain outside-tap dismissal closes
    // on its pointerdown and the click that follows reopens it - the control that opens the
    // drawer would permanently stop being able to close it, and every "the drawer is open"
    // assertion in this suite would still pass. `ModelRail.svelte` exempts the opener from its
    // own dismissal for exactly that reason; this is the assertion that the exemption is there.
    await page.getByTestId('model-rail-open').click();
    await expect(rail).toBeVisible();
    await page.getByTestId('model-rail-open').click();
    await expect(
      rail,
      'the rail trigger opens the drawer but can no longer close it'
    ).toHaveCount(0);
  } finally {
    await showModel(page, false);
  }

  // --- and the half that is not a dismissal gesture at all: the menu's own links. The entry
  // navigates inside the same document, so nothing unmounts the dropdown on the way.
  const again = await openAccountMenu(page);
  await again.locator('[data-nav="account"]').click();
  await expect(page).toHaveURL(/\/account$/);
  await expect(
    page.locator('.menu'),
    'the account menu rode a client-side navigation onto the next surface'
  ).toHaveCount(0);
});

// ---------------------------------------------------------------------------------------------
// 4-6. What the wire says, and what the household is told
// ---------------------------------------------------------------------------------------------

test('a 401 returns the member to the sign-in page', async ({ page, context }) => {
  // §3.2 makes the session a server fact the shell must obey. Before `api.js` had a seam there
  // was no interceptor at all: every store turned its own 401 into its own red line, so a
  // rotated SESSION_SECRET, a restored backup or the hourly prune printed `api/deps.py`'s "not
  // signed in" on Rate, then on Rank, then on Home - with the person's name still in the chip
  // and Log out the only way forward.
  //
  // THIS ASSERTION CANNOT PASS BY ACCIDENT, and that is worth stating because the obvious way
  // to write it can. The shell re-reads `/auth/me` at BOOT and nowhere else - no route change
  // calls `refreshUser` - so a client-side navigation with the cookie gone produces exactly one
  // thing that knows the session ended: the surface's own 401. `guard()` still holds the old
  // `session.user` and routes nobody. Without the seam this page stays on /rank with a red line
  // on it. A `page.goto` instead of a nav tap would have booted the app afresh and landed on
  // /login with no seam involved at all.
  await page.goto('/');
  await expect(page.getByTestId('home-greeting')).toBeVisible();

  const unauthorized = [];
  const watch = (res) => {
    if (res.status() === 401) unauthorized.push(new URL(res.url()).pathname);
  };
  page.on('response', watch);

  await context.clearCookies();
  await page
    .getByRole('navigation', { name: 'Surfaces' })
    .getByRole('link', { name: 'Rank', exact: true })
    .click();

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  page.off('response', watch);

  // "Within one request": the household met the refusal on ONE surface. Every 401 seen belongs
  // to the board they tapped; a second surface's path in this list is the failure the seam
  // exists to end.
  //
  // WHICH IS TWO PATHS, because Rank makes two reads and always has. `rank/+page.svelte:52-53`
  // asks for §4.1's kind-scoped genre and decade vocabulary beside the board itself, and both
  // are in flight from the same `onMount` before either can answer - so `/api/facets` here IS
  // the board they tapped, and naming only `/api/rank` described the surface wrongly rather than
  // describing a second one. Enumerating what one surface reads is not the same act as
  // forgiving a path because it went red: Home's `/api/home`, `/api/titles` and
  // `/api/prompts/finish` and Rate's `/api/rate` all still fail this line, and they are the
  // failure it was written for.
  const rankReads = (path) => path.startsWith('/api/rank') || path === '/api/facets';
  expect(
    unauthorized.filter((path) => !rankReads(path)),
    'the member met a 401 on a surface they never asked for'
  ).toEqual([]);

  // No backend sentence anywhere on the way out. §6.8: the copy a household reads is the app's,
  // and "not signed in" under a header carrying their own name is neither true nor actionable.
  await expect(page.locator('[role=alert]')).toHaveCount(0);
  await expect(page.locator('.err')).toHaveCount(0);
  // And the client-side session went with it: the chip that carried the name is gone. What a
  // reload would have cleared anyway is asserted where it belongs, in the sign-out test below.
  await expect(page.getByTestId('account-chip')).toHaveCount(0);

  // --- and now the two 401s that are NOT a lost session, which is the half that is easy to get
  // wrong in the direction that hurts more.
  await login(page);

  // A 401 from a route that VERIFIES a credential is the server answering the question that was
  // asked. Mistyping the account password into §3.2's PIN box must cost a sentence, not the
  // session it was typed from.
  //
  // Driven with `page.route` rather than with a genuinely wrong password: `api/auth.py:416`
  // counts every failed verify toward §3.2's lockout, and a test that spends one of a household
  // admin's attempts to prove a client-side rule has reached past its own subject. What is
  // under test is which paths `api.js` exempts, and that is decided entirely by the response.
  await page.route('**/api/auth/pin', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'wrong current password' })
    })
  );
  try {
    await page.goto('/account');
    const card = page.locator('section.card', { hasText: 'Switch PIN' });
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

  // And §3.2's 24-hour admin re-prompt, which is a 401 meaning "prove it again", not "you are
  // out". The server says so with a header, and that header is the only place it says so
  // outside `/auth/me` - which is read at boot and therefore still claims the clock is clear.
  // sec-05: the flag was parsed in `api.js` and consumed nowhere, so an admin past 24 hours met
  // raw 401s on every admin fetch with nothing on screen to explain them.
  await page.route('**/api/admin/users', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      headers: { 'x-spielplan-reauth': 'admin' },
      body: JSON.stringify({ detail: 'admin re-prompt' })
    })
  );
  try {
    await page.goto('/admin/users');
    await expect(page.getByTestId('admin-reauth')).toBeVisible();
    expect(new URL(page.url()).pathname, 'the re-prompt threw away a live admin session').toBe(
      '/admin/users'
    );
  } finally {
    await page.unroute('**/api/admin/users');
  }
});

/**
 * The sentence `api.js` says when its own deadline expires.
 *
 * Written as an escape rather than as the character, so this file stays ASCII on a Windows
 * cp1252 console while still asserting the literal the app ships: the em dash is the house
 * register for copy a household reads (`rank.svelte.js` is the precedent), and an ASCII hyphen
 * here would match nothing and quietly stop asserting.
 */
const NO_ANSWER = 'the network did not answer \u2014 try again';

test('a request that never answers ends with a sentence, not a dead surface', async ({ page }) => {
  // §2 puts this app on a LAN or a Tailscale address, where a request that never lands is the
  // ordinary failure rather than the exotic one. `fetch` has no timeout of its own, so before
  // the deadline the promise simply never settled: the door stayed busy, the control stayed
  // disabled, and nothing on the screen ever said what had happened. Ten seconds, declared in
  // `api.js`, for every route but the three long ones (decision 269).
  const OPEN_ROOM = /\/api\/tonight\/sessions$/;
  let held = null;
  await page.route(OPEN_ROOM, (route) => {
    held = route;
  });
  try {
    await atTonightDoor(page);
    await page.getByTestId('tonight-open').click();

    // Longer than the 10 s deadline, because the deadline IS the subject and the config's
    // expect timeout is exactly 10 s. The test timeout is 60 s, so this fits with room to
    // report in rather than to fail on the clock it is measuring.
    await expect(
      page.getByTestId('tonight-error'),
      'the request never ended in anything a person could read'
    ).toHaveText(NO_ANSWER, { timeout: 25_000 });

    // "Not a dead surface" is the other half, and the reason the deadline was worth having: the
    // controls are back, so the household can try again on the same screen.
    await expect(page.getByTestId('tonight-controls')).toBeVisible();
    await expect(page.getByTestId('tonight-open')).toBeEnabled();
  } finally {
    await page.unroute(OPEN_ROOM);
    await held?.abort().catch(() => {});
  }
});

test('a refused field says which field and why', async ({ page }) => {
  // FastAPI's `HTTPException` puts a string in `detail` and this app's importer puts an object
  // with a `text`, but pydantic's validation failure puts a LIST of `{type, loc, msg}` - which
  // matched neither case, so every refused field reached the household as the HTTP reason
  // phrase or as the empty string. Tonight's guests box is one of the two places an ordinary
  // control reaches that branch with no client-side guard in the way: Svelte's numeric binding
  // writes `null` when the field is emptied, and `api/tonight.py:248` bounds the field.
  await atTonightDoor(page);
  await page.getByTestId('tonight-guests').fill('');
  await page.getByTestId('tonight-open').click();

  const error = page.getByTestId('tonight-error');
  await expect(error).toBeVisible();
  // The field, then the server's own sentence about it. The sentence is pydantic's and is left
  // unpinned on purpose - pinning a library's copy is how a test starts failing on an upgrade
  // that broke nothing - but the two things the household was NOT told are pinned hard.
  await expect(error).toHaveText(/^guests: .+/);
  await expect(error).not.toHaveText('Unprocessable Entity');
});

// ---------------------------------------------------------------------------------------------
// 7-8. The two states a shared device gets into
// ---------------------------------------------------------------------------------------------

test.describe('the shell cache', () => {
  // The one block that needs a service worker, and decision 284's stated exception: an offline
  // boot IS the cache serving the document, so a context that registers no worker would be
  // asserting nothing. Nothing in here holds or answers a request, so nothing in here needs the
  // interception the rest of the file blocks the worker for.
  test.use({ serviceWorkers: 'allow' });

  test('an offline boot keeps the session and says the appliance is unreachable', async ({
    page,
    context,
    browserName
  }) => {
    // Not on WebKit, and not because the app fails there. Playwright's WebKit refuses the
    // navigation itself while the context is offline - `page.reload` returns "WebKit encountered
    // an internal error" nine milliseconds in, before the worker that holds the cached shell is
    // ever asked - so what a run here would measure is the harness. Reaching the state another
    // way costs the claim: with the worker blocked there is no cache to boot from, and with the
    // API stubbed instead the document comes off the network, which is the half being asserted.
    // So it is owed on a real device beside decision 281's other three, and NOT pre-signed.
    // [decision 284; §6 preamble]
    test.skip(
      browserName === 'webkit',
      'Playwright/WebKit cannot load a document while offline; owed as a device check'
    );
    // §6's preamble builds the shell cache for exactly this phone, and §3.1 asks for "an explicit
    // state instead of erroring". `bootstrap()` rethrew every non-401 `/auth/me` failure AFTER
    // setting `booted`, so the shell's own guard never ran, the already-armed effect read a user
    // nobody had managed to fetch, and an appliance that was merely restarting sent every open
    // phone to a sign-in form whose POST could not leave the device - with the session cookie
    // untouched the whole time. A failed read is not a sign-out; the 401 branch is.
    await page.goto('/');
    await expect(page.getByTestId('home-greeting')).toBeVisible();

    // The cache has to be IN CHARGE before the network goes, or the reload below is not an
    // offline boot of the app - it is a browser error page, and the test would be measuring
    // Playwright. `install` caches the shell and `activate` calls `clients.claim()`, so what is
    // waited for is the controller rather than the registration.
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

      // The session is a server fact and nothing here touched it: the cookie the phone holds is
      // the same one it held a moment ago, which is why routing to /login would have been both
      // useless and wrong.
      const cookies = await context.cookies();
      expect(
        cookies.some((c) => c.name === 'spielplan_session'),
        'the shell threw away the session over a read that never arrived'
      ).toBeTruthy();

      // Decision 271, seen from its cheapest side. The header's one line that states a fact about
      // the household rather than rendering one asserted "no bundle imported" from the initial
      // value whenever `/config` failed. The tri-state's own case - `/auth/me` answering while
      // `/config` does not - is asserted in `session.svelte.test.js`, where the two reads can be
      // failed independently; what a browser can say is that a shell which read nothing claims
      // nothing.
      await expect(page.getByText('no bundle imported')).toHaveCount(0);
    } finally {
      await context.setOffline(false);
    }

    // Back without a manual reload. In an installed standalone web view there is no reload
    // gesture to find, so asking again by itself is not a convenience - it is the only way back.
    // The `online` event is the fast path and it is NOT what this measures: Chromium's offline
    // emulation restores the network without dispatching one, which is the same silence a LAN
    // box that restarts under a live interface produces. What answers here is the shell's own
    // retry while the card is up (decision 283), which is the half a household can rely on.
    await expect(
      page.getByTestId('home-greeting'),
      'the shell stayed on the unreachable card after the network came back'
    ).toBeVisible({ timeout: 30_000 });
  });
});

test("the next person to sign in sees none of the previous one's surfaces", async ({ page }) => {
  // §3.2's chip and the per-user Ledger exist to keep two members apart on one device. The
  // three surface stores are module-level `$state` singletons that outlive a client-side
  // navigation, and logout reset Rank alone: Rate kept `card`, `session`, `ledger`, `log` and
  // `booted = true`, and `tonight.svelte.js`'s `bootstrap()` writes state only `if (mine)`, so
  // a member seated in no live room kept the previous person's controls, step, lobby, ballot
  // slate, ticked approvals and result indefinitely. On the family tablet that is one member's
  // taste shown to another.
  await withABundle(page);

  // A second household account with a password of its own, through §6.6's Users card - the same
  // path an operator walks (decisions 164 and 166).
  const member = await createMember(page, 'shell-switch', { reuse: true });
  await signInAsMember(page, member);
  // `signInAsMember` leaves this context holding the MEMBER's cookie, which is how it sets the
  // password. Back to the admin through the form, so the rest of this test starts where a
  // household does.
  await page.request.post('/api/auth/logout');
  await login(page);

  // The previous person leaves a trace on two surfaces. Tonight's controls are the sharper of
  // the two because they need no room and no clean-up: they are module state, they are on
  // screen the moment the door opens, and nothing on the server has heard of them.
  //
  // BOTH TRACES HAVE TO BE LEFT IN ONE DOCUMENT, which is why Rate is reached by a nav tap: a
  // `page.goto` between them would load a fresh page and reset `tonight.controls` before the
  // sign-out this test is about had a chance to fail to.
  const nav = page.getByRole('navigation', { name: 'Surfaces' });
  await atTonightDoor(page);
  await page.getByTestId('tonight-kind-series').click();
  await page.getByTestId('tonight-guests').fill('3');
  await expect(page.getByTestId('tonight-kind-series')).toHaveAttribute('aria-pressed', 'true');

  await nav.getByRole('link', { name: 'Rate', exact: true }).click();
  await expect(page.getByTestId('rate-surface')).toBeVisible();
  const theirCard = page.getByTestId('rate-card-title');
  await expect(
    theirCard,
    'the admin was served no card, so this test would prove nothing about clearing one'
  ).toBeVisible({ timeout: 20_000 });
  const theirTitle = (await theirCard.textContent())?.trim();
  expect(theirTitle, 'the card on screen carries no title to recognise it by').toBeTruthy();

  // Out through the chip, as a person does.
  //
  // LEAVING THE DOCUMENT IS THE SUBJECT AND IT HAS TO BE WAITED FOR. Decision 272 clears the
  // previous person by leaving the document rather than by three hand-written `clearAll()`
  // functions in three files: a document load is total by construction and cannot miss a field a
  // store gains next month. Decision 285 then named the destination - `location.assign('/login')`
  // and NOT `goto` followed by `location.reload()`, because a reload has no destination of its
  // own: it takes whatever is in the address bar, which at the third browser gate was /rate, and
  // Rate mounted for nobody and latched `api/deps.py`'s sentence into the next person's session.
  // If the document is never left, everything below runs inside the previous person's page -
  // which is the defect itself. `load` is the right instrument for it either way, which is why
  // this assertion cannot tell the two shapes apart and the sentence above has to.
  // [decisions 272, 285]
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

  // Signed in ON THE FORM THAT IS ALREADY THERE, and NOT through `loginAsMember`: that helper
  // opens with `page.goto('/login')`, and a fresh document load clears the stores whether or
  // not logging out had - so the assertions below would hold against the unfixed app, which is
  // the one thing they must not do. This is also the real journey: tap Log out, hand the phone
  // over, sign in as somebody else on the form that appears.
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
    // The frame the reload closes, and the only thing here worth holding a request open for:
    // the new person's first read is still in flight, so anything on screen now belongs to
    // somebody else. Reached by a nav TAP rather than a `goto`, for the reason above.
    await nav.getByRole('link', { name: 'Rate', exact: true }).click();
    await expect(page.getByTestId('rate-surface')).toBeVisible();
    await expect(page.getByTestId('rate-loading')).toBeVisible();
    await expect(page.getByTestId('rate-sweep-card')).toHaveCount(0);
    await expect(page.getByTestId('rate-battle-card')).toHaveCount(0);
    await expect(
      page.getByText(theirTitle, { exact: true }),
      "the previous person's card is on the new person's screen"
    ).toHaveCount(0);

    // Rank's own `reset()` has run on logout since M3 - but it clears the lift, the log and
    // `booted`, and leaves `tiers` standing, so the previous person's board is what a held read
    // would still be painting under. The board element itself is unconditional (proposal 82:
    // empty tiers stay on screen as valid drop targets), so what is counted is the rows in it.
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

  // And Tonight, which needs nothing held: the controls render from module state before any
  // read lands at all, so the previous person's choices would be on screen immediately.
  await nav.getByRole('link', { name: 'Tonight', exact: true }).click();
  await expect(page.getByTestId('tonight-surface')).toBeVisible();
  await expect(page.getByTestId('tonight-booting')).toHaveCount(0, { timeout: 20_000 });
  await expect(
    page.getByTestId('tonight-controls'),
    "the new person landed inside the previous one's evening"
  ).toBeVisible();
  await expect(
    page.getByTestId('tonight-kind-movie'),
    "the previous person's series night carried over"
  ).toHaveAttribute('aria-pressed', 'true');
  await expect(
    page.getByTestId('tonight-guests'),
    "the previous person's guests carried over"
  ).toHaveValue('0');
  // No ballot, no approvals, no reveal: all three live past the door, and `Back` is rendered
  // only when this device is somewhere past it.
  await expect(page.getByTestId('tonight-back')).toHaveCount(0);
});
