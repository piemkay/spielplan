import { devices, expect, test } from '@playwright/test';

import { signedIn } from '../helpers.js';

/**
 * Member first-run onboarding (§6 preamble, §3.1, §4.2): Share → Add to Home Screen guidance and
 * the push prompt. What a browser cannot prove here: installation itself, a real
 * `beforeinstallprompt` (dispatched by the test), a push service (the registration is stubbed, so
 * the endpoint is fabricated and everything after it is real), a second real device, or iOS.
 */
test.describe.configure({ mode: 'serial' });

// Replaces the whole `navigator.serviceWorker`: a Proxy over the native one throws "Illegal
// invocation". So the real service worker is tested in a context without the stub.
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
  // Survives a reload, as a real phone's subscription does.
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

/** A member on first run, through §3.1's forced password change to `/account?welcome=1`. */
async function firstRunMember(admin, browser, contextOptions = {}) {
  const name = `e2e-onboard-${Date.now()}-${sequence++}`;
  const created = await admin.request.post('/api/admin/users', {
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
    // to Home Screen".
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
      await expect(page.getByTestId('onboarding-install')).toHaveCount(0);
      await expect(page.getByTestId('onboarding-install-unavailable')).toBeVisible();
      await expect(page.getByTestId('onboarding')).toHaveAttribute('data-platform', 'browser');

      // Chromium will not fire this on demand.
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
      // The button must call the event's prompt(), the only way to the browser's own dialog.
      expect(await page.evaluate(() => window.__installPrompts)).toBe(1);
      await expect(page.getByTestId('onboarding-install-outcome')).toContainText('Installed');
      await expect(install).toHaveCount(0);
    } finally {
      await context.close();
    }
  });

  test('granting push permission registers exactly one device, and re-opening adds none', async ({
    browser
  }) => {
    const endpoint = `https://push.example.test/e2e/${Date.now()}`;
    const { context, page } = await firstRunMember(admin, browser);
    try {
      await context.grantPermissions(['notifications']);
      await context.addInitScript(pushServiceStub, endpoint);
      await page.reload();

      // Before the click, so a missing button fails with its state.
      await expect(page.getByTestId('onboarding')).toHaveAttribute('data-push-state', 'off');
      await page.getByTestId('onboarding-push-enable').click();
      await expect(page.getByTestId('onboarding')).toHaveAttribute('data-push-state', 'on');
      await expect(page.getByTestId('onboarding-device')).toHaveCount(1);

      const state = await pushState(page);
      expect(state.subscriptions).toHaveLength(1);
      // Never the endpoint or the keys: a device is identified by a hash.
      expect(JSON.stringify(state)).not.toContain(endpoint);

      expect(state.onboarding_complete).toBe(true);
      await expect(page.getByTestId('onboarding')).toHaveAttribute(
        'data-onboarding-state',
        'settled'
      );
      await expect(page.getByTestId('onboarding-prompt')).toHaveCount(0);
      await expect(page.getByTestId('onboarding-decline')).toHaveCount(0);

      // Re-opening re-posts the held subscription: the SAME row (§4.2's UNIQUE endpoint).
      await page.reload();
      await expect(page.getByTestId('onboarding-device')).toHaveCount(1);
      await expect(page.getByTestId('onboarding-prompt')).toHaveCount(0);
      const after = await pushState(page);
      expect(after.subscriptions).toHaveLength(1);
      expect(after.subscriptions[0].id).toBe(state.subscriptions[0].id);
      // The nag stops; the control does not.
      await expect(page.getByTestId('onboarding-push-disable')).toBeVisible();
    } finally {
      await context.close();
    }
  });

  test('declining stores nothing, finishes the step, and is not asked again', async ({
    browser
  }) => {
    // §3.1's fifth step: "declined" is a completion.
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
      // A completed step silences the nag, not the settings. (A test browser reports
      // notifications as blocked, so this is the "site settings" line, not the button.)
      await expect(page.getByTestId('onboarding-push-state')).toBeVisible();
      expect((await pushState(page)).subscriptions).toEqual([]);
    } finally {
      await context.close();
    }
  });

  /**
   * The member's OTHER phone, through §4.2's route. `push.example.test`: `SubscriptionIn` refuses
   * plain HTTP and private or loopback hosts (sec-13).
   */
  async function registerTheOtherPhone(page) {
    const created = await page.request.post('/api/push/subscribe', {
      data: {
        endpoint: `https://push.example.test/other-phone/${Date.now()}-${sequence++}`,
        keys: { p256dh: 'BOtherPhonePublicKey', auth: 'other-phone-auth-secret' },
        device_label: 'The other phone'
      }
    });
    expect(created.status(), 'the member registers their other phone (§4.2)').toBe(201);
    return (await created.json()).id;
  }

  test('a browser holding no subscription of its own reads off and is offered the switch', async ({
    browser
  }) => {
    // The account's device list cannot say whether THIS browser is registered; only the browser
    // knows. Nothing is stubbed here: the app's own service worker, and no subscription.
    const { context, page } = await firstRunMember(admin, browser);
    try {
      // Granted: a test browser starts `denied`, which is a different screen.
      await context.grantPermissions(['notifications']);
      await registerTheOtherPhone(page);
      await page.reload();

      const card = page.getByTestId('onboarding');
      await expect(card).toHaveAttribute('data-push-state', 'off');
      await expect(page.getByTestId('onboarding-push-enable')).toBeVisible();
      await expect(page.getByTestId('onboarding-push-disable')).toHaveCount(0);

      // The ACCOUNT's list, shown whether this device is on or off. `unknown`: over plain HTTP
      // there is no `crypto.subtle` to hash this browser's endpoint with.
      const rows = page.getByTestId('onboarding-device');
      await expect(rows).toHaveCount(1);
      await expect(rows.first()).toHaveAttribute('data-device', 'unknown');
      await expect(page.getByTestId('onboarding-devices-why')).toContainText(
        'None of these is this browser'
      );

      expect((await pushState(page)).subscriptions).toHaveLength(1);
    } finally {
      await context.close();
    }
  });

  test('turning notifications off on one device leaves the other one registered', async ({
    browser
  }) => {
    // The DELETE answers with what is LEFT; the other phone must still be listed.
    const endpoint = `https://push.example.test/this-browser/${Date.now()}`;
    const { context, page } = await firstRunMember(admin, browser);
    try {
      await context.grantPermissions(['notifications']);
      await context.addInitScript(pushServiceStub, endpoint);
      const otherPhone = await registerTheOtherPhone(page);
      await page.reload();

      await expect(page.getByTestId('onboarding')).toHaveAttribute('data-push-state', 'off');
      await page.getByTestId('onboarding-push-enable').click();
      await expect(page.getByTestId('onboarding')).toHaveAttribute('data-push-state', 'on');

      // "this" comes from the act itself: the state route answers the same list to both.
      await expect(page.getByTestId('onboarding-device')).toHaveCount(2);
      await expect(
        page.locator('[data-testid="onboarding-device"][data-device="this"]')
      ).toHaveCount(1);
      await expect(
        page.locator('[data-testid="onboarding-device"][data-device="other"]')
      ).toHaveCount(1);

      await page.getByTestId('onboarding-push-disable').click();
      await expect(page.getByTestId('onboarding')).toHaveAttribute('data-push-state', 'off');
      await expect(page.getByTestId('onboarding-push-enable')).toBeVisible();
      const rows = page.getByTestId('onboarding-device');
      await expect(rows).toHaveCount(1);
      await expect(rows.first()).toHaveAttribute('data-device', 'unknown');

      const left = await pushState(page);
      expect(left.subscriptions).toHaveLength(1);
      expect(left.subscriptions[0].id, 'the surviving row is the OTHER phone').toBe(otherPhone);
      // Never the endpoint: the other one is a bearer capability for somebody's lock screen.
      expect(JSON.stringify(left)).not.toContain(endpoint);
    } finally {
      await context.close();
    }
  });

  test('the service worker push depends on is served, registers, and caches no api response', async ({
    browser
  }) => {
    // §6's "service-worker shell cache" must stay a shell cache: a cached /api/rate card would
    // hand back a card already answered.
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
      expect(cached.paths).toContain('/manifest.webmanifest');
    } finally {
      await context.close();
    }
  });
});
