import { expect, test } from '@playwright/test';

import { createMember, signedIn, signInAsMember } from '../helpers.js';

/**
 * §6 preamble: "responsive PWA, phone-first (48 px targets, one-handed, swipe), desktop as
 * progressive enhancement, installable, service-worker shell cache."
 *
 * Runs on both projects; assertions branch on which, because both must be correct.
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

  // `design.css`'s `--touch: 48px`.
  const nav = page.getByRole('navigation', { name: 'Surfaces' });
  for (const link of await nav.getByRole('link').all()) {
    const box = await link.boundingBox();
    expect(box.height, 'nav targets are at least --touch (48px) tall').toBeGreaterThanOrEqual(48);
    expect(box.width, 'nav targets are at least --touch (48px) wide').toBeGreaterThanOrEqual(48);
  }

  // Both dimensions: `design.css`'s coarse block raises `min-height` and never `min-width`.
  for (const control of await page.getByRole('group', { name: 'Kind' }).getByRole('button').all()) {
    const box = await control.boundingBox();
    const name = (await control.textContent())?.trim();
    const floor = `the ${name} kind toggle is at least --touch (48px)`;
    expect(box.height, `${floor} tall`).toBeGreaterThanOrEqual(48);
    expect(box.width, `${floor} wide`).toBeGreaterThanOrEqual(48);
  }
});

test('the bottom bar sits inside the visible viewport, not the large one', async ({
  page,
  isMobile
}) => {
  test.skip(!isMobile, 'the desktop rail is a column down the side and has no toolbar over it');

  // On iOS Safari `100vh` is the large viewport, under the toolbar. Playwright has no toolbar, so
  // `100vh` and `100dvh` agree here: this holds only the geometry, at rest and scrolled
  // (the device fact is a manual check in docs/TESTING.md, decision 281).
  const nav = page.getByRole('navigation', { name: 'Surfaces' });
  await expect(nav).toBeVisible();

  const measure = async (when) => {
    const box = await nav.boundingBox();
    const inner = await page.evaluate(() => window.innerHeight);
    expect(
      Math.round(box.y + box.height),
      `${when}, the tab bar's bottom edge is below the visible viewport (${inner}px)`
    ).toBeLessThanOrEqual(inner + 1);
  };

  await measure('at rest');

  // Whichever element takes the gesture, the bar must not travel with it.
  await page.evaluate(() => {
    window.scrollTo(0, document.body.scrollHeight);
    for (const el of document.querySelectorAll('main, .shell, .body')) {
      el.scrollTop = el.scrollHeight;
    }
  });
  await measure('with the surface scrolled to the end');
});

test('the header grows by the status-bar inset when one is reported', async ({
  page,
  browserName
}) => {
  test.skip(browserName !== 'chromium', 'Emulation.* is CDP, and WebKit has no CDP');

  // Decision 279: the header reserves `env(safe-area-inset-top)` in its padding and its height.
  // Chromium's `Emulation.setSafeAreaInsetsOverride` injects the inset: the arithmetic is
  // measured here, not the device fact (decisions 281, 284).
  const header = page.locator('.shell > header');
  await expect(header).toBeVisible();

  const before = await header.boundingBox();
  const padding = () => header.evaluate((el) => getComputedStyle(el).paddingTop);
  expect(await padding(), 'no engine here reports an inset until one is asked for').toBe('0px');

  const INSET = 47; // iPhone 13, portrait. 59 from the 14 Pro on.
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Emulation.setSafeAreaInsetsOverride', { insets: { top: INSET } });
  await expect
    .poll(padding, { message: 'the header does not pad itself by env(safe-area-inset-top)' })
    .toBe(`${INSET}px`);

  const after = await header.boundingBox();
  expect(
    Math.round(after.height - before.height),
    'the header pads by the inset but does not GROW by it, so the row below it moves up under ' +
      'the status bar'
  ).toBe(INSET);

  const brand = await page.locator('.shell > header .brand').boundingBox();
  expect(
    brand.y,
    'the header reserved the inset and drew its content inside it anyway'
  ).toBeGreaterThanOrEqual(INSET);

  // And at phone width: the phone header's `height: auto` discards the base rule's `calc()`, so
  // the inset is repeated there on min-height.
  await page.setViewportSize({ width: 390, height: 844 });
  // Re-sent: whether `setViewportSize` clears a safe-area override is not a Playwright contract.
  // With the bottom inset, which `NavRail.svelte`'s `env(safe-area-inset-bottom)` reserves.
  const BOTTOM = 34; // iPhone 13, portrait: the home indicator.
  await cdp.send('Emulation.setSafeAreaInsetsOverride', {
    insets: { top: INSET, bottom: BOTTOM }
  });
  await expect
    .poll(() => header.evaluate((el) => getComputedStyle(el).minHeight), {
      message:
        'the phone header does not repeat the inset on min-height: `height: auto` discards the ' +
        'base rule calc(), so the whole 54 px row sits under the status bar (decision 279)'
    })
    .toBe(`${54 + INSET}px`);
  expect(
    await padding(),
    'the phone block dropped the padding the base rule reserves'
  ).toBe(`${INSET}px`);
  const wrapped = await header.boundingBox();
  expect(
    Math.round(wrapped.height),
    'the phone header is shorter than 54 px plus the inset it pads by'
  ).toBeGreaterThanOrEqual(54 + INSET);
  const phoneBrand = await page.locator('.shell > header .brand').boundingBox();
  expect(
    phoneBrand.y,
    'the phone header reserved the inset and drew the wordmark inside it anyway'
  ).toBeGreaterThanOrEqual(INSET);

  const bar = page.getByRole('navigation', { name: 'Surfaces' });
  await expect
    .poll(() => bar.evaluate((el) => getComputedStyle(el).paddingBottom), {
      message:
        'the bottom bar does not reserve env(safe-area-inset-bottom), so its lowest 34 px sit ' +
        'under the home indicator on every iPhone in portrait'
    })
    .toBe(`${BOTTOM}px`);
  const inner = await page.evaluate(() => window.innerHeight);
  const link = await bar.locator('a').last().boundingBox();
  expect(
    Math.round(link.y + link.height),
    'the bar padded itself by the home indicator and drew its last link inside it anyway'
  ).toBeLessThanOrEqual(inner - BOTTOM + 1);
});

test('the page never scrolls sideways', async ({ page }) => {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test('the document never scrolls under the shell', async ({ page }) => {
  // `main` is the scroller; an absolutely placed marker once escaped it (decision 516).
  await expect(page.getByTestId('home-mode')).toHaveCount(1);
  const overflow = await page.evaluate(() => ({
    down: document.documentElement.scrollHeight - document.documentElement.clientHeight,
    across: document.documentElement.scrollWidth - document.documentElement.clientWidth
  }));
  expect(overflow.down, 'the document scrolls under the shell').toBeLessThanOrEqual(1);
  expect(overflow.across, 'the document scrolls sideways').toBeLessThanOrEqual(1);
});

test('a phone opens Home on the shelves, with the filters behind one control', async ({
  page,
  isMobile
}) => {
  // Decision 516: the kind switch and search stay; the other filters wait behind Filters.
  test.skip(!isMobile, 'the fold is a phone measurement');
  await expect(page.getByRole('group', { name: 'Kind' })).toBeVisible();
  await expect(page.getByTestId('home-search')).toBeVisible();
  for (const id of ['filter-genre', 'filter-decade', 'filter-seen', 'filter-owned']) {
    await expect(page.getByTestId(id), `${id} is on the first screen`).toHaveCount(0);
  }
  const toggle = page.getByTestId('filter-toggle');
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  // Room for a shelf under the count line on the first screen, above the 61 px bottom bar.
  const count = await page.getByTestId('count-line').boundingBox();
  expect(count.y + count.height).toBeLessThan(page.viewportSize().height - 160);
  await toggle.click();
  await expect(page.getByTestId('filter-genre')).toBeVisible();
});

test('a long genre option does not widen the page', async ({ page }) => {
  // A native select is as wide as its longest option; the fixture's genres are all short, so
  // this adds a long one.
  await page.getByTestId('filter-toggle').click();
  const genre = page.getByTestId('filter-genre');
  await expect(genre).toBeVisible();
  await genre.evaluate((el) => {
    const option = document.createElement('option');
    option.textContent = "horror based on children's characters, a long one";
    el.appendChild(option);
  });
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow, 'a long genre option pushed the page wider than the screen').toBeLessThanOrEqual(1);
  const box = await genre.boundingBox();
  expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize().width + 1);
});

test('the title detail panel is full-width on a phone', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'desktop shows it as a side panel');

  await page.locator('.card-wrap').first().click();
  const panel = page.getByLabel('Title detail');
  await expect(panel).toBeVisible();
  const box = await panel.boundingBox();
  expect(box.width).toBeGreaterThan(page.viewportSize().width * 0.95);
});

test('on a phone every Rate control is on screen, and a battle keeps Tie and its toggle in reach', async ({
  page,
  isMobile
}) => {
  // §6.1's "persistent Undo" and the decisive switch (decision 520) must be on the screen, not
  // past a hidden scrollbar or below the fold.
  test.skip(!isMobile, 'the wrap and the fold are the phone layout');

  // Three films liked so a pair exists. Not the admin: 19-phone-shell reads the admin's queue.
  await signInAsMember(page, await createMember(page, 'rate-phone'));
  await page.request.post('/api/rate/session', {
    data: { restart: true, mode: 'sweep', kinds: ['movie'] }
  });
  for (let i = 0; i < 3; i++) {
    const { card } = await (await page.request.get('/api/rate')).json();
    if (card?.type !== 'sweep') break;
    const res = await page.request.post('/api/rate/verdict', {
      data: { card_token: card.token, value: 2 }
    });
    expect(res.ok(), 'seeding a liked film (§6.1)').toBeTruthy();
  }
  await page.request.post('/api/rate/session', {
    data: { restart: true, kinds: ['movie', 'series'] }
  });

  await page.goto('/rate');
  await expect(page.getByTestId('rate-sweep-card')).toBeVisible();
  for (const id of [
    'rate-mode-mix', 'rate-mode-sweep', 'rate-mode-battle', 'rate-find-toggle',
    'rate-kind-movie', 'rate-kind-series', 'rate-undo', 'rate-verdict-0', 'rate-verdict-2'
  ]) {
    await expect(page.getByTestId(id), `${id} is on the screen`).toBeInViewport();
  }
  const sideways = await page
    .getByTestId('rate-surface')
    .locator('.controls')
    .evaluate((row) => row.scrollWidth - row.clientWidth);
  expect(sideways, 'the control row does not scroll sideways').toBeLessThanOrEqual(0);

  await page.request.post('/api/rate/session', { data: { mode: 'battle' } });
  await page.goto('/rate');
  await expect(page.getByTestId('rate-battle-card')).toBeVisible();
  for (const id of ['rate-strip-tie', 'rate-decisive', 'rate-battle-skip']) {
    await expect(page.getByTestId(id), `${id} is on the screen`).toBeInViewport();
  }
});

test('the app is installable: a manifest, an icon, and a theme colour', async ({ page }) => {
  // On iOS, Web Push works only for a PWA added to the home screen.
  const href = await page.locator('link[rel=manifest]').getAttribute('href');
  expect(href).toBeTruthy();

  const manifest = await (await page.request.get(href)).json();
  expect(manifest.display).toBe('standalone');
  expect(manifest.start_url).toBe('/');
  expect(manifest.icons.length).toBeGreaterThan(0);

  const icon = await page.request.get(manifest.icons[0].src);
  expect(icon.ok(), 'the manifest must not point at a missing icon').toBeTruthy();
  await expect(page.locator('meta[name=theme-color]')).toHaveAttribute('content', '#0d0d0f');

  // `app.html`'s metas, which no static guard reads. The CDP test above injects past the
  // viewport-fit gate, so only this sees it removed.
  const metas = await page.evaluate(() =>
    Object.fromEntries([...document.querySelectorAll('meta[name]')].map((m) => [m.name, m.content]))
  );
  expect(
    metas.viewport,
    'without viewport-fit=cover every env(safe-area-inset-*) in the app resolves to 0 in the ' +
      'installed web view, and the header stops reserving the status bar (decision 279)'
  ).toContain('viewport-fit=cover');
  expect(
    metas.viewport,
    'a scale lock suppresses focus zoom by taking pinch zoom away from everybody, which is not ' +
      'the repair: 16 px type is (decision 279)'
  ).not.toMatch(/user-scalable|maximum-scale/);
  expect(
    metas['apple-mobile-web-app-capable'],
    'without this, Add to Home Screen opens a Safari tab below iOS 16.4 and there is no ' +
      'standalone view for three of the four owed device checks to be performed in'
  ).toBe('yes');
  expect(
    metas['apple-mobile-web-app-status-bar-style'],
    'this is the premise of the whole header inset: it is what puts the web view under the ' +
      'status bar in the first place'
  ).toBe('black-translucent');
});

test('fonts are self-hosted, not fetched from a third party', async ({ page }) => {
  // The app renders on the LAN and over Tailscale with no route to the internet.
  const external = await page.evaluate(() =>
    [...document.querySelectorAll('link[rel=stylesheet], link[rel=preload], link[rel=preconnect]')]
      .map((l) => l.href)
      .filter((h) => h && new URL(h, location.href).origin !== location.origin)
  );
  expect(external, 'no stylesheet or font may come from another origin').toEqual([]);
});
