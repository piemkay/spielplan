import { expect, test } from '@playwright/test';

import {
  ADMIN,
  JELLYFIN,
  createMember,
  login,
  signInAsMember,
  signedIn
} from '../helpers.js';

/**
 * Admin > Users, end to end (§6.6, §3.1, §3.2; decisions 164, 166, 170). In a browser because
 * §6.6's surface *enforces* the floors, and a one-time password shown exactly once is a claim
 * about a screen and a reload. One admin page for the file, plus contexts for the others.
 */
test.describe.configure({ mode: 'serial' });

/** §6.6's roster line: role, passkey count, PIN state, Jellyfin link, active state. */
const FACTS = new RegExp(
  '^(admin|member) · \\d+ passkeys? · PIN (set|unset)' +
    ' · Jellyfin (unlinked|linked|needs sign-in) · (active|disabled)$'
);

test.describe('users, roles and the account surface', () => {
  /** @type {import('@playwright/test').Page} */
  let admin;
  const contexts = [];
  /** The third member this file adds, carried between tests. */
  const third = { name: `e2e-third-${Date.now()}`, otp: '', password: 'e2e-third-password' };
  /** @type {import('@playwright/test').Page} */
  let memberPage;

  async function fresh(browser, baseURL) {
    const context = await browser.newContext({ baseURL });
    contexts.push(context);
    return context.newPage();
  }

  test.beforeAll(async ({ browser, baseURL }) => {
    admin = await fresh(browser, baseURL);
    await signedIn(admin);
  });

  test.afterAll(async () => {
    for (const context of contexts) await context.close();
  });

  /** Open one roster row's editor, by account name. */
  async function openRow(name) {
    const row = admin.getByTestId('user-row').filter({ hasText: name }).first();
    await expect(row).toBeVisible();
    if ((await row.getByRole('button', { expanded: true }).count()) === 0) {
      await row.locator('button').first().click();
    }
    return row;
  }

  // --- 1 ------------------------------------------------------------------------------------

  test('the Users tab is a link and the roster carries every column 6.6 names', async () => {
    await admin.goto('/admin/data');
    await admin.getByRole('link', { name: 'Users' }).click();
    await expect(admin.getByRole('heading', { name: 'Users' })).toBeVisible();

    await expect(admin.getByTestId('users-roster')).toBeVisible();
    await expect(admin.getByTestId('user-row')).not.toHaveCount(0);
    // §14 risk 4, with the value printed, so an admin can check it against the phone in hand.
    await expect(admin.getByTestId('users-public-url')).toContainText(new URL(admin.url()).origin);

    for (const facts of await admin.locator('[data-user-facts]').allInnerTexts()) {
      expect(facts.replace(/\s+/g, ' ').trim()).toMatch(FACTS);
    }
  });

  // --- 2 ------------------------------------------------------------------------------------

  test('adding a member shows its one-time password once and nowhere afterwards', async () => {
    await admin.goto('/admin/users');
    await admin.getByLabel('New account name').fill(third.name);
    await admin.getByRole('button', { name: 'Create' }).click();

    const card = admin.getByTestId('user-otp');
    await expect(card).toContainText(third.name);
    third.otp = (await card.locator('code').innerText()).trim();
    expect(third.otp.length).toBeGreaterThan(8);

    // §6.6's third floor: shown once. A reload asks a second time.
    await admin.reload();
    await expect(admin.getByTestId('user-otp')).toHaveCount(0);
    await expect(admin.locator('body')).not.toContainText(third.otp);

    for (const path of ['/api/admin/users', '/api/auth/me', '/api/setup/state']) {
      const body = await (await admin.request.get(path)).text();
      expect(body, `${path} handed the one-time password back`).not.toContain(third.otp);
    }
  });

  // --- 3 ------------------------------------------------------------------------------------

  test('the member signs in on another browser and exchanges the password for its own', async ({
    browser,
    baseURL,
    request
  }) => {
    memberPage = await fresh(browser, baseURL);
    await memberPage.goto('/login');
    await memberPage.locator('input[type=text]').first().fill(third.name);
    await memberPage.locator('input[type=password]').fill(third.otp);
    await memberPage.getByRole('button', { name: 'Sign in', exact: true }).click();

    // §3.1: "the account is locked to a password change at first login".
    await expect(memberPage.getByRole('heading', { name: 'Choose a password' })).toBeVisible();
    // `exact`: the page's prose names the one-time password too.
    await expect(memberPage.getByText('ONE-TIME PASSWORD', { exact: true })).toBeVisible();
    const fields = memberPage.locator('input[type=password]');
    await fields.nth(0).fill(third.otp);
    await fields.nth(1).fill(third.password);
    await fields.nth(2).fill(third.password);
    await memberPage.getByRole('button', { name: 'Set password' }).click();
    // Not `home-greeting`: a WebAuthn browser lands on /account (§3.1's passkey prompt).
    await expect(memberPage).not.toHaveURL(/\/account\/password/);
    const me = await (await memberPage.request.get('/api/auth/me')).json();
    expect(me.must_change_password).toBe(false);

    const reused = await request.post('/api/auth/login', {
      data: { name: third.name, password: third.otp },
      failOnStatusCode: false
    });
    expect(reused.status()).toBe(401);
  });

  // --- 4 ------------------------------------------------------------------------------------

  test('a password reset reissues, re-arms the lock and ends the sessions', async ({
    request
  }) => {
    await admin.goto('/admin/users');
    await openRow(third.name);
    await admin.getByRole('button', { name: 'Reset password' }).click();

    const reissued = (await admin.getByTestId('user-otp').locator('code').innerText()).trim();
    expect(reissued).not.toBe(third.otp);

    const stale = await request.post('/api/auth/login', {
      data: { name: third.name, password: third.password },
      failOnStatusCode: false
    });
    expect(stale.status(), 'the password the member chose is gone').toBe(401);

    const signedInAgain = await request.post('/api/auth/login', {
      data: { name: third.name, password: reissued }
    });
    expect(signedInAgain.ok()).toBeTruthy();
    expect((await (await request.get('/api/auth/me')).json()).must_change_password).toBe(true);

    // §6.6: a reset ends the other devices' sessions too.
    const orphaned = await memberPage.request.get('/api/auth/me');
    expect(orphaned.status()).toBe(401);
    third.otp = reissued;
  });

  // --- 5 ------------------------------------------------------------------------------------

  test('disabling ends the account and deleting removes the row', async ({ request }) => {
    const back = await memberPage.request.post('/api/auth/login', {
      data: { name: third.name, password: third.otp }
    });
    expect(back.ok(), 'the member is signed in again, so disable has a session to end').toBeTruthy();

    await admin.goto('/admin/users');
    await openRow(third.name);
    await admin.getByRole('button', { name: 'Disable' }).click();
    await expect(
      admin.getByTestId('user-row').filter({ hasText: third.name }).first()
    ).toContainText('disabled');

    expect((await memberPage.request.get('/api/auth/me')).status()).toBe(401);
    const refused = await request.post('/api/auth/login', {
      data: { name: third.name, password: third.otp },
      failOnStatusCode: false
    });
    expect(refused.status(), 'a disabled account cannot sign in either').toBe(401);

    await openRow(third.name);
    await admin.getByRole('button', { name: 'Delete', exact: true }).click();
    await admin.getByRole('button', { name: 'Confirm delete' }).click();
    await expect(admin.getByTestId('user-row').filter({ hasText: third.name })).toHaveCount(0);
  });

  // --- 6 ------------------------------------------------------------------------------------

  test('accounts are made here and nowhere else, and there are two roles', async ({
    browser,
    baseURL
  }) => {
    // Decision 166: two roles, refused by the route's schema.
    const guest = await admin.request.post('/api/admin/users', {
      data: { name: 'ghost', role: 'guest' },
      failOnStatusCode: false
    });
    expect(guest.status()).toBe(422);

    // Decision 164: the wizard's member step is gone, route and all.
    const wizardRoute = await admin.request.post('/api/setup/members', {
      data: { name: 'ghost', role: 'member' },
      failOnStatusCode: false
    });
    expect(wizardRoute.status()).toBe(404);

    // …and the wizard is create admin -> connectors -> bundle, still reachable by the admin.
    await admin.goto('/setup');
    // ARIA makes a progressbar's children presentational, so the step buttons are outside it
    // (decision 280).
    const steps = admin.getByTestId('setup-step');
    await expect(steps).toHaveCount(3);
    await expect(admin.getByRole('progressbar')).toHaveCount(1);
    await expect(admin.getByRole('progressbar').getByRole('button')).toHaveCount(0);
    await expect(admin.getByRole('heading', { name: 'Connectors' })).toBeVisible();

    // §14 risk 4's warning and its origin sit together on step one, one click back.
    await steps.first().click();
    await expect(admin.getByRole('heading', { name: 'Create the admin account' })).toBeVisible();
    await expect(admin.getByTestId('setup-public-url')).toContainText(
      new URL(admin.url()).origin
    );
    await expect(admin.getByText(/Member accounts/i)).toHaveCount(0);
    await expect(admin.getByText(/Add to Home Screen/i)).toHaveCount(0);

    // ...and not a stranger's: both layers read `required`, the bit a stranger is given.
    const stranger = await fresh(browser, baseURL);
    // Whether the form EVER existed, in any frame before the client-side redirect lands.
    await stranger.addInitScript(() => {
      window.__mintable = false;
      const look = () => {
        const button = [...document.querySelectorAll('button')].some(
          (b) => b.textContent.trim() === 'Create admin'
        );
        if (button || document.querySelector('input[autocomplete=new-password]')) {
          window.__mintable = true;
        }
      };
      new MutationObserver(look).observe(document, { childList: true, subtree: true });
      look();
    });
    await stranger.goto('/setup');
    await expect(stranger).toHaveURL(/\/login$/);
    await expect(stranger.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    expect(
      await stranger.evaluate(() => window.__mintable),
      'a signed-out visitor was offered the admin-creation form on an installed app'
    ).toBe(false);
  });

  // --- 7 ------------------------------------------------------------------------------------

  test('the last active admin cannot be demoted, disabled or deleted', async () => {
    await admin.goto('/admin/users');
    const me = (await (await admin.request.get('/api/auth/me')).json()).id;

    const attempts = [
      ['PATCH', `/api/admin/users/${me}`, { role: 'member' }],
      ['POST', `/api/admin/users/${me}/active`, { is_active: false }],
      ['DELETE', `/api/admin/users/${me}`, undefined]
    ];
    for (const [method, url, data] of attempts) {
      const refused = await admin.request.fetch(url, { method, data, failOnStatusCode: false });
      expect(refused.status(), `${method} ${url}`).toBe(409);
      expect((await refused.json()).detail).toContain('the last active admin cannot be');
    }

    const roster = await (await admin.request.get('/api/admin/users')).json();
    const still = roster.find((u) => u.id === me);
    expect([still.role, still.is_active]).toEqual(['admin', true]);

    // §6.6 enforces the floors on the surface too.
    await admin.reload();
    await openRow(ADMIN.name);
    for (const action of ['demote', 'delete', 'disable', 'reset-password', 'reset-pin']) {
      await expect(admin.locator(`[data-floor="${action}"]`)).toBeVisible();
    }
    // Demote is the role `<select>`, not a button, so it is asserted by name.
    await expect(admin.getByLabel(`Role for ${ADMIN.name}`, { exact: true })).toBeDisabled();
    await expect(admin.getByRole('button', { name: 'Disable', exact: true })).toBeDisabled();
    await expect(admin.getByRole('button', { name: 'Delete', exact: true })).toBeDisabled();
    await expect(admin.getByRole('button', { name: 'Reset password' })).toBeDisabled();
  });

  // --- 8 ------------------------------------------------------------------------------------

  test('a PIN switch session cannot mint a credential', async ({ browser, baseURL }) => {
    // §3.2's PIN is "for fast user-switching on a shared device". Closes the chain: switch in,
    // register a passkey, sign in with it past the admin's 24 h re-prompt.
    const owner = await fresh(browser, baseURL);
    await login(owner);
    const member = await createMember(owner, 'pin-switch');
    const memberDevice = await fresh(browser, baseURL);
    await signInAsMember(memberDevice, member);
    const pinSet = await memberDevice.request.post('/api/auth/pin', {
      data: { pin: '4821', current_password: member.password }
    });
    expect(pinSet.ok(), 'decision 170: setting a PIN costs the password').toBeTruthy();
    const memberId = (await (await memberDevice.request.get('/api/auth/me')).json()).id;

    // A separate context, so the file's admin page keeps its session.
    const handover = await fresh(browser, baseURL);
    await login(handover);
    const switched = await handover.request.post('/api/auth/switch', {
      data: { user_id: memberId, pin: '4821' }
    });
    expect(switched.ok()).toBeTruthy();

    const garbage = { id: 'x', rawId: 'x', type: 'public-key', response: {} };
    // §3.2's credential routes, each given the strongest body the session could send.
    const minting = (password) => [
      ['POST', '/api/auth/passkey/register/options', {}],
      ['POST', '/api/auth/passkey/register', { ceremony_id: 'x', credential: garbage }],
      ['DELETE', '/api/auth/passkey/credentials/whatever', undefined],
      ['POST', '/api/auth/pin', { pin: '1111', current_password: password }],
      [
        'POST',
        '/api/push/subscribe',
        { endpoint: 'https://push.example/x', keys: { p256dh: 'k', auth: 'a' } }
      ]
    ];
    async function refusedEverything(device, password, whose) {
      for (const [method, url, data] of minting(password)) {
        const refused = await device.request.fetch(url, {
          method,
          data,
          failOnStatusCode: false
        });
        expect(refused.status(), `${whose}: ${method} ${url}`).toBe(403);
      }
    }
    await refusedEverything(handover, member.password, 'switched into the member');

    // Refused for the role before the re-prompt is checked; the admin switch below reaches it.
    const byRole = await handover.request.get('/api/admin/users', { failOnStatusCode: false });
    expect(byRole.status(), 'a member session is refused for being a member').toBe(403);

    // Into the ADMIN account: a PIN session carries no admin stamp, so §3.2's re-prompt answers.
    const adminPin = '9137';
    const pinned = await admin.request.post('/api/auth/pin', {
      data: { pin: adminPin, current_password: ADMIN.password }
    });
    expect(pinned.ok(), 'decision 170: the admin pays its own password for a PIN').toBeTruthy();
    const adminId = (await (await admin.request.get('/api/auth/me')).json()).id;
    const escalated = await memberDevice.request.post('/api/auth/switch', {
      data: { user_id: adminId, pin: adminPin }
    });
    expect(escalated.ok(), 'the shared device switches into the admin profile').toBeTruthy();

    await refusedEverything(memberDevice, ADMIN.password, 'switched into the admin');
    const shut = await memberDevice.request.get('/api/admin/users', { failOnStatusCode: false });
    expect(shut.status()).toBe(401);
    expect(shut.headers()['x-spielplan-reauth']).toBe('admin');
  });

  // --- 9 ------------------------------------------------------------------------------------

  test('the row editor lists a passkey and revokes that one credential', async ({
    browser,
    baseURL,
    browserName
  }) => {
    test.skip(browserName !== 'chromium', 'the virtual authenticator is a Chromium feature');
    // §6.6: "passkey list with per-credential revoke", on a real credential.
    const member = await createMember(admin, 'passkey-revoke');
    const device = await fresh(browser, baseURL);
    const cdp = await device.context().newCDPSession(device);
    await cdp.send('WebAuthn.enable');
    await cdp.send('WebAuthn.addVirtualAuthenticator', {
      options: {
        protocol: 'ctap2',
        transport: 'internal',
        hasResidentKey: true,
        hasUserVerification: true,
        isUserVerified: true,
        automaticPresenceSimulation: true
      }
    });
    await signInAsMember(device, member);
    await device.goto('/account');
    await device.getByPlaceholder('Name this device (optional)').fill('lost-phone');
    await device.getByRole('button', { name: 'Add a passkey' }).click();
    await expect(device.getByText('lost-phone')).toBeVisible();

    await admin.goto('/admin/users');
    const row = await openRow(member.name);
    const credential = row.getByTestId('user-passkey');
    await expect(credential).toHaveCount(1);
    await expect(credential).toContainText('lost-phone');
    await credential.getByRole('button', { name: 'Revoke' }).click();

    // The count comes from the roster, re-read: the server agreeing.
    await expect(row.locator('[data-user-facts]')).toContainText('0 passkeys');
    await expect(row.locator('[data-empty="passkeys"]')).toBeVisible();

    await device.goto('/account');
    await expect(device.locator('[data-empty="passkeys"]')).toBeVisible();
  });

  // --- 10 -----------------------------------------------------------------------------------

  test('the row editor links, re-links and unlinks Jellyfin', async () => {
    // §6.6: "Jellyfin re-link / unlink" in the row editor. 08-jellyfin left both fake users free.
    const member = await createMember(admin, 'jellyfin-link');
    await admin.goto('/admin/users');
    const row = await openRow(member.name);
    const jellyfin = row.getByTestId('user-jellyfin');
    await jellyfin.getByRole('combobox').selectOption({ label: 'patrick' });
    await jellyfin.getByRole('button', { name: 'Link', exact: true }).click();

    // §7.3: without a sign-in the link cannot write Played state.
    await expect(row.locator('[data-user-facts]')).toContainText('Jellyfin needs sign-in');

    await jellyfin.getByPlaceholder('jellyfin username').fill('patrick');
    await jellyfin.getByPlaceholder('password (once)').fill(JELLYFIN.password);
    await jellyfin.getByRole('button', { name: 'Re-link' }).click();
    await expect(row.locator('[data-user-facts]')).toContainText('Jellyfin linked');
    await expect(jellyfin.locator('[data-jellyfin="token"]')).toBeVisible();

    // §3.3: the link is optional.
    await jellyfin.getByRole('button', { name: 'Unlink' }).click();
    await expect(row.locator('[data-user-facts]')).toContainText('Jellyfin unlinked');
  });
});
