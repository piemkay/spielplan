/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ get: vi.fn(), post: vi.fn() }));

import { get, post } from '$lib/api.js';
import { hideToast, toast } from '$lib/toast.svelte.js';
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
  it('asks about one title in one line: how far Jellyfin saw it play', async () => {
    vi.mocked(post).mockResolvedValue(answerBody(true));
    const app = await open();
    try {
      const card = target.querySelector(CARD);
      expect(card.querySelector('.headline').textContent).toBe('Did you finish Severance?');
      expect([...card.querySelectorAll('.line')].map((p) => p.textContent)).toEqual([
        'Jellyfin saw it play to 96%.'
      ]);
      expect(card.querySelectorAll('[data-testid="rate-poster"]')).toHaveLength(1);
    } finally {
      unmount(app);
    }
  });
});

describe('its x (decision 554)', () => {
  afterEach(() => hideToast());

  it('puts the question away without an answer, and Undo brings it back', async () => {
    vi.mocked(post).mockResolvedValue({ ok: true, title_id: PROMPT.title_id });
    const app = await open();
    try {
      target.querySelector(`${CARD} [aria-label="Dismiss Severance"]`).click();
      await settle();
      expect(vi.mocked(post).mock.calls).toEqual([['/prompts/finish/11/close']]);
      expect(target.querySelector(CARD)).toBeNull();
      expect(target.querySelector(HANDOFF), 'the x is no answer').toBeNull();
      expect(answered).toEqual([]);
      expect([toast.message, toast.actionLabel]).toEqual(['Removed', 'Undo']);

      toast.action();
      await settle();
      expect(vi.mocked(post).mock.calls.at(-1)).toEqual(['/prompts/finish/11/reopen']);
      expect(target.querySelector(CARD).getAttribute('data-finish-prompt')).toBe('7');
    } finally {
      unmount(app);
    }
  });

  it('closes the answer that followed, on this screen only', async () => {
    vi.mocked(post).mockResolvedValue(answerBody(true));
    const app = await open();
    try {
      await tap('Yes');
      target.querySelector(`${HANDOFF} [aria-label="Close"]`).click();
      flushSync();
      expect(target.querySelector(HANDOFF)).toBeNull();
      expect(vi.mocked(post)).toHaveBeenCalledTimes(1);
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
      expect(target.querySelector(CARD)).toBeNull();
      const handoff = target.querySelector(HANDOFF);
      expect(handoff).not.toBeNull();
      expect(handoff.getAttribute('data-answer')).toBe('seen');
      // `/rate` pins a repeated `head` to the front; a bare `/rate` would present another title.
      expect(target.querySelector(CTA).getAttribute('href')).toBe('/rate?head=7');
    } finally {
      unmount(app);
    }
  });

  it('tells the surface that owns the banner', async () => {
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
      expect(target.querySelector(HANDOFF).textContent).toContain("Jellyfin isn't connected");
      expect(target.querySelector(HANDOFF).textContent).not.toContain('not configured');
    } finally {
      unmount(app);
    }
  });
});

describe('a no', () => {
  it('says what it wrote, and offers nothing to rate', async () => {
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
    // Dropped on success only: in a `finally`, a failed write looked like a successful one.
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
