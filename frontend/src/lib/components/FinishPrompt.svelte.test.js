/**
 * @vitest-environment jsdom
 *
 * What the finish prompt OFFERS, and what it says it is doing. Spec v2.1 §7.3, §6.1; decisions
 * 211 and 212; M4.11 finding 13.
 *
 * §7.3 is one sentence: "one tap sets `seen` and offers the verdict flow". The write shipped and
 * the offer did not — the card posted, dropped the row, and left the member on a Home whose banner
 * still held the population the answer had just changed. The comment that justified the omission
 * cited proposal 150, which no owner decision adopted, and proposal 150's own substitute (the
 * answered title "leaves a title in the banner's population") was undelivered too, because the
 * banner is server-rendered and this component was mounted with no callback.
 *
 * And the other half is copy. Under decision 211 a decline writes `unseen` as an explicit action —
 * that is what lets the sweep leave an open prompt alone — so the card's "Nothing is marked until
 * you say so" became false for the second of its two buttons.
 *
 * MOUNTED RATHER THAN IN PLAYWRIGHT, for the reason `rate-page.test.js` gives about the Rate
 * surface: the states worth asserting are states of the payload, and two of them (a push Jellyfin
 * refused, a write that fails mid-answer) cannot be produced from the outside without breaking the
 * stack for every later spec in a filename-ordered suite. `e2e/specs/08-jellyfin.spec.js` keeps the
 * end-to-end path — a real poll arms it, a real tap writes through.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ get: vi.fn(), post: vi.fn() }));

import { get, post } from '$lib/api.js';
import FinishPrompt from './FinishPrompt.svelte';

const CARD = '[data-finish-prompt]';
const HANDOFF = '[data-finish-handoff]';
const CTA = '[data-testid="finish-prompt-cta"]';

/** One row of `playback.pending()`, in the shape `GET /api/prompts/finish` sends. */
const PROMPT = {
  id: 11,
  title_id: 7,
  name: 'Severance',
  kind: 'series',
  year: 2022,
  progress: 0.96,
  poster_path: null
};

/** What `POST /api/prompts/finish/{id}` answers, for both answers, since decision 211. */
const answerBody = (seen, sync = { state: seen ? 'seen' : 'unseen', synced: true, reason: null }) => ({
  ok: true,
  title_id: PROMPT.title_id,
  seen,
  sync
});

let target;
let answered;

beforeEach(() => {
  answered = [];
  target = document.createElement('div');
  document.body.appendChild(target);
  vi.mocked(get).mockResolvedValue([PROMPT]);
  vi.mocked(post).mockReset();
});

afterEach(() => {
  target.remove();
});

/** Let the mocked route answers land, then render. Every promise here is already resolved. */
async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

async function open() {
  const app = mount(FinishPrompt, {
    target,
    props: { onAnswered: (titleId, state) => answered.push([titleId, state]) }
  });
  await settle();
  return app;
}

const tap = async (label) => {
  const button = [...target.querySelectorAll('button')].find((b) => b.textContent.includes(label));
  expect(button, `no button named ${label}`).toBeDefined();
  button.click();
  await settle();
};

describe('the card itself', () => {
  it('asks about one title and says that either answer is recorded', async () => {
    vi.mocked(post).mockResolvedValue(answerBody(true));
    const app = await open();
    try {
      const card = target.querySelector(CARD);
      expect(card).not.toBeNull();
      expect(card.textContent).toContain('Did you finish');
      // Decision 211: the decline is an explicit `unseen`, so the old promise is now a false one.
      expect(card.textContent).not.toContain('Nothing is marked until you say so');
      expect(card.textContent).toContain('no marks it not seen');
    } finally {
      unmount(app);
    }
  });
});

describe('a yes', () => {
  it('hands off to the rate queue with this title at its head', async () => {
    vi.mocked(post).mockResolvedValue(answerBody(true));
    const app = await open();
    try {
      await tap('Yes');
      // The question is over, so the card is gone — the handoff is a separate element, which is
      // also what keeps "the card does not come back" true.
      expect(target.querySelector(CARD)).toBeNull();
      const handoff = target.querySelector(HANDOFF);
      expect(handoff).not.toBeNull();
      expect(handoff.getAttribute('data-answer')).toBe('seen');
      // Repeated-`head` shape, as §6.0's banner CTA uses: `/rate` reads `head` from the URL and
      // pins it to the front of the queue. A bare `/rate` would present a different title.
      expect(target.querySelector(CTA).getAttribute('href')).toBe('/rate?head=7');
    } finally {
      unmount(app);
    }
  });

  it('tells the surface that owns the banner', async () => {
    // Decision 212. The identical seen write from the title card has re-read the shelves since M2
    // for the same reason: a verdict-less title that just became `seen` belongs in §6.0's banner.
    vi.mocked(post).mockResolvedValue(answerBody(true));
    const app = await open();
    try {
      await tap('Yes');
      expect(answered).toEqual([[7, 'seen']]);
    } finally {
      unmount(app);
    }
  });

  it('prints the reason when the state was written but Jellyfin was not told', async () => {
    vi.mocked(post).mockResolvedValue(
      answerBody(true, { state: 'seen', synced: false, reason: 'Jellyfin not configured' })
    );
    const app = await open();
    try {
      await tap('Yes');
      expect(target.querySelector(HANDOFF).textContent).toContain('Jellyfin not configured');
    } finally {
      unmount(app);
    }
  });
});

describe('a no', () => {
  it('says what it wrote, and offers nothing to rate', async () => {
    // Decision 211: "no" is an explicit `unseen`, not an absence — the absence was what the
    // 15-minute sweep adopted Jellyfin's Played flag into. There is nothing to rate, so the CTA
    // is not rendered at all rather than rendered into an empty queue.
    vi.mocked(post).mockResolvedValue(answerBody(false));
    const app = await open();
    try {
      await tap('No');
      const handoff = target.querySelector(HANDOFF);
      expect(handoff.getAttribute('data-answer')).toBe('unseen');
      expect(handoff.textContent).toContain('not seen');
      expect(target.querySelector(CTA)).toBeNull();
      expect(answered).toEqual([[7, 'unseen']]);
    } finally {
      unmount(app);
    }
  });
});

describe('a write that failed', () => {
  it('keeps the question on screen and claims nothing', async () => {
    // The negative control, and the reason the card is dropped on success only: in a `finally` a
    // failed write looked exactly like a successful one.
    vi.mocked(post).mockRejectedValue(new Error('database error'));
    const app = await open();
    try {
      await tap('Yes');
      expect(target.querySelector(CARD)).not.toBeNull();
      expect(target.querySelector(HANDOFF)).toBeNull();
      expect(answered).toEqual([]);
      expect(target.querySelector(CARD).textContent).toContain('database error');
    } finally {
      unmount(app);
    }
  });
});
