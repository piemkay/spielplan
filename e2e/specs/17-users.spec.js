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
 * Admin > Users, end to end. Spec v2.1 §6.6, §3.1, §3.2, §14 risk 4; decisions 164, 166, 170.
 *
 * This file is M4.6's exit criterion, and it is a browser test rather than a `curl` transcript
 * on purpose: §6.6 says the surface *enforces* the floors, and "the route answers 409" is not
 * the same claim as "the household can see why the button is grey". The one-time password is
 * the sharpest case — §6.6's third floor is that it is shown exactly once, which is a statement
 * about a screen and a reload, not about a response body.
 *
 * Before this milestone the only account-minting UI was the wizard's fourth step, which the
 * shell redirected away from the moment an admin existed: a household could not add its third
 * member at all, and the Users tab was a dead `<span>` labelled M5.
 *
 * ONE PAGE FOR THE FILE, plus contexts for the people who are not the admin. Playwright hands
 * each test a fresh context, which would throw away the member these tests create in turn — the
 * same reason `15-tonight-group.spec.js` keeps its own pages. Desktop only, and for the same
 * reason that file is: the member's second browser and the handed-over phone are two more
 * contexts, and the phone project exists for the one-device gestures §6's preamble is about.
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
    // §14 risk 4 is repeated here because revocation is felt here, and it prints the value:
    // an admin can only check the origin against the address on the phone in their hand.
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

    // §6.6's third floor. A reload is the cheapest way to ask for it a second time, and the
    // roster carries no password field for it to come back in.
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

    // §3.1: "the account is locked to a password change at first login", and the lock is the
    // auth layer's — the shell has nowhere else to send them.
    await expect(memberPage.getByRole('heading', { name: 'Choose a password' })).toBeVisible();
    // `exact`: step 18 put an explanatory paragraph on this page that names the one-time
    // password in a sentence, so the bare substring matches the prose as well as the label.
    await expect(memberPage.getByText('ONE-TIME PASSWORD', { exact: true })).toBeVisible();
    const fields = memberPage.locator('input[type=password]');
    await fields.nth(0).fill(third.otp);
    await fields.nth(1).fill(third.password);
    await fields.nth(2).fill(third.password);
    await memberPage.getByRole('button', { name: 'Set password' }).click();
    // Not `home-greeting`: §3.1 says "passkey registration is prompted afterwards", so a
    // browser that supports WebAuthn lands on /account instead. What the lock clearing means
    // is that this page is no longer where the shell puts them.
    await expect(memberPage).not.toHaveURL(/\/account\/password/);
    const me = await (await memberPage.request.get('/api/auth/me')).json();
    expect(me.must_change_password).toBe(false);

    // The one-time password is spent, not merely superseded.
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

    // §3.1: the reissue re-arms the same lock the first issue armed.
    const signedInAgain = await request.post('/api/auth/login', {
      data: { name: third.name, password: reissued }
    });
    expect(signedInAgain.ok()).toBeTruthy();
    expect((await (await request.get('/api/auth/me')).json()).must_change_password).toBe(true);

    // §6.6: a reset that left the other devices signed in would reset nothing an attacker holds.
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
    // Decision 166. The role is a `Literal` on the one route that makes accounts, so this is
    // the schema's refusal rather than a hand-written check; migration 0016's CHECK says the
    // same thing one layer down, which `test_account_security.py` asserts against the column.
    const guest = await admin.request.post('/api/admin/users', {
      data: { name: 'ghost', role: 'guest' },
      failOnStatusCode: false
    });
    expect(guest.status()).toBe(422);

    // Decision 164: the wizard's member step is gone, route and all. 404 rather than the 405 a
    // deleted-but-path-shaped route answers with: the SPA catch-all `create_app` mounts in the
    // container declines the /api namespace at match time, so a route that is gone reads as
    // gone here exactly as it does under pytest, where no catch-all exists (app.py `SpaFallback`).
    const wizardRoute = await admin.request.post('/api/setup/members', {
      data: { name: 'ghost', role: 'member' },
      failOnStatusCode: false
    });
    expect(wizardRoute.status()).toBe(404);

    // …and the wizard itself now runs create admin -> connectors -> bundle, and stops. It is
    // also still reachable, which it was not: the shell used to bounce an admin to Home the
    // instant the admin row existed, making its last two steps unreachable in the built app.
    await admin.goto('/setup');
    const steps = admin.getByRole('progressbar').getByRole('button');
    await expect(steps).toHaveCount(3);
    // A revisiting admin lands on step two, because `onMount` skips the step whose work is done.
    await expect(admin.getByRole('heading', { name: 'Connectors' })).toBeVisible();

    // §14 risk 4's warning and the origin it warns about live on step one, next to each other
    // (plan step 19), so that is where this asks for them. The dots are the wizard's own
    // navigation and the admin step is one click back: an operator who wants to check which
    // origin their passkeys are bound to can reach it, which is the claim being made.
    await steps.first().click();
    await expect(admin.getByRole('heading', { name: 'Create the admin account' })).toBeVisible();
    await expect(admin.getByTestId('setup-public-url')).toContainText(
      new URL(admin.url()).origin
    );
    await expect(admin.getByText(/Member accounts/i)).toHaveCount(0);
    await expect(admin.getByText(/Add to Home Screen/i)).toHaveCount(0);

    // ...and it is the ADMIN's revisitable page, not a stranger's. sec-14 cut `has_admin` out
    // of the anonymous /setup/state payload while the wizard and the shell were both still
    // deciding from it, and `undefined` is falsy: an installed household app answered a
    // passer-by at PUBLIC_URL/setup with the enabled "Create the admin account" form (fe-46).
    // `required` is the bit a stranger IS given and §3.1 defines it as exactly "no admin
    // exists", so it is what both layers now read: the shell sends this visitor to /login and
    // the wizard has nothing to mint on the way.
    const stranger = await fresh(browser, baseURL);
    // Whether the form ever EXISTED, not whether it is gone once the dust settles. The bounce is
    // a client-side navigation and the wizard renders a frame before it lands, so the redirect
    // alone leaves the visitor a mintable form to look at — asserting after the fact cannot see
    // it. This observer is installed before hydration and answers for every frame in between,
    // which is why the page reads `required` as well as the shell redirecting on it.
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

    // §6.6 enforces the floors on the surface too: a control that only fails once pressed
    // teaches that the rule is a server mood rather than the household's shape.
    await admin.reload();
    await openRow(ADMIN.name);
    for (const action of ['demote', 'delete', 'disable', 'reset-password', 'reset-pin']) {
      await expect(admin.locator(`[data-floor="${action}"]`)).toBeVisible();
    }
    // Each of the three the floor names, by the control the household would press. The demote
    // control is the role `<select>` and not a button, so a sweep over buttons alone reports
    // two of the three and passes while the one §6.6 states first goes unchecked — which is
    // what happened: `Disable` and the select were asserted disabled at no layer at all.
    await expect(admin.getByLabel(`Role for ${ADMIN.name}`, { exact: true })).toBeDisabled();
    await expect(admin.getByRole('button', { name: 'Disable', exact: true })).toBeDisabled();
    await expect(admin.getByRole('button', { name: 'Delete', exact: true })).toBeDisabled();
    await expect(admin.getByRole('button', { name: 'Reset password' })).toBeDisabled();
  });

  // --- 8 ------------------------------------------------------------------------------------

  test('a PIN switch session cannot mint a credential', async ({ browser, baseURL }) => {
    // §3.2's PIN is "for fast user-switching on a shared device". The chain this closes is:
    // switch in with four digits, register a passkey, sign in with it — and a passkey session
    // is user-verified, so on an admin account it walks past the 24 h re-prompt.
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

    // The phone is handed over. A separate context, so the file's own admin page keeps its
    // session — signing in elsewhere replaces only the session the incoming cookie named.
    const handover = await fresh(browser, baseURL);
    await login(handover);
    const switched = await handover.request.post('/api/auth/switch', {
      data: { user_id: memberId, pin: '4821' }
    });
    expect(switched.ok()).toBeTruthy();

    const garbage = { id: 'x', rawId: 'x', type: 'public-key', response: {} };
    /** §3.2's credential routes, each given the strongest body the session could send. */
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

    // The admin surface is shut for this session too, but for being a member's rather than for
    // being a PIN's: `deps.admin_user` checks the role before it checks the re-prompt, so a
    // switch into a member never reaches the 401 — the switch below is the one that does, and
    // it is also the one the chain was aiming at.
    const byRole = await handover.request.get('/api/admin/users', { failOnStatusCode: false });
    expect(byRole.status(), 'a member session is refused for being a member').toBe(403);

    // Four digits again, into the ADMIN account. §3.2 lets any account set a PIN, so the
    // shared device can switch into the one that matters — and `create_session`'s CASE keeps
    // the admin stamp off a PIN session, so the surface answers §3.2's 24 h re-prompt instead
    // of opening. The passkey that would have walked past that re-prompt is refused above.
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
    // The header the shell reads to know it is a re-prompt rather than a sign-out.
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
    // §6.6: "passkey list with per-credential revoke". The credential has to be a real one,
    // because the gap this closes is exactly the gap between the two halves: the roster
    // carried a count, the revoke route took an id, and nothing in the app turned one into
    // the other — so the admin's only remedy for a lost phone was a password reset, which
    // §3.2 says leaves every passkey registered.
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

    // The count on the row head comes from the roster, re-read after the write, so this is the
    // server agreeing rather than the list crossing itself out.
    await expect(row.locator('[data-user-facts]')).toContainText('0 passkeys');
    await expect(row.locator('[data-empty="passkeys"]')).toBeVisible();

    // …and it was that account's credential, not a row the admin happened to be looking at.
    await device.goto('/account');
    await expect(device.locator('[data-empty="passkeys"]')).toBeVisible();
  });

  // --- 10 -----------------------------------------------------------------------------------

  test('the row editor links, re-links and unlinks Jellyfin', async () => {
    // §6.6 puts "Jellyfin re-link / unlink" in the row editor and plan step 17 wires the two
    // routes that already existed; the screen used to point at the Connectors tab instead,
    // which made the admin find the same person twice to act on what this row just told them.
    // 08-jellyfin.spec.js configured the connector and unlinked its account again, so both
    // fake users are free.
    const member = await createMember(admin, 'jellyfin-link');
    await admin.goto('/admin/users');
    const row = await openRow(member.name);
    const jellyfin = row.getByTestId('user-jellyfin');
    await jellyfin.getByRole('combobox').selectOption({ label: 'patrick' });
    await jellyfin.getByRole('button', { name: 'Link', exact: true }).click();

    // §7.3: a link with no sign-in attributes playback and feeds the P(seen) prior but cannot
    // write Played state, which is the "needs sign-in" the roster line reports.
    await expect(row.locator('[data-user-facts]')).toContainText('Jellyfin needs sign-in');

    await jellyfin.getByPlaceholder('jellyfin username').fill('patrick');
    await jellyfin.getByPlaceholder('password (once)').fill(JELLYFIN.password);
    await jellyfin.getByRole('button', { name: 'Re-link' }).click();
    await expect(row.locator('[data-user-facts]')).toContainText('Jellyfin linked');
    await expect(jellyfin.locator('[data-jellyfin="token"]')).toBeVisible();

    // §3.3: the map is optional, so the editor that makes it has to be able to unmake it.
    await jellyfin.getByRole('button', { name: 'Unlink' }).click();
    await expect(row.locator('[data-user-facts]')).toContainText('Jellyfin unlinked');
  });
});
