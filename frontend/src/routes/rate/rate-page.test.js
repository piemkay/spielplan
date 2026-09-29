/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The page reads `$page.url` synchronously at init, so the store answers on subscribe; a sheet
// stays open while its history entry is in `$page.state`.
const nav = vi.hoisted(() => ({ page: null }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  nav.page = writable({ url: new URL('http://localhost/rate'), state: {} });
  return { page: nav.page };
});
vi.mock('$app/navigation', () => ({
  pushState: (_url, state) => nav.page.update((p) => ({ ...p, state }))
}));

import RatePage from './+page.svelte';
import { rate, rateTitle } from '$lib/rate.svelte.js';

const DRAINED = '[data-testid="rate-drained"]';
const SUBSTITUTED = '[data-testid="rate-substituted"]';

/** One `GET /api/rate` envelope, in the shape `rate/session.payload` sends. */
const envelope = (over = {}) => ({
  session: {
    id: 7,
    mode: 'battle',
    kinds: ['movie'],
    block: { index: 0, slot: 1, size: 15, counter: '1 of 15', serving: 'battle' }
  },
  card: null,
  drained: null,
  class_balance: {
    counts: [0, 0, 0],
    shares: [0, 0, 0],
    labels: ['disliked', 'fine', 'liked'],
    total: 0,
    warn: false,
    copy: 'rate a few more',
    threshold: 0.6
  },
  undo: { available: false, kind: null, reason: 'empty' },
  reveal: null,
  ledger: null,
  log: [],
  ...over
});

/** `rate/session.DRAINED_CAUSES`, verbatim. */
const CAUSES = {
  queue: {
    cause: 'queue',
    text: "You've rated everything we can queue right now. Battles sharpen what you've already said."
  },
  pool: {
    cause: 'pool',
    text:
      'A battle compares two titles you rated the same way, and there is no new pair to ' +
      'compare yet. Rate a few more in Sweep and the pairs start arriving.'
  },
  both: {
    cause: 'both',
    text:
      "You've rated everything we can queue right now, and there is no new pair of your " +
      'ratings left to compare either.'
  }
};

/** A sweep card the §6.0 banner's head redraw produced: a battle slot, a pool that can be full. */
const substitutedSweep = {
  type: 'sweep',
  token: 'tok-1',
  kind: 'movie',
  title: { id: 41, name: 'Heat', year: 1995, runtime_min: 170, poster_path: null, recall_aid: null },
  reason: 'queued because: you marked this seen and gave it no verdict',
  p_seen: 0.9,
  substituted_for: 'battle',
  verdict_labels: [
    [0, 'disliked'],
    [1, 'fine'],
    [2, 'liked']
  ],
  controls: ['verdict', 'not_seen', 'skip']
};

let fetchMock;
let target;
let app;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  // The store is module state shared by every case, so reset it.
  rate.booted = false;
  rate.loading = true;
  rate.busy = false;
  rate.card = null;
  rate.drained = null;
  rate.session = null;
  rate.notice = '';
  rate.error = '';
  rate.done = null;
  nav.page.update((p) => ({ ...p, state: {} }));
  target = document.createElement('div');
  document.body.appendChild(target);
});

afterEach(() => {
  if (app) unmount(app);
  app = null;
  vi.unstubAllGlobals();
  target.remove();
});

function respond(payload, status = 200) {
  fetchMock.mockResolvedValueOnce({
    ok: status < 400,
    status,
    headers: { get: () => null },
    text: async () => JSON.stringify(payload)
  });
}

// A macrotask, not counted microtasks: `api.js` is three hops deep.
async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 0));
  flushSync();
}

async function open(payload) {
  respond(payload);
  app = mount(RatePage, { target });
  await settle();
}

describe("§6.1's empty state, by cause (finding 20, M410-D8-01)", () => {
  it('does not tell a person with no pairs that there is nothing left to queue', async () => {
    // Battle selected with zero verdicts: the one state with a cause and no card at all.
    await open(envelope({ drained: CAUSES.pool }));

    const block = target.querySelector(DRAINED);
    expect(block).toBeTruthy();
    expect(block.textContent).toContain('no new pair to compare yet');
    expect(block.querySelector('h2').textContent).not.toMatch(/left to queue/i);
    expect(block.textContent).not.toMatch(/Sharpen your list/);
  });

  it('sends a person with no pairs to Sweep rather than to an empty tier board', async () => {
    await open(envelope({ drained: CAUSES.pool }));

    const block = target.querySelector(DRAINED);
    expect(block.querySelector('a[href="/rank"]')).toBeNull();

    // The CTA is a mode change, not a link: the server's sentence names Sweep.
    respond(envelope({ session: { mode: 'sweep' }, card: substitutedSweep }));
    block.querySelector('[data-testid="rate-drained-cta"]').click();
    await settle();

    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toContain('/api/rate/session');
    expect(JSON.parse(init.body).mode).toBe('sweep');
  });

  it("keeps proposal 37's end state for the queue that really is spent", async () => {
    await open(envelope({ session: { mode: 'sweep' }, drained: CAUSES.queue }));

    const block = target.querySelector(DRAINED);
    expect(block.querySelector('h2').textContent).toBe('Nothing left to queue');
    expect(block.textContent).toContain('"Sharpen your list" on the Rank page');
    expect(block.textContent).not.toMatch(/§|\bM[0-7]\b/);
    expect(block.querySelector('a[href="/rank"]')).toBeTruthy();
  });

  it('keeps it for the both-empty cause, where the queue is spent as well', async () => {
    await open(envelope({ drained: CAUSES.both }));

    const block = target.querySelector(DRAINED);
    expect(block.querySelector('h2').textContent).toBe('Nothing left to queue');
    expect(block.querySelector('a[href="/rank"]')).toBeTruthy();
  });
});

describe('the substitution line (finding 21, M410-D8-07; C4.5 of the household test)', () => {
  it('prints nothing about the slot over a sweep card that stands in for a battle', async () => {
    await open(envelope({ card: substitutedSweep }));

    expect(target.querySelector('[data-testid="rate-sweep-card"]')).toBeTruthy();
    expect(target.querySelector(SUBSTITUTED)).toBeNull();
    expect(target.textContent).not.toMatch(/was due in this slot/);
    // The title names the mode chosen, whatever card stands in.
    expect(target.querySelector('[data-testid="rate-menu"]').textContent.trim()).toBe('Pairs');
  });

  it('says in plain words why a battle stands in for a sweep', async () => {
    const battle = {
      type: 'battle',
      token: 'tok-2',
      kind: 'movie',
      left: { id: 1, name: 'Heat', year: 1995, runtime_min: 170, outcome: 'A' },
      right: { id: 2, name: 'Drive', year: 2011, runtime_min: 100, outcome: 'B' },
      reason: 'queued because: you rated both liked · random pairs build your profile best',
      substituted_for: 'sweep',
      outcomes: ['A', 'B', 'TIE'],
      corrections: { label: 'not seen', sides: ['left', 'both', 'right'] },
      controls: ['duel', 'correction', 'skip']
    };
    await open(envelope({ card: battle }));

    const line = target.querySelector(SUBSTITUTED);
    expect(line.textContent).toContain("Nothing new to rate right now — comparing titles you've");
    expect(line.textContent).not.toMatch(/sweep queue|drained/);
  });
});

describe('"a title you know" (C5.2 of the household test)', () => {
  const hits = {
    q: 'heat',
    items: [
      { id: 41, kind: 'movie', name: 'Heat', year: 1995, rated: null, is_owned: true },
      { id: 42, kind: 'movie', name: 'The Heat', year: 2013, rated: 'fine', is_owned: false }
    ]
  };
  const other = { ...substitutedSweep, title: { ...substitutedSweep.title, id: 7, name: 'Else' } };

  it('finds a title, offers only the unrated one, and pins the pick into the queue', async () => {
    await open(envelope({ session: { mode: 'mix' }, card: other }));
    // In the menu the screen's title opens (decision 528).
    target.querySelector('[data-testid="rate-menu"]').click();
    flushSync();
    target.querySelector('[data-testid="rate-find-toggle"]').click();
    flushSync();

    const input = target.querySelector('[data-testid="rate-find-input"]');
    expect(input).toBeTruthy();
    respond(hits);
    input.value = 'heat';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 300));
    await settle();

    const [searchUrl] = fetchMock.mock.calls[1];
    expect(searchUrl).toContain('/api/rate/search?q=heat');
    const buttons = target.querySelectorAll('[data-testid="rate-find-hit"]');
    expect(buttons).toHaveLength(2);
    expect(buttons[0].disabled).toBe(false);
    expect(buttons[1].disabled).toBe(true);
    expect(buttons[1].textContent).toContain('You thought it was fine');

    respond(envelope({ session: { mode: 'mix' }, card: { ...substitutedSweep, substituted_for: null } }));
    buttons[0].click();
    await settle();

    const [pinUrl] = fetchMock.mock.calls[2];
    expect(pinUrl).toContain('/api/rate?head=41');
    expect(target.querySelector('[data-testid="rate-card-title"]').textContent).toBe('Heat');
    expect(target.querySelector('[data-testid="rate-find"]')).toBeNull();
    expect(target.querySelector('[data-testid="rate-notice"]')).toBeNull();
  });

  it('says so when a pick could not be put on the table', async () => {
    await open(envelope({ session: { mode: 'mix' }, card: other }));

    // The server kept the card it had: the pick was rated a moment ago on another device.
    respond(envelope({ session: { mode: 'mix' }, card: other }));
    await rateTitle(hits.items[0]);
    await settle();
    expect(target.querySelector('[data-testid="rate-notice"]').textContent).toBe(
      "Heat can't be rated right now."
    );
  });
});

describe('the second household test on Rate (2026-09-26)', () => {
  const pairCard = {
    type: 'battle',
    token: 'tok-3',
    kind: 'movie',
    left: { id: 1, name: 'Heat', year: 1995, runtime_min: 170, outcome: 'A' },
    right: { id: 2, name: 'Drive', year: 2011, runtime_min: 100, outcome: 'B' },
    reason: 'You rated both liked',
    substituted_for: null,
    outcomes: ['A', 'B', 'TIE'],
    corrections: { label: 'not seen', sides: ['left', 'both', 'right'] },
    controls: ['duel', 'correction', 'skip']
  };
  const mixed = (over = {}) =>
    envelope({
      session: {
        id: 7,
        mode: 'mix',
        kinds: ['movie', 'series'],
        block: { index: 0, slot: 4, size: 15, counter: '4 of 15', serving: 'battle' }
      },
      ...over
    });

  it('names the modes plainly, in the title and its menu (A2, A3, policy h)', async () => {
    await open(mixed({ card: pairCard }));

    // The card on the table is a pair, and the title still says what was chosen.
    const title = target.querySelector('[data-testid="rate-menu"]');
    expect(title.textContent.trim()).toBe('Mixed');
    expect(target.querySelector('[data-testid="rate-counter"]').textContent).toBe('4 of 15');
    expect(target.querySelector('[data-testid="rate-mode-mix"]')).toBeNull();

    title.click();
    flushSync();
    const modes = [...target.querySelectorAll('[data-testid^="rate-mode-"]')];
    expect(modes.map((el) => el.querySelector('.row-text > span').textContent)).toEqual([
      'Mixed',
      'Singles',
      'Pairs'
    ]);
    expect(modes.map((el) => el.getAttribute('aria-pressed'))).toEqual(['true', 'false', 'false']);
    expect(target.querySelector('[data-testid="rate-mode-mix"]').textContent).toContain(
      'the pairs start at 15 ratings'
    );
    expect(target.textContent).not.toMatch(/\b(sweep|battle)\b/i);

    // A mode change is a write; the kinds are two toggles, and the last one on stays on.
    respond(mixed({ session: { ...mixed().session, mode: 'battle' }, card: pairCard }));
    target.querySelector('[data-testid="rate-mode-battle"]').click();
    await settle();
    expect(JSON.parse(fetchMock.mock.calls[1][1].body).mode).toBe('battle');
    expect(title.textContent.trim()).toBe('Pairs');
    expect(
      target.querySelector('[data-testid="rate-mode-battle"]').getAttribute('aria-pressed')
    ).toBe('true');

    respond(mixed({ session: { ...mixed().session, kinds: ['series'] }, card: pairCard }));
    target.querySelector('[data-testid="rate-kind-movie"]').click();
    await settle();
    expect(JSON.parse(fetchMock.mock.calls[2][1].body).kinds).toEqual(['series']);
    target.querySelector('[data-testid="rate-kind-series"]').click();
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('asks the pair question and answers it in one tap, no switch first (decision 528)', async () => {
    await open(mixed({ card: pairCard }));

    expect(target.querySelector('[data-testid="rate-battle-question"]').textContent).toBe(
      'Which did you enjoy more?'
    );
    expect(target.querySelector('[data-testid="rate-battle-reason"]').textContent).toBe(
      'You rated both liked'
    );
    expect(target.querySelector('[role="switch"]')).toBeNull();
    expect(target.textContent).not.toMatch(/Clear favourite|About the same|Haven't seen one/);

    respond(mixed({ card: { ...pairCard, token: 'tok-4' } }));
    target.querySelector('[data-testid="rate-duel-A-much"]').click();
    await settle();
    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toContain('/api/rate/duel');
    expect(JSON.parse(init.body)).toMatchObject({ card_token: 'tok-3', outcome: 'A', decisive: true });
  });

  it('frames a single as a pair: the question, Not seen under the poster, three answers (decision 529)', async () => {
    const sweep = { ...substitutedSweep, substituted_for: null };
    await open(mixed({ card: sweep }));

    const card = target.querySelector('[data-testid="rate-sweep-card"]');
    expect(card.querySelector('.question').textContent).toBe('How was it?');
    expect([...card.querySelectorAll('.tiles [data-answer]')].map((t) => t.textContent)).toEqual([
      'Disliked',
      'Fine',
      'Liked'
    ]);
    const unseen = target.querySelector('[data-testid="rate-not-seen"]');
    expect(unseen.closest('.tiles')).toBeNull();
    expect(unseen.getAttribute('aria-label')).toBe('Not seen: Heat');

    respond(mixed({ card: sweep }));
    unseen.click();
    await settle();
    expect(fetchMock.mock.calls[1][0]).toContain('/api/rate/not-seen');
  });

  it('lights the answer in flight while the rest of the card waits (A4)', async () => {
    await open(mixed({ card: { ...substitutedSweep, substituted_for: null } }));

    /** @type {(response: any) => void} */
    let answer = () => {};
    fetchMock.mockReturnValueOnce(new Promise((resolve) => (answer = resolve)));
    target.querySelector('[data-testid="rate-verdict-2"]').click();
    flushSync();

    const liked = target.querySelector('[data-testid="rate-verdict-2"]');
    expect(liked.classList.contains('picked')).toBe(true);
    expect(liked.getAttribute('aria-busy')).toBe('true');
    expect(target.querySelector('[data-testid="rate-verdict-0"]').classList.contains('picked')).toBe(
      false
    );

    answer({
      ok: true,
      status: 200,
      headers: { get: () => null },
      text: async () => JSON.stringify(mixed({ card: pairCard, reveal: null }))
    });
    await settle();
    expect(target.querySelector('.picked')).toBeNull();
  });

  it('names the kind the spread counts when only one is selected (A5)', async () => {
    await open(
      envelope({
        session: {
          id: 7,
          mode: 'sweep',
          kinds: ['series'],
          block: { index: 0, slot: 2, size: 15, counter: '2 of 15', serving: 'sweep' }
        },
        card: { ...substitutedSweep, substituted_for: null },
        class_balance: { ...envelope().class_balance, counts: [2, 3, 4], total: 9, arms_at: 15 }
      })
    );
    // The progress row's meter opens the mix, and when the balance check starts (decision 528).
    const meter = target.querySelector('[data-testid="rate-mix"]');
    expect(meter.getAttribute('aria-label')).toBe('Your mix: 2 disliked, 3 fine, 4 liked');
    expect(target.querySelector('[data-testid="rate-balance-arming"]')).toBeNull();
    meter.click();
    flushSync();
    const mix = target.querySelector('[role="dialog"][aria-label="Your mix"]');
    expect(mix.querySelector('[data-testid="rate-balance-total"]').textContent).toBe(
      '9 series ratings'
    );
    expect(mix.querySelector('[data-testid="rate-balance-arming"]').textContent).toBe(
      'A balance check starts at 15 ratings.'
    );
    mix.querySelector('.done').click();
    flushSync();
    target.querySelector('[data-testid="rate-why"]').click();
    flushSync();
    expect(target.querySelector('[data-testid="rate-label-count"]').textContent).toContain(
      '9 series ratings'
    );
  });

  it('shows the balance warning as the server words it, once it has armed', async () => {
    const copy =
      "Heavy on 'liked'. Spreading your ratings across all three answers matters about five " +
      "times more than anything else you can do here. Rate some titles you didn't enjoy as " +
      'well - but never change an honest answer to even things out.';
    const armed = { ...envelope().class_balance, counts: [2, 3, 12], total: 17, warn: true, copy };
    await open(mixed({ card: { ...substitutedSweep, substituted_for: null }, class_balance: armed }));
    // One line on the screen; the sentence is in the sheet it opens.
    const chip = target.querySelector('[data-testid="rate-balance-chip"]');
    expect(chip.textContent.replace(/\s+/g, ' ').trim()).toBe('Heavy on liked · See why');
    expect(target.querySelector('[data-testid="rate-balance-warning"]')).toBeNull();
    chip.click();
    flushSync();
    expect(target.querySelector('[data-testid="rate-balance-warning"]').textContent).toBe(copy);
    expect(target.querySelector('[data-testid="rate-balance-arming"]')).toBeNull();
  });

  it('opens "About this film" from a poster, and its Not seen corrects that side', async () => {
    await open(mixed({ card: pairCard }));
    respond({
      title: { id: 2, name: 'Drive', overview: 'A driver for hire.' },
      genres: ['Crime', 'Drama'],
      credits: [
        { person_id: 9, name: 'Nicolas Winding Refn', role_class: 'director', job: 'Director' },
        { person_id: 7, name: 'Ryan Gosling', role_class: 'cast', job: 'Actor', character: 'Driver' }
      ]
    });
    target.querySelector('[data-testid="rate-battle-right"]').click();
    await settle();

    expect(fetchMock.mock.calls[1][0]).toContain('/api/titles/2');
    const peek = target.querySelector('[data-testid="rate-peek"]');
    expect(peek.textContent).toContain('2011 · 1h 40m · Crime, drama');
    expect(peek.textContent).toContain('Directed by Nicolas Winding Refn');
    expect(peek.textContent).toContain('Driver');
    expect(peek.textContent).toContain('Looking never counts as an answer.');
    expect(fetchMock.mock.calls.some(([url]) => url.includes('/rate/duel'))).toBe(false);

    respond(mixed({ card: { ...pairCard, token: 'tok-5' } }));
    target.querySelector('[data-testid="rate-peek-not-seen"]').click();
    await settle();
    const [url, init] = fetchMock.mock.calls[2];
    expect(url).toContain('/api/rate/correction');
    expect(JSON.parse(init.body)).toEqual({ card_token: 'tok-3', side: 'right' });
  });
});

describe('the keyboard (decision 528)', () => {
  const pairCard = {
    type: 'battle',
    token: 'tok-7',
    kind: 'movie',
    left: { id: 1, name: 'Heat', year: 1995, runtime_min: 170, outcome: 'A' },
    right: { id: 2, name: 'Drive', year: 2011, runtime_min: 100, outcome: 'B' },
    reason: 'You rated both liked',
    corrections: { label: 'not seen', sides: ['left', 'both', 'right'] }
  };
  const press = (key, init = {}) =>
    window.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, ...init }));
  const sent = () =>
    fetchMock.mock.calls
      .slice(1)
      .map(([url, init]) => [url.replace(/^.*\/api/, ''), JSON.parse(init.body)]);

  it('answers a pair with the arrows, Shift for much more, and Q or P for not seen', async () => {
    await open(envelope({ card: pairCard }));
    for (const [key, init] of [
      ['ArrowLeft', {}],
      ['ArrowRight', { shiftKey: true }],
      ['ArrowDown', {}],
      ['q', {}],
      ['P', {}]
    ]) {
      respond(envelope({ card: pairCard }));
      press(key, init);
      await settle();
    }
    expect(sent().map(([url, body]) => [url, body.outcome ?? body.side, body.decisive])).toEqual([
      ['/rate/duel', 'A', false],
      ['/rate/duel', 'B', true],
      ['/rate/duel', 'TIE', false],
      ['/rate/correction', 'left', undefined],
      ['/rate/correction', 'right', undefined]
    ]);
  });

  it('rates a single with 1, 2 and 3, and says not seen with N', async () => {
    const sweep = { ...substitutedSweep, substituted_for: null };
    await open(envelope({ session: { mode: 'sweep' }, card: sweep }));
    respond(envelope({ session: { mode: 'sweep' }, card: sweep }));
    press('3');
    await settle();
    expect(sent()[0]).toEqual(['/rate/verdict', expect.objectContaining({ value: 2 })]);
    respond(envelope({ session: { mode: 'sweep' }, card: sweep }));
    press('n');
    await settle();
    expect(sent()[1][0]).toBe('/rate/not-seen');
  });

  it('does nothing while a sheet is open', async () => {
    await open(envelope({ card: pairCard }));
    target.querySelector('[data-testid="rate-menu"]').click();
    flushSync();
    press('ArrowLeft');
    press('s');
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('the echo (decision 530)', () => {
  const sweep = { ...substitutedSweep, substituted_for: null };
  const next = { ...sweep, token: 'tok-2', title: { ...sweep.title, id: 42, name: 'Drive' } };

  it("names the card just rated and its guess in the next card's reason line, until the next tap", async () => {
    await open(envelope({ session: { mode: 'sweep' }, card: sweep }));
    respond(
      envelope({
        session: { mode: 'sweep' },
        card: next,
        reveal: { available: true, agreed: false, text: "we'd have guessed fine" }
      })
    );
    target.querySelector('[data-testid="rate-verdict-2"]').click();
    await settle();

    // The next card is up at once; the guess sits where its reason goes, with the answer's glyph.
    expect(target.querySelector('[data-testid="rate-card-title"]').textContent).toBe('Drive');
    const echo = target.querySelector('.reason [data-testid="rate-reveal"]');
    expect(echo.textContent).toBe("Heat · we'd have guessed fine");
    expect(echo.querySelector('svg')).toBeTruthy();
    expect(echo.getAttribute('data-reveal-available')).toBe('true');
    expect(echo.getAttribute('data-reveal-agreed')).toBe('false');
    expect(target.querySelector('[data-testid="rate-queue-reason"]')).toBeNull();
    expect(target.querySelector('.sr-only[role="status"]').textContent).toBe(
      "Heat · we'd have guessed fine"
    );

    fetchMock.mockReturnValueOnce(new Promise(() => {}));
    target.querySelector('[data-testid="rate-verdict-0"]').click();
    flushSync();
    expect(target.querySelector('[data-testid="rate-reveal"]')).toBeNull();
    expect(target.querySelector('[data-testid="rate-queue-reason"]')).toBeTruthy();
  });
});

describe("a block's end (decisions 199 and 527)", () => {
  const sweep = { ...substitutedSweep, substituted_for: null };
  const at = (slot, index = 0) =>
    envelope({
      session: {
        id: 7,
        mode: 'sweep',
        kinds: ['movie'],
        block: { index, slot, size: 15, counter: `${slot} of 15`, serving: 'sweep' }
      },
      card: sweep,
      class_balance: { ...envelope().class_balance, counts: [3, 5, 7], total: 15 }
    });
  const fifteenth = {
    ...at(1, 1),
    reveal: { available: true, agreed: true, text: "we'd have guessed the same" },
    undo: { available: true, kind: 'verdict', reason: null }
  };

  it('is a screen of its own, still undoable, and Rate 15 more goes on to the next card', async () => {
    await open(at(15));
    expect(target.querySelector('[data-testid="rate-done"]')).toBeNull();

    respond(fifteenth);
    target.querySelector('[data-testid="rate-verdict-1"]').click();
    await settle();

    const done = target.querySelector('[data-testid="rate-done"]');
    expect(done.querySelector('h2').textContent).toBe("That's 15.");
    expect(done.textContent).toContain('Your suggestions just got sharper.');
    expect(target.querySelector('[data-testid="rate-counter"]').textContent).toBe('15 of 15');
    expect(done.querySelector('[data-testid="rate-balance-total"]').textContent).toBe(
      '15 film ratings'
    );
    expect(target.querySelector('[data-testid="rate-undo"]').disabled).toBe(false);
    expect(target.querySelector('[data-testid="rate-sweep-card"]')).toBeNull();
    expect(target.querySelector('[data-testid="rate-skip"]')).toBeNull();
    expect(done.querySelector('[data-testid="rate-done-home"]').getAttribute('href')).toBe('/');
    expect(done.querySelector('.echo-slot [data-testid="rate-reveal"]')).toBeTruthy();

    target.querySelector('[data-testid="rate-done-more"]').click();
    flushSync();
    expect(target.querySelector('[data-testid="rate-done"]')).toBeNull();
    expect(target.querySelector('[data-testid="rate-reveal"]'), 'the echo shows once').toBeNull();
    expect(target.querySelector('[data-testid="rate-sweep-card"]')).toBeTruthy();
    expect(target.querySelector('[data-testid="rate-counter"]').textContent).toBe('1 of 15');
  });

  it('goes back to the fifteenth card when Undo takes that answer back', async () => {
    await open(at(15));
    respond(fifteenth);
    target.querySelector('[data-testid="rate-verdict-1"]').click();
    await settle();
    expect(target.querySelector('[data-testid="rate-done"]')).toBeTruthy();

    respond(at(15));
    target.querySelector('[data-testid="rate-undo"]').click();
    await settle();
    expect(target.querySelector('[data-testid="rate-done"]')).toBeNull();
    expect(target.querySelector('[data-testid="rate-counter"]').textContent).toBe('15 of 15');
    expect(target.querySelector('[data-testid="rate-sweep-card"]')).toBeTruthy();
  });
});
