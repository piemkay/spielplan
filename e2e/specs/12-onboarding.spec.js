import { devices, expect, test } from '@playwright/test';

import { signedIn } from '../helpers.js';

/**
 * Member first-run onboarding, in a real browser. Spec v2.1 §6 preamble, §3.1, §4.2, §12 (M2).
 *
 * §6's preamble is the requirement and the reason this file is shaped the way it is:
 *
 *   "on iPhone, Web Push works only for a PWA added to the home screen (iOS 16.4+) … iOS has
 *    no programmatic install prompt, so member first-run onboarding *guides* Share → Add to
 *    Home Screen, detects standalone mode, and nags until push is granted."
 *
 * WHAT A BROWSER CANNOT PROVE HERE, stated plainly so nobody reads more into a green run:
 *
 *   - **Installation itself.** Playwright cannot install a PWA, cannot open Chrome's install
 *     dialog and cannot press Safari's Share sheet. The install half is tested as far as our
 *     own code goes — which branch renders, and that the button spends the browser's event —
 *     and no further.
 *   - **A real `beforeinstallprompt`.** Chromium fires it on its own installability heuristics,
 *     never reliably inside a test run, so the event here is dispatched by the test. That
 *     proves our handling of the event, not that Chrome will hand us one.
 *   - **Web Push end to end.** There is no push service behind a test browser: `subscribe()`
 *     fails in headless Chromium with no endpoint to give. The one test that needs a
 *     subscription stubs the service-worker registration it comes from, so the *endpoint is
 *     fabricated* and everything after it — the permission gate, the POST, the row, the
 *     screen — is real. The `push` and `notificationclick` handlers are not exercised at all;
 *     they need a sender, which is M4.
 *   - **iOS.** A WebKit/iOS user agent is not an iPhone. It proves the platform branch, not
 *     that Add to Home Screen works or that iOS 16.4 will deliver a push to it.
 *
 * `backend/tests/test_push.py` owns the row-level properties (one row per endpoint, ownership,
 * the secrets never leaving). This file owns the screen.
 */
test.describe.configure({ mode: 'serial' });

// The stub above, injected into a context before anything loads. It replaces the whole
// `navigator.serviceWorker` container rather than reaching into the real one: a Proxy over a
// native container loses `this` on every method and throws "Illegal invocation" on the first
// call. The cost is that the real service worker is not registered in that context — which is
// why the test below that checks the real one runs in a context with no stub at all.
function pushServiceStub(endpoint) {
  const subscription = {
    endpoint,
    expirationTime: null,
    toJSON: () => ({
      endpoint,
      expirationTime: null,
      keys: { p256dh: 'BTestApplicationPublicKey', auth: 'test-auth-secret' }
    }),
    unsubscribe: async () => true
  };
  // Survives a reload, because a real phone does: the browser hands the same subscription back
  // on the next open, which is the case the server's upsert exists for.
  const HELD = 'e2e-push-subscription';
  const registration = {
    scope: `${location.origin}/`,
    pushManager: {
      getSubscription: async () => (localStorage.getItem(HELD) ? subscription : null),
      subscribe: async () => {
        localStorage.setItem(HELD, endpoint);
        return subscription;
      }
    }
  };
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    value: {
      ready: Promise.resolve(registration),
      register: async () => registration,
      addEventListener() {},
      controller: null
    }
  });
}

let sequence = 0;

/**
 * A member on their very first run: created by the admin, signed in with the one-time password,
 * through §3.1's forced password change — which lands them on `/account?welcome=1`, the screen
 * this file is about.
 */
async function firstRunMember(admin, browser, contextOptions = {}) {
  const name = `e2e-onboard-${Date.now()}-${sequence++}`;
  const created = await admin.request.post('/api/setup/members', {
    data: { name, role: 'member' }
  });
  expect(created.ok(), 'the admin must be able to create a member').toBeTruthy();
  const otp = (await created.json()).one_time_password;

  const context = await browser.newContext(contextOptions);
  const page = await context.newPage();
  await page.goto('/login');
  await page.locator('input[type=text]').first().fill(name);
  await page.locator('input[type=password]').fill(otp);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();

  await expect(page).toHaveURL(/\/account\/password/);
  await page.locator('input[type=password]').nth(0).fill(otp);
  await page.locator('input[type=password]').nth(1).fill('a-real-member-password');
  await page.locator('input[type=password]').nth(2).fill('a-real-member-password');
  await page.getByRole('button', { name: /Set password|Save|Continue/ }).click();
  await expect(page).toHaveURL(/\/account\?welcome=1/);

  return { context, page, name };
}

const pushState = async (page) => (await page.request.get('/api/push/state')).json();

test.describe('onboarding', () => {
  let admin;

  test.beforeAll(async ({ browser }) => {
    admin = await browser.newPage();
    await signedIn(admin);
  });

  test.afterAll(async () => {
    await admin?.close();
  });

  test('a member arrives at the onboarding prompt and is asked once', async ({ browser }) => {
    // §3.1 puts onboarding after the forced password change; that redirect is the whole reason
    // a member ever sees this screen without being sent a link.
    const { context, page } = await firstRunMember(admin, browser);
    try {
      const card = page.getByTestId('onboarding');
      await expect(card).toHaveAttribute('data-onboarding-state', 'prompt');
      await expect(page.getByTestId('onboarding-prompt')).toBeVisible();
      await expect(page.getByTestId('onboarding-decline')).toBeVisible();
      expect((await pushState(page)).onboarding_complete).toBe(false);
    } finally {
      await context.close();
    }
  });

  test('on iOS the screen guides Share → Add to Home Screen and offers no install button', async ({
    browser
  }) => {
    // §6 preamble: "iOS has no programmatic install prompt … onboarding *guides* Share → Add
    // to Home Screen". An Install button on an iPhone would be a button that cannot work.
    const { context, page } = await firstRunMember(admin, browser, { ...devices['iPhone 13'] });
    try {
      await expect(page.getByTestId('onboarding')).toHaveAttribute('data-platform', 'ios-safari');
      const steps = page.getByTestId('onboarding-ios-steps');
      await expect(steps).toBeVisible();
      await expect(steps).toContainText('Share');
      await expect(steps).toContainText('Add to Home Screen');
      await expect(page.getByTestId('onboarding-install')).toHaveCount(0);
      await expect(page.getByTestId('onboarding-install-unavailable')).toHaveCount(0);
    } finally {
      await context.close();
    }
  });

  test('the install button appears only when the browser offers one, and spends it', async ({
    browser
  }) => {
    const { context, page } = await firstRunMember(admin, browser);
    try {
      // Before the event: no button, and the screen says why rather than showing a dead one.
      await expect(page.getByTestId('onboarding-install')).toHaveCount(0);
      await expect(page.getByTestId('onboarding-install-unavailable')).toBeVisible();
      await expect(page.getByTestId('onboarding')).toHaveAttribute('data-platform', 'browser');

      // Dispatched by the test — see the header. Chromium will not fire this on demand.
      await page.evaluate(() => {
        const event = new Event('beforeinstallprompt');
        // @ts-expect-error — the real event carries these; this is the shape we consume.
        event.prompt = async () => {
          window.__installPrompts = (window.__installPrompts ?? 0) + 1;
        };
        // @ts-expect-error — as above.
        event.userChoice = Promise.resolve({ outcome: 'accepted', platform: 'web' });
        window.dispatchEvent(event);
      });

      const install = page.getByTestId('onboarding-install');
      await expect(install).toBeVisible();
      await expect(page.getByTestId('onboarding')).toHaveAttribute('data-platform', 'installable');

      await install.click();
      // The button must open the browser's own dialog — the event is the only way to do that,
      // and an install "confirmed" without calling prompt() would be a lie on the screen.
      expect(await page.evaluate(() => window.__installPrompts)).toBe(1);
      await expect(page.getByTestId('onboarding-install-outcome')).toContainText('Installed');
      // The event is single-use, so the button goes with it.
      await expect(install).toHaveCount(0);
    } finally {
      await context.close();
    }
  });

  test('granting push permission registers exactly one device, and re-opening adds none', async ({
    browser
  }) => {
    // §4.2's `push_subscription`, written for the first time by the act §6's preamble describes.
    const endpoint = `https://push.example.test/e2e/${Date.now()}`;
    const { context, page } = await firstRunMember(admin, browser);
    try {
      await context.grantPermissions(['notifications']);
      await context.addInitScript(pushServiceStub, endpoint);
      await page.reload();

      // Asserted before the click so a missing button fails with the state it was in rather
      // than hanging on an element that will never appear.
      await expect(page.getByTestId('onboarding')).toHaveAttribute('data-push-state', 'off');
      await page.getByTestId('onboarding-push-enable').click();
      await expect(page.getByTestId('onboarding')).toHaveAttribute('data-push-state', 'on');
      await expect(page.getByTestId('onboarding-device')).toHaveCount(1);

      const state = await pushState(page);
      expect(state.subscriptions).toHaveLength(1);
      // Never the endpoint or the keys — the account page identifies a device by a hash.
      expect(JSON.stringify(state)).not.toContain(endpoint);

      // §3.1's fifth step is recorded by the same act, and the nag stops.
      expect(state.onboarding_complete).toBe(true);
      await expect(page.getByTestId('onboarding')).toHaveAttribute(
        'data-onboarding-state',
        'settled'
      );
      await expect(page.getByTestId('onboarding-prompt')).toHaveCount(0);
      await expect(page.getByTestId('onboarding-decline')).toHaveCount(0);

      // Re-opening the app re-posts the subscription the browser still holds (a phone
      // re-registers its service worker on every update, and resubscribes with it). One
      // device, still — and the SAME row, which is the property §4.2's UNIQUE endpoint buys.
      await page.reload();
      await expect(page.getByTestId('onboarding-device')).toHaveCount(1);
      await expect(page.getByTestId('onboarding-prompt')).toHaveCount(0);
      const after = await pushState(page);
      expect(after.subscriptions).toHaveLength(1);
      expect(after.subscriptions[0].id).toBe(state.subscriptions[0].id);
      // The nag stops; the control does not. Turning notifications off later has to be
      // possible from the same place they were turned on.
      await expect(page.getByTestId('onboarding-push-disable')).toBeVisible();
    } finally {
      await context.close();
    }
  });

  test('declining stores nothing, finishes the step, and is not asked again', async ({
    browser
  }) => {
    // The load-bearing half of §3.1's fifth step: "declined" is a completion. It also shows the
    // scoping — the member above has a device row, and this member's list is empty.
    const { context, page } = await firstRunMember(admin, browser);
    try {
      await page.getByTestId('onboarding-decline').click();
      await expect(page.getByTestId('onboarding')).toHaveAttribute(
        'data-onboarding-state',
        'settled'
      );

      const state = await pushState(page);
      expect(state.onboarding_complete).toBe(true);
      expect(state.subscriptions).toEqual([]);

      await page.reload();
      await expect(page.getByTestId('onboarding-prompt')).toHaveCount(0);
      await expect(page.getByTestId('onboarding-decline')).toHaveCount(0);
      // The section stays and still says where this device stands — a completed step silences
      // the nag, not the settings. (A test browser reports notifications as already blocked,
      // so what shows here is the "change it in site settings" line rather than the button.)
      await expect(page.getByTestId('onboarding-push-state')).toBeVisible();
      expect((await pushState(page)).subscriptions).toEqual([]);
    } finally {
      await context.close();
    }
  });

  test('the service worker push depends on is served, registers, and caches no api response', async ({
    browser
  }) => {
    // Web Push does not exist without a service worker — the push event is delivered there and
    // nowhere else. And §6's "service-worker shell cache" must stay a *shell* cache: a cached
    // /api/rate card would hand back a card the server has already collected an answer for.
    const { context, page } = await firstRunMember(admin, browser);
    try {
      const served = await page.request.get('/service-worker.js');
      expect(served.status()).toBe(200);
      expect(served.headers()['content-type']).toMatch(/javascript/);

      const registered = await page.evaluate(async () => {
        const registration = await navigator.serviceWorker.ready;
        return registration.active?.scriptURL ?? '';
      });
      expect(registered).toContain('/service-worker.js');

      const cached = await page.evaluate(async () => {
        const names = await caches.keys();
        const paths = [];
        for (const name of names) {
          const cache = await caches.open(name);
          for (const request of await cache.keys()) paths.push(new URL(request.url).pathname);
        }
        return { names, paths };
      });
      expect(cached.names.some((name) => name.startsWith('spielplan-shell-'))).toBeTruthy();
      expect(cached.paths.filter((path) => path.startsWith('/api/'))).toEqual([]);
      // It is a shell cache, so the shell had better be in it.
      expect(cached.paths).toContain('/manifest.webmanifest');
    } finally {
      await context.close();
    }
  });
});
