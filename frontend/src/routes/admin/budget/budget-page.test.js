/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// A page store that follows `pushState`, so a sheet stays open once it has pushed its entry.
const nav = vi.hoisted(() => {
  let value = { url: new URL('http://localhost/admin/budget'), state: {} };
  const runs = new Set();
  return {
    page: {
      subscribe(run) {
        runs.add(run);
        run(value);
        return () => runs.delete(run);
      }
    },
    push(state) {
      value = { ...value, state };
      for (const run of runs) run(value);
    }
  };
});

vi.mock('$app/stores', () => ({ page: nav.page }));
vi.mock('$app/navigation', () => ({ goto: vi.fn(), pushState: (_url, state) => nav.push(state) }));
vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

import { get } from '$lib/api.js';
import BudgetPage from './+page.svelte';

const provider = (name, over = {}) => ({
  name,
  configured: true,
  has_api_key: true,
  secrets_unreadable: false,
  model: `${name}-model`,
  structured_output: 'structured outputs',
  price_basis: 'unknown',
  models: [],
  ...over
});

/** `GET /api/admin/llm`, trimmed to what the page reads. */
const llm = () => ({
  providers: [
    provider('anthropic', { configured: false, has_api_key: false }),
    provider('openai', { configured: false }),
    provider('gemini')
  ],
  settings: { extraction_provider: 'gemini', parallel: null, parallel_providers: null, passes: null },
  meter: {
    spent_usd: '0.31',
    unsettled_usd: '0',
    cap_usd: '2',
    remaining_usd: '1.69',
    period_start: '2026-09-01T00:00:00+02:00',
    period_end: '2026-10-01T00:00:00+02:00',
    tz: 'Europe/Berlin'
  },
  estimate: { per_title_usd: '0.032175', passes: 1, providers: ['gemini'], basis: [] },
  projected: { window_days: 30, titles: 3, monthly_usd: '0.0965', remaining_usd: '1.69' }
});

const flywheel = () => ({
  items: [],
  providers: [{ name: 'gemini', configured: true, reason: null }],
  defaults: { providers: ['gemini'], passes: 1 },
  meter: {}
});

function wire(over = {}) {
  const answers = {
    '/admin/llm': llm,
    '/admin/connectors': () => ({ sources: [], keyless: [] }),
    '/admin/flywheel': flywheel,
    ...over
  };
  vi.mocked(get).mockImplementation(async (path) => {
    if (path.startsWith('/admin/flywheel/quote')) return { launchable: false, reason: 'select rows first' };
    const answer = answers[path];
    if (!answer) throw new Error(`unexpected GET ${path}`);
    return answer();
  });
}

let target;

beforeEach(() => {
  nav.push({});
  target = document.createElement('div');
  document.body.appendChild(target);
  vi.mocked(get).mockReset();
  wire();
});

afterEach(() => {
  target.remove();
});

async function open() {
  const app = mount(BudgetPage, { target });
  for (let i = 0; i < 20; i++) await Promise.resolve();
  flushSync();
  return app;
}

describe('Budget and AI', () => {
  it('keeps the money in one place: meter, plan, providers and the queue', async () => {
    const app = await open();
    try {
      expect(target.querySelector('[data-testid="spend-meter"]').textContent).toContain(
        '$0.31 of $2.00 this month'
      );
      expect(target.querySelector('[data-testid="llm-extraction"]')).not.toBeNull();
      expect(target.querySelector('[data-testid="flywheel-queue"]')).not.toBeNull();
      const rows = [...target.querySelectorAll('[data-provider-row]')];
      expect(rows.map((r) => r.getAttribute('data-provider-row'))).toEqual([
        'anthropic',
        'openai',
        'gemini'
      ]);
      expect(rows.map((r) => r.textContent.replace(/\s+/g, ' ').trim())).toEqual([
        'Anthropic No key',
        'OpenAI No price',
        'Gemini In use'
      ]);
    } finally {
      unmount(app);
    }
  });

  it("opens a provider's key, model and price editor from its row", async () => {
    const app = await open();
    try {
      expect(target.querySelector('[data-provider]')).toBeNull();
      target.querySelector('[data-provider-row="gemini"]').click();
      flushSync();
      const card = target.querySelector('[data-provider="gemini"]');
      expect(card).not.toBeNull();
      expect(card.querySelector('input[type="password"]').value).toBe('');
      expect(card.textContent).toContain('Structured output');
    } finally {
      unmount(app);
    }
  });

  it('keeps the queue on the page when the spend read fails', async () => {
    wire({
      '/admin/llm': () => {
        throw new Error('Not Found');
      }
    });
    const app = await open();
    try {
      expect(target.textContent).toContain('Not Found');
      expect(target.querySelector('[data-testid="flywheel-queue"]')).not.toBeNull();
    } finally {
      unmount(app);
    }
  });
});
