import { expect, test } from '@playwright/test';

import { openTitle, signedIn } from '../helpers.js';

/**
 * §6.0's title detail card, and the §4.1 rules that are only observable at the last step —
 * the two DNA tiers staying distinguishable, credits deduped at read time, and platform
 * scores carrying their display-only caption.
 */

test.beforeEach(async ({ page }) => {
  await signedIn(page);
  const config = await (await page.request.get('/api/config')).json();
  test.skip(!config.has_bundle, 'needs an imported bundle — run 01-first-boot first');
  // By name. Clicking the first card opens whatever the grid shows before the debounced
  // search lands — the catalog is ordered by year descending, so that is Paddington 2, and
  // every Heat-specific assertion below then fails for a reason that has nothing to do with
  // what it is testing.
  await openTitle(page, 'Heat');
});

test('the card carries metadata, overview and the model line', async ({ page }) => {
  const panel = page.getByLabel('Title detail');
  await expect(panel.getByRole('heading', { name: 'Heat' })).toBeVisible();
  await expect(panel.locator('.sub')).toContainText('1995');
  await expect(panel.locator('.sub')).toContainText('movie');
  // §6.0: the model line is in the data voice and is NOT gated by the show-the-model toggle
  // (decision 117) — it is the M0 transparency promise.
  //
  // ASSERTED OUTRIGHT, not as an alternation. This read
  // `/bundle test-v1|model line unavailable/` — which accepts the FAILING outcome, and this
  // spec skips without a bundle, so the only two states the assertion could ever meet both
  // satisfied it. It was the browser half of coverage row `library-rate-model-line-no-bundle`,
  // whose `what` says the branch returns the real numbers; a disjunction with the failure in
  // it closes that row with a tautology. §10's import runs the rebuild set in-request
  // (`placement/reconcile.run_rebuild` folds in and materialises `title_prior` before the
  // bundle is activated), so by the time this file runs Heat has a prior for the active
  // bundle and the available branch is the only honest outcome. The numeric half is the point
  // of the line — §6.0 spells it `b(t) 0.52 · β 0.8 · gate 0.93`, and a version string with no
  // figure beside it would satisfy a presence check while saying nothing.
  // [M4.9, plan §6 "Rows to amend"]
  //
  // THE SIGN IS PART OF THE NUMBER. §5.1 defines b(t) as "the shrunk item prior" — a quantity on
  // the latent score scale, pulled toward the crowd mean μ by the gate — so it is centred, and a
  // title the crowd rates below that mean carries a negative one. Heat's is: the fixture's
  // backbone draws b_i from N(0, 0.6) and Heat's draw is -0.7997, which at n=4218 (gate 0.998)
  // prints `b(t) -0.80`. `[\d.]+` encoded a non-negativity §5 does not state, and three of the
  // fixture's seven backbone titles falsify it. Two decimals, because that is what
  // `scoring/serve._format_line` guarantees — §6.0's own example prints one on β and the
  // formatter deliberately does not.
  await expect(panel.locator('.modelline')).toContainText('bundle test-v1');
  await expect(panel.locator('.modelline')).toContainText(/b\(t\) -?\d+\.\d\d/);
});

test('both actions are present, and a missing one is disabled rather than absent', async ({
  page,
}) => {
  // §6.0 requires two actions. The prototype shipped only "Show on map"; a missing Jellyfin
  // link must read as "not configured", not as "this film cannot be played".
  const panel = page.getByLabel('Title detail');
  await expect(panel.getByRole('button', { name: 'Play on Jellyfin' })).toBeDisabled();
  await expect(panel.getByRole('link', { name: 'Show on map' })).toBeVisible();
});

test('the two DNA tiers are visibly distinct and a shared term appears in both', async ({
  page,
}) => {
  // §4.1 rule 1: "14,181 (title,term) pairs exist in both and must stay distinguishable."
  // The fixture reproduces that overlap in miniature; this is where it becomes visible.
  const panel = page.getByLabel('Title detail');
  await expect(panel.getByText('DNA — EXTRACTED')).toBeVisible();
  await expect(panel.getByText('DNA — PROJECTED (INFERRED)')).toBeVisible();

  const extracted = panel.locator('.tag .term');
  const projected = panel.locator('.chip');
  await expect(extracted.filter({ hasText: 'themes.obsession' })).toBeVisible();
  await expect(projected.filter({ hasText: 'themes.obsession' })).toBeVisible();
});

test('every extracted tag shows its evidence quote and source', async ({ page }) => {
  // §4.1 rule 1: "a tag without its quote is unfalsifiable."
  const panel = page.getByLabel('Title detail');
  const tags = panel.locator('.tag');
  await expect(tags.first()).toBeVisible();

  for (const tag of await tags.all()) {
    const quotes = tag.locator('.quote');
    await expect(quotes.first()).toBeVisible();
    // The json-codec bug rendered ~80 empty quotes per tag; a non-empty first quote and a
    // sane count are what would have caught it.
    await expect(quotes.first()).not.toHaveText('“”');
    expect(await quotes.count()).toBeLessThan(6);
    await expect(tag.locator('.src').first()).toContainText(/:/); // e.g. trakt:comment
  }
});

test('salience is shown, and nothing is filtered by it', async ({ page }) => {
  // §4.1 rule 2: weights, never filters. Salience is visible next to the tag it weights.
  const panel = page.getByLabel('Title detail');
  await expect(panel.locator('.tag').first().getByText(/sal [123]/)).toBeVisible();
});

test('credits are deduped at read time and cite their sources', async ({ page }) => {
  // §4.1: "credit (dedupe at read time, never at import)". The fixture stores the director
  // twice, from tmdb and omdb; the card must show one row that says so.
  const panel = page.getByLabel('Title detail');
  const director = panel.locator('.person', { hasText: 'Michael Mann' });
  await expect(director).toHaveCount(1);
  await expect(director).toContainText('2 sources');
});

test('platform scores travel with their display-only caption', async ({ page }) => {
  // §4.1 rule 3: aggregate platform scores are a popularity conduit and are banned as model
  // features. The caption is the only thing stopping a reader assuming otherwise.
  const panel = page.getByLabel('Title detail');
  await expect(panel.locator('.scores')).toBeVisible();
  await expect(panel.getByText(/display-only schema.*never model features/)).toBeVisible();
});

test('tapping a second poster re-fetches instead of showing the first', async ({ page }) => {
  // Both posters have to come from the SAME grid. §6.0 M2 makes typing into search switch Home
  // into the grid AND close the open card, so re-searching between the two taps would close the
  // panel and reopen it — which proves only that a reopened panel shows what it was reopened
  // with. The bug this guards against is a panel that keeps the first title's data, and it is
  // only observable when the panel is already open as the second poster is tapped.
  const panel = page.getByLabel('Title detail');
  await page.getByRole('searchbox', { name: 'Search titles' }).fill('e');
  const heat = page.locator('.card-wrap', { hasText: 'Heat' }).first();
  const prisoners = page.locator('.card-wrap', { hasText: 'Prisoners' }).first();
  await expect(heat).toBeVisible();
  await expect(prisoners).toBeVisible();

  await heat.click();
  await expect(panel.getByRole('heading', { name: 'Heat' })).toBeVisible();
  await prisoners.click();
  await expect(panel.getByRole('heading', { name: 'Prisoners' })).toBeVisible();
  await expect(panel.getByRole('heading', { name: 'Heat' })).toHaveCount(0);
});

// --- M4.9: the chip, the count line, and the card that used to throw --------------------------

/** §4.3: a vocabulary id IS `facet.term`, so a chip carries exactly one dot. */
const VOCAB_ID = /^[a-z_]+\.[a-z0-9_]+$/;
/** The defect: `{facet}.{term}` printed over a term that already carries its prefix. */
const DOUBLED = /^[a-z_]+\.[a-z_]+\./;

/**
 * What one chip was actually painted with: the custom property the component named, and the
 * value the document resolves it to.
 *
 * The declared property is read rather than `getComputedStyle(el).color`, because the neutral
 * and a facet colour are both an rgb triple by the time the cascade is done — the falsifiable
 * fact is WHICH token the component reached for. The resolved value is read as well, so a
 * `--facet-*` name that `design.css` never defines (which the browser silently drops back to
 * the inherited colour) fails here rather than looking correct.
 */
async function chipColour(chip) {
  return chip.evaluate((el) => {
    const declared = el.style.color || '';
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
  // §4.3 + §6.8, both halves of finding 3 and finding 4 at the one place they are observable.
  //
  // THE LABEL. The corpus keys the vocabulary as `characters.amateur_sleuth`, so `dna_tag.term`
  // already carries its facet; the card printed `{facet}.{term}` on top of that and rendered
  // `narrative_themes.themes.obsession` for 92.5% of the shipped tags. The term is printed
  // alone now and the facet is spent on the colour, which is the identity §6.8 asks for.
  //
  // THE COLOUR. §6.8: "a fixed colour per vocabulary facet (11)". The extraction axis the
  // corpus ships (`character_dynamics`, `narrative_themes`) is not a vocabulary facet id, so
  // ten of the eleven facets resolved to `--ink-4` and the palette was a decoration nothing
  // could read. Both tiers are walked: the two lists are built from different payload keys and
  // the projected one was written by copying the extracted one, which is how a fix to one of
  // them could leave the other wrong.
  const panel = page.getByLabel('Title detail');
  const chips = [
    ...(await panel.locator('.tag .term').all()),
    ...(await panel.locator('.chips .chip').all())
  ];
  expect(
    chips.length,
    'Heat carries tags in both tiers - with none, every assertion below is vacuous'
  ).toBeGreaterThan(1);

  for (const chip of chips) {
    const text = ((await chip.textContent()) ?? '').trim();
    expect(text, `chip "${text}" is not a vocabulary id printed once`).toMatch(VOCAB_ID);
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
  // §6.0 applies a count-line discipline to the kind toggle — "with one active the count line
  // says how many the other holds" — and this surface ignored it: twelve of a median
  // twenty-four credits rendered with nothing saying so, and 89.7% of corpus titles carry more
  // than twelve, so a writer, composer or cinematographer was simply absent. [M4.9 finding 7]
  //
  // THE PAYLOAD IS INTERCEPTED, and this is the thing to read before trusting the case.
  // `make_bundle.py`'s richest title collapses to TWO credit rows, so no fixture title reaches
  // `CREDIT_FOLD` and the disclosure never renders on this bundle — the case would pass by
  // never finding its own subject, which is the shape of failure this file exists to stop. The
  // fixture belongs to M4.8 and adding a twenty-credit title to it is not this milestone's
  // line, so the ROWS are extended instead: the response is the server's own, `credits` is the
  // server's own list with rows of the same shape appended, and what is under test is the
  // client rule the corpus makes load-bearing. Reported to the owner as a fixture gap rather
  // than left implicit here.
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
    // The heading names what is on screen against what is held, in the data voice.
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
    // Both labels are the constant, not a word for it. The expanded button said `Show twelve`,
    // which is the one spelling of `CREDIT_FOLD` that an edit to the constant cannot reach.
    // [M4.9 review cycle 1: M49-CARD-5]
    await expect(disclosure).toHaveText(`Show ${FOLD}`);
    // The disclosure spends what the client already holds: `credits_for` returns every row and
    // the card kept twelve, so no route changed and no query grew a LIMIT to make this
    // possible — and a second fetch here would mean one had.
    expect(served, 'the disclosure fetched instead of spending the payload').toBe(fetched);

    // …and back, because a fold that only opens has stopped being one.
    await disclosure.click();
    await expect(panel.getByTestId('credit-count')).toHaveText(`${FOLD} of ${TOTAL}`);
    await expect(panel.locator('.people .person')).toHaveCount(FOLD);
  } finally {
    await page.unroute(detail);
  }
});

test('the worst cross-department titles open without a console error', async ({ page }) => {
  // The browser half of M4.9's exit criterion (plan §7). `credits_for` groups by (person, job)
  // rather than by (person, department, job) because TMDB files one job under two department
  // spellings — 7,918 such triples across 1,216 of the corpus's 19,071 titles — and before that
  // the card produced two rows the keyed each could not tell apart. Svelte 5 throws
  // `each_key_duplicate` on it, in the production branch too, and with no +error.svelte the
  // whole card died mid-render. The offenders are FOUND, not named: which titles collide is a
  // property of the imported bundle, and M4.8 put exactly one such triple in the fixture.
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
  // Resource loads are the page's assets, not its render, and WebKit reports a missing favicon
  // as a console error on a suite that never asked for one. Everything else counts, and an
  // uncaught exception — which is what `each_key_duplicate` is — arrives as a pageerror.
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    if (/Failed to load resource|favicon/i.test(message.text())) return;
    errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(String(error)));

  const worst = ranked.slice(0, 3);
  for (const title of worst) {
    const panel = await openTitle(page, title.name);
    // Past CAST & CREW, which is where the throw was: a card that dies mid-render still shows
    // its heading.
    await expect(panel.getByTestId('credit-count')).toBeVisible();
    await expect(panel.locator('.people .person').first()).toBeVisible();
  }
  expect(errors, `the card threw on ${worst.map((t) => t.name).join(', ')}`).toEqual([]);
});
