/**
 * @vitest-environment jsdom
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/api.js', () => ({ api: vi.fn(), get: vi.fn(), post: vi.fn() }));

import { get, post } from '$lib/api.js';
import BundleImport from './components/BundleImport.svelte';
import MovieDataPage from '../routes/admin/movie-data/+page.svelte';
import { session } from './session.svelte.js';
import {
  FAILED,
  IDLE,
  IMPORTED,
  LIVE,
  POLL_DEADLINE_MS,
  POLL_INTERVAL_MS,
  POLL_READ_TIMEOUT_MS,
  RESTART,
  RUNNING,
  UNKNOWN,
  UNKNOWN_OUTCOME,
  VALIDATED,
  detailLine,
  detailPairs,
  importDisabled,
  phaseForImportError,
  phaseOfJob,
  pollImportJob,
  servedAfterImport,
  stepsLit
} from './bundleImport.svelte.js';

/** `ImportReport.as_dict()`, every key, as `api/artifacts.py` returns it. */
const report = (over = {}) => ({
  bundle_version: 'test-v1',
  vocabulary_version: 'v1',
  ok: true,
  counts: {},
  unmapped_columns: {},
  skipped_tables: [],
  findings: [],
  ...over
});

const finding = (severity, rule, message, detail = {}) => ({ severity, rule, message, detail });

/** `POST /api/admin/bundle/import` -> 202, as the route answers it. */
const accepted = (over = {}) => ({
  bundle_version: 'test-v1',
  job_id: 7,
  phase: 'queued',
  report: report(),
  text: '',
  poll: '/api/admin/bundle/state',
  ...over
});

/** `GET /api/admin/bundle/state`, every key the Movie data page reads. */
const bundleState = (over = {}) => ({
  bundles: [],
  active: null,
  loaded: null,
  restart_required: false,
  broken: false,
  missing_path: null,
  rebuild_set: ['placement.run_rebuild'],
  import_job: null,
  ...over
});

/** One `job_run` row as `_running_import` renders it. */
const job = (over = {}) => ({
  job_id: 7,
  phase: 'running',
  bundle_version: 'test-v1',
  path: '/data/import',
  started_at: '2026-09-12T10:00:00Z',
  finished_at: null,
  ok: null,
  report: null,
  text: '',
  ...over
});

/** An `ApiError` as the component sees it: `api.js` is mocked, so its class is not here. */
const apiError = (message, status, detail) => Object.assign(new Error(message), { status, detail });

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
  for (let i = 0; i < 40; i++) await Promise.resolve();
  flushSync();
}

const press = async (label) => {
  const button = [...target.querySelectorAll('button')].find((b) => b.textContent.includes(label));
  expect(button, `no button named ${label}`).toBeDefined();
  button.click();
  await settle();
};

const importButton = () =>
  [...target.querySelectorAll('button')].find((b) => b.textContent.includes('Import and activate'));

// Named by its idle label, the only state it can be pressed in.
const validateButton = () =>
  [...target.querySelectorAll('button')].find((b) => b.textContent.includes('Validate bundle'));

const box = () => target.querySelector('.box');
const lit = () => target.querySelectorAll('.step.on').length;

describe('the import button', () => {
  it('stays dark on an idle screen, where nothing has been validated', () => {
    expect(importDisabled({ phase: IDLE, report: null })).toBe(true);
  });

  it('is armed by a positive validation report and by nothing else', () => {
    expect(importDisabled({ phase: VALIDATED, report: report() })).toBe(false);
    expect(importDisabled({ phase: VALIDATED, report: report({ ok: false }) })).toBe(true);
    // An absent `ok` is not a green light: this object crosses the wire as JSON.
    expect(importDisabled({ phase: VALIDATED, report: { findings: [] } })).toBe(true);
  });

  it('stays dark while the import is running in the worker', () => {
    // A second press would race the first import for the staging tree.
    expect(importDisabled({ phase: RUNNING, report: report() })).toBe(true);
  });

  it('stays dark once the bundle is imported', () => {
    expect(importDisabled({ phase: IMPORTED, report: report() })).toBe(true);
  });

  it('stays dark after a failed import', () => {
    expect(importDisabled({ phase: FAILED, report: report({ ok: false }) })).toBe(true);
    // A transport failure leaves `report` null, which the old rule let through.
    expect(importDisabled({ phase: FAILED, report: null })).toBe(true);
  });

  it('stays dark when the outcome of an import is unknown', () => {
    expect(importDisabled({ phase: UNKNOWN, report: report() })).toBe(true);
    expect(importDisabled({ phase: UNKNOWN, report: null })).toBe(true);
  });

  it('stays dark while a request of its own is in flight', () => {
    expect(importDisabled({ busy: true, phase: VALIDATED, report: report() })).toBe(true);
  });
});

describe('the poll', () => {
  const reader = (...payloads) => {
    let i = 0;
    return vi.fn(async () => {
      const next = payloads[Math.min(i, payloads.length - 1)];
      i += 1;
      if (next instanceof Error) throw next;
      return next;
    });
  };
  const noSleep = { sleep: async () => {}, intervalMs: 0 };

  it('stops at the first terminal phase and carries the report the worker stored', async () => {
    const stored = report({ findings: [finding('note', 'stage', 'artifacts staged to /data')] });
    const read = reader(
      bundleState({ import_job: job() }),
      bundleState({ import_job: job({ phase: 'active', ok: true, report: stored }) })
    );
    const outcome = await pollImportJob(read, 7, noSleep);
    expect(outcome.phase).toBe(IMPORTED);
    expect(outcome.job.report).toBe(stored);
    expect(read).toHaveBeenCalledTimes(2);
  });

  it('keeps asking while the job is queued and while it is running', async () => {
    const read = reader(
      bundleState({ import_job: job({ phase: 'queued' }) }),
      bundleState({ import_job: job({ phase: 'running' }) }),
      bundleState({ import_job: job({ phase: 'failed', ok: false, report: report({ ok: false }) }) })
    );
    const outcome = await pollImportJob(read, 7, noSleep);
    expect(outcome.phase).toBe(FAILED);
    expect(read).toHaveBeenCalledTimes(3);
  });

  it('does not let one failed read decide an import it cannot see', async () => {
    // A lost read is not a failed import.
    const read = reader(
      new Error('Failed to fetch'),
      bundleState({ import_job: job({ phase: 'active', ok: true, report: report() }) })
    );
    const outcome = await pollImportJob(read, 7, noSleep);
    expect(outcome.phase).toBe(IMPORTED);
    expect(outcome.error).toBe('');
  });

  it("gives up at its own deadline, because the api client sets none", async () => {
    // `api.js` sets no timeout, so the poll owns its deadline; the clock is injected.
    let clock = 0;
    const read = reader(bundleState({ import_job: job({ phase: 'running' }) }));
    const outcome = await pollImportJob(read, 7, {
      intervalMs: 1_000,
      deadlineMs: 5_000,
      now: () => clock,
      sleep: async (ms) => {
        clock += ms;
      }
    });
    expect(outcome.phase).toBe(UNKNOWN);
    expect(read).toHaveBeenCalledTimes(6);
    // The row it gives up on is still this import's own.
    expect(outcome.job.job_id).toBe(7);
  });

  it("will not take another import's row as this one's answer", async () => {
    let clock = 0;
    const read = reader(bundleState({ import_job: job({ job_id: 99, phase: 'active', ok: true }) }));
    const outcome = await pollImportJob(read, 7, {
      intervalMs: 1_000,
      deadlineMs: 2_000,
      now: () => clock,
      sleep: async (ms) => {
        clock += ms;
      }
    });
    expect(outcome.phase).toBe(UNKNOWN);
    // Nor is the foreign row handed back: the caller would render its report as this screen's.
    expect(outcome.job).toBe(null);
  });

  it('answers for the row it was given, with no off-switch on the way it is chosen', async () => {
    // No id is no match: the guard has no off-switch.
    let clock = 0;
    const read = reader(bundleState({ import_job: job({ phase: 'active', ok: true }) }));
    const outcome = await pollImportJob(read, undefined, {
      intervalMs: 1_000,
      deadlineMs: 2_000,
      now: () => clock,
      sleep: async (ms) => {
        clock += ms;
      }
    });
    expect(outcome.phase).toBe(UNKNOWN);
    expect(outcome.job).toBe(null);
  });

  it('stops for good when the screen that asked for it is gone', async () => {
    // Destroying a component does not stop its async poll; `null` is the absence of an outcome.
    let clock = 0;
    let gone = false;
    const read = reader(bundleState({ import_job: job({ phase: 'running' }) }));
    // The deadline is injected too, so a poll that ignored the departure fails this case.
    const outcome = await pollImportJob(read, 7, {
      intervalMs: 1_000,
      deadlineMs: 5_000,
      now: () => clock,
      sleep: async (ms) => {
        clock += ms;
        gone = true;
      },
      abandoned: () => gone
    });
    expect(outcome).toBe(null);
    // One read, then the sleep that ends it.
    expect(read).toHaveBeenCalledTimes(1);
  });

  it("reads the server's own phase names and refuses to guess at one it does not know", () => {
    expect(phaseOfJob(null)).toBe(null);
    expect(phaseOfJob(job({ phase: 'queued' }))).toBe(RUNNING);
    expect(phaseOfJob(job({ phase: 'active' }))).toBe(IMPORTED);
    expect(phaseOfJob(job({ phase: 'failed' }))).toBe(FAILED);
    expect(phaseOfJob(job({ phase: 'reticulating' }))).toBe(UNKNOWN);
  });
});

describe('a refused import and a lost one', () => {
  it('tells an answer the server gave apart from a request that never arrived', () => {
    // A 4xx means nothing was enqueued; a 5xx or a thrown fetch might have queued a job.
    expect(phaseForImportError(apiError('report says no', 422))).toBe(FAILED);
    expect(phaseForImportError(apiError('already running', 409))).toBe(FAILED);
    expect(phaseForImportError(apiError('bad path', 400))).toBe(FAILED);
    expect(phaseForImportError(apiError('Bad Gateway', 502))).toBe(UNKNOWN);
    expect(phaseForImportError(new Error('Failed to fetch'))).toBe(UNKNOWN);
  });

  it('does not advance the steps strip past the report on a failure', () => {
    expect(stepsLit(VALIDATED)).toBe(2);
    expect(stepsLit(RUNNING)).toBe(3);
    expect(stepsLit(IMPORTED)).toBe(4);
    expect(stepsLit(FAILED)).toBe(2);
    expect(stepsLit(UNKNOWN)).toBe(2);
    // A refusal with no report must not light the step named "report".
    expect(stepsLit(FAILED, false)).toBe(1);
    expect(stepsLit(IDLE)).toBe(0);
  });
});

describe("a failing finding's detail", () => {
  it('names every key, in the order the report renders them', () => {
    const f = finding('fail', 'rule7-denylist', 'bundle contains 3 denied table(s)', {
      tables: ['review_bak', 'title_good'],
      database: 'reviews.sqlite'
    });
    expect(detailPairs(f)).toEqual([
      ['database', 'reviews.sqlite'],
      ['tables', 'review_bak, title_good']
    ]);
    expect(detailPairs(finding('fail', 'r', 'm'))).toEqual([]);
  });

  it('excerpts a long list the way the stored report does, total and all', () => {
    // `importer/report._detail_value`: five items and the total.
    expect(detailLine([1, 2, 3, 4, 5, 6, 7])).toBe('1, 2, 3, 4, 5, ... (7 total)');
    expect(detailLine(42)).toBe('42');
    expect(detailLine('x'.repeat(120)).endsWith('...')).toBe(true);
    expect(detailLine('x'.repeat(120)).length).toBe(96);
  });
});

describe("Movie data's import control", () => {
  const open = async () => {
    const app = mount(BundleImport, { target, props: {} });
    await settle();
    return app;
  };

  it('renders the report the worker stored rather than the one the request returned', async () => {
    // The 202 carries the validation report; the migration report is the one the worker stored.
    const stored = report({
      findings: [finding('note', 'stage', 'artifacts staged to /data/artifacts/test-v1')]
    });
    vi.mocked(post).mockResolvedValueOnce({ report: report(), text: '' });
    vi.mocked(post).mockResolvedValueOnce(accepted());
    vi.mocked(get).mockResolvedValue(
      bundleState({ import_job: job({ phase: 'active', ok: true, report: stored }) })
    );
    const app = await open();
    try {
      await press('Validate bundle');
      await press('Import and activate');
      expect(box().getAttribute('data-phase')).toBe(IMPORTED);
      // The options carry the per-read deadline; this line is about the endpoint.
      expect(vi.mocked(get)).toHaveBeenCalledWith('/admin/bundle/state', expect.anything());
      expect(target.querySelector('.finding .msg').textContent).toContain(
        'artifacts staged to /data/artifacts/test-v1'
      );
      expect(lit()).toBe(4);
    } finally {
      unmount(app);
    }
  });

  it('holds the destructive button down while the import is still running', async () => {
    // Initialised so svelte-check sees it is callable; this promise holds the component in running.
    let release = (/** @type {any} */ state) => state;
    vi.mocked(post).mockResolvedValueOnce({ report: report(), text: '' });
    vi.mocked(post).mockResolvedValueOnce(accepted());
    vi.mocked(get).mockImplementationOnce(() => new Promise((resolve) => (release = resolve)));
    const app = await open();
    try {
      await press('Validate bundle');
      expect(importButton().disabled, 'an ok report arms it, and only that').toBe(false);
      await press('Import and activate');
      expect(box().getAttribute('data-phase')).toBe(RUNNING);
      expect(importButton().disabled).toBe(true);
      expect(lit(), 'the swap is under way and the strip says so').toBe(3);
      release(bundleState({ import_job: job({ phase: 'active', ok: true, report: report() }) }));
      await settle();
      expect(box().getAttribute('data-phase')).toBe(IMPORTED);
      expect(importButton().disabled).toBe(true);
    } finally {
      unmount(app);
    }
  });

  it('does not re-arm the destructive button when the import request dies in transit', async () => {
    // A transport failure used to leave the destructive control live.
    vi.mocked(post).mockResolvedValueOnce({ report: report(), text: '' });
    vi.mocked(post).mockRejectedValueOnce(apiError('Bad Gateway', 502));
    const app = await open();
    try {
      await press('Validate bundle');
      expect(importButton().disabled).toBe(false);
      await press('Import and activate');
      expect(importButton().disabled).toBe(true);
      expect(box().getAttribute('data-phase')).toBe(UNKNOWN);
      expect(target.querySelector('[data-unknown]').textContent).toContain(UNKNOWN_OUTCOME);
      expect(target.querySelector('.err:not([data-unknown])').textContent).toContain('Bad Gateway');
      // The validation report stays, and the strip stops where this page stopped watching.
      expect(target.querySelector('.verdict').textContent).toBe('valid');
      expect(lit(), 'no swap was watched, so the strip does not claim one').toBe(2);
    } finally {
      unmount(app);
    }
  });

  it('says why a refusal refused, even with a report still on screen', async () => {
    // A 409 after a validate used to print nothing while a report was on screen.
    vi.mocked(post).mockResolvedValueOnce({ report: report(), text: '' });
    vi.mocked(post).mockRejectedValueOnce(
      apiError('another bundle import is already running on this install', 409, 'a string detail')
    );
    const app = await open();
    try {
      await press('Validate bundle');
      await press('Import and activate');
      expect(box().getAttribute('data-phase')).toBe(FAILED);
      expect(target.querySelector('.err').textContent).toContain('already running');
      expect(importButton().disabled).toBe(true);
    } finally {
      unmount(app);
    }
  });

  it('names the tables a denied-table failure found, and not just the count', async () => {
    vi.mocked(post).mockResolvedValueOnce({
      report: report({
        ok: false,
        findings: [
          finding('fail', 'rule7-denylist', 'bundle contains 2 denied table(s)', {
            database: 'reviews.sqlite',
            tables: ['review_bak', 'title_good']
          })
        ]
      }),
      text: ''
    });
    const app = await open();
    try {
      await press('Validate bundle');
      const details = [...target.querySelectorAll('.detail')].map((d) => d.textContent);
      expect(details).toHaveLength(2);
      expect(details[0]).toContain('reviews.sqlite');
      expect(details[1]).toContain('review_bak, title_good');
      expect(importButton().disabled, 'a rejected report never arms the import').toBe(true);
    } finally {
      unmount(app);
    }
  });

  it('asks for a bundle directory or an archive, which is what the importer now takes', async () => {
    const app = await open();
    try {
      expect(target.querySelector('label').textContent).toContain('.tar.zst');
    } finally {
      unmount(app);
    }
  });

  const importTo = async (state) => {
    vi.mocked(post).mockResolvedValueOnce({ report: report(), text: '' });
    vi.mocked(post).mockResolvedValueOnce(accepted());
    vi.mocked(get).mockResolvedValue(state);
    const app = await open();
    await press('Validate bundle');
    await press('Import and activate');
    return app;
  };

  it('says the imported bundle is live when the backend has loaded it, and names no restart', async () => {
    // The backend loads the flip on the read that reports it (decision 497).
    const app = await importTo(
      bundleState({
        active: 'test-v1',
        loaded: { version: 'test-v1' },
        import_job: job({ phase: 'active', ok: true, report: report() })
      })
    );
    try {
      expect(box().getAttribute('data-phase')).toBe(IMPORTED);
      expect(target.querySelector('[data-served="live"]').textContent).toContain('test-v1 is live');
      expect(box().textContent).not.toMatch(/restart backend|docker compose/i);
    } finally {
      unmount(app);
    }
  });

  it('names the command only when the server says a restart is owed', async () => {
    const app = await importTo(
      bundleState({
        active: 'test-v1',
        loaded: null,
        restart_required: true,
        import_job: job({ phase: 'active', ok: true, report: report() })
      })
    );
    try {
      const owed = target.querySelector('[data-served="restart"]');
      expect(owed.textContent).toContain('docker compose restart backend worker');
      expect(target.querySelector('[data-served="live"]')).toBeNull();
    } finally {
      unmount(app);
    }
  });
});

describe('what an import leaves served', () => {
  it('reads the terminal state payload, and says nothing it cannot read', () => {
    const state = (over) => bundleState({ active: 'test-v1', ...over });
    expect(servedAfterImport(state({ loaded: { version: 'test-v1' } }))).toBe(LIVE);
    expect(servedAfterImport(state({ loaded: null, restart_required: true }))).toBe(RESTART);
    // Broken is the restore banner's, and an older store still loaded is not "live".
    expect(servedAfterImport(state({ loaded: null, broken: true }))).toBeNull();
    expect(servedAfterImport(state({ loaded: { version: 'test-v0' } }))).toBeNull();
    expect(servedAfterImport(undefined)).toBeNull();
  });

  it('hands the terminal payload back with the phase, because that read is the one that re-pinned', async () => {
    const terminal = bundleState({
      active: 'test-v1',
      loaded: { version: 'test-v1' },
      import_job: job({ phase: 'active', ok: true, report: report() })
    });
    const outcome = await pollImportJob(async () => terminal, 7, {
      sleep: async () => {},
      intervalMs: 0
    });
    expect(outcome.state).toBe(terminal);
  });
});

describe('the Movie data page', () => {
  const pageState = (over = {}) => bundleState({ active: 'test-v1', bundles: [], ...over });

  const openPage = async (state) => {
    vi.mocked(get).mockImplementation(async (path) => {
      if (path === '/admin/bundle/state') return state;
      throw new Error('no sources in this fixture');
    });
    const app = mount(MovieDataPage, { target, props: {} });
    await settle();
    return app;
  };

  const warnings = () => [...target.querySelectorAll('.warn')].map((w) => w.textContent);

  it('renders the restore instruction rather than the restart one when the directory is gone', async () => {
    // `restart_required` excludes `broken`, so the two banners never render together (decision 258).
    const app = await openPage(
      pageState({ broken: true, missing_path: '/data/artifacts/test-v1', restart_required: false })
    );
    try {
      const shown = warnings();
      expect(shown).toHaveLength(1);
      expect(shown[0]).toContain('/data/artifacts/test-v1');
      expect(shown[0]).toMatch(/Restore \/data\/artifacts from backup/);
      expect(shown[0]).toMatch(/import test-v1 again/);
      expect(shown[0]).toMatch(/restages/);
      expect(shown[0]).not.toMatch(/restart backend and worker to load/);
    } finally {
      unmount(app);
    }
  });

  it('renders the restart instruction alone once the files are there and the row has moved', async () => {
    const app = await openPage(
      pageState({ restart_required: true, loaded: { version: 'test-v0' }, broken: false })
    );
    try {
      const shown = warnings();
      expect(shown).toHaveLength(1);
      expect(shown[0]).toContain('restart backend and worker');
      expect(shown[0]).not.toMatch(/restore/);
    } finally {
      unmount(app);
    }
  });

  it('adopts an import that was already running when this page loaded', async () => {
    // A reload mid-import, or a second device, finds the import already running.
    const stored = report({ findings: [finding('note', 'stage', 'artifacts staged to /data')] });
    /** @type {(value: any) => void} */
    let release = (value) => value;
    const reads = [
      pageState({ import_job: job({ phase: 'running' }) }),
      new Promise((resolve) => (release = resolve))
    ];
    let i = 0;
    vi.mocked(get).mockImplementation(async (path) => {
      if (path === '/admin/bundle/state') return reads[Math.min(i++, reads.length - 1)];
      if (path === '/admin/data/sources') throw new Error('no sources in this fixture');
      // The boot reads: the adopted import lands and `onImported` bootstraps the shell.
      return {};
    });
    const app = mount(MovieDataPage, { target, props: {} });
    await settle();
    try {
      expect(box().getAttribute('data-phase')).toBe(RUNNING);
      expect(importButton().disabled, 'and the button is dark for all of it').toBe(true);
      expect(lit(), 'the swap is under way and the strip says so').toBe(3);
      release(pageState({ import_job: job({ phase: 'active', ok: true, report: stored }) }));
      await settle();
      expect(box().getAttribute('data-phase')).toBe(IMPORTED);
      expect(target.querySelector('.finding .msg').textContent).toContain('artifacts staged');
    } finally {
      unmount(app);
    }
  });

  it('holds the validate button down for an import it adopted rather than pressed', async () => {
    // An adopted watch sets the phase without `busy`, so Validate must be dark too.
    /** @type {(value: any) => void} */
    let release = (value) => value;
    const reads = [
      pageState({ import_job: job({ phase: 'running' }) }),
      new Promise((resolve) => (release = resolve))
    ];
    let i = 0;
    vi.mocked(get).mockImplementation(async (path) => {
      if (path === '/admin/bundle/state') return reads[Math.min(i++, reads.length - 1)];
      if (path === '/admin/data/sources') throw new Error('no sources in this fixture');
      return {};
    });
    // What the server would answer: an install mid-import still validates a bundle ok.
    vi.mocked(post).mockResolvedValue({ report: report(), text: '' });
    const app = mount(MovieDataPage, { target, props: {} });
    await settle();
    try {
      expect(box().getAttribute('data-phase')).toBe(RUNNING);
      expect(validateButton().disabled, 'the adopted watch owns the phase').toBe(true);
      await press('Validate bundle');
      expect(vi.mocked(post), 'and a dark button sends nothing').not.toHaveBeenCalled();
      expect(box().getAttribute('data-phase'), 'nothing else wrote the phase').toBe(RUNNING);
      expect(importButton().disabled, 'so the destructive control cannot re-arm').toBe(true);
      expect(lit(), 'and the strip still says a swap is under way').toBe(3);
      release(pageState({ import_job: job({ phase: 'active', ok: true, report: report() }) }));
      await settle();
      expect(box().getAttribute('data-phase')).toBe(IMPORTED);
    } finally {
      unmount(app);
    }
  });

  it('does not adopt an import that already finished, on every later visit', async () => {
    // The newest row outlives the import, so adopting any row would re-poll on every visit.
    const app = await openPage(
      pageState({ import_job: job({ phase: 'active', ok: true, report: report() }) })
    );
    try {
      expect(box().getAttribute('data-phase')).toBe(IDLE);
      expect(target.querySelector('.report'), 'and no stored report is resurrected').toBe(null);
      // Counted on the page's own two routes; the cards below read their own.
      const bundleReads = vi
        .mocked(get)
        .mock.calls.filter(([path]) => path === '/admin/bundle/state' || path === '/admin/data/sources');
      expect(bundleReads, 'state and sources, and no poll').toHaveLength(2);
    } finally {
      unmount(app);
    }
  });

  it('leaves no poll behind when the operator walks away mid-import', async () => {
    // The page is destroyed and rebuilt on a tab switch, and an async poll survives both.
    const onImported = vi.fn();
    /** @type {(value: any) => void} */
    let release = (value) => value;
    const reads = [new Promise((resolve) => (release = resolve))];
    vi.mocked(get).mockImplementation(async () => reads[0]);
    const app = mount(BundleImport, {
      target,
      props: { importJob: job({ phase: 'running' }), onImported }
    });
    await settle();
    expect(box().getAttribute('data-phase'), 'the adopted import is being watched').toBe(RUNNING);
    expect(vi.mocked(get)).toHaveBeenCalledTimes(1);

    unmount(app);
    // The in-flight read answers terminal after the screen is gone.
    release(bundleState({ import_job: job({ phase: 'active', ok: true, report: report() }) }));
    await settle();
    expect(onImported, 'a destroyed screen does not re-bootstrap the shell').not.toHaveBeenCalled();
    expect(vi.mocked(get), 'and it asks nothing further').toHaveBeenCalledTimes(1);
  });

  it('bounds each read of the state route, which the api client does not', async () => {
    // The poll's deadline is tested between reads, so each read needs its own.
    vi.mocked(get).mockImplementation(() => new Promise(() => {}));
    const app = mount(BundleImport, { target, props: { importJob: job({ phase: 'running' }) } });
    await settle();
    try {
      const [path, options] = vi.mocked(get).mock.calls[0];
      expect(path).toBe('/admin/bundle/state');
      expect(options.signal, 'every read carries a deadline of its own').toBeInstanceOf(
        AbortSignal
      );
      expect(options.signal.aborted).toBe(false);
      // Longer than one interval, shorter than the whole poll.
      expect(POLL_READ_TIMEOUT_MS).toBeGreaterThan(POLL_INTERVAL_MS);
      expect(POLL_READ_TIMEOUT_MS).toBeLessThan(POLL_DEADLINE_MS);
    } finally {
      unmount(app);
    }
  });
});

describe("the first-boot wizard's importer", () => {
  // The wizard hands no row down, and first boot is where the long import happens.
  afterEach(() => {
    session.user = null;
  });

  it('adopts a running import nobody handed it, the way Movie data does', async () => {
    session.user = { id: 1, name: 'admin', role: 'admin', must_change_password: false };
    const stored = report({ findings: [finding('note', 'stage', 'artifacts staged to /data')] });
    /** @type {(value: any) => void} */
    let release = (value) => value;
    const reads = [
      bundleState({ import_job: job({ phase: 'running' }) }),
      new Promise((resolve) => (release = resolve))
    ];
    let i = 0;
    vi.mocked(get).mockImplementation(async () => reads[Math.min(i++, reads.length - 1)]);
    const app = mount(BundleImport, { target, props: {} });
    await settle();
    try {
      expect(box().getAttribute('data-phase')).toBe(RUNNING);
      expect(validateButton().disabled, 'and Validate cannot write over the swap').toBe(true);
      expect(importButton().disabled, 'nor can the destructive button re-arm').toBe(true);
      release(bundleState({ import_job: job({ phase: 'active', ok: true, report: stored }) }));
      await settle();
      expect(box().getAttribute('data-phase')).toBe(IMPORTED);
      expect(target.querySelector('.finding .msg').textContent).toContain('artifacts staged');
    } finally {
      unmount(app);
    }
  });

  it('asks an admin route nothing when nobody is signed in', async () => {
    // The wizard is reachable signed out, and `/admin/bundle/state` is admin-gated.
    const app = mount(BundleImport, { target, props: {} });
    await settle();
    try {
      expect(vi.mocked(get)).not.toHaveBeenCalled();
      expect(box().getAttribute('data-phase')).toBe(IDLE);
    } finally {
      unmount(app);
    }
  });

  it('starts no poll when the step is left while its own read is in flight', async () => {
    // `onDestroy` has run by the time the read answers, so the decision is taken again after the await.
    session.user = { id: 1, name: 'admin', role: 'admin', must_change_password: false };
    /** @type {(value: any) => void} */
    let release = (value) => value;
    vi.mocked(get).mockReturnValue(new Promise((resolve) => (release = resolve)));
    const app = mount(BundleImport, { target, props: {} });
    await settle();
    unmount(app);
    release(bundleState({ import_job: job({ phase: 'running' }) }));
    await settle();
    expect(vi.mocked(get), 'the state read, and nothing after it').toHaveBeenCalledTimes(1);
  });

  it('stays idle, and silent, when the state route refuses the read', async () => {
    // A read nobody asked for cannot become an error banner about itself.
    session.user = { id: 1, name: 'admin', role: 'admin', must_change_password: false };
    vi.mocked(get).mockRejectedValue(Object.assign(new Error('Unauthorized'), { status: 401 }));
    const app = mount(BundleImport, { target, props: {} });
    await settle();
    try {
      expect(box().getAttribute('data-phase')).toBe(IDLE);
      expect(target.querySelector('.err'), 'and says nothing about it').toBe(null);
    } finally {
      unmount(app);
    }
  });
});
