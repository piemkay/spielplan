import { expect, test } from '@playwright/test';

import { signedIn } from '../helpers.js';

/**
 * §6.1's Rate surface: that the surface obeys rules the integration tests already cover. Its own
 * members, created per run, because verdicts are append-only and a rated title never returns.
 * Three of them: the fixture's eight titles are spent by pairs and by the fifteen-card block.
 * Serial, one page: this is a session.
 */
test.describe.configure({ mode: 'serial' });

const MEMBER_PASSWORD = 'rate-e2e-member-password';

/**
 * The model's belief about the title, which §6.1 forbids before the tap. `verdict_class` is the
 * person's own prior label, which anchors just as hard.
 */
const BELIEF_KEYS = [
  'cdf',
  'sigma',
  's',
  'tier',
  'score',
  'predicted',
  'predicted_label',
  'prediction',
  'verdict_class',
  'label_count',
  'reask_of',
  'placement'
];

// Decision 491: the balance check arms at fifteen ratings, and says so until it does.
const ARMING = 'A balance check starts at 15 ratings.';

// --- reading the surface ---------------------------------------------------------------------

const counter = (page) => page.getByTestId('rate-counter');
const sweepCard = (page) => page.getByTestId('rate-sweep-card');
const battleCard = (page) => page.getByTestId('rate-battle-card');
const undoChip = (page) => page.getByTestId('rate-undo');
const modeTitle = (page) => page.getByTestId('rate-menu');

/** The counter reads "7 of 15" and the screen's title names the mode chosen (decisions 519, 527). */
async function expectAt(page, slot, mode) {
  await expect(counter(page)).toHaveText(`${slot} of 15`);
  await expect(modeTitle(page)).toHaveText(mode);
}

async function envelope(page) {
  const res = await page.request.get('/api/rate');
  expect(res.ok(), 'the Rate surface must answer GET /api/rate').toBeTruthy();
  return res.json();
}

async function seenState(page, titleId) {
  const res = await page.request.get(`/api/titles/${titleId}/state`);
  expect(res.ok()).toBeTruthy();
  return (await res.json()).state;
}

function keysOf(value, into = new Set()) {
  if (Array.isArray(value)) {
    for (const item of value) keysOf(item, into);
    return into;
  }
  if (value && typeof value === 'object') {
    for (const [key, nested] of Object.entries(value)) {
      into.add(key);
      keysOf(nested, into);
    }
  }
  return into;
}

// --- arranging a state the browser then has to render ------------------------------------------

/** Arrangement only: every assertion is made against what the page renders. */
async function control(page, body) {
  const res = await page.request.post('/api/rate/session', { data: body });
  expect(res.ok(), `POST /api/rate/session ${JSON.stringify(body)}`).toBeTruthy();
  return res.json();
}

async function answerOverHttp(page, { value = 2 } = {}) {
  const { card } = await envelope(page);
  if (!card) return null;
  const [path, data] =
    card.type === 'sweep'
      ? ['/api/rate/verdict', { card_token: card.token, value }]
      : ['/api/rate/duel', { card_token: card.token, outcome: 'A' }];
  const res = await page.request.post(path, { data });
  expect(res.ok(), `answering the ${card.type} card over HTTP`).toBeTruthy();
  return res.json();
}

/** A fresh block: §6.1's session resumes, so `restart` is the only way to a nameable counter. */
async function openFreshBlock(page, body = {}) {
  const opened = await control(page, { restart: true, ...body });
  await openRate(page);
  return opened;
}

async function openRate(page) {
  await page.goto('/rate');
  await expect(page.getByTestId('rate-surface')).toBeVisible();
  await expect(counter(page)).toHaveText(/^\d+ of 15$/);
}

/** The modes and kinds live in the menu the screen's title opens (decision 527). */
async function openMenu(page) {
  await modeTitle(page).click();
  const menu = page.getByRole('dialog', { name: 'How to rate' });
  await expect(menu).toBeVisible();
  return menu;
}

async function closeSheet(sheet) {
  await sheet.getByRole('button', { name: 'Done', exact: true }).click();
  await expect(sheet).toHaveCount(0);
}

/** The mix sits behind the progress row's meter (decision 528); read it, then close it again. */
async function inMix(page, read) {
  await page.getByTestId('rate-mix').click();
  const mix = page.getByRole('dialog', { name: 'Your mix' });
  await expect(mix).toBeVisible();
  const value = await read(mix);
  await closeSheet(mix);
  return value;
}

const ratingsSoFar = (page) => inMix(page, (mix) => mix.getByTestId('rate-balance-total').textContent());

/** Find sits in the menu the screen's title opens (decision 528). */
async function openFind(page) {
  const menu = await openMenu(page);
  await menu.getByTestId('rate-find-toggle').click();
  const find = page.getByRole('dialog', { name: 'Rate a title you know' });
  await expect(find).toBeVisible();
  return { menu, find };
}

/** Choose a mode and wait for the redraw: `aria-pressed` flips with the new card. */
async function chooseMode(page, mode) {
  const menu = await openMenu(page);
  const row = page.getByTestId(`rate-mode-${mode}`);
  await row.click();
  await expect(row).toHaveAttribute('aria-pressed', 'true');
  await closeSheet(menu);
}

/**
 * Answer the card as a person does and return its type. Proposal 42's reveal holds the previous
 * card and a frozen counter for ~1.2 s, so this waits it out and asserts the counter reaches the
 * slot the write's response names.
 */
async function tapAnswer(page, { value = 2 } = {}) {
  await expect(page.getByTestId('rate-reveal')).toHaveCount(0);
  const sweep = (await sweepCard(page).count()) > 0;
  const route = sweep ? '/api/rate/verdict' : '/api/rate/duel';
  const written = page.waitForResponse(
    (res) => res.url().includes(route) && res.request().method() === 'POST'
  );
  if (sweep) {
    await page.getByTestId(`rate-verdict-${value}`).click();
  } else {
    await page.getByTestId('rate-duel-TIE').click();
  }
  const { session } = await (await written).json();
  // Slot 1 after an answer is a roll: the fifteenth ends its block on a screen of its own.
  if (session.block.slot === 1) await expect(page.getByTestId('rate-done')).toBeVisible();
  else await expect(counter(page)).toHaveText(`${session.block.slot} of 15`);
  return sweep ? 'sweep' : 'battle';
}

// --- the member this file rates as ------------------------------------------------------------

async function createMember(page) {
  const name = `rate-e2e-${Date.now()}`;
  const res = await page.request.post('/api/admin/users', { data: { name, role: 'member' } });
  expect(res.status(), 'the admin adds a household member (§3.1)').toBe(201);
  return { name, otp: (await res.json()).one_time_password };
}

/** A member with a virgin queue; the admin makes them, so this signs in as the admin first. */
async function switchToFreshMember(page) {
  await page.request.post('/api/auth/logout');
  await signedIn(page);
  await signInAsMember(page, await createMember(page));
}

/** An unrated title not on the table, looked up: the queue order depends on the household. */
async function findUnrated(page) {
  const onTable = (await envelope(page)).card?.title?.id;
  for (const name of ['Heat', 'Prisoners', 'Paddington 2', 'Tampopo', 'Severance', 'The Bear']) {
    const res = await page.request.get(`/api/rate/search?q=${encodeURIComponent(name)}`);
    expect(res.ok(), 'GET /api/rate/search').toBeTruthy();
    const hit = (await res.json()).items.find(
      (item) => item.name === name && !item.rated && item.id !== onTable
    );
    if (hit) return hit;
  }
  throw new Error('every fixture title is rated or on the table, so there is nothing to find');
}

async function signInAsMember(page, member) {
  await page.request.post('/api/auth/logout');
  const login = await page.request.post('/api/auth/login', {
    data: { name: member.name, password: member.otp }
  });
  expect(login.ok(), 'the one-time password signs the new member in').toBeTruthy();
  expect(
    (await login.json()).must_change_password,
    '§3.1 locks a new account to a password change at first login'
  ).toBe(true);
  const changed = await page.request.post('/api/auth/password', {
    data: { current_password: member.otp, new_password: MEMBER_PASSWORD }
  });
  expect(changed.ok(), 'setting a password unlocks the rest of the app').toBeTruthy();
}

test.describe('rate', () => {
  /** @type {import('@playwright/test').Page} */
  let page;

  test.beforeAll(async ({ browser, baseURL }) => {
    page = await browser.newPage({ baseURL });
    await signedIn(page);
    await signInAsMember(page, await createMember(page));
  });

  test.afterAll(async () => {
    await page?.close();
  });

  test('a fresh session opens in Mix, at 1 of 15, on a sweep card that says why it is here', async () => {
    // §6.1: "**Modes:** **Mix** (default — alternates sweep and battle); blocks of 15".
    await openRate(page);

    const menu = await openMenu(page);
    await expect(page.getByTestId('rate-mode-mix')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByTestId('rate-mode-sweep')).toHaveAttribute('aria-pressed', 'false');
    await expect(page.getByTestId('rate-mode-battle')).toHaveAttribute('aria-pressed', 'false');
    await closeSheet(menu);

    await expectAt(page, 1, 'Mixed');
    await expect(sweepCard(page)).toBeVisible();
    await expect(page.getByTestId('rate-card-title')).not.toBeEmpty();

    // §6.8's one-line why, a sentence with no probability in it, "Why these?" after it
    // (decisions 486, 519 and 528).
    await expect(page.getByTestId('rate-queue-reason')).toHaveText(/^[A-Z][^.]*[^.\s]$/);
    await expect(page.getByTestId('rate-queue-reason')).not.toContainText(/%|queued because/);

    // Decision 35: the chip stays on screen, disabled (decision 528 drops the line under it).
    await expect(undoChip(page)).toBeDisabled();
    await expect(undoChip(page)).toHaveAttribute('data-undo-reason', 'empty');
    await expect(page.getByTestId('rate-undo-reason')).toHaveCount(0);
  });

  test('the sweep card carries no model belief, and the reveal arrives only with the write', async () => {
    // §6.1: "Prediction reveal strictly *after* the tap (anchoring; Cosley 2003)." On the wire
    // AND in the DOM, which fail independently.
    await openRate(page);
    const before = await envelope(page);
    expect(before.card.type).toBe('sweep');
    expect(before.reveal, 'nothing predicted before the tap').toBeNull();

    const keys = keysOf(before.card);
    for (const key of BELIEF_KEYS) {
      expect(keys.has(key), `the sweep card must not carry '${key}' before the answer`).toBe(
        false
      );
    }

    const card = sweepCard(page);
    await expect(card).toBeVisible();
    // By pattern, so a newly added annotation trips it too.
    await expect(
      card.locator(
        '[data-testid*="reveal"], [data-testid*="model"], [data-testid*="tier"], ' +
          '[data-testid*="score"], [data-tier], [data-cdf]'
      )
    ).toHaveCount(0);
    await expect(page.getByTestId('rate-model-log')).toHaveCount(0);
    await expect(page.getByTestId('rate-card-meta')).toHaveText(/^\d{4}( · .+)?$/);
    await expect(page.getByTestId('rate-queue-reason')).not.toHaveText(/guess|predict/i);
    for (const [value, label] of [
      [0, 'Disliked'],
      [1, 'Fine'],
      [2, 'Liked']
    ]) {
      await expect(page.getByTestId(`rate-verdict-${value}`)).toHaveText(label);
    }

    const written = page.waitForResponse((res) => res.url().includes('/api/rate/verdict'));
    await page.getByTestId('rate-verdict-1').click();
    const body = await (await written).json();
    expect(body.reveal, 'the reveal rides on the verdict response and on no other').toBeTruthy();

    const reveal = page.getByTestId('rate-reveal');
    await expect(reveal).toBeVisible();
    // Proposal 153: before the first fit the reveal is suppressed with its reason.
    if ((await reveal.getAttribute('data-reveal-available')) === 'true') {
      await expect(reveal).toContainText(/we'd have guessed/);
      await expect(reveal).not.toContainText(/cdf|\d\.\d/);
    } else {
      await expect(reveal).toContainText('no guess yet - rate a few more first');
    }
  });

  test('the class-balance widget counts from the first rating and holds its warning until 15', async () => {
    // §5.2's 60% line, armed at fifteen ratings (decision 491): past the line and under the floor,
    // it only says when the check begins. The warning itself needs more than the fixture's titles.
    // Both sit in the sheet the progress row's meter opens (decision 528).
    await expect(page.getByTestId('rate-reveal')).toHaveCount(0);
    await inMix(page, async (mix) => {
      await expect(mix.getByTestId('rate-balance-total')).toHaveText('1 rating');
      await expect(mix.getByTestId('rate-balance')).toHaveAttribute('data-warn', 'false');
      await expect(mix.getByTestId('rate-balance-arming')).toHaveText(ARMING);
    });

    await tapAnswer(page, { value: 0 });
    expect(await ratingsSoFar(page)).toBe('2 ratings');

    await tapAnswer(page, { value: 0 }); // 2 disliked of 3 — 66.7%, past the line, under the floor
    await expect(page.getByTestId('rate-balance-chip')).toHaveCount(0);
    await inMix(page, async (mix) => {
      await expect(mix.getByTestId('rate-balance-total')).toHaveText('3 ratings');
      await expect(mix.getByTestId('rate-balance')).toHaveAttribute('data-warn', 'false');
      await expect(mix.getByTestId('rate-balance-warning')).toHaveCount(0);
      await expect(mix.getByTestId('rate-balance-arming')).toHaveText(ARMING);
    });
    expect((await envelope(page)).class_balance.arms_at).toBe(15);
  });

  test('Mix serves single titles until 15 ratings stand, and its counter says so', async () => {
    // Decision 492: Mix alternates only once a block of ratings stands; the alternation itself
    // is beyond the fixture and is covered in `test_rate_session.py`.
    await openFreshBlock(page);
    await expectAt(page, 1, 'Mixed');
    const menu = await openMenu(page);
    await expect(page.getByTestId('rate-mode-mix')).toContainText('the pairs start at 15 ratings');
    await closeSheet(menu);

    expect(await tapAnswer(page, { value: 2 })).toBe('sweep');
    await expectAt(page, 2, 'Mixed');
    await expect(sweepCard(page)).toBeVisible();
    await expect(battleCard(page)).toHaveCount(0);
    await expect(page.getByTestId('rate-substituted')).toHaveCount(0);
    expect((await envelope(page)).session.block.serving).toBe('sweep');
  });

  test('Undo pops a verdict, restores the exact card, and takes the label back with it', async () => {
    // Decision 35: "a bounded observation journal with compensating writes". The title, since a
    // neighbouring card would look identical.
    await openFreshBlock(page);
    await expect(sweepCard(page)).toBeVisible();
    const title = await page.getByTestId('rate-card-title').textContent();
    const labelsBefore = await ratingsSoFar(page);

    await tapAnswer(page);
    await expect(page.getByTestId('rate-reveal')).toHaveCount(0);
    expect(await ratingsSoFar(page)).not.toBe(labelsBefore);
    await expect(undoChip(page)).toBeEnabled();
    await expect(undoChip(page)).toHaveAttribute('data-undo-kind', 'verdict');
    await expect(undoChip(page)).toHaveAttribute('aria-label', 'Undo the last rating');

    await undoChip(page).click();
    await expect(sweepCard(page)).toBeVisible();
    await expect(page.getByTestId('rate-card-title')).toHaveText(title);
    await expectAt(page, 1, 'Mixed');
    expect(await ratingsSoFar(page)).toBe(labelsBefore);
    await expect(undoChip(page)).toBeDisabled();
    await expect(undoChip(page)).toHaveAttribute('data-undo-reason', 'empty');
  });

  test('a title you know can be found on Rate and rated on its own card', async () => {
    // Search pins the pick to the head of the queue, so it is still rated on §6.1's own card.
    const unrated = await findUnrated(page);
    await openRate(page);
    await openFind(page);
    await page.getByTestId('rate-find-input').fill(unrated.name);
    const hit = page.locator(`[data-testid="rate-find-hit"][data-title-id="${unrated.id}"]`);
    await expect(hit).toBeEnabled();
    await hit.click();

    // The pick closes the find sheet and the menu under it.
    await expect(page.getByTestId('rate-card-title')).toHaveText(unrated.name);
    await expect(page.getByTestId('rate-queue-reason')).toHaveText('You picked this one');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await tapAnswer(page, { value: 1 });

    const { menu, find } = await openFind(page);
    await page.getByTestId('rate-find-input').fill(unrated.name);
    const rated = page.locator(`[data-testid="rate-find-hit"][data-title-id="${unrated.id}"]`);
    await expect(rated).toBeDisabled();
    await expect(rated).toContainText('You rated it fine');
    await closeSheet(find);
    await closeSheet(menu);
  });

  test('Undo pops a duel too, and the same pair comes back rather than a reshuffled one', async () => {
    // Decision 35: "the most recent observation of ANY kind", and the pair verbatim. A second
    // member with every film liked, since a first sitting's battles skip disliked (decision 493).
    await switchToFreshMember(page);
    await control(page, { restart: true, mode: 'sweep', kinds: ['movie'] });
    for (let card = (await envelope(page)).card; card?.type === 'sweep'; ) {
      await answerOverHttp(page, { value: 2 });
      card = (await envelope(page)).card;
    }

    await openFreshBlock(page, { mode: 'sweep', kinds: ['movie', 'series'] });
    await expect(sweepCard(page)).toBeVisible();
    await tapAnswer(page);
    await chooseMode(page, 'battle');
    await expect(battleCard(page)).toBeVisible();
    await expectAt(page, 2, 'Pairs');
    const left = await page.getByTestId('rate-battle-left').getAttribute('data-title-id');
    const right = await page.getByTestId('rate-battle-right').getAttribute('data-title-id');

    await tapAnswer(page);
    await expectAt(page, 3, 'Pairs');

    await undoChip(page).click();
    await expect(battleCard(page)).toBeVisible();
    await expect(page.getByTestId('rate-battle-left')).toHaveAttribute('data-title-id', left);
    await expect(page.getByTestId('rate-battle-right')).toHaveAttribute('data-title-id', right);
    await expectAt(page, 2, 'Pairs');
    // One pop, not a rewind.
    await expect(undoChip(page)).toBeEnabled();
    await expect(undoChip(page)).toHaveAttribute('data-undo-kind', 'verdict');
  });

  test('Not seen under a film swaps it, keeps the other, writes no duel, and does not advance', async () => {
    // §6.1: a correction "sets that side `unseen`, swaps it out of the pair, **writes no duel
    // row**, syncs per §7.3, covered by the persistent Undo." One tap under each film, and the
    // other film stays for a new partner (decision 528). Films only: the one band deep enough.
    await openFreshBlock(page, { kinds: ['movie'] });
    await chooseMode(page, 'battle');
    await expect(battleCard(page)).toBeVisible();

    const before = await counter(page).textContent();
    const left = await page.getByTestId('rate-battle-left').getAttribute('data-title-id');
    const right = await page.getByTestId('rate-battle-right').getAttribute('data-title-id');
    const served = (await envelope(page)).card;
    expect([String(served.left.id), String(served.right.id)]).toEqual([left, right]);
    expect(await seenState(page, left)).toBe('seen');

    // A poster only shows the film: it never answers (decision 528).
    await page.getByTestId('rate-battle-left').click();
    const peek = page.getByRole('dialog', { name: 'About this film' });
    await expect(peek).toContainText(served.left.name);
    await expect(peek).toContainText('Looking never counts as an answer.');
    await closeSheet(peek);
    await expect(counter(page)).toHaveText(before);
    await expect(page.getByTestId('rate-battle-left')).toHaveAttribute('data-title-id', left);

    await expect(page.getByTestId('rate-correction-both')).toHaveCount(0);
    await expect(page.getByTestId('rate-correction-left')).toHaveAccessibleName(
      `Not seen: ${served.left.name}`
    );
    await page.getByTestId('rate-correction-left').click();

    await expect(page.getByTestId('rate-battle-left')).not.toHaveAttribute('data-title-id', left);
    await expect(page.getByTestId('rate-battle-right')).toHaveAttribute('data-title-id', right);
    expect(await seenState(page, left), 'the corrected side goes unseen (§4.2)').toBe('unseen');
    expect(await seenState(page, right), 'the other side is untouched').toBe('seen');

    // "A correction is a repair of the question, not an answer to it": the counter does not move.
    await expect(counter(page)).toHaveText(before);
    await expect(undoChip(page)).toHaveAttribute('data-undo-kind', 'correction');
    await expect(undoChip(page)).toHaveAttribute('aria-label', 'Undo the last not seen');

    await undoChip(page).click();
    await expect(page.getByTestId('rate-battle-left')).toHaveAttribute('data-title-id', left);
    expect(await seenState(page, left)).toBe('seen');
    await expect(counter(page)).toHaveText(before);

    // P marks the right film not seen, as the pill under it does.
    await page.keyboard.press('p');
    await expect(page.getByTestId('rate-battle-right')).not.toHaveAttribute('data-title-id', right);
    await expect(page.getByTestId('rate-battle-left')).toHaveAttribute('data-title-id', left);
    expect(await seenState(page, right)).toBe('unseen');
    await expect(counter(page)).toHaveText(before);

    await undoChip(page).click();
    await expect(page.getByTestId('rate-battle-right')).toHaveAttribute('data-title-id', right);
    expect(await seenState(page, right)).toBe('seen');
  });

  test('five steps answer a pair in one tap, and only Much more counts for more', async () => {
    // Decision 528 retires the Clear favourite switch (decision 520): the session holds no switch,
    // and "Much more" is decisive for its one answer.
    await openFreshBlock(page);
    await chooseMode(page, 'battle');
    await expect(battleCard(page)).toBeVisible();
    await expect(page.getByTestId('rate-battle-question')).toHaveText('Which did you enjoy more?');
    await expect(page.getByTestId('rate-decisive')).toHaveCount(0);
    expect((await envelope(page)).session).not.toHaveProperty('decisive');

    const scale = page.getByRole('group', { name: 'Which did you enjoy more?' });
    await expect(scale.getByRole('button')).toHaveText(['Much more', 'More', 'Same', 'More', 'Much more']);
    const written = page.waitForRequest(
      (req) => req.url().includes('/api/rate/duel') && req.method() === 'POST'
    );
    await page.getByTestId('rate-duel-A-much').click();
    expect((await written).postDataJSON()).toMatchObject({ outcome: 'A', decisive: true });
    await expect(undoChip(page)).toHaveAttribute('data-undo-kind', 'duel');
  });

  test('the kind toggles are either or both, and the empty selection is refused, not sent', async () => {
    // Decision 18: "kind is two toggles, either or both, never neither". The control must never
    // send the empty selection the server would 422. They sit in the title's menu (decision 527).
    await openFreshBlock(page);
    const menu = await openMenu(page);
    const movie = page.getByTestId('rate-kind-movie');
    const series = page.getByTestId('rate-kind-series');
    await expect(movie).toHaveAttribute('aria-pressed', 'true');
    await expect(series).toHaveAttribute('aria-pressed', 'true');

    await movie.click();
    await expect(movie).toHaveAttribute('aria-pressed', 'false');
    await expect(series).toHaveAttribute('aria-pressed', 'true');

    let sent = 0;
    const watch = (req) => {
      if (req.method() === 'POST' && req.url().includes('/api/rate/session')) sent++;
    };
    page.on('request', watch);
    await series.click();
    // Nothing happening has no event, so a fixed wait longer than a round trip.
    await page.waitForTimeout(400);
    page.off('request', watch);

    expect(sent, 'the last active toggle does not turn off, and nothing is sent').toBe(0);
    await expect(series).toHaveAttribute('aria-pressed', 'true');
    await expect(movie).toHaveAttribute('aria-pressed', 'false');
    await expect(page.getByTestId('rate-error')).toHaveCount(0);
    expect((await envelope(page)).session.kinds).toEqual(['series']);

    await movie.click();
    await expect(movie).toHaveAttribute('aria-pressed', 'true');
    expect((await envelope(page)).session.kinds).toEqual(['movie', 'series']);
    await closeSheet(menu);
  });

  test('the counter runs to 15, the block ends on its own screen, and Undo stops at the edge', async () => {
    // Decision 35: "the chip disables visibly, not silently, at the boundary", which decisions 174
    // and 199 place at the first observation of the next block; decision 527 gives a block's end a
    // screen of its own. Fourteen answers over HTTP, the fifteenth a tap. A third member, with all
    // eight titles unrated.
    await switchToFreshMember(page);
    const opened = await openFreshBlock(page);
    const block = opened.session.block.index;
    for (let i = 0; i < 14; i++) {
      expect(await answerOverHttp(page), 'the queue must outlast the block').not.toBeNull();
    }

    await openRate(page);
    await expect(counter(page)).toHaveText('15 of 15');
    await expect(undoChip(page)).toBeEnabled();

    const fifteenth = await tapAnswer(page);

    const done = page.getByTestId('rate-done');
    await expect(done.getByRole('heading', { name: "That's 15." })).toBeVisible();
    await expect(counter(page)).toHaveText('15 of 15');
    await expect(battleCard(page)).toHaveCount(0);
    await expect(sweepCard(page)).toHaveCount(0);
    expect((await envelope(page)).session.block.index, 'the block rolled').toBe(block + 1);

    // Decision 199: the roll is not the commit; the fifteenth stays retractable until the
    // sixteenth lands.
    await expect(undoChip(page)).toBeEnabled();
    // The kind follows the card tapped: `tapAnswer` answers a battle with Same, and a tie is its
    // own journal kind. Slot 15's card type is not fixed (decision 200).
    await expect(undoChip(page)).toHaveAttribute(
      'data-undo-kind',
      fifteenth === 'sweep' ? 'verdict' : 'tie'
    );

    await page.getByTestId('rate-done-more').click();
    await expect(done).toHaveCount(0);
    // Every card since slot 8 is the drained state's pairs; the title still names Mix.
    await expectAt(page, 1, 'Mixed');
    await expect(battleCard(page)).toBeVisible();

    // The sixteenth commits the block it ended: retracting it cannot reach back past slot 1.
    await tapAnswer(page);
    await undoChip(page).click();
    await expect(counter(page)).toHaveText('1 of 15');

    await expect(undoChip(page)).toBeDisabled();
    await expect(undoChip(page)).toHaveAttribute('data-undo-reason', 'block_boundary');
  });
});
