import { expect, test } from '@playwright/test';

import { openTitle, signedIn } from '../helpers.js';

/**
 * §6.0's two Home modes, and §6.7's one toggle over them.
 *
 * THE STATE MACHINE. §6.0 gives Home exactly one: "Search or an active person-filter switches
 * Home into the catalog grid; clearing it returns the shelves." Both directions are driven
 * below, because the way *back* is the half nothing else in the spec supplies — and proposal 30
 * adds the side effects the sentence leaves implicit: "Tapping a credit navigates to Home,
 * closes the title card, and clears the search box; the person chip is itself the clear
 * control."
 *
 * THE TOGGLE. §6.7 + decision 117: "one global per-user toggle, in the account dropdown,
 * default off … It governs the event rail **and** every inline numeric annotation. The title
 * card's `b(t) · β · gate` line is **not** gated." The gate is a deletion on the server
 * (`home/rail.py`), so the assertions here are about absence from the DOM *and* from the
 * payload: a number hidden by CSS is still in the network tab, in the service-worker cache and
 * in anything that logs a response, which is the promise §6.7 would be making cosmetically.
 *
 * WHY THIS FILE SEEDS. Proposal 20 suppresses every score-ordered shelf for a profile with no
 * verdicts, so a signed-in-and-nothing-else account has no shelf cards at all and every
 * assertion below about a shelf card would pass vacuously. `seedLedger` builds one through
 * §6.1's own routes before anything is asserted.
 *
 * Needs an imported bundle. Skips rather than pretending if the app has none.
 */

/** The second household account, signed in inside its OWN browser context. A second tab shares
 *  the cookie jar, which makes decision 117's "per user" unfalsifiable. */
const SECOND = { name: 'e2e-home-second', password: 'e2e-home-second-pw' };

/** §6.0's shelf table, in its normative order (`home/shelves.py`'s `SHELF_IDS`). */
const SHELF_IDS = [
  'because_anchor',
  'top_of_ledger',
  'never_watched_term',
  'shared_sweet_spot',
  'school_night',
  'new_in_library'
];

/** Decision 117's inventory, verbatim from `home/rail.py`'s `GATED_KEYS`. */
const GATED_KEYS = ['model', 'rail', 'suppressed', 'log', 'ledger'];

const KINDS = ['movie', 'series'];

/**
 * §6.0's Home payload, for the kinds the caller means.
 *
 * `kinds` is not a convenience. §4.1 rule 5 partitions every shelf per kind, so the payload for
 * Films alone holds half the sections the payload for both holds — and a test comparing what
 * the screen renders against what the route answers has to ask the route the question the
 * screen asked.
 */
async function homePayload(request, kinds = KINDS) {
  const query = kinds.map((kind) => `kind=${kind}`).join('&');
  const res = await request.get(`/api/home?${query}`);
  expect(res.ok(), '§6.0 Home must answer for a signed-in user').toBeTruthy();
  return res.json();
}

/** Every card on every shelf. A shelf has no items — only kind-scoped sections do (§4.1
 *  rule 5), which is why this is two flattenings and not one. */
function shelfCards(home) {
  return (home.shelves ?? [])
    .flatMap((shelf) => shelf.sections ?? [])
    .flatMap((section) => section.items ?? []);
}

/**
 * Every decision-117 key found ANYWHERE in a payload.
 *
 * Recursive rather than a check of the six places the annotations happen to live today: the
 * gate is one recursive deletion on the server, so a shelf builder that adds a seventh
 * annotation inherits it — and a test that enumerated call sites would not notice if one did
 * not.
 */
function gatedKeysIn(value, found = new Set()) {
  if (Array.isArray(value)) {
    for (const item of value) gatedKeysIn(item, found);
  } else if (value && typeof value === 'object') {
    for (const [key, nested] of Object.entries(value)) {
      if (GATED_KEYS.includes(key)) found.add(key);
      gatedKeysIn(nested, found);
    }
  }
  return found;
}

/**
 * Give this session's user enough of a ledger for §6.0's shelves to ship.
 *
 * Through §6.1's own routes rather than SQL: §4.2's verdict journal is append-only and the rate
 * session is the only thing that writes it, so anything else would seed a shape the app does
 * not produce.
 *
 * SERIES ONLY, deliberately. The fixture bundle owns six films and two series. Rating the
 * series clears proposal 20's zero-verdict state ("no verdicts yet — a score-ordered shelf
 * would rank on a ledger this profile does not have") while leaving the three films under 110
 * minutes unseen — which is exactly `school_night`'s population, the one shelf a fresh profile
 * can ship on the fixture bundle alone. Rating films instead would mark them seen and suppress
 * that shelf for having fewer than proposal 28's three members.
 */
async function seedLedger(request) {
  if (shelfCards(await homePayload(request)).length) return; // this profile already has one

  await request.post('/api/rate/session', {
    data: { mode: 'sweep', kinds: ['series'], restart: true }
  });
  for (let i = 0; i < 8; i++) {
    const { card } = await (await request.get('/api/rate')).json();
    // The queue drains, and a card of another type or kind is not this seed's to answer.
    if (!card || card.type !== 'sweep' || card.kind !== 'series') break;
    // 0, 1, 2, 0 … — §6.1's class-balance widget warns above a 0.6 share in one class, and a
    // seed that trips the warning of the surface it came from is a seed arguing with itself.
    const answered = await request.post('/api/rate/verdict', {
      data: { card_token: card.token, value: i % 3 },
      failOnStatusCode: false
    });
    if (!answered.ok()) break;
  }
  // §4.2 keeps every row; this closes the live session only, so a later spec opening Rate does
  // not resume a block this file half-filled.
  await request.delete('/api/rate/session');

  expect(
    shelfCards(await homePayload(request)).length,
    'no shelf shipped even with a ledger — every shelf-card assertion below would be vacuous'
  ).toBeGreaterThan(0);
}

/**
 * The first shelf card whose title actually carries a credit.
 *
 * Found rather than named: which titles carry credits is a property of the imported bundle
 * (the fixture credits four of its eight), and without one there is no credit to tap, so
 * §6.0's second trigger into the grid has nothing to fire it.
 */
async function creditedShelfCard(request, home) {
  const tried = new Set();
  for (const card of shelfCards(home)) {
    if (tried.has(card.title_id)) continue;
    if (tried.size >= 24) break; // bounded: this is a lookup, not a crawl
    tried.add(card.title_id);
    const res = await request.get(`/api/titles/${card.title_id}`, { failOnStatusCode: false });
    if (!res.ok()) continue;
    if (((await res.json()).credits ?? []).length) return card;
  }
  return null;
}

/** One shelf card's poster, by title id. The same title can sit on two shelves, so `.first()`
 *  — either instance opens the same card. */
function shelfPoster(page, titleId) {
  return page
    .locator(`[data-testid="shelf-card"][data-title="${titleId}"]`)
    .first()
    .getByRole('button');
}

function titlePanel(page) {
  return page.getByRole('complementary', { name: 'Title detail' });
}

/**
 * Throw decision 117's switch from where the decision puts it — the account dropdown — and
 * wait for the server to have it.
 *
 * Asserting on `aria-checked` rather than on the click is what makes this survive being called
 * when the switch is already in the wanted position.
 */
async function setShowModel(page, on) {
  await page.getByTestId('account-chip').click();
  const toggle = page.getByTestId('show-model-toggle');
  await expect(toggle).toBeVisible();
  if ((await toggle.getAttribute('aria-checked')) !== String(on)) await toggle.click();
  await expect(toggle).toHaveAttribute('aria-checked', String(on));
  await page.getByTestId('account-chip').click();
  await expect(toggle).toHaveCount(0);
}

/**
 * A second household account, in its own context.
 *
 * Created the way §3.1 creates one — an admin issues a one-time password and the account is
 * locked to a password change at first login — so the account this file signs in as is a real
 * member rather than a row invented behind the app's back.
 */
async function secondAccount(page, browser, baseURL) {
  const created = await page.request.post('/api/setup/members', {
    data: { name: SECOND.name, role: 'member' },
    failOnStatusCode: false
  });
  expect([201, 409], 'creating the second member must either work or say it already exists')
    .toContain(created.status());

  const context = await browser.newContext({ baseURL, colorScheme: 'dark' });
  const other = await context.newPage();

  if (created.status() === 201) {
    const otp = (await created.json()).one_time_password;
    await other.request.post('/api/auth/login', { data: { name: SECOND.name, password: otp } });
    await other.request.post('/api/auth/password', {
      data: { current_password: otp, new_password: SECOND.password }
    });
  } else {
    const back = await other.request.post('/api/auth/login', {
      data: { name: SECOND.name, password: SECOND.password },
      failOnStatusCode: false
    });
    expect(
      back.ok(),
      `${SECOND.name} exists with another password — run \`node e2e/reset.mjs\` first`
    ).toBeTruthy();
  }
  return { context, other };
}

test.beforeEach(async ({ page }) => {
  await signedIn(page);
  const config = await (await page.request.get('/api/config')).json();
  test.skip(!config.has_bundle, 'needs an imported bundle — run 01-first-boot first');
  // §6.7's toggle is a PERSISTED preference, so the starting state is set rather than assumed.
  // "Default off" is asserted where it is still true — on the account created below, which
  // nobody has ever thrown the switch for.
  await page.request.post('/api/auth/preferences', { data: { show_model: false } });
  await seedLedger(page.request);
  await page.goto('/');
});

test.afterEach(async ({ page }) => {
  // Leaving it on would change the opening state of every spec that runs after this file.
  await page.request
    .post('/api/auth/preferences', { data: { show_model: false }, failOnStatusCode: false })
    .catch(() => {});
});

// --- §6.0's grid switch -------------------------------------------------------------------

test('search replaces the shelves with the catalog grid and closes the open title card', async ({
  page
}) => {
  // §6.0: "Search or an active person-filter switches Home into the catalog grid". Home opens
  // on the shelves, and the shelves are what the grid has to REPLACE — asserting the grid
  // arrived says nothing unless they were there first.
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'shelves');
  await expect(page.getByTestId('shelves')).toBeVisible();
  await expect(page.getByTestId('shelf-why').first()).not.toBeEmpty();

  // Films only, because that is what Home opens with (owner decision 18). A card read out of
  // the both-kinds payload can be a series that is not on this screen at all, and searching a
  // series name against the film partition returns "No matches" — a different screen from the
  // one under test.
  const home = await homePayload(page.request, ['movie']);
  const film = shelfCards(home)[0];
  expect(film, 'no film on any shelf — the fixture bundle owns six').toBeTruthy();

  await shelfPoster(page, film.title_id).click();
  await expect(titlePanel(page)).toBeVisible();

  await page.getByTestId('home-search').fill(film.name);

  const mode = page.getByTestId('home-mode');
  await expect(mode).toHaveAttribute('data-mode', 'grid');
  await expect(mode).toHaveAttribute('data-reason', 'search');
  await expect(page.getByTestId('shelves')).toHaveCount(0);
  await expect(page.getByTestId('shelf')).toHaveCount(0);
  await expect(page.locator('.card-wrap', { hasText: film.name }).first()).toBeVisible();
  // §6.0's parenthesis: the switch CLOSES the open detail card. Count, not visibility — a card
  // still in the DOM behind the grid is a card that will be back on the next re-render.
  await expect(titlePanel(page)).toHaveCount(0);
});

test('clearing the search box returns the shelves', async ({ page }) => {
  // §6.0: "clearing it returns the shelves". Nothing else in the spec says how a user gets
  // back out of the grid, so this direction is the half that cannot be inferred.
  const search = page.getByTestId('home-search');
  await search.fill('the');
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'grid');
  await expect(page.getByTestId('shelves')).toHaveCount(0);

  await search.fill('');

  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'shelves');
  await expect(page.getByTestId('shelves')).toBeVisible();
  await expect(page.getByTestId('shelf-card').first()).toBeVisible();
});

test('tapping a credit swaps the shelves for a filmography behind a person chip', async ({
  page
}) => {
  // §6.0: "credits, each person tappable → filters the library to their filmography", and
  // "an active person-filter switches Home into the catalog grid". Proposal 30 names the three
  // side effects: navigate to Home, close the title card, clear the search box.
  // Films only — the kinds this screen is actually showing (decision 18).
  const home = await homePayload(page.request, ['movie']);
  const credited = await creditedShelfCard(page.request, home);
  expect(credited, 'no shelf card carries a credit — there is no credit to tap').toBeTruthy();

  await expect(page.getByTestId('shelves')).toBeVisible();
  await shelfPoster(page, credited.title_id).click();

  const panel = titlePanel(page);
  await expect(panel).toBeVisible();
  const person = panel.locator('.person').first();
  const name = (await person.locator('.pname').textContent())?.trim();
  await person.click();

  const mode = page.getByTestId('home-mode');
  await expect(mode).toHaveAttribute('data-mode', 'grid');
  await expect(mode).toHaveAttribute('data-reason', 'person');
  await expect(page.getByTestId('shelves')).toHaveCount(0);
  await expect(panel).toHaveCount(0);
  await expect(page.getByTestId('home-search')).toHaveValue('');

  // Proposal 30: "the person chip is itself the clear control" — so it is always rendered and
  // always removable, never a filter with no visible way off.
  await expect(page.getByTestId('person-chip')).toContainText(name ?? '');
});

test('removing the person chip returns the shelves', async ({ page }) => {
  const home = await homePayload(page.request, ['movie']);
  const credited = await creditedShelfCard(page.request, home);
  expect(credited, 'no shelf card carries a credit — there is no credit to tap').toBeTruthy();

  await shelfPoster(page, credited.title_id).click();
  await titlePanel(page).locator('.person').first().click();

  const chip = page.getByTestId('person-chip');
  await expect(chip).toBeVisible();
  await chip.click();

  // §6.0: "clearing it returns the shelves" — the person half of the same sentence.
  await expect(page.getByTestId('person-chip')).toHaveCount(0);
  await expect(page.getByTestId('home-mode')).toHaveAttribute('data-mode', 'shelves');
  await expect(page.getByTestId('shelves')).toBeVisible();
  await expect(page.getByTestId('shelf-card').first()).toBeVisible();
});

// --- §6.7 / decision 117's toggle ---------------------------------------------------------

test('with the toggle off the rail and every inline number are absent, not merely hidden', async ({
  page
}) => {
  // Decision 117's toggle "governs the event rail **and** every inline numeric annotation".
  // The shelves are on screen and carry cards, so these are absences with something to be
  // absent from.
  await expect(page.getByTestId('shelves')).toBeVisible();
  await expect(page.getByTestId('shelf-card').first()).toBeVisible();

  await expect(page.getByTestId('model-rail-open')).toHaveCount(0);
  await expect(page.getByTestId('model-rail')).toHaveCount(0);
  await expect(page.locator('[data-model-note]')).toHaveCount(0);

  // Proposal 118 puts the rail one keyboard shortcut from every Home render. With the toggle
  // off the shortcut must not be a back door into it.
  await page.keyboard.press('m');
  await expect(page.getByTestId('model-rail')).toHaveCount(0);

  // The sharp half. `toHaveCount(0)` only proves the client did not render them; the promise
  // is that the server never sent them, and that is the difference between a gate and a
  // stylesheet. `home/rail.py`: "hidden by CSS makes the promise cosmetic — the numbers would
  // still be on the wire, in the browser's network tab, in the service-worker cache."
  const payload = await homePayload(page.request, ['movie']);
  expect(shelfCards(payload).length, 'a payload with no cards proves nothing').toBeGreaterThan(0);
  expect([...gatedKeysIn(payload)]).toEqual([]);
});

test('the title card model line renders with the toggle off and with it on', async ({ page }) => {
  // Decision 117, in as many words: "The title card's `b(t) · β · gate` line is **not** gated
  // — §6.0 lists it unconditionally as the M0 transparency promise." A spec that let this line
  // ride on the toggle would pass while the product broke its oldest promise, so the assertion
  // is equality of the two renders rather than presence in one of them.
  await openTitle(page, 'Heat');
  const line = page.getByTestId('title-model-line');
  await expect(line).toBeVisible();
  const off = (await line.textContent())?.trim();
  expect(off, 'the line must say something, even if it is why it is unavailable').toBeTruthy();

  await page.goto('/');
  await setShowModel(page, true);

  await openTitle(page, 'Heat');
  await expect(page.getByTestId('title-model-line')).toBeVisible();
  expect((await page.getByTestId('title-model-line').textContent())?.trim()).toBe(off);
});

test('turning the toggle on reveals the rail, the inline numbers and what did not ship', async ({
  page
}) => {
  await setShowModel(page, true);
  await page.goto('/');
  await expect(page.getByTestId('shelves')).toBeVisible();
  // Both kinds on, so the payload read back below is the one this screen is rendering. Home
  // opens with Films only (decision 18) and §4.1 rule 5 gives every shelf one section per
  // selected kind, so asking the route for both while the page asked for one compares twelve
  // sections against six.
  await page.getByTestId('kind-series').click();
  await expect(page.getByTestId('kind-series')).toHaveAttribute('aria-pressed', 'true');

  // §6.7 calls the rail "the primary M2 debugging instrument"; proposal 118 requires it
  // "reachable in two taps", which is what this button is.
  await expect(page.getByTestId('model-rail-open')).toBeVisible();

  const notes = page.locator('[data-model-note]');
  expect(await notes.count(), 'no inline annotation with the toggle on').toBeGreaterThan(0);
  // §6.8: "model numbers appear in the data voice next to their name … never bare." A name,
  // then its number — a bare figure would satisfy a presence check and break the rule.
  await expect(notes.first()).toHaveText(/[a-zβ]\S*\s+-?\d/i);

  await page.getByTestId('model-rail-open').click();
  const rail = page.getByTestId('model-rail');
  await expect(rail).toBeVisible();
  // Proposal 118: "the last **15** events — a pinned depth, not 'about fifteen'".
  await expect(rail).toContainText('last 15 events');

  const payload = await homePayload(page.request);
  expect([...gatedKeysIn(payload)].sort()).toContain('model');

  // §6.0: "a shelf that cannot say why it exists doesn't ship", and proposal 28 suppresses a
  // section with fewer than three members. An absent shelf is indistinguishable from a bug
  // unless something names it, so every (shelf, kind) either shipped or is named here with a
  // reason — which is also why the list is gated with the rest of the debugging instruments.
  const shipped = new Set(
    payload.shelves.flatMap((shelf) => shelf.sections.map((s) => `${shelf.id}:${s.kind}`))
  );
  const named = new Set(payload.suppressed.map((s) => `${s.shelf}:${s.kind}`));
  for (const shelf of SHELF_IDS) {
    for (const kind of KINDS) {
      expect(
        shipped.has(`${shelf}:${kind}`) || named.has(`${shelf}:${kind}`),
        `${shelf}/${kind} neither shipped nor said why it did not`
      ).toBeTruthy();
    }
  }
  for (const entry of payload.suppressed) expect(entry.reason.trim()).not.toBe('');
  await expect(rail.getByTestId('model-rail-suppressed')).toHaveCount(payload.suppressed.length);

  // And back off again: the same one switch, in both directions.
  await setShowModel(page, false);
  await expect(page.getByTestId('model-rail')).toHaveCount(0);
  await expect(page.getByTestId('model-rail-open')).toHaveCount(0);
  await expect(page.locator('[data-model-note]')).toHaveCount(0);
});

test('the toggle is off by default, and one user turning it on leaves the other unchanged', async ({
  page,
  browser,
  baseURL
}) => {
  // Decision 117: "one global per user … default off". A SEPARATE CONTEXT, not a second tab —
  // one cookie jar signed in as one account cannot show that a preference is per user.
  const { context, other } = await secondAccount(page, browser, baseURL);
  try {
    await seedLedger(other.request);
    await other.goto('/');
    await expect(other.getByTestId('home-greeting')).toBeVisible();
    // Two accounts, said out loud. Everything below is about one session not seeing the
    // other's preference, and two contexts signed in as the same person would prove nothing.
    await expect(other.getByTestId('account-chip')).toContainText(SECOND.name);
    await expect(page.getByTestId('account-chip')).not.toContainText(SECOND.name);

    // "Default off", asserted on the only account where it is still the default: this one was
    // created moments ago and nobody has thrown its switch.
    await other.getByTestId('account-chip').click();
    await expect(other.getByTestId('show-model-toggle')).toHaveAttribute('aria-checked', 'false');
    await other.getByTestId('account-chip').click();

    const shelfCardCount = await other.getByTestId('shelf-card').count();
    expect(
      shelfCardCount,
      'the second account needs shelves of its own for their bareness to mean anything'
    ).toBeGreaterThan(0);
    await expect(other.getByTestId('model-rail-open')).toHaveCount(0);
    await expect(other.locator('[data-model-note]')).toHaveCount(0);

    // The first account turns it on…
    await setShowModel(page, true);
    await page.goto('/');
    await expect(page.getByTestId('model-rail-open')).toBeVisible();
    expect(await page.locator('[data-model-note]').count()).toBeGreaterThan(0);

    // …and the second account's Home is exactly what it was. Both halves: the screen, and the
    // payload behind it — the toggle is read from the session user, so a gate keyed off
    // anything more global than that would leak here and nowhere else.
    await other.reload();
    await expect(other.getByTestId('home-greeting')).toBeVisible();
    await expect(other.getByTestId('shelf-card')).toHaveCount(shelfCardCount);
    await expect(other.getByTestId('model-rail-open')).toHaveCount(0);
    await expect(other.locator('[data-model-note]')).toHaveCount(0);
    expect([...gatedKeysIn(await homePayload(other.request))]).toEqual([]);

    // The ungated half survives the split too: §6.0's model line is there for the account that
    // never asked to see the model.
    await openTitle(other, 'Heat');
    await expect(other.getByTestId('title-model-line')).toBeVisible();
  } finally {
    await context.close();
  }
});
