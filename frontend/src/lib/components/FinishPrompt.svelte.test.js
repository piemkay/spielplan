/**
 * @vitest-environment jsdom
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
      // A decline is an explicit `unseen` (decision 211), so the old promise would be false.
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
