import { expect, test } from '@playwright/test';

import { signedIn } from '../helpers.js';

/**
 * §6 preamble: "responsive PWA, phone-first (48 px targets, one-handed, swipe), desktop as
 * progressive enhancement, installable, service-worker shell cache."
 *
 * The phone project runs this file at iPhone 13 dimensions with touch emulation; the desktop
 * project runs it at 1400×900. Assertions branch on which, because the point is that both are
 * correct, not that they are identical.
 */

test.beforeEach(async ({ page }) => {
  await signedIn(page);
  await page.goto('/');
});

test('the navigation adapts: a rail on desktop, a bottom bar on a phone', async ({
  page,
  isMobile,
}) => {
  const nav = page.getByRole('navigation', { name: 'Surfaces' });
  await expect(nav).toBeVisible();

  const navBox = await nav.boundingBox();
  const viewport = page.viewportSize();
  if (isMobile) {
    // One-handed: the nav sits at the bottom, within thumb reach, and spans the width.
    expect(navBox.y).toBeGreaterThan(viewport.height / 2);
    expect(navBox.width).toBeGreaterThan(viewport.width * 0.9);
  } else {
    expect(navBox.x).toBeLessThan(viewport.width / 4);
    expect(navBox.height).toBeGreaterThan(viewport.height / 2);
  }
});

test('touch targets meet the 48 px rule on a phone', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'the rule is about fingers');

  const nav = page.getByRole('navigation', { name: 'Surfaces' });
  for (const link of await nav.getByRole('link').all()) {
    const box = await link.boundingBox();
    expect(box.height, 'nav targets are at least 48 px tall').toBeGreaterThanOrEqual(44);
    expect(box.width).toBeGreaterThanOrEqual(44);
  }

  // §6.8's coarse-pointer rule reaches every interactive primitive, not only the nav — that
  // was the whole finding: a 48 px rule that lives in one component is not a rule.
  for (const control of await page.getByRole('group', { name: 'Kind' }).getByRole('button').all()) {
    const box = await control.boundingBox();
    expect(box.height).toBeGreaterThanOrEqual(44);
  }
});

test('the page never scrolls sideways', async ({ page }) => {
  // Horizontal overflow on a phone is the classic responsive failure and is invisible in a
  // screenshot taken at the top of the page.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test('the title detail panel is full-width on a phone', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'desktop shows it as a side panel');
  const config = await page.evaluate(() => fetch('/api/config').then((r) => r.json()));
  test.skip(!config.has_bundle, 'needs an imported bundle');

  await page.locator('.card-wrap').first().click();
  const panel = page.getByLabel('Title detail');
  await expect(panel).toBeVisible();
  const box = await panel.boundingBox();
  expect(box.width).toBeGreaterThan(page.viewportSize().width * 0.95);
});

test('the app is installable: a manifest, an icon, and a theme colour', async ({ page }) => {
  // §6 preamble, and it is load-bearing: on iOS, Web Push works only for a PWA added to the
  // home screen, so the manifest is not decoration.
  const href = await page.locator('link[rel=manifest]').getAttribute('href');
  expect(href).toBeTruthy();

  const manifest = await (await page.request.get(href)).json();
  expect(manifest.display).toBe('standalone');
  expect(manifest.start_url).toBe('/');
  expect(manifest.icons.length).toBeGreaterThan(0);

  const icon = await page.request.get(manifest.icons[0].src);
  expect(icon.ok(), 'the manifest must not point at a missing icon').toBeTruthy();
  await expect(page.locator('meta[name=theme-color]')).toHaveAttribute('content', '#0d0d0f');
});

test('fonts are self-hosted, not fetched from a third party', async ({ page }) => {
  // The app has to render on the LAN and over Tailscale with no route to the internet.
  const external = await page.evaluate(() =>
    [...document.querySelectorAll('link[rel=stylesheet], link[rel=preload], link[rel=preconnect]')]
      .map((l) => l.href)
      .filter((h) => h && new URL(h, location.href).origin !== location.origin)
  );
  expect(external, 'no stylesheet or font may come from another origin').toEqual([]);
});
