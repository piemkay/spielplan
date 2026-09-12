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

/**
 * What each surface puts on the screen that no other surface does.
 *
 * The identity, not the status code. `app.py:134-147` raises 404 only for paths beginning
 * `api/` and answers index.html for everything else, so the assertion this replaces - navigate,
 * then `status < 400` - was true of a renamed href, of a typo and of a path that never existed;
 * `07-boundaries.spec.js:28-30` already documents that fallback, which is what made the check
 * vacuous rather than merely weak. Its companion "main is not empty" was satisfied by
 * SvelteKit's own error page and by either placeholder, so neither half could fail.
 *
 * §12's unbuilt surfaces are identified by the placeholder's heading rather than by a testid:
 * `Milestone.svelte` renders the surface name as its `h1`, and the nav carries a LINK of the
 * same name, so the role filter is what separates the destination from the way in.
 * [tq4-nav-dead-link-check-against-a-200-for-everything]
 */
const MARKER = {
  '/': (page) => page.getByTestId('home-mode'),
  '/rate': (page) => page.getByTestId('rate-surface'),
  '/tonight': (page) => page.getByTestId('tonight-surface'),
  '/rank': (page) => page.getByTestId('rank-surface'),
  '/map': (page) => page.getByRole('heading', { level: 1, name: 'Map', exact: true }),
  '/taste': (page) => page.getByRole('heading', { level: 1, name: 'Taste', exact: true })
};

test('every nav destination resolves to its own surface', async ({ page }) => {
  const nav = page.getByRole('navigation', { name: 'Surfaces' });
  const hrefs = await nav.getByRole('link').evaluateAll((els) => els.map((e) => e.getAttribute('href')));

  // The table is half the assertion. A surface renamed in `api/auth.py`'s SURFACES and nowhere
  // else would otherwise look up `undefined` here and fail on a TypeError naming neither side
  // of the disagreement.
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

test('the account chip states the role and the auth method', async ({ page }) => {
  // §3.2: 'the chip reads "member · passkey + PIN"'. That string is an inventory of what the
  // account holds, not a constant — the chip printed it to everyone until fe-13 derived it from
  // `/auth/me`, so a member with neither credential was told they had both. The assertion here
  // used to be a regex admitting 'passkey + PIN' or 'PIN', which the constant satisfied by
  // construction and which the derived line does not: on the desktop project this runs before
  // 09-passkeys and 17-users item 8, so the admin holds neither and the chip reads
  // 'admin · password'; on the phone project, which runs after both, the same account holds
  // both. Asking the server what it holds is the one assertion that is true in both places and
  // that a constant cannot pass.
  const me = await (await page.request.get('/api/auth/me')).json();
  const method = [me.passkeys > 0 ? 'passkey' : 'password', me.has_pin ? 'PIN' : null]
    .filter(Boolean)
    .join(' + ');
  const menu = await openAccountMenu(page);
  await expect(menu.locator('.data').first()).toHaveText(`${me.role} · ${method}`);
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

/**
 * NO SERVICE WORKER FOR THE ONE TEST WHOSE SUBJECT IS A REQUEST THAT DOES NOT LAND.
 *
 * `page.route` does not see a request a service worker mediates, and this file registers no
 * worker option of its own — so on the `phone` project (iPhone 13, WebKit) the abort below never
 * happened: the POST landed, the session really ended, and the assertion could not fail. It was
 * then asserting what the test above it already asserts, on the form factor §6's preamble makes
 * primary, for a row M0 shipped. Decision 284 measured that and deferred the repair on the ground
 * that 02-shell was a file the gate had not opened; it is open in this milestone's diff, so the
 * ground is gone and the three lines are free.
 *
 * A describe rather than a file-scope `test.use`: `the model rail opens from every surface` and
 * `reopening the rail refills it from the live log, once` are cache-adjacent and were written
 * against a live worker, and 19-phone-shell blocks at file scope only because every one of its
 * tests wants that. [decision 284]
 */
test.describe(() => {
  test.use({ serviceWorkers: 'block' });

  test('a logout the server never answers still ends it on this device', async ({ page }) => {
    // feroutes-logout. §3.2 makes logout "clears the session cookie only", and on the LAN or
    // Tailscale origin §2 puts this app on, a request that never lands is the ordinary failure
    // rather than the exotic one. Unguarded, it rejected out of the click handler and left the
    // menu open showing the name of a person whose session the server may already have destroyed;
    // the local half — forget the user, drop Rank's pending lift, go to /login — must happen
    // either way, which is what the try/catch in the shell is for and what this aborts to prove.
    await page.route('**/api/auth/logout', (route) => route.abort());
    const menu = await openAccountMenu(page);
    await menu.getByRole('button', { name: 'Log out' }).click();
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    // The session the POST never ended is still alive on the appliance, which is why decision 272
    // was amended to leave the document only on a sign-out the server CONFIRMED: what this device
    // can still do is forget the person, and that is the whole of what is asserted here.
    expect((await page.request.get('/api/auth/me')).status()).toBe(200);
    await page.unroute('**/api/auth/logout');
  });
});

test("an unknown address renders the app's own error card, not the framework's page", async ({
  page
}) => {
  // The only test at any layer that RENDERS `+error.svelte`. The static guard beside it reads the
  // file - that it exists, that it imports design.css, that it prints `$page.error` and offers two
  // doors - and a file that is never mounted can satisfy all four while rendering nothing.
  //
  // How this address gets here: `app.py`'s SPA fallback answers index.html for every GET that is
  // not under `api/`, so a mistyped or retired address reaches the client router, which matches no
  // route; and because `+layout.js` sets `ssr = false` the first navigation is unhydrated, so
  // SvelteKit renders the ROOT error page rather than reloading the address it is already on. What
  // a household got before M4.15 was the framework's light-themed page on white, outside the
  // design system and with no way back into the dark shell - a dead end in an installed standalone
  // view, which has no address bar. §3.1 asks for an explicit state instead of an error.
  // [M4.15 finding 19, fe-15; review cycle 2: M415-C2-COMP-04]
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

/** Decision 117's switch, set through the route the account dropdown PATCHes.
 *
 *  Through the API rather than the menu here, unlike the toggle test above: that test's subject
 *  IS the control, while these two are about a drawer that the control merely gates, and each
 *  of them crosses four surfaces where re-opening the dropdown would be four more chances for
 *  an unrelated flake. `10-home.spec.js` seeds the same preference the same way. */
async function showModel(page, on) {
  const res = await page.request.post('/api/auth/preferences', { data: { show_model: on } });
  expect(res.ok(), `setting show_model=${on}: ${res.status()}`).toBeTruthy();
}

test('the model rail opens from every surface, not only Home', async ({ page }) => {
  // §6.7 calls the rail "the primary M2 debugging instrument" and proposal 118 requires it
  // "reachable in two taps" — from wherever the person is when the model does something
  // surprising, which is Rate and Rank more often than Home. It was mounted inside
  // `routes/+page.svelte`, so the four surfaces that write the events it narrates could not
  // open it at all, and the keyboard shortcut only worked on the one screen that did not need
  // it. [M4.9 finding 25]
  //
  // ONE mount and ONE control, asserted as counts rather than as visibility: the failure mode
  // of moving a drawer into the shell is leaving the old mount behind, and two drawers reading
  // the same ephemeral log render two copies of every line while only one of them closes.
  await showModel(page, true);
  try {
    for (const surface of ['/', '/rate', '/rank', '/tonight']) {
      await page.goto(surface);
      // Not bounced: the trigger is absent on `/login` and `/setup` too, for a reason that has
      // nothing to do with where the drawer is mounted.
      await expect(page, `${surface} bounced to sign-in`).not.toHaveURL(/\/login$/);

      const trigger = page.getByTestId('model-rail-open');
      await expect(trigger, `no rail control on ${surface}`).toHaveCount(1);
      await trigger.click();

      const rail = page.getByTestId('model-rail');
      await expect(rail, `${surface} mounted the drawer more than once`).toHaveCount(1);
      await expect(rail).toBeVisible();
      // §6.7's header is the promise the drawer makes about itself; proposal 118 pins the
      // depth ("the last 15 events — a pinned depth, not 'about fifteen'").
      await expect(rail).toContainText('last 15 events');

      await page.getByTestId('model-rail-close').click();
      await expect(rail, `${surface} could not close the drawer it opened`).toHaveCount(0);
    }
  } finally {
    // Default off is part of the contract, and every spec after this file opens on it.
    await showModel(page, false);
  }
});

test('reopening the rail refills it from the live log, once', async ({ page }) => {
  // The half of §6.7's "ephemeral log … never persisted" that only a real server can answer.
  //
  // THE OTHER HALF IS NOT HERE, AND CANNOT BE. The drawer must drop its payload on close, so
  // that the frame between a reopen and its refetch is the "reading the journal" branch rather
  // than the previous open's events under a live header. That frame is one round trip long, so
  // asserting it needs the response held — and this layer cannot hold it: the app is a PWA, and
  // although `src/service-worker.js` refuses to cache anything under `/api` it is still what
  // every request passes through, which is enough that `page.route` never sees one. Blocking
  // service workers means a browser context of this test's own, and a filename-ordered suite
  // that signs in once has none to spend. It is asserted where the claim lives, against a
  // mounted component with the reply held open, in
  // `frontend/src/lib/components/ModelRail.svelte.test.js`. [M4.9 finding 27]
  //
  // What is left here is the complement, and it is the part that would still be broken if the
  // clear were the whole fix: a drawer that drops the payload and does not read it back is a
  // panel stuck on the loading line, and one that merges instead of replacing shows every line
  // twice. §6.7's deque is ephemeral in the server's process, not in the drawer, so a close
  // that writes nothing leaves the same body to come back — whether that body is events or the
  // "nothing written yet" line depends on what this run has done by now, and the assertion is
  // that it is the SAME one either way.
  await showModel(page, true);
  try {
    await page.goto('/');
    const rail = page.getByTestId('model-rail');
    const body = rail.getByTestId('model-rail-event').or(rail.getByTestId('model-rail-empty'));

    await page.getByTestId('model-rail-open').click();
    await expect(rail).toBeVisible();
    await expect(body.first(), 'the first open never finished reading').toBeVisible();
    const read = await body.allTextContents();

    await page.getByTestId('model-rail-close').click();
    await expect(rail).toHaveCount(0);

    await page.getByTestId('model-rail-open').click();
    await expect(rail).toBeVisible();
    await expect(body.first(), 'the reopened drawer never finished reading').toBeVisible();
    expect(await body.allTextContents(), 'the reopened drawer is not the log again').toEqual(read);
    // §6.7's header is the promise the drawer makes about itself, and it is the promise the
    // reopened one has to be able to keep.
    await expect(rail).toContainText('last 15 events');
  } finally {
    await showModel(page, false);
  }
});
