import { expect, test } from '@playwright/test';

import { ADMIN, openAccountMenu, signedIn } from '../helpers.js';

/**
 * §3.2 passkeys, round trip in a real browser. Chromium's virtual authenticator (CDP) simulates
 * only the biometric; `test_webauthn.py` covers the refusals. Chromium only. Must run against
 * PUBLIC_URL's origin, to which WebAuthn binds the credential.
 */
test.describe.configure({ mode: 'serial' });

test.describe('passkeys', () => {
  let page;
  let cdp;
  let secondAuthenticator;

  test.beforeAll(async ({ browser, browserName }) => {
    test.skip(browserName !== 'chromium', 'the virtual authenticator is a Chromium feature');
    page = await browser.newPage();
    cdp = await page.context().newCDPSession(page);
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
    await signedIn(page);
  });

  test.afterAll(async () => {
    await page?.close();
  });

  test('the account page starts with no passkey and says so', async () => {
    await page.goto('/account');
    await expect(page.getByRole('heading', { name: 'Account' })).toBeVisible();
    await expect(page.locator('[data-empty="passkeys"]')).toBeVisible();
  });

  test('a passkey registers from the profile page', async () => {
    // §3.2: "Registration from the profile page".
    await page.goto('/account');
    await page.getByPlaceholder('Name this device (optional)').fill('e2e-laptop');
    await page.getByRole('button', { name: 'Add a passkey' }).click();

    await expect(page.getByText('Passkey registered.')).toBeVisible();
    await expect(page.getByText('e2e-laptop')).toBeVisible();
    await expect(page.locator('[data-empty="passkeys"]')).toHaveCount(0);
  });

  test('the credential is bound to this origin', async () => {
    // §14.4: the rp_id is shown, so a PUBLIC_URL change explains itself on the screen.
    await page.goto('/account');
    const rpId = new URL(page.url()).hostname;
    await expect(page.locator('.list li').first()).toContainText(rpId);
  });

  test('a second passkey can be registered for the same account', async () => {
    // §3.2: "multiple passkeys per user". A second authenticator: the first is excluded.
    ({ authenticatorId: secondAuthenticator } = await cdp.send(
      'WebAuthn.addVirtualAuthenticator',
      {
        options: {
          protocol: 'ctap2',
          transport: 'usb',
          hasResidentKey: true,
          hasUserVerification: true,
          isUserVerified: true,
          automaticPresenceSimulation: true
        }
      }
    ));
    await page.goto('/account');
    await page.getByPlaceholder('Name this device (optional)').fill('e2e-key');
    await page.getByRole('button', { name: 'Add a passkey' }).click();
    await expect(page.getByText('e2e-key')).toBeVisible();
    await expect(page.locator('.list li')).toHaveCount(2);
  });

  test('signing out and back in with the passkey works, with no password typed', async () => {
    const menu = await openAccountMenu(page);
    await menu.getByRole('button', { name: 'Log out' }).click();
    await expect(page).toHaveURL(/\/login/);

    await page.getByRole('button', { name: 'Sign in with a passkey' }).click();
    await expect(
      page.getByTestId('home-greeting')
    ).toBeVisible();
    await expect(page.locator('.chip')).toContainText(ADMIN.name);
  });

  test('the session reports that it was authenticated by passkey', async () => {
    const me = await (await page.request.get('/api/auth/me')).json();
    expect(me.auth_method).toBe('passkey');
    expect(me.passkeys).toBe(2);
  });

  test('logout leaves the passkeys registered', async () => {
    // §3.2: "Logout clears the session cookie only — passkeys remain registered."
    const menu = await openAccountMenu(page);
    await menu.getByRole('button', { name: 'Log out' }).click();
    await expect(page).toHaveURL(/\/login/);

    await page.getByRole('button', { name: 'Sign in with a passkey' }).click();
    await expect(
      page.getByTestId('home-greeting')
    ).toBeVisible();

    await page.goto('/account');
    await expect(page.locator('.list li')).toHaveCount(2);
  });

  test('a removed passkey is gone and the other still signs in', async () => {
    await page.goto('/account');
    await page.locator('.list li', { hasText: 'e2e-key' }).getByRole('button', {
      name: 'Remove'
    }).click();
    await expect(page.locator('.list li')).toHaveCount(1);
    await expect(page.getByText('e2e-key')).toHaveCount(0);

    // The device is gone, so its authenticator goes too; else sign-in might offer the forgotten key.
    await cdp.send('WebAuthn.removeVirtualAuthenticator', {
      authenticatorId: secondAuthenticator
    });

    const menu = await openAccountMenu(page);
    await menu.getByRole('button', { name: 'Log out' }).click();
    await page.getByRole('button', { name: 'Sign in with a passkey' }).click();
    await expect(
      page.getByTestId('home-greeting')
    ).toBeVisible();
  });

  test('the password fallback is never hidden behind the passkey button', async () => {
    // §3.2: password login is "always available".
    const menu = await openAccountMenu(page);
    await menu.getByRole('button', { name: 'Log out' }).click();
    await expect(page.getByRole('button', { name: 'Sign in with a passkey' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
    await expect(page.locator('input[type=password]')).toBeVisible();
    await signedIn(page);
  });

  test('a new member is prompted to add a passkey right after the forced change', async ({
    browser
  }) => {
    // §3.1: "a one-time password is issued, the account is locked to a password change at
    // first login, and passkey registration is prompted afterwards."
    const name = `e2e-member-${Date.now()}`;
    const created = await page.request.post('/api/admin/users', {
      data: { name, role: 'member' }
    });
    expect(created.ok()).toBeTruthy();
    const otp = (await created.json()).one_time_password;

    const member = await browser.newPage();
    try {
      await member.goto('/login');
      await member.locator('input[type=text]').first().fill(name);
      await member.locator('input[type=password]').fill(otp);
      await member.getByRole('button', { name: 'Sign in', exact: true }).click();

      // Locked to the change, and no way past it (§3.1 — enforced server-side).
      await expect(member).toHaveURL(/\/account\/password/);
      await member.locator('input[type=password]').nth(0).fill(otp);
      await member.locator('input[type=password]').nth(1).fill('a-real-member-password');
      await member.locator('input[type=password]').nth(2).fill('a-real-member-password');
      await member.getByRole('button', { name: /Set password|Save|Continue/ }).click();

      await expect(member).toHaveURL(/\/account\?welcome=1/);
      await expect(member.locator('[data-passkey-prompt]')).toBeVisible();
      await expect(member.getByRole('button', { name: 'Add a passkey' })).toBeVisible();
    } finally {
      await member.close();
    }
  });
});
