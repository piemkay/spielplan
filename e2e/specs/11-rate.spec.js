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

const BOUNDARY_REASON = 'undo reaches back to the start of this block of 15 and no further';

// --- reading the surface ---------------------------------------------------------------------

const counter = (page) => page.getByTestId('rate-counter');
const sweepCard = (page) => page.getByTestId('rate-sweep-card');
const battleCard = (page) => page.getByTestId('rate-battle-card');
const undoChip = (page) => page.getByTestId('rate-undo');

/** The counter line, exact, since its parts must move together. It names the mode (decision 519). */
const counterLine = (slot, mode, kinds = 'film + series') =>
  `${slot} / 15 this block · ${kinds} · ${mode}`;

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
  await expect(counter(page)).toContainText('this block');
}

/** Choose a mode and wait for the redraw: `aria-pressed` flips with the new card. */
async function chooseMode(page, mode) {
  const pill = page.getByTestId(`rate-mode-${mode}`);
  await pill.click();
  await expect(pill).toHaveAttribute('aria-pressed', 'true');
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
    await page.getByTestId('rate-strip-tie').click();
  }
  const { session } = await (await written).json();
  await expect(counter(page)).toContainText(`${session.block.slot} / 15 this block`);
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
    const config = await (await page.request.get('/api/config')).json();
    test.skip(!config.has_bundle, 'needs an imported bundle — run 01-first-boot first');
    await signInAsMember(page, await createMember(page));
  });

  test.afterAll(async () => {
    await page?.close();
  });

  test('a fresh session opens in Mix, at 1 / 15, on a sweep card that says why it is here', async () => {
    // §6.1: "**Modes:** **Mix** (default — alternates sweep and battle); blocks of 15".
    await openRate(page);

    await expect(page.getByTestId('rate-mode-mix')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByTestId('rate-mode-sweep')).toHaveAttribute('aria-pressed', 'false');
    await expect(page.getByTestId('rate-mode-battle')).toHaveAttribute('aria-pressed', 'false');

    await expect(counter(page)).toHaveText(counterLine(1, 'Mixed'));
    await expect(sweepCard(page)).toBeVisible();
    await expect(page.getByTestId('rate-card-title')).not.toBeEmpty();

    // §6.8's one-line why, a whole sentence with no probability in it (decisions 486, 519).
    await expect(page.getByTestId('rate-queue-reason')).toHaveText(/^[A-Z].*\.$/);
    await expect(page.getByTestId('rate-queue-reason')).not.toContainText(/%|queued because/);

    // Decision 35: the chip disables visibly, not silently.
    await expect(undoChip(page)).toBeDisabled();
    await expect(undoChip(page)).toHaveAttribute('data-undo-reason', 'empty');
    await expect(page.getByTestId('rate-undo-reason')).toHaveText(
      'nothing to undo in this block'
    );
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
      [0, 'disliked'],
      [1, 'fine'],
      [2, 'liked']
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
    await expect(page.getByTestId('rate-balance-total')).toHaveText('1 rating');
    await expect(page.getByTestId('rate-balance')).toHaveAttribute('data-warn', 'false');
    await expect(page.getByTestId('rate-balance-arming')).toHaveText(ARMING);

    await tapAnswer(page, { value: 0 });
    await expect(page.getByTestId('rate-balance-total')).toHaveText('2 ratings');

    await tapAnswer(page, { value: 0 }); // 2 disliked of 3 — 66.7%, past the line, under the floor
    await expect(page.getByTestId('rate-balance-total')).toHaveText('3 ratings');
    await expect(page.getByTestId('rate-balance')).toHaveAttribute('data-warn', 'false');
    await expect(page.getByTestId('rate-balance-warning')).toHaveCount(0);
    await expect(page.getByTestId('rate-balance-arming')).toHaveText(ARMING);
    expect((await envelope(page)).class_balance.arms_at).toBe(15);
  });

  test('Mix serves single titles until 15 ratings stand, and its counter says so', async () => {
    // Decision 492: Mix alternates only once a block of ratings stands; the alternation itself
    // is beyond the fixture and is covered in `test_rate_session.py`.
    await openFreshBlock(page);
    await expect(counter(page)).toHaveText(counterLine(1, 'Mixed'));
    await expect(page.getByTestId('rate-mode-note')).toContainText('the pairs start at 15 ratings');

    expect(await tapAnswer(page, { value: 2 })).toBe('sweep');
    await expect(counter(page)).toHaveText(counterLine(2, 'Mixed'));
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
    const labelsBefore = await page.getByTestId('rate-balance-total').textContent();

    await tapAnswer(page);
    await expect(page.getByTestId('rate-balance-total')).not.toHaveText(labelsBefore);
    await expect(undoChip(page)).toBeEnabled();
    await expect(undoChip(page)).toHaveAttribute('data-undo-kind', 'verdict');
    await expect(undoChip(page)).toContainText('undo rating');

    await undoChip(page).click();
    await expect(sweepCard(page)).toBeVisible();
    await expect(page.getByTestId('rate-card-title')).toHaveText(title);
    await expect(counter(page)).toHaveText(counterLine(1, 'Mixed'));
    await expect(page.getByTestId('rate-balance-total')).toHaveText(labelsBefore);
    await expect(undoChip(page)).toBeDisabled();
    await expect(undoChip(page)).toHaveAttribute('data-undo-reason', 'empty');
  });

  test('a title you know can be found on Rate and rated on its own card', async () => {
    // Search pins the pick to the head of the queue, so it is still rated on §6.1's own card.
    const unrated = await findUnrated(page);
    await openRate(page);
    await page.getByTestId('rate-find-toggle').click();
    await page.getByTestId('rate-find-input').fill(unrated.name);
    const hit = page.locator(`[data-testid="rate-find-hit"][data-title-id="${unrated.id}"]`);
    await expect(hit).toBeEnabled();
    await hit.click();

    await expect(page.getByTestId('rate-card-title')).toHaveText(unrated.name);
    await expect(page.getByTestId('rate-queue-reason')).toHaveText('You picked this one.');
    await expect(page.getByTestId('rate-find')).toHaveCount(0);
    await tapAnswer(page, { value: 1 });

    await page.getByTestId('rate-find-toggle').click();
    await page.getByTestId('rate-find-input').fill(unrated.name);
    const rated = page.locator(`[data-testid="rate-find-hit"][data-title-id="${unrated.id}"]`);
    await expect(rated).toBeDisabled();
    await expect(rated).toContainText('you rated it fine');
    await page.getByTestId('rate-find-toggle').click();
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
    await expect(counter(page)).toHaveText(counterLine(2, 'Pairs'));
    const left = await page.getByTestId('rate-battle-left').getAttribute('data-title-id');
    const right = await page.getByTestId('rate-battle-right').getAttribute('data-title-id');

    await tapAnswer(page);
    await expect(counter(page)).toHaveText(counterLine(3, 'Pairs'));

    await undoChip(page).click();
    await expect(battleCard(page)).toBeVisible();
    await expect(page.getByTestId('rate-battle-left')).toHaveAttribute('data-title-id', left);
    await expect(page.getByTestId('rate-battle-right')).toHaveAttribute('data-title-id', right);
    await expect(counter(page)).toHaveText(counterLine(2, 'Pairs'));
    // One pop, not a rewind.
    await expect(undoChip(page)).toBeEnabled();
    await expect(undoChip(page)).toHaveAttribute('data-undo-kind', 'verdict');
  });

  test('the corrections row swaps the named side, writes no duel, and does not advance', async () => {
    // §6.1: "`not seen: [left] [both] [right]` → sets that side `unseen`, swaps it out of the
    // pair (`both` swaps the whole pair), **writes no duel row**, syncs per §7.3, covered by
    // the persistent Undo." Films only: the one band deep enough to redraw from.
    await openFreshBlock(page, { kinds: ['movie'] });
    await chooseMode(page, 'battle');
    await expect(battleCard(page)).toBeVisible();

    const before = await counter(page).textContent();
    const left = await page.getByTestId('rate-battle-left').getAttribute('data-title-id');
    const right = await page.getByTestId('rate-battle-right').getAttribute('data-title-id');
    const served = (await envelope(page)).card;
    expect([String(served.left.id), String(served.right.id)]).toEqual([left, right]);
    expect(await seenState(page, left)).toBe('seen');

    await expect(page.getByTestId('rate-corrections')).toContainText('not seen:');
    for (const side of ['left', 'both', 'right']) {
      await expect(page.getByTestId(`rate-correction-${side}`)).toBeVisible();
    }
    await page.getByTestId('rate-correction-left').click();

    await expect(page.getByTestId('rate-battle-left')).not.toHaveAttribute('data-title-id', left);
    await expect(page.getByTestId('rate-battle-right')).toHaveAttribute('data-title-id', right);
    expect(await seenState(page, left), 'the corrected side goes unseen (§4.2)').toBe('unseen');
    expect(await seenState(page, right), 'the other side is untouched').toBe('seen');

    // "A correction is a repair of the question, not an answer to it": the counter does not move.
    await expect(counter(page)).toHaveText(before);
    await expect(undoChip(page)).toHaveAttribute('data-undo-kind', 'correction');
    await expect(undoChip(page)).toContainText('undo not seen');

    await undoChip(page).click();
    await expect(page.getByTestId('rate-battle-left')).toHaveAttribute('data-title-id', left);
    expect(await seenState(page, left)).toBe('seen');
    await expect(counter(page)).toHaveText(before);

    await page.getByTestId('rate-correction-both').click();
    await expect(page.getByTestId('rate-battle-left')).not.toHaveAttribute('data-title-id', left);
    await expect(page.getByTestId('rate-battle-right')).not.toHaveAttribute(
      'data-title-id',
      right
    );
    expect(await seenState(page, left)).toBe('unseen');
    expect(await seenState(page, right)).toBe('unseen');
    await expect(counter(page)).toHaveText(before);
    await expect(undoChip(page)).toHaveAttribute('data-undo-kind', 'correction');

    await undoChip(page).click();
    await expect(page.getByTestId('rate-battle-left')).toHaveAttribute('data-title-id', left);
    await expect(page.getByTestId('rate-battle-right')).toHaveAttribute('data-title-id', right);
    expect(await seenState(page, left)).toBe('seen');
    expect(await seenState(page, right)).toBe('seen');
  });

  test('the clear-favourite switch holds for its pair across a reload, and is off for the next', async () => {
    // Decision 520: the server holds the switch, so a discarded tab keeps it; it belongs to the
    // pair on the table.
    await openFreshBlock(page);
    await chooseMode(page, 'battle');
    await expect(battleCard(page)).toBeVisible();
    await expect(page.getByTestId('rate-battle-question')).toHaveText('Which did you enjoy more?');

    const toggle = page.getByTestId('rate-decisive');
    await expect(toggle).toHaveAttribute('aria-checked', 'false');
    await expect(toggle).toContainText('clear favourite');
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-checked', 'true');
    await expect(page.getByTestId('rate-decisive-why')).toContainText('resets for the next pair');

    await openRate(page);
    await expect(page.getByTestId('rate-decisive')).toHaveAttribute('aria-checked', 'true');
    expect((await envelope(page)).session.decisive, 'stored on the session, not in the tab').toBe(
      true
    );

    await tapAnswer(page);
    expect((await envelope(page)).session.decisive, 'the next pair starts with it off').toBe(false);
    if ((await battleCard(page).count()) > 0) {
      await expect(page.getByTestId('rate-decisive')).toHaveAttribute('aria-checked', 'false');
    }
  });

  test('the kind toggles are either or both, and the empty selection is refused, not sent', async () => {
    // Decision 18: "kind is two toggles, either or both, never neither". The control must never
    // send the empty selection the server would 422.
    await openFreshBlock(page);
    const movie = page.getByTestId('rate-kind-movie');
    const series = page.getByTestId('rate-kind-series');
    await expect(movie).toHaveAttribute('aria-pressed', 'true');
    await expect(series).toHaveAttribute('aria-pressed', 'true');

    await movie.click();
    await expect(movie).toHaveAttribute('aria-pressed', 'false');
    await expect(series).toHaveAttribute('aria-pressed', 'true');
    await expect(counter(page)).toContainText('· series ·');
    // The count is per kind, and says so.
    await expect(page.getByTestId('rate-balance-total')).toHaveText(/^\d+ series ratings?$/);

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
    await expect(counter(page)).toContainText('· film + series ·');
    expect((await envelope(page)).session.kinds).toEqual(['movie', 'series']);
  });

  test('the counter runs to 15 and rolls, and Undo then disables visibly at the boundary', async () => {
    // Decision 35: "the chip disables visibly, not silently, at the boundary", which decisions 174
    // and 199 place at the first observation of the next block. Fourteen answers over HTTP, the
    // fifteenth a tap. A third member, with all eight titles unrated.
    await switchToFreshMember(page);
    const opened = await openFreshBlock(page);
    const block = opened.session.block.index;
    for (let i = 0; i < 14; i++) {
      expect(await answerOverHttp(page), 'the queue must outlast the block').not.toBeNull();
    }

    await openRate(page);
    await expect(counter(page)).toContainText('15 / 15 this block');
    await expect(undoChip(page)).toBeEnabled();

    const fifteenth = await tapAnswer(page);

    await expect(counter(page)).toContainText('1 / 15 this block');
    expect((await envelope(page)).session.block.index, 'the block rolled').toBe(block + 1);
    // Every card since slot 8 is the drained state's pairs; the counter still names Mix.
    await expect(counter(page)).toContainText('· Mixed');
    await expect(battleCard(page)).toBeVisible();

    // Decision 199: the roll is not the commit; the fifteenth stays retractable until the
    // sixteenth lands.
    await expect(undoChip(page)).toBeEnabled();
    // The kind follows the card tapped: `tapAnswer` answers a battle with the tie strip, and a
    // tie is its own journal kind. Slot 15's card type is not fixed (decision 200).
    await expect(undoChip(page)).toHaveAttribute(
      'data-undo-kind',
      fifteenth === 'sweep' ? 'verdict' : 'tie'
    );

    // The sixteenth commits the block it ended: retracting it cannot reach back past slot 1.
    await tapAnswer(page);
    await undoChip(page).click();
    await expect(counter(page)).toContainText('1 / 15 this block');

    await expect(undoChip(page)).toBeDisabled();
    await expect(undoChip(page)).toHaveAttribute('data-undo-reason', 'block_boundary');
    await expect(page.getByTestId('rate-undo-reason')).toHaveText(BOUNDARY_REASON);
  });
});
