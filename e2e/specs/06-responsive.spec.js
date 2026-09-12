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

  // 48, not 44. `design.css` defines `--touch: 48px` and no 44-47 px value exists anywhere in
  // `frontend/src`, so a title promising 48 that asserted 44 admitted every control this
  // milestone had to find: the number was the one thing the test could not be wrong about and
  // was. The token is named in each message because a bare "44" in a failure sends the reader
  // looking for a constant that is not there.
  // [tq4-48px-rule-asserted-at-44-against-a-48px-token]
  const nav = page.getByRole('navigation', { name: 'Surfaces' });
  for (const link of await nav.getByRole('link').all()) {
    const box = await link.boundingBox();
    expect(box.height, 'nav targets are at least --touch (48px) tall').toBeGreaterThanOrEqual(48);
    expect(box.width, 'nav targets are at least --touch (48px) wide').toBeGreaterThanOrEqual(48);
  }

  // §6.8's coarse-pointer rule reaches every interactive primitive, not only the nav — that
  // was the whole finding: a 48 px rule that lives in one component is not a rule.
  //
  // BOTH dimensions here too, which the second half of this loop never asked for. `design.css`'s
  // coarse block raises `min-height` and never `min-width`, so a control 48 px tall and 32 px
  // wide satisfied every 48 px assertion the suite had; that is exactly the shape the two
  // overlays' close buttons shipped in, and a height-only sweep is how they shipped.
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

  // §6 preamble: "phone-first (48 px targets, one-handed, swipe)". One-handed means the surface
  // switcher is under the thumb, and on iOS Safari a shell sized to `100vh` puts it under the
  // browser's own toolbar instead: `100vh` is the LARGE viewport (745 px on an iPhone 13) while
  // 664 px are visible with the toolbar expanded, and because the document is then exactly the
  // layout viewport nothing overflows, so Safari never collapses the toolbar and reveals it.
  // Every member's first session happens in a tab, because §3.1 puts the install after the
  // sign-in.
  //
  // WHAT THIS TEST IS, HONESTLY. Playwright's viewport IS the visible viewport by construction:
  // there is no browser toolbar in it, so `100vh` and `100dvh` resolve to the same number here
  // and a shell that dropped the `100dvh` line would still pass. That is precisely why
  // `06-responsive` was green while the bar was covered on a real phone. The rule itself is
  // asserted where it can be - at the source, by
  // `test_static_contracts.py::test_the_shell_uses_the_dynamic_viewport` - and the device fact
  // is owed as an unsigned check in `docs/TESTING.md` (decision 281). What this adds is the
  // geometry that engine CAN see and that no assertion held: the bar is inside the viewport
  // rather than below its fold, and it stays there when the surface behind it is scrolled,
  // which is the half a fixed-position mistake would break here as well as on the phone.
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

  // Scroll everything that can scroll. The shell is a column of its own height with the surface
  // scrolling inside it, so the window may not move at all - which is the point: whichever one
  // takes the gesture, the bar must not travel with it.
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
  // CDP, so Chromium only - the same shape `09-passkeys` uses for the virtual authenticator.
  test.skip(browserName !== 'chromium', 'Emulation.* is CDP, and WebKit has no CDP');

  // §6 preamble: installable. `app.html` sets `viewport-fit=cover` and a translucent status bar,
  // so the installed web view starts at the physical top edge and the header has to reserve
  // `env(safe-area-inset-top)` in both its padding and its height (decision 279).
  //
  // THE PREMISE THIS TEST FALSIFIES. Three places in this repository said env() cannot be
  // exercised in any engine the suite runs, so the rule was asserted at the source and nowhere
  // else. It can be: Chromium exposes `Emulation.setSafeAreaInsetsOverride`, which overrides the
  // values `env(safe-area-inset-*)` resolves to, and under it the two header rules COMPOSE - the
  // padding, the height, the phone block's min-height and the cascade between them - which is
  // the half a text guard cannot read. A `box-sizing` regression, a later `padding-top: 0`, a
  // `min-height: unset`: none of those writes the literal the static sweep looks for, and all
  // three are visible here.
  //
  // What it still does NOT measure, and why `docs/TESTING.md` keeps its owed device check
  // unsigned: this is desktop Chromium with a number injected. There is no installed standalone
  // web view, no real inset, no rotation, and no iOS WebKit. The ARITHMETIC is measured; the
  // DEVICE FACT is not, and a green run here is not evidence for it (decisions 281, 284).
  // [§6 preamble; decision 279; M4.15 review cycle 1: M415-C1-COV-02]
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

  // The content, not only the box: the wordmark is the leftmost thing in that row, and the
  // account chip beside it is a 32 px control centred in it.
  const brand = await page.locator('.shell > header .brand').boundingBox();
  expect(
    brand.y,
    'the header reserved the inset and drew its content inside it anyway'
  ).toBeGreaterThanOrEqual(INSET);

  // AND THE HALF DECISION 279 IS ACTUALLY ABOUT, which this test claimed and did not measure.
  // The inset is written in TWO places because M4.9 gave the phone header `height: auto` so four
  // badges can wrap, which silently discards the base rule's `calc()` on exactly the form factor
  // the inset exists for. Everything above runs at the project's 1400 px, where
  // `@media (max-width: 720px)` is inert - so a `padding-top: 0` or a `min-height: unset` added
  // to the phone block would pass here, and the static guard reads one property inside that block
  // and would pass too. Same Chromium page, resized: no new test, no new project, and the
  // override is what decides the box.
  await page.setViewportSize({ width: 390, height: 844 });
  // Re-sent rather than assumed: `setViewportSize` issues its own device-metrics override, and
  // whether that clears a safe-area override is not a contract Playwright states either way.
  //
  // And the BOTTOM inset with it, because the same instrument takes it and one line of CSS in the
  // whole tree depends on it. `NavRail.svelte`'s phone block reserves
  // `max(6px, env(safe-area-inset-bottom))`, and until this it was held by nothing at any layer:
  // the static guard beside this test is scoped to `env(safe-area-inset-top)` by an explicit
  // comment naming the bottom one as the near miss it must REJECT; the 48 px sweep measures link
  // boxes, which a container's padding does not move; and `the bottom bar sits inside the visible
  // viewport` asserts the nav's own box against innerHeight, which holds identically at 6 px and
  // at 34. Delete the line and every iPhone in portrait draws the bottom 34 px of six primary
  // targets under the home indicator with the whole gate green - the same failure as the header's,
  // on the same component, in the same milestone's exclusive file.
  // [§6 preamble; review cycle 3: M415-C3-E2E-02]
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
  // The content, not only the box: the six links are what a thumb aims at, and reserving the
  // inset is only worth something if it lifts them out of it.
  const inner = await page.evaluate(() => window.innerHeight);
  const link = await bar.locator('a').last().boundingBox();
  expect(
    Math.round(link.y + link.height),
    'the bar padded itself by the home indicator and drew its last link inside it anyway'
  ).toBeLessThanOrEqual(inner - BOTTOM + 1);
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

  // AND THE THREE METAS THIS MILESTONE ARGUES FROM, which nothing in this repository read.
  // `app.html` is one of M4.15's six exclusive files and no static guard CAN see it:
  // `_frontend_sources()` filters to .css/.js/.svelte, so the .html is outside every sweep by
  // construction, and the only mentions of it anywhere in backend/tests or e2e are prose. They
  // land on this test rather than on a new one because installability is what makes them matter,
  // and this test already reads the live document on both projects.
  //
  // What was unheld: delete the twenty characters `, viewport-fit=cover` and the installed web
  // view still starts at the physical top edge - that is what `black-translucent` does - while
  // `env(safe-area-inset-top)` resolves to 0, so the header's padding and its `calc()` both
  // collapse and the wordmark sits under the clock, which is decision 279's whole defect. Nothing
  // would have gone red, the CDP test above least of all: `Emulation.setSafeAreaInsetsOverride`
  // writes what `env()` resolves to, below the viewport-fit gate that produces it in production.
  // Delete `apple-mobile-web-app-capable` and Add to Home Screen opens a Safari tab on iOS below
  // 16.4, which retires three of the four owed device checks as unperformable while
  // `manifest.display === 'standalone'` keeps the assertions above green. And the inverse is
  // unheld too: `maximum-scale=1` would stop Safari's focus zoom the wrong way, satisfy the owed
  // check by taking pinch zoom from everybody, and redden nothing.
  // [§6 preamble; decisions 279, 281; review cycle 3: M415-C3-E2E-01]
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
  // The app has to render on the LAN and over Tailscale with no route to the internet.
  const external = await page.evaluate(() =>
    [...document.querySelectorAll('link[rel=stylesheet], link[rel=preload], link[rel=preconnect]')]
      .map((l) => l.href)
      .filter((h) => h && new URL(h, location.href).origin !== location.origin)
  );
  expect(external, 'no stylesheet or font may come from another origin').toEqual([]);
});
