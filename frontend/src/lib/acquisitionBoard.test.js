/**
 * @vitest-environment jsdom
 *
 * The legend is invented where the names only pass through: §8's own names could not tell a
 * pass-through from a copy. The plain step names are keyed by §8's names, so those tests use them.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));
vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  return { page: writable({ url: new URL('http://localhost/admin/titles'), state: {} }) };
});
vi.mock('$app/navigation', async () => {
  const stores = /** @type {any} */ (await import('$app/stores'));
  return { pushState: (_url, state) => stores.page.update((p) => ({ ...p, state })) };
});

import * as stores from '$app/stores';
import { api, get } from '$lib/api.js';
import AcquisitionBoard from './components/AcquisitionBoard.svelte';

// The mock above made it writable.
const page = /** @type {import('svelte/store').Writable<any>} */ (/** @type {unknown} */ (stores.page));
import {
  ABANDON,
  ALL,
  CURRENT,
  DONE,
  READY,
  RETRY,
  RETRY_FROM,
  UNREACHED,
  WAITING,
  actionRequest,
  documentFacts,
  filterCounts,
  groupsOf,
  metaOf,
  offered,
  progressOf,
  refusalOf,
  retryStages,
  segments,
  statusOf,
  stepName,
  summaryOf
} from './acquisitionBoard.svelte.js';

const NAMES = ['alpha', 'beta', 'gamma', 'delta four', 'epsilon', 'zeta', 'eta', 'theta', 'iota', 'kappa'];
const LEGEND = NAMES.map((name, i) => ({ number: i + 1, name }));

const SECTION_8 = [
  'identify',
  'enrich',
  'derive',
  'reviews gate',
  'dna pack',
  'dna extract',
  'verify',
  'project',
  'place',
  'ready'
];
const STAGES = SECTION_8.map((name, i) => ({ number: i + 1, name }));

const job = (over = {}) => ({
  title_id: 4,
  name: 'Chungking Express',
  year: 1994,
  title_kind: 'movie',
  stage: 4,
  status: 'parked',
  reason: 'reviews window: 2 sources, 38 words - retry window 30 days',
  retry_after: null,
  actions: [RETRY_FROM, ABANDON],
  ...over
});

async function settle() {
  for (let i = 0; i < 40; i++) await Promise.resolve();
  flushSync();
}

describe('segments', () => {
  it('marks each stage behind the job done, its own stage current and the rest unreached', () => {
    const states = segments(LEGEND, job({ stage: 4 })).map((s) => s.state);
    expect(states).toEqual([DONE, DONE, DONE, CURRENT, ...Array(6).fill(UNREACHED)]);
    expect(progressOf(LEGEND, job({ stage: 4 }))).toEqual({ done: 3, total: 10 });
  });

  it('passes the legend through untouched, in its own order', () => {
    const drawn = segments(LEGEND, job({ stage: 2 }));
    expect(drawn.map((s) => s.name)).toEqual(NAMES);
    expect(drawn.map((s) => s.number)).toEqual(LEGEND.map((s) => s.number));
    expect(drawn.map((s) => s.plain), 'a name it has no plain words for is shown as sent').toEqual(NAMES);
  });

  it('counts the last stage of a ready job as run, not as the one it is stuck at', () => {
    const states = segments(LEGEND, job({ stage: 10, status: 'ready' })).map((s) => s.state);
    expect(states).toEqual(Array(10).fill(DONE));
    expect(progressOf(LEGEND, job({ stage: 10, status: 'ready' }))).toEqual({ done: 10, total: 10 });
    const failed = segments(LEGEND, job({ stage: 10, status: 'failed' })).map((s) => s.state);
    expect(failed[9]).toBe(CURRENT);
  });

  it("puts plain words beside each of section 8's names, one per step", () => {
    const plain = STAGES.map(stepName);
    expect(new Set(plain).size).toBe(10);
    expect(plain[1]).toBe('Gather details');
    expect(plain.some((name, i) => name === SECTION_8[i])).toBe(false);
  });
});

describe('statuses', () => {
  it('tells parked, failed and abandoned apart in tone and in words', () => {
    const parked = statusOf(job());
    const failed = statusOf(job({ status: 'failed' }));
    const abandoned = statusOf(job({ status: 'abandoned' }));
    expect(new Set([parked.tone, failed.tone, abandoned.tone]).size).toBe(3);
    expect(new Set([parked.label, failed.label, abandoned.label]).size).toBe(3);
    expect(failed).toMatchObject({ label: 'Failed', badge: 'bad' });
    expect(statusOf(job({ status: 'ready' }))).toMatchObject({ label: 'Ready', badge: 'ok' });
  });

  it('says a title parked for want of film details is waiting for details, and no more elsewhere', () => {
    expect(statusOf(job({ stage: 2 }), STAGES).label).toBe('Waiting for details');
    expect(statusOf(job({ stage: 6 }), STAGES).label).toBe('Waiting');
  });

  it('shows a status it has no words for exactly as the server spelled it', () => {
    expect(statusOf(job({ status: 'something-new' }))).toEqual({
      tone: 'other',
      label: 'something-new',
      badge: ''
    });
  });

  it('sums a job up plainly, saying a park is not a breakage and naming no spec section', () => {
    const parked = summaryOf(STAGES, job({ stage: 2 }));
    expect(parked).toContain('step 2 of 10 (Gather details)');
    expect(parked).toMatch(/Nothing is broken/);
    expect(summaryOf(STAGES, job({ status: 'failed' }))).toMatch(/went wrong/);
    expect(summaryOf(STAGES, job({ status: 'ready', stage: 10 }))).toMatch(/Every step has run/);
    for (const status of ['parked', 'failed', 'abandoned', 'queued', 'running', 'ready', 'odd']) {
      expect(summaryOf(STAGES, job({ status }))).not.toMatch(/§|decision|section/i);
    }
  });

  it("reads a title's kind and year as one line", () => {
    expect(metaOf(job({ title_kind: 'series', year: 2024 }))).toBe('Series · 2024');
    expect(metaOf(job())).toBe('Film · 1994');
    expect(metaOf(job({ title_kind: null }))).toBe('1994');
  });
});

describe('filters', () => {
  const jobs = [
    job({ title_id: 1, status: 'ready' }),
    job({ title_id: 2, status: 'parked' }),
    job({ title_id: 3, status: 'failed' }),
    job({ title_id: 4, status: 'running' }),
    job({ title_id: 5, status: 'parked' })
  ];
  const ids = (groups) => groups.map((g) => [g.heading, g.jobs.map((j) => j.title_id)]);

  it('counts parked and failed as waiting, and every job under All', () => {
    expect(filterCounts(jobs)).toEqual({ [WAITING]: 3, [READY]: 1, [ALL]: 5 });
  });

  it('puts failed before parked, keeps server order in a group, and leaves ready out of Waiting', () => {
    expect(ids(groupsOf(jobs, WAITING))).toEqual([
      ['Failed', [3]],
      ['Waiting', [2, 5]]
    ]);
    expect(ids(groupsOf(jobs, READY))).toEqual([['Ready', [1]]]);
    expect(ids(groupsOf(jobs, ALL)).map(([heading]) => heading)).toEqual([
      'Failed',
      'Waiting',
      'In progress',
      'Ready'
    ]);
  });
});

describe('actions', () => {
  it('offers exactly what the job carries in `actions`, whatever its status says', () => {
    expect(offered(job())).toEqual({ retry: false, retryFrom: true, abandon: true });
    expect(offered(job({ status: 'failed', actions: [RETRY, RETRY_FROM, ABANDON] }))).toEqual({
      retry: true,
      retryFrom: true,
      abandon: true
    });
    expect(offered(job({ status: 'failed', actions: [] }))).toEqual({
      retry: false,
      retryFrom: false,
      abandon: false
    });
    expect(offered({ status: 'running' })).toEqual({ retry: false, retryFrom: false, abandon: false });
  });

  it('offers a retry from stage 1 up to the stage the job reached and never past it', () => {
    expect(retryStages(LEGEND, job({ stage: 4 })).map((s) => s.number)).toEqual([1, 2, 3, 4]);
  });

  it('builds each action as its own route under the title', () => {
    expect(actionRequest(RETRY, 7)).toEqual({ path: '/admin/acquisition/7/retry', body: undefined });
    expect(actionRequest(RETRY_FROM, 7, '3')).toEqual({
      path: '/admin/acquisition/7/retry-from',
      body: { stage: 3 }
    });
    expect(actionRequest(ABANDON, 7)).toEqual({ path: '/admin/acquisition/7/abandon', body: undefined });
    expect(() => actionRequest('accept', 7)).toThrow();
  });

  it("keeps the server's refusal whole", () => {
    const sentence = 'over spend cap: a retry here would reserve $0.02 and the month has $0.01 left';
    expect(refusalOf(new Error(sentence))).toBe(sentence);
  });
});

describe('documents', () => {
  it('lists the raw_document metadata with the url as text and never a path to the bytes', () => {
    const facts = documentFacts({
      id: 1,
      source: 'wikipedia',
      kind: 'page',
      url: 'https://en.wikipedia.org/wiki/Heat',
      http_status: 200,
      content_sha256: 'ab12',
      byte_size: 5120,
      fetched_at: '2026-09-24T10:00:00Z',
      error: null,
      content_path: '/data/raw/ab/12'
    });
    const keys = facts.map(([key]) => key);
    expect(keys).toEqual(['source', 'kind', 'url', 'http', 'sha256', 'bytes', 'fetched']);
    expect(Object.fromEntries(facts).url).toBe('https://en.wikipedia.org/wiki/Heat');
    expect(JSON.stringify(facts)).not.toContain('/data/raw');
  });
});

describe('the mounted board', () => {
  let target;

  beforeEach(() => {
    target = document.createElement('div');
    document.body.appendChild(target);
    vi.mocked(get).mockReset();
    vi.mocked(api).mockReset();
    page.set({ url: new URL('http://localhost/admin/titles'), state: {} });
    // Back pops the newest sheet, as the browser's popstate would.
    vi.spyOn(history, 'back').mockImplementation(() =>
      page.update((p) => ({ ...p, state: { sheets: (p.state.sheets ?? []).slice(0, -1) } }))
    );
  });

  afterEach(() => {
    target.remove();
    vi.restoreAllMocks();
  });

  const envelope = () => ({
    stages: STAGES,
    jobs: [
      job(),
      job({
        title_id: 9,
        name: 'Tampopo',
        status: 'failed',
        reason: 'enrich raised: the host answered 503\ntwice',
        actions: [RETRY, RETRY_FROM, ABANDON]
      }),
      job({ title_id: 12, name: 'Heat', status: 'ready', stage: 10, reason: null, actions: [RETRY_FROM] })
    ]
  });

  const rowOf = (id) => target.querySelector(`[data-testid="board-job"][data-title-id="${id}"]`);
  const sheet = () => target.querySelector('[data-testid="board-title"]');
  const press = (within, label) => {
    const button = [...within.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
    expect(button, `no button named ${label}`).toBeDefined();
    button.click();
  };

  it('opens on what is waiting, each filter counted, and keeps a ready title under Ready', async () => {
    vi.mocked(get).mockResolvedValue(envelope());
    const app = mount(AcquisitionBoard, { target, props: {} });
    await settle();
    try {
      const filters = [...target.querySelectorAll('.segmented button')];
      expect(filters.map((b) => b.textContent.trim())).toEqual(['Waiting · 2', 'Ready · 1', 'All · 3']);
      expect(filters[0].getAttribute('aria-pressed')).toBe('true');
      expect(rowOf(4)).not.toBe(null);
      expect(rowOf(9)).not.toBe(null);
      expect(rowOf(12)).toBe(null);
      expect(rowOf(4).textContent).toContain('3 of 10');
      expect(rowOf(4).className).not.toBe(rowOf(9).className);
      filters[1].click();
      await settle();
      expect(rowOf(12)).not.toBe(null);
      expect(rowOf(4)).toBe(null);
    } finally {
      unmount(app);
    }
  });

  it('opens a title on the stages verbatim, its reason whole, and the actions it admits', async () => {
    vi.mocked(get).mockResolvedValue(envelope());
    const app = mount(AcquisitionBoard, { target, props: {} });
    await settle();
    try {
      rowOf(4).click();
      await settle();
      const texts = (id) => [...sheet().querySelectorAll(`[data-testid="${id}"]`)].map((e) => e.textContent);
      expect(texts('board-stage')).toEqual(SECTION_8);
      expect(texts('board-reason')).toEqual([envelope().jobs[0].reason]);
      const parkedSays = sheet().querySelector('[data-testid="board-status"]').textContent.trim();
      const buttons = () => [...sheet().querySelectorAll('button')].map((b) => b.textContent.trim());
      expect(buttons()).not.toContain('Retry now');
      expect(buttons()).toEqual(expect.arrayContaining(['Retry from a step…', 'Stop trying']));

      history.back();
      await settle();
      rowOf(9).click();
      await settle();
      expect(sheet().getAttribute('data-title-id')).toBe('9');
      expect(texts('board-reason')).toEqual([envelope().jobs[1].reason]);
      expect(sheet().querySelector('[data-testid="board-status"]').textContent.trim()).not.toBe(parkedSays);
      const all = ['Retry now', 'Retry from a step…', 'Stop trying'];
      expect(buttons()).toEqual(expect.arrayContaining(all));
    } finally {
      unmount(app);
    }
  });

  it("shows a refused action's sentence whole in the title, and reads the board again", async () => {
    const sentence = 'a plain retry is for a failed job; this one is parked';
    vi.mocked(get).mockResolvedValue(envelope());
    vi.mocked(api).mockRejectedValue(new Error(sentence));
    const app = mount(AcquisitionBoard, { target, props: {} });
    await settle();
    try {
      rowOf(9).click();
      await settle();
      press(sheet(), 'Retry now');
      await settle();
      expect(vi.mocked(api)).toHaveBeenCalledWith('/admin/acquisition/9/retry', {
        method: 'POST',
        body: undefined
      });
      expect(sheet().querySelector('[data-testid="board-refusal"]').textContent).toBe(sentence);
      expect(vi.mocked(get).mock.calls.map(([path]) => path), 'the board, then the sidebar count').toEqual([
        '/admin/acquisition',
        '/admin/acquisition',
        '/admin/system'
      ]);
    } finally {
      unmount(app);
    }
  });

  it('asks before it stops trying, and retries from the step the operator picked', async () => {
    vi.mocked(get).mockResolvedValue(envelope());
    vi.mocked(api).mockResolvedValue({ job: job() });
    const app = mount(AcquisitionBoard, { target, props: {} });
    await settle();
    try {
      rowOf(4).click();
      await settle();
      press(sheet(), 'Stop trying');
      await settle();
      expect(vi.mocked(api), 'the first tap only asks').not.toHaveBeenCalled();
      const menu = () => target.querySelector('[role="menu"]');
      press(menu(), 'Stop trying');
      await settle();
      expect(vi.mocked(api)).toHaveBeenCalledWith('/admin/acquisition/4/abandon', {
        method: 'POST',
        body: undefined
      });

      press(sheet(), 'Retry from a step…');
      await settle();
      const items = [...menu().querySelectorAll('[role="menuitem"]')];
      expect(items.map((b) => b.textContent.trim())).toEqual(STAGES.slice(0, 4).map(stepName));
      press(menu(), 'Gather details');
      await settle();
      expect(vi.mocked(api)).toHaveBeenLastCalledWith('/admin/acquisition/4/retry-from', {
        method: 'POST',
        body: { stage: 2 }
      });
    } finally {
      unmount(app);
    }
  });
});
