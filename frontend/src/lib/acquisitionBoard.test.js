/**
 * @vitest-environment jsdom
 *
 * §6.6 Data's acquisition board: segments, statuses and actions read off the server's envelope.
 * Spec v2.1 §6.6 Data, §8; decisions 336, 345, 424 and 444.
 *
 * Named BESIDE `test_acquire_actions.py` and `20-admin-data.spec.js` on the board's coverage row,
 * never instead of them (decision 226). The legend here is invented on purpose: the board must pass
 * the server's stage names through untouched, and a fixture that used §8's own names could not
 * tell a pass-through from a second copy of the list.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

import { api, get } from '$lib/api.js';
import AcquisitionBoard from './components/AcquisitionBoard.svelte';
import {
  ABANDON,
  CURRENT,
  DONE,
  RETRY,
  RETRY_FROM,
  UNREACHED,
  actionRequest,
  documentFacts,
  offered,
  refusalOf,
  retryStages,
  segments,
  stageLabel,
  statusOf
} from './acquisitionBoard.svelte.js';

const NAMES = ['alpha', 'beta', 'gamma', 'delta four', 'epsilon', 'zeta', 'eta', 'theta', 'iota', 'kappa'];
const LEGEND = NAMES.map((name, i) => ({ number: i + 1, name }));

const job = (over = {}) => ({
  title_id: 4,
  name: 'Chungking Express',
  year: 1994,
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
  });

  it('passes the legend through untouched, in its own order', () => {
    const drawn = segments(LEGEND, job({ stage: 2 }));
    expect(drawn.map((s) => s.name)).toEqual(NAMES);
    expect(drawn.map((s) => s.number)).toEqual(LEGEND.map((s) => s.number));
    expect(stageLabel(LEGEND, job({ stage: 4 }))).toBe('4 delta four');
  });

  it('counts the last stage of a ready job as run, not as the one it is stuck at', () => {
    const states = segments(LEGEND, job({ stage: 10, status: 'ready' })).map((s) => s.state);
    expect(states).toEqual(Array(10).fill(DONE));
    const failed = segments(LEGEND, job({ stage: 10, status: 'failed' })).map((s) => s.state);
    expect(failed[9]).toBe(CURRENT);
  });
});

describe('statuses', () => {
  it('tells parked, failed and abandoned apart in tone and in words', () => {
    const parked = statusOf('parked');
    const failed = statusOf('failed');
    const abandoned = statusOf('abandoned');
    expect(new Set([parked.tone, failed.tone, abandoned.tone]).size).toBe(3);
    expect(new Set([parked.label, failed.label, abandoned.label]).size).toBe(3);
    expect(parked.label).toMatch(/may change/);
    expect(failed.label).toMatch(/raised/);
  });

  it('shows a status it has no words for exactly as the server spelled it', () => {
    expect(statusOf('something-new')).toEqual({ tone: 'other', label: 'something-new' });
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
  });

  afterEach(() => {
    target.remove();
  });

  const envelope = () => ({
    stages: LEGEND,
    jobs: [
      job(),
      job({
        title_id: 9,
        name: 'Tampopo',
        status: 'failed',
        reason: 'enrich raised: the host answered 503\ntwice',
        actions: [RETRY, RETRY_FROM, ABANDON]
      })
    ]
  });

  const row = (id) => target.querySelector(`[data-title-id="${id}"]`);
  const buttons = (id) => [...row(id).querySelectorAll('button')].map((b) => b.textContent.trim());

  it('draws the legend and each reason verbatim, and only the buttons each job admits', async () => {
    vi.mocked(get).mockResolvedValue(envelope());
    const app = mount(AcquisitionBoard, { target, props: {} });
    await settle();
    try {
      const legend = [...target.querySelectorAll('[data-testid="board-stage"]')].map((e) => e.textContent);
      expect(legend).toEqual(NAMES);
      const reason = (id) => row(id).querySelector('[data-testid="board-reason"]').textContent;
      expect(reason(4)).toBe(envelope().jobs[0].reason);
      expect(reason(9)).toBe(envelope().jobs[1].reason);
      expect(row(4).getAttribute('data-status')).toBe('parked');
      expect(row(9).getAttribute('data-status')).toBe('failed');
      expect(row(4).className).not.toBe(row(9).className);
      expect(buttons(4)).not.toContain('Retry');
      expect(buttons(4)).toEqual(expect.arrayContaining(['Retry from stage', 'Abandon']));
      expect(buttons(9)).toEqual(expect.arrayContaining(['Retry', 'Retry from stage', 'Abandon']));
      const options = [...row(4).querySelectorAll('option')].map((o) => o.textContent);
      expect(options).toEqual(['1 alpha', '2 beta', '3 gamma', '4 delta four']);
    } finally {
      unmount(app);
    }
  });

  it("shows a refused action's sentence beside its job, whole, and reads the board again", async () => {
    const sentence = 'a plain retry is for a failed job; this one is parked (decision 336)';
    vi.mocked(get).mockResolvedValue(envelope());
    vi.mocked(api).mockRejectedValue(new Error(sentence));
    const app = mount(AcquisitionBoard, { target, props: {} });
    await settle();
    try {
      [...row(9).querySelectorAll('button')].find((b) => b.textContent.trim() === 'Retry').click();
      await settle();
      expect(vi.mocked(api)).toHaveBeenCalledWith('/admin/acquisition/9/retry', {
        method: 'POST',
        body: undefined
      });
      expect(row(9).querySelector('[data-testid="board-refusal"]').textContent).toBe(sentence);
      expect(row(4).querySelector('[data-testid="board-refusal"]')).toBe(null);
      expect(vi.mocked(get)).toHaveBeenCalledTimes(2);
    } finally {
      unmount(app);
    }
  });

  it('asks before it abandons, and retries from the stage the operator picked', async () => {
    vi.mocked(get).mockResolvedValue(envelope());
    vi.mocked(api).mockResolvedValue({ job: job() });
    const app = mount(AcquisitionBoard, { target, props: {} });
    await settle();
    try {
      const press = (label) =>
        [...row(4).querySelectorAll('button')].find((b) => b.textContent.trim() === label).click();
      press('Abandon');
      await settle();
      expect(vi.mocked(api), 'the first tap only asks').not.toHaveBeenCalled();
      press('Yes, abandon');
      await settle();
      expect(vi.mocked(api)).toHaveBeenCalledWith('/admin/acquisition/4/abandon', {
        method: 'POST',
        body: undefined
      });
      const select = row(4).querySelector('select');
      select.value = '2';
      select.dispatchEvent(new Event('change', { bubbles: true }));
      await settle();
      press('Retry from stage');
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
