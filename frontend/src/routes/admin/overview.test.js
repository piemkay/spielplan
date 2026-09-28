import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ get: vi.fn() }));

import { get } from '$lib/api.js';
import { attention, facts, health, manage, refresh, waiting } from './overview.svelte.js';

const NOW = Date.parse('2026-09-27T20:00:00Z');
const minutesAgo = (m) => new Date(NOW - m * 60_000).toISOString();

const ok = (name, minutes = 2) => ({
  name,
  started_at: minutesAgo(minutes),
  finished_at: minutesAgo(minutes),
  ok: true,
  detail: {}
});

/** A household with nothing wrong: every read answered, every check green. */
function fine() {
  return {
    system: {
      backup: { at: minutesAgo(14 * 60), bytes: 196_000_000, stale: false, stale_after_hours: 36 },
      jobs: [ok('jellyfin-seen-sync'), ok('storage-check'), ok('nightly-backup', 14 * 60)],
      secrets: { configured: true, fingerprint: '0123456789ab', key_id: 'k1', unreadable: false },
      queue: { by_state: { pending: 0, leased: 0, done: 4, failed: 0, skipped: 0 }, by_kind: [] },
      last_syncs: [{ name: 'jellyfin-seen-sync', connector: 'Jellyfin', at: minutesAgo(4) }],
      acquisition: { queued: 0, running: 0, parked: 0, ready: 12, failed: 0, abandoned: 0 }
    },
    llm: {
      meter: {
        spent_usd: '0.31',
        cap_usd: '2.00',
        remaining_usd: '1.69',
        period_end: '2026-09-30T22:00:00+00:00'
      },
      projected: { window_days: 30, monthly_usd: '0.30' }
    },
    sources: {
      sources: [{ name: 'tmdb', required: true, has_api_key: true, secrets_unreadable: false }]
    },
    jellyfin: { configured: true, secrets_unreadable: false },
    users: [{ id: 1 }, { id: 2 }, { id: 3 }],
    config: { bundle: { version: 'v20260926b', titles: 19071 } },
    errors: {}
  };
}

const keys = (f) => attention(f, NOW).map((item) => item.key);

describe('what needs the admin (decision 527)', () => {
  it('asks nothing of a household where everything is fine', () => {
    expect(attention(fine(), NOW)).toEqual([]);
    expect(health(fine(), NOW).map((row) => row.state)).toEqual(['ok', 'ok', 'ok', 'ok']);
    expect(manage(fine(), NOW).system).toBe('Healthy');
  });

  it("says when the budget runs out at this month's pace, and until when titles then wait", () => {
    const f = fine();
    f.llm.projected.monthly_usd = '28.67';
    const [budget] = attention(f, NOW);
    expect(budget.key).toBe('budget');
    expect(budget.headline).toBe('The AI budget runs out in about 2 days');
    expect(budget.body).toContain('about $28.67 a month');
    expect(budget.body).toContain('Once $2.00 is spent, they wait until October.');
    expect(budget.href).toBe('/admin/budget');
  });

  it('stays quiet when the cap outlasts the month, and speaks when no cap is set', () => {
    const f = fine();
    f.llm.projected.monthly_usd = '3.00';
    expect(keys(f), 'a $1.69 remainder at $0.10 a day lasts past the 1st').toEqual([]);
    f.llm.meter.cap_usd = null;
    f.llm.meter.remaining_usd = null;
    expect(attention(f, NOW)[0].headline).toBe('No AI budget is set');
  });

  it("calls the budget used up once what is left can't hold one title's two attempts", () => {
    const f = fine();
    f.llm.meter.remaining_usd = '0.05';
    f.llm.estimate = { per_title_usd: '0.032175' };
    expect(attention(f, NOW)[0]).toMatchObject({
      key: 'budget',
      headline: 'The AI budget is used up',
      body: 'New titles wait until October, unless you raise the limit.'
    });
    f.llm.estimate.per_title_usd = '0.02';
    expect(keys(f), 'two attempts at $0.02 fit in $0.05').toEqual([]);
    f.llm.estimate.per_title_usd = 'unknown';
    expect(keys(f), 'no price, no claim').toEqual([]);
  });

  it('counts parked and failed titles from the whole board', () => {
    const f = fine();
    f.system.acquisition = { ...f.system.acquisition, parked: 96, failed: 1 };
    const items = attention(f, NOW);
    expect(items.map((item) => item.headline)).toEqual([
      "1 new title couldn't be added",
      '96 new titles are waiting'
    ]);
    expect(items[1].body).toContain('12 are ready.');
    expect(waiting(f)).toBe(96);
    expect(manage(f, NOW).titles).toBe('96 waiting');
  });

  it('names a failed job, and a stalled one, but not one still running', () => {
    const f = fine();
    f.system.jobs.push({
      name: 'metadata-backfill',
      started_at: minutesAgo(3),
      finished_at: null,
      ok: null,
      detail: null
    });
    expect(keys(f), 'three minutes in, it is running').toEqual([]);
    f.system.jobs.at(-1).started_at = minutesAgo(90);
    expect(attention(f, NOW)[0].headline).toBe("Film info backfill didn't finish");
    f.system.jobs.push({ ...ok('art-lookup'), ok: false });
    expect(attention(f, NOW)[0].headline).toBe('2 background jobs stopped working');
    expect(health(f, NOW).find((row) => row.key === 'jobs').detail).toBe('3 of 5 healthy');
    expect(manage(f, NOW).system).toBe('Needs you');
  });

  it('flags a stale or missing backup, unreadable secrets and a missing required key', () => {
    const f = fine();
    f.system.backup = { at: null, bytes: null, stale: true, stale_after_hours: 36 };
    f.jellyfin.secrets_unreadable = true;
    f.sources.sources[0].has_api_key = false;
    expect(keys(f)).toEqual(['secrets', 'required-key', 'backup']);
    expect(attention(f, NOW)[1].headline).toBe('Film info needs a TMDB key');
    expect(health(f, NOW)[0]).toMatchObject({ label: 'Backups', detail: 'Never', state: 'warn' });
  });

  it('reads Jellyfin as healthy only while it is set up and synced within the hour', () => {
    const f = fine();
    expect(health(f, NOW)[1]).toMatchObject({ detail: 'Synced 4 min ago', state: 'ok' });
    f.system.last_syncs[0].at = minutesAgo(180);
    expect(health(f, NOW)[1]).toMatchObject({ detail: 'Last synced 3 h ago', state: 'warn' });
    f.jellyfin.configured = false;
    expect(health(f, NOW)[1]).toMatchObject({ detail: 'Not set up', state: 'warn' });
  });

  it('gives every Manage row a plain value', () => {
    expect(manage(fine(), NOW)).toEqual({
      titles: 'Up to date',
      people: '3',
      services: 'Jellyfin, film info',
      budget: '$0.31 of $2.00',
      movieData: '19,071 titles',
      corrections: '3 lists',
      system: 'Healthy'
    });
  });
});

describe('the reads behind them', () => {
  beforeEach(() => {
    vi.mocked(get).mockReset();
  });

  it('asks each endpoint once while a read of it is in flight', async () => {
    /** @type {(value: any) => void} */
    let answer = () => {};
    vi.mocked(get).mockImplementation(() => new Promise((resolve) => (answer = resolve)));
    const first = refresh(['system']);
    const second = refresh(['system']);
    expect(get).toHaveBeenCalledTimes(1);
    answer({ jobs: [] });
    await Promise.all([first, second]);
    expect(facts.system).toEqual({ jobs: [] });

    vi.mocked(get).mockResolvedValue({ jobs: [1] });
    await refresh(['system']);
    expect(get, 'a settled read is asked again').toHaveBeenCalledTimes(2);
  });

  it('keeps what one read refused apart from what the others answered', async () => {
    vi.mocked(get).mockImplementation((path) =>
      path === '/admin/llm' ? Promise.reject(new Error('admin re-prompt')) : Promise.resolve({})
    );
    await refresh(['llm', 'users']);
    expect(facts.errors).toEqual({ llm: 'admin re-prompt' });
    expect(facts.users).toEqual({});
  });
});
