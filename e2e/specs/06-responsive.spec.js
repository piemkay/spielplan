import { expect, test } from '@playwright/test';

import { signedIn } from '../helpers.js';

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
  const nav = page.getByRole('navigation', { name: 'Main' });
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
  const nav = page.getByRole('navigation', { name: 'Main' });
  for (const link of await nav.getByRole('link').all()) {
    const box = await link.boundingBox();
    expect(box.height, 'nav targets are at least --touch (48px) tall').toBeGreaterThanOrEqual(48);
    expect(box.width, 'nav targets are at least --touch (48px) wide').toBeGreaterThanOrEqual(48);
  }

  // The segmented control draws 36 px and extends its hit area (decision 527): measure where a
  // finger lands, not the box that is drawn.
  for (const control of await page.getByRole('group', { name: 'Kind' }).getByRole('button').all()) {
    const hit = await hitArea(control);
    const name = (await control.textContent())?.trim();
    const floor = `the ${name} kind toggle takes a tap across at least --touch (48px)`;
    expect(hit.height, `${floor} of height`).toBeGreaterThanOrEqual(48);
    expect(hit.width, `${floor} of width`).toBeGreaterThanOrEqual(48);
  }
});

/** How far from its centre a tap still lands on the control, vertically and horizontally. */
async function hitArea(locator) {
  return locator.evaluate((el) => {
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const lands = (x, y) => {
      const at = document.elementFromPoint(x, y);
      return !!at && (at === el || el.contains(at));
    };
    const span = (step) => {
      let lo = 0;
      let hi = 0;
      while (lo < 60 && step(-(lo + 1))) lo += 1;
      while (hi < 60 && step(hi + 1)) hi += 1;
      return lo + hi + 1;
    };
    return {
      height: span((d) => lands(cx, cy + d)),
      width: span((d) => lands(cx + d, cy))
    };
  });
}

test('the bottom bar sits inside the visible viewport, not the large one', async ({
  page,
  isMobile
}) => {
  test.skip(!isMobile, 'the desktop rail is a column down the side and has no toolbar over it');

  // On iOS Safari `100vh` is the large viewport, under the toolbar. Playwright has no toolbar, so
  // `100vh` and `100dvh` agree here: this holds only the geometry, at rest and scrolled
  // (the device fact is a manual check in docs/TESTING.md, decision 281).
  const nav = page.getByRole('navigation', { name: 'Main' });
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

test('the top row grows by the status-bar inset when one is reported', async ({
  page,
  browserName
}) => {
  test.skip(browserName !== 'chromium', 'Emulation.* is CDP, and WebKit has no CDP');

  // Decision 279: the top row reserves `env(safe-area-inset-top)` in its padding and its height.
  // Chromium's `Emulation.setSafeAreaInsetsOverride` injects the inset: the arithmetic is
  // measured here, not the device fact (decisions 281, 284).
  const header = page.locator('header.topbar');
  await expect(header).toBeVisible();

  const padding = () => header.evaluate((el) => getComputedStyle(el).paddingTop);
  const INSET = 47; // iPhone 13, portrait. 59 from the 14 Pro on.
  const BOTTOM = 34; // iPhone 13, portrait: the home indicator.
  await page.setViewportSize({ width: 390, height: 844 });
  const before = await header.boundingBox();
  expect(await padding(), 'no engine here reports an inset until one is asked for').toBe('0px');

  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Emulation.setSafeAreaInsetsOverride', {
    insets: { top: INSET, bottom: BOTTOM }
  });
  await expect
    .poll(padding, { message: 'the top row does not pad itself by env(safe-area-inset-top)' })
    .toBe(`${INSET}px`);

  const after = await header.boundingBox();
  expect(
    Math.round(after.height - before.height),
    'the top row pads by the inset but does not GROW by it, so the page moves up under the status bar'
  ).toBe(INSET);

  const avatar = await page.getByTestId('account-chip').boundingBox();
  expect(avatar.y, 'the top row reserved the inset and drew You inside it anyway').toBeGreaterThanOrEqual(
    INSET
  );

  const bar = page.getByRole('navigation', { name: 'Main' });
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

test('the document is what scrolls, never an inner box', async ({ page }) => {
  // Decision 527: tap-to-top, the toolbar's collapse and rubber-banding all act on the document.
  await expect(page.getByTestId('home-mode')).toHaveCount(1);
  const scroll = await page.evaluate(() => ({
    main: getComputedStyle(document.querySelector('main')).overflowY,
    across: document.documentElement.scrollWidth - document.documentElement.clientWidth
  }));
  expect(scroll.main, 'main is a scroll box again').toBe('visible');
  expect(scroll.across, 'the document scrolls sideways').toBeLessThanOrEqual(1);
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
  // Room for a shelf under the search on the first screen, above the 61 px bottom bar.
  const search = await page.getByTestId('home-search').boundingBox();
  expect(search.y + search.height).toBeLessThan(page.viewportSize().height - 160);
  await toggle.click();
  await expect(page.getByTestId('filter-genre')).toBeVisible();
});

test('Home on a desktop is one width, as Rank is', async ({ page, isMobile }) => {
  // Decision 554: from 721 px the search joins the top row, every block in the flow spans the
  // content width, and the grid's posters are the shelves' size. No x is pressed here: a sticky
  // one would hide its notice for the rest of the day (section 8's state hazard).
  test.skip(isMobile, 'a phone keeps the search under the top row');
  for (const viewport of [{ width: 1280, height: 800 }, { width: 1024, height: 768 }]) {
    await page.setViewportSize(viewport);
    await page.goto('/');
    await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'shelves');
    const at = `at ${viewport.width}x${viewport.height}`;

    const top = await page.locator('header.topbar').boundingBox();
    const search = await page.getByTestId('home-search').boundingBox();
    expect(search.y + search.height, `the search is not in the top row ${at}`).toBeLessThanOrEqual(
      top.y + top.height + 1
    );

    const spans = () =>
      page.evaluate(() => {
        const main = document.querySelector('main');
        const style = getComputedStyle(main);
        const left = main.getBoundingClientRect().left + parseFloat(style.paddingLeft);
        const width = main.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
        const blocks = [...document.querySelectorAll(
          '.notice-bar, [data-testid=shelf], #home-filters, .chips, .grid'
        )];
        return {
          content: [Math.round(left), Math.round(width)],
          off: blocks
            .map((el) => {
              const r = el.getBoundingClientRect();
              return [el.dataset.testid || el.id || el.className, Math.round(r.left), Math.round(r.width)];
            })
            .filter(([, x, w]) => Math.abs(x - left) > 1 || Math.abs(w - width) > 1)
        };
      });
    expect((await spans()).off, `blocks narrower than the content ${at}`).toEqual([]);

    await page.getByTestId('filter-toggle').click();
    await expect(page.getByTestId('filter-genre')).toBeVisible();
    const panel = await spans();
    expect(panel.off, `the Filters panel is not the content's width ${at}`).toEqual([]);
    const [genre, owned] = await Promise.all([
      page.getByTestId('filter-genre').boundingBox(),
      page.getByTestId('filter-owned').boundingBox()
    ]);
    expect(Math.abs(genre.y - owned.y), `the Filters are not one row of cells ${at}`).toBeLessThan(40);
    await page.getByTestId('filter-toggle').click();

    const poster = await page.evaluate(() =>
      parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--shelf-poster'))
    );
    const shelfCard = page.getByTestId('shelf-card').first();
    if (await shelfCard.count()) {
      expect(Math.round((await shelfCard.boundingBox()).width), `a shelf poster ${at}`).toBe(poster);
    }
    await page.getByTestId('home-search').fill('e');
    await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'grid');
    const gridCard = page.locator('.grid .card-wrap').first();
    await expect(gridCard).toBeVisible();
    expect(Math.round((await gridCard.boundingBox()).width), `a search resized the posters ${at}`).toBe(
      poster
    );
    expect((await spans()).off, `the grid is not the content's width ${at}`).toEqual([]);
  }
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

test("What it's like and People open where they belong: a popover on a desktop, a sheet on a phone", async ({
  page,
  isMobile
}) => {
  // Decisions 554 item 1 and 557: the two cells share one row from 721 px, each its own field, and
  // their lists are overlays, a popover under the field on a desktop and a sheet on a phone. Nothing
  // is chosen.
  const viewports = isMobile ? [page.viewportSize()] : [{ width: 1280, height: 800 }, { width: 1024, height: 768 }];
  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    await page.goto('/');
    await page.getByTestId('filter-toggle').click();
    const terms = page.getByTestId('filter-terms');
    const people = page.getByTestId('filter-people');
    await expect(terms).toBeVisible();
    const at = `at ${viewport.width}x${viewport.height}`;
    const [termBox, peopleBox] = await Promise.all([terms.boundingBox(), people.boundingBox()]);
    if (isMobile) {
      expect(peopleBox.y, `People is not the row under What it's like ${at}`).toBeGreaterThan(termBox.y);
    } else {
      expect(Math.abs(termBox.y - peopleBox.y), `the two cells are not one row ${at}`).toBeLessThan(2);
    }
    for (const [cell, name] of [[terms, "What it's like"], [people, 'People']]) {
      await cell.click();
      // A desktop's People field lists only what a name finds.
      if (name === 'People' && !isMobile) await page.getByTestId('people-search').fill('vill');
      const picker = page.getByRole('dialog', { name });
      await expect(picker).toBeVisible();
      const box = await picker.boundingBox();
      expect(box.x, `the ${name} picker leaves the screen ${at}`).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width, `the ${name} picker leaves the screen ${at}`).toBeLessThanOrEqual(viewport.width + 1);
      if (isMobile) {
        expect(box.width, `the ${name} sheet is not the phone's width`).toBeGreaterThan(viewport.width * 0.95);
        await picker.getByRole('button', { name: 'Done' }).click();
      } else {
        const cellBox = await cell.boundingBox();
        expect(box.y, `the ${name} popover is not under its cell ${at}`).toBeGreaterThan(cellBox.y);
        await page.keyboard.press('Escape');
      }
      await expect(picker).toHaveCount(0);
    }
  }
});

test('a long term label does not widen the page', async ({ page }) => {
  // The fixture's labels are short, so this lengthens one chip's, as the genre test lengthens an option.
  await page.goto('/?term=mood.cosy&kind=movie');
  const chip = page.getByTestId('term-chip');
  await expect(chip).toBeVisible();
  await chip.locator('.label').evaluate((el) => {
    el.textContent = 'the cost of an extraordinarily long vendetta across three generations of one family';
  });
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow, 'a long term label pushed the page wider than the screen').toBeLessThanOrEqual(1);
  const box = await chip.boundingBox();
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
  await expect(page.locator('meta[name=theme-color]')).toHaveAttribute('content', '#0c0b0a');

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
