/**
 * @vitest-environment jsdom
 *
 * What the seen control says about a write that was deliberately never sent. Spec v2.1 §7.3, §6.8;
 * decision 210(a); M4.11.
 *
 * Jellyfin stores no Played flag on a Series — `Folder.FillUserDataDtoValues` computes the folder's
 * from its episodes — so the only way to un-mark one is a recursive DELETE across every episode,
 * which would destroy watch history the app never recorded and cannot put back. Decision 210(a)
 * settles it: `kind='series'` with `seen=False` writes the app side, stamps `jf_synced_at` so the
 * sweep does not re-owe the row, sends nothing, and returns the pair `(True, 'series unseen is
 * app-only')`. "And the surface says so" is the other half of that decision, and this card was
 * printing the opposite: it read `synced` alone, so a reason that arrived WITH a success rendered as
 * "synced to Jellyfin" — a sentence about a request that was never made.
 *
 * MOUNTED because both assertions are about one line of copy against one payload, which is cheaper
 * and more exact than reaching a series title through a stateful browser suite; `08-jellyfin.spec.js`
 * keeps the movie path end to end, where the same line must still read "synced to Jellyfin".
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ get: vi.fn(), post: vi.fn() }));

import { get, post } from '$lib/api.js';
import TitleDetail from './TitleDetail.svelte';

const NOTE = '[data-testid="title-series-unseen-note"]';
const SYNCNOTE = '.syncnote';

/** One `GET /api/titles/{id}` payload, in the shape `api/library.py` sends. */
const payload = (over = {}) => ({
  title: {
    id: 6,
    name: 'Severance',
    kind: 'series',
    year: 2022,
    runtime_min: 24,
    seen_state: 'seen',
    original_name: null,
    overview: null,
    trailer_key: null,
    ...over
  },
  model_line: { available: false, reason: 'no bundle' },
  credits: [],
  platform_ratings: { items: [], note: 'display-only' },
  dna: { extracted: [], projected: [] },
  actions: { play_on_jellyfin: null, show_on_map: { title_id: 6 } }
});

let target;

beforeEach(() => {
  target = document.createElement('div');
  document.body.appendChild(target);
  vi.mocked(get).mockReset();
  vi.mocked(post).mockReset();
});

afterEach(() => {
  target.remove();
});

async function settle() {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
}

async function open(over = {}) {
  vi.mocked(get).mockResolvedValue(payload(over));
  const app = mount(TitleDetail, {
    target,
    props: { titleId: 6, onClose: () => {}, onPerson: () => {}, onStateChange: () => {} }
  });
  await settle();
  return app;
}

const tapSeen = async () => {
  const button = target.querySelector('button.seen');
  expect(button, 'the seen control is not on screen').not.toBeNull();
  button.click();
  await settle();
};

describe("decision 210(a)'s why-line", () => {
  it('warns before the tap that un-marking a series stays in this app', async () => {
    const app = await open();
    try {
      expect(target.querySelector(NOTE).textContent).toContain('kept in Spielplan only');
    } finally {
      unmount(app);
    }
  });

  it('is absent on a film, and absent on a series that is not seen yet', async () => {
    // The negative control, twice over: the line is about the un-marking direction of a series, and
    // §6.8's register is a quiet line where the consequence is — not a standing disclaimer.
    let app = await open({ kind: 'movie' });
    expect(target.querySelector(NOTE)).toBeNull();
    unmount(app);
    app = await open({ seen_state: 'unseen' });
    try {
      expect(target.querySelector(NOTE)).toBeNull();
    } finally {
      unmount(app);
    }
  });
});

describe('the sync note', () => {
  it('prints the reason a successful write gives, rather than claiming a push', async () => {
    vi.mocked(post).mockResolvedValue({
      state: 'unseen',
      synced: true,
      reason: 'series unseen is app-only'
    });
    const app = await open();
    try {
      await tapSeen();
      expect(target.querySelector(SYNCNOTE).textContent).toContain('series unseen is app-only');
    } finally {
      unmount(app);
    }
  });

  it('still says so when the push really did land', async () => {
    // The negative control `08-jellyfin.spec.js` asserts on the movie path: a successful push with
    // nothing to explain must keep reading "synced to Jellyfin".
    vi.mocked(post).mockResolvedValue({ state: 'seen', synced: true, reason: null });
    const app = await open({ kind: 'movie', seen_state: 'unseen' });
    try {
      await tapSeen();
      expect(target.querySelector(SYNCNOTE).textContent).toContain('synced to Jellyfin');
    } finally {
      unmount(app);
    }
  });
});
