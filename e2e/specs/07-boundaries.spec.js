import { expect, test } from '@playwright/test';

import { ADMIN, signedIn } from '../helpers.js';

/** A request context with no cookies. The bare `request` fixture already is one, but making
 * it explicit keeps these tests honest about what they are proving. */
const anonymousContext = (playwright, baseURL) => playwright.request.newContext({ baseURL });

/**
 * Regressions for the boundaries an adversarial review found open. Each of these shipped once;
 * each is cheap to reopen. They are asserted at the HTTP layer because that is where they
 * failed — a UI test would not have caught any of them.
 */

test('the SPA fallback cannot be walked out of its directory', async ({ request }) => {
  // The fallback joined the raw URL path onto the static root with no containment and no auth.
  // `GET /..%2f..%2fdata/backups/dump.sql` returned the file.
  for (const attack of [
    '/../../etc/passwd',
    '/..%2f..%2f..%2fetc%2fpasswd',
    '/../../../proc/self/environ',
    '/..%5c..%5cwindows%5cwin.ini',
    '/static/../../.env',
  ]) {
    const res = await request.get(attack, { maxRedirects: 0 });
    const body = await res.text();
    expect(body, `${attack} must not return file contents`).not.toMatch(/root:x:|SECRETS_KEY|PATH=/);
    // Anything that is not a real asset falls back to the app shell.
    expect([200, 404]).toContain(res.status());
    if (res.status() === 200) expect(body).toContain('<!doctype html>');
  }
});

test('an unknown API route answers JSON 404, not the app shell', async ({ request }) => {
  // A client that gets HTML where it expected JSON fails somewhere much less obvious.
  const res = await request.get('/api/no-such-endpoint');
  expect(res.status()).toBe(404);
  expect(res.headers()['content-type']).toContain('application/json');
});

test('profile switching requires an existing session', async ({ playwright, baseURL }) => {
  // §3.2's PIN is a convenience for a device someone is already signed in on. Accepting one
  // from an anonymous caller would make a 4-digit secret the whole authentication story.
  const anonymous = await anonymousContext(playwright, baseURL);
  const res = await anonymous.post('/api/auth/switch', {
    data: { user_id: 1, pin: '1234' },
    failOnStatusCode: false,
  });
  expect(res.status()).toBe(401);
  await anonymous.dispose();
});

test('the household roster is not readable by an anonymous caller', async ({ playwright, baseURL }) => {
  const anonymous = await anonymousContext(playwright, baseURL);
  const res = await anonymous.get('/api/auth/switchable', { failOnStatusCode: false });
  expect(res.status()).toBe(401);
  await anonymous.dispose();
});

test('admin routes refuse a signed-out caller', async ({ playwright, baseURL }) => {
  const anonymous = await anonymousContext(playwright, baseURL);
  for (const path of ['/api/admin/bundle/state']) {
    const res = await anonymous.get(path, { failOnStatusCode: false });
    expect(res.status(), `${path} must not be public`).toBe(401);
  }
  await anonymous.dispose();
});

test('the bundle path cannot point outside the data directory', async ({ page }) => {
  // An admin-only route that takes a filesystem path still gets a boundary, so a typo cannot
  // make the app read arbitrary host files.
  await signedIn(page);
  const res = await page.request.post('/api/admin/bundle/validate', {
    data: { path: '/etc' },
    failOnStatusCode: false,
  });
  expect(res.status()).toBe(400);
  expect(await res.text()).toContain('must live under');
});

test('the session cookie is HttpOnly and same-site', async ({ page, context }) => {
  await signedIn(page);
  const cookie = (await context.cookies()).find((c) => c.name === 'spielplan_session');
  expect(cookie, 'a session cookie must exist after signing in').toBeTruthy();
  expect(cookie.httpOnly, 'JS must not be able to read the session').toBe(true);
  expect(cookie.sameSite).toBe('Lax');
  // §2 signs it, so rotating SESSION_SECRET invalidates sessions. A raw opaque id would not
  // survive that promise.
  expect(cookie.value).toContain('.');
});

test('a tampered session cookie is rejected', async ({ page, context }) => {
  await signedIn(page);
  const cookies = await context.cookies();
  const session = cookies.find((c) => c.name === 'spielplan_session');
  await context.clearCookies();
  await context.addCookies([{ ...session, value: session.value.replace(/^./, 'X') }]);

  const res = await page.request.get('/api/auth/me', { failOnStatusCode: false });
  expect(res.status()).toBe(401);
});

test('login does not reveal which names exist', async ({ playwright, baseURL }) => {
  const anonymous = await anonymousContext(playwright, baseURL);
  const unknown = await anonymous.post('/api/auth/login', {
    data: { name: 'nobody-here', password: 'wrong-password' },
    failOnStatusCode: false,
  });
  const known = await anonymous.post('/api/auth/login', {
    data: { name: ADMIN.name, password: 'wrong-password' },
    failOnStatusCode: false,
  });
  expect(unknown.status()).toBe(known.status());
  expect(await unknown.text()).toBe(await known.text());
  await anonymous.dispose();
});
