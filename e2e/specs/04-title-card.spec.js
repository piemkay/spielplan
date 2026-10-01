import { expect, test } from '@playwright/test';

import { openTitle, signedIn } from '../helpers.js';

/**
 * §6.0's title detail card, a sheet since decision 527, and the §4.1 rules that are only
 * observable at the last step —
 * the two DNA tiers staying distinguishable, credits deduped at read time, and platform
 * scores carrying their display-only caption.
 */

test.beforeEach(async ({ page }) => {
  await signedIn(page);
  await openTitle(page, 'Heat');
});

/** Show the model (decision 117). Callers put it back in `finally`: later specs start from off. */
async function showModel(page, on) {
  const set = await page.request.post('/api/auth/preferences', { data: { show_model: on } });
  expect(set.ok(), 'the preference did not save').toBeTruthy();
}

/** Open "More about this film", behind which the scores and both DNA tiers wait (decision 517). */
async function openMore(panel) {
  const more = panel.getByTestId('title-more');
  if (!(await more.evaluate((el) => el.open))) await panel.getByTestId('title-more-toggle').click();
  await expect(more).toHaveAttribute('open', '');
}

test('the card carries metadata, overview and the model line', async ({ page }) => {
  let panel = page.getByLabel('Title detail');
  await expect(panel.getByRole('heading', { name: 'Heat' })).toBeVisible();
  // "1995 · 2h 50m": no column value on the card (decision 486).
  await expect(panel.locator('.sub')).toHaveText(/^1995 · \d/);
  await expect(panel.getByTestId('title-directed')).toHaveText('Directed by Michael Mann');
  // Decision 486: the model line is shown only with Show the model on.
  await expect(panel.locator('.modelline')).toHaveCount(0);
  await showModel(page, true);
  try {
    panel = await openTitle(page, 'Heat');
    await expectTheModelLine(panel);
  } finally {
    await showModel(page, false);
  }
});

async function expectTheModelLine(panel) {
  // Asserted outright, never as an alternation with the failure: the import builds Heat's prior.
  // b(t) is centred (§5.1) and Heat's is negative; `_format_line` prints two decimals.
  await expect(panel.locator('.modelline')).toContainText(/b\(t\) -?\d+\.\d\d/);
  // Where the coordinate came from, in words; the source id and the bundle stay off the card.
  await expect(panel.locator('.modelline')).toContainText('Placed by');
  await expect(panel.locator('.modelline')).not.toContainText(/bundle|_/);
}

test('the card leads with Play, the ranking row and Watched or Not seen, and folds the rest', async ({
  page
}) => {
  // Decisions 517 and 536: no verdict tiles, the ladder rates; the long sections are folded.
  const panel = page.getByLabel('Title detail');
  await expect(panel.getByRole('button', { name: 'Play on Jellyfin' })).toBeVisible();
  await expect(panel.getByTestId('rank-card-tier')).toBeVisible();
  // Heat is unwatched here, so the pair stands on Not seen; 08-jellyfin taps "Mark as watched".
  await expect(panel.getByTestId('title-watched')).toHaveText('Mark as watched');
  await expect(panel.getByTestId('title-not-seen')).toHaveAttribute('aria-pressed', 'true');
  await expect(panel.locator('[data-answer]')).toHaveCount(0);
  await expect(panel.getByText("You haven't rated this yet.")).toHaveCount(0);
  const more = panel.getByTestId('title-more');
  await expect(more).not.toHaveAttribute('open', '');
  await expect(panel.getByTestId('title-more-toggle')).toHaveText('More about this film');
  await expect(panel.locator('.scores')).toBeHidden();
  await expect(panel.locator('.tag').first()).toBeHidden();
  await openMore(panel);
  await expect(panel.locator('.scores')).toBeVisible();
  await expect(panel.locator('.tag').first()).toBeVisible();
});

test('Play is disabled with its reason', async ({ page }) => {
  // The real reason, never a milestone label (decision 486).
  const panel = page.getByLabel('Title detail');
  await expect(panel.getByRole('button', { name: 'Play on Jellyfin' })).toBeDisabled();
  const why = panel.getByTestId('title-jellyfin-why');
  await expect(why).toBeVisible();
  await expect(why).not.toContainText(/\bM\d\b/);
});

test('Shares a lot with is absent while fewer than three films share with Heat', async ({ page }) => {
  // Decision 550: absent under 3; each fixture term sits on one title, so nothing shares two.
  const listing = await (await page.request.get('/api/titles?kind=movie&q=heat')).json();
  const heat = listing.items.find((t) => t.name === 'Heat');
  expect((await (await page.request.get(`/api/titles/${heat.id}`)).json()).shares).toEqual([]);
  await expect(page.getByLabel('Title detail').getByTestId('title-shares')).toHaveCount(0);
});

test('the two DNA tiers are visibly distinct, and a term in both is shown once, quoted', async ({
  page,
}) => {
  // §4.1 rule 1: the payload keeps both tiers apart; the card shows a shared term once, in the
  // quoted tier (decision 517).
  const listing = await (await page.request.get('/api/titles?kind=movie&q=heat')).json();
  const heat = listing.items.find((t) => t.name === 'Heat');
  const dna = (await (await page.request.get(`/api/titles/${heat.id}`)).json()).dna;
  expect(dna.extracted.map((t) => t.term)).toContain('themes.obsession');
  expect(dna.projected.map((t) => t.term)).toContain('themes.obsession');

  const panel = page.getByLabel('Title detail');
  await openMore(panel);
  await expect(panel.getByRole('heading', { name: "What it's like" })).toBeVisible();
  // The inferred tier is apart and named as less certain; Heat's one guess is folded.
  await expect(panel.getByText('Our read · less certain')).toBeVisible();

  // By label: the fixture ships `themes.obsession` with the label "obsession".
  const extracted = panel.locator('.tag .term');
  const projected = panel.locator('.chip .chiplabel');
  await expect(extracted.filter({ hasText: /^obsession$/ })).toBeVisible();
  await expect(projected.filter({ hasText: /^obsession$/ })).toHaveCount(0);
});

test('every extracted tag shows its evidence quote and source', async ({ page }) => {
  // §4.1 rule 1: "a tag without its quote is unfalsifiable." A chip shows its quotes when picked.
  const panel = page.getByLabel('Title detail');
  await openMore(panel);
  const tags = panel.locator('.tag');
  await expect(tags.first()).toBeVisible();
  const card = panel.getByTestId('title-evidence');

  for (const tag of await tags.all()) {
    await tag.click();
    await expect(tag).toHaveAttribute('aria-pressed', 'true');
    await expect(card).toContainText((await tag.textContent())?.trim() ?? '');
    const quotes = card.locator('.quote');
    await expect(quotes.first()).toBeVisible();
    // A codec bug once rendered ~80 empty quotes per tag.
    await expect(quotes.first()).not.toHaveText('“”');
    expect(await quotes.count()).toBeLessThan(6);
    // By name ("Trakt · comment"), never the stored key (decision 486).
    await expect(card.locator('.src').first()).toHaveText(/\S/);
    await expect(card.locator('.src').first()).not.toContainText(':');
  }
});

test('a quote cut mid-sentence says so and a one-source projection is fainter, never dropped', async ({
  page,
}) => {
  // §4.1 rules 1 and 2: a span with neither a sentence's start nor its end is printed as a
  // fragment; a single-source projection is fainter and folded, never dropped (decision 517).
  const panel = page.getByLabel('Title detail');
  await openMore(panel);
  // The chip's text starts with the dot's whitespace, so the label is matched on its own span.
  await panel.locator('.tag').filter({ has: page.locator('.term', { hasText: /^obsession$/ }) }).click();
  await expect(panel.locator('.quote', { hasText: 'the work eats the man' })).toHaveText(
    '“…the work eats the man and he lets it…”'
  );
  await expect(panel.locator('.chips .chip')).toHaveCount(1);
  await expect(panel.locator('.chips .chip.faint')).toHaveCount(1);
  const fold = panel.getByTestId('title-weak-chips');
  await fold.locator('summary').click();
  await expect(fold.locator('.chip.faint')).toBeVisible();
});

test("salience is Show the model's, and nothing is filtered by it", async ({ page }) => {
  // §4.1 rule 2: weights, never filters. Salience shows only with the switch on (decision 486).
  let panel = page.getByLabel('Title detail');
  const tags = await panel.locator('.tag').count();
  expect(tags, 'Heat carries extracted tags').toBeGreaterThan(0);
  await expect(panel.getByText(/\bsal [123]\b/)).toHaveCount(0);
  await showModel(page, true);
  try {
    panel = await openTitle(page, 'Heat');
    await openMore(panel);
    await expect(panel.getByTestId('title-evidence').getByText(/sal [123]/)).toBeVisible();
    await expect(panel.locator('.tag')).toHaveCount(tags);
  } finally {
    await showModel(page, false);
  }
});

test('credits are deduped at read time, and their source count is the operator\'s', async ({
  page
}) => {
  // §4.1: "credit (dedupe at read time, never at import)". The fixture stores the director twice.
  const panel = page.getByLabel('Title detail');
  const director = panel.locator('.person', { hasText: 'Michael Mann' });
  await expect(director).toHaveCount(1);
  await expect(director).not.toContainText('sources');
});

test('platform scores travel with their display-only caption', async ({ page }) => {
  // §4.1 rule 3: platform scores are banned as model features, and the caption says so.
  const panel = page.getByLabel('Title detail');
  await openMore(panel);
  await expect(panel.locator('.scores')).toBeVisible();
  await expect(panel.getByText(/never change your suggestions/)).toBeVisible();
});

test('tapping a second poster re-fetches instead of showing the first', async ({ page }) => {
  // Both posters from the SAME grid. The card is a sheet over the grid, so it closes between.
  const panel = page.getByLabel('Title detail');
  await page.keyboard.press('Escape');
  await expect(panel).toHaveCount(0);
  await page.getByRole('searchbox', { name: 'Search titles' }).fill('e');
  // "e" only sits inside "Prisoners", so it waits behind the looser matches (decision 516).
  await page.getByTestId('weak-matches-toggle').click();
  const heat = page.locator('.card-wrap', { hasText: 'Heat' }).first();
  const prisoners = page.locator('.card-wrap', { hasText: 'Prisoners' }).first();
  await expect(heat).toBeVisible();
  await expect(prisoners).toBeVisible();

  await heat.click();
  await expect(panel.getByRole('heading', { name: 'Heat' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(panel).toHaveCount(0);
  await prisoners.click();
  await expect(panel.getByRole('heading', { name: 'Prisoners' })).toBeVisible();
  await expect(panel.getByRole('heading', { name: 'Heat' })).toHaveCount(0);
});

// --- M4.9: the chip, the count line, and the card that used to throw --------------------------

// §4.3: a vocabulary id IS `facet.term`, and no chip prints one (decision 486).
const VOCAB_ID = /^[a-z_]+\.[a-z0-9_]+$/;
// `{facet}.{term}` printed over a term that already carries its prefix.
const DOUBLED = /^[a-z_]+\.[a-z_]+\./;

/**
 * The token a chip's facet dot was painted with and its resolved value. The declared property,
 * because computed colours are all rgb; resolved too, so an undefined `--facet-*` name fails.
 */
async function chipColour(chip) {
  return chip.evaluate((el) => {
    const declared = el.querySelector('.dot')?.style.background || '';
    const named = declared.match(/var\(\s*(--[a-z0-9-]+)\s*\)/);
    const token = named ? named[1] : '';
    return {
      declared,
      token,
      value: token
        ? getComputedStyle(document.documentElement).getPropertyValue(token).trim()
        : ''
    };
  });
}

test('a DNA chip prints its term once and wears its facet colour', async ({ page }) => {
  // The term's label, never its id (decision 486); the facet is spent on the dot beside it, "a
  // fixed colour per vocabulary facet" (§6.8). Both tiers, built from different payload keys.
  const panel = page.getByLabel('Title detail');
  const chips = [
    ...(await panel.locator('.tag').all()).map((el) => [el, el.locator('.term')]),
    ...(await panel.locator('.chips .chip').all()).map((el) => [el, el.locator('.chiplabel')])
  ];
  expect(
    chips.length,
    'Heat carries tags in both tiers - with none, every assertion below is vacuous'
  ).toBeGreaterThan(1);

  for (const [chip, label] of chips) {
    const text = ((await label.textContent()) ?? '').trim();
    expect(text, 'a chip with no name').not.toBe('');
    expect(text, `chip "${text}" prints the vocabulary id, not its label`).not.toMatch(VOCAB_ID);
    expect(text, `chip "${text}" prints its facet twice`).not.toMatch(DOUBLED);

    const colour = await chipColour(chip);
    expect(
      colour.token,
      `chip "${text}" was painted with ${colour.declared || 'no colour at all'}`
    ).toMatch(/^--facet-/);
    expect(
      colour.value,
      `chip "${text}" names ${colour.token}, which design.css does not define`
    ).not.toBe('');
  }
});

test('the credit list says how many it is hiding and the disclosure reveals them', async ({
  page
}) => {
  // THE PAYLOAD IS INTERCEPTED: no fixture title has enough credits to reach `CREDIT_FOLD`, so the
  // server's own rows are extended with rows of the same shape.
  const listing = await (await page.request.get('/api/titles?kind=movie&limit=60')).json();
  const heat = listing.items.find((t) => t.name === 'Heat');
  expect(heat, 'the film this file opens must be in the catalog').toBeTruthy();

  const real = await (await page.request.get(`/api/titles/${heat.id}`)).json();
  expect(real.credits.length, 'a payload with no credits cannot be folded').toBeGreaterThan(0);

  const FOLD = 12; // `TitleDetail.svelte`'s CREDIT_FOLD, restated where the number is asserted
  const TOTAL = 20;
  const credits = [...real.credits];
  while (credits.length < TOTAL) {
    const i = credits.length;
    credits.push({
      ...real.credits[i % real.credits.length],
      person_id: 900000 + i,
      name: `Crew Member ${i}`,
      job: `Crew Job ${i}`,
      ord: i,
      sources: ['tmdb']
    });
  }

  let served = 0;
  const detail = new RegExp(`/api/titles/${heat.id}(\\?|$)`);
  await page.route(detail, async (route) => {
    served += 1;
    await route.fulfill({ json: { ...real, credits } });
  });

  try {
    const panel = await openTitle(page, 'Heat');
    await expect(panel.locator('.people .person').first()).toBeVisible();
    await openMore(panel);
    await expect(panel.getByTestId('credit-count')).toHaveText(`${FOLD} of ${TOTAL}`);
    await expect(panel.locator('.people .person')).toHaveCount(FOLD);

    const disclosure = panel.getByTestId('credits-disclosure');
    await expect(disclosure).toHaveText(`Show all ${TOTAL}`);
    await expect(disclosure).toHaveAttribute('aria-expanded', 'false');

    const fetched = served;
    await disclosure.click();
    await expect(panel.getByTestId('credit-count')).toHaveText(`${TOTAL} of ${TOTAL}`);
    await expect(panel.locator('.people .person')).toHaveCount(TOTAL);
    await expect(disclosure).toHaveAttribute('aria-expanded', 'true');
    await expect(disclosure).toHaveText(`Show ${FOLD}`);
    // The disclosure spends the payload it already holds.
    expect(served, 'the disclosure fetched instead of spending the payload').toBe(fetched);

    await disclosure.click();
    await expect(panel.getByTestId('credit-count')).toHaveText(`${FOLD} of ${TOTAL}`);
    await expect(panel.locator('.people .person')).toHaveCount(FOLD);
  } finally {
    await page.unroute(detail);
  }
});

test('the worst cross-department titles open without a console error', async ({ page }) => {
  // TMDB files one job under two department spellings, and duplicate keys made Svelte throw
  // `each_key_duplicate` mid-render. The offenders are found, not named: they are the bundle's.
  const listing = await (
    await page.request.get('/api/titles?kind=movie&kind=series&limit=200')
  ).json();
  const ranked = [];
  for (const row of listing.items) {
    const detail = await (await page.request.get(`/api/titles/${row.id}`)).json();
    const collisions = (detail.credits ?? []).filter((c) => (c.departments ?? []).length > 1);
    if (collisions.length) ranked.push({ name: row.name, n: collisions.length });
  }
  ranked.sort((a, b) => b.n - a.n || a.name.localeCompare(b.name));
  expect(
    ranked.length,
    'no title in this bundle carries a cross-department credit - nothing here is under test'
  ).toBeGreaterThan(0);

  const errors = [];
  // WebKit reports a missing favicon as a console error; asset loads are not the render.
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    if (/Failed to load resource|favicon/i.test(message.text())) return;
    errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(String(error)));

  const worst = ranked.slice(0, 3);
  for (const title of worst) {
    const panel = await openTitle(page, title.name);
    // Past the credits, where the throw was: a card that dies mid-render still shows its heading.
    await expect(panel.locator('.people .person').first()).toBeVisible();
    await openMore(panel);
    await expect(panel.getByRole('heading', { name: "What it's like" })).toBeVisible();
  }
  expect(errors, `the card threw on ${worst.map((t) => t.name).join(', ')}`).toEqual([]);
});
