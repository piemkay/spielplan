import { expect, test } from '@playwright/test';

import { signedIn } from '../helpers.js';

/**
 * §6.1's Rate surface, driven in a browser.
 *
 * Every rule this file touches already has integration coverage in
 * `backend/tests/test_rate_session.py` and `test_rate_queue.py`, and none of the eleven Rate
 * rows in `spec_coverage.toml` is closed here. What an integration test structurally cannot
 * reach is the thing this file exists for: that the *surface* obeys them — that the counter on
 * screen is the one the server advanced, that the chip which says "undo" is disabled rather
 * than inert, that the card in front of a person carries no prediction, and that the empty
 * kind selection is refused by the control rather than sent to a 422.
 *
 * The one that matters most is `the sweep card carries no model belief until the answer`.
 * §6.1 cites Cosley 2003: a card that shows the model's guess before the tap contaminates the
 * label it exists to collect, silently, and every downstream measurement inherits it. That
 * test asserts the absence on the wire AND in the DOM, because the two fail independently —
 * the server could start sending it, or a component could start rendering something it
 * already receives.
 *
 * SELF-CONTAINED SEEDING. This file makes its own household member and signs in as them. It
 * has to: §4.2's verdicts are append-only and `rate.queue` never re-serves a title the user
 * has already rated, so a shared account cannot be rewound between runs — the second run would
 * find a drained queue and quietly assert nothing. A member per run gets a virgin queue, the
 * imported `seed_list` in position order, and a block counter starting at 1.
 *
 * Serial, and one page: this is a *session*, and Playwright's default fresh context per test
 * would throw away the one the surface is built around.
 */
test.describe.configure({ mode: 'serial' });

/** The password the seeded member gets, replacing §3.1's one-time password. Never real. */
const MEMBER_PASSWORD = 'rate-e2e-member-password';

/**
 * Keys that are the model's belief about the title being rated. §6.1 forbids all of them
 * before the tap; `session.public_card` enforces it with an allow-list, and this is the list
 * read from the other side. `s`, `cdf` and `sigma` are §5.2's fitted quantities, `tier` is
 * §6.3's, and `verdict_class` is the band a battle pair was drawn from — the person's own
 * prior label, which anchors just as hard as the model's.
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

/** §6.1's warning copy, verbatim. §5.2's measured 5x lever — a paraphrase is a different claim. */
const BALANCE_WARNING =
  "Heavy on 'disliked'. Spreading across all three classes matters about five times more " +
  'than anything else you can do here.';

/** Decision 35's sentence for the boundary, which has to be legible next to the dead chip. */
const BOUNDARY_REASON = 'undo reaches back to the start of this block of 15 and no further';

// --- reading the surface ---------------------------------------------------------------------

const counter = (page) => page.getByTestId('rate-counter');
const sweepCard = (page) => page.getByTestId('rate-sweep-card');
const battleCard = (page) => page.getByTestId('rate-battle-card');
const undoChip = (page) => page.getByTestId('rate-undo');

/**
 * The counter line, spelled out. §6.1's "blocks of 15" with proposal 46's partition and the
 * card type actually on the table: `2 / 15 this block · film + series · battle`.
 *
 * Written as one exact string rather than three `toContainText` calls because the alternation
 * test's whole claim is that the slot and the served type move together — a partial match
 * would pass while they disagreed.
 */
const counterLine = (slot, serving, kinds = 'film + series') =>
  `${slot} / 15 this block · ${kinds} · ${serving}`;

/** The Rate envelope, read exactly as the surface reads it. */
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

/** Every key that appears anywhere inside a value, however deeply nested. */
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

/**
 * §6.1's controls route. Used here only to ARRANGE — every assertion in this file is made
 * against what the page renders, never against what this returns.
 */
async function control(page, body) {
  const res = await page.request.post('/api/rate/session', { data: body });
  expect(res.ok(), `POST /api/rate/session ${JSON.stringify(body)}`).toBeTruthy();
  return res.json();
}

/** Answer whatever is on the table over HTTP. Arrangement only — see `control`. */
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

/**
 * Land on Rate with a fresh block.
 *
 * §6.1's session resumes rather than restarts — "a person who closes the app at slot 7 comes
 * back to slot 7" — so `restart` is the only way to a counter a test can name. It ends the
 * live session and opens a new one, which also puts the mode back to Mix.
 */
async function openFreshBlock(page, body = {}) {
  const opened = await control(page, { restart: true, ...body });
  await openRate(page);
  return opened;
}

async function openRate(page) {
  await page.goto('/rate');
  await expect(page.getByTestId('rate-surface')).toBeVisible();
  // The counter is the readiness signal: it renders only once the first envelope has landed.
  await expect(counter(page)).toContainText('this block');
}

/**
 * Choose a mode and wait for the redraw it causes.
 *
 * §6.1's mode change "drops the card on the table — a battle pair is meaningless once Sweep is
 * selected". The old card stays rendered until the new envelope lands, and when both cards are
 * battles (a drained sweep queue substitutes one) nothing about the *shape* of the DOM changes
 * at the swap — so a test that read the pair straight after the click would read the pair it
 * was about to lose. `aria-pressed` flips from the same response the card does.
 */
async function chooseMode(page, mode) {
  const pill = page.getByTestId(`rate-mode-${mode}`);
  await pill.click();
  await expect(pill).toHaveAttribute('aria-pressed', 'true');
}

/**
 * Answer the card in front of us the way a person does, and return the type it was.
 *
 * Proposal 42's reveal holds the card *just rated* — and its counter — on screen for ~1.2 s.
 * Two consequences, and both are handled here rather than at each call site:
 *
 *   - What is rendered during a hold is the previous card, so choosing a control before the
 *     hold clears would choose against a question that has already been answered. Waiting for
 *     the reveal to go is waiting for the card in front of the person to be the current one.
 *   - The counter is frozen for the duration, so "it changed" is not a signal the tap landed.
 *     The write's own response names the slot it moved to, and the assertion is that the
 *     counter on screen becomes that slot — which is this whole file's thesis in one line.
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

/**
 * §3.1's member creation, used as seeding: "a one-time password is issued, the account is
 * locked to a password change at first login".
 */
async function createMember(page) {
  const name = `rate-e2e-${Date.now()}`;
  const res = await page.request.post('/api/setup/members', { data: { name, role: 'member' } });
  expect(res.status(), 'the admin adds a household member (§3.1)').toBe(201);
  return { name, otp: (await res.json()).one_time_password };
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
    // §6.1: "**Modes:** **Mix** (default — alternates sweep and battle); blocks of 15", and
    // slot 1 is a sweep, so the first question a new member ever sees needs no prior ratings.
    await openRate(page);

    await expect(page.getByTestId('rate-mode-mix')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByTestId('rate-mode-sweep')).toHaveAttribute('aria-pressed', 'false');
    await expect(page.getByTestId('rate-mode-battle')).toHaveAttribute('aria-pressed', 'false');

    await expect(counter(page)).toHaveText(counterLine(1, 'sweep'));
    await expect(sweepCard(page)).toBeVisible();
    await expect(page.getByTestId('rate-card-title')).not.toBeEmpty();

    // §6.8: "every shelf, recommendation, question and conflict carries a one-line why", and
    // §6.1 fixes this one's shape: "queued because: 72% likely you have seen it".
    await expect(page.getByTestId('rate-queue-reason')).toContainText(/queued because:/);

    // Decision 35: the chip disables visibly, not silently — including on the first card,
    // where there is nothing behind it to pop.
    await expect(undoChip(page)).toBeDisabled();
    await expect(undoChip(page)).toHaveAttribute('data-undo-reason', 'empty');
    await expect(page.getByTestId('rate-undo-reason')).toHaveText(
      'nothing to undo in this block'
    );
  });

  test('the sweep card carries no model belief, and the reveal arrives only with the write', async () => {
    // §6.1: "Prediction reveal strictly *after* the tap (anchoring; Cosley 2003)."
    //
    // This is the regression that would never announce itself: the surface would keep working,
    // people would keep tapping, and every label collected afterwards would be anchored on the
    // number the card showed them. So it is asserted twice over, on the wire and in the DOM,
    // because those two fail independently.
    //
    // Same card as the test above — `GET /api/rate` is idempotent, which is itself the reason
    // a refresh cannot shuffle a question out from under someone mid-answer.
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
    // Nothing on the card is a statement about the model — no reveal, no rail line, no badge.
    // Matched by pattern rather than by name so a *newly added* annotation trips it too.
    await expect(
      card.locator(
        '[data-testid*="reveal"], [data-testid*="model"], [data-testid*="tier"], ' +
          '[data-testid*="score"], [data-tier], [data-cdf]'
      )
    ).toHaveCount(0);
    await expect(page.getByTestId('rate-model-log')).toHaveCount(0);
    // §6.1's meta line is proposal 40's two facts and no third: `{year} · {runtime}`.
    await expect(page.getByTestId('rate-card-meta')).toHaveText(/^\d{4}( · .+)?$/);
    // The why-line names a queue cause, never a guess about the answer.
    await expect(page.getByTestId('rate-queue-reason')).not.toHaveText(/guess|predict/i);
    // What the person is offered is the question itself.
    for (const [value, label] of [
      [0, 'disliked'],
      [1, 'fine'],
      [2, 'liked']
    ]) {
      await expect(page.getByTestId(`rate-verdict-${value}`)).toHaveText(label);
    }

    // And now the tap. The response to the write is the *only* place the prediction rides.
    const written = page.waitForResponse((res) => res.url().includes('/api/rate/verdict'));
    await page.getByTestId('rate-verdict-1').click();
    const body = await (await written).json();
    expect(body.reveal, 'the reveal rides on the verdict response and on no other').toBeTruthy();

    const reveal = page.getByTestId('rate-reveal');
    await expect(reveal).toBeVisible();
    // Proposal 153: before the person's first fit the reveal is *suppressed with its reason*,
    // not banded — "a guess drawn from someone else's thresholds is not a prediction about
    // this user". Either shape is correct; inventing a class when there is no fit is not.
    if ((await reveal.getAttribute('data-reveal-available')) === 'true') {
      await expect(reveal).toContainText(/we'd have guessed/);
    } else {
      await expect(reveal).toContainText(/no fitted ranking|no labels of your own/);
    }
  });

  test('the class-balance warning appears when a class passes 60%, in the measured words', async () => {
    // §6.1's widget and §5.2's threshold: "a 60%-'liked' labeller gives up ~0.07 rho". One
    // 'fine' label already stands; two 'disliked' answers walk the distribution across the
    // line, and the warning has to be absent on the near side of it.
    await expect(page.getByTestId('rate-balance')).toHaveAttribute('data-warn', 'true');

    await tapAnswer(page, { value: 0 }); // 1 disliked, 1 fine — 50%, under the threshold
    await expect(page.getByTestId('rate-balance-total')).toHaveText('2 labels');
    await expect(page.getByTestId('rate-balance')).toHaveAttribute('data-warn', 'false');
    await expect(page.getByTestId('rate-balance-warning')).toHaveCount(0);

    await tapAnswer(page, { value: 0 }); // 2 disliked of 3 — 66.7%, over it
    await expect(page.getByTestId('rate-balance-total')).toHaveText('3 labels');
    await expect(page.getByTestId('rate-balance')).toHaveAttribute('data-warn', 'true');
    // Verbatim. The sentence is §5.2's measurement written down, not a message to reword.
    await expect(page.getByTestId('rate-balance-warning')).toHaveText(BALANCE_WARNING);
    await expect(page.getByTestId('rate-balance-threshold')).toContainText('60%');
  });

  test('Mix alternates on the counter, so an answered duel is followed by a sweep card', async () => {
    // §6.1: "Mix (default — alternates sweep and battle)". The card type is a function of the
    // SLOT, never of the last card served — the bug this guards against is a mode that derives
    // the next card from what was just answered, where a run of duels never returns a sweep.
    await openFreshBlock(page);
    await expect(counter(page)).toHaveText(counterLine(1, 'sweep'));
    await expect(sweepCard(page)).toBeVisible();

    expect(await tapAnswer(page, { value: 0 })).toBe('sweep');
    await expect(counter(page)).toHaveText(counterLine(2, 'battle'));
    await expect(battleCard(page)).toBeVisible();
    await expect(sweepCard(page)).toHaveCount(0);

    expect(await tapAnswer(page)).toBe('battle');
    await expect(counter(page)).toHaveText(counterLine(3, 'sweep'));
    await expect(sweepCard(page)).toBeVisible();
    await expect(battleCard(page)).toHaveCount(0);
  });

  test('Undo pops a verdict, restores the exact card, and takes the label back with it', async () => {
    // Decision 35: "a bounded observation journal with compensating writes", and §6 preamble's
    // "undo everywhere". Restoring the *neighbouring queue position* would look identical on a
    // screenshot and be a different card, so the title is what is asserted.
    await openFreshBlock(page);
    await expect(sweepCard(page)).toBeVisible();
    const title = await page.getByTestId('rate-card-title').textContent();
    const labelsBefore = await page.getByTestId('rate-balance-total').textContent();

    await tapAnswer(page);
    await expect(page.getByTestId('rate-balance-total')).not.toHaveText(labelsBefore);
    await expect(undoChip(page)).toBeEnabled();
    // The kind rides along, so "undo verdict" is a promise the person can read before tapping.
    await expect(undoChip(page)).toHaveAttribute('data-undo-kind', 'verdict');

    await undoChip(page).click();
    await expect(sweepCard(page)).toBeVisible();
    await expect(page.getByTestId('rate-card-title')).toHaveText(title);
    await expect(counter(page)).toHaveText(counterLine(1, 'sweep'));
    // The compensating write, visible: the label the tap added is gone again.
    await expect(page.getByTestId('rate-balance-total')).toHaveText(labelsBefore);
    await expect(undoChip(page)).toBeDisabled();
    await expect(undoChip(page)).toHaveAttribute('data-undo-reason', 'empty');
  });

  test('Undo pops a duel too, and the same pair comes back rather than a reshuffled one', async () => {
    // Decision 35 again, on the other card type — "the most recent observation of ANY kind".
    // `rate_observation.card` holds the pair verbatim, and a sampler that redrew would put two
    // plausible posters back on screen that the person had never been asked about.
    await tapAnswer(page); // answer the restored sweep card; slot 2 is a battle
    await expect(battleCard(page)).toBeVisible();
    const left = await page.getByTestId('rate-battle-left').getAttribute('data-title-id');
    const right = await page.getByTestId('rate-battle-right').getAttribute('data-title-id');

    await tapAnswer(page);
    await expect(counter(page)).toHaveText(counterLine(3, 'sweep'));

    await undoChip(page).click();
    await expect(battleCard(page)).toBeVisible();
    await expect(page.getByTestId('rate-battle-left')).toHaveAttribute('data-title-id', left);
    await expect(page.getByTestId('rate-battle-right')).toHaveAttribute('data-title-id', right);
    await expect(counter(page)).toHaveText(counterLine(2, 'battle'));
    // One pop, not a rewind: the verdict underneath it is still there to be undone next.
    await expect(undoChip(page)).toBeEnabled();
    await expect(undoChip(page)).toHaveAttribute('data-undo-kind', 'verdict');
  });

  test('the corrections row swaps the named side, writes no duel, and does not advance', async () => {
    // §6.1: "`not seen: [left] [both] [right]` → sets that side `unseen`, swaps it out of the
    // pair (`both` swaps the whole pair), **writes no duel row**, syncs per §7.3, covered by
    // the persistent Undo."
    //
    // Arrangement first, and it is not incidental. The redraw keeps the half the person did
    // not correct and draws the other from the titles that are *neither* — so a band holding
    // only the two on screen has no opponent left, and the slot honestly falls back to a sweep
    // rather than inventing one. Draining the sweep queue into one band is what makes the swap
    // observable *as a swap*; the fallback is `ensure_card`'s own behaviour and has its own
    // integration coverage.
    await control(page, { restart: true, mode: 'sweep' });
    for (let card = (await envelope(page)).card; card?.type === 'sweep'; ) {
      await answerOverHttp(page, { value: 0 });
      card = (await envelope(page)).card;
    }

    // Films only, so the pair can only come from the one band deep enough to repair itself —
    // §4.1 rule 5's partition doing arrangement work as well as ranking work.
    await openFreshBlock(page, { kinds: ['movie'] });
    await chooseMode(page, 'battle');
    await expect(battleCard(page)).toBeVisible();

    const before = await counter(page).textContent();
    const left = await page.getByTestId('rate-battle-left').getAttribute('data-title-id');
    const right = await page.getByTestId('rate-battle-right').getAttribute('data-title-id');
    // The posters on screen are the pair the server dealt — the correction names a side of
    // *this* card, so the sides have to be the ones it holds.
    const served = (await envelope(page)).card;
    expect([String(served.left.id), String(served.right.id)]).toEqual([left, right]);
    expect(await seenState(page, left)).toBe('seen');

    await expect(page.getByTestId('rate-corrections')).toContainText('not seen:');
    for (const side of ['left', 'both', 'right']) {
      await expect(page.getByTestId(`rate-correction-${side}`)).toBeVisible();
    }
    await page.getByTestId('rate-correction-left').click();

    // The named side, and only it. `both` is what swaps the pair.
    await expect(page.getByTestId('rate-battle-left')).not.toHaveAttribute('data-title-id', left);
    await expect(page.getByTestId('rate-battle-right')).toHaveAttribute('data-title-id', right);
    expect(await seenState(page, left), 'the corrected side goes unseen (§4.2)').toBe('unseen');
    expect(await seenState(page, right), 'the other side is untouched').toBe('seen');

    // "A correction is a repair of the question, not an answer to it" — so the counter that
    // Undo's depth is measured in does not move, and the journal names a `correction` where a
    // duel would have named itself. That is as far as a browser can see the "writes no duel
    // row" half; the row's absence is `test_a_correction_unsees_exactly_the_named_side_and_
    // writes_no_duel`'s to prove, and what is proven here is that the surface asked for a
    // correction and not for a comparison.
    await expect(counter(page)).toHaveText(before);
    await expect(undoChip(page)).toHaveAttribute('data-undo-kind', 'correction');
    await expect(undoChip(page)).toContainText('undo correction');

    // "covered by the persistent Undo" — which also puts the pair back for `both` below.
    await undoChip(page).click();
    await expect(page.getByTestId('rate-battle-left')).toHaveAttribute('data-title-id', left);
    expect(await seenState(page, left)).toBe('seen');
    await expect(counter(page)).toHaveText(before);

    // "(`both` swaps the whole pair)" — the same rule applied to two halves at once.
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

  test('the decisive toggle survives a reload — the server holds it, not the tab', async () => {
    // §6.1: "a persistent **decisive toggle** sets the margin weight (~1.6 vs 1.0)". Persistent
    // is the load-bearing word: a client-side toggle would silently reset every time the phone
    // discarded the tab, and the margins would drift with it.
    await openFreshBlock(page);
    await chooseMode(page, 'battle');
    await expect(battleCard(page)).toBeVisible();

    const toggle = page.getByTestId('rate-decisive');
    await expect(toggle).toHaveAttribute('aria-checked', 'false');
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-checked', 'true');
    // §6.1 asks the toggle to carry its own reason rather than leaving it two cards down.
    await expect(page.getByTestId('rate-decisive-why')).toHaveText(
      'a decisive pick teaches more than a hesitant one'
    );

    await openRate(page);
    await expect(page.getByTestId('rate-decisive')).toHaveAttribute('aria-checked', 'true');
    expect((await envelope(page)).session.decisive, 'stored on the session, not in the tab').toBe(
      true
    );

    await page.getByTestId('rate-decisive').click();
    await expect(page.getByTestId('rate-decisive')).toHaveAttribute('aria-checked', 'false');
  });

  test('the kind toggles are either or both, and the empty selection is refused, not sent', async () => {
    // §4.1 rule 5 / decision 18: "kind is two toggles, either or both, never neither". The
    // server answers the empty selection with a 422 — which is correct and useless to look at,
    // because the control must never produce it. A surface that sends it and renders the error
    // has turned a rule into an error message.
    await openFreshBlock(page);
    const movie = page.getByTestId('rate-kind-movie');
    const series = page.getByTestId('rate-kind-series');
    await expect(movie).toHaveAttribute('aria-pressed', 'true');
    await expect(series).toHaveAttribute('aria-pressed', 'true');

    await movie.click();
    await expect(movie).toHaveAttribute('aria-pressed', 'false');
    await expect(series).toHaveAttribute('aria-pressed', 'true');
    // Proposal 46: the counter names the active partition, so the toggle is legible from it.
    await expect(counter(page)).toContainText('· series ·');

    let sent = 0;
    const watch = (req) => {
      if (req.method() === 'POST' && req.url().includes('/api/rate/session')) sent++;
    };
    page.on('request', watch);
    await series.click();
    // The claim is that nothing happens, and there is no event for that — so the only honest
    // wait is a fixed one, comfortably longer than the round trip the other toggles make.
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
    // §6.1's "blocks of 15", and decision 35's commit: "the instant the 15th observation lands,
    // the block index moves and everything in the old block stops being undoable … the chip
    // disables visibly, not silently, at the boundary."
    //
    // The first fourteen are answered over HTTP — they are the arrangement, not the claim. The
    // fifteenth is a tap, because the roll is what has to be visible.
    const opened = await openFreshBlock(page);
    const block = opened.session.block.index;
    for (let i = 0; i < 14; i++) {
      expect(await answerOverHttp(page), 'the queue must outlast the block').not.toBeNull();
    }

    await openRate(page);
    await expect(counter(page)).toContainText('15 / 15 this block');
    await expect(undoChip(page)).toBeEnabled();

    await tapAnswer(page);

    await expect(counter(page)).toContainText('1 / 15 this block');
    expect((await envelope(page)).session.block.index, 'the block rolled').toBe(block + 1);

    // Not a button that quietly does nothing: disabled, with the server's reason beside it.
    await expect(undoChip(page)).toBeDisabled();
    await expect(undoChip(page)).toHaveAttribute('data-undo-reason', 'block_boundary');
    await expect(page.getByTestId('rate-undo-reason')).toHaveText(BOUNDARY_REASON);
  });
});
